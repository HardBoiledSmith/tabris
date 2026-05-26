"""사용자별 memory S3 sync 헬퍼 단위 테스트."""

import subprocess
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

import run_server

_TEST_CREDS = {
    'AWS_ACCESS_KEY_ID': 'ak-test',
    'AWS_SECRET_ACCESS_KEY': 'sk-test',
    'AWS_SESSION_TOKEN': 'st-test',
}


def test_aws_s3_sync_builds_correct_command(tmp_path):
    """_aws_s3_sync가 올바른 명령과 creds env로 subprocess.run을 호출하는지 검증한다."""
    src = str(tmp_path / 'src')
    dst = 's3://hbsmith-tabris-memory/users/UTEST/'
    captured_calls: list[dict] = []

    def fake_run(cmd, env, **kwargs):
        captured_calls.append({'cmd': cmd, 'env': env})
        result = MagicMock()
        result.returncode = 0
        result.stderr = ''
        return result

    with patch.object(run_server.subprocess, 'run', side_effect=fake_run):
        run_server._aws_s3_sync(src, dst, _TEST_CREDS)

    assert len(captured_calls) == 1
    call = captured_calls[0]
    assert 's3' in call['cmd']
    assert 'sync' in call['cmd']
    assert src in call['cmd']
    assert dst in call['cmd']
    assert '--delete' not in call['cmd']
    assert call['env']['AWS_ACCESS_KEY_ID'] == 'ak-test'
    assert call['env']['AWS_SECRET_ACCESS_KEY'] == 'sk-test'
    assert call['env']['AWS_SESSION_TOKEN'] == 'st-test'
    assert call['env']['AWS_DEFAULT_REGION'] == run_server.AWS_DEFAULT_REGION


def test_aws_s3_sync_includes_delete_when_requested(tmp_path):
    """delete=True이면 aws s3 sync 명령에 --delete가 포함된다."""
    src = str(tmp_path / 'src')
    dst = 's3://hbsmith-tabris-memory/users/UTEST/'
    captured_cmd: list[list[str]] = []

    def fake_run(cmd, env, **kwargs):
        captured_cmd.append(cmd)
        result = MagicMock()
        result.returncode = 0
        result.stderr = ''
        return result

    with patch.object(run_server.subprocess, 'run', side_effect=fake_run):
        run_server._aws_s3_sync(src, dst, _TEST_CREDS, delete=True)

    assert '--delete' in captured_cmd[0]


def test_aws_s3_sync_raises_on_nonzero_returncode(tmp_path):
    """aws s3 sync가 0이 아닌 종료코드를 반환하면 CalledProcessError를 발생시킨다."""
    result = MagicMock()
    result.returncode = 1
    result.stderr = 'NoSuchBucket'
    result.stdout = ''

    with patch.object(run_server.subprocess, 'run', return_value=result):
        with pytest.raises(subprocess.CalledProcessError):
            run_server._aws_s3_sync('src/', 's3://bucket/prefix/', _TEST_CREDS)


def test_memory_dir_has_files(tmp_path):
    """_memory_dir_has_files는 하위 파일 존재 여부만 판별한다."""
    empty = tmp_path / 'empty'
    empty.mkdir()
    assert run_server._memory_dir_has_files(str(empty)) is False

    with_files = tmp_path / 'with'
    with_files.mkdir()
    (with_files / 'note.md').write_text('x', encoding='utf-8')
    assert run_server._memory_dir_has_files(str(with_files)) is True


def test_sync_memory_from_s3_noop_when_disabled(tmp_path, monkeypatch):
    """MEMORY_S3_SYNC_ENABLED=False이면 subprocess.run이 호출되지 않는다."""
    monkeypatch.setattr(run_server, 'MEMORY_S3_SYNC_ENABLED', False)

    with patch.object(run_server.subprocess, 'run') as mock_run:
        run_server.sync_memory_from_s3('UTEST', str(tmp_path), _TEST_CREDS)

    mock_run.assert_not_called()


def test_sync_memory_to_s3_noop_when_disabled(tmp_path, monkeypatch):
    """MEMORY_S3_SYNC_ENABLED=False이면 subprocess.run이 호출되지 않는다."""
    monkeypatch.setattr(run_server, 'MEMORY_S3_SYNC_ENABLED', False)

    with patch.object(run_server.subprocess, 'run') as mock_run:
        run_server.sync_memory_to_s3('UTEST', str(tmp_path), _TEST_CREDS)

    mock_run.assert_not_called()


