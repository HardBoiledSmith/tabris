#!/usr/bin/env bash
#
# run_update.sh — 이미 존재하는 Tabris Fargate 샌드박스 리소스에 "설정 변경"을 반영한다.
#
# run_create.sh 가 신규 생성에 특화돼 있다면, 이 스크립트는 기존 리소스를 전제로 한다.
#   - 리소스가 없으면 생성하지 않고 명확히 실패한다("먼저 run_create.sh 를 실행하세요").
#   - 변경 가능한 설정만 in-place 로 갱신한다(SQS 속성·IAM 정책·SSM 시크릿·task def·오토스케일).
#   - 서비스는 항상 --force-new-deployment 로 롤링한다(코드/이미지/튜닝 변경을 즉시 반영).
#
# 갱신 대상(컴포넌트). 인자로 일부만 고를 수 있고, 인자가 없으면 전부 수행한다:
#   image      ECR 이미지 빌드 & 푸시(ARM64). SKIP_BUILD=1 이면 image 가 선택돼도 건너뛴다.
#   iam        execution/task role 인라인 정책 + 봇 EB role 디스패치 정책 갱신
#   secrets    SSM Parameter Store 시크릿(SecureString) 덮어쓰기
#   sqs        기존 큐/DLQ 속성(visibility·retention·redrive) 갱신
#   logs       CloudWatch Logs 보존기간 갱신
#   deploy     task def 새 리비전 등록 + 서비스를 그 리비전으로 --force-new-deployment 롤링
#   autoscale  스케일러블 타깃(min/max)·step 정책·알람·스케줄드 액션 갱신
#
# 사용법:
#   AWS_PROFILE=<op 프로파일> ./run_update.sh                 # 전체 갱신(이미지 빌드 포함) + 서비스 롤
#   AWS_PROFILE=<op 프로파일> SKIP_BUILD=1 ./run_update.sh     # 이미지 빌드만 생략하고 나머지 갱신
#   AWS_PROFILE=<op 프로파일> ./run_update.sh image deploy     # 이미지 새로 굽고 서비스만 롤(가장 흔한 코드배포)
#   AWS_PROFILE=<op 프로파일> ./run_update.sh deploy           # task def env 튜닝값만 바꿔 롤(MAX_JOBS 등)
#   AWS_PROFILE=<op 프로파일> ./run_update.sh sqs autoscale    # 큐/오토스케일 설정만 갱신
#
# 사전 조건: aws CLI v2 (+ image 갱신 시 docker). 자격증명은 op 계정(187063173014)을 가리켜야 한다.

set -euo pipefail

# ---------------------------------------------------------------------------
# 설정 (run_create.sh 와 동일하게 settings_local.py 에서 읽는다)
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
    'CFG_SUBNET_IDS':         'ECS_SUBNET_IDS',
    'CFG_SG_ID':              'ECS_SECURITY_GROUP_ID',
    'CFG_ASSIGN_PUBLIC_IP':   'ECS_ASSIGN_PUBLIC_IP',
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

# 인프라 리소스 이름(설정 파일에 없음). 고정 — run_create.sh 와 동일.
TASK_ROLE_NAME='tabris-sandbox-task-role'
EXEC_ROLE_NAME='tabris-ecs-execution-role'
LOG_GROUP='/ecs/tabris-sandbox'
LOG_RETENTION_DAYS="${LOG_RETENTION_DAYS:-14}"
BOT_ROLE_NAME="${BOT_ROLE_NAME:-tabris-test-ec2-role}"
SSM_PREFIX='/tabris/sandbox/'

# 워밍 풀 리소스(이름 고정) / 튜닝값(env로 override 가능) — run_create.sh 와 동일 기본값.
QUEUE_NAME="${QUEUE_NAME:-tabris-sandbox-jobs.fifo}"
DLQ_NAME="${DLQ_NAME:-tabris-sandbox-jobs-dlq.fifo}"
SERVICE_NAME="${SERVICE_NAME:-tabris-sandbox-pool}"
MAX_JOBS="${MAX_JOBS:-1}"
MAX_LIFETIME_SEC="${MAX_LIFETIME_SEC:-2700}"
SQS_VISIBILITY_TIMEOUT_SEC="${SQS_VISIBILITY_TIMEOUT_SEC:-360}"
POOL_MAX_TASKS="${POOL_MAX_TASKS:-5}"
POOL_BUSINESS_MIN="${POOL_BUSINESS_MIN:-1}"
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

