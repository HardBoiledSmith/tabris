#!/usr/bin/env bash
#
# run_create.sh — Tabris Fargate 샌드박스 운영 AWS 리소스를 op 계정(187063173014)에 생성한다.
#
# 생성 대상:
#   - ECR 이미지 (hbsmith/tabris:latest, ARM64)  ※ 공유 계정(591379657681) repo에 cross-account push
#   - S3 workspace 버킷 (prompt/input/cancel, lifecycle 1일)
#   - IAM: task role + execution role
#   - CloudWatch Logs 그룹
#   - ECS 클러스터 (FARGATE) + 워밍 풀 서비스/오토스케일
#   - ECS Task Definition 등록 (tabris-sandbox)
#   - 봇 EB role(tabris-test-ec2-role)에 디스패치 권한(SQS/S3/StopTask) 인라인 부착
#
# 네트워크(서브넷·보안그룹·퍼블릭 IP 여부)는 settings_local.py에서 읽는다(기본 VPC 자동 탐지 안 함):
#   ECS_SUBNET_IDS / ECS_SECURITY_GROUP_ID / ECS_ASSIGN_PUBLIC_IP.
#   프라이빗 서브넷(assignPublicIp=DISABLED) 사용 시 ECR/S3/SQS/SSM/Logs/Anthropic 아웃바운드를 위해
#   해당 서브넷에 NAT(또는 VPC 엔드포인트)가 있어야 한다.
#
# 봇은 op 계정 EB에서 구동되고 run_server.py가 SQS로 잡을 디스패치한다.
# 시크릿은 SSM Parameter Store(SecureString)에 적재하고 task def secrets 블록이 참조한다.
#
# 사전 조건: aws CLI v2 + docker. AWS 자격증명은 op 계정(187063173014)을 가리켜야 하고,
#            공유 계정(591379657681) ECR repo에 push 권한이 있어야 한다.
#            (예: export AWS_PROFILE=<op 계정 프로파일> 후 aws sso login)
#
# 사용법:  ./run_create.sh
#          IMAGE_TAG=latest SKIP_BUILD=1 ./run_create.sh   # 이미지 빌드/푸시 생략

set -euo pipefail

# ---------------------------------------------------------------------------
# 설정 (필요 시 환경변수로 override)
# ---------------------------------------------------------------------------
AWS_REGION="${AWS_REGION:-ap-northeast-2}"
ACCOUNT_ID="${ACCOUNT_ID:-187063173014}"   # op 계정. 자격증명이 이 계정을 가리켜야 함.
export AWS_PROFILE="${AWS_PROFILE:-hbsmith-op}"
# 이미지는 공유 계정(591379657681) repo에 둔다. op 계정 자격증명으로 cross-account push/pull한다.
REGISTRY="591379657681.dkr.ecr.${AWS_REGION}.amazonaws.com"
ECR_REPO="hbsmith/tabris"
IMAGE_TAG="${IMAGE_TAG:-latest}"

# --- 봇 settings_local.py에서 버킷/클러스터/태스크/네트워크 값을 읽어 기본값으로 사용 ---
# 우선순위: 명시적 env > settings_local 값 > 스크립트 내장 기본값.
# 이렇게 리소스 이름·네트워크를 봇 설정과 항상 일치시켜 불일치로 인한 AccessDenied를 막는다.
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
    # 네트워크: 기본 VPC를 탐지하지 않고 운영 서브넷/SG/퍼블릭IP 여부를 설정에서 읽는다.
    'CFG_SUBNET_IDS':         'ECS_SUBNET_IDS',
    'CFG_SG_ID':              'ECS_SECURITY_GROUP_ID',
    'CFG_ASSIGN_PUBLIC_IP':   'ECS_ASSIGN_PUBLIC_IP',
    # 시크릿은 SSM Parameter Store(SecureString)에 적재하고 task def secrets 블록이 참조한다.
    # 워커는 평소처럼 env로 받으므로(ECS가 시작 시 주입) 코드는 그대로다.
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