def test_sync_memory_to_s3_skips_empty_memory_dir(tmp_path, monkeypatch):
    """로컬 memory가 비어 있으면 S3 업로드(sync)를 하지 않는다."""
    monkeypatch.setattr(run_server, 'MEMORY_S3_SYNC_ENABLED', True)
    memory_dir = str(tmp_path / 'memory')
    memory_dir_path = tmp_path / 'memory'
    memory_dir_path.mkdir()

    with patch.object(run_server, '_aws_s3_sync') as mock_sync:
        run_server.sync_memory_to_s3('UTEST', memory_dir, _TEST_CREDS)

    mock_sync.assert_not_called()


def test_sync_memory_from_s3_uses_correct_s3_uri(tmp_path, monkeypatch):
    """sync_memory_from_s3가 올바른 S3 URI와 로컬 경로로 sync를 호출한다."""
    monkeypatch.setattr(run_server, 'MEMORY_S3_SYNC_ENABLED', True)
    memory_dir = str(tmp_path)
    sync_calls: list[tuple[str, str, bool]] = []

    def fake_sync(src, dst, creds, *, delete=False):
        sync_calls.append((src, dst, delete))

    monkeypatch.setattr(run_server, '_aws_s3_sync', fake_sync)
    run_server.sync_memory_from_s3('UTEST', memory_dir, _TEST_CREDS)

    assert len(sync_calls) == 1
    src, dst, delete = sync_calls[0]
    assert src == f's3://{run_server.MEMORY_S3_BUCKET}/users/UTEST/'
    assert dst == memory_dir
    assert delete is False


def test_sync_memory_to_s3_uses_correct_s3_uri_and_delete(tmp_path, monkeypatch):
    """sync_memory_to_s3가 로컬→S3 sync를 delete=True로 호출한다."""
    monkeypatch.setattr(run_server, 'MEMORY_S3_SYNC_ENABLED', True)
    memory_dir = str(tmp_path)
    (tmp_path / 'memory.md').write_text('data', encoding='utf-8')
    sync_calls: list[tuple[str, str, bool]] = []

    def fake_sync(src, dst, creds, *, delete=False):
        sync_calls.append((src, dst, delete))

    monkeypatch.setattr(run_server, '_aws_s3_sync', fake_sync)
    run_server.sync_memory_to_s3('UTEST', memory_dir, _TEST_CREDS)

    assert len(sync_calls) == 1
    src, dst, delete = sync_calls[0]
    assert src == memory_dir
    assert dst == f's3://{run_server.MEMORY_S3_BUCKET}/users/UTEST/'
    assert delete is True


def test_run_claude_returns_error_when_user_missing(monkeypatch):
    """event에 user가 없으면 docker를 실행하지 않고 오류 문자열을 반환한다."""
    popen_mock = MagicMock()
    monkeypatch.setattr(run_server.subprocess, 'Popen', popen_mock)

    event = {'channel_type': 'im', 'ts': '1.1', 'thread_ts': '1.1'}
    success, result = run_server.run_claude(event, '', 'hello')

    assert success is False
    assert '⚠️' in result
    popen_mock.assert_not_called()


def test_run_claude_s3_download_failure_blocks_docker(monkeypatch, tmp_path):
    """S3 download가 실패하면 docker를 실행하지 않고 오류 문자열을 반환한다."""
    monkeypatch.setattr(run_server, 'MEMORY_S3_SYNC_ENABLED', True)
    monkeypatch.setattr(
        run_server,
        'sync_memory_from_s3',
        MagicMock(side_effect=subprocess.CalledProcessError(1, ['aws'])),
    )
    popen_mock = MagicMock()
    monkeypatch.setattr(run_server.subprocess, 'Popen', popen_mock)

    event = {'channel_type': 'im', 'ts': '1.1', 'thread_ts': '1.1', 'user': 'UTEST'}
    success, result = run_server.run_claude(event, '', 'hello')

    assert success is False
    assert '⚠️' in result
    popen_mock.assert_not_called()