SUBNET_SPEC="${CFG_SUBNET_IDS:?settings_local.py에 ECS_SUBNET_IDS가 없습니다}"
SG_SPEC="${CFG_SG_ID:?settings_local.py에 ECS_SECURITY_GROUP_ID가 없습니다}"
ASSIGN_PUBLIC_IP="${CFG_ASSIGN_PUBLIC_IP:-DISABLED}"

# 시크릿(secrets 컴포넌트에서만 사용) — settings_local 에서 읽어 SSM 에 덮어쓴다.
ANTHROPIC_API_KEY="${CFG_ANTHROPIC_API_KEY:?settings_local.py에 ANTHROPIC_API_KEY가 없습니다}"
SLACK_BOT_TOKEN="${CFG_SLACK_BOT_TOKEN:?settings_local.py에 SLACK_BOT_TOKEN가 없습니다}"
NERV_MCP_TOKEN="${CFG_NERV_MCP_TOKEN:?settings_local.py에 NERV_MCP_TOKEN가 없습니다}"
GITHUB_PAT="${CFG_GITHUB_PAT:?settings_local.py에 GITHUB_PAT가 없습니다}"
SENTRY_AUTH_TOKEN="${CFG_SENTRY_AUTH_TOKEN:?settings_local.py에 SENTRY_AUTH_TOKEN가 없습니다}"
JIRA_API_KEY="${CFG_JIRA_API_KEY:?settings_local.py에 JIRA_API_KEY가 없습니다}"
JIRA_API_USERNAME="${CFG_JIRA_API_USERNAME:?settings_local.py에 JIRA_API_USERNAME가 없습니다}"
ATLASSIAN_ROVO_MCP_TOKEN="$(printf '%s:%s' "${JIRA_API_USERNAME}" "${JIRA_API_KEY}" | base64 | tr -d '\n')"

# aws_inspect 스킬이 assume하는 OrchestratorRole (read-only 체인 진입점, 공유 계정).
ORCHESTRATOR_ROLE_ARN="arn:aws:iam::591379657681:role/ai-agent/HBsmithAIAgent-InspectOrchestratorRole"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
TEMPLATE="${SCRIPT_DIR}/task_definition_sandbox.json"
ENV_OUT="${SCRIPT_DIR}/resources.env"

