# tabris

Slack Bolt (Socket Mode) 기반 봇. 사용자의 멘션/DM에 응답하여 Docker 컨테이너에서 Claude Code를 실행하고 결과를 Slack 스레드로 돌려준다.

## 구성

- `run_server.py` — Slack Bolt 앱. Socket Mode로 연결, 이벤트 수신 시 `docker run`으로 `hbsmith-claude-sandbox` 컨테이너에서 Claude Code 실행.
- `Dockerfile` — `hbsmith-claude-sandbox` 이미지. `node:20-slim` 위에 `@anthropic-ai/claude-code`를 전역 설치한 최소 런타임.
- `mcp.json` — Claude Code MCP 서버 설정. 기본은 빈 설정으로 시작 가능.
- `_provisioning/requirements.txt` — Python 의존성 (`slack-bolt`, `slack-markdown-parser` 등).
- `_provisioning/` — Vagrant VM 프로비저닝. 상세는 아래 섹션.

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
                "users:read"
            ]
        },
        "pkce_enabled": true
    },
    "settings": {
        "event_subscriptions": {
            "bot_events": [
                "app_mention",
                "message.im"
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

docker build -t hbsmith-claude-sandbox .
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

# Dockerfile / sandbox 이미지 재빌드
vagrant ssh -c 'sudo docker build -t hbsmith-claude-sandbox /opt/tabris'
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
| `DOCKER_IMAGE` |   | `hbsmith-claude-sandbox` | sandbox 이미지명 |
| `CLAUDE_TIMEOUT` |   | `120` | Claude 실행 타임아웃(초) |
| `MAX_WORKERS` |   | `5` | 동시 처리 스레드 수 |
| `MEMORY_S3_BUCKET` |   | `hbsmith-tabris-memory` | 사용자별 memory S3 버킷명 |
| `MEMORY_S3_SYNC_ENABLED` |   | `True` | S3 sync 활성화 여부. 로컬·Vagrant는 `False`로 설정 |
| `MEMORY_S3_SYNC_TIMEOUT` |   | `60` | `aws s3 sync` subprocess 타임아웃(초) |
| `ARTIFACT_S3_BUCKET` |   | `hbsmith-tabris-artifacts` | web artifact S3 버킷명 |
| `ARTIFACT_BASE_URL` |   | `https://tabris-artifacts.hbsmith.io` | Slack에 공유할 공개 URL prefix |
| `ARTIFACT_S3_SYNC_ENABLED` |   | `True` | web artifact S3 업로드 활성화 여부. 로컬·Vagrant는 `False`로 설정 |
| `ARTIFACT_S3_SYNC_TIMEOUT` |   | `60` | artifact `aws s3 sync` subprocess 타임아웃(초) |

VM에서는 `/etc/tabris/settings_local.py`에 Python 상수로 정의한다. 로컬 개발 시에도 동일하게 `settings_local`를 import할 수 있는 경로에 두면 된다.

## 인프라 선행 조건 (운영 배포 시 필수)

### Web Artifact 호스팅

web-artifacts-builder 스킬로 생성된 `bundle.html`은 Docker 종료 후 `s3://hbsmith-tabris-artifacts/{user_id}/{unix_ts}/`에 업로드되고, CloudFront(`tabris-artifacts.hbsmith.io`)를 통해 Slack 스레드에 공개 URL로 공유된다. 인프라(S3 버킷, CloudFront 배포, Route 53 레코드)는 별도로 구축한다.

**배포 시 sandbox 이미지 재빌드 필수**: `CLAUDE.md`, `web-artifacts-builder` 스킬, `bundle-artifact.sh` 변경 사항이 이미지에 반영되어야 한다.

```bash
# VM에서 코드 반영 후 이미지 재빌드
vagrant ssh -c 'cd /opt/tabris && sudo git pull && sudo docker build -t hbsmith-claude-sandbox /opt/tabris && sudo systemctl restart tabris'
```

tabris EC2 instance profile에 아래 IAM 정책을 추가한다.

```json
{
  "Effect": "Allow",
  "Action": ["s3:ListBucket"],
  "Resource": "arn:aws:s3:::hbsmith-tabris-artifacts"
},
{
  "Effect": "Allow",
  "Action": ["s3:PutObject"],
  "Resource": "arn:aws:s3:::hbsmith-tabris-artifacts/*"
}
```

`MEMORY_S3_SYNC_ENABLED = True`로 운영하려면 아래가 갖춰져 있어야 한다.

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
