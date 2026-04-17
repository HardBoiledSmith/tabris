"""End-to-end 테스트.

진입점: `on_mention` / `on_dm` (Slack Bolt가 호출하는 핸들러)
경계 (외부 의존성):
  - Slack WebClient → MagicMock (`slack_client` fixture)
  - subprocess.run (Docker/Claude 실행) → 패치
  - settings_local → conftest에서 stub
  - executor.submit → conftest에서 동기화

테스트는 이벤트를 넣고 Slack API 호출 결과만 검사한다(내부 함수는 검사하지 않음).
"""

import subprocess as real_subprocess
from unittest.mock import MagicMock


def _dm_event(text: str = 'hi there', *, team_id: str = 'T_ALLOWED') -> dict:
    return {
        'type': 'message',
        'channel_type': 'im',
        'channel': 'D123',
        'user': 'U_USER',
        'team_id': team_id,
        'ts': '1700000000.000001',
        'text': text,
    }


def _mention_event(text: str = '<@UBOT> ping', *, team_id: str = 'T_ALLOWED') -> dict:
    return {
        'type': 'app_mention',
        'channel': 'C123',
        'user': 'U_USER',
        'team_id': team_id,
        'ts': '1700000000.000002',
        'text': text,
    }


def test_dm_happy_path_posts_claude_result(run_server_module, slack_client, fake_claude_ok):
    """DM 수신 → Claude 응답이 대기 메시지를 갱신하며 그대로 게시된다."""
    fake_claude_ok('안녕하세요')

    run_server_module.on_dm(_dm_event('hello bot'), slack_client)

    slack_client.chat_postMessage.assert_any_call(channel='D123', thread_ts='1700000000.000001', text='⏳ 처리 중...')
    assert slack_client.chat_update.called, 'Claude 응답이 대기 메시지를 갱신해야 한다'
    update_kwargs = slack_client.chat_update.call_args.kwargs
    assert update_kwargs['channel'] == 'D123'
    assert update_kwargs['ts'] == '1700000000.000100'
    assert '안녕하세요' in update_kwargs['text']


def test_mention_happy_path_posts_claude_result(run_server_module, slack_client, fake_claude_ok):
    """채널 멘션 수신 → Claude 응답이 게시된다."""
    fake_claude_ok('mention answer')

    run_server_module.on_mention(_mention_event('<@UBOT> ping'), slack_client)

    slack_client.chat_postMessage.assert_any_call(channel='C123', thread_ts='1700000000.000002', text='⏳ 처리 중...')
    assert slack_client.chat_update.called
    assert 'mention answer' in slack_client.chat_update.call_args.kwargs['text']


def test_disallowed_team_is_rejected_without_claude(run_server_module, slack_client, monkeypatch):
    """ALLOWED_TEAM_ID와 다른 팀이면 거부 메시지만 보내고 Claude를 돌리지 않는다."""
    run_mock = MagicMock()
    monkeypatch.setattr(run_server_module.subprocess, 'run', run_mock)

    run_server_module.on_dm(_dm_event('hi', team_id='T_OTHER'), slack_client)

    run_mock.assert_not_called()
    slack_client.chat_postMessage.assert_called_once_with(
        channel='D123',
        thread_ts='1700000000.000001',
        text=run_server_module.TEAM_ACCESS_DENIED_TEXT,
    )
    slack_client.chat_update.assert_not_called()


def test_claude_nonzero_exit_returns_error_message(run_server_module, slack_client, monkeypatch):
    """Claude 종료 코드 ≠ 0 → 오류 메시지가 스레드에 게시된다."""
    completed = MagicMock(returncode=1, stdout='', stderr='boom!')
    monkeypatch.setattr(run_server_module.subprocess, 'run', MagicMock(return_value=completed))

    run_server_module.on_dm(_dm_event('fail please'), slack_client)

    assert slack_client.chat_update.called
    text = slack_client.chat_update.call_args.kwargs['text']
    assert '⚠️ 실행 오류' in text
    assert 'boom!' in text


def test_claude_timeout_returns_timeout_message(run_server_module, slack_client, monkeypatch):
    """subprocess가 TimeoutExpired를 내면 타임아웃 메시지가 게시된다."""

    def _raise(*args, **kwargs):
        raise real_subprocess.TimeoutExpired(cmd='docker', timeout=30)

    monkeypatch.setattr(run_server_module.subprocess, 'run', _raise)

    run_server_module.on_dm(_dm_event('long task'), slack_client)

    assert slack_client.chat_update.called
    text = slack_client.chat_update.call_args.kwargs['text']
    assert '시간 초과' in text


def test_dm_from_bot_is_ignored(run_server_module, slack_client, monkeypatch):
    """bot_id가 붙은 DM은 무시한다(봇 자신의 메시지 루프 방지)."""
    run_mock = MagicMock()
    monkeypatch.setattr(run_server_module.subprocess, 'run', run_mock)

    event = _dm_event('echo')
    event['bot_id'] = 'B_OTHER'

    run_server_module.on_dm(event, slack_client)

    run_mock.assert_not_called()
    slack_client.chat_postMessage.assert_not_called()
    slack_client.chat_update.assert_not_called()


def test_channel_message_without_mention_is_ignored(run_server_module, slack_client, monkeypatch):
    """일반 채널 메시지(`channel_type != 'im'`)는 `on_dm` 경로에서 무시된다."""
    run_mock = MagicMock()
    monkeypatch.setattr(run_server_module.subprocess, 'run', run_mock)

    event = _dm_event('hi')
    event['channel_type'] = 'channel'

    run_server_module.on_dm(event, slack_client)

    run_mock.assert_not_called()
    slack_client.chat_postMessage.assert_not_called()


def test_dm_with_subtype_is_ignored(run_server_module, slack_client, monkeypatch):
    """subtype이 있는 메시지(예: message_changed)는 무시한다."""
    run_mock = MagicMock()
    monkeypatch.setattr(run_server_module.subprocess, 'run', run_mock)

    event = _dm_event('hi')
    event['subtype'] = 'message_changed'

    run_server_module.on_dm(event, slack_client)

    run_mock.assert_not_called()
    slack_client.chat_postMessage.assert_not_called()


def test_thread_history_is_passed_to_claude(run_server_module, slack_client, monkeypatch):
    """스레드에 이전 대화가 있으면 Claude에 전달되는 프롬프트에 포함된다."""
    slack_client.conversations_replies.return_value = {
        'messages': [
            {'text': 'earlier user question', 'user': 'U_USER'},
            {'text': 'earlier bot answer', 'bot_id': 'B1'},
            {'text': 'current message — should be excluded', 'user': 'U_USER'},
        ]
    }

    captured = {}

    def _fake_run(cmd, *args, **kwargs):
        captured['cmd'] = cmd
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = '{"result": "ok"}'
        completed.stderr = ''
        return completed

    monkeypatch.setattr(run_server_module.subprocess, 'run', _fake_run)

    run_server_module.on_dm(_dm_event('current question'), slack_client)

    assert 'cmd' in captured, 'subprocess.run이 호출되어야 한다'
    # cmd 리스트 안 어딘가에 Claude 프롬프트가 문자열로 실려간다.
    prompt_blob = '\n'.join(str(x) for x in captured['cmd'])
    assert '이전 대화' in prompt_blob
    assert 'earlier user question' in prompt_blob
    assert 'earlier bot answer' in prompt_blob
    assert 'current question' in prompt_blob
    assert 'current message — should be excluded' not in prompt_blob
