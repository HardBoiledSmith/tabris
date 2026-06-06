"""_run_claude_fargate가 RunTask 호출에 올바른 env override / 네트워크 설정을 싣는지 검증한다.

경계: S3 업로드(_s3_put_bytes)와 ECS RunTask(_ecs_run_task)를 패치해 캡처만 한다.
"""

from unittest.mock import MagicMock

import run_server


def _capture_run_task(monkeypatch):
    """_s3_put_bytes / _ecs_run_task / _resolve_aws_credentials를 패치하고 캡처 dict를 돌려준다."""
    cap = {}

    monkeypatch.setattr(run_server, '_resolve_aws_credentials', lambda: {
        'AWS_ACCESS_KEY_ID': 'a', 'AWS_SECRET_ACCESS_KEY': 's', 'AWS_SESSION_TOKEN': 't',
    })

    def fake_put(bucket, key, body, creds):
        cap.setdefault('puts', []).append({'bucket': bucket, 'key': key, 'body': body})

    def fake_run_task(network_config, overrides, creds):
        cap['network_config'] = network_config
        cap['overrides'] = overrides
        return {'tasks': [{'taskArn': 'arn:aws:ecs:...:task/abc'}], 'failures': []}

    monkeypatch.setattr(run_server, '_s3_put_bytes', fake_put)
    monkeypatch.setattr(run_server, '_ecs_run_task', fake_run_task)
    return cap


def _env_dict(overrides: dict) -> dict:
    """containerOverrides[0].environment 리스트를 {name: value} dict로 변환."""
    env = overrides['containerOverrides'][0]['environment']
    return {e['name']: e['value'] for e in env}


def _event() -> dict:
    return {
        'channel': 'D123',
        'channel_type': 'im',
        'team_id': 'T_ALLOWED',
        'ts': '1700000000.000001',
        'user': 'U_USER',
        'text': 'hello',
    }


def test_run_task_env_override_includes_job_and_secrets(monkeypatch):
    cap = _capture_run_task(monkeypatch)

    run_server._run_claude_fargate(
        _event(), 'PROMPT BODY', '1700000000.000001', '1700000000.000100', [], received_at=1700000000.5
    )

    env = _env_dict(cap['overrides'])
    # job 파라미터
    assert env['TABRIS_SLACK_CHANNEL'] == 'D123'
    assert env['TABRIS_SLACK_THREAD_TS'] == '1700000000.000001'
    assert env['TABRIS_SLACK_WAITING_MSG_TS'] == '1700000000.000100'
    assert env['TABRIS_PROMPT_S3_KEY'] == 'runs/1700000000.000001/prompt.txt'
    assert env['TABRIS_REQUEST_EPOCH'] == '1700000000.500'
    # 시크릿 (env override 직접 주입)
    assert env['ANTHROPIC_API_KEY'] == 'sk-test'
    assert env['SLACK_BOT_TOKEN'] == 'xoxb-test'
    assert env['NERV_MCP_TOKEN'] == 'nerv-token'
    assert env['GITHUB_PAT'] == 'ghp-test'
    # 컨테이너 이름이 task def와 일치
    assert cap['overrides']['containerOverrides'][0]['name'] == 'tabris-sandbox'


def test_run_task_prompt_uploaded_to_s3(monkeypatch):
    cap = _capture_run_task(monkeypatch)

    run_server._run_claude_fargate(
        _event(), 'PROMPT BODY', '1700000000.000001', '1700000000.000100', [], received_at=1.0
    )

    prompt_puts = [p for p in cap['puts'] if p['key'].endswith('prompt.txt')]
    assert prompt_puts, 'prompt가 S3에 업로드되어야 한다'
    assert prompt_puts[0]['body'] == b'PROMPT BODY'


def test_run_task_network_config_uses_subnets_and_sg(monkeypatch):
    cap = _capture_run_task(monkeypatch)
    monkeypatch.setattr(run_server, 'ECS_SUBNET_IDS', 'subnet-1,subnet-2')
    monkeypatch.setattr(run_server, 'ECS_SECURITY_GROUP_ID', 'sg-xyz')
    monkeypatch.setattr(run_server, 'ECS_ASSIGN_PUBLIC_IP', 'ENABLED')

    run_server._run_claude_fargate(
        _event(), 'p', '1700000000.000001', '1700000000.000100', [], received_at=1.0
    )

    awsvpc = cap['network_config']['awsvpcConfiguration']
    assert awsvpc['subnets'] == ['subnet-1', 'subnet-2']
    assert awsvpc['securityGroups'] == ['sg-xyz']
    assert awsvpc['assignPublicIp'] == 'ENABLED'


def test_run_task_raises_when_network_unconfigured(monkeypatch):
    _capture_run_task(monkeypatch)
    monkeypatch.setattr(run_server, 'ECS_SUBNET_IDS', '')
    monkeypatch.setattr(run_server, 'ECS_SECURITY_GROUP_ID', '')

    try:
        run_server._run_claude_fargate(
            _event(), 'p', '1700000000.000001', '1700000000.000100', [], received_at=1.0
        )
        raised = False
    except RuntimeError:
        raised = True
    assert raised, '서브넷/보안그룹 미설정 시 RuntimeError가 나야 한다'


def test_run_task_input_files_json_passed(monkeypatch):
    cap = _capture_run_task(monkeypatch)
    files = [{'filename': 'a.txt', 's3_key': 'runs/x/input/a.txt'}]

    run_server._run_claude_fargate(
        _event(), 'p', '1700000000.000001', '1700000000.000100', files, received_at=1.0
    )

    env = _env_dict(cap['overrides'])
    import json
    assert json.loads(env['TABRIS_INPUT_FILES_JSON']) == files


def test_dispatch_failure_updates_waiting_message(monkeypatch, slack_client):
    """run-task 실패 시 _handle_request_fargate가 대기 메시지를 오류로 갱신한다."""
    monkeypatch.setattr(run_server, '_upload_slack_files_to_s3', lambda e, t: [])
    monkeypatch.setattr(run_server, '_run_claude_fargate', MagicMock(side_effect=RuntimeError('boom')))

    run_server.handle_request(_event(), slack_client)

    # 접수 메시지 1회 + 오류 갱신 1회
    assert slack_client.chat_update.called
    err = slack_client.chat_update.call_args.kwargs['text']
    assert '샌드박스 시작 실패' in err
