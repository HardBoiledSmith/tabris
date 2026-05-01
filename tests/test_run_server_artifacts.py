import sys
from unittest.mock import MagicMock

import pytest

import run_server


def test_collect_workspace_artifacts_returns_files(tmp_path):
    root = tmp_path / 'artifacts'
    root.mkdir()
    (root / 'a.txt').write_bytes(b'hello')
    items = run_server._collect_workspace_artifacts(str(root))
    assert len(items) == 1
    assert items[0][0] == 'a.txt'
    assert items[0][1] == b'hello'


def test_collect_skips_hidden_and_nested(tmp_path):
    root = tmp_path / 'artifacts'
    root.mkdir()
    (root / '.hidden').write_bytes(b'x')
    sub = root / 'sub'
    sub.mkdir()
    (sub / 'b.txt').write_bytes(b'y')
    items = run_server._collect_workspace_artifacts(str(root))
    names = sorted([x[0] for x in items])
    assert names == ['sub/b.txt']


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX-only symlink test')
def test_collect_skips_symlink(tmp_path):
    root = tmp_path / 'artifacts'
    root.mkdir()
    real = root / 'real.txt'
    real.write_text('data')
    link = root / 'link.txt'
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip('symlink not available')
    items = run_server._collect_workspace_artifacts(str(root))
    names = sorted([x[0] for x in items])
    assert names == ['real.txt']


def test_collect_respects_max_files(monkeypatch, tmp_path):
    monkeypatch.setattr(run_server, 'ARTIFACT_MAX_FILES', 2)
    root = tmp_path / 'artifacts'
    root.mkdir()
    for i in range(5):
        (root / f'f{i}.txt').write_bytes(b'x')
    items = run_server._collect_workspace_artifacts(str(root))
    assert len(items) == 2


def test_post_workspace_artifacts_to_thread_uploads(tmp_path):
    ws = tmp_path / 'sandbox'
    art = ws / 'artifacts'
    art.mkdir(parents=True)
    (art / 'out.bin').write_bytes(b'\xff\x00')
    client = MagicMock()
    run_server.post_workspace_artifacts_to_thread(client, 'D123', '1700000000.000001', str(ws))
    client.files_upload_v2.assert_called_once()
    kwargs = client.files_upload_v2.call_args.kwargs
    assert kwargs['channel'] == 'D123'
    assert kwargs['thread_ts'] == '1700000000.000001'
    assert kwargs['content'] == b'\xff\x00'
