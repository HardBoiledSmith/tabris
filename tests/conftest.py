import sys
import types
from concurrent.futures import Future
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest


def _install_settings_local_stub() -> None:
    if 'settings_local' in sys.modules:
        return
    m = types.ModuleType('settings_local')
    m.ALLOWED_TEAM_ID = 'T_ALLOWED'
    m.ANTHROPIC_API_KEY = 'sk-test'
    m.BOT_USER_ID = 'UBOT'
    m.CLAUDE_TIMEOUT = 30
    m.DOCKER_IMAGE = 'test-image'
    m.JIRA_API_KEY = 'jira-key'
    m.JIRA_API_USERNAME = 'jira-user'
    m.MAX_WORKERS = 2
    m.NERV_MCP_TOKEN = 'nerv-token'
    m.SENTRY_AUTH_TOKEN = 'sntrys_test'
    m.SLACK_APP_TOKEN = 'xapp-test'
    m.SLACK_BOT_TOKEN = 'xoxb-test'
    m.MEMORY_S3_BUCKET = 'hbsmith-tabris-memory'
    m.MEMORY_S3_SYNC_ENABLED = False
    m.MEMORY_S3_SYNC_TIMEOUT = 60
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
        install_claude_popen_mock(monkeypatch, stdout_text=result_text, returncode=0)
        return None

    return _set


@pytest.fixture
def run_server_module():
    return run_server
