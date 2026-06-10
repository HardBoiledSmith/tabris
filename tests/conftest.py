import json
import os
import sys
import tempfile
import types
from concurrent.futures import Future
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

# sandbox_worker import 전에 이벤트 JSON 로그 경로를 tmp 파일로 돌린다(stdout 대신).
# 워커의 이벤트 FileHandler가 모듈 로드 시점에 열리므로 import보다 먼저 설정해야 한다.
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
    m.GITHUB_PAT = 'ghp-test'
    m.JIRA_API_KEY = 'jira-key'
    m.JIRA_API_USERNAME = 'jira-user'
    m.MAX_WORKERS = 2
    m.NERV_MCP_TOKEN = 'nerv-token'
    m.SENTRY_AUTH_TOKEN = 'sntrys_test'
    m.SLACK_APP_TOKEN = 'xapp-test'
    m.SLACK_BOT_TOKEN = 'xoxb-test'
    m.MEMORY_S3_BUCKET = ''  # 빈 값 = memory S3 동기화 비활성 (테스트 기본)
    m.ARTIFACTS_S3_BUCKET = 'hbsmith-tabris-artifacts'
    m.ARTIFACTS_BASE_URL = 'https://tabris-artifacts.hbsmith.io'
    m.DOCUMENTS_S3_BUCKET = 'hbsmith-tabris-documents'
    # Fargate 설정 (샌드박스는 항상 Fargate). dispatch 테스트가 쓰는 더미 값.
    m.WORKSPACE_S3_BUCKET = 'test-workspace'
    m.ECS_CLUSTER = 'test-cluster'  # 취소(StopTask)에 사용
    m.SQS_QUEUE_URL = 'https://sqs.test/000000000000/tabris-sandbox-jobs.fifo'
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
import sandbox_worker  # noqa: E402  (이벤트 JSON 로그는 워커가 남긴다)


def _sync_submit(fn, *args, **kwargs):
    future: Future = Future()
    try:
        future.set_result(fn(*args, **kwargs))
    except BaseException as exc:
        future.set_exception(exc)
    return future


@pytest.fixture(autouse=True)
def _mock_ec2_imds_credentials(monkeypatch):
    """EC2 IMDS 없이도 aws CLI 자격증명 경로가 동작하도록 임시 자격증명을 고정한다."""

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
    handler = next((h for h in sandbox_worker.event_logger.handlers if hasattr(h, 'flush')), None)

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
def dispatch_mock(monkeypatch):
    """봇의 SQS 디스패치(`_enqueue_claude_job`)를 MagicMock으로 대체해 호출 여부/인자만 검사한다."""
    mock = MagicMock(return_value='job-1')
    monkeypatch.setattr(run_server, '_enqueue_claude_job', mock)
    return mock


@pytest.fixture
def run_server_module():
    return run_server
