"""End-to-end 테스트.

진입점: `on_mention` / `on_dm` (Slack Bolt가 호출하는 핸들러)
경계 (외부 의존성):
  - Slack WebClient → MagicMock (`slack_client` fixture)
  - subprocess.Popen (Docker/Claude 실행) → 패치
  - settings_local → conftest에서 stub
  - executor.submit → conftest에서 동기화

테스트는 이벤트를 넣고 Slack API 호출 결과만 검사한다(내부 함수는 검사하지 않음).
"""

from unittest.mock import MagicMock

from tests.helpers import install_claude_popen_mock


def _dm_event(text: str = 'hi there', *, team_id: str = 'T_ALLOWED', user: str = 'U_USER') -> dict:
    return {
        'type': 'message',
        'channel_type': 'im',
        'channel': 'D123',
        'user': user,
        'team_id': team_id,
        'ts': '1700000000.000001',
        'text': text,
    }


def _mention_event(text: str = '<@UBOT> ping', *, team_id: str = 'T_ALLOWED', user: str = 'U_USER') -> dict:
    return {
        'type': 'app_mention',
        'channel': 'C123',
        'user': user,
        'team_id': team_id,
        'ts': '1700000000.000002',
        'text': text,
    }


def test_dm_happy_path_posts_claude_result(run_server_module, slack_client, fake_claude_ok):
    """DM 수신 → Claude 응답이 대기 메시지를 갱신하며 그대로 게시된다."""
    fake_claude_ok('안녕하세요')

    run_server_module.on_dm(_dm_event('hello bot'), slack_client)

    post_calls = slack_client.chat_postMessage.call_args_list
    waiting_call = [c for c in post_calls if c.kwargs.get('text') == '⏳ 처리 중...']
    assert waiting_call, '대기 메시지가 게시되어야 한다'
    assert waiting_call[0].kwargs['channel'] == 'D123'
    assert waiting_call[0].kwargs['thread_ts'] == '1700000000.000001'
    assert slack_client.chat_update.called, 'Claude 응답이 대기 메시지를 갱신해야 한다'
    update_kwargs = slack_client.chat_update.call_args.kwargs
    assert update_kwargs['channel'] == 'D123'
    assert update_kwargs['ts'] == '1700000000.000100'
    assert '안녕하세요' in update_kwargs['text']


def test_mention_happy_path_posts_claude_result(run_server_module, slack_client, fake_claude_ok):
    """채널 멘션 수신 → Claude 응답이 게시된다."""
    fake_claude_ok('mention answer')

    run_server_module.on_mention(_mention_event('<@UBOT> ping'), slack_client)

    post_calls = slack_client.chat_postMessage.call_args_list
    waiting_call = [c for c in post_calls if c.kwargs.get('text') == '⏳ 처리 중...']
    assert waiting_call, '대기 메시지가 게시되어야 한다'
    assert waiting_call[0].kwargs['channel'] == 'C123'
    assert waiting_call[0].kwargs['thread_ts'] == '1700000000.000002'
    assert slack_client.chat_update.called
    assert 'mention answer' in slack_client.chat_update.call_args.kwargs['text']


def test_disallowed_team_is_rejected_without_claude(run_server_module, slack_client, monkeypatch):
    """허용 팀과 다른 팀이면 거부 메시지만 보내고 Claude를 돌리지 않는다."""
    popen_mock = MagicMock()
    monkeypatch.setattr(run_server_module.subprocess, 'Popen', popen_mock)

    run_server_module.on_dm(_dm_event('hi', team_id='T_OTHER'), slack_client)

    popen_mock.assert_not_called()
    slack_client.chat_postMessage.assert_called_once_with(
        channel='D123',
        thread_ts='1700000000.000001',
        text=run_server_module.TEAM_ACCESS_DENIED_TEXT,
    )
    slack_client.chat_update.assert_not_called()


def test_any_allowed_team_is_accepted(run_server_module, slack_client, fake_claude_ok, monkeypatch):
    """허용 팀 중 하나면 Claude를 실행한다."""
    monkeypatch.setattr(run_server_module, '_ALLOWED_TEAM_IDS', frozenset({'T_ALLOWED', 'T_ALLOWED_ALT'}))
    monkeypatch.setattr(run_server_module, '_ALL_ALLOWED_TEAMS', frozenset({'T_ALLOWED', 'T_ALLOWED_ALT'}))
    fake_claude_ok('alt team ok')

    run_server_module.on_dm(_dm_event('hello', team_id='T_ALLOWED_ALT'), slack_client)

    assert slack_client.chat_update.called
    assert 'alt team ok' in slack_client.chat_update.call_args.kwargs['text']