# 인프라 리소스 이름(설정 파일에 없음). 고정.
TASK_ROLE_NAME='tabris-sandbox-task-role'
EXEC_ROLE_NAME='tabris-ecs-execution-role'
LOG_GROUP='/ecs/tabris-sandbox'
# 봇이 구동되는 EB 인스턴스 role. 디스패치 권한(SQS/S3/StopTask)을 인라인으로 부착한다.
BOT_ROLE_NAME="${BOT_ROLE_NAME:-tabris-test-ec2-role}"
# 시크릿은 평문 env 대신 SSM Parameter Store(SecureString)에 두고, task def의 secrets 블록이
# valueFrom으로 참조한다(콘솔/RunTask 호출에 평문 미노출). 파라미터 이름 접두사.
SSM_PREFIX='/tabris/sandbox/'

# 워밍 풀 리소스(이름 고정) / 튜닝값(env로 override 가능).
QUEUE_NAME="${QUEUE_NAME:-tabris-sandbox-jobs.fifo}"
DLQ_NAME="${DLQ_NAME:-tabris-sandbox-jobs-dlq.fifo}"
SERVICE_NAME="${SERVICE_NAME:-tabris-sandbox-pool}"
MAX_JOBS="${MAX_JOBS:-1}"
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

# 네트워크: ID(subnet-*/sg-*) 또는 이름(태그/그룹명)을 받는다. 이름은 아래 0.5에서 ID로 해석.
# 퍼블릭 IP 여부는 기본 DISABLED(프라이빗 서브넷 전제).
SUBNET_SPEC="${CFG_SUBNET_IDS:?settings_local.py에 ECS_SUBNET_IDS가 없습니다}"
SG_SPEC="${CFG_SG_ID:?settings_local.py에 ECS_SECURITY_GROUP_ID가 없습니다}"
ASSIGN_PUBLIC_IP="${CFG_ASSIGN_PUBLIC_IP:-DISABLED}"

# 시크릿은 settings_local에서 읽어 SSM에 적재한다(아래 6.7). task def는 SSM을 valueFrom으로 참조.
ANTHROPIC_API_KEY="${CFG_ANTHROPIC_API_KEY:?settings_local.py에 ANTHROPIC_API_KEY가 없습니다}"
SLACK_BOT_TOKEN="${CFG_SLACK_BOT_TOKEN:?settings_local.py에 SLACK_BOT_TOKEN가 없습니다}"
NERV_MCP_TOKEN="${CFG_NERV_MCP_TOKEN:?settings_local.py에 NERV_MCP_TOKEN가 없습니다}"
GITHUB_PAT="${CFG_GITHUB_PAT:?settings_local.py에 GITHUB_PAT가 없습니다}"
SENTRY_AUTH_TOKEN="${CFG_SENTRY_AUTH_TOKEN:?settings_local.py에 SENTRY_AUTH_TOKEN가 없습니다}"
JIRA_API_KEY="${CFG_JIRA_API_KEY:?settings_local.py에 JIRA_API_KEY가 없습니다}"
JIRA_API_USERNAME="${CFG_JIRA_API_USERNAME:?settings_local.py에 JIRA_API_USERNAME가 없습니다}"
# Atlassian MCP Basic auth: base64("user:api_key") — run_server.py와 동일 규칙.
ATLASSIAN_ROVO_MCP_TOKEN="$(printf '%s:%s' "${JIRA_API_USERNAME}" "${JIRA_API_KEY}" | base64 | tr -d '\n')"

# aws_inspect 스킬이 assume하는 OrchestratorRole (read-only 체인 진입점).
# 이 role은 공유 계정(591379657681)에 존재한다(aws_inspect 스킬과 동일). op 계정이 아님에 주의.
ORCHESTRATOR_ROLE_ARN="arn:aws:iam::591379657681:role/ai-agent/HBsmithAIAgent-InspectOrchestratorRole"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
TEMPLATE="${SCRIPT_DIR}/task_definition_sandbox.json"
ENV_OUT="${SCRIPT_DIR}/resources.env"

log() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
aws() { command aws --region "${AWS_REGION}" "$@"; }

# ---------------------------------------------------------------------------
# 0. 자격증명 / 계정 확인
# ---------------------------------------------------------------------------
log "AWS 자격증명 확인"
CALLER_ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
if [[ "${CALLER_ACCOUNT}" != "${ACCOUNT_ID}" ]]; then
  echo "❌ 현재 자격증명 계정(${CALLER_ACCOUNT})이 대상 계정(${ACCOUNT_ID})과 다릅니다." >&2
  echo "   AWS_PROFILE 을 ${ACCOUNT_ID} 계정으로 설정 후 다시 실행하세요." >&2
  exit 1
fi
echo "✅ account=${CALLER_ACCOUNT}"

# ---------------------------------------------------------------------------
# 0.5 네트워크 이름 → ID 해석
#     settings_local의 ECS_SUBNET_IDS / ECS_SECURITY_GROUP_ID 는 ID 또는 이름을 받는다.
#       - sg-* / subnet-* 접두사면 ID로 간주하고 그대로 사용
#       - 그 외는 이름으로 보고 조회: SG는 group-name(없으면 tag:Name), 서브넷은 tag:Name
#     모호성 방지: 먼저 SG를 해석해 VPC를 확정하고, 서브넷 이름 조회를 그 VPC로 한정한다.
#     서브넷 이름은 와일드카드(예: eb_private_*)를 허용한다.
# ---------------------------------------------------------------------------
log "네트워크 이름 → ID 해석"

# SG 해석
if [[ "${SG_SPEC}" == sg-* ]]; then
  SG_ID="${SG_SPEC}"
else
  SG_ID="$(aws ec2 describe-security-groups \
    --filters "Name=group-name,Values=${SG_SPEC}" \
    --query 'SecurityGroups[].GroupId' --output text)"
  if [[ -z "${SG_ID}" || "${SG_ID}" == "None" ]]; then
    SG_ID="$(aws ec2 describe-security-groups \
      --filters "Name=tag:Name,Values=${SG_SPEC}" \
      --query 'SecurityGroups[].GroupId' --output text)"
  fi
  SG_N="$(printf '%s' "${SG_ID}" | wc -w | tr -d ' ')"
  if [[ "${SG_N}" -eq 0 ]]; then
    echo "❌ 보안그룹 '${SG_SPEC}' 를 group-name/tag:Name 으로 찾을 수 없습니다." >&2; exit 1
  elif [[ "${SG_N}" -gt 1 ]]; then
    echo "❌ 보안그룹 '${SG_SPEC}' 가 여러 개 매칭됩니다(${SG_ID}). ID(sg-...)로 지정하세요." >&2; exit 1
  fi
fi

# SG의 VPC 확정(서브넷 이름 조회 범위 한정용)
VPC_ID="$(aws ec2 describe-security-groups --group-ids "${SG_ID}" \
  --query 'SecurityGroups[0].VpcId' --output text)"

# 서브넷 해석 (CSV; 각 토큰이 ID면 그대로, 이름이면 tag:Name 조회 — 와일드카드 허용)
RESOLVED_SUBNETS=()
IFS=',' read -ra _SUBNET_TOKENS <<< "${SUBNET_SPEC}"
for _tok in "${_SUBNET_TOKENS[@]}"; do
  _tok="$(printf '%s' "${_tok}" | xargs)"   # 공백 트림
  [[ -z "${_tok}" ]] && continue
  if [[ "${_tok}" == subnet-* ]]; then
    RESOLVED_SUBNETS+=("${_tok}")
    continue
  fi
  _ids="$(aws ec2 describe-subnets \
    --filters "Name=tag:Name,Values=${_tok}" "Name=vpc-id,Values=${VPC_ID}" \
    --query 'Subnets[].SubnetId' --output text)"
  if [[ -z "${_ids}" || "${_ids}" == "None" ]]; then
    echo "❌ 서브넷 이름 '${_tok}' (vpc=${VPC_ID}) 에 매칭되는 서브넷이 없습니다." >&2; exit 1
  fi
  for _i in ${_ids}; do RESOLVED_SUBNETS+=("${_i}"); done
