"""Docker 실행 명령에 SLACK_USER_ID 환경변수가 포함되는지 검증한다."""

from unittest.mock import MagicMock

import run_server
from tests.helpers import install_claude_popen_mock


def test_docker_cmd_includes_slack_user_id(monkeypatch, slack_client):
    """SLACK_USER_ID=<user> 가 docker run 명령의 -e 목록에 포함된다."""
    cmd_capture = {}
    install_claude_popen_mock(
        monkeypatch,
        stdout_text='hello',
        returncode=0,
        cmd_capture=cmd_capture,
    )
    monkeypatch.setattr(run_server, 'post_claude_markdown_to_thread', MagicMock())
    monkeypatch.setattr(run_server, 'post_workspace_artifacts_to_thread', MagicMock())
    monkeypatch.setattr(run_server.shutil, 'rmtree', MagicMock())

    event = {
        'channel': 'D123',
        'channel_type': 'im',
        'team_id': 'T_ALLOWED',
        'ts': '1700000000.000001',
        'thread_ts': '1700000000.000001',
        'user': 'U_USER',
        'text': 'hello',
    }
    run_server.handle_request(event, slack_client)

    cmd = cmd_capture.get('cmd', [])
    assert 'SLACK_USER_ID=U_USER' in cmd, f'SLACK_USER_ID not found in docker cmd: {cmd}'


def test_docker_cmd_includes_bucket_env(monkeypatch, slack_client):
    """DOCUMENTS_S3_BUCKET / MEMORY_S3_BUCKET 가 docker run 명령의 -e 목록에 포함된다."""
    cmd_capture = {}
    install_claude_popen_mock(
        monkeypatch,
        stdout_text='hello',
        returncode=0,
        cmd_capture=cmd_capture,
    )
    monkeypatch.setattr(run_server, 'post_claude_markdown_to_thread', MagicMock())
    monkeypatch.setattr(run_server, 'post_workspace_artifacts_to_thread', MagicMock())
    monkeypatch.setattr(run_server.shutil, 'rmtree', MagicMock())
    # 버킷명이 -e로 전달되는지만 검증한다. 실제 aws s3 sync는 mock으로 막는다.
    monkeypatch.setattr(run_server, 'MEMORY_S3_BUCKET', 'mem-bucket-test')
    monkeypatch.setattr(run_server, 'sync_memory_from_s3', MagicMock())
    monkeypatch.setattr(run_server, 'sync_memory_to_s3', MagicMock())

    event = {
        'channel': 'D123',
        'channel_type': 'im',
        'team_id': 'T_ALLOWED',
        'ts': '1700000000.000001',
        'thread_ts': '1700000000.000001',
        'user': 'U_USER',
        'text': 'hello',
    }
    run_server.handle_request(event, slack_client)

    cmd = cmd_capture.get('cmd', [])
    assert f'DOCUMENTS_S3_BUCKET={run_server.DOCUMENTS_S3_BUCKET}' in cmd, f'DOCUMENTS_S3_BUCKET not in docker cmd: {cmd}'
    assert 'MEMORY_S3_BUCKET=mem-bucket-test' in cmd, f'MEMORY_S3_BUCKET not in docker cmd: {cmd}'