def test_all_user_team_allows_any_user(run_server_module, slack_client, fake_claude_ok, monkeypatch):
    """ALLOWED_ALL_USER_TEAM_IDS에 속한 팀이면 user 목록에 없는 사람도 허용한다."""
    monkeypatch.setattr(run_server_module, '_ALLOWED_ALL_USER_TEAM_IDS', frozenset({'T_OPEN'}))
    monkeypatch.setattr(run_server_module, '_ALL_ALLOWED_TEAMS', frozenset({'T_ALLOWED', 'T_OPEN'}))
    fake_claude_ok('open team ok')

    run_server_module.on_dm(_dm_event('hi', team_id='T_OPEN', user='U_RANDOM'), slack_client)

    assert slack_client.chat_update.called
    assert 'open team ok' in slack_client.chat_update.call_args.kwargs['text']


def test_disallowed_user_is_rejected_on_mention(run_server_module, slack_client, monkeypatch):
    """멘션 경로는 ALLOWED_USER_IDS에 없는 user면 거부한다."""
    popen_mock = MagicMock()
    monkeypatch.setattr(run_server_module.subprocess, 'Popen', popen_mock)

    run_server_module.on_mention(_mention_event('<@UBOT> hi', user='U_OTHER'), slack_client)

    popen_mock.assert_not_called()
    slack_client.chat_postMessage.assert_called_once_with(
        channel='C123',
        thread_ts='1700000000.000002',
        text=run_server_module.USER_ACCESS_DENIED_TEXT,
    )
    slack_client.chat_update.assert_not_called()


def test_disallowed_user_is_rejected_on_dm(run_server_module, slack_client, monkeypatch):
    """DM 경로도 사람(user)에는 ALLOWED_USER_IDS를 적용해 미허용 user를 거부한다."""
    popen_mock = MagicMock()
    monkeypatch.setattr(run_server_module.subprocess, 'Popen', popen_mock)

    run_server_module.on_dm(_dm_event('hi', user='U_OTHER'), slack_client)

    popen_mock.assert_not_called()
    slack_client.chat_postMessage.assert_called_once_with(
        channel='D123',
        thread_ts='1700000000.000001',
        text=run_server_module.USER_ACCESS_DENIED_TEXT,
    )
    slack_client.chat_update.assert_not_called()


def test_claude_nonzero_exit_returns_error_message(run_server_module, slack_client, monkeypatch):
    """Claude 종료 코드 ≠ 0 → 오류 메시지가 스레드에 게시된다."""
    install_claude_popen_mock(
        monkeypatch,
        stdout_text='',
        stderr_text='boom!',
        returncode=1,
    )

    run_server_module.on_dm(_dm_event('fail please'), slack_client)

    assert slack_client.chat_update.called
    text = slack_client.chat_update.call_args.kwargs['text']
    assert '⚠️ 실행 오류' in text
    assert 'boom!' in text


def test_claude_timeout_returns_timeout_message(run_server_module, slack_client, monkeypatch):
    """경과 시간이 CLAUDE_TIMEOUT을 넘기면 타임아웃 메시지가 게시된다."""

    monkeypatch.setattr(run_server_module, 'CLAUDE_TIMEOUT', -1)
    install_claude_popen_mock(monkeypatch, stdout_text='never mind', returncode=0)

    run_server_module.on_dm(_dm_event('long task'), slack_client)

    assert slack_client.chat_update.called
    text = slack_client.chat_update.call_args.kwargs['text']
    assert '시간 초과' in text


def test_dm_from_other_bot_is_accepted(run_server_module, slack_client, fake_claude_ok):
    """다른 봇(Slack workflow 등)이 보낸 DM은 처리한다(자기 자신만 제외)."""
    fake_claude_ok('bot dm ok')

    event = _dm_event('echo')
    event.pop('user', None)
    event['bot_id'] = 'B_OTHER'

    run_server_module.on_dm(event, slack_client)

    assert slack_client.chat_update.called
    assert 'bot dm ok' in slack_client.chat_update.call_args.kwargs['text']


def test_dm_from_bot_in_disallowed_team_is_rejected(run_server_module, slack_client, monkeypatch):
    """봇이라도 허용 팀에 없는 팀이면 거부한다(봇은 팀 검사만 받는다)."""
    popen_mock = MagicMock()
    monkeypatch.setattr(run_server_module.subprocess, 'Popen', popen_mock)

    event = _dm_event('echo', team_id='T_OTHER')
    event.pop('user', None)
    event['bot_id'] = 'B_OTHER'

    run_server_module.on_dm(event, slack_client)

    popen_mock.assert_not_called()
    slack_client.chat_postMessage.assert_called_once_with(
        channel='D123',
        thread_ts='1700000000.000001',
        text=run_server_module.TEAM_ACCESS_DENIED_TEXT,
    )
    slack_client.chat_update.assert_not_called()


