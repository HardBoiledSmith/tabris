#!/usr/bin/env bash
#
# run_create_poc.sh — Tabris Fargate 샌드박스 PoC용 AWS 리소스를 788968797716 계정에 생성한다.
#
# 생성 대상:
#   - ECR 이미지 (hbsmith/tabris:latest, ARM64)  ※ 기존 :latest 등과 충돌하지 않도록 전용 태그
#   - S3 workspace 버킷 (prompt/input/cancel, lifecycle 1일)
#   - IAM: task role + execution role
#   - CloudWatch Logs 그룹
#   - ECS 클러스터 (FARGATE)
#   - 보안그룹 (기본 VPC, egress all)  + 기본 VPC 퍼블릭 서브넷 자동 탐지
#   - ECS Task Definition 등록 (tabris-sandbox)
#
# 봇은 EB가 아니라 기존 Vagrant에서 그대로 돌리고, run_server.py가 ecs.run_task()를 직접 호출한다.
# 시크릿은 RunTask env override로 주입하므로 Secrets Manager는 만들지 않는다.
#
# 사전 조건: aws CLI v2 + docker. AWS 자격증명은 788968797716 계정을 가리켜야 한다.
#            (예: export AWS_PROFILE=<788968797716 프로파일> 후 aws sso login)
#
# 사용법:  ./run_create_poc.sh
#          IMAGE_TAG=latest SKIP_BUILD=1 ./run_create_poc.sh   # 이미지 빌드/푸시 생략

set -euo pipefail

# ---------------------------------------------------------------------------
# 설정 (필요 시 환경변수로 override)
# ---------------------------------------------------------------------------
AWS_REGION="${AWS_REGION:-ap-northeast-2}"
export AWS_PROFILE="${AWS_PROFILE:-hbsmith-dv}"
ACCOUNT_ID="${ACCOUNT_ID:-788968797716}"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
ECR_REPO="hbsmith/tabris"
IMAGE_TAG="${IMAGE_TAG:-latest}"

# --- 봇 settings_local.py에서 버킷/클러스터/태스크 이름을 읽어 기본값으로 사용 ---
# 우선순위: 명시적 env > settings_local 값 > 스크립트 내장 기본값.
# 이렇게 PoC 리소스 이름을 봇 설정과 항상 일치시켜 불일치로 인한 AccessDenied를 막는다.
SETTINGS_FILE="${TABRIS_SETTINGS:-/etc/tabris/settings_local.py}"
if [[ ! -f "${SETTINGS_FILE}" ]]; then
  echo "❌ settings_local.py를 찾을 수 없습니다: ${SETTINGS_FILE}" >&2
  echo "   TABRIS_SETTINGS 로 경로를 지정하거나 봇 설정이 있는 호스트에서 실행하세요." >&2
  exit 1
fi
echo "settings_local 로드: ${SETTINGS_FILE}"
eval "$(python3 - "${SETTINGS_FILE}" <<'PY'
import runpy, shlex, sys
try:
    cfg = runpy.run_path(sys.argv[1])
except Exception as exc:
    sys.stderr.write(f'settings_local 파싱 실패: {exc}\n')
    sys.exit(0)
mapping = {
    'CFG_WORKSPACE_BUCKET':   'WORKSPACE_S3_BUCKET',
    'CFG_MEMORY_BUCKET':      'MEMORY_S3_BUCKET',
    'CFG_ARTIFACTS_BUCKET':   'ARTIFACTS_S3_BUCKET',
    'CFG_DOCUMENTS_BUCKET':   'DOCUMENTS_S3_BUCKET',
    'CFG_ARTIFACTS_BASE_URL': 'ARTIFACTS_BASE_URL',
    'CFG_CLUSTER':            'ECS_CLUSTER',
    'CFG_TASK_FAMILY':        'ECS_SANDBOX_TASK_DEFINITION',
    # 워밍 풀 워커는 RunTask override가 없으므로 시크릿을 task def env로 상주시켜야 한다.
    # (보안 수준은 기존 RunTask override 평문 주입과 동일.)
    'CFG_ANTHROPIC_API_KEY':  'ANTHROPIC_API_KEY',
    'CFG_SLACK_BOT_TOKEN':    'SLACK_BOT_TOKEN',
    'CFG_NERV_MCP_TOKEN':     'NERV_MCP_TOKEN',
    'CFG_GITHUB_PAT':         'GITHUB_PAT',
    'CFG_SENTRY_AUTH_TOKEN':  'SENTRY_AUTH_TOKEN',
    'CFG_JIRA_API_KEY':       'JIRA_API_KEY',
    'CFG_JIRA_API_USERNAME':  'JIRA_API_USERNAME',
}
for shvar, pykey in mapping.items():
    val = cfg.get(pykey)
    if val not in (None, ''):
        print(f'{shvar}={shlex.quote(str(val))}')