done
if [[ "${#RESOLVED_SUBNETS[@]}" -eq 0 ]]; then
  echo "❌ ECS_SUBNET_IDS 해석 결과가 비었습니다: '${SUBNET_SPEC}'" >&2; exit 1
fi
# 중복 제거(순서 보존) 후 CSV 조립
SUBNET_IDS="$(printf '%s\n' "${RESOLVED_SUBNETS[@]}" | awk '!seen[$0]++' | paste -sd, -)"

echo "  SG     ${SG_SPEC} → ${SG_ID} (vpc=${VPC_ID})"
echo "  subnet ${SUBNET_SPEC} → ${SUBNET_IDS}"

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
#    이미 있으면(본 계정 소유든 타 계정 소유든) 생성을 건너뛴다.
#      - head-bucket 200      → 접근 가능(본 계정 소유 가정) → 생성 skip, 버킷설정 적용
#      - create BucketAlreadyOwnedByYou → 본 계정 소유 → skip, 버킷설정 적용
#      - create BucketAlreadyExists     → 타 계정 소유(공유 버킷) → 생성/설정 모두 skip(소유 계정이 관리)
# ---------------------------------------------------------------------------
log "S3 workspace 버킷: ${WORKSPACE_BUCKET}"
WORKSPACE_BUCKET_OWNED=0
if aws s3api head-bucket --bucket "${WORKSPACE_BUCKET}" 2>/dev/null; then
  echo "이미 존재(접근 가능) — 생성 건너뜀"
  WORKSPACE_BUCKET_OWNED=1
else
  if CREATE_ERR="$(aws s3api create-bucket \
        --bucket "${WORKSPACE_BUCKET}" \
        --create-bucket-configuration "LocationConstraint=${AWS_REGION}" 2>&1)"; then
    echo "생성 완료"
    WORKSPACE_BUCKET_OWNED=1
  elif printf '%s' "${CREATE_ERR}" | grep -q 'BucketAlreadyOwnedByYou'; then
    echo "이미 존재(본 계정 소유) — 건너뜀"
    WORKSPACE_BUCKET_OWNED=1
  elif printf '%s' "${CREATE_ERR}" | grep -q 'BucketAlreadyExists'; then
    echo "⚠️ '${WORKSPACE_BUCKET}' 는 다른 계정이 소유한 버킷입니다 — 생성/버킷설정을 건너뜁니다(공유 버킷으로 가정)." >&2
    echo "   봇/워커가 이 버킷을 read/write 하려면 소유 계정의 버킷 정책이 op 계정(${ACCOUNT_ID})을 허용해야 합니다." >&2
  else
    echo "${CREATE_ERR}" >&2
    exit 1
  fi
fi

if [[ "${WORKSPACE_BUCKET_OWNED}" == "1" ]]; then
  aws s3api put-public-access-block --bucket "${WORKSPACE_BUCKET}" \
    --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
  # lifecycle: runs/*(prompt·input)와 jobs/*(done·cancel 멱등 마커)를 1일 후 만료(코드 없는 청소).
  aws s3api put-bucket-lifecycle-configuration --bucket "${WORKSPACE_BUCKET}" \
    --lifecycle-configuration '{
      "Rules": [
        {"ID": "expire-runs", "Filter": {"Prefix": "runs/"}, "Status": "Enabled", "Expiration": {"Days": 1}},
        {"ID": "expire-jobs", "Filter": {"Prefix": "jobs/"}, "Status": "Enabled", "Expiration": {"Days": 1}}
      ]
    }'
