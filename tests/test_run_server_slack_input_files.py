"""Slack 트리거 메시지 첨부 → `input/` 저장 및 DM subtype 처리."""

from unittest.mock import MagicMock

import run_server


class _FakeHTTPResponse:
    """`urllib.request.urlopen` 컨텍스트 매니저용 스텁."""

    def __init__(self, body: bytes, headers: dict | None = None):
        self._body = body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, max_plus_one: int):
        return self._body[:max_plus_one]


def test_sanitize_strips_path_segments():
    assert run_server._sanitize_slack_attachment_filename('../../../x.txt') == 'x.txt'
    assert run_server._sanitize_slack_attachment_filename('') == 'attached'


def test_download_writes_input(tmp_path, monkeypatch):
    ws = tmp_path / 'ws'
    ws.mkdir()
    event = {
        'files': [
            {
                'id': 'F1',
                'name': 'note.txt',
                'size': 5,
                'url_private_download': 'https://files.slack.com/fake',
            }
        ]
    }

    def fake_urlopen(req, timeout=90):
        assert 'Authorization' in req.headers
        return _FakeHTTPResponse(b'hello')

    monkeypatch.setattr(run_server.urllib.request, 'urlopen', fake_urlopen)
    paths = run_server.download_slack_message_files_to_input(event, str(ws), 'xoxb-test')
    assert paths == ['input/note.txt']
    assert (ws / 'input' / 'note.txt').read_bytes() == b'hello'


def test_download_skips_without_url(tmp_path):
    ws = tmp_path / 'ws'
    ws.mkdir()
    event = {'files': [{'id': 'F1', 'name': 'a.bin'}]}
    assert run_server.download_slack_message_files_to_input(event, str(ws), 'xoxb-test') == []


def test_download_skips_when_declared_size_over_limit(tmp_path, monkeypatch):
    ws = tmp_path / 'ws'
    ws.mkdir()
    event = {
        'files': [
            {
                'name': 'huge.bin',
                'size': run_server.ARTIFACT_MAX_BYTES_PER_FILE + 1,
                'url_private_download': 'https://files.slack.com/fake',
            }
        ]
    }
    called = []

    def fake_urlopen(req, timeout=90):
        called.append(True)
        return _FakeHTTPResponse(b'x')

    monkeypatch.setattr(run_server.urllib.request, 'urlopen', fake_urlopen)
    assert run_server.download_slack_message_files_to_input(event, str(ws), 'xoxb-test') == []
    assert not called


def test_download_duplicate_names_get_suffix(tmp_path, monkeypatch):
    ws = tmp_path / 'ws'
    ws.mkdir()
    event = {
        'files': [
            {
                'name': 'same.txt',
                'size': 1,
                'url_private_download': 'https://files.slack.com/1',
            },
            {
                'name': 'same.txt',
                'size': 1,
                'url_private_download': 'https://files.slack.com/2',
            },
        ]
    }
    bodies = [b'a', b'b']

    def fake_urlopen(req, timeout=90):
        return _FakeHTTPResponse(bodies.pop(0))

    monkeypatch.setattr(run_server.urllib.request, 'urlopen', fake_urlopen)
    paths = run_server.download_slack_message_files_to_input(event, str(ws), 'xoxb-test')
    assert sorted(paths) == ['input/same.txt', 'input/same_2.txt']


def test_on_dm_file_share_calls_submit(monkeypatch):
    called = []

    def fake_submit(event, client, context=None):
        called.append((event, client, context))

    monkeypatch.setattr(run_server, '_submit', fake_submit)
    run_server.on_dm(
        {
            'channel_type': 'im',
            'subtype': 'file_share',
            'ts': '1.0',
            'files': [],
        },
        MagicMock(),
    )
    assert len(called) == 1


def test_enrich_event_team_id_from_context():
    event = {'subtype': 'file_share', 'channel': 'D1', 'user': 'U1'}
    ctx = MagicMock()
    ctx.team_id = 'T_ALLOWED'
    ctx.actor_team_id = None
    out = run_server._enrich_event_team_id_for_acl(event, ctx)
    assert out['team_id'] == 'T_ALLOWED'
    assert 'team_id' not in event


def test_enrich_event_noop_when_team_present():
    event = {'team_id': 'T_ALLOWED', 'channel': 'D1'}
    ctx = MagicMock()
    ctx.team_id = 'T_OTHER'
    out = run_server._enrich_event_team_id_for_acl(event, ctx)
    assert out is event


def test_on_dm_unknown_subtype_skips(monkeypatch):
    called = []

    monkeypatch.setattr(run_server, '_submit', lambda e, c, context=None: called.append(True))
    run_server.on_dm(
        {
            'channel_type': 'im',
            'subtype': 'message_changed',
            'ts': '1.0',
        },
        MagicMock(),
    )
    assert not called


def test_handle_request_file_only_invokes_run_claude(monkeypatch, slack_client):
    seen = []

    def fake_run_claude(event, context, user_request, progress_callback=None):
        seen.append(user_request)
        return 'done'

    monkeypatch.setattr(run_server, 'run_claude', fake_run_claude)
    monkeypatch.setattr(run_server, 'post_claude_markdown_to_thread', MagicMock())
    monkeypatch.setattr(run_server, 'post_workspace_artifacts_to_thread', MagicMock())
    monkeypatch.setattr(run_server.shutil, 'rmtree', MagicMock())

    event = {
        'channel': 'D123',
        'channel_type': 'im',
        'team_id': 'T_ALLOWED',
        'user': 'U_USER',
        'thread_ts': '1700000000.000001',
        'text': '',
        'files': [
            {
                'name': 'x.txt',
                'size': 1,
                'url_private_download': 'https://example.com/f',
            }
        ],
    }
    run_server.handle_request(event, slack_client)
    assert seen and 'input' in seen[0].lower()
    assert slack_client.chat_postMessage.called