PY
)"

# 인프라 리소스 이름(설정 파일에 없음). PoC 전용이라 고정.
TASK_ROLE_NAME='tabris-sandbox-task-role'
EXEC_ROLE_NAME='tabris-ecs-execution-role'
LOG_GROUP='/ecs/tabris-sandbox'
SG_NAME='tabris-sandbox-sg'

# 워밍 풀 리소스(이름 고정) / 튜닝값(env로 override 가능).
QUEUE_NAME="${QUEUE_NAME:-tabris-sandbox-jobs.fifo}"
DLQ_NAME="${DLQ_NAME:-tabris-sandbox-jobs-dlq.fifo}"
SERVICE_NAME="${SERVICE_NAME:-tabris-sandbox-pool}"
MAX_JOBS="${MAX_JOBS:-2}"
MAX_LIFETIME_SEC="${MAX_LIFETIME_SEC:-2700}"
SQS_VISIBILITY_TIMEOUT_SEC="${SQS_VISIBILITY_TIMEOUT_SEC:-360}"
POOL_MAX_TASKS="${POOL_MAX_TASKS:-5}"            # autoscaling 상한
POOL_BUSINESS_MIN="${POOL_BUSINESS_MIN:-1}"      # 업무시간 warm 최소치
# 스케줄(UTC). KST 09–19시 = UTC 00–10시. 평일만.
SCHED_UP_CRON="${SCHED_UP_CRON:-cron(0 0 ? * MON-FRI *)}"
SCHED_DOWN_CRON="${SCHED_DOWN_CRON:-cron(0 10 ? * MON-FRI *)}"

# 봇과 공유하는 값은 settings_local.py에서만 읽는다(누락 시 명확히 실패).
WORKSPACE_BUCKET="${CFG_WORKSPACE_BUCKET:?settings_local.py에 WORKSPACE_S3_BUCKET가 없습니다}"
MEMORY_BUCKET="${CFG_MEMORY_BUCKET:?settings_local.py에 MEMORY_S3_BUCKET가 없습니다}"
ARTIFACTS_BUCKET="${CFG_ARTIFACTS_BUCKET:?settings_local.py에 ARTIFACTS_S3_BUCKET가 없습니다}"
DOCUMENTS_BUCKET="${CFG_DOCUMENTS_BUCKET:?settings_local.py에 DOCUMENTS_S3_BUCKET가 없습니다}"
ARTIFACTS_BASE_URL="${CFG_ARTIFACTS_BASE_URL:?settings_local.py에 ARTIFACTS_BASE_URL가 없습니다}"
CLUSTER="${CFG_CLUSTER:?settings_local.py에 ECS_CLUSTER가 없습니다}"
TASK_FAMILY="${CFG_TASK_FAMILY:?settings_local.py에 ECS_SANDBOX_TASK_DEFINITION가 없습니다}"