fi

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
# task def secrets(valueFrom)를 ECS 에이전트가 해석하려면 execution role에 SSM 읽기 권한이 필요.
# SecureString 복호화는 SSM 경유(kms:ViaService) kms:Decrypt로 한정한다(기본 키 alias/aws/ssm 대응).
EXEC_SSM_POLICY="$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadSandboxSecrets",
      "Effect": "Allow",
      "Action": ["ssm:GetParameters"],
      "Resource": "arn:aws:ssm:${AWS_REGION}:${ACCOUNT_ID}:parameter${SSM_PREFIX}*"
    },
    {
      "Sid": "DecryptSandboxSecrets",
      "Effect": "Allow",
      "Action": ["kms:Decrypt"],
      "Resource": "*",
      "Condition": {"StringEquals": {"kms:ViaService": "ssm.${AWS_REGION}.amazonaws.com"}}
    }
  ]
}
JSON
)"
aws iam put-role-policy --role-name "${EXEC_ROLE_NAME}" \
  --policy-name tabris-sandbox-ssm --policy-document "${EXEC_SSM_POLICY}"

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

# (네트워크는 위 0.5에서 settings_local의 ID/이름을 ID로 해석해 SG_ID/SUBNET_IDS/VPC_ID 확정)
if [[ "${ASSIGN_PUBLIC_IP}" == "DISABLED" ]]; then
  echo "  ↳ assignPublicIp=DISABLED(프라이빗 서브넷) — ECR/S3/SQS/SSM/Logs/Anthropic 아웃바운드용 NAT(또는 VPC 엔드포인트)가 있어야 함"
fi

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
# 6.6 봇 EB role 디스패치 권한 부착
#     봇(run_server.py)은 EB 인스턴스 role로 구동되며 SQS 적재·workspace 업로드·취소(StopTask)를
#     수행한다. 기존 role(${BOT_ROLE_NAME})에 디스패치 전용 인라인 정책을 부착한다.
#     ※ 기존 운영 role을 수정하는 단계다. role이 없으면 경고만 남기고 계속 진행한다.
# ---------------------------------------------------------------------------
log "봇 EB role 디스패치 권한 부착: ${BOT_ROLE_NAME} (sqs:SendMessage / s3:PutObject / ecs:StopTask)"
if aws iam get-role --role-name "${BOT_ROLE_NAME}" >/dev/null 2>&1; then
  BOT_POLICY="$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DispatchToSandboxQueue",
      "Effect": "Allow",
      "Action": "sqs:SendMessage",
      "Resource": "arn:aws:sqs:${AWS_REGION}:${ACCOUNT_ID}:${QUEUE_NAME}"
    },
    {
      "Sid": "PutWorkspacePromptAndMarkers",
      "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": [
        "arn:aws:s3:::${WORKSPACE_BUCKET}/runs/*",
        "arn:aws:s3:::${WORKSPACE_BUCKET}/jobs/*"
      ]
    },
    {
      "Sid": "CancelSandboxTask",
      "Effect": "Allow",
      "Action": "ecs:StopTask",
      "Resource": "arn:aws:ecs:${AWS_REGION}:${ACCOUNT_ID}:task/${CLUSTER}/*"
    }
  ]
}
JSON
)"
  aws iam put-role-policy --role-name "${BOT_ROLE_NAME}" \
    --policy-name tabris-bot-dispatch --policy-document "${BOT_POLICY}"
  echo "  ✓ ${BOT_ROLE_NAME} 에 tabris-bot-dispatch 인라인 정책 부착"
else
  echo "  ⚠️ EB role '${BOT_ROLE_NAME}' 를 찾을 수 없어 권한 부착을 건너뜁니다." >&2
  echo "     BOT_ROLE_NAME 으로 올바른 role 이름을 지정해 다시 실행하세요(봇이 디스패치 못 함)." >&2
fi

