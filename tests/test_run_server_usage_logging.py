"""`--output-format json` 응답에서 본문(result)을 뽑고 usage를 RESPONSE 로그에 남기는지 검증한다."""

import logging

from tests.helpers import install_claude_popen_mock


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


def test_json_result_extracted_not_posted_raw(run_server_module, slack_client, fake_claude_ok):
    """게시되는 본문은 JSON 봉투가 아니라 result 텍스트여야 한다."""
    fake_claude_ok('ANSWER_MARKER_42')

    run_server_module.on_dm(_dm_event('질문'), slack_client)

    assert slack_client.chat_update.called
    posted = slack_client.chat_update.call_args.kwargs['text']
    assert 'ANSWER_MARKER_42' in posted
    # JSON 봉투가 그대로 새어나가지 않아야 한다.
    assert 'total_cost_usd' not in posted
    assert '"result"' not in posted


def test_response_log_includes_token_usage(run_server_module, slack_client, fake_claude_ok, caplog):
    """RESPONSE 로그에 total_cost_usd / input_tokens / output_tokens 가 찍힌다(output_length 대체)."""
    fake_claude_ok('ok')

    with caplog.at_level(logging.INFO, logger='run_server'):
        run_server_module.on_dm(_dm_event('질문'), slack_client)

    response_logs = [r.getMessage() for r in caplog.records if '[RESPONSE]' in r.getMessage()]
    assert response_logs, 'RESPONSE 로그가 남아야 한다'
    msg = response_logs[0]
    assert 'model=claude-opus-4-8[1m]' in msg
    assert 'total_cost_usd=0.012300' in msg
    assert 'input_tokens=100' in msg
    assert 'output_tokens=20' in msg
    assert 'output_length' not in msg


def test_event_json_file_gets_request_and_response(run_server_module, slack_client, fake_claude_ok, event_log_lines):
    """REQUEST/RESPONSE가 별도 JSON 파일에 같은 내용으로 한 줄씩 기록된다."""
    fake_claude_ok('json answer')

    run_server_module.on_dm(_dm_event('질문 내용'), slack_client)

    events = event_log_lines()
    by_evt = {e['evt']: e for e in events}
    assert 'request' in by_evt, '요청 이벤트 JSON이 있어야 한다'
    assert 'response' in by_evt, '응답 이벤트 JSON이 있어야 한다'

    req = by_evt['request']
    assert req['type'] == 'DM'
    assert req['user'] == 'U_USER'
    assert req['team_id'] == 'T_ALLOWED'
    assert req['text'] == '질문 내용'
    assert 'ts' in req

    resp = by_evt['response']
    assert resp['user'] == 'U_USER'
    assert resp['total_cost_usd'] == 0.0123
    assert resp['input_tokens'] == 100
    assert resp['output_tokens'] == 20
    # 보조 haiku가 아니라 비용을 지배하는 메인 모델이 기재돼야 한다.
    assert resp['model'] == 'claude-opus-4-8[1m]'
    # request/response가 같은 메시지로 상관(correlate) 가능해야 한다.
    assert req['msg_id'] == resp['msg_id']


def test_response_log_falls_back_on_non_json_stdout(monkeypatch, run_server_module, slack_client, caplog):
    """JSON 파싱 실패 시 raw stdout을 본문으로 쓰고 토큰은 N/A로 남긴다."""
    install_claude_popen_mock(monkeypatch, stdout_text='plain text answer', returncode=0)

    with caplog.at_level(logging.INFO, logger='run_server'):
        run_server_module.on_dm(_dm_event('질문'), slack_client)

    assert slack_client.chat_update.called
    assert 'plain text answer' in slack_client.chat_update.call_args.kwargs['text']
    response_logs = [r.getMessage() for r in caplog.records if '[RESPONSE]' in r.getMessage()]
    assert response_logs and 'total_cost_usd=N/A' in response_logs[0]