# 워밍 풀 워커는 task def env로 시크릿을 받는다(RunTask override 없음). settings_local에서만 읽는다.
ANTHROPIC_API_KEY="${CFG_ANTHROPIC_API_KEY:?settings_local.py에 ANTHROPIC_API_KEY가 없습니다}"
SLACK_BOT_TOKEN="${CFG_SLACK_BOT_TOKEN:?settings_local.py에 SLACK_BOT_TOKEN가 없습니다}"
NERV_MCP_TOKEN="${CFG_NERV_MCP_TOKEN:?settings_local.py에 NERV_MCP_TOKEN가 없습니다}"
GITHUB_PAT="${CFG_GITHUB_PAT:?settings_local.py에 GITHUB_PAT가 없습니다}"
SENTRY_AUTH_TOKEN="${CFG_SENTRY_AUTH_TOKEN:?settings_local.py에 SENTRY_AUTH_TOKEN가 없습니다}"
JIRA_API_KEY="${CFG_JIRA_API_KEY:?settings_local.py에 JIRA_API_KEY가 없습니다}"
JIRA_API_USERNAME="${CFG_JIRA_API_USERNAME:?settings_local.py에 JIRA_API_USERNAME가 없습니다}"
# Atlassian MCP Basic auth: base64("user:api_key") — run_server.py와 동일 규칙.
ATLASSIAN_ROVO_MCP_TOKEN="$(printf '%s:%s' "${JIRA_API_USERNAME}" "${JIRA_API_KEY}" | base64 | tr -d '\n')"

# aws_inspect 스킬이 assume하는 OrchestratorRole (read-only 체인 진입점)
ORCHESTRATOR_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/ai-agent/HBsmithAIAgent-InspectOrchestratorRole"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
TEMPLATE="${SCRIPT_DIR}/task_definition_sandbox.json"
ENV_OUT="${SCRIPT_DIR}/poc_resources.env"

log() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
aws() { command aws --region "${AWS_REGION}" "$@"; }

# ---------------------------------------------------------------------------
# 0. 자격증명 / 계정 확인
# ---------------------------------------------------------------------------
log "AWS 자격증명 확인"
CALLER_ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
if [[ "${CALLER_ACCOUNT}" != "${ACCOUNT_ID}" ]]; then
  echo "❌ 현재 자격증명 계정(${CALLER_ACCOUNT})이 PoC 계정(${ACCOUNT_ID})과 다릅니다." >&2
  echo "   AWS_PROFILE 을 ${ACCOUNT_ID} 계정으로 설정 후 다시 실행하세요." >&2
  exit 1
fi
echo "✅ account=${CALLER_ACCOUNT}"

# ---------------------------------------------------------------------------
# 1. ECR 이미지 빌드 & 푸시 (ARM64, 태그 latest)
# ---------------------------------------------------------------------------
if [[ "${SKIP_BUILD:-0}" == "1" ]]; then
  log "SKIP_BUILD=1 → 이미지 빌드/푸시 생략"
else
  log "ECR repo 확인/생성: ${ECR_REPO}"
  aws ecr describe-repositories --repository-names "${ECR_REPO}" >/dev/null 2>&1 \
    || aws ecr create-repository --repository-name "${ECR_REPO}" >/dev/null

  log "ECR 로그인: ${REGISTRY}"
  aws ecr get-login-password | docker login --username AWS --password-stdin "${REGISTRY}"

  log "이미지 빌드 (linux/arm64): ${REGISTRY}/${ECR_REPO}:${IMAGE_TAG}"
  docker build --platform linux/arm64 -t "${REGISTRY}/${ECR_REPO}:${IMAGE_TAG}" "${REPO_ROOT}"

  log "이미지 푸시"
  docker push "${REGISTRY}/${ECR_REPO}:${IMAGE_TAG}"
fi

# ---------------------------------------------------------------------------
# 2. S3 workspace 버킷
# ---------------------------------------------------------------------------
log "S3 workspace 버킷: ${WORKSPACE_BUCKET}"
if aws s3api head-bucket --bucket "${WORKSPACE_BUCKET}" 2>/dev/null; then
  echo "이미 존재 — 건너뜀"
else
  aws s3api create-bucket \
    --bucket "${WORKSPACE_BUCKET}" \
    --create-bucket-configuration "LocationConstraint=${AWS_REGION}" >/dev/null
  aws s3api put-public-access-block --bucket "${WORKSPACE_BUCKET}" \
    --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
