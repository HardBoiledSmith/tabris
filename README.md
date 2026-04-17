# tabris

Slack Bolt (Socket Mode) 기반 봇. 사용자의 멘션/DM에 응답하여 Docker 컨테이너에서 Claude Code를 실행하고 결과를 Slack 스레드로 돌려준다.

## 구성

- `bot.py` — Slack Bolt 앱. Socket Mode로 연결, 이벤트 수신 시 `docker run`으로 `my-claude-sandbox` 컨테이너에서 Claude Code 실행.
- `Dockerfile` — `my-claude-sandbox` 이미지. `node:20-slim` 위에 `@anthropic-ai/claude-code`를 전역 설치한 최소 런타임.
- `mcp.json` — Claude Code MCP 서버 설정. 기본은 빈 설정으로 시작 가능.
- `requirements.txt` — Python 의존성 (`slack-bolt`, `python-dotenv`).
- `_provisioning/` — Vagrant VM 프로비저닝. 상세는 아래 섹션.

## 1. Slack App 설정 (최초 1회)

[api.slack.com/apps](https://api.slack.com/apps) 에서 진행.

### ① App 생성
- **Create New App** → **From scratch** → App Name: `tabris`, Workspace 선택.

### ② Socket Mode 활성화
- 좌측 메뉴 **Socket Mode** → Enable Socket Mode = **On**.
- App-Level Token 생성: Token Name `tabris-socket`, Scope `connections:write`.
- 발급된 `xapp-...` 토큰을 `SLACK_APP_TOKEN`에 사용.

### ③ OAuth & Permissions — Bot Token Scopes
`bot.py`가 호출하는 API에 필요한 최소 스코프:

| 스코프 | 필요 이유 |
|--------|-----------|
| `app_mentions:read` | `@app.event("app_mention")` — 멘션 수신 |
| `chat:write` | `client.chat_postMessage`, `client.chat_update` |
| `channels:history` | 공개 채널 스레드 히스토리 (`conversations.replies`) |
| `groups:history` | 프라이빗 채널 스레드 |
| `im:history` | DM 메시지 및 스레드 |
| `mpim:history` | 그룹 DM 스레드 |
| `im:read` | DM 채널 메타데이터 |

### ④ Event Subscriptions
- Enable Events = **On**.
- Subscribe to bot events:
  - `app_mention` — 멘션 수신 (`on_mention`)
  - `message.im` — DM 수신 (`on_dm`)
- Socket Mode이므로 Request URL 입력 불필요.

### ⑤ Install to Workspace
- **Install App** → 설치 → `xoxb-...` 봇 토큰 발급 → `SLACK_BOT_TOKEN`에 사용.
- 스코프 변경 시 반드시 **Reinstall to Workspace** 재실행 후 토큰 갱신.

### ⑥ Bot User ID 확인
- Slack 클라이언트에서 봇 프로필 클릭 → **Copy member ID** (`U...`) → `BOT_USER_ID`.

### ⑦ 채널 초대
- 봇이 응답할 채널에서 `/invite @tabris`.
- DM은 초대 불필요 (워크스페이스 설치 시점부터 가능).

## 2. 로컬 Mac 개발

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# → SLACK_BOT_TOKEN, SLACK_APP_TOKEN, BOT_USER_ID, ANTHROPIC_API_KEY 채우기

docker build -t my-claude-sandbox .
python bot.py
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
vagrant ssh -c 'sudo tail -f /var/log/tabris/bot.log'
```
- 초대된 Slack 채널에서 봇을 멘션하거나 DM을 보내 `RAW incoming body` / `[mention]` 로그가 찍히는지 확인.

### ⑤ 업데이트 반영
```bash
# remote(`master`)에 push된 변경 반영
vagrant ssh -c 'cd /opt/tabris && sudo git pull && sudo systemctl restart tabris'

# provisioning.py 자체가 바뀐 경우
vagrant provision

# Dockerfile / sandbox 이미지 재빌드
vagrant ssh -c 'sudo docker build -t my-claude-sandbox /opt/tabris'
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
| `MCP_CONFIG_PATH` |   | `./mcp.json` | MCP 서버 설정 경로 |
| `DOCKER_IMAGE` |   | `my-claude-sandbox` | sandbox 이미지명 |
| `CLAUDE_TIMEOUT` |   | `120` | Claude 실행 타임아웃(초) |
| `MAX_WORKERS` |   | `5` | 동시 처리 스레드 수 |

VM에서는 `/etc/tabris/settings_local.py`에 Python 상수로 정의하면 `bot.py`가 기동 시 `os.environ`에 주입한다. 로컬 Mac에서는 루트의 `.env`가 `load_dotenv()`로 로드된다.
