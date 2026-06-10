"""워밍 풀(SQS 디스패치 + 워커 루프 모드 + 멱등/취소 마커) 단위 테스트.

경계:
  - 봇: _resolve_aws_credentials / _s3_put_bytes / _sqs_send_message / _ecs_stop_task 패치 후 캡처
  - 워커: _sqs_* / _marker_* / process_job / reset_workspace 패치 후 루프 흐름 검증
"""

from unittest.mock import MagicMock

import pytest

import run_server
import sandbox_worker
from tabris_slack_utils import decode_cancel_value
from tabris_slack_utils import encode_cancel_value


# ---------------------------------------------------------------------------
# 취소 버튼 value 인코딩
# ---------------------------------------------------------------------------
def test_encode_decode_cancel_value_roundtrip():
    value = encode_cancel_value('arn:aws:ecs:r:1:task/c/x', 'TS-MID')
    assert decode_cancel_value(value) == ('arn:aws:ecs:r:1:task/c/x', 'TS-MID')


def test_decode_cancel_value_legacy_plain_arn():
    # 레거시(평문 ARN) value도 그대로 task_arn으로 수용, job_id는 None.
    assert decode_cancel_value('arn:aws:ecs:r:1:task/c/x') == ('arn:aws:ecs:r:1:task/c/x', None)


def test_encode_cancel_value_handles_none():
    assert decode_cancel_value(encode_cancel_value(None, None)) == ('', None)


# ---------------------------------------------------------------------------
# 봇: SQS 디스패치
# ---------------------------------------------------------------------------
def _patch_dispatch(monkeypatch):
    cap = {'puts': []}
    monkeypatch.setattr(
        run_server,
        '_resolve_aws_credentials',
        lambda: {
            'AWS_ACCESS_KEY_ID': 'a',
            'AWS_SECRET_ACCESS_KEY': 's',
            'AWS_SESSION_TOKEN': 't',
        },
    )

    def fake_put(bucket, key, body, creds):
        cap['puts'].append({'bucket': bucket, 'key': key, 'body': body})

    monkeypatch.setattr(run_server, '_s3_put_bytes', fake_put)

    def fake_send(queue_url, group_id, dedup_id, body, creds):
        cap['send'] = {'queue_url': queue_url, 'group_id': group_id, 'dedup_id': dedup_id, 'body': body}
        return {'MessageId': 'm1'}

    monkeypatch.setattr(run_server, '_sqs_send_message', fake_send)
    monkeypatch.setattr(run_server, 'SQS_QUEUE_URL', 'https://sqs/q.fifo')
    return cap


def test_enqueue_claude_job_builds_body_and_group(monkeypatch):
    cap = _patch_dispatch(monkeypatch)
    event = {'channel': 'D1', 'channel_type': 'im', 'user': 'U_USER', 'ts': '1700000000.000001'}

    job_id = run_server._enqueue_claude_job(
        event,
        'PROMPT BODY',
        '1700000000.000001',
        '1700000000.000100',
        [{'filename': 'a.txt', 'url': 'https://files.slack.com/a', 'size': 5}],
        received_at=1700000000.5,
    )

    # prompt가 S3로 업로드된다.
    assert any(p['key'] == 'runs/1700000000.000001/prompt.txt' and p['body'] == b'PROMPT BODY' for p in cap['puts'])
    send = cap['send']
    assert send['queue_url'] == 'https://sqs/q.fifo'
    # 직렬화 경계 = 유저 → group=user_id, dedup=job_id.
    assert send['group_id'] == 'U_USER'
    assert send['dedup_id'] == job_id
    body = send['body']
    assert body['job_id'] == job_id
    assert body['channel'] == 'D1'
    assert body['thread_ts'] == '1700000000.000001'
    assert body['waiting_msg_ts'] == '1700000000.000100'
    assert body['user_id'] == 'U_USER'
    assert body['is_dm'] is True
    assert body['prompt_s3_key'] == 'runs/1700000000.000001/prompt.txt'
    assert body['input_files'] == [{'filename': 'a.txt', 'url': 'https://files.slack.com/a', 'size': 5}]
    assert body['request_epoch'] == 1700000000.5


def test_enqueue_claude_job_group_id_falls_back_to_thread(monkeypatch):
    cap = _patch_dispatch(monkeypatch)
    event = {'channel': 'C1', 'ts': '1700000000.000009'}  # user/bot_id 없음

    run_server._enqueue_claude_job(event, 'P', '1700000000.000009', '1700000000.000010', [], received_at=1.0)

    assert cap['send']['group_id'] == 'thread:1700000000.000009'