# ---------------------------------------------------------------------------
# 6.7 SSM Parameter Store — 시크릿 적재(SecureString). task def secrets가 이 값을 참조한다.
# ---------------------------------------------------------------------------
log "SSM Parameter Store에 시크릿 적재: ${SSM_PREFIX}*"
_put_secret() {  # $1=파라미터 이름, $2=값
  aws ssm put-parameter --type SecureString --overwrite \
    --name "${SSM_PREFIX}$1" --value "$2" >/dev/null
  echo "  ✓ ${SSM_PREFIX}$1"
}
_put_secret ANTHROPIC_API_KEY        "${ANTHROPIC_API_KEY}"
_put_secret SLACK_BOT_TOKEN          "${SLACK_BOT_TOKEN}"
_put_secret NERV_MCP_TOKEN           "${NERV_MCP_TOKEN}"
_put_secret ATLASSIAN_ROVO_MCP_TOKEN "${ATLASSIAN_ROVO_MCP_TOKEN}"
_put_secret GITHUB_PAT               "${GITHUB_PAT}"
_put_secret SENTRY_AUTH_TOKEN        "${SENTRY_AUTH_TOKEN}"

# ---------------------------------------------------------------------------
# 7. Task Definition 등록 (ARM64)
# ---------------------------------------------------------------------------
log "Task Definition 렌더 & 등록: ${TASK_FAMILY}"
RENDERED="$(mktemp)"
# 시크릿은 더 이상 템플릿에 치환하지 않는다(SSM 참조). secrets ARN 조립용으로 SSM_PREFIX만 추가.
export TASK_FAMILY ACCOUNT_ID TASK_ROLE_NAME EXEC_ROLE_NAME REGISTRY IMAGE_TAG \
       AWS_REGION WORKSPACE_BUCKET MEMORY_BUCKET ARTIFACTS_BUCKET ARTIFACTS_BASE_URL \
       DOCUMENTS_BUCKET LOG_GROUP \
       QUEUE_URL MAX_JOBS MAX_LIFETIME_SEC SQS_VISIBILITY_TIMEOUT_SEC \
       SSM_PREFIX
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

NET_CONF="awsvpcConfiguration={subnets=[${SUBNET_IDS}],securityGroups=[${SG_ID}],assignPublicIp=${ASSIGN_PUBLIC_IP}}"

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
# run_create.sh 생성 결과 (생성 시점 스냅샷). run_terminate.sh 가 참고한다.
# SUBNET_IDS / SG_ID 는 settings_local.py에서 읽은 기존 리소스이며 terminate가 삭제하지 않는다.
AWS_REGION=${AWS_REGION}
ACCOUNT_ID=${ACCOUNT_ID}
WORKSPACE_BUCKET=${WORKSPACE_BUCKET}
CLUSTER=${CLUSTER}
TASK_FAMILY=${TASK_FAMILY}
TASK_ROLE_NAME=${TASK_ROLE_NAME}
EXEC_ROLE_NAME=${EXEC_ROLE_NAME}
BOT_ROLE_NAME=${BOT_ROLE_NAME}
LOG_GROUP=${LOG_GROUP}
VPC_ID=${VPC_ID}
SG_ID=${SG_ID}
SUBNET_IDS=${SUBNET_IDS}
ASSIGN_PUBLIC_IP=${ASSIGN_PUBLIC_IP}
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
settings_local.py 의 아래 값이 생성 결과와 일치하는지 확인하고 봇을 재시작하세요:

WORKSPACE_S3_BUCKET = '${WORKSPACE_BUCKET}'
ECS_CLUSTER = '${CLUSTER}'      # 취소(StopTask)에 사용

# 워밍 풀 디스패치 큐. 필수 — 비어 있으면 봇이 기동을 거부한다.
SQS_QUEUE_URL = '${QUEUE_URL}'

※ 봇 EB role(${BOT_ROLE_NAME})에는 본 스크립트가 디스패치 권한(tabris-bot-dispatch)을 부착했습니다:
     - sqs:SendMessage  → ${QUEUE_NAME}
     - s3:PutObject     → ${WORKSPACE_BUCKET}/jobs/* (cancel 마커), runs/* (prompt/input)
     - ecs:StopTask     → cluster ${CLUSTER}
   워커(task role)에는 SQS receive/delete/visibility 권한을 부여했습니다.
────────────────────────────────────────────────────────────────────
SNIPPET
