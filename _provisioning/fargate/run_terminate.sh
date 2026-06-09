#!/usr/bin/env bash
#
# run_terminate.sh — run_create.sh 가 op 계정(187063173014)에 만든 리소스를 삭제한다.
#
# 삭제 대상:
#   - 워밍 풀 ECS Service + 오토스케일/스케줄드/알람
#   - 실행 중인 Fargate 태스크 중지
#   - ECS Task Definition 전 리비전 deregister
#   - ECS 클러스터 삭제
#   - CloudWatch Logs 그룹 삭제
#   - IAM task/execution role 삭제
#   - 봇 EB role(tabris-test-ec2-role)의 디스패치 인라인 정책(tabris-bot-dispatch) 제거 (role 자체는 보존)
#   - S3 workspace 버킷 비우고 삭제
#   - SQS 큐 + DLQ 삭제
#
# 건드리지 않는 것:
#   - 공유 버킷(hbsmith-tabris-memory / -artifacts / -documents)
#   - 네트워크: ECS_SUBNET_IDS / ECS_SECURITY_GROUP_ID(eb_private) — 사전 구성된 기존 리소스
#   - 공유 계정(591379657681) ECR repo 및 이미지 (기본 보존)
#
# 사용법:  ./run_terminate.sh
#          DELETE_IMAGE=1 ./run_terminate.sh   # 공유 repo의 이미지 태그까지 삭제(cross-account 권한 필요, 주의)

set -uo pipefail  # 삭제는 best-effort: 개별 실패는 무시하고 계속 진행

AWS_REGION="${AWS_REGION:-ap-northeast-2}"
# op 계정 프로파일을 지정해 실행하세요 (예: AWS_PROFILE=<op 프로파일> ./run_terminate.sh).
export AWS_PROFILE="${AWS_PROFILE:-hbsmith-op}"
ACCOUNT_ID="${ACCOUNT_ID:-187063173014}"
# 이미지는 공유 계정(591379657681) repo에 있다(create와 동일).
REGISTRY="591379657681.dkr.ecr.${AWS_REGION}.amazonaws.com"
ECR_REPO="hbsmith/tabris"
IMAGE_TAG="${IMAGE_TAG:-latest}"
# 봇 EB role (create가 디스패치 인라인 정책을 부착한 대상).
BOT_ROLE_NAME="${BOT_ROLE_NAME:-tabris-test-ec2-role}"

# --- 봇 settings_local.py에서 버킷/클러스터/태스크 이름을 읽어 기본값으로 사용 ---
# 우선순위: 명시적 env > settings_local 값 > 스크립트 내장 기본값.
# (단, 아래에서 resources.env가 있으면 생성 시점 스냅샷으로 최종 override한다.)
SETTINGS_FILE="${TABRIS_SETTINGS:-/etc/tabris/settings_local.py}"
if [[ -f "${SETTINGS_FILE}" ]]; then
  echo "settings_local 로드: ${SETTINGS_FILE}"
  eval "$(python3 - "${SETTINGS_FILE}" <<'PY'
import runpy, shlex, sys
try:
    cfg = runpy.run_path(sys.argv[1])
except Exception as exc:
    sys.stderr.write(f'settings_local 파싱 실패: {exc}\n')
    sys.exit(0)
mapping = {
    'CFG_WORKSPACE_BUCKET': 'WORKSPACE_S3_BUCKET',
    'CFG_CLUSTER':          'ECS_CLUSTER',
    'CFG_TASK_FAMILY':      'ECS_SANDBOX_TASK_DEFINITION',
}
for shvar, pykey in mapping.items():
    val = cfg.get(pykey)
    if val not in (None, ''):
        print(f'{shvar}={shlex.quote(str(val))}')
PY
)"
fi

