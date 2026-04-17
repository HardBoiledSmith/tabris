# Copy this file to settings_local.py in the same directory and fill in real values.
# During `vagrant up`, provisioning.py copies it to /etc/tabris/settings_local.py (chmod 600).
# bot.py reads it via the TABRIS_SETTINGS env var and seeds os.environ with each UPPER_CASE constant.

SLACK_BOT_TOKEN = 'xoxb-...'
SLACK_APP_TOKEN = 'xapp-...'
BOT_USER_ID = 'U1234567'
ANTHROPIC_API_KEY = 'sk-ant-...'

# Optional — defaults are fine for the standard VM layout.
MCP_CONFIG_PATH = '/opt/tabris/mcp.json'
DOCKER_IMAGE = 'my-claude-sandbox'
CLAUDE_TIMEOUT = 120
MAX_WORKERS = 5