fi
# lifecycle: runs/*(prompt·input)와 jobs/*(done·cancel 멱등 마커)를 1일 후 만료(코드 없는 청소).
aws s3api put-bucket-lifecycle-configuration --bucket "${WORKSPACE_BUCKET}" \
  --lifecycle-configuration '{
    "Rules": [
      {"ID": "expire-runs", "Filter": {"Prefix": "runs/"}, "Status": "Enabled", "Expiration": {"Days": 1}},
      {"ID": "expire-jobs", "Filter": {"Prefix": "jobs/"}, "Status": "Enabled", "Expiration": {"Days": 1}}
    ]
  }'

# ---------------------------------------------------------------------------
# 3. IAM 역할
# ---------------------------------------------------------------------------
ECS_TRUST='{
  "Version": "2012-10-17",
  "Statement": [{"Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"}, "Action": "sts:AssumeRole"}]
}'

log "IAM execution role: ${EXEC_ROLE_NAME}"
aws iam get-role --role-name "${EXEC_ROLE_NAME}" >/dev/null 2>&1 \
  || aws iam create-role --role-name "${EXEC_ROLE_NAME}" \
       --assume-role-policy-document "${ECS_TRUST}" >/dev/null
aws iam attach-role-policy --role-name "${EXEC_ROLE_NAME}" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy

log "IAM task role: ${TASK_ROLE_NAME}"
echo "  grant 대상 버킷 (settings_local.py 와 일치해야 함):"
echo "    workspace=${WORKSPACE_BUCKET}"
echo "    memory   =${MEMORY_BUCKET}"
echo "    artifacts=${ARTIFACTS_BUCKET}"
echo "    documents=${DOCUMENTS_BUCKET}"
aws iam get-role --role-name "${TASK_ROLE_NAME}" >/dev/null 2>&1 \
  || aws iam create-role --role-name "${TASK_ROLE_NAME}" \
       --assume-role-policy-document "${ECS_TRUST}" >/dev/null