def test_dm_from_self_bot_id_is_ignored(run_server_module, slack_client, monkeypatch):
    """봇 자신의 bot_id로 들어온 DM은 무시한다(무한루프 방지)."""
    popen_mock = MagicMock()
    monkeypatch.setattr(run_server_module.subprocess, 'Popen', popen_mock)

    event = _dm_event('echo')
    event.pop('user', None)
    event['bot_id'] = 'B1'  # context의 self bot_id와 동일
    ctx = {'bot_id': 'B1', 'bot_user_id': 'UBOT'}

    run_server_module.on_dm(event, slack_client, ctx)

    popen_mock.assert_not_called()
    slack_client.chat_postMessage.assert_not_called()
    slack_client.chat_update.assert_not_called()


def test_dm_from_self_user_id_is_ignored(run_server_module, slack_client, monkeypatch):
    """context가 없어도 BOT_USER_ID로 자기 자신 DM은 무시한다(폴백 방어)."""
    popen_mock = MagicMock()
    monkeypatch.setattr(run_server_module.subprocess, 'Popen', popen_mock)

    run_server_module.on_dm(_dm_event('echo', user='UBOT'), slack_client)

    popen_mock.assert_not_called()
    slack_client.chat_postMessage.assert_not_called()
    slack_client.chat_update.assert_not_called()


def test_channel_message_without_mention_is_ignored(run_server_module, slack_client, monkeypatch):
    """일반 채널 메시지(`channel_type != 'im'`)는 `on_dm` 경로에서 무시된다."""
    popen_mock = MagicMock()
    monkeypatch.setattr(run_server_module.subprocess, 'Popen', popen_mock)

    event = _dm_event('hi')
    event['channel_type'] = 'channel'

    run_server_module.on_dm(event, slack_client)

    popen_mock.assert_not_called()
    slack_client.chat_postMessage.assert_not_called()


def test_dm_with_subtype_is_ignored(run_server_module, slack_client, monkeypatch):
    """subtype이 있는 메시지(예: message_changed)는 무시한다."""
    popen_mock = MagicMock()
    monkeypatch.setattr(run_server_module.subprocess, 'Popen', popen_mock)

    event = _dm_event('hi')
    event['subtype'] = 'message_changed'

    run_server_module.on_dm(event, slack_client)

    popen_mock.assert_not_called()
    slack_client.chat_postMessage.assert_not_called()


def test_dm_file_share_without_event_team_id_enriches_from_context(run_server_module, slack_client, fake_claude_ok):
    """일부 DM(file_share 등)은 event에 team_id가 없어도 Bolt context로 ACL 통과한다."""
    fake_claude_ok('처리 완료')

    event = {
        'type': 'message',
        'channel_type': 'im',
        'channel': 'D123',
        'user': 'U_USER',
        'subtype': 'file_share',
        'ts': '1700000000.000099',
        'text': '이 파일로 재시도 해줘',
        'files': [],
    }
    ctx = MagicMock()
    ctx.team_id = 'T_ALLOWED'
    ctx.actor_team_id = None

    run_server_module.on_dm(event, slack_client, ctx)

    assert slack_client.chat_update.called


def test_thread_history_is_passed_to_claude(run_server_module, slack_client, monkeypatch):
    """스레드에 이전 대화가 있으면 Claude에 전달되는 프롬프트에 포함된다."""
    slack_client.conversations_replies.return_value = {
        'messages': [
            {'text': 'earlier user question', 'user': 'U_USER'},
            {'text': 'earlier bot answer', 'bot_id': 'B1'},
            {'text': 'current message — should be excluded', 'user': 'U_USER'},
        ]
    }

    captured: dict = {}
    install_claude_popen_mock(monkeypatch, stdout_text='ok', returncode=0, cmd_capture=captured)

    run_server_module.on_dm(_dm_event('current question'), slack_client)

    assert 'cmd' in captured, 'subprocess.Popen이 호출되어야 한다'
    prompt_blob = '\n'.join(str(x) for x in captured['cmd'])
    assert '이전 대화' in prompt_blob
    assert 'earlier user question' in prompt_blob
    assert 'earlier bot answer' in prompt_blob
    assert 'current question' in prompt_blob
    assert 'current message — should be excluded' not in prompt_blob