# 봇과 공유하는 값은 settings_local.py에서 읽는다(아래 resources.env가 있으면 그쪽이 최종 우선).
TASK_FAMILY="${CFG_TASK_FAMILY:-tabris-sandbox}"
CLUSTER="${CFG_CLUSTER:-tabris}"
WORKSPACE_BUCKET="${CFG_WORKSPACE_BUCKET:-hbsmith-tabris-workspace}"
# 인프라 리소스 이름(설정 파일에 없음). 고정.
TASK_ROLE_NAME='tabris-sandbox-task-role'
EXEC_ROLE_NAME='tabris-ecs-execution-role'
LOG_GROUP='/ecs/tabris-sandbox'
# 워밍 풀 리소스(resources.env가 있으면 그쪽 값이 우선 override).
SERVICE_NAME="${SERVICE_NAME:-tabris-sandbox-pool}"
QUEUE_NAME="${QUEUE_NAME:-tabris-sandbox-jobs.fifo}"
DLQ_NAME="${DLQ_NAME:-tabris-sandbox-jobs-dlq.fifo}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_OUT="${SCRIPT_DIR}/resources.env"
# 생성 시 기록한 값을 최우선으로 사용(가장 정확한 스냅샷)
[[ -f "${ENV_OUT}" ]] && source "${ENV_OUT}"

log() { printf '\n\033[1;31m==>\033[0m %s\n' "$*"; }
aws() { command aws --region "${AWS_REGION}" "$@"; }

log "AWS 계정 확인"
CALLER_ACCOUNT="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)"
if [[ "${CALLER_ACCOUNT}" != "${ACCOUNT_ID}" ]]; then
  echo "❌ 현재 자격증명 계정(${CALLER_ACCOUNT})이 PoC 계정(${ACCOUNT_ID})과 다릅니다. 중단." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 0.5 워밍 풀: ECS Service + Auto Scaling 삭제 (클러스터 삭제보다 먼저)
# ---------------------------------------------------------------------------
RESOURCE_ID="service/${CLUSTER}/${SERVICE_NAME}"
log "오토스케일 대상 deregister: ${RESOURCE_ID} (스케일 정책·스케줄드 함께 제거)"
aws application-autoscaling deregister-scalable-target \
  --service-namespace ecs --resource-id "${RESOURCE_ID}" \
  --scalable-dimension ecs:service:DesiredCount >/dev/null 2>&1 \
  && echo "deregistered" || echo "건너뜀(없음)"

# step scaling 알람은 수동 생성이라 deregister로 안 지워진다 — 명시 삭제.
log "CloudWatch 스케일 알람 삭제"
aws cloudwatch delete-alarms \
  --alarm-names tabris-pool-backlog-high tabris-pool-backlog-empty >/dev/null 2>&1 \
  && echo "deleted alarms" || echo "건너뜀(없음)"

log "ECS Service 삭제: ${SERVICE_NAME}"
aws ecs delete-service --cluster "${CLUSTER}" --service "${SERVICE_NAME}" --force >/dev/null 2>&1 \
  && echo "deleted" || echo "건너뜀(없음)"

# ---------------------------------------------------------------------------
# 1. 실행 중인 태스크 중지
# ---------------------------------------------------------------------------
log "실행 중인 Fargate 태스크 중지 (cluster=${CLUSTER})"
RUNNING="$(aws ecs list-tasks --cluster "${CLUSTER}" --desired-status RUNNING \
  --query 'taskArns' --output text 2>/dev/null)"
for t in ${RUNNING}; do
  [[ -n "${t}" && "${t}" != "None" ]] || continue
  echo "stop ${t}"
  aws ecs stop-task --cluster "${CLUSTER}" --task "${t}" >/dev/null 2>&1
done

# ---------------------------------------------------------------------------
# 2. Task Definition 전 리비전 deregister
# ---------------------------------------------------------------------------
log "Task Definition deregister: ${TASK_FAMILY}"
for arn in $(aws ecs list-task-definitions --family-prefix "${TASK_FAMILY}" \
  --query 'taskDefinitionArns' --output text 2>/dev/null); do
  [[ -n "${arn}" && "${arn}" != "None" ]] || continue
  aws ecs deregister-task-definition --task-definition "${arn}" >/dev/null 2>&1 \
    && echo "deregistered ${arn}"
done

# ---------------------------------------------------------------------------
# 3. ECS 클러스터 삭제
# ---------------------------------------------------------------------------
log "ECS 클러스터 삭제: ${CLUSTER}"
aws ecs delete-cluster --cluster "${CLUSTER}" >/dev/null 2>&1 \
  && echo "deleted" || echo "건너뜀(없거나 태스크 잔존)"

