import sys
from unittest.mock import MagicMock

import pytest

import tabris_slack_utils


def _ws_with_output(tmp_path):
    """호스트 워크스페이스와 동일하게 `output/` 하위만 수집 대상으로 쓴다."""
    out = tmp_path / 'output'
    out.mkdir(parents=True)
    return tmp_path, out


def test_collect_workspace_files_returns_files(tmp_path):
    ws, out = _ws_with_output(tmp_path)
    (out / 'a.txt').write_bytes(b'hello')
    items = tabris_slack_utils._collect_workspace_files_for_upload(str(ws))
    assert len(items) == 1
    assert items[0][0] == 'a.txt'
    assert items[0][1] == b'hello'


def test_collect_skips_hidden_and_nested(tmp_path):
    ws, out = _ws_with_output(tmp_path)
    (out / '.hidden').write_bytes(b'x')
    sub = out / 'sub'
    sub.mkdir()
    (sub / 'b.txt').write_bytes(b'y')
    items = tabris_slack_utils._collect_workspace_files_for_upload(str(ws))
    names = sorted([x[0] for x in items])
    assert names == ['sub/b.txt']


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX-only symlink test')
def test_collect_skips_symlink(tmp_path):
    ws, out = _ws_with_output(tmp_path)
    real = out / 'real.txt'
    real.write_text('data')
    link = out / 'link.txt'
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip('symlink not available')
    items = tabris_slack_utils._collect_workspace_files_for_upload(str(ws))
    names = sorted([x[0] for x in items])
    assert names == ['real.txt']


def test_collect_respects_max_files(monkeypatch, tmp_path):
    monkeypatch.setattr(tabris_slack_utils, 'ARTIFACT_MAX_FILES', 2)
    ws, out = _ws_with_output(tmp_path)
    for i in range(5):
        (out / f'f{i}.txt').write_bytes(b'x')
    items = tabris_slack_utils._collect_workspace_files_for_upload(str(ws))
    assert len(items) == 2


def test_collect_empty_when_output_missing(tmp_path):
    items = tabris_slack_utils._collect_workspace_files_for_upload(str(tmp_path))
    assert items == []


def test_post_workspace_artifacts_to_thread_uploads(tmp_path):
    ws, out = _ws_with_output(tmp_path)
    (out / 'out.bin').write_bytes(b'\xff\x00')
    client = MagicMock()
    tabris_slack_utils.post_workspace_artifacts_to_thread(client, 'D123', '1700000000.000001', str(ws))
    client.files_upload_v2.assert_called_once()
    kwargs = client.files_upload_v2.call_args.kwargs
    assert kwargs['channel'] == 'D123'
    assert kwargs['thread_ts'] == '1700000000.000001'
    assert kwargs['content'] == b'\xff\x00'
