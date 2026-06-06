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

# --- Fargate 설정 (샌드박스는 항상 ECS Fargate RunTask로 실행) ---
# run_create_poc.sh 실행 후 출력되는 값을 채워 넣는다. (poc_resources.env 참고)
WORKSPACE_S3_BUCKET = 'hbsmith-tabris-workspace'  # 신규 workspace 버킷 (prompt/input/cancel)
ECS_CLUSTER = 'tabris'
ECS_SANDBOX_TASK_DEFINITION = 'tabris-sandbox'
ECS_SUBNET_IDS = 'subnet-xxxxxxxx,subnet-yyyyyyyy'  # 기본 VPC 퍼블릭 서브넷 (CSV)
ECS_SECURITY_GROUP_ID = 'sg-xxxxxxxx'
ECS_ASSIGN_PUBLIC_IP = 'ENABLED'  # 퍼블릭 서브넷이므로 ENABLED (아웃바운드 인터넷용)
