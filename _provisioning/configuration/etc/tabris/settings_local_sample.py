# 허용 팀/사용자 (쉼표 구분 문자열). 빈 문자열이면 해당 검사 생략.
# 봇/워크플로우는 user 목록과 무관하게 허용 팀에 포함되기만 하면 허용된다.
ALLOWED_TEAM_IDS = 'T289HMD6H'  # 특정 user만 받는 허용 팀
ALLOWED_USER_IDS = ''  # 허용 사용자(비우면 팀 내 전원 허용)
ALLOWED_ALL_USER_TEAM_IDS = ''  # 이 팀들은 user 목록과 무관하게 전원 허용. 예: 'T289HMD6H,T319XXXXX'
ANTHROPIC_API_KEY = 'sk-ant-...'
BOT_USER_ID = 'U1234567'
CLAUDE_TIMEOUT = 1800  # 샌드박스(sandbox_worker)의 claude 실행 타임아웃(초)
MAX_WORKERS = 5
NERV_MCP_TOKEN = '...'
SENTRY_AUTH_TOKEN = '...'
SLACK_APP_TOKEN = 'xapp-...'
SLACK_BOT_TOKEN = 'xoxb-...'
JIRA_API_KEY = '...'
JIRA_API_USERNAME = '...'
MEMORY_S3_BUCKET = 'hbsmith-tabris-memory'  # 빈 문자열이면 memory S3 동기화 기능 비활성화
GITHUB_PAT = '...'
ARTIFACTS_S3_BUCKET = 'hbsmith-tabris-artifacts'
ARTIFACTS_BASE_URL = 'https://tabris-artifacts.hbsmith.io'
DOCUMENTS_S3_BUCKET = 'hbsmith-tabris-documents'  # 사내 참고 자료 버킷 (aws_inspect Fast Path 대상)

# --- Fargate 설정 (샌드박스는 ECS Fargate 워밍 풀로 실행) ---
# run_create_poc.sh 실행 후 출력되는 값을 채워 넣는다. (poc_resources.env 참고)
WORKSPACE_S3_BUCKET = 'hbsmith-tabris-workspace'  # 신규 workspace 버킷 (prompt/input/cancel)
ECS_CLUSTER = 'tabris'  # 봇 런타임: 취소(StopTask)에 사용
# 아래는 봇 런타임이 아니라 프로비저닝 스크립트(run_create_poc.sh / run_terminate_poc.sh)가
# 워밍 풀 인프라를 생성·삭제할 때 읽는 값이다.
ECS_SANDBOX_TASK_DEFINITION = 'tabris-sandbox'
ECS_SUBNET_IDS = 'subnet-xxxxxxxx,subnet-yyyyyyyy'  # 기본 VPC 미사용 시 수동 지정 (CSV)
ECS_SECURITY_GROUP_ID = 'sg-xxxxxxxx'

# --- 워밍 풀(SQS) 디스패치 ---
# 봇은 이 SQS FIFO 큐로 잡을 적재하고, 상주 워커 풀(ECS Service)이 소비한다.
# 필수 — 비어 있으면 봇이 기동을 거부한다.
# run_create_poc.sh 실행 후 출력되는 큐 URL을 채운다. (.fifo로 끝남)
SQS_QUEUE_URL = 'https://sqs.ap-northeast-2.amazonaws.com/788968797716/tabris-sandbox-jobs.fifo'  # 예시
