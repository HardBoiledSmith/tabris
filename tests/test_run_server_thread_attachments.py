"""스레드 페이지네이션 + 과거 첨부 목록 + build_context 첨부 표기."""

from unittest.mock import MagicMock

import run_server
from tabris_slack_utils import THREAD_ATTACHMENTS_LIST_MAX


def _file(file_id, name, **extra):
    base = {'id': file_id, 'name': name, 'url_private_download': f'https://files.slack.com/{file_id}'}
    base.update(extra)
    return base


def test_fetch_thread_messages_paginates():
    """next_cursor가 있으면 모든 페이지를 모은다."""
    client = MagicMock()
    client.conversations_replies.side_effect = [
        {'messages': [{'ts': '1'}], 'response_metadata': {'next_cursor': 'C2'}},
        {'messages': [{'ts': '2'}], 'response_metadata': {'next_cursor': ''}},
    ]
    msgs = run_server._fetch_thread_messages(client, 'D1', 'TS')
    assert [m['ts'] for m in msgs] == ['1', '2']
    assert client.conversations_replies.call_count == 2


def test_fetch_thread_messages_failure_returns_partial():
    client = MagicMock()
    client.conversations_replies.side_effect = Exception('boom')
    assert run_server._fetch_thread_messages(client, 'D1', 'TS') == []


def test_collect_thread_attachments_dedup_and_exclude_current():
    messages = [
        {'ts': '1', 'user': 'U_USER', 'files': [_file('F1', 'a.pdf', size=10, mimetype='application/pdf')]},
        {'ts': '2', 'bot_id': 'B1', 'files': [_file('F2', 'out.csv')]},
        {'ts': '3', 'user': 'U_USER', 'files': [_file('F1', 'a.pdf')]},  # 중복 id
        {'ts': '4', 'user': 'U_USER', 'files': [_file('FCUR', 'current.txt')]},  # 현재 메시지 첨부
    ]
    attachments, truncated = run_server._collect_thread_attachments(messages, exclude_file_ids={'FCUR'})

    names = [a['name'] for a in attachments]
    assert names == ['a.pdf', 'out.csv']  # F1 한 번만, FCUR 제외
    assert truncated is False
    by_name = {a['name']: a for a in attachments}
    assert by_name['a.pdf']['source'] == 'User'
    assert by_name['out.csv']['source'] == 'Assistant'  # bot_id 메시지


def test_collect_thread_attachments_bot_user_id_is_assistant():
    """files_upload_v2 아티팩트처럼 bot_id 없이 user==BOT_USER_ID인 경우도 Assistant."""
    messages = [{'ts': '1', 'user': run_server.BOT_USER_ID, 'files': [_file('F9', 'art.png')]}]
    attachments, _ = run_server._collect_thread_attachments(messages, exclude_file_ids=set())
    assert attachments[0]['source'] == 'Assistant'


def test_collect_thread_attachments_skips_no_url():
    messages = [{'ts': '1', 'user': 'U_USER', 'files': [{'id': 'F1', 'name': 'gone.txt'}]}]
    attachments, _ = run_server._collect_thread_attachments(messages, exclude_file_ids=set())
    assert attachments == []


def test_collect_thread_attachments_truncates():
    messages = [
        {'ts': str(i), 'user': 'U_USER', 'files': [_file(f'F{i}', f'f{i}.txt')]}
        for i in range(THREAD_ATTACHMENTS_LIST_MAX + 5)
    ]
    attachments, truncated = run_server._collect_thread_attachments(messages, exclude_file_ids=set())
    assert truncated is True
    assert len(attachments) == THREAD_ATTACHMENTS_LIST_MAX
    # 최근(뒤쪽) 것이 남는다.
    assert attachments[-1]['name'] == f'f{THREAD_ATTACHMENTS_LIST_MAX + 4}.txt'


def test_build_thread_attachments_note_renders_download_command():
    attachments = [
        {
            'name': 'a.pdf',
            'size': 10,
            'mimetype': 'application/pdf',
            'source': 'User',
            'msg_ts': '1',
            'url': 'https://files.slack.com/F1',
        }
    ]
    note = run_server._build_thread_attachments_note(attachments, truncated=False)
    assert '스레드의 과거 첨부' in note
    assert 'download_files.py' in note
    assert "'https://files.slack.com/F1'" in note  # 작은따옴표로 감쌈
    assert 'a.pdf' in note


def test_build_thread_attachments_note_empty():
    assert run_server._build_thread_attachments_note([], truncated=False) == ''


def test_build_context_includes_file_only_message():
    """텍스트 없이 첨부만 있는 메시지도 [첨부:] 표기로 포함된다."""
    messages = [
        {'user': 'U_USER', 'text': '', 'files': [{'id': 'F1', 'name': 'report.pdf'}], 'channel_type': 'im'},
    ]
    ctx = run_server.build_context(messages, is_dm=True)
    assert '[첨부: report.pdf]' in ctx
    assert ctx.startswith('User:')


def test_build_context_appends_attachment_note_to_text():
    messages = [{'user': 'U_USER', 'text': '여기 자료', 'files': [{'id': 'F1', 'name': 'a.csv'}]}]
    ctx = run_server.build_context(messages, is_dm=True)
    assert ctx == 'User: 여기 자료 [첨부: a.csv]'