log()  { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
die()  { echo "❌ $*" >&2; exit 1; }
aws()  { command aws --region "${AWS_REGION}" "$@"; }

# ---------------------------------------------------------------------------
# 갱신 대상 선택: 인자가 있으면 그 컴포넌트만, 없으면 전부.
# ---------------------------------------------------------------------------
ALL_TARGETS=(image iam secrets sqs logs deploy autoscale)
if [[ $# -gt 0 ]]; then
  TARGETS=("$@")
  for t in "${TARGETS[@]}"; do
    case "${t}" in
      image|iam|secrets|sqs|logs|deploy|autoscale) ;;
      *) die "알 수 없는 대상: '${t}'. 가능: ${ALL_TARGETS[*]}" ;;
    esac
  done
else
  TARGETS=("${ALL_TARGETS[@]}")
fi
want() { local x; for x in "${TARGETS[@]}"; do [[ "${x}" == "$1" ]] && return 0; done; return 1; }

# ---------------------------------------------------------------------------
# 0. 자격증명 / 계정 확인
# ---------------------------------------------------------------------------
log "AWS 자격증명 확인 (대상=${TARGETS[*]})"
CALLER_ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
[[ "${CALLER_ACCOUNT}" == "${ACCOUNT_ID}" ]] \
  || die "현재 자격증명 계정(${CALLER_ACCOUNT})이 대상 계정(${ACCOUNT_ID})과 다릅니다. AWS_PROFILE 확인."
echo "✅ account=${CALLER_ACCOUNT}"

# ---------------------------------------------------------------------------
# 0.5 네트워크 이름 → ID 해석 (deploy 시 서비스 network 동기화에 필요)
#     run_create.sh 0.5 와 동일 규칙.
# ---------------------------------------------------------------------------
resolve_network() {
  log "네트워크 이름 → ID 해석"
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
    local n; n="$(printf '%s' "${SG_ID}" | wc -w | tr -d ' ')"
    [[ "${n}" -eq 1 ]] || die "보안그룹 '${SG_SPEC}' 해석 실패(매칭 ${n}건: ${SG_ID}). ID(sg-...)로 지정하세요."
  fi
  VPC_ID="$(aws ec2 describe-security-groups --group-ids "${SG_ID}" \
    --query 'SecurityGroups[0].VpcId' --output text)"

  local resolved=() tok ids i
  IFS=',' read -ra _toks <<< "${SUBNET_SPEC}"
  for tok in "${_toks[@]}"; do
    tok="$(printf '%s' "${tok}" | xargs)"; [[ -z "${tok}" ]] && continue
    if [[ "${tok}" == subnet-* ]]; then resolved+=("${tok}"); continue; fi
    ids="$(aws ec2 describe-subnets \
      --filters "Name=tag:Name,Values=${tok}" "Name=vpc-id,Values=${VPC_ID}" \
      --query 'Subnets[].SubnetId' --output text)"
    [[ -n "${ids}" && "${ids}" != "None" ]] || die "서브넷 이름 '${tok}' (vpc=${VPC_ID}) 매칭 없음."
    for i in ${ids}; do resolved+=("${i}"); done
  done
  [[ "${#resolved[@]}" -gt 0 ]] || die "ECS_SUBNET_IDS 해석 결과가 비었습니다: '${SUBNET_SPEC}'"
  SUBNET_IDS="$(printf '%s\n' "${resolved[@]}" | awk '!seen[$0]++' | paste -sd, -)"
  echo "  SG     ${SG_SPEC} → ${SG_ID} (vpc=${VPC_ID})"
  echo "  subnet ${SUBNET_SPEC} → ${SUBNET_IDS}"
}

# ---------------------------------------------------------------------------
# 존재 확인 헬퍼 — update 는 생성하지 않는다. 없으면 안내 후 중단.
# ---------------------------------------------------------------------------
require_cluster() {
  local st; st="$(aws ecs describe-clusters --clusters "${CLUSTER}" \
    --query 'clusters[0].status' --output text 2>/dev/null || true)"
  [[ "${st}" == "ACTIVE" ]] || die "클러스터 '${CLUSTER}' 가 없습니다(status=${st:-none}). 먼저 run_create.sh 를 실행하세요."
}
require_service() {
  local st; st="$(aws ecs describe-services --cluster "${CLUSTER}" --services "${SERVICE_NAME}" \
    --query 'services[0].status' --output text 2>/dev/null || true)"
  [[ "${st}" == "ACTIVE" ]] || die "서비스 '${SERVICE_NAME}' 가 ACTIVE 가 아닙니다(status=${st:-none}). 먼저 run_create.sh 를 실행하세요."
}
require_role() {
  aws iam get-role --role-name "$1" >/dev/null 2>&1 \
    || die "IAM role '$1' 가 없습니다. 먼저 run_create.sh 를 실행하세요."
}
queue_url_of() {  # $1=queue name → stdout: url (없으면 빈 문자열)
  aws sqs get-queue-url --queue-name "$1" --query 'QueueUrl' --output text 2>/dev/null || true
}
require_queue_url() {
  local u; u="$(queue_url_of "$1")"
  [[ -n "${u}" && "${u}" != "None" ]] || die "SQS 큐 '$1' 가 없습니다. 먼저 run_create.sh 를 실행하세요."
  printf '%s' "${u}"
}

# ===========================================================================
# image — ECR 이미지 빌드 & 푸시 (ARM64)
# ===========================================================================
update_image() {
  if [[ "${SKIP_BUILD:-0}" == "1" ]]; then
    log "[image] SKIP_BUILD=1 → 빌드/푸시 생략"
    return
  fi
  command -v docker >/dev/null 2>&1 || die "docker 가 필요합니다(image 갱신). SKIP_BUILD=1 로 건너뛸 수 있습니다."
  log "[image] ECR 로그인: ${REGISTRY}"
  aws ecr get-login-password | docker login --username AWS --password-stdin "${REGISTRY}"
  log "[image] 빌드 (linux/arm64): ${REGISTRY}/${ECR_REPO}:${IMAGE_TAG}"
  docker build --platform linux/arm64 -t "${REGISTRY}/${ECR_REPO}:${IMAGE_TAG}" "${REPO_ROOT}"
  log "[image] 푸시"
  docker push "${REGISTRY}/${ECR_REPO}:${IMAGE_TAG}"
}

# ===========================================================================
# iam — execution/task role 인라인 정책 + 봇 EB role 디스패치 정책 갱신
#       (put-role-policy 는 멱등 — 기존 정책 문서를 현재 설정으로 덮어쓴다)
# ===========================================================================
update_iam() {
  require_role "${EXEC_ROLE_NAME}"
  require_role "${TASK_ROLE_NAME}"

  log "[iam] execution role SSM 읽기 정책 갱신: ${EXEC_ROLE_NAME}"
  aws iam attach-role-policy --role-name "${EXEC_ROLE_NAME}" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
  local exec_ssm
  exec_ssm="$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid": "ReadSandboxSecrets", "Effect": "Allow", "Action": ["ssm:GetParameters"],
     "Resource": "arn:aws:ssm:${AWS_REGION}:${ACCOUNT_ID}:parameter${SSM_PREFIX}*"},
    {"Sid": "DecryptSandboxSecrets", "Effect": "Allow", "Action": ["kms:Decrypt"], "Resource": "*",
     "Condition": {"StringEquals": {"kms:ViaService": "ssm.${AWS_REGION}.amazonaws.com"}}}
  ]
}
JSON
)"
  aws iam put-role-policy --role-name "${EXEC_ROLE_NAME}" \
    --policy-name tabris-sandbox-ssm --policy-document "${exec_ssm}"

  log "[iam] task role 정책 갱신: ${TASK_ROLE_NAME}"
  local task_policy
  task_policy="$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid": "WorkspaceBucket", "Effect": "Allow",
     "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
     "Resource": ["arn:aws:s3:::${WORKSPACE_BUCKET}", "arn:aws:s3:::${WORKSPACE_BUCKET}/*"]},
    {"Sid": "MemoryBucket", "Effect": "Allow",
     "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
     "Resource": ["arn:aws:s3:::${MEMORY_BUCKET}", "arn:aws:s3:::${MEMORY_BUCKET}/*"]},
    {"Sid": "ArtifactsBucket", "Effect": "Allow",
     "Action": ["s3:GetObject", "s3:PutObject"],
     "Resource": ["arn:aws:s3:::${ARTIFACTS_BUCKET}/*"]},
    {"Sid": "DocumentsBucket", "Effect": "Allow",
     "Action": ["s3:GetObject", "s3:ListBucket"],
     "Resource": ["arn:aws:s3:::${DOCUMENTS_BUCKET}", "arn:aws:s3:::${DOCUMENTS_BUCKET}/*"]},
    {"Sid": "SandboxJobQueue", "Effect": "Allow",
     "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"],
     "Resource": "arn:aws:sqs:${AWS_REGION}:${ACCOUNT_ID}:${QUEUE_NAME}"},
    {"Sid": "AwsInspectAssumeRole", "Effect": "Allow",
     "Action": "sts:AssumeRole", "Resource": "${ORCHESTRATOR_ROLE_ARN}"}
  ]
}
JSON
)"
  aws iam put-role-policy --role-name "${TASK_ROLE_NAME}" \
    --policy-name tabris-sandbox --policy-document "${task_policy}"

  log "[iam] 봇 EB role 디스패치 정책 갱신: ${BOT_ROLE_NAME}/tabris-bot-dispatch"
  if aws iam get-role --role-name "${BOT_ROLE_NAME}" >/dev/null 2>&1; then
    local bot_policy
    bot_policy="$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid": "DispatchToSandboxQueue", "Effect": "Allow", "Action": "sqs:SendMessage",
     "Resource": "arn:aws:sqs:${AWS_REGION}:${ACCOUNT_ID}:${QUEUE_NAME}"},
    {"Sid": "PutWorkspacePromptAndMarkers", "Effect": "Allow", "Action": "s3:PutObject",
     "Resource": ["arn:aws:s3:::${WORKSPACE_BUCKET}/runs/*", "arn:aws:s3:::${WORKSPACE_BUCKET}/jobs/*"]},
    {"Sid": "CancelSandboxTask", "Effect": "Allow", "Action": "ecs:StopTask",
     "Resource": "arn:aws:ecs:${AWS_REGION}:${ACCOUNT_ID}:task/${CLUSTER}/*"}
  ]
}
JSON
)"
    aws iam put-role-policy --role-name "${BOT_ROLE_NAME}" \
      --policy-name tabris-bot-dispatch --policy-document "${bot_policy}"
    echo "  ✓ ${BOT_ROLE_NAME} 갱신"
  else
    echo "  ⚠️ EB role '${BOT_ROLE_NAME}' 없음 — 디스패치 정책 갱신 건너뜀(봇이 디스패치 못 할 수 있음)." >&2
  fi
}

