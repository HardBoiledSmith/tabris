import sys
from unittest.mock import MagicMock

import pytest

import run_server


def test_collect_workspace_files_returns_files(tmp_path):
    (tmp_path / 'a.txt').write_bytes(b'hello')
    items = run_server._collect_workspace_files_for_upload(str(tmp_path))
    assert len(items) == 1
    assert items[0][0] == 'a.txt'
    assert items[0][1] == b'hello'


def test_collect_skips_hidden_and_nested(tmp_path):
    (tmp_path / '.hidden').write_bytes(b'x')
    sub = tmp_path / 'sub'
    sub.mkdir()
    (sub / 'b.txt').write_bytes(b'y')
    items = run_server._collect_workspace_files_for_upload(str(tmp_path))
    names = sorted([x[0] for x in items])
    assert names == ['sub/b.txt']


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX-only symlink test')
def test_collect_skips_symlink(tmp_path):
    real = tmp_path / 'real.txt'
    real.write_text('data')
    link = tmp_path / 'link.txt'
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip('symlink not available')
    items = run_server._collect_workspace_files_for_upload(str(tmp_path))
    names = sorted([x[0] for x in items])
    assert names == ['real.txt']


def test_collect_respects_max_files(monkeypatch, tmp_path):
    monkeypatch.setattr(run_server, 'ARTIFACT_MAX_FILES', 2)
    for i in range(5):
        (tmp_path / f'f{i}.txt').write_bytes(b'x')
    items = run_server._collect_workspace_files_for_upload(str(tmp_path))
    assert len(items) == 2


def test_collect_includes_named_markdown_and_json(tmp_path):
    (tmp_path / 'context.md').write_bytes(b'secret')
    (tmp_path / 'CLAUDE.md').write_bytes(b'x')
    (tmp_path / 'mcp.json').write_bytes(b'{}')
    (tmp_path / 'keep.txt').write_bytes(b'ok')
    items = run_server._collect_workspace_files_for_upload(str(tmp_path))
    assert sorted(x[0] for x in items) == ['CLAUDE.md', 'context.md', 'keep.txt', 'mcp.json']


def test_collect_skips_dot_claude_tree(tmp_path):
    dc = tmp_path / '.claude' / 'skills'
    dc.mkdir(parents=True)
    (dc / 'x.md').write_text('y')
    (tmp_path / 'ok.txt').write_text('z')
    items = run_server._collect_workspace_files_for_upload(str(tmp_path))
    assert sorted(x[0] for x in items) == ['ok.txt']


def test_post_workspace_artifacts_to_thread_uploads(tmp_path):
    ws = tmp_path / 'sandbox'
    ws.mkdir()
    (ws / 'out.bin').write_bytes(b'\xff\x00')
    client = MagicMock()
    run_server.post_workspace_artifacts_to_thread(client, 'D123', '1700000000.000001', str(ws))
    client.files_upload_v2.assert_called_once()
    kwargs = client.files_upload_v2.call_args.kwargs
    assert kwargs['channel'] == 'D123'
    assert kwargs['thread_ts'] == '1700000000.000001'
    assert kwargs['content'] == b'\xff\x00'
