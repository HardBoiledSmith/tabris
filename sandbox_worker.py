"""Fargate 샌드박스 진입점 — 단일 잡(1건)을 처리하고 종료하는 1회용 워커.

run_server.py(봇)가 ECS RunTask로 이 컨테이너를 띄울 때 job 파라미터·시크릿·설정을
환경변수(env override)로 주입한다. 워커는:

  1. S3에서 prompt + 입력 파일을 내려받고
  2. 사용자 memory를 S3에서 동기화한 뒤
  3. claude CLI를 직접 실행하며(컨테이너 자체가 샌드박스이므로 docker run 래퍼 불필요)
  4. 10초마다 Slack 대기 메시지를 갱신하고
  5. 결과/아티팩트를 Slack에 게시하고
  6. memory를 S3로 되돌린 뒤
  7. 프로세스를 종료한다(태스크 1회용).

AWS 자격증명은 ECS task IAM role이 자동 제공한다(aws CLI 기본 체인).
취소는 봇이 ecs StopTask로 이 컨테이너를 직접 종료하는 방식이라, 워커 내부에 취소 폴링이 없다.
진행률 메시지의 취소 버튼에는 ECS 메타데이터로 조회한 자기 task ARN을 담는다.
"""

import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

from slack_sdk import WebClient

from tabris_slack_utils import WORKSPACE_INPUT_SUBDIR
from tabris_slack_utils import _build_cancel_blocks
from tabris_slack_utils import _format_duration
from tabris_slack_utils import _progress_waiting_text
from tabris_slack_utils import post_claude_markdown_to_thread
from tabris_slack_utils import post_workspace_artifacts_to_thread

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('sandbox_worker')

# REQUEST/RESPONSE 이벤트를 사람용 로그와 별개로 JSON 한 줄로 남긴다.
# 실제 토큰/비용(usage)이 발생하는 곳이 샌드박스이고, Fargate stdout이 CloudWatch Logs로
# 수집되므로 여기서 남겨야 Logs Insights로 사용자별 토큰/비용을 집계할 수 있다.
# TABRIS_EVENT_LOG_PATH가 있으면 파일로(테스트용), 없으면 stdout으로 출력한다.
EVENT_LOG_PATH = os.environ.get('TABRIS_EVENT_LOG_PATH')
event_logger = logging.getLogger('tabris.events')
event_logger.setLevel(logging.INFO)
event_logger.propagate = False  # 일반 로그와 섞이지 않게 한다.
try:
    _event_handler = (
        logging.FileHandler(EVENT_LOG_PATH, encoding='utf-8') if EVENT_LOG_PATH else logging.StreamHandler(sys.stdout)
    )
    _event_handler.setFormatter(logging.Formatter('%(message)s'))  # 라인 전체가 순수 JSON이 되도록
    event_logger.addHandler(_event_handler)
except OSError:
    logger.warning('이벤트 JSON 로그를 열 수 없어 비활성화한다: %s', EVENT_LOG_PATH, exc_info=True)


