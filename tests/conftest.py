import json
import os
import sys
import tempfile
import types
from concurrent.futures import Future
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

# run_server import 전에 이벤트 JSON 로그 경로를 tmp로 돌린다(실 /var/log 오염 방지).
# FileHandler가 모듈 로드 시점에 열리므로 import보다 먼저 설정해야 한다.
EVENT_LOG_PATH = os.path.join(tempfile.gettempdir(), 'tabris_test_events.jsonl.log')
os.environ['TABRIS_EVENT_LOG_PATH'] = EVENT_LOG_PATH
try:
    os.remove(EVENT_LOG_PATH)
except OSError:
    pass


def _install_settings_local_stub() -> None:
    if 'settings_local' in sys.modules:
        return
    m = types.ModuleType('settings_local')
    m.ALLOWED_TEAM_IDS = 'T_ALLOWED'
    m.ALLOWED_USER_IDS = 'U_USER'
    m.ALLOWED_ALL_USER_TEAM_IDS = ''
    m.ANTHROPIC_API_KEY = 'sk-test'
    m.BOT_USER_ID = 'UBOT'
    m.CLAUDE_TIMEOUT = 30
    m.DOCKER_IMAGE = 'test-image'
    m.GITHUB_PAT = 'ghp-test'
    m.JIRA_API_KEY = 'jira-key'
    m.JIRA_API_USERNAME = 'jira-user'
    m.MAX_WORKERS = 2
    m.NERV_MCP_TOKEN = 'nerv-token'
    m.SENTRY_AUTH_TOKEN = 'sntrys_test'
    m.SLACK_APP_TOKEN = 'xapp-test'
    m.SLACK_BOT_TOKEN = 'xoxb-test'
    m.MEMORY_S3_BUCKET = ''  # 빈 값 = memory S3 동기화 비활성 (테스트 기본)
    m.MEMORY_S3_SYNC_TIMEOUT = 60
    m.ARTIFACTS_S3_BUCKET = 'hbsmith-tabris-artifacts'
    m.ARTIFACTS_BASE_URL = 'https://tabris-artifacts.hbsmith.io'
    m.DOCUMENTS_S3_BUCKET = 'hbsmith-tabris-documents'
    sys.modules['settings_local'] = m


_install_settings_local_stub()

from slack_sdk.web.client import WebClient  # noqa: E402

_auth_patch = patch.object(
    WebClient,
    'auth_test',
    return_value={'ok': True, 'user_id': 'UBOT', 'bot_id': 'B1'},
)
_auth_patch.start()

import run_server  # noqa: E402
from tests.helpers import install_claude_popen_mock  # noqa: E402


def _sync_submit(fn, *args, **kwargs):
    future: Future = Future()
    try:
        future.set_result(fn(*args, **kwargs))
    except BaseException as exc:
        future.set_exception(exc)
    return future


@pytest.fixture(autouse=True)
def _mock_ec2_imds_credentials(monkeypatch):
    """EC2 IMDS 없이도 Docker 실행 경로가 동작하도록 임시 자격증명을 고정한다."""

    monkeypatch.setattr(
        run_server,
        'fetch_ec2_instance_role_credentials',
        lambda: {
            'AWS_ACCESS_KEY_ID': 'aws-ak-test',
            'AWS_SECRET_ACCESS_KEY': 'aws-sk-test',
            'AWS_SESSION_TOKEN': 'aws-st-test',
        },
    )


@pytest.fixture(autouse=True)
def _sync_executor(monkeypatch):
    """on_mention/on_dm → executor.submit을 동기 실행으로 바꿔 테스트 결정성 확보."""
    monkeypatch.setattr(run_server.executor, 'submit', _sync_submit)


@pytest.fixture
def event_log_lines():
    """이벤트 JSON 로그 파일을 비우고, 호출 시 현재까지 기록된 JSON 객체 리스트를 돌려준다."""
    handler = next((h for h in run_server.event_logger.handlers if hasattr(h, 'flush')), None)

    def _truncate():
        try:
            with open(EVENT_LOG_PATH, 'w', encoding='utf-8'):
                pass
        except OSError:
            pass

    _truncate()

    def _read():
        if handler:
            handler.flush()
        try:
            with open(EVENT_LOG_PATH, encoding='utf-8') as fp:
                return [json.loads(line) for line in fp if line.strip()]
        except FileNotFoundError:
            return []

    yield _read
    _truncate()


@pytest.fixture
def slack_client():
    """모의 Slack WebClient. chat_postMessage는 ts가 있는 응답을 돌려준다."""
    client = MagicMock()
    client.chat_postMessage.return_value = {'ok': True, 'ts': '1700000000.000100'}
    client.chat_update.return_value = {'ok': True, 'ts': '1700000000.000100'}
    client.conversations_replies.return_value = {'messages': []}
    client.files_upload_v2.return_value = {'ok': True}
    return client


@pytest.fixture
def fake_claude_ok(monkeypatch):
    """Claude Docker 실행을 성공 응답으로 흉내낸다."""

    def _set(result_text: str = 'Hello from Claude'):
        # 실제 Claude `--output-format json` 출력을 흉내낸 단일 JSON 객체.
        envelope = json.dumps(
            {
                'type': 'result',
                'subtype': 'success',
                'is_error': False,
                'result': result_text,
                'total_cost_usd': 0.0123,
                'usage': {'input_tokens': 100, 'output_tokens': 20},
                'modelUsage': {
                    # 메인 모델(비용 큼) + 보조 모델(비용 작음). _primary_model은 메인을 골라야 한다.
                    'claude-opus-4-8[1m]': {'inputTokens': 100, 'outputTokens': 20, 'costUSD': 0.0123},
                    'claude-haiku-4-5-20251001': {'inputTokens': 40, 'outputTokens': 30, 'costUSD': 0.0005},
                },
            }
        )
        install_claude_popen_mock(monkeypatch, stdout_text=envelope, returncode=0)
        return None

    return _set


@pytest.fixture
def run_server_module():
    return run_server
