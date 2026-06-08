# tabris

Slack Bolt (Socket Mode) 기반 봇. 사용자의 멘션/DM에 응답하여 **ECS Fargate 샌드박스(워밍 풀)**에서 Claude Code를 실행하고 결과를 Slack 스레드로 돌려준다.

## 구성

- `run_server.py` — Slack Bolt 앱(Socket Mode). 이벤트 수신 시 잡을 **SQS FIFO 큐**에 적재한다(워밍 풀). `SQS_QUEUE_URL` 미설정 시 레거시로 ECS RunTask를 직접 호출한다.
- `sandbox_worker.py` — Fargate 샌드박스 워커. 워밍 풀 모드에선 SQS를 long-poll 하며 잡을 처리하고, 일정 사용량/수명마다 스스로 은퇴(ECS가 교체)한다. 큐 미설정 시 RunTask로 1건 처리 후 종료.
- `tabris_slack_utils.py` — Slack 게시(Block Kit 변환)·아티팩트 업로드 등 봇/워커 공용 유틸.
- `Dockerfile` — Fargate 샌드박스 이미지(`hbsmith/tabris`). `python:3.12-slim` 위에 node20 + `@anthropic-ai/claude-code` + aws CLI를 올린 런타임. ECR로 푸시해 사용.
- `_provisioning/fargate/` — SQS·ECS 클러스터/서비스(워밍 풀)·오토스케일·IAM·태스크 정의 생성/삭제 스크립트(`run_create_poc.sh` / `run_terminate_poc.sh`).
- `_provisioning/` — 봇을 돌리는 Vagrant VM 프로비저닝. 상세는 아래 섹션.

### 디스패치 아키텍처 (워밍 풀)

봇은 요청마다 콜드 Fargate 태스크를 띄우는 대신(부팅 ~1분+), 상주 워커 풀에 잡을 넘긴다.

1. 봇이 Slack 이벤트 수신 → 대기 메시지 게시 → prompt/입력을 S3에 올리고 **SQS FIFO**에 잡 적재. `MessageGroupId=user_id`로 같은 유저 잡을 직렬화한다(유저별 memory 레이스·풀 과점유 방지).
2. **ECS Service**(Fargate Spot)로 떠 있는 워커(`sandbox_worker.py`)가 큐를 소비해 Claude 실행 후 결과를 Slack에 게시한다.
3. 워커는 잡 경계마다 workspace를 비우고(유저 간 격리), `MAX_JOBS`/`MAX_LIFETIME_SEC`마다 은퇴 → ECS가 새 태스크로 교체(오래 재활용하지 않음).
4. 멱등/취소는 S3 마커(`jobs/{job_id}/done`·`cancel`)로 처리(DynamoDB 미사용). 취소는 마커를 먼저 쓰고 `StopTask` 한다(좀비 재실행 방지).
5. 오토스케일(step scaling): 대기 메시지가 생기면 워커를 늘리고(0→N), 백로그가 비면 0으로 복귀. 업무시간엔 warm 1대를 유지한다(스케줄드 floor).

`SQS_QUEUE_URL`을 비우면 레거시 1회용 RunTask 경로로 동작한다(롤백 안전망).

## 1. Slack App 설정 (최초 1회)