# ---------------------------------------------------------------------------
# 봇: 취소 마커 (StopTask 전에 cancel 마커를 먼저 쓴다)
# ---------------------------------------------------------------------------
def test_stop_task_writes_cancel_marker_before_stop(monkeypatch):
    seq = []
    monkeypatch.setattr(
        run_server,
        '_resolve_aws_credentials',
        lambda: {
            'AWS_ACCESS_KEY_ID': 'a',
            'AWS_SECRET_ACCESS_KEY': 's',
            'AWS_SESSION_TOKEN': 't',
        },
    )
    monkeypatch.setattr(run_server, '_s3_put_bytes', lambda bucket, key, body, creds: seq.append(('marker', key)))
    monkeypatch.setattr(run_server, '_ecs_stop_task', lambda arn, reason, creds: seq.append(('stop', arn)) or True)
    client = MagicMock()

    result = run_server._stop_task_fargate('arn:task/x', 'TS-MID', 'U_C', 'D1', '123.45', client)

    assert result == 'stopped'
    # 마커가 StopTask보다 먼저, 키는 jobs/{job_id}/cancel.
    assert seq == [('marker', 'jobs/TS-MID/cancel'), ('stop', 'arn:task/x')]


def test_stop_task_without_job_id_skips_marker(monkeypatch):
    seq = []
    monkeypatch.setattr(
        run_server,
        '_resolve_aws_credentials',
        lambda: {
            'AWS_ACCESS_KEY_ID': 'a',
            'AWS_SECRET_ACCESS_KEY': 's',
            'AWS_SESSION_TOKEN': 't',
        },
    )
    monkeypatch.setattr(run_server, '_s3_put_bytes', lambda *a, **k: seq.append('marker'))
    monkeypatch.setattr(run_server, '_ecs_stop_task', lambda arn, reason, creds: seq.append('stop') or True)

    run_server._stop_task_fargate('arn:task/x', None, 'U_C', 'D1', '123.45', MagicMock())

    assert seq == ['stop']  # 레거시(job_id 없음) → 마커 안 씀


# ---------------------------------------------------------------------------
# 워커: process_job (job dict + heartbeat + task_arn)
# ---------------------------------------------------------------------------
def test_process_job_uses_arg_and_calls_heartbeat(monkeypatch):
    monkeypatch.setenv('SLACK_BOT_TOKEN', 'xoxb-test')
    client = MagicMock()
    monkeypatch.setattr(sandbox_worker, 'WebClient', lambda token: client)
    monkeypatch.setattr(sandbox_worker.os, 'makedirs', lambda *a, **k: None)
    monkeypatch.setattr(sandbox_worker, 'download_prompt_from_s3', lambda key: 'p')
    monkeypatch.setattr(sandbox_worker, 'download_input_files', lambda files: [])
    monkeypatch.setattr(sandbox_worker, 'sync_memory_from_s3', lambda user: None)
    monkeypatch.setattr(sandbox_worker, 'sync_memory_to_s3', lambda user: None)
    monkeypatch.setattr(sandbox_worker, 'post_claude_markdown_to_thread', lambda *a, **k: None)
    monkeypatch.setattr(sandbox_worker, 'post_workspace_artifacts_to_thread', lambda *a, **k: None)
    monkeypatch.setattr(sandbox_worker, 'run_claude_direct', lambda *a, **k: (0, 'answer', None))
    # task_arn이 인자로 주어지면 메타데이터 조회를 하지 않아야 한다.
    monkeypatch.setattr(
        sandbox_worker,
        '_self_task_arn',
        lambda: (_ for _ in ()).throw(AssertionError('_self_task_arn should not be called')),
    )

    hb = MagicMock()
    job = {
        'job_id': 'TS-MID',
        'channel': 'D1',
        'thread_ts': '1700000000.1',
        'waiting_msg_ts': '1700000000.2',
        'user_id': 'U1',
        'prompt_s3_key': 'runs/x/prompt.txt',
        'input_files': [],
        'request_epoch': 1700000000.0,
    }
    sandbox_worker.process_job(job, heartbeat=hb, task_arn='arn:task/self')

    # 시작 즉시 진행 메시지(취소 버튼 부착) → 하트비트 1회 이상 호출.
    assert hb.call_count >= 1
    # 취소 버튼 value가 (task_arn, job_id)로 디코드된다.
    button_values = []
    for call in client.chat_update.call_args_list:
        for block in call.kwargs.get('blocks') or []:
            for el in block.get('elements', []):
                if el.get('action_id') == 'cancel_claude_run':
                    button_values.append(el['value'])
    assert button_values
    assert decode_cancel_value(button_values[0]) == ('arn:task/self', 'TS-MID')