def _log_event_json(payload: dict) -> None:
    """이벤트 한 건을 JSON 한 줄로 남긴다. 실패해도 본 처리 흐름에 영향 주지 않는다."""
    record = {'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), **payload}
    try:
        event_logger.info(json.dumps(record, ensure_ascii=False, separators=(',', ':')))
    except Exception:
        logger.warning('이벤트 JSON 로깅 실패', exc_info=True)


AWS_DEFAULT_REGION = os.environ.get('AWS_DEFAULT_REGION', 'ap-northeast-2')

# 컨테이너 내부 고정 경로. workdir이 /workspace이므로 Claude Code 프로젝트 ID는 -workspace.
WORKSPACE_DIR = '/workspace'
INPUT_DIR = os.path.join(WORKSPACE_DIR, WORKSPACE_INPUT_SUBDIR)
OUTPUT_DIR = os.path.join(WORKSPACE_DIR, 'output')
MEMORY_DIR = '/home/claude/.claude/projects/-workspace/memory'
CLAUDE_CONFIG = '/home/claude/.claude.json'

CLAUDE_TIMEOUT = int(os.environ.get('CLAUDE_TIMEOUT', '1800'))
WORKSPACE_S3_BUCKET = os.environ.get('WORKSPACE_S3_BUCKET', '')
MEMORY_S3_BUCKET = os.environ.get('MEMORY_S3_BUCKET', '')

PROGRESS_INTERVAL_SEC = 10


def _aws_cli() -> str:
    return shutil.which('aws') or '/usr/local/bin/aws'


def _self_task_arn() -> str | None:
    """ECS Task Metadata Endpoint로 자기 자신의 task ARN을 조회한다(취소 버튼 value용).

    Fargate는 ECS_CONTAINER_METADATA_URI_V4를 자동 주입한다. 취소 버튼이 이 ARN을 담아야
    봇의 StopTask가 동작한다. 로컬/메타데이터 부재 시 None.
    """
    uri = os.environ.get('ECS_CONTAINER_METADATA_URI_V4')
    if not uri:
        return None
    try:
        with urllib.request.urlopen(f'{uri}/task', timeout=5) as resp:
            return json.loads(resp.read().decode('utf-8')).get('TaskARN')
    except Exception:
        logger.warning('task ARN 메타데이터 조회 실패', exc_info=True)
        return None


def _primary_model(model_usage) -> str | None:
    if not isinstance(model_usage, dict) or not model_usage:
        return None
    return max(model_usage.items(), key=lambda kv: (kv[1] or {}).get('costUSD', 0))[0]


def download_prompt_from_s3(prompt_key: str) -> str:
    """aws CLI로 prompt 객체를 stdout으로 받아 텍스트로 반환한다(task role 자동 인증)."""
    result = subprocess.run(
        [_aws_cli(), 's3', 'cp', f's3://{WORKSPACE_S3_BUCKET}/{prompt_key}', '-'],
        capture_output=True,
        timeout=60,
        check=True,
    )
    return result.stdout.decode('utf-8')


def download_inputs_from_s3(input_files: list[dict]) -> list[str]:
    """INPUT_FILES_JSON 목록을 `/workspace/input/`에 내려받는다."""
    os.makedirs(INPUT_DIR, exist_ok=True)
    saved = []
    for f in input_files:
        filename = f.get('filename')
        s3_key = f.get('s3_key')
        if not filename or not s3_key:
            continue
        dest = os.path.join(INPUT_DIR, filename)
        try:
            subprocess.run(
                [_aws_cli(), 's3', 'cp', f's3://{WORKSPACE_S3_BUCKET}/{s3_key}', dest, '--only-show-errors'],
                check=True,
                timeout=120,
            )
            saved.append(filename)
        except Exception:
            logger.warning('Failed to download input %s', s3_key, exc_info=True)
    return saved


def sync_memory_from_s3(user_id: str) -> None:
    """S3 → 로컬 memory. task role 자격증명 자동 사용. MEMORY_S3_BUCKET 미설정이면 no-op."""
    if not MEMORY_S3_BUCKET or not user_id:
        return
    os.makedirs(MEMORY_DIR, exist_ok=True)
    s3_uri = f's3://{MEMORY_S3_BUCKET}/users/{user_id}/'
    logger.info('Syncing memory from S3: %s -> %s', s3_uri, MEMORY_DIR)
    subprocess.run(
        [_aws_cli(), 's3', 'sync', s3_uri, MEMORY_DIR, '--only-show-errors'],
        check=False,
        timeout=120,
    )


def sync_memory_to_s3(user_id: str) -> None:
    """로컬 memory → S3 미러(--delete). 로컬에 파일이 없으면 건너뛴다."""
    if not MEMORY_S3_BUCKET or not user_id:
        return
    has_files = any(files for _root, _dirs, files in os.walk(MEMORY_DIR))
    if not has_files:
        logger.warning('Skipping memory upload: empty memory dir for user %s', user_id)
        return
    s3_uri = f's3://{MEMORY_S3_BUCKET}/users/{user_id}/'
    logger.info('Syncing memory to S3: %s -> %s', MEMORY_DIR, s3_uri)
    subprocess.run(
        [_aws_cli(), 's3', 'sync', MEMORY_DIR, s3_uri, '--delete', '--only-show-errors'],
        check=False,
        timeout=120,
    )


def run_claude_direct(prompt: str, progress_callback) -> tuple[int, str, dict | None]:
    """claude CLI를 /workspace에서 직접 실행하고 (returncode, output_text, usage)를 반환한다.

    usage는 성공 시 {model, total_cost_usd, input_tokens, output_tokens} dict, 그 외엔 None.
    취소는 봇이 ecs StopTask로 컨테이너 자체를 종료하므로 워커 내부 취소 처리는 없다.
    """
    cmd = [
        'claude',
        '-p',
        prompt,
        '--mcp-config',
        CLAUDE_CONFIG,
        '--dangerously-skip-permissions',
        '--output-format',
        'json',
    ]
    process = subprocess.Popen(
        cmd,
        cwd=WORKSPACE_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    output_queue: queue.Queue = queue.Queue()
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    def _enqueue_stream(stream, stream_name: str) -> None:
        try:
            for line in iter(stream.readline, ''):
                output_queue.put((stream_name, line))
        finally:
            stream.close()

    threading.Thread(target=_enqueue_stream, args=(process.stdout, 'stdout'), daemon=True).start()
    threading.Thread(target=_enqueue_stream, args=(process.stderr, 'stderr'), daemon=True).start()

    started_at = time.time()
    last_progress_at = started_at

    while True:
        now = time.time()
        elapsed = int(now - started_at)
        if elapsed > CLAUDE_TIMEOUT:
            process.kill()
            return 1, f'⚠️ 작업 시간 초과 ({CLAUDE_TIMEOUT}초)', None

        try:
            stream_name, chunk = output_queue.get(timeout=1)
            if stream_name == 'stdout':
                stdout_chunks.append(chunk)
            else:
                stderr_chunks.append(chunk)
        except queue.Empty:
            pass

        now = time.time()
        elapsed = int(now - started_at)
        if progress_callback and now - last_progress_at >= PROGRESS_INTERVAL_SEC:
            progress_callback(_progress_waiting_text(elapsed, CLAUDE_TIMEOUT))
            last_progress_at = now

        if process.poll() is not None and output_queue.empty():
            break

    stdout = ''.join(stdout_chunks)
    stderr = ''.join(stderr_chunks)

    if process.returncode in (137, 143):
        return process.returncode, '🛑 사용자에 의해 취소되었습니다.', None
    if process.returncode != 0:
        logger.error('Claude exited with code %d: %s', process.returncode, stderr)
        return process.returncode, f'⚠️ 실행 오류:\n```{stderr[:300]}```', None

    raw_out = stdout.strip()
    if not raw_out:
        return process.returncode, '⚠️ 응답이 비어 있습니다.', None

    text_out = raw_out
    usage = None
    try:
        payload = json.loads(raw_out)
    except (json.JSONDecodeError, ValueError):
        logger.warning('Claude JSON 출력 파싱 실패; raw stdout으로 폴백한다.')
    else:
        text_out = (payload.get('result') or '').strip()
        u = payload.get('usage') or {}
        usage = {
            'model': _primary_model(payload.get('modelUsage')),
            'total_cost_usd': payload.get('total_cost_usd', payload.get('cost_usd')),
            'input_tokens': u.get('input_tokens'),
            'output_tokens': u.get('output_tokens'),
        }

    if not text_out:
        return process.returncode, '⚠️ 응답이 비어 있습니다.', None
    return process.returncode, text_out, usage


def process_job() -> None:
    job_id = os.environ['TABRIS_JOB_ID']
    channel = os.environ['TABRIS_SLACK_CHANNEL']
    thread_ts = os.environ['TABRIS_SLACK_THREAD_TS']
    waiting_msg_ts = os.environ['TABRIS_SLACK_WAITING_MSG_TS']
    slack_user_id = os.environ.get('TABRIS_SLACK_USER_ID', '')
    prompt_s3_key = os.environ['TABRIS_PROMPT_S3_KEY']
    input_files = json.loads(os.environ.get('TABRIS_INPUT_FILES_JSON', '[]'))
    # 봇이 메시지를 받은 시점(epoch). 실행 시간 = (워커 종료 시각 - 이 값)으로 디스패치/cold start 포함.
    request_epoch = float(os.environ.get('TABRIS_REQUEST_EPOCH') or 0)

    slack = WebClient(token=os.environ['SLACK_BOT_TOKEN'])

    os.makedirs(INPUT_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(MEMORY_DIR, exist_ok=True)

    # 진행률 업데이트 시 취소 버튼을 유지한다. value는 자기 task ARN(봇이 단 버튼과 동일).
    # ARN을 못 구하면(로컬 등) 버튼 없이 텍스트만 갱신한다.
    cancel_value = _self_task_arn()

    def _progress_callback(progress_text: str) -> None:
        try:
            blocks = _build_cancel_blocks(progress_text, cancel_value) if cancel_value else None
            slack.chat_update(
                channel=channel,
                ts=waiting_msg_ts,
                text=progress_text,
                blocks=blocks or [],
            )
        except Exception:
            logger.warning('Failed to post progress update', exc_info=True)

    _log_event_json(
        {
            'evt': 'request',
            'job_id': job_id,
            'channel': channel,
            'thread_ts': thread_ts,
            'user': slack_user_id,
        }
    )

    # 봇이 메시지를 받은 시점부터 측정(없으면 워커 시작 시점으로 폴백).
    run_start = request_epoch or time.time()
    prompt = download_prompt_from_s3(prompt_s3_key)
    download_inputs_from_s3(input_files)
    sync_memory_from_s3(slack_user_id)

    returncode, answer, usage = run_claude_direct(prompt, _progress_callback)

    if returncode in (137, 143):
        # 외부 신호로 종료된 경우(취소 등). 봇이 메시지를 갱신하므로 결과는 게시하지 않는다.
        logger.info('Job %s terminated (rc=%d); skipping result post.', job_id, returncode)
        return

    elapsed_sec = int(time.time() - run_start)
    elapsed_display = _format_duration(elapsed_sec)
    elapsed_suffix = None
    if returncode == 0:
        elapsed_suffix = [
            {
                'type': 'context',
                'elements': [{'type': 'mrkdwn', 'text': f'⏱️ 실행 시간: {elapsed_display}'}],
            }
        ]
        u = usage or {}
        _log_event_json(
            {
                'evt': 'response',
                'job_id': job_id,
                'channel': channel,
                'thread_ts': thread_ts,
                'user': slack_user_id,
                'model': u.get('model'),
                'total_cost_usd': u.get('total_cost_usd'),
                'input_tokens': u.get('input_tokens'),
                'output_tokens': u.get('output_tokens'),
                'elapsed_sec': elapsed_sec,
            }
        )
        try:
            sync_memory_to_s3(slack_user_id)
        except Exception:
            logger.exception('Failed to backup memory to S3 for user %s', slack_user_id)

    try:
        post_claude_markdown_to_thread(
            slack,
            channel=channel,
            thread_ts=thread_ts,
            markdown_text=answer,
            update_ts=waiting_msg_ts,
            suffix_blocks=elapsed_suffix,
        )
    except Exception:
        logger.exception('Block Kit post failed; falling back to plain chat_update')
        try:
            slack.chat_update(channel=channel, ts=waiting_msg_ts, text=answer[:3000], blocks=[])
        except Exception:
            logger.exception('Fallback chat_update also failed')

    post_workspace_artifacts_to_thread(slack, channel, thread_ts, WORKSPACE_DIR)


def main() -> None:
    try:
        process_job()
    except Exception:
        logger.exception('sandbox_worker failed')
        # 가능하면 대기 메시지에 오류를 남긴다.
        try:
            WebClient(token=os.environ['SLACK_BOT_TOKEN']).chat_update(
                channel=os.environ['TABRIS_SLACK_CHANNEL'],
                ts=os.environ['TABRIS_SLACK_WAITING_MSG_TS'],
                text='⚠️ 샌드박스 처리 중 오류가 발생했습니다.',
                blocks=[],
            )
        except Exception:
            pass
        raise


if __name__ == '__main__':
    main()