# ---------------------------------------------------------------------------
# 4. 봇 EB role 디스패치 정책 제거 (role 자체는 보존)
#    네트워크 SG(eb_private)는 사전 구성된 기존 리소스이므로 삭제하지 않는다.
# ---------------------------------------------------------------------------
log "봇 EB role 디스패치 정책 제거: ${BOT_ROLE_NAME}/tabris-bot-dispatch"
aws iam delete-role-policy --role-name "${BOT_ROLE_NAME}" \
  --policy-name tabris-bot-dispatch >/dev/null 2>&1 \
  && echo "deleted" || echo "건너뜀(없음)"

# ---------------------------------------------------------------------------
# 5. CloudWatch Logs 그룹 삭제
# ---------------------------------------------------------------------------
log "Logs 그룹 삭제: ${LOG_GROUP}"
aws logs delete-log-group --log-group-name "${LOG_GROUP}" 2>/dev/null \
  && echo "deleted" || echo "건너뜀(없음)"

# ---------------------------------------------------------------------------
# 6. IAM 역할 삭제
# ---------------------------------------------------------------------------
delete_role() {
  local role="$1"
  aws iam get-role --role-name "${role}" >/dev/null 2>&1 || { echo "건너뜀(${role} 없음)"; return; }
  for p in $(aws iam list-attached-role-policies --role-name "${role}" \
    --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null); do
    aws iam detach-role-policy --role-name "${role}" --policy-arn "${p}" 2>/dev/null
  done
  for p in $(aws iam list-role-policies --role-name "${role}" \
    --query 'PolicyNames' --output text 2>/dev/null); do
    aws iam delete-role-policy --role-name "${role}" --policy-name "${p}" 2>/dev/null
  done
  aws iam delete-role --role-name "${role}" 2>/dev/null && echo "deleted ${role}"
}
log "IAM 역할 삭제"
delete_role "${TASK_ROLE_NAME}"
delete_role "${EXEC_ROLE_NAME}"

# ---------------------------------------------------------------------------
# 7. S3 workspace 버킷 비우고 삭제
# ---------------------------------------------------------------------------
log "S3 workspace 버킷 삭제: ${WORKSPACE_BUCKET}"
if aws s3api head-bucket --bucket "${WORKSPACE_BUCKET}" 2>/dev/null; then
  aws s3 rm "s3://${WORKSPACE_BUCKET}" --recursive >/dev/null 2>&1
  aws s3api delete-bucket --bucket "${WORKSPACE_BUCKET}" 2>/dev/null \
    && echo "deleted" || echo "삭제 실패(수동 확인 필요)"
else
  echo "건너뜀(없음)"
fi

# ---------------------------------------------------------------------------
# 8. ECR 이미지 태그 (공유 계정 591379657681 repo — 기본 보존)
#    공유 repo이므로 기본은 보존한다. cross-account 삭제 권한이 있고 정말 지우려면 DELETE_IMAGE=1.
# ---------------------------------------------------------------------------
if [[ "${DELETE_IMAGE:-0}" == "1" ]]; then
  log "DELETE_IMAGE=1 → 공유 repo ECR 이미지 태그 삭제 시도: ${ECR_REPO}:${IMAGE_TAG}"
  aws ecr batch-delete-image --registry-id 591379657681 --repository-name "${ECR_REPO}" \
    --image-ids "imageTag=${IMAGE_TAG}" >/dev/null 2>&1 \
    && echo "deleted" || echo "건너뜀(없음/권한없음)"
else
  log "ECR 이미지 태그(${IMAGE_TAG}) 보존 (공유 repo — DELETE_IMAGE=1로만 삭제)"
fi

# ---------------------------------------------------------------------------
# 9. SQS 큐 + DLQ 삭제
# ---------------------------------------------------------------------------
log "SQS 큐 삭제: ${QUEUE_NAME} / ${DLQ_NAME}"
for qn in "${QUEUE_NAME}" "${DLQ_NAME}"; do
  qurl="$(aws sqs get-queue-url --queue-name "${qn}" --query 'QueueUrl' --output text 2>/dev/null)"
  if [[ -n "${qurl}" && "${qurl}" != "None" ]]; then
    aws sqs delete-queue --queue-url "${qurl}" 2>/dev/null && echo "deleted ${qn}"
  else
    echo "건너뜀(${qn} 없음)"
  fi
done

# 생성 스냅샷 정리
rm -f "${ENV_OUT}"
log "리소스 정리 완료 ✅ (공유 버킷·ECR repo·네트워크 서브넷/SG는 보존됨)"