# ===========================================================================
# secrets — SSM Parameter Store 시크릿 덮어쓰기(SecureString)
# ===========================================================================
update_secrets() {
  log "[secrets] SSM Parameter Store 갱신: ${SSM_PREFIX}*"
  put() { aws ssm put-parameter --type SecureString --overwrite --name "${SSM_PREFIX}$1" --value "$2" >/dev/null && echo "  ✓ ${SSM_PREFIX}$1"; }
  put ANTHROPIC_API_KEY        "${ANTHROPIC_API_KEY}"
  put SLACK_BOT_TOKEN          "${SLACK_BOT_TOKEN}"
  put NERV_MCP_TOKEN           "${NERV_MCP_TOKEN}"
  put ATLASSIAN_ROVO_MCP_TOKEN "${ATLASSIAN_ROVO_MCP_TOKEN}"
  put GITHUB_PAT               "${GITHUB_PAT}"
  put SENTRY_AUTH_TOKEN        "${SENTRY_AUTH_TOKEN}"
}

# ===========================================================================
# sqs — 기존 큐/DLQ 속성 갱신 (set-queue-attributes; create-queue 아님)
#       FifoQueue 등 불변 속성은 건드리지 않는다.
# ===========================================================================
update_sqs() {
  local dlq_url dlq_arn q_url redrive
  dlq_url="$(require_queue_url "${DLQ_NAME}")"
  q_url="$(require_queue_url "${QUEUE_NAME}")"
  dlq_arn="$(aws sqs get-queue-attributes --queue-url "${dlq_url}" \
    --attribute-names QueueArn --query 'Attributes.QueueArn' --output text)"

  log "[sqs] DLQ 속성 갱신: ${DLQ_NAME}"
  aws sqs set-queue-attributes --queue-url "${dlq_url}" \
    --attributes 'MessageRetentionPeriod=1209600' >/dev/null
  echo "  ✓ retention=14d"

  log "[sqs] 큐 속성 갱신: ${QUEUE_NAME} (visibility=${SQS_VISIBILITY_TIMEOUT_SEC}s)"
  redrive="{\"deadLetterTargetArn\":\"${dlq_arn}\",\"maxReceiveCount\":\"3\"}"
  aws sqs set-queue-attributes --queue-url "${q_url}" \
    --attributes "$(cat <<JSON
{
  "VisibilityTimeout": "${SQS_VISIBILITY_TIMEOUT_SEC}",
  "MessageRetentionPeriod": "1209600",
  "RedrivePolicy": "$(printf '%s' "${redrive}" | sed 's/"/\\"/g')"
}
JSON
)" >/dev/null
  echo "  ✓ visibility/retention/redrive 갱신"
}

