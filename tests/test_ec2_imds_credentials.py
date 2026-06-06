"""EC2 IMDSv2 임시 자격증명 조회 단위 테스트."""

import json
import subprocess
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


def test_fetch_credentials_via_aws_profile_success():
    export_payload = {
        'Version': 1,
        'AccessKeyId': 'AKIAPROFILE',
        'SecretAccessKey': 'profile-secret',
        'SessionToken': 'profile-session-token',
        'Expiration': '2026-01-01T00:00:00Z',
    }
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(export_payload), stderr='')

    with patch('run_server.subprocess.run', return_value=completed) as mock_run:
        got = run_server.fetch_credentials_via_aws_profile('hbsmith-dv')

    assert got == {
        'AWS_ACCESS_KEY_ID': 'AKIAPROFILE',
        'AWS_SECRET_ACCESS_KEY': 'profile-secret',
        'AWS_SESSION_TOKEN': 'profile-session-token',
    }
    cmd = mock_run.call_args.args[0]
    assert cmd[1:] == ['configure', 'export-credentials', '--profile', 'hbsmith-dv', '--format', 'process']


def test_fetch_credentials_via_aws_profile_non_zero_exit():
    completed = subprocess.CompletedProcess(
        args=[], returncode=255, stdout='', stderr='Error loading SSO Token: Token has expired'
    )

    with patch('run_server.subprocess.run', return_value=completed):
        with pytest.raises(RuntimeError, match='aws sso login'):
            run_server.fetch_credentials_via_aws_profile('hbsmith-dv')


def test_resolve_aws_credentials_falls_back_to_profile_on_imds_failure(monkeypatch):
    """IMDS 실패 시 _resolve_aws_credentials가 aws CLI 프로파일로 폴백한다(boto3 미사용 경로 공용)."""
    import urllib.error

    def _imds_fails():
        raise urllib.error.URLError('IMDS unreachable')

    monkeypatch.setattr(run_server, 'fetch_ec2_instance_role_credentials', _imds_fails)
    monkeypatch.setattr(
        run_server,
        'fetch_credentials_via_aws_profile',
        lambda profile: {
            'AWS_ACCESS_KEY_ID': 'AKIAFALLBACK',
            'AWS_SECRET_ACCESS_KEY': 'sk-fallback',
            'AWS_SESSION_TOKEN': 'st-fallback',
        },
    )

    creds = run_server._resolve_aws_credentials()

    assert creds['AWS_ACCESS_KEY_ID'] == 'AKIAFALLBACK'
    assert creds['AWS_SESSION_TOKEN'] == 'st-fallback'


def test_aws_creds_env_includes_region_and_tokens(monkeypatch):
    """_aws_creds_env가 자격증명 + 리전을 서브프로세스 env에 싣는다."""
    env = run_server._aws_creds_env(
        {'AWS_ACCESS_KEY_ID': 'ak', 'AWS_SECRET_ACCESS_KEY': 'sk', 'AWS_SESSION_TOKEN': 'st'}
    )
    assert env['AWS_ACCESS_KEY_ID'] == 'ak'
    assert env['AWS_SECRET_ACCESS_KEY'] == 'sk'
    assert env['AWS_SESSION_TOKEN'] == 'st'
    assert env['AWS_DEFAULT_REGION'] == run_server.AWS_DEFAULT_REGION
