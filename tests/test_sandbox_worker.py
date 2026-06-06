"""sandbox_worker(Fargate 1회용 워커) 단위 테스트.

경계:
  - claude 실행(subprocess.Popen) → tests.helpers.install_claude_popen_mock
  - aws CLI(subprocess.run) → 패치
"""

import json
from unittest.mock import MagicMock

import sandbox_worker
from tests.helpers import install_claude_popen_mock


def _envelope(result_text: str) -> str:
    return json.dumps(
        {
            'type': 'result',
            'result': result_text,
            'total_cost_usd': 0.0123,
            'usage': {'input_tokens': 100, 'output_tokens': 20},
            'modelUsage': {'claude-opus-4-8[1m]': {'costUSD': 0.0123}},
        }
    )


def test_run_claude_direct_extracts_result(monkeypatch):
    install_claude_popen_mock(monkeypatch, stdout_text=_envelope('HELLO_WORLD'), returncode=0)
    rc, text, usage = sandbox_worker.run_claude_direct('prompt', None)
    assert rc == 0
    assert text == 'HELLO_WORLD'
    # usage가 비용 집계용으로 파싱되어 반환된다.
    assert usage['model'] == 'claude-opus-4-8[1m]'
    assert usage['total_cost_usd'] == 0.0123
    assert usage['input_tokens'] == 100
    assert usage['output_tokens'] == 20


def test_run_claude_direct_non_json_falls_back_to_raw(monkeypatch):
    install_claude_popen_mock(monkeypatch, stdout_text='plain answer', returncode=0)
    rc, text, _usage = sandbox_worker.run_claude_direct('prompt', None)
    assert rc == 0
    assert 'plain answer' in text


def test_run_claude_direct_nonzero_returns_error(monkeypatch):
    install_claude_popen_mock(monkeypatch, stdout_text='', stderr_text='boom!', returncode=1)
    rc, text, _usage = sandbox_worker.run_claude_direct('prompt', None)
    assert rc == 1
    assert '실행 오류' in text
    assert 'boom!' in text


def test_run_claude_direct_timeout(monkeypatch):
    monkeypatch.setattr(sandbox_worker, 'CLAUDE_TIMEOUT', -1)
    install_claude_popen_mock(monkeypatch, stdout_text=_envelope('never'), returncode=0)
    rc, text, _usage = sandbox_worker.run_claude_direct('prompt', None)
    assert rc == 1
    assert '시간 초과' in text


def test_download_prompt_from_s3(monkeypatch):
    monkeypatch.setattr(sandbox_worker, 'WORKSPACE_S3_BUCKET', 'wb')
    captured = {}

    def fake_run(cmd, **kwargs):
        captured['cmd'] = cmd
        return MagicMock(stdout=b'PROMPT_BODY')

    monkeypatch.setattr(sandbox_worker.subprocess, 'run', fake_run)
    out = sandbox_worker.download_prompt_from_s3('runs/TS/prompt.txt')

    assert out == 'PROMPT_BODY'
    assert 's3://wb/runs/TS/prompt.txt' in captured['cmd']
    assert '-' in captured['cmd']  # stdout으로 받음


def test_self_task_arn_reads_metadata(monkeypatch):
    """ECS 메타데이터 엔드포인트에서 자기 task ARN을 읽는다(취소 버튼 value용)."""
    monkeypatch.setenv('ECS_CONTAINER_METADATA_URI_V4', 'http://169.254.170.2/v4/abc')

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({'TaskARN': 'arn:aws:ecs:region:111:task/x/y'}).encode()

    monkeypatch.setattr(sandbox_worker.urllib.request, 'urlopen', lambda url, timeout=5: _Resp())
    assert sandbox_worker._self_task_arn() == 'arn:aws:ecs:region:111:task/x/y'


def test_self_task_arn_none_without_metadata(monkeypatch):
    monkeypatch.delenv('ECS_CONTAINER_METADATA_URI_V4', raising=False)
    assert sandbox_worker._self_task_arn() is None


def test_download_inputs_from_s3(monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox_worker, 'WORKSPACE_S3_BUCKET', 'wb')
    monkeypatch.setattr(sandbox_worker, 'INPUT_DIR', str(tmp_path))
    calls = []
    monkeypatch.setattr(sandbox_worker.subprocess, 'run', lambda cmd, **k: calls.append(cmd))

    saved = sandbox_worker.download_inputs_from_s3(
        [{'filename': 'a.txt', 's3_key': 'runs/TS/input/a.txt'}]
    )

    assert saved == ['a.txt']
    assert any('s3://wb/runs/TS/input/a.txt' in c for c in calls)


def test_process_job_emits_request_and_response_events(monkeypatch, event_log_lines):
    """워커가 토큰/비용 집계용 request·response JSON 이벤트를 남긴다(봇이 아닌 워커 책임)."""
    for k, v in {
        'TABRIS_JOB_ID': 'job-1',
        'TABRIS_SLACK_CHANNEL': 'D1',
        'TABRIS_SLACK_THREAD_TS': '1700000000.000001',
        'TABRIS_SLACK_WAITING_MSG_TS': '1700000000.000100',
        'TABRIS_SLACK_USER_ID': 'U1',
        'TABRIS_PROMPT_S3_KEY': 'runs/x/prompt.txt',
        'TABRIS_INPUT_FILES_JSON': '[]',
        'SLACK_BOT_TOKEN': 'xoxb-test',
        'TABRIS_REQUEST_EPOCH': '1700000000.0',
    }.items():
        monkeypatch.setenv(k, v)

    monkeypatch.setattr(sandbox_worker, 'WebClient', lambda token: MagicMock())
    monkeypatch.setattr(sandbox_worker.os, 'makedirs', lambda *a, **k: None)
    monkeypatch.setattr(sandbox_worker, 'download_prompt_from_s3', lambda key: 'p')
    monkeypatch.setattr(sandbox_worker, 'download_inputs_from_s3', lambda files: [])
    monkeypatch.setattr(sandbox_worker, 'sync_memory_from_s3', lambda user: None)
    monkeypatch.setattr(sandbox_worker, 'sync_memory_to_s3', lambda user: None)
    monkeypatch.setattr(sandbox_worker, '_self_task_arn', lambda: None)
    monkeypatch.setattr(sandbox_worker, 'post_claude_markdown_to_thread', lambda *a, **k: None)
    monkeypatch.setattr(sandbox_worker, 'post_workspace_artifacts_to_thread', lambda *a, **k: None)
    monkeypatch.setattr(
        sandbox_worker,
        'run_claude_direct',
        lambda *a, **k: (
            0,
            'answer',
            {'model': 'claude-opus-4-8[1m]', 'total_cost_usd': 0.0123, 'input_tokens': 100, 'output_tokens': 20},
        ),
    )

    sandbox_worker.process_job()

    by_evt = {e['evt']: e for e in event_log_lines()}
    assert 'request' in by_evt and 'response' in by_evt
    resp = by_evt['response']
    assert resp['job_id'] == 'job-1'
    assert resp['user'] == 'U1'
    assert resp['model'] == 'claude-opus-4-8[1m]'
    assert resp['total_cost_usd'] == 0.0123
    assert resp['input_tokens'] == 100
    assert resp['output_tokens'] == 20
    assert 'elapsed_sec' in resp