TASK_POLICY="$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "WorkspaceBucket",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
      "Resource": ["arn:aws:s3:::${WORKSPACE_BUCKET}", "arn:aws:s3:::${WORKSPACE_BUCKET}/*"]
    },
    {
      "Sid": "MemoryBucket",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
      "Resource": ["arn:aws:s3:::${MEMORY_BUCKET}", "arn:aws:s3:::${MEMORY_BUCKET}/*"]
    },
    {
      "Sid": "ArtifactsBucket",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": ["arn:aws:s3:::${ARTIFACTS_BUCKET}/*"]
    },
    {
      "Sid": "DocumentsBucket",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:ListBucket"],
      "Resource": ["arn:aws:s3:::${DOCUMENTS_BUCKET}", "arn:aws:s3:::${DOCUMENTS_BUCKET}/*"]
    },
    {
      "Sid": "SandboxJobQueue",
      "Effect": "Allow",
      "Action": [
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:ChangeMessageVisibility",
        "sqs:GetQueueAttributes"
      ],
      "Resource": "arn:aws:sqs:${AWS_REGION}:${ACCOUNT_ID}:${QUEUE_NAME}"
    },
    {
      "Sid": "AwsInspectAssumeRole",
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": "${ORCHESTRATOR_ROLE_ARN}"
    }
  ]
}
JSON
)"
aws iam put-role-policy --role-name "${TASK_ROLE_NAME}" \
  --policy-name tabris-sandbox --policy-document "${TASK_POLICY}"

# ---------------------------------------------------------------------------
# 4. CloudWatch Logs 그룹
# ---------------------------------------------------------------------------
log "CloudWatch Logs 그룹: ${LOG_GROUP}"
aws logs create-log-group --log-group-name "${LOG_GROUP}" 2>/dev/null \
  && aws logs put-retention-policy --log-group-name "${LOG_GROUP}" --retention-in-days 14 \
  || echo "이미 존재 — 건너뜀"

# ---------------------------------------------------------------------------
# 5. ECS 클러스터
# ---------------------------------------------------------------------------
log "ECS 클러스터: ${CLUSTER}"
aws ecs create-cluster --cluster-name "${CLUSTER}" >/dev/null
echo "✅ cluster ready"

# ---------------------------------------------------------------------------
# 6. 네트워크 — 기본 VPC + 퍼블릭 서브넷 + 보안그룹
# ---------------------------------------------------------------------------
log "기본 VPC / 퍼블릭 서브넷 탐지"
VPC_ID="$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
  --query 'Vpcs[0].VpcId' --output text)"
if [[ -z "${VPC_ID}" || "${VPC_ID}" == "None" ]]; then
  echo "❌ 기본 VPC를 찾을 수 없습니다. ECS_SUBNET_IDS/ECS_SECURITY_GROUP_ID를 수동 지정하세요." >&2
  exit 1
fi
SUBNET_IDS="$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=${VPC_ID}" \
  --query 'Subnets[].SubnetId' --output text | tr '\t' ',')"
echo "VPC=${VPC_ID} subnets=${SUBNET_IDS}"

log "보안그룹: ${SG_NAME} (egress all)"
SG_ID="$(aws ec2 describe-security-groups \
  --filters "Name=group-name,Values=${SG_NAME}" "Name=vpc-id,Values=${VPC_ID}" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || true)"
if [[ -z "${SG_ID}" || "${SG_ID}" == "None" ]]; then
  SG_ID="$(aws ec2 create-security-group --group-name "${SG_NAME}" \
    --description 'tabris fargate sandbox PoC (egress only)' \
    --vpc-id "${VPC_ID}" --query 'GroupId' --output text)"
fi
echo "SG=${SG_ID}"

# ---------------------------------------------------------------------------
# 6.5 SQS FIFO 큐 + DLQ (워밍 풀 디스패치)
# ---------------------------------------------------------------------------
log "SQS DLQ: ${DLQ_NAME}"
DLQ_URL="$(aws sqs create-queue --queue-name "${DLQ_NAME}" \
  --attributes 'FifoQueue=true,ContentBasedDeduplication=false,MessageRetentionPeriod=1209600' \
  --query 'QueueUrl' --output text)"
DLQ_ARN="$(aws sqs get-queue-attributes --queue-url "${DLQ_URL}" \
  --attribute-names QueueArn --query 'Attributes.QueueArn' --output text)"
echo "DLQ=${DLQ_URL}"

log "SQS 큐: ${QUEUE_NAME}"
# visibilityTimeout은 워커 하트비트의 기준값과 맞춘다. maxReceiveCount=3 초과 시 DLQ로.
# 잡 본문은 그룹별 순서가 필요하므로 FIFO. dedup은 봇이 MessageDeduplicationId(job_id)로 명시.
REDRIVE="{\"deadLetterTargetArn\":\"${DLQ_ARN}\",\"maxReceiveCount\":\"3\"}"
QUEUE_URL="$(aws sqs create-queue --queue-name "${QUEUE_NAME}" \
  --attributes "$(cat <<JSON
{
  "FifoQueue": "true",
  "ContentBasedDeduplication": "false",
  "VisibilityTimeout": "${SQS_VISIBILITY_TIMEOUT_SEC}",
  "MessageRetentionPeriod": "1209600",
  "RedrivePolicy": "$(printf '%s' "${REDRIVE}" | sed 's/"/\\"/g')"
}
JSON
)" \
  --query 'QueueUrl' --output text)"
echo "QUEUE=${QUEUE_URL}"

# ---------------------------------------------------------------------------
# 7. Task Definition 등록 (ARM64)
# ---------------------------------------------------------------------------
log "Task Definition 렌더 & 등록: ${TASK_FAMILY}"
RENDERED="$(mktemp)"
export TASK_FAMILY ACCOUNT_ID TASK_ROLE_NAME EXEC_ROLE_NAME REGISTRY IMAGE_TAG \
       AWS_REGION WORKSPACE_BUCKET MEMORY_BUCKET ARTIFACTS_BUCKET ARTIFACTS_BASE_URL \
       DOCUMENTS_BUCKET LOG_GROUP \
       QUEUE_URL MAX_JOBS MAX_LIFETIME_SEC SQS_VISIBILITY_TIMEOUT_SEC \
       ANTHROPIC_API_KEY SLACK_BOT_TOKEN NERV_MCP_TOKEN ATLASSIAN_ROVO_MCP_TOKEN \
       GITHUB_PAT SENTRY_AUTH_TOKEN
python3 - "${TEMPLATE}" > "${RENDERED}" <<'PY'
import os, sys, string
tpl = string.Template(open(sys.argv[1]).read())
sys.stdout.write(tpl.substitute(os.environ))
PY
# IAM 전파 지연 대비 (execution/task role 인식까지 잠시 대기)
sleep 8
aws ecs register-task-definition --cli-input-json "file://${RENDERED}" \
  --query 'taskDefinition.taskDefinitionArn' --output text
rm -f "${RENDERED}"

# ---------------------------------------------------------------------------
# 7.5 ECS Service (워밍 풀, Fargate Spot) + Application Auto Scaling + 스케줄드
# ---------------------------------------------------------------------------
log "클러스터 capacity provider에 FARGATE_SPOT 연결"
aws ecs put-cluster-capacity-providers --cluster "${CLUSTER}" \
  --capacity-providers FARGATE FARGATE_SPOT \
  --default-capacity-provider-strategy capacityProvider=FARGATE_SPOT,weight=1 >/dev/null

NET_CONF="awsvpcConfiguration={subnets=[${SUBNET_IDS}],securityGroups=[${SG_ID}],assignPublicIp=ENABLED}"

log "ECS Service: ${SERVICE_NAME} (desiredCount=0 — 스케줄드/오토스케일이 조절)"
SVC_STATUS="$(aws ecs describe-services --cluster "${CLUSTER}" --services "${SERVICE_NAME}" \
  --query 'services[0].status' --output text 2>/dev/null || true)"
if [[ "${SVC_STATUS}" == "ACTIVE" ]]; then
  echo "이미 존재 — task def 갱신만 반영"
  aws ecs update-service --cluster "${CLUSTER}" --service "${SERVICE_NAME}" \
    --task-definition "${TASK_FAMILY}" >/dev/null
else
  aws ecs create-service \
    --cluster "${CLUSTER}" \
    --service-name "${SERVICE_NAME}" \
    --task-definition "${TASK_FAMILY}" \
    --desired-count 0 \
    --capacity-provider-strategy capacityProvider=FARGATE_SPOT,weight=1 \
    --network-configuration "${NET_CONF}" >/dev/null
fi
echo "✅ service ready"

# --- Application Auto Scaling (Step Scaling) ---
# 타깃 추적(raw 큐깊이)은 0→1 스케일아웃을 못 한다(metric==target이면 알람 미발화). 그래서
# step scaling으로 간다: 대기 메시지가 생기면 +1, 백로그(대기+처리중)가 0으로 지속되면 0으로 복귀.
RESOURCE_ID="service/${CLUSTER}/${SERVICE_NAME}"

# 배포 시점 floor 보정: 스케줄드 'up' cron은 한 번만 발화하므로, 업무시간 중에 배포하면
# 다음 cron까지 min이 0에 묶인다(=오늘 하루 0대). 현재 UTC가 업무시간(평일 00–10시)이면 즉시 min을 올린다.
INIT_MIN=0
DOW="$(date -u +%u)"; HOUR="$(date -u +%H)"
if [[ "${DOW}" -le 5 && "${HOUR}" -ge 0 && "${HOUR}" -lt 10 ]]; then
  INIT_MIN="${POOL_BUSINESS_MIN}"
fi
log "오토스케일 대상 등록: ${RESOURCE_ID} (min=${INIT_MIN} max=${POOL_MAX_TASKS})"
aws application-autoscaling register-scalable-target \
  --service-namespace ecs \
  --resource-id "${RESOURCE_ID}" \
  --scalable-dimension ecs:service:DesiredCount \
  --min-capacity "${INIT_MIN}" --max-capacity "${POOL_MAX_TASKS}" >/dev/null

# 구버전(타깃 추적) 정책이 남아 있으면 제거(재실행 멱등). 정책 삭제 시 연결 알람도 함께 사라진다.
aws application-autoscaling delete-scaling-policy --service-namespace ecs \
  --resource-id "${RESOURCE_ID}" --scalable-dimension ecs:service:DesiredCount \
  --policy-name tabris-sandbox-sqs-backlog >/dev/null 2>&1 || true

log "Step scaling 정책 + CloudWatch 알람"
SCALE_OUT_ARN="$(aws application-autoscaling put-scaling-policy \
  --service-namespace ecs --resource-id "${RESOURCE_ID}" --scalable-dimension ecs:service:DesiredCount \
  --policy-name tabris-pool-scale-out --policy-type StepScaling \
  --step-scaling-policy-configuration '{"AdjustmentType":"ChangeInCapacity","MetricAggregationType":"Maximum","Cooldown":180,"StepAdjustments":[{"MetricIntervalLowerBound":0,"ScalingAdjustment":1}]}' \
  --query 'PolicyARN' --output text)"
SCALE_IN_ARN="$(aws application-autoscaling put-scaling-policy \
  --service-namespace ecs --resource-id "${RESOURCE_ID}" --scalable-dimension ecs:service:DesiredCount \
  --policy-name tabris-pool-scale-in --policy-type StepScaling \
  --step-scaling-policy-configuration '{"AdjustmentType":"ExactCapacity","MetricAggregationType":"Maximum","Cooldown":60,"StepAdjustments":[{"MetricIntervalUpperBound":0,"ScalingAdjustment":0}]}' \
  --query 'PolicyARN' --output text)"

# scale-out: 대기 메시지(Visible)가 하나라도 있으면(1분) → +1. (notBreaching로 메시지 없으면 OK)
aws cloudwatch put-metric-alarm \
  --alarm-name tabris-pool-backlog-high \
  --alarm-description 'tabris pool: 대기 메시지 발생 → scale out' \
  --namespace AWS/SQS --metric-name ApproximateNumberOfMessagesVisible \
  --dimensions Name=QueueName,Value="${QUEUE_NAME}" \
  --statistic Maximum --period 60 --evaluation-periods 1 \
  --threshold 0 --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching --alarm-actions "${SCALE_OUT_ARN}" >/dev/null
# scale-in: 백로그(대기+처리중)가 0으로 5분 지속 → 0으로 복귀. 처리중(NotVisible)을 포함해
# 작업 중인 워커를 죽이지 않는다.
aws cloudwatch put-metric-alarm \
  --alarm-name tabris-pool-backlog-empty \
  --alarm-description 'tabris pool: 백로그(대기+처리중) 0 지속 → scale in 0' \
  --metrics "$(cat <<JSON
[
  {"Id":"visible","MetricStat":{"Metric":{"Namespace":"AWS/SQS","MetricName":"ApproximateNumberOfMessagesVisible","Dimensions":[{"Name":"QueueName","Value":"${QUEUE_NAME}"}]},"Period":60,"Stat":"Maximum"},"ReturnData":false},
  {"Id":"inflight","MetricStat":{"Metric":{"Namespace":"AWS/SQS","MetricName":"ApproximateNumberOfMessagesNotVisible","Dimensions":[{"Name":"QueueName","Value":"${QUEUE_NAME}"}]},"Period":60,"Stat":"Maximum"},"ReturnData":false},
  {"Id":"backlog","Expression":"visible+inflight","Label":"backlog","ReturnData":true}
]
JSON
)" \
  --evaluation-periods 5 --threshold 1 --comparison-operator LessThanThreshold \
  --treat-missing-data notBreaching --alarm-actions "${SCALE_IN_ARN}" >/dev/null

# 스케줄드: 업무시간 min 플로어를 올려 warm 워커 상시 1대, 그 외엔 0으로 비움(비용 절감).
log "스케줄드 스케일: UP='${SCHED_UP_CRON}' (min=${POOL_BUSINESS_MIN}) / DOWN='${SCHED_DOWN_CRON}' (min=0)"
aws application-autoscaling put-scheduled-action \
  --service-namespace ecs --resource-id "${RESOURCE_ID}" \
  --scalable-dimension ecs:service:DesiredCount \
  --scheduled-action-name tabris-pool-business-up \
  --schedule "${SCHED_UP_CRON}" \
  --scalable-target-action "MinCapacity=${POOL_BUSINESS_MIN},MaxCapacity=${POOL_MAX_TASKS}" >/dev/null
aws application-autoscaling put-scheduled-action \
  --service-namespace ecs --resource-id "${RESOURCE_ID}" \
  --scalable-dimension ecs:service:DesiredCount \
  --scheduled-action-name tabris-pool-offhours-down \
  --schedule "${SCHED_DOWN_CRON}" \
  --scalable-target-action "MinCapacity=0,MaxCapacity=${POOL_MAX_TASKS}" >/dev/null

# ---------------------------------------------------------------------------
# 8. 결과 출력 + settings_local 스니펫
# ---------------------------------------------------------------------------
cat > "${ENV_OUT}" <<ENV
# run_create_poc.sh 생성 결과 (생성 시점 스냅샷). run_terminate_poc.sh 가 참고한다.
AWS_REGION=${AWS_REGION}
ACCOUNT_ID=${ACCOUNT_ID}
WORKSPACE_BUCKET=${WORKSPACE_BUCKET}
CLUSTER=${CLUSTER}
TASK_FAMILY=${TASK_FAMILY}
TASK_ROLE_NAME=${TASK_ROLE_NAME}
EXEC_ROLE_NAME=${EXEC_ROLE_NAME}
LOG_GROUP=${LOG_GROUP}
SG_NAME=${SG_NAME}
SG_ID=${SG_ID}
VPC_ID=${VPC_ID}
SUBNET_IDS=${SUBNET_IDS}
IMAGE_TAG=${IMAGE_TAG}
SERVICE_NAME=${SERVICE_NAME}
QUEUE_NAME=${QUEUE_NAME}
QUEUE_URL=${QUEUE_URL}
DLQ_NAME=${DLQ_NAME}
DLQ_URL=${DLQ_URL}
ENV

log "완료 ✅  생성 정보 → ${ENV_OUT}"
cat <<SNIPPET

────────────────────────────────────────────────────────────────────
settings_local.py 에 아래를 반영하고 봇을 재시작하세요 (Vagrant):

WORKSPACE_S3_BUCKET = '${WORKSPACE_BUCKET}'
ECS_CLUSTER = '${CLUSTER}'
ECS_SANDBOX_TASK_DEFINITION = '${TASK_FAMILY}'
ECS_SUBNET_IDS = '${SUBNET_IDS}'
ECS_SECURITY_GROUP_ID = '${SG_ID}'
ECS_ASSIGN_PUBLIC_IP = 'ENABLED'

# 워밍 풀로 전환하려면 아래를 설정(빈 값이면 위 ECS_* 기반 1회용 RunTask 경로로 동작):
SQS_QUEUE_URL = '${QUEUE_URL}'

※ 봇은 aws CLI만 사용하므로 추가 pip 설치가 필요 없습니다.
   단, 봇 IAM(인스턴스 role 또는 hbsmith-dv)에 아래 권한이 있어야 합니다:
     - sqs:SendMessage  → ${QUEUE_NAME}
     - s3:PutObject     → ${WORKSPACE_BUCKET}/jobs/* (cancel 마커), runs/* (prompt/input)
   워커(task role)에는 본 스크립트가 SQS receive/delete/visibility 권한을 부여했습니다.
────────────────────────────────────────────────────────────────────
SNIPPET
