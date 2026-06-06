"""샌드박스 워커의 memory S3 sync 헬퍼 단위 테스트.

봇이 아니라 sandbox_worker가 memory를 동기화한다(ECS task role 자동 인증, boto3 미사용).
"""

from unittest.mock import patch

import sandbox_worker


def _ok_run():
    """subprocess.run 대역. 호출 인자를 기록하기 위한 list와 fake를 돌려준다."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return None

    return calls, fake_run


def test_sync_from_noop_when_bucket_unset(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox_worker, 'MEMORY_S3_BUCKET', '')
    monkeypatch.setattr(sandbox_worker, 'MEMORY_DIR', str(tmp_path))
    with patch.object(sandbox_worker.subprocess, 'run') as mock_run:
        sandbox_worker.sync_memory_from_s3('UTEST')
    mock_run.assert_not_called()


def test_sync_from_noop_when_user_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox_worker, 'MEMORY_S3_BUCKET', 'mem-bucket')
    monkeypatch.setattr(sandbox_worker, 'MEMORY_DIR', str(tmp_path))
    with patch.object(sandbox_worker.subprocess, 'run') as mock_run:
        sandbox_worker.sync_memory_from_s3('')
    mock_run.assert_not_called()


def test_sync_from_builds_command(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox_worker, 'MEMORY_S3_BUCKET', 'mem-bucket')
    monkeypatch.setattr(sandbox_worker, 'MEMORY_DIR', str(tmp_path))
    calls, fake = _ok_run()
    monkeypatch.setattr(sandbox_worker.subprocess, 'run', fake)

    sandbox_worker.sync_memory_from_s3('UTEST')

    assert len(calls) == 1
    cmd = calls[0]
    assert 's3' in cmd and 'sync' in cmd
    assert 's3://mem-bucket/users/UTEST/' in cmd
    assert str(tmp_path) in cmd
    assert '--delete' not in cmd


def test_sync_to_skips_empty_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox_worker, 'MEMORY_S3_BUCKET', 'mem-bucket')
    monkeypatch.setattr(sandbox_worker, 'MEMORY_DIR', str(tmp_path))  # 빈 디렉터리
    with patch.object(sandbox_worker.subprocess, 'run') as mock_run:
        sandbox_worker.sync_memory_to_s3('UTEST')
    mock_run.assert_not_called()


def test_sync_to_builds_command_with_delete(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox_worker, 'MEMORY_S3_BUCKET', 'mem-bucket')
    monkeypatch.setattr(sandbox_worker, 'MEMORY_DIR', str(tmp_path))
    (tmp_path / 'note.md').write_text('x', encoding='utf-8')
    calls, fake = _ok_run()
    monkeypatch.setattr(sandbox_worker.subprocess, 'run', fake)

    sandbox_worker.sync_memory_to_s3('UTEST')

    assert len(calls) == 1
    cmd = calls[0]
    assert 's3' in cmd and 'sync' in cmd and '--delete' in cmd
    # 업로드는 (로컬 → s3) 순서
    assert cmd.index(str(tmp_path)) < cmd.index('s3://mem-bucket/users/UTEST/')
