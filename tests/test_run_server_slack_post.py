"""post_claude_markdown_to_thread의 msg_too_long degrade ladder 단위 테스트.

경계:
  - Slack WebClient → MagicMock
  - convert_markdown_to_slack_blocks → 1개 블록 반환하도록 패치
  - SlackApiError(msg_too_long) side_effect로 각 단계 강제
"""

from unittest.mock import MagicMock

import pytest
from slack_sdk.errors import SlackApiError

import run_server
from run_server import SLACK_MSG_FILE_NOTICE
from run_server import SLACK_MSG_REDIRECT_NOTICE
from run_server import post_claude_markdown_to_thread

_CHANNEL = 'C_TEST'
_THREAD_TS = '111.0'
_UPDATE_TS = '222.0'
_MARKDOWN = '## Hello\nworld'
_SOURCE_TEXT = '## Hello\nworld'


def _msg_too_long_error() -> SlackApiError:
    response = MagicMock()
    response.get = lambda key, default=None: 'msg_too_long' if key == 'error' else default
    return SlackApiError('msg_too_long', response)


def _other_slack_error() -> SlackApiError:
    response = MagicMock()
    response.get = lambda key, default=None: 'channel_not_found' if key == 'error' else default
    return SlackApiError('channel_not_found', response)


@pytest.fixture()
def client():
    c = MagicMock()
    c.chat_update.return_value = {'ok': True}
    c.chat_postMessage.return_value = {'ok': True}
    c.files_upload_v2.return_value = {'ok': True}
    return c