# ===========================================================================
# logs — CloudWatch Logs 보존기간 갱신
# ===========================================================================
update_logs() {
  log "[logs] 보존기간 갱신: ${LOG_GROUP} (${LOG_RETENTION_DAYS}d)"
  aws logs describe-log-groups --log-group-name-prefix "${LOG_GROUP}" \
    --query "logGroups[?logGroupName=='${LOG_GROUP}'] | length(@)" --output text 2>/dev/null \
    | grep -qx 1 || die "Logs 그룹 '${LOG_GROUP}' 가 없습니다. 먼저 run_create.sh 를 실행하세요."
  aws logs put-retention-policy --log-group-name "${LOG_GROUP}" --retention-in-days "${LOG_RETENTION_DAYS}"
  echo "  ✓ retention=${LOG_RETENTION_DAYS}d"
}

# ===========================================================================
# deploy — task def 새 리비전 등록 + 서비스를 그 리비전으로 강제 롤링
#          (image/iam/secrets/logs 변경을 실제 워커에 반영하는 핵심 단계)
# ===========================================================================
update_deploy() {
  require_cluster
  require_service
  require_role "${TASK_ROLE_NAME}"
  require_role "${EXEC_ROLE_NAME}"
  [[ -n "${SG_ID:-}" ]] || resolve_network
  QUEUE_URL="$(require_queue_url "${QUEUE_NAME}")"

  log "[deploy] Task Definition 렌더 & 등록: ${TASK_FAMILY}"
  local rendered; rendered="$(mktemp)"
  export TASK_FAMILY ACCOUNT_ID TASK_ROLE_NAME EXEC_ROLE_NAME REGISTRY IMAGE_TAG \
         AWS_REGION WORKSPACE_BUCKET MEMORY_BUCKET ARTIFACTS_BUCKET ARTIFACTS_BASE_URL \
         DOCUMENTS_BUCKET LOG_GROUP \
         QUEUE_URL MAX_JOBS MAX_LIFETIME_SEC SQS_VISIBILITY_TIMEOUT_SEC \
         SSM_PREFIX
  python3 - "${TEMPLATE}" > "${rendered}" <<'PY'
import os, sys, string
sys.stdout.write(string.Template(open(sys.argv[1]).read()).substitute(os.environ))
PY
  local new_arn
  new_arn="$(aws ecs register-task-definition --cli-input-json "file://${rendered}" \
    --query 'taskDefinition.taskDefinitionArn' --output text)"
  rm -f "${rendered}"
  echo "  ✓ 등록: ${new_arn}"

  log "[deploy] 서비스 롤링: ${SERVICE_NAME} → ${new_arn} (--force-new-deployment)"
  local net_conf="awsvpcConfiguration={subnets=[${SUBNET_IDS}],securityGroups=[${SG_ID}],assignPublicIp=${ASSIGN_PUBLIC_IP}}"
  aws ecs update-service --cluster "${CLUSTER}" --service "${SERVICE_NAME}" \
    --task-definition "${new_arn}" \
    --network-configuration "${net_conf}" \
    --force-new-deployment >/dev/null
  echo "  ✓ 서비스가 새 리비전으로 롤링됩니다(실행 중 태스크가 0이면 다음 스케일업 시 반영)."
}

