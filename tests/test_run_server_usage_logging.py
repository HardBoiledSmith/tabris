"""봇의 REQUEST 로깅과 Fargate 취소(sentinel) 동작/로깅을 검증한다.

구조화된 JSON 이벤트 로그(request/response, 토큰·비용)는 샌드박스 워커(sandbox_worker)가
CloudWatch로 남기므로 봇은 사람용 key=value 로그(logger.info)만 남긴다.
"""

import logging
from unittest.mock import MagicMock

import run_server


def _dm_event(text: str = 'hi') -> dict:
    return {
        'type': 'message',
        'channel_type': 'im',
        'channel': 'D123',
        'user': 'U_USER',
        'team_id': 'T_ALLOWED',
        'ts': '1700000000.000001',
        'text': text,
    }


def _cancel_body(
    *, team_id: str = 'T_ALLOWED', value: str = 'arn:aws:ecs:ap-northeast-2:111:task/tabris-poc/abc'
) -> dict:
    """취소 버튼 클릭(block_actions) 페이로드. value는 task ARN."""
    return {
        'team': {'id': team_id},
        'user': {'id': 'U_CANCELLER'},
        'channel': {'id': 'D123'},
        'message': {'ts': '1700000000.000100', 'thread_ts': '1700000000.000001'},
        'actions': [{'value': value}],
    }


def test_request_logged_human_readable(run_server_module, slack_client, dispatch_mock, caplog):
    """봇은 [REQUEST] key=value 사람용 로그를 남기고 디스패치한다(JSON 이벤트는 워커가 담당)."""
    with caplog.at_level(logging.INFO, logger='run_server'):
        run_server_module.on_dm(_dm_event('질문 내용'), slack_client)

    request_logs = [r.getMessage() for r in caplog.records if '[REQUEST]' in r.getMessage()]
    assert request_logs
    assert 'user=U_USER' in request_logs[0]
    assert 'team_id=T_ALLOWED' in request_logs[0]
    dispatch_mock.assert_called_once()


def test_cancel_stops_task_and_logs(monkeypatch, run_server_module, slack_client, caplog):
    """취소 → ecs StopTask 호출 + [CANCEL] 로그(result=stopped) + 안내 메시지 갱신."""
    stops = []
    monkeypatch.setattr(run_server, '_resolve_aws_credentials', lambda: {
        'AWS_ACCESS_KEY_ID': 'a', 'AWS_SECRET_ACCESS_KEY': 's', 'AWS_SESSION_TOKEN': 't',
    })
    monkeypatch.setattr(run_server, '_ecs_stop_task', lambda arn, reason, creds: stops.append(arn) or True)

    arn = 'arn:aws:ecs:ap-northeast-2:111:task/tabris-poc/abc'
    with caplog.at_level(logging.INFO, logger='run_server'):
        run_server_module.on_cancel_claude_run(MagicMock(), _cancel_body(value=arn), slack_client)

    # 해당 task ARN으로 StopTask 호출
    assert stops == [arn]

    cancel_logs = [r.getMessage() for r in caplog.records if '[CANCEL]' in r.getMessage()]
    assert cancel_logs and 'user=U_CANCELLER' in cancel_logs[0]
    assert 'result=stopped' in cancel_logs[0]
    assert f'task={arn}' in cancel_logs[0]
    # 사용자에게 취소 안내 메시지로 갱신
    assert slack_client.chat_update.called


def test_cancel_result_error_when_stop_fails(monkeypatch, run_server_module, slack_client, caplog):
    """StopTask 실패(이미 종료 등) 시 [CANCEL] 로그에 result=error로 남는다."""
    monkeypatch.setattr(run_server, '_resolve_aws_credentials', lambda: {
        'AWS_ACCESS_KEY_ID': 'a', 'AWS_SECRET_ACCESS_KEY': 's', 'AWS_SESSION_TOKEN': 't',
    })
    monkeypatch.setattr(run_server, '_ecs_stop_task', lambda arn, reason, creds: False)

    with caplog.at_level(logging.INFO, logger='run_server'):
        run_server_module.on_cancel_claude_run(MagicMock(), _cancel_body(), slack_client)

    cancel_logs = [r.getMessage() for r in caplog.records if '[CANCEL]' in r.getMessage()]
    assert cancel_logs and 'result=error' in cancel_logs[0]


def test_cancel_denied_for_disallowed_team_does_not_stop(monkeypatch, run_server_module, slack_client):
    """허용되지 않은 팀의 취소는 StopTask를 호출하지 않는다(가드 유지)."""
    stop_mock = MagicMock()
    monkeypatch.setattr(run_server, '_ecs_stop_task', stop_mock)

    run_server_module.on_cancel_claude_run(MagicMock(), _cancel_body(team_id='T_OTHER'), slack_client)

    stop_mock.assert_not_called()
