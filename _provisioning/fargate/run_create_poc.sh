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

# 봇과 공유하는 값은 settings_local.py에서만 읽는다(누락 시 명확히 실패).
WORKSPACE_BUCKET="${CFG_WORKSPACE_BUCKET:?settings_local.py에 WORKSPACE_S3_BUCKET가 없습니다}"
MEMORY_BUCKET="${CFG_MEMORY_BUCKET:?settings_local.py에 MEMORY_S3_BUCKET가 없습니다}"
ARTIFACTS_BUCKET="${CFG_ARTIFACTS_BUCKET:?settings_local.py에 ARTIFACTS_S3_BUCKET가 없습니다}"
DOCUMENTS_BUCKET="${CFG_DOCUMENTS_BUCKET:?settings_local.py에 DOCUMENTS_S3_BUCKET가 없습니다}"
ARTIFACTS_BASE_URL="${CFG_ARTIFACTS_BASE_URL:?settings_local.py에 ARTIFACTS_BASE_URL가 없습니다}"
CLUSTER="${CFG_CLUSTER:?settings_local.py에 ECS_CLUSTER가 없습니다}"
TASK_FAMILY="${CFG_TASK_FAMILY:?settings_local.py에 ECS_SANDBOX_TASK_DEFINITION가 없습니다}"

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
# lifecycle: runs/*(prompt·input)는 1일 후 만료. (취소는 ecs StopTask라 cancel/ sentinel 없음)
aws s3api put-bucket-lifecycle-configuration --bucket "${WORKSPACE_BUCKET}" \
  --lifecycle-configuration '{
    "Rules": [
      {"ID": "expire-runs", "Filter": {"Prefix": "runs/"}, "Status": "Enabled", "Expiration": {"Days": 1}}
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
# 7. Task Definition 등록 (ARM64)
# ---------------------------------------------------------------------------
log "Task Definition 렌더 & 등록: ${TASK_FAMILY}"
RENDERED="$(mktemp)"
export TASK_FAMILY ACCOUNT_ID TASK_ROLE_NAME EXEC_ROLE_NAME REGISTRY IMAGE_TAG \
       AWS_REGION WORKSPACE_BUCKET MEMORY_BUCKET ARTIFACTS_BUCKET ARTIFACTS_BASE_URL \
       DOCUMENTS_BUCKET LOG_GROUP
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
ENV

log "완료 ✅  생성 정보 → ${ENV_OUT}"
cat <<SNIPPET

────────────────────────────────────────────────────────────────────
settings_local.py 에 아래를 반영하고 봇을 재시작하세요 (Vagrant):

EXECUTION_MODE = 'fargate'
WORKSPACE_S3_BUCKET = '${WORKSPACE_BUCKET}'
ECS_CLUSTER = '${CLUSTER}'
ECS_SANDBOX_TASK_DEFINITION = '${TASK_FAMILY}'
ECS_SUBNET_IDS = '${SUBNET_IDS}'
ECS_SECURITY_GROUP_ID = '${SG_ID}'
ECS_ASSIGN_PUBLIC_IP = 'ENABLED'

※ 봇은 aws CLI만 사용하므로 추가 pip 설치가 필요 없습니다.
   (자격증명·S3·RunTask 모두 기존 aws CLI 경유)
────────────────────────────────────────────────────────────────────
SNIPPET