# ===========================================================================
# autoscale — 스케일러블 타깃·step 정책·알람·스케줄드 액션 갱신 (모두 멱등 put)
# ===========================================================================
update_autoscale() {
  require_service
  local resource_id="service/${CLUSTER}/${SERVICE_NAME}"

  # 업무시간(UTC 평일 00–10시)에 갱신하면 즉시 min 플로어를 올린다(다음 cron까지 0 묶임 방지).
  local init_min=0 dow hour
  dow="$(date -u +%u)"; hour="$(date -u +%H)"
  if [[ "${dow}" -le 5 && "${hour}" -ge 0 && "${hour}" -lt 10 ]]; then init_min="${POOL_BUSINESS_MIN}"; fi

  log "[autoscale] 스케일러블 타깃 갱신: ${resource_id} (min=${init_min} max=${POOL_MAX_TASKS})"
  aws application-autoscaling register-scalable-target --service-namespace ecs \
    --resource-id "${resource_id}" --scalable-dimension ecs:service:DesiredCount \
    --min-capacity "${init_min}" --max-capacity "${POOL_MAX_TASKS}" >/dev/null

  log "[autoscale] step scaling 정책 + CloudWatch 알람 갱신"
  local out_arn in_arn
  out_arn="$(aws application-autoscaling put-scaling-policy --service-namespace ecs \
    --resource-id "${resource_id}" --scalable-dimension ecs:service:DesiredCount \
    --policy-name tabris-pool-scale-out --policy-type StepScaling \
    --step-scaling-policy-configuration '{"AdjustmentType":"ChangeInCapacity","MetricAggregationType":"Maximum","Cooldown":180,"StepAdjustments":[{"MetricIntervalLowerBound":0,"ScalingAdjustment":1}]}' \
    --query 'PolicyARN' --output text)"
  in_arn="$(aws application-autoscaling put-scaling-policy --service-namespace ecs \
    --resource-id "${resource_id}" --scalable-dimension ecs:service:DesiredCount \
    --policy-name tabris-pool-scale-in --policy-type StepScaling \
    --step-scaling-policy-configuration '{"AdjustmentType":"ExactCapacity","MetricAggregationType":"Maximum","Cooldown":60,"StepAdjustments":[{"MetricIntervalUpperBound":0,"ScalingAdjustment":0}]}' \
    --query 'PolicyARN' --output text)"

  aws cloudwatch put-metric-alarm --alarm-name tabris-pool-backlog-high \
    --alarm-description 'tabris pool: 대기 메시지 발생 → scale out' \
    --namespace AWS/SQS --metric-name ApproximateNumberOfMessagesVisible \
    --dimensions Name=QueueName,Value="${QUEUE_NAME}" \
    --statistic Maximum --period 60 --evaluation-periods 1 \
    --threshold 0 --comparison-operator GreaterThanThreshold \
    --treat-missing-data notBreaching --alarm-actions "${out_arn}" >/dev/null
  aws cloudwatch put-metric-alarm --alarm-name tabris-pool-backlog-empty \
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
    --treat-missing-data notBreaching --alarm-actions "${in_arn}" >/dev/null

  log "[autoscale] 스케줄드 갱신: UP='${SCHED_UP_CRON}' (min=${POOL_BUSINESS_MIN}) / DOWN='${SCHED_DOWN_CRON}' (min=0)"
  aws application-autoscaling put-scheduled-action --service-namespace ecs \
    --resource-id "${resource_id}" --scalable-dimension ecs:service:DesiredCount \
    --scheduled-action-name tabris-pool-business-up --schedule "${SCHED_UP_CRON}" \
    --scalable-target-action "MinCapacity=${POOL_BUSINESS_MIN},MaxCapacity=${POOL_MAX_TASKS}" >/dev/null
  aws application-autoscaling put-scheduled-action --service-namespace ecs \
    --resource-id "${resource_id}" --scalable-dimension ecs:service:DesiredCount \
    --scheduled-action-name tabris-pool-offhours-down --schedule "${SCHED_DOWN_CRON}" \
    --scalable-target-action "MinCapacity=0,MaxCapacity=${POOL_MAX_TASKS}" >/dev/null
  echo "  ✓ 오토스케일/스케줄드/알람 갱신"
}

# ---------------------------------------------------------------------------
# 실행 — 의존성 순서대로(인자 순서 무시). deploy 전에 image/iam/secrets/logs 가 반영되도록.
# ---------------------------------------------------------------------------
if want image;     then update_image;     fi
if want iam;       then update_iam;       fi
if want secrets;   then update_secrets;   fi
if want sqs;       then update_sqs;       fi
if want logs;      then update_logs;      fi
if want deploy;    then update_deploy;    fi
if want autoscale; then update_autoscale; fi

# resources.env 스냅샷 갱신(deploy 를 거쳐 네트워크/큐를 해석한 경우에만 — terminate 참고용).
if [[ -n "${SG_ID:-}" && -n "${QUEUE_URL:-}" ]]; then
  DLQ_URL="$(queue_url_of "${DLQ_NAME}")"
  cat > "${ENV_OUT}" <<ENV
# run_update.sh 갱신 결과 스냅샷. run_terminate.sh 가 참고한다.
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
  echo "  ✓ 스냅샷 갱신 → ${ENV_OUT}"
fi

log "완료 ✅  (대상: ${TARGETS[*]})"
