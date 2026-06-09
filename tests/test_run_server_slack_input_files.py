"""Slack 트리거 메시지 첨부 → S3 `runs/{thread}/input/` 업로드 및 DM subtype 처리."""

from unittest.mock import MagicMock

import run_server


def _stub_creds_and_put(monkeypatch):
    """자격증명 해석과 S3 put을 무력화하고, 업로드 호출을 기록하는 list를 돌려준다."""
    monkeypatch.setattr(run_server, '_resolve_aws_credentials', lambda: {
        'AWS_ACCESS_KEY_ID': 'a', 'AWS_SECRET_ACCESS_KEY': 's', 'AWS_SESSION_TOKEN': 't',
    })
    puts = []
    monkeypatch.setattr(run_server, '_s3_put_bytes', lambda bucket, key, body, creds: puts.append((key, body)))
    return puts


def test_sanitize_strips_path_segments():
    assert run_server._sanitize_slack_attachment_filename('../../../x.txt') == 'x.txt'
    assert run_server._sanitize_slack_attachment_filename('') == 'attached'


def test_upload_writes_to_s3(monkeypatch):
    puts = _stub_creds_and_put(monkeypatch)
    monkeypatch.setattr(run_server, '_read_slack_private_url', lambda url, token, mx: b'hello')

    event = {
        'files': [
            {'id': 'F1', 'name': 'note.txt', 'size': 5, 'url_private_download': 'https://files.slack.com/fake'}
        ]
    }
    out = run_server._upload_slack_files_to_s3(event, '1700000000.000001')

    assert out == [{'filename': 'note.txt', 's3_key': 'runs/1700000000.000001/input/note.txt'}]
    assert puts == [('runs/1700000000.000001/input/note.txt', b'hello')]


def test_upload_skips_without_url(monkeypatch):
    _stub_creds_and_put(monkeypatch)
    event = {'files': [{'id': 'F1', 'name': 'a.bin'}]}
    assert run_server._upload_slack_files_to_s3(event, 'TS') == []


def test_upload_no_files_returns_empty(monkeypatch):
    _stub_creds_and_put(monkeypatch)
    assert run_server._upload_slack_files_to_s3({}, 'TS') == []


def test_upload_duplicate_names_get_suffix(monkeypatch):
    _stub_creds_and_put(monkeypatch)
    monkeypatch.setattr(run_server, '_read_slack_private_url', lambda url, token, mx: b'x')

    event = {
        'files': [
            {'name': 'same.txt', 'size': 1, 'url_private_download': 'https://files.slack.com/1'},
            {'name': 'same.txt', 'size': 1, 'url_private_download': 'https://files.slack.com/2'},
        ]
    }
    out = run_server._upload_slack_files_to_s3(event, 'TS')
    names = sorted(f['filename'] for f in out)
    assert names == ['same.txt', 'same_2.txt']


def test_on_dm_file_share_calls_submit(monkeypatch):
    called = []

    def fake_submit(event, client, context=None):
        called.append((event, client, context))

    monkeypatch.setattr(run_server, '_submit', fake_submit)
    run_server.on_dm(
        {'channel_type': 'im', 'subtype': 'file_share', 'ts': '1.0', 'files': []},
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
        {'channel_type': 'im', 'subtype': 'message_changed', 'ts': '1.0'},
        MagicMock(),
    )
    assert not called


def test_handle_request_file_only_dispatches_with_input_note(monkeypatch, slack_client):
    """텍스트 없이 첨부만 있는 메시지도 디스패치되며, 프롬프트에 input 안내가 들어간다."""
    dispatch = MagicMock(return_value='job')
    monkeypatch.setattr(run_server, '_enqueue_claude_job', dispatch)
    monkeypatch.setattr(
        run_server,
        '_upload_slack_files_to_s3',
        lambda event, thread_ts: [{'filename': 'x.txt', 's3_key': 'runs/TS/input/x.txt'}],
    )

    event = {
        'channel': 'D123',
        'channel_type': 'im',
        'team_id': 'T_ALLOWED',
        'user': 'U_USER',
        'thread_ts': '1700000000.000001',
        'text': '',
        'files': [{'name': 'x.txt', 'size': 1, 'url_private_download': 'https://example.com/f'}],
    }
    run_server.handle_request(event, slack_client)

    dispatch.assert_called_once()
    prompt = dispatch.call_args.args[1]
    assert '/workspace/input/x.txt' in prompt
    assert slack_client.chat_postMessage.called
