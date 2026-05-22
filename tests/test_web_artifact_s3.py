"""upload_web_artifacts_to_s3 및 handle_request web artifact 통합 테스트."""

import subprocess
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

import run_server
from tests.helpers import install_claude_popen_mock

_TEST_CREDS = {
    'AWS_ACCESS_KEY_ID': 'ak-test',
    'AWS_SECRET_ACCESS_KEY': 'sk-test',
    'AWS_SESSION_TOKEN': 'st-test',
}


def test_upload_web_artifacts_disabled(tmp_path, monkeypatch):
    """ARTIFACT_S3_SYNC_ENABLED=False면 sync 미호출, None 반환."""
    monkeypatch.setattr(run_server, 'ARTIFACT_S3_SYNC_ENABLED', False)
    (tmp_path / 'bundle.html').write_text('<html/>')

    with patch.object(run_server, '_aws_s3_sync') as mock_sync:
        result = run_server.upload_web_artifacts_to_s3('UTEST', '1700000000', str(tmp_path), _TEST_CREDS)

    mock_sync.assert_not_called()
    assert result is None


def test_upload_web_artifacts_empty_dir(tmp_path, monkeypatch):
    """빈 artifact 디렉터리면 sync 미호출, None 반환."""
    monkeypatch.setattr(run_server, 'ARTIFACT_S3_SYNC_ENABLED', True)

    with patch.object(run_server, '_aws_s3_sync') as mock_sync:
        result = run_server.upload_web_artifacts_to_s3('UTEST', '1700000000', str(tmp_path), _TEST_CREDS)

    mock_sync.assert_not_called()
    assert result is None


def test_upload_web_artifacts_invalid_user_id(tmp_path, monkeypatch):
    """user_id 형식 불일치 시 업로드 스킵, None 반환."""
    monkeypatch.setattr(run_server, 'ARTIFACT_S3_SYNC_ENABLED', True)
    (tmp_path / 'bundle.html').write_text('<html/>')

    with patch.object(run_server, '_aws_s3_sync') as mock_sync:
        result = run_server.upload_web_artifacts_to_s3('invalid-id', '1700000000', str(tmp_path), _TEST_CREDS)

    mock_sync.assert_not_called()
    assert result is None


def test_upload_web_artifacts_none_user_id(tmp_path, monkeypatch):
    """user_id가 None이면 업로드 스킵."""
    monkeypatch.setattr(run_server, 'ARTIFACT_S3_SYNC_ENABLED', True)
    (tmp_path / 'bundle.html').write_text('<html/>')

    with patch.object(run_server, '_aws_s3_sync') as mock_sync:
        result = run_server.upload_web_artifacts_to_s3(None, '1700000000', str(tmp_path), _TEST_CREDS)

    mock_sync.assert_not_called()
    assert result is None


def test_upload_web_artifacts_s3_uri(tmp_path, monkeypatch):
    """올바른 S3 URI로 _aws_s3_sync를 호출한다."""
    monkeypatch.setattr(run_server, 'ARTIFACT_S3_SYNC_ENABLED', True)
    monkeypatch.setattr(run_server, 'ARTIFACT_S3_BUCKET', 'hbsmith-tabris-artifacts')
    (tmp_path / 'bundle.html').write_text('<html/>')

    sync_calls = []

    def fake_sync(src, dst, creds, *, delete=False, timeout=None):
        sync_calls.append({'src': src, 'dst': dst, 'delete': delete})

    monkeypatch.setattr(run_server, '_aws_s3_sync', fake_sync)
    run_server.upload_web_artifacts_to_s3('UTEST', '1700000000', str(tmp_path), _TEST_CREDS)

    assert len(sync_calls) == 1
    assert sync_calls[0]['dst'] == 's3://hbsmith-tabris-artifacts/UTEST/1700000000/'
    assert sync_calls[0]['src'] == str(tmp_path)
    assert sync_calls[0]['delete'] is False


def test_upload_web_artifacts_returns_bundle_url(tmp_path, monkeypatch):
    """bundle.html 있을 때 올바른 공개 URL을 반환한다."""
    monkeypatch.setattr(run_server, 'ARTIFACT_S3_SYNC_ENABLED', True)
    monkeypatch.setattr(run_server, 'ARTIFACT_S3_BUCKET', 'hbsmith-tabris-artifacts')
    monkeypatch.setattr(run_server, 'ARTIFACT_BASE_URL', 'https://tabris-artifacts.hbsmith.io')
    (tmp_path / 'bundle.html').write_text('<html/>')

    monkeypatch.setattr(run_server, '_aws_s3_sync', lambda *a, **kw: None)
    result = run_server.upload_web_artifacts_to_s3('UTEST', '1700000000', str(tmp_path), _TEST_CREDS)

    assert result == 'https://tabris-artifacts.hbsmith.io/UTEST/1700000000/bundle.html'


def test_upload_web_artifacts_no_bundle_no_url(tmp_path, monkeypatch):
    """bundle.html이 없으면 sync는 하되 URL은 None."""
    monkeypatch.setattr(run_server, 'ARTIFACT_S3_SYNC_ENABLED', True)
    (tmp_path / 'other.html').write_text('<html/>')

    sync_calls = []
    monkeypatch.setattr(run_server, '_aws_s3_sync', lambda *a, **kw: sync_calls.append(1))
    result = run_server.upload_web_artifacts_to_s3('UTEST', '1700000000', str(tmp_path), _TEST_CREDS)

    assert len(sync_calls) == 1
    assert result is None


