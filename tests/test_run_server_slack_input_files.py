"""트리거 메시지 첨부 메타 수집(다운로드/S3 없음) 및 DM subtype 처리."""

from unittest.mock import MagicMock

import run_server


def test_sanitize_strips_path_segments():
    assert run_server._sanitize_slack_attachment_filename('../../../x.txt') == 'x.txt'
    assert run_server._sanitize_slack_attachment_filename('') == 'attached'


def test_collect_returns_metadata():
    event = {
        'files': [{'id': 'F1', 'name': 'note.txt', 'size': 5, 'url_private_download': 'https://files.slack.com/fake'}]
    }
    out = run_server._collect_current_message_files(event)
    assert out == [{'filename': 'note.txt', 'url': 'https://files.slack.com/fake', 'size': 5}]


def test_collect_skips_without_url():
    event = {'files': [{'id': 'F1', 'name': 'a.bin'}]}
    assert run_server._collect_current_message_files(event) == []


def test_collect_no_files_returns_empty():
    assert run_server._collect_current_message_files({}) == []


def test_collect_duplicate_names_get_suffix():
    event = {
        'files': [
            {'name': 'same.txt', 'size': 1, 'url_private_download': 'https://files.slack.com/1'},
            {'name': 'same.txt', 'size': 1, 'url_private_download': 'https://files.slack.com/2'},
        ]
    }
    out = run_server._collect_current_message_files(event)
    names = sorted(f['filename'] for f in out)
    assert names == ['same.txt', 'same_2.txt']


def test_collect_skips_oversized_file():
    """per-file 한도를 넘는 첨부는 size 메타로 사전 컷한다."""
    event = {
        'files': [
            {
                'name': 'big.bin',
                'size': run_server.ARTIFACT_MAX_BYTES_PER_FILE + 1,
                'url_private_download': 'https://files.slack.com/big',
            },
            {'name': 'ok.txt', 'size': 3, 'url_private_download': 'https://files.slack.com/ok'},
        ]
    }
    out = run_server._collect_current_message_files(event)
    assert [f['filename'] for f in out] == ['ok.txt']


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

    event = {
        'channel': 'D123',
        'channel_type': 'im',
        'team_id': 'T_ALLOWED',
        'user': 'U_USER',
        'thread_ts': '1700000000.000001',
        'ts': '1700000000.000001',
        'text': '',
        'files': [{'id': 'F1', 'name': 'x.txt', 'size': 1, 'url_private_download': 'https://example.com/f'}],
    }
    run_server.handle_request(event, slack_client)

    dispatch.assert_called_once()
    prompt = dispatch.call_args.args[1]
    assert '/workspace/input/x.txt' in prompt
    assert slack_client.chat_postMessage.called