[api.slack.com/apps](https://api.slack.com/apps) 에서 진행.

### ① 앱 생성 (App Manifest)
새 워크스페이스에 앱을 만들 때는 아래 매니페스트로 한 번에 설정한다. (봇 표시명·OAuth 스코프·`app_mention` / `message.im` 구독·Socket Mode·Interactivity 등이 포함된다.)

1. **Create New App** → **From an app manifest** → 대상 Workspace 선택.
2. 아래 JSON을 붙여넣고 **Next** → 내용 확인 후 **Create**.
3. 생성 후 좌측 **App Manifest**에서 동일 JSON이 반영됐는지 확인. 스코프·이벤트를 바꿨다면 이후 **Reinstall to Workspace**가 필요하다.

```json
{
    "display_information": {
        "name": "HBsmith Tabris Bot",
        "description": "HBsmith 업무 자동화를 위한 AI Agent",
        "background_color": "#121212"
    },
    "features": {
        "bot_user": {
            "display_name": "tabris",
            "always_online": true
        }
    },
    "oauth_config": {
        "scopes": {
            "bot": [
                "files:write",
                "app_mentions:read",
                "channels:history",
                "channels:read",
                "chat:write",
                "files:read",
                "groups:history",
                "groups:read",
                "im:history",
                "mpim:history",
                "users:read"
            ]
        },
        "pkce_enabled": true
    },
    "settings": {
        "event_subscriptions": {
            "bot_events": [
                "app_mention",
                "message.im",
                "message.mpim"
            ]
        },
        "interactivity": {
            "is_enabled": true
        },
        "org_deploy_enabled": false,
        "socket_mode_enabled": true,
        "token_rotation_enabled": false,
        "is_mcp_enabled": false
    }
}
```

### ② Socket Mode App-Level Token
매니페스트로 Socket Mode는 켜지지만, 연결용 **App-Level Token**은 별도로 발급한다.

- 좌측 **Socket Mode** → **Generate Token** (또는 App-Level Tokens).
- Token Name: `tabris-socket`, Scope: `connections:write`.
- 발급된 `xapp-...` 토큰을 `SLACK_APP_TOKEN`에 사용.

### ③ Install to Workspace
- **Install App** → 설치 → `xoxb-...` 봇 토큰 발급 → `SLACK_BOT_TOKEN`에 사용.
- 스코프 변경 시 반드시 **Reinstall to Workspace** 재실행 후 토큰 갱신.

### ④ Bot User ID 확인
- Slack 클라이언트에서 봇 프로필 클릭 → **Copy member ID** (`U...`) → `BOT_USER_ID`.

### ⑤ 채널 초대
- 봇이 응답할 채널에서 `/invite @tabris`.
- DM은 초대 불필요 (워크스페이스 설치 시점부터 가능).

## 2. 로컬 Mac 개발

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# → SLACK_BOT_TOKEN, SLACK_APP_TOKEN, BOT_USER_ID, ANTHROPIC_API_KEY 채우기

# 봇만 로컬에서 Socket Mode로 띄운다. 샌드박스 실행은 ECS Fargate(SQS 워밍 풀 또는 RunTask)가
# 담당하므로, settings_local에 ECS_*·(선택)SQS_QUEUE_URL과 AWS 자격증명이 필요하다.
python run_server.py
```

## 3. Vagrant VM 실행

### 사전 준비
- macOS + [Parallels Desktop](https://www.parallels.com/products/desktop/)
- [Vagrant](https://www.vagrantup.com/) + `vagrant-parallels` 플러그인
- `hbsmith/al2023` box 추가

### ① 토큰 설정
```bash
cp _provisioning/configuration/etc/tabris/settings_local.py.example \
   _provisioning/configuration/etc/tabris/settings_local.py
# → SLACK_*, BOT_USER_ID, ANTHROPIC_API_KEY 채우기
```

### ② GitHub 접근용 SSH 키 배치
```bash
cp ~/.ssh/id_ed25519 _provisioning/configuration/root/.ssh/id_ed25519
# 해당 키의 public 키가 HardBoiledSmith/tabris repo에 read 권한을 가져야 함
```

### ③ VM 기동
```bash
cd _provisioning
vagrant up
# 특정 브랜치로: BRANCH=dev vagrant up   (기본값: master)
```
최초 프로비저닝은 5–10분 소요 (docker 설치 + sandbox 이미지 빌드 + venv 설정).

### ④ 상태 확인
```bash
vagrant ssh -c 'sudo systemctl status tabris'
vagrant ssh -c 'sudo tail -f /var/log/tabris/run_server.log'
```
- 초대된 Slack 채널에서 봇을 멘션하거나 DM을 보내 `RAW incoming body` / `[mention]` 로그가 찍히는지 확인.

### ⑤ 업데이트 반영
```bash
# remote(`master`)에 push된 변경 반영
vagrant ssh -c 'cd /opt/tabris && sudo git pull && sudo systemctl restart tabris'

# provisioning.py 자체가 바뀐 경우
vagrant provision

# 샌드박스 이미지(Dockerfile/스킬/워커) 변경 반영 — ECR 빌드·푸시 + task def 갱신 + 풀 서비스 업데이트.
# (봇 호스트가 아니라 ECR/ECS에 반영된다. AWS_PROFILE은 해당 계정으로.)
cd _provisioning/fargate && AWS_PROFILE=<acct-profile> ./run_create_poc.sh
```

### ⑥ 종료 / 정리
```bash
vagrant halt           # VM 중지
vagrant destroy -f     # VM 완전 삭제
```

## 환경변수

| 변수 | 필수 | 기본값 | 설명 |
|------|------|--------|------|
| `SLACK_BOT_TOKEN` | ✓ | — | `xoxb-` 봇 토큰 |
| `SLACK_APP_TOKEN` | ✓ | — | `xapp-` 앱 토큰 (Socket Mode) |
| `BOT_USER_ID` | ✓ | — | 봇 member ID (`U...`) |
| `ANTHROPIC_API_KEY` | ✓ | — | Claude Code 실행용 API 키 |
| `NERV_MCP_TOKEN` | ✓ | — | op-nerv MCP 서버 인증 토큰 |
| `CLAUDE_TIMEOUT` |   | `1800` | Claude 실행 타임아웃(초) |
| `MAX_WORKERS` |   | `5` | 봇 이벤트 동시 처리 스레드 수 |
| `MEMORY_S3_BUCKET` |   | `hbsmith-tabris-memory` | 사용자별 memory S3 버킷명. **빈 문자열이면 S3 sync 기능 비활성화** (로컬·Vagrant) |
| `WORKSPACE_S3_BUCKET` | ✓ | — | prompt/입력·멱등 마커(`jobs/`)용 버킷 |
| `SQS_QUEUE_URL` |   | `''` | 워밍 풀 디스패치 큐(FIFO). **빈 문자열이면 레거시 RunTask 경로** |
| `ECS_CLUSTER` / `ECS_SANDBOX_TASK_DEFINITION` / `ECS_SUBNET_IDS` / `ECS_SECURITY_GROUP_ID` / `ECS_ASSIGN_PUBLIC_IP` | ✓ | — | Fargate 디스패치 설정(`run_create_poc.sh` 출력값) |

VM에서는 `/etc/tabris/settings_local.py`에 Python 상수로 정의한다. 샘플은 `_provisioning/configuration/etc/tabris/settings_local_sample.py` 참고. 로컬 개발 시에도 동일하게 `settings_local`를 import할 수 있는 경로에 두면 된다.

> 워커(샌드박스) 쪽 튜닝값 `MAX_JOBS`(기본 2)·`MAX_LIFETIME_SEC`(2700)·`SQS_VISIBILITY_TIMEOUT_SEC`(360)는 **Fargate 태스크 정의의 env**로 주입된다(`_provisioning/fargate/task_definition_sandbox.json`, `run_create_poc.sh`가 치환).
>
> 시크릿(`ANTHROPIC_API_KEY`·`SLACK_BOT_TOKEN`·`NERV_MCP_TOKEN`·`ATLASSIAN_ROVO_MCP_TOKEN`·`GITHUB_PAT`·`SENTRY_AUTH_TOKEN`)은 평문 env가 아니라 **SSM Parameter Store(SecureString, `/tabris/sandbox/*`)**에 저장하고, 태스크 정의의 `secrets` 블록이 `valueFrom`으로 참조한다. `run_create_poc.sh`가 `settings_local.py`에서 읽어 SSM에 적재하고 execution role에 읽기 권한을 부여하므로, 콘솔/`describe-task-definition`/RunTask 호출 어디에도 평문이 노출되지 않는다. 키 회전 시 SSM 값만 갱신 후 새 태스크가 뜨면 반영된다.

## 인프라 선행 조건 (운영 배포 시 필수)

`MEMORY_S3_BUCKET`을 설정해 memory S3 sync를 운영하려면 아래가 갖춰져 있어야 한다.

1. **S3 버킷** `hbsmith-tabris-memory` 생성 (리전 `ap-northeast-2`, Block Public Access, SSE-S3 이상)
2. **tabris EC2 instance profile** IAM policy 추가:
   ```json
   {
     "Effect": "Allow",
     "Action": ["s3:ListBucket"],
     "Resource": "arn:aws:s3:::hbsmith-tabris-memory",
     "Condition": {"StringLike": {"s3:prefix": ["users/*"]}}
   },
   {
     "Effect": "Allow",
     "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
     "Resource": "arn:aws:s3:::hbsmith-tabris-memory/users/*"
   }
   ```
   업로드 sync(`sync_memory_to_s3`)는 로컬 memory를 정본으로 S3 prefix를 미러링하며, 로컬에 없는 객체는 `aws s3 sync --delete`로 제거한다. 로컬 memory가 비어 있으면 업로드는 건너뛴다(S3 백업 보호).
3. `hbsmith-tabris-documents` 버킷 및 `aws_inspect` Fast Path와 **분리 유지** — 에이전트가 memory 버킷에 접근하지 않도록 한다.

### 웹 아티팩트 호스팅 (구축 완료)

`web-artifacts-builder` 스킬로 생성한 `bundle.html`을 공개 URL로 공유하는 인프라가 구축되어 있다.

- **S3 버킷** `hbsmith-tabris-artifacts` (리전 `ap-northeast-2`) — 에이전트가 `upload-artifact.sh`로 직접 업로드.
- **CloudFront + Route 53** `tabris-artifacts.hbsmith.io` — S3 앞단 웹 호스팅 엔드포인트.
- **tabris EC2 instance profile** IAM policy: `s3:PutObject` on `arn:aws:s3:::hbsmith-tabris-artifacts/*`.

샌드박스에 들어가는 스킬·`upload-artifact.sh`·`SKILL.md`·`CLAUDE.md` 변경은 **이미지를 재빌드·ECR 푸시**해야 반영된다. `run_create_poc.sh`가 빌드·푸시·task def 갱신·서비스 업데이트를 한 번에 수행한다(이미지 변경 없이 인프라만 재적용할 땐 `SKIP_BUILD=1`):

```bash
cd _provisioning/fargate && AWS_PROFILE=<acct-profile> ./run_create_poc.sh
```