# ---------------------------------------------------------------------------
# 워커: main_loop
# ---------------------------------------------------------------------------
class _StopLoop(Exception):
    """테스트에서 무한 루프를 끊기 위한 센티넬(큐 소진 시 raise)."""


def _patch_loop(monkeypatch, *, jobs, markers=None):
    """_sqs_receive_one을 jobs 시퀀스로, 마커/삭제/처리 호출을 캡처한다."""
    state = {'received': list(jobs), 'deleted': [], 'done_markers': [], 'processed': []}
    markers = markers or set()

    monkeypatch.setattr(sandbox_worker, 'TABRIS_QUEUE_URL', 'q')
    monkeypatch.setattr(sandbox_worker, '_self_task_arn', lambda: 'arn:self')
    monkeypatch.setattr(sandbox_worker, 'reset_workspace', lambda: None)

    def fake_receive(queue_url):
        if state['received']:
            return state['received'].pop(0)
        raise _StopLoop()

    monkeypatch.setattr(sandbox_worker, '_sqs_receive_one', fake_receive)
    monkeypatch.setattr(sandbox_worker, '_marker_exists', lambda job_id, name: (job_id, name) in markers)

    def fake_put_marker(job_id, name):
        state['done_markers'].append((job_id, name))

    monkeypatch.setattr(sandbox_worker, '_put_marker', fake_put_marker)
    monkeypatch.setattr(sandbox_worker, '_sqs_delete', lambda q, r: state['deleted'].append(r))
    return state


def test_main_loop_success_marks_done_and_retires_at_max_jobs(monkeypatch):
    state = _patch_loop(monkeypatch, jobs=[({'job_id': 'j1'}, 'r1')])
    monkeypatch.setattr(sandbox_worker, 'MAX_JOBS', 1)
    monkeypatch.setattr(sandbox_worker, 'process_job', lambda job, **k: state['processed'].append(job['job_id']))

    sandbox_worker.main_loop()  # MAX_JOBS=1 → 1건 처리 후 정상 은퇴(예외 없이 반환)

    assert state['processed'] == ['j1']
    assert state['done_markers'] == [('j1', 'done')]
    assert state['deleted'] == ['r1']


def test_main_loop_skips_when_cancel_marker_exists(monkeypatch):
    state = _patch_loop(monkeypatch, jobs=[({'job_id': 'j1'}, 'r1')], markers={('j1', 'cancel')})
    monkeypatch.setattr(sandbox_worker, 'MAX_JOBS', 5)
    monkeypatch.setattr(sandbox_worker, 'process_job', lambda job, **k: state['processed'].append(job['job_id']))

    with pytest.raises(_StopLoop):
        sandbox_worker.main_loop()

    assert state['processed'] == []  # 취소 마커 → 처리 안 함
    assert state['deleted'] == ['r1']  # 메시지는 삭제
    assert state['done_markers'] == []  # done 마커 안 씀


def test_main_loop_keeps_message_on_processing_exception(monkeypatch):
    state = _patch_loop(monkeypatch, jobs=[({'job_id': 'j1'}, 'r1')])
    monkeypatch.setattr(sandbox_worker, 'MAX_JOBS', 5)

    def boom(job, **k):
        raise RuntimeError('infra blip')

    monkeypatch.setattr(sandbox_worker, 'process_job', boom)

    with pytest.raises(_StopLoop):
        sandbox_worker.main_loop()

    assert state['deleted'] == []  # 삭제 안 함 → visibility 만료 후 재배달=재시도
    assert state['done_markers'] == []


def test_main_loop_discards_message_without_job_id(monkeypatch):
    state = _patch_loop(monkeypatch, jobs=[({}, 'r1')])
    monkeypatch.setattr(sandbox_worker, 'MAX_JOBS', 5)
    monkeypatch.setattr(sandbox_worker, 'process_job', lambda job, **k: state['processed'].append('x'))

    with pytest.raises(_StopLoop):
        sandbox_worker.main_loop()

    assert state['processed'] == []
    assert state['deleted'] == ['r1']  # job_id 없는 메시지는 폐기
