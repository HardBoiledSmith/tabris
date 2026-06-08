"""End-to-end 테스트 (SQS 디스패치 기준).

진입점: `on_mention` / `on_dm` (Slack Bolt가 호출하는 핸들러)
경계 (외부 의존성):
  - Slack WebClient → MagicMock (`slack_client` fixture)
  - SQS 디스패치(`_enqueue_claude_job`) → MagicMock (`dispatch_mock` fixture)
  - settings_local → conftest에서 stub
  - executor.submit → conftest에서 동기화

샌드박스(claude 실행/결과 게시)는 별도 컨테이너(sandbox_worker)가 담당하므로,
봇 e2e는 ACL 통과 시 "접수 메시지 게시 + SQS 디스패치", 거부 시 "거부 메시지 + 디스패치 없음"만 검사한다.
"""

from unittest.mock import MagicMock


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


def _mpim_event(text: str = 'hi group', *, team_id: str = 'T_ALLOWED', user: str = 'U_USER') -> dict:
    return {
        'type': 'message',
        'channel_type': 'mpim',
        'channel': 'G123',
        'user': user,
        'team_id': team_id,
        'ts': '1700000000.000003',
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


# ---------------------------------------------------------------------------
# 해피 패스: 접수 메시지 게시 + Fargate 디스패치
# ---------------------------------------------------------------------------


def test_dm_happy_path_dispatches_fargate(run_server_module, slack_client, dispatch_mock):
    """DM 수신 → 접수 메시지 게시 + RunTask 디스패치."""
    run_server_module.on_dm(_dm_event('hello bot'), slack_client)

    waiting = [c for c in slack_client.chat_postMessage.call_args_list if '접수' in c.kwargs.get('text', '')]
    assert waiting, '접수(대기) 메시지가 게시되어야 한다'
    assert waiting[0].kwargs['channel'] == 'D123'
    assert waiting[0].kwargs['thread_ts'] == '1700000000.000001'
    dispatch_mock.assert_called_once()


def test_mention_happy_path_dispatches_fargate(run_server_module, slack_client, dispatch_mock):
    """채널 멘션 수신 → 접수 메시지 + 디스패치."""
    run_server_module.on_mention(_mention_event('<@UBOT> ping'), slack_client)

    waiting = [c for c in slack_client.chat_postMessage.call_args_list if '접수' in c.kwargs.get('text', '')]
    assert waiting and waiting[0].kwargs['channel'] == 'C123'
    dispatch_mock.assert_called_once()


def test_mpim_plaintext_happy_path_dispatches(run_server_module, slack_client, dispatch_mock):
    """그룹 DM(mpim)의 멘션 없는 평문도 on_dm을 통과해 디스패치된다."""
    run_server_module.on_dm(_mpim_event('hello group'), slack_client)

    waiting = [c for c in slack_client.chat_postMessage.call_args_list if '접수' in c.kwargs.get('text', '')]
    assert waiting and waiting[0].kwargs['channel'] == 'G123'
    dispatch_mock.assert_called_once()


def test_received_at_passed_to_dispatch(run_server_module, slack_client, dispatch_mock):
    """봇이 메시지를 받은 시점(received_at)이 디스패치에 전달된다(샌드박스 실행시간 계산용)."""
    run_server_module.on_dm(_dm_event('hello'), slack_client)

    # _enqueue_claude_job(event, prompt, thread_ts, waiting_ts, slack_input_files, received_at)
    received_at = dispatch_mock.call_args.args[5]
    assert isinstance(received_at, float) and received_at > 0


# ---------------------------------------------------------------------------
# ACL 거부: 디스패치 없음
# ---------------------------------------------------------------------------


def test_disallowed_team_is_rejected_without_dispatch(run_server_module, slack_client, dispatch_mock):
    run_server_module.on_dm(_dm_event('hi', team_id='T_OTHER'), slack_client)

    dispatch_mock.assert_not_called()
    slack_client.chat_postMessage.assert_called_once_with(
        channel='D123',
        thread_ts='1700000000.000001',
        text=run_server_module.TEAM_ACCESS_DENIED_TEXT,
    )


def test_any_allowed_team_is_accepted(run_server_module, slack_client, dispatch_mock, monkeypatch):
    monkeypatch.setattr(run_server_module, '_ALLOWED_TEAM_IDS', frozenset({'T_ALLOWED', 'T_ALLOWED_ALT'}))
    monkeypatch.setattr(run_server_module, '_ALL_ALLOWED_TEAMS', frozenset({'T_ALLOWED', 'T_ALLOWED_ALT'}))

    run_server_module.on_dm(_dm_event('hello', team_id='T_ALLOWED_ALT'), slack_client)

    dispatch_mock.assert_called_once()


def test_all_user_team_allows_any_user(run_server_module, slack_client, dispatch_mock, monkeypatch):
    monkeypatch.setattr(run_server_module, '_ALLOWED_ALL_USER_TEAM_IDS', frozenset({'T_OPEN'}))
    monkeypatch.setattr(run_server_module, '_ALL_ALLOWED_TEAMS', frozenset({'T_ALLOWED', 'T_OPEN'}))

    run_server_module.on_dm(_dm_event('hi', team_id='T_OPEN', user='U_RANDOM'), slack_client)

    dispatch_mock.assert_called_once()


def test_disallowed_user_is_rejected_on_mention(run_server_module, slack_client, dispatch_mock):
    run_server_module.on_mention(_mention_event('<@UBOT> hi', user='U_OTHER'), slack_client)

    dispatch_mock.assert_not_called()
    slack_client.chat_postMessage.assert_called_once_with(
        channel='C123',
        thread_ts='1700000000.000002',
        text=run_server_module.USER_ACCESS_DENIED_TEXT,
    )


def test_disallowed_user_is_rejected_on_dm(run_server_module, slack_client, dispatch_mock):
    run_server_module.on_dm(_dm_event('hi', user='U_OTHER'), slack_client)

    dispatch_mock.assert_not_called()
    slack_client.chat_postMessage.assert_called_once_with(
        channel='D123',
        thread_ts='1700000000.000001',
        text=run_server_module.USER_ACCESS_DENIED_TEXT,
    )


def test_disallowed_user_is_rejected_on_mpim(run_server_module, slack_client, dispatch_mock):
    run_server_module.on_dm(_mpim_event('hi', user='U_OTHER'), slack_client)

    dispatch_mock.assert_not_called()
    slack_client.chat_postMessage.assert_called_once_with(
        channel='G123',
        thread_ts='1700000000.000003',
        text=run_server_module.USER_ACCESS_DENIED_TEXT,
    )


# ---------------------------------------------------------------------------
# 봇 메시지 / 자기 자신 / 무시 케이스
# ---------------------------------------------------------------------------


def test_dm_from_other_bot_is_accepted(run_server_module, slack_client, dispatch_mock):
    """다른 봇(Slack workflow 등)이 보낸 DM은 처리한다(자기 자신만 제외)."""
    event = _dm_event('echo')
    event.pop('user', None)
    event['bot_id'] = 'B_OTHER'

    run_server_module.on_dm(event, slack_client)

    dispatch_mock.assert_called_once()


def test_dm_from_bot_in_disallowed_team_is_rejected(run_server_module, slack_client, dispatch_mock):
    event = _dm_event('echo', team_id='T_OTHER')
    event.pop('user', None)
    event['bot_id'] = 'B_OTHER'

    run_server_module.on_dm(event, slack_client)

    dispatch_mock.assert_not_called()
    slack_client.chat_postMessage.assert_called_once_with(
        channel='D123',
        thread_ts='1700000000.000001',
        text=run_server_module.TEAM_ACCESS_DENIED_TEXT,
    )


def test_dm_from_self_bot_id_is_ignored(run_server_module, slack_client, dispatch_mock):
    event = _dm_event('echo')
    event.pop('user', None)
    event['bot_id'] = 'B1'  # context의 self bot_id와 동일
    ctx = {'bot_id': 'B1', 'bot_user_id': 'UBOT'}

    run_server_module.on_dm(event, slack_client, ctx)

    dispatch_mock.assert_not_called()
    slack_client.chat_postMessage.assert_not_called()


def test_dm_from_self_user_id_is_ignored(run_server_module, slack_client, dispatch_mock):
    run_server_module.on_dm(_dm_event('echo', user='UBOT'), slack_client)

    dispatch_mock.assert_not_called()
    slack_client.chat_postMessage.assert_not_called()


def test_channel_message_without_mention_is_ignored(run_server_module, slack_client, dispatch_mock):
    event = _dm_event('hi')
    event['channel_type'] = 'channel'

    run_server_module.on_dm(event, slack_client)

    dispatch_mock.assert_not_called()
    slack_client.chat_postMessage.assert_not_called()


def test_dm_with_subtype_is_ignored(run_server_module, slack_client, dispatch_mock):
    event = _dm_event('hi')
    event['subtype'] = 'message_changed'

    run_server_module.on_dm(event, slack_client)

    dispatch_mock.assert_not_called()
    slack_client.chat_postMessage.assert_not_called()


def test_dm_file_share_without_event_team_id_enriches_from_context(run_server_module, slack_client, dispatch_mock):
    """team_id 없는 file_share DM도 Bolt context로 ACL을 통과해 디스패치된다."""
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

    dispatch_mock.assert_called_once()


def test_thread_history_is_included_in_prompt(run_server_module, slack_client, dispatch_mock):
    """스레드 이전 대화가 디스패치되는 프롬프트에 포함되고 현재 메시지는 제외된다."""
    slack_client.conversations_replies.return_value = {
        'messages': [
            {'text': 'earlier user question', 'user': 'U_USER'},
            {'text': 'earlier bot answer', 'bot_id': 'B1'},
            {'text': 'current message — should be excluded', 'user': 'U_USER'},
        ]
    }

    run_server_module.on_dm(_dm_event('current question'), slack_client)

    dispatch_mock.assert_called_once()
    prompt = dispatch_mock.call_args.args[1]  # _enqueue_claude_job(event, prompt, ...)
    assert '이전 대화' in prompt
    assert 'earlier user question' in prompt
    assert 'earlier bot answer' in prompt
    assert 'current question' in prompt
    assert 'current message — should be excluded' not in prompt