@pytest.fixture(autouse=True)
def _stub_markdown_converter(monkeypatch):
    """markdown → 단일 블록 반환으로 고정해 Slack 변환 라이브러리와 분리한다."""
    fake_block = [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': _SOURCE_TEXT}}]
    monkeypatch.setattr(run_server, 'convert_markdown_to_slack_blocks', lambda *a, **kw: fake_block)
    monkeypatch.setattr(
        run_server,
        'build_fallback_text_from_blocks',
        lambda blocks, **kw: _SOURCE_TEXT if blocks else '',
    )


# ---------------------------------------------------------------------------
# 정상 경로: chat.update 성공
# ---------------------------------------------------------------------------


def test_happy_path_update_succeeds(client):
    """chat.update가 성공하면 redirect stub / postMessage 없음."""
    post_claude_markdown_to_thread(client, _CHANNEL, _THREAD_TS, _MARKDOWN, _UPDATE_TS)

    client.chat_update.assert_called_once()
    update_kwargs = client.chat_update.call_args.kwargs
    assert update_kwargs['channel'] == _CHANNEL
    assert update_kwargs['ts'] == _UPDATE_TS
    assert 'blocks' in update_kwargs

    # 새 메시지 게시 없음
    client.chat_postMessage.assert_not_called()
    client.files_upload_v2.assert_not_called()


# ---------------------------------------------------------------------------
# 1단계 실패: update msg_too_long → stub update + postMessage(blocks) 성공
# ---------------------------------------------------------------------------


def test_update_msg_too_long_then_post_blocks_succeeds(client):
    """chat.update가 msg_too_long → stub update + postMessage(blocks+text) 성공."""
    client.chat_update.side_effect = [
        _msg_too_long_error(),  # 원본 update 실패
        {'ok': True},  # stub (REDIRECT_NOTICE) update 성공
    ]

    post_claude_markdown_to_thread(client, _CHANNEL, _THREAD_TS, _MARKDOWN, _UPDATE_TS)

    # 첫 번째 update: 원본 payload, 두 번째 update: stub
    assert client.chat_update.call_count == 2
    stub_call = client.chat_update.call_args_list[1]
    assert stub_call.kwargs['text'] == SLACK_MSG_REDIRECT_NOTICE
    assert stub_call.kwargs['blocks'] == []

    # 본문은 postMessage(blocks+text)로
    client.chat_postMessage.assert_called_once()
    pm_kwargs = client.chat_postMessage.call_args.kwargs
    assert 'blocks' in pm_kwargs
    assert pm_kwargs['channel'] == _CHANNEL
    assert pm_kwargs['thread_ts'] == _THREAD_TS

    client.files_upload_v2.assert_not_called()


# ---------------------------------------------------------------------------
# 2단계 실패: update + post(blocks) msg_too_long → postMessage(text-only) 성공
# ---------------------------------------------------------------------------


def test_update_and_post_blocks_fail_then_text_only_succeeds(client):
    """update + postMessage(blocks) 모두 msg_too_long → postMessage(text-only) 성공."""
    client.chat_update.side_effect = [
        _msg_too_long_error(),  # 원본 update 실패
        {'ok': True},  # stub update 성공
    ]
    client.chat_postMessage.side_effect = [
        _msg_too_long_error(),  # stage 1: blocks+text 실패
        {'ok': True},  # stage 2: text-only 성공
    ]

    post_claude_markdown_to_thread(client, _CHANNEL, _THREAD_TS, _MARKDOWN, _UPDATE_TS)

    assert client.chat_update.call_count == 2
    assert client.chat_postMessage.call_count == 2

    # 두 번째 postMessage: text-only (blocks 없음)
    text_only_call = client.chat_postMessage.call_args_list[1]
    assert 'blocks' not in text_only_call.kwargs
    assert text_only_call.kwargs['text'] == _SOURCE_TEXT

    client.files_upload_v2.assert_not_called()


# ---------------------------------------------------------------------------
# 3단계: update + post(blocks) + post(text-only) 모두 msg_too_long → 파일 첨부
# ---------------------------------------------------------------------------


def test_all_post_fail_then_file_upload(client):
    """모든 postMessage가 msg_too_long → file notice + files_upload_v2 호출."""
    client.chat_update.side_effect = [
        _msg_too_long_error(),
        {'ok': True},  # stub update
    ]
    client.chat_postMessage.side_effect = [
        _msg_too_long_error(),  # stage 1
        _msg_too_long_error(),  # stage 2
        {'ok': True},  # stage 3: file notice
    ]

    post_claude_markdown_to_thread(client, _CHANNEL, _THREAD_TS, _MARKDOWN, _UPDATE_TS)

    # stage 3 notice 메시지
    notice_call = client.chat_postMessage.call_args_list[2]
    assert notice_call.kwargs['text'] == SLACK_MSG_FILE_NOTICE

    # 파일 업로드
    client.files_upload_v2.assert_called_once()
    upload_kwargs = client.files_upload_v2.call_args.kwargs
    assert upload_kwargs['channel'] == _CHANNEL
    assert upload_kwargs['thread_ts'] == _THREAD_TS
    assert _SOURCE_TEXT.encode('utf-8') == upload_kwargs['content']


# ---------------------------------------------------------------------------
# 비 msg_too_long SlackApiError는 degrade 없이 예외 전파
# ---------------------------------------------------------------------------


def test_non_msg_too_long_update_error_falls_through_without_stub(client):
    """update가 msg_too_long 이외의 오류 → stub 없이 _post_with_degrade 시도."""
    client.chat_update.side_effect = _other_slack_error()

    # postMessage는 성공
    post_claude_markdown_to_thread(client, _CHANNEL, _THREAD_TS, _MARKDOWN, _UPDATE_TS)

    # stub(REDIRECT_NOTICE) update가 호출되지 않음
    update_texts = [c.kwargs.get('text') for c in client.chat_update.call_args_list]
    assert SLACK_MSG_REDIRECT_NOTICE not in update_texts

    # 새 메시지로 본문 게시
    client.chat_postMessage.assert_called_once()


def test_post_non_msg_too_long_error_is_propagated(client):
    """postMessage가 msg_too_long 이외의 오류 → 예외가 상위로 전파된다."""
    client.chat_update.side_effect = _msg_too_long_error()
    client.chat_update.side_effect = [_msg_too_long_error(), {'ok': True}]
    client.chat_postMessage.side_effect = _other_slack_error()

    with pytest.raises(SlackApiError):
        post_claude_markdown_to_thread(client, _CHANNEL, _THREAD_TS, _MARKDOWN, _UPDATE_TS)


# ---------------------------------------------------------------------------
# 2번째 이후 chunk는 update 없이 _post_with_degrade만 사용
# ---------------------------------------------------------------------------


def test_second_chunk_uses_post_only(client, monkeypatch):
    """50블록 초과 시 첫 chunk는 update, 나머지는 postMessage만."""
    block = {'type': 'section', 'text': {'type': 'mrkdwn', 'text': 'x'}}
    # 51개 블록 반환 → 2개 chunk
    monkeypatch.setattr(run_server, 'convert_markdown_to_slack_blocks', lambda *a, **kw: [block] * 51)

    post_claude_markdown_to_thread(client, _CHANNEL, _THREAD_TS, _MARKDOWN, _UPDATE_TS)

    # update 1회 (첫 chunk), postMessage 1회 (두 번째 chunk)
    client.chat_update.assert_called_once()
    client.chat_postMessage.assert_called_once()
    pm_kwargs = client.chat_postMessage.call_args.kwargs
    assert pm_kwargs['thread_ts'] == _THREAD_TS
    assert 'ts' not in pm_kwargs  # update 아님
