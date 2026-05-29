#!/usr/bin/env bash
#
# pull_image.sh — ECR에서 tabris sandbox 이미지를 받아와 로컬 태그로 올려둔다.
#
# `docker build -t hbsmith-claude-sandbox /opt/tabris` 의 드롭인 대체.
# pull 한 ECR 이미지를 기존 로컬 이름(hbsmith-claude-sandbox)으로 태깅하므로
# run_server.py / settings_local.py 는 수정할 필요가 없다.
#
# 인증: EC2 인스턴스 프로파일(IAM role) 자격증명을 사용한다. (IMDS)
#       role 에 ecr:GetAuthorizationToken / ecr:BatchGetImage /
#       ecr:GetDownloadUrlForLayer / ecr:BatchCheckLayerAvailability 권한 필요.
#
# 사용법:
#   ./pull_image.sh                 # :latest 를 받아 hbsmith-claude-sandbox 로 태깅
#   IMAGE_TAG=v1.2.3 ./pull_image.sh

set -euo pipefail

AWS_REGION='ap-northeast-2'
REGISTRY='591379657681.dkr.ecr.ap-northeast-2.amazonaws.com'
ECR_IMAGE='591379657681.dkr.ecr.ap-northeast-2.amazonaws.com/hbsmith/tabris'
IMAGE_TAG="${IMAGE_TAG:-latest}"
LOCAL_TAG='hbsmith-claude-sandbox'

REMOTE_IMAGE="${ECR_IMAGE}:${IMAGE_TAG}"

log() { printf '%s\n' "########################################" "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" "########################################"; }

log "ECR login: ${REGISTRY}"
aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin "${REGISTRY}"

log "Pull: ${REMOTE_IMAGE}"
docker pull "${REMOTE_IMAGE}"

log "Tag: ${REMOTE_IMAGE} -> ${LOCAL_TAG}"
docker tag "${REMOTE_IMAGE}" "${LOCAL_TAG}"

log "Prune dangling images (이전 빌드/pull 로 태그를 잃은 이미지 정리)"
docker image prune -f

log "Done. Local image ready: ${LOCAL_TAG}"
docker image inspect "${LOCAL_TAG}" --format 'id={{.Id}} created={{.Created}}'
