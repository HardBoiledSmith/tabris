"""EC2 IMDSv2 임시 자격증명 조회 단위 테스트."""

import json
from unittest.mock import patch

import pytest

import run_server

_ORIGINAL_FETCH_EC2_CREDS = run_server.fetch_ec2_instance_role_credentials


class _ImdsHttpResponse:
    """urllib.request.urlopen 컨텍스트 매니저 응답 흉내."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


@pytest.fixture(autouse=True)
def _use_real_fetch_ec2_credentials(monkeypatch, _mock_ec2_imds_credentials):
    """conftest autouse mock 이후 실제 IMDS 구현을 복원한다."""

    monkeypatch.setattr(
        run_server,
        'fetch_ec2_instance_role_credentials',
        _ORIGINAL_FETCH_EC2_CREDS,
    )


def _urlopen_side_effect(token_body: bytes, role_name: str, creds_payload: dict):
    calls: list[str] = []

    def _fake_urlopen(req, timeout=5):
        calls.append(req.full_url)
        if req.full_url.endswith('/api/token') and req.method == 'PUT':
            return _ImdsHttpResponse(token_body)
        if req.full_url.endswith('/iam/security-credentials/'):
            return _ImdsHttpResponse(role_name.encode('utf-8'))
        if f'/iam/security-credentials/{role_name}' in req.full_url:
            return _ImdsHttpResponse(json.dumps(creds_payload).encode('utf-8'))
        raise AssertionError(f'unexpected IMDS URL: {req.full_url}')

    return _fake_urlopen, calls


def test_fetch_ec2_instance_role_credentials_success():
    creds_payload = {
        'Code': 'Success',
        'AccessKeyId': 'AKIATEST',
        'SecretAccessKey': 'secret',
        'Token': 'session-token',
    }
    fake_urlopen, calls = _urlopen_side_effect(b'imds-token', 'TabrisInstanceRole', creds_payload)

    with patch('run_server.urllib.request.urlopen', side_effect=fake_urlopen):
        got = run_server.fetch_ec2_instance_role_credentials()

    assert got == {
        'AWS_ACCESS_KEY_ID': 'AKIATEST',
        'AWS_SECRET_ACCESS_KEY': 'secret',
        'AWS_SESSION_TOKEN': 'session-token',
    }
    assert calls[0].endswith('/api/token')
    assert calls[1].endswith('/iam/security-credentials/')
    assert calls[2].endswith('/iam/security-credentials/TabrisInstanceRole')


def test_fetch_ec2_instance_role_credentials_non_success_code():
    creds_payload = {'Code': 'Failed', 'Message': 'nope'}
    fake_urlopen, _calls = _urlopen_side_effect(b't', 'role', creds_payload)

    with patch('run_server.urllib.request.urlopen', side_effect=fake_urlopen):
        with pytest.raises(RuntimeError, match='Code'):
            run_server.fetch_ec2_instance_role_credentials()


def test_run_claude_injects_imds_credentials_into_docker_cmd(monkeypatch):
    monkeypatch.setattr(
        run_server,
        'fetch_ec2_instance_role_credentials',
        lambda: {
            'AWS_ACCESS_KEY_ID': 'AKIAINJECT',
            'AWS_SECRET_ACCESS_KEY': 'sk-inject',
            'AWS_SESSION_TOKEN': 'st-inject',
        },
    )
    captured: dict = {}
    from tests.helpers import install_claude_popen_mock

    install_claude_popen_mock(monkeypatch, stdout_text='ok', returncode=0, cmd_capture=captured)

    event = {'channel_type': 'im', 'ts': '1.1', 'thread_ts': '1.1', 'user': 'UTEST'}
    run_server.run_claude(event, '', 'hello')

    cmd = captured['cmd']
    env: dict[str, str] = {}
    volumes: list[str] = []
    i = 0
    while i < len(cmd):
        if cmd[i] == '-e' and i + 1 < len(cmd):
            key, _, val = cmd[i + 1].partition('=')
            env[key] = val
            i += 2
        elif cmd[i] == '-v' and i + 1 < len(cmd):
            volumes.append(cmd[i + 1])
            i += 2
        else:
            i += 1
    assert env['AWS_ACCESS_KEY_ID'] == 'AKIAINJECT'
    assert env['AWS_SECRET_ACCESS_KEY'] == 'sk-inject'
    assert env['AWS_SESSION_TOKEN'] == 'st-inject'
    assert env['AWS_DEFAULT_REGION'] == run_server.AWS_DEFAULT_REGION

    # run workspace는 runs/{thread_ts} 경로를 사용한다.
    assert any(f'{run_server.SANDBOX_RUNS_DIR}/1.1:/workspace:rw' == v for v in volumes)
    # 사용자별 memory 볼륨이 포함되어 있다.
    assert any(
        f'{run_server.SANDBOX_USERS_DIR}/UTEST/{run_server.WORKSPACE_MEMORY_SUBDIR}:{run_server.CLAUDE_MEMORY_CONTAINER_PATH}:rw'
        == v
        for v in volumes
    )