def test_upload_web_artifacts_raises_on_sync_failure(tmp_path, monkeypatch):
    """_aws_s3_sync 실패 시 CalledProcessError를 전파한다."""
    monkeypatch.setattr(run_server, 'ARTIFACT_S3_SYNC_ENABLED', True)
    (tmp_path / 'bundle.html').write_text('<html/>')

    def bad_sync(*a, **kw):
        raise subprocess.CalledProcessError(1, ['aws'])

    monkeypatch.setattr(run_server, '_aws_s3_sync', bad_sync)
    with pytest.raises(subprocess.CalledProcessError):
        run_server.upload_web_artifacts_to_s3('UTEST', '1700000000', str(tmp_path), _TEST_CREDS)


def test_handle_request_posts_artifact_url(monkeypatch, slack_client, tmp_path):
    """handle_request가 artifact/bundle.html 존재 시 URL을 Slack에 게시한다."""
    monkeypatch.setattr(run_server, 'ARTIFACT_S3_SYNC_ENABLED', True)
    monkeypatch.setattr(run_server, 'ARTIFACT_BASE_URL', 'https://tabris-artifacts.hbsmith.io')
    monkeypatch.setattr(run_server, 'ARTIFACT_S3_BUCKET', 'hbsmith-tabris-artifacts')
    monkeypatch.setattr(run_server, 'SANDBOX_RUNS_DIR', str(tmp_path))

    install_claude_popen_mock(monkeypatch, stdout_text='결과입니다', returncode=0)

    monkeypatch.setattr(run_server, 'sync_memory_from_s3', MagicMock())
    monkeypatch.setattr(run_server, 'sync_memory_to_s3', MagicMock())
    monkeypatch.setattr(run_server, 'post_workspace_artifacts_to_thread', MagicMock())
    monkeypatch.setattr(run_server.shutil, 'rmtree', MagicMock())

    def fake_fetch_creds():
        return _TEST_CREDS

    monkeypatch.setattr(run_server, 'fetch_ec2_instance_role_credentials', fake_fetch_creds)

    # bundle.html을 artifact/ 에 심어놓는다 (bundle-artifact.sh 결과 시뮬레이션)
    def fake_upload(slack_user_id, run_id, artifact_dir, creds):
        import os
        bundle = os.path.join(artifact_dir, 'bundle.html')
        if os.path.isfile(bundle):
            return f'https://tabris-artifacts.hbsmith.io/{slack_user_id}/{run_id}/bundle.html'
        return None

    monkeypatch.setattr(run_server, 'upload_web_artifacts_to_s3', fake_upload)

    # handle_request가 artifact_dir을 만든 뒤 bundle.html을 넣어두는 훅
    orig_makedirs = run_server.os.makedirs
    created_dirs = []

    def capturing_makedirs(path, exist_ok=False):
        orig_makedirs(path, exist_ok=exist_ok)
        created_dirs.append(path)
        if path.endswith('/artifact') or path.endswith(run_server.WORKSPACE_WEB_ARTIFACT_SUBDIR):
            import os as _os
            (_os.path.dirname(path) and True)  # ensure parent exists
            with open(_os.path.join(path, 'bundle.html'), 'w') as f:
                f.write('<html/>')

    monkeypatch.setattr(run_server.os, 'makedirs', capturing_makedirs)

    event = {
        'channel': 'C123',
        'channel_type': 'channel',
        'team_id': 'T_ALLOWED',
        'user': 'UTEST123',
        'ts': '1700000000.000001',
        'thread_ts': '1700000000.000001',
        'text': '<@UBOT> 차트 만들어줘',
    }
    run_server.handle_request(event, slack_client)

    posted_texts = [
        call.kwargs.get('text', '') or ''
        for call in slack_client.chat_postMessage.call_args_list
    ]
    assert any('tabris-artifacts.hbsmith.io' in t for t in posted_texts), (
        f'artifact URL이 Slack에 게시되지 않았음. 실제 메시지: {posted_texts}'
    )


def test_handle_request_no_artifact_no_url_message(monkeypatch, slack_client, tmp_path):
    """artifact/가 비어 있으면 URL 메시지를 게시하지 않는다."""
    monkeypatch.setattr(run_server, 'ARTIFACT_S3_SYNC_ENABLED', True)
    monkeypatch.setattr(run_server, 'SANDBOX_RUNS_DIR', str(tmp_path))

    install_claude_popen_mock(monkeypatch, stdout_text='결과입니다', returncode=0)

    monkeypatch.setattr(run_server, 'sync_memory_from_s3', MagicMock())
    monkeypatch.setattr(run_server, 'sync_memory_to_s3', MagicMock())
    monkeypatch.setattr(run_server, 'post_workspace_artifacts_to_thread', MagicMock())
    monkeypatch.setattr(run_server.shutil, 'rmtree', MagicMock())
    monkeypatch.setattr(run_server, 'fetch_ec2_instance_role_credentials', lambda: _TEST_CREDS)
    monkeypatch.setattr(run_server, 'upload_web_artifacts_to_s3', lambda *a, **kw: None)

    event = {
        'channel': 'C123',
        'channel_type': 'channel',
        'team_id': 'T_ALLOWED',
        'user': 'UTEST123',
        'ts': '1700000000.000002',
        'thread_ts': '1700000000.000002',
        'text': '<@UBOT> 안녕',
    }
    run_server.handle_request(event, slack_client)

    posted_texts = [
        call.kwargs.get('text', '') or ''
        for call in slack_client.chat_postMessage.call_args_list
    ]
    assert not any('tabris-artifacts.hbsmith.io' in t for t in posted_texts)
