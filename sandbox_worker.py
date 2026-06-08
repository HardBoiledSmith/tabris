"""Fargate 샌드박스 진입점 — 잡을 처리하는 워커(워밍 풀).

ECS Service가 띄운 상주 워커가 SQS FIFO 큐를 long-poll 하며 잡을 소비한다. 콜드 부팅
(ENI·이미지풀·인터프리터)을 유저 요청 임계경로에서 제거한다. 격리를 위해 잡 경계마다
workspace를 비우고, MAX_JOBS/MAX_LIFETIME마다 스스로 은퇴(exit 0)해 ECS가 새 태스크로 교체한다.

처리 흐름:
  1. S3에서 prompt + 입력 파일을 내려받고
  2. 사용자 memory를 S3에서 동기화한 뒤
  3. claude CLI를 직접 실행하며(컨테이너 자체가 샌드박스이므로 docker run 래퍼 불필요)
  4. 10초마다 Slack 대기 메시지를 갱신하고(풀 모드에선 동시에 SQS visibility를 연장=하트비트)
  5. 결과/아티팩트를 Slack에 게시하고
  6. memory를 S3로 되돌린다.

AWS 자격증명은 ECS task IAM role이 자동 제공한다(aws CLI 기본 체인).
취소는 봇이 ecs StopTask로 이 컨테이너를 종료하는 방식이라 워커 내부에 취소 폴링이 없다.
풀 모드에선 봇이 StopTask 전에 S3 `jobs/{job_id}/cancel` 마커를 먼저 써, 메시지가 재배달돼도
다른 워커가 이를 보고 스킵한다(좀비 재실행 방지). 진행률 메시지의 취소 버튼에는 자기 task ARN과
job_id를 담는다.
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
from tabris_slack_utils import encode_cancel_value
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
# Claude Code의 -workspace 프로젝트 디렉토리(대화 상태 + memory). 잡 경계마다 통째로 비워
# 유저 간 컨텍스트/메모리 오염을 막는다(워밍 풀 격리).
PROJECT_DIR = '/home/claude/.claude/projects/-workspace'
MEMORY_DIR = os.path.join(PROJECT_DIR, 'memory')
CLAUDE_CONFIG = '/home/claude/.claude.json'

CLAUDE_TIMEOUT = int(os.environ.get('CLAUDE_TIMEOUT', '1800'))
WORKSPACE_S3_BUCKET = os.environ.get('WORKSPACE_S3_BUCKET', '')
MEMORY_S3_BUCKET = os.environ.get('MEMORY_S3_BUCKET', '')

PROGRESS_INTERVAL_SEC = 10

# --- 워밍 풀(루프 모드) 설정 ---
# SQS FIFO를 소비하는 상주 루프의 대상 큐. 필수 — 비어 있으면 기동을 거부한다.
TABRIS_QUEUE_URL = os.environ.get('TABRIS_QUEUE_URL', '')
# 격리·위생을 위해 워커를 오래 재활용하지 않는다: 둘 중 먼저 도달하는 쪽에서 은퇴(exit 0)한다.
MAX_JOBS = int(os.environ.get('MAX_JOBS', '2'))
MAX_LIFETIME_SEC = int(os.environ.get('MAX_LIFETIME_SEC', '2700'))  # 45분
# 메시지 visibility: 처리 중 하트비트로 주기 연장. 거대 정적값 대신 모더릿 + 연장으로
# 워커 급사 시 빠른 재배달을 노린다.
SQS_VISIBILITY_TIMEOUT_SEC = int(os.environ.get('SQS_VISIBILITY_TIMEOUT_SEC', '360'))
SQS_WAIT_TIME_SEC = int(os.environ.get('SQS_WAIT_TIME_SEC', '20'))  # long-poll


def _aws_cli() -> str:
    return shutil.which('aws') or '/usr/local/bin/aws'


# ---------------------------------------------------------------------------
# SQS (aws CLI, boto3 미사용). task role 자격증명 자동 사용.
# ---------------------------------------------------------------------------
def _sqs_receive_one(queue_url: str) -> tuple[dict, str] | None:
    """FIFO 큐에서 메시지 1건을 long-poll로 받아 (body_dict, receipt_handle)를 반환한다.

    메시지 없음/오류면 None. Body JSON 파싱 실패 시 그 메시지를 삭제하고 None(독성 메시지 폐기).
    """
    result = subprocess.run(
        [
            _aws_cli(), 'sqs', 'receive-message',
            '--queue-url', queue_url,
            '--max-number-of-messages', '1',
            '--wait-time-seconds', str(SQS_WAIT_TIME_SEC),
            '--visibility-timeout', str(SQS_VISIBILITY_TIMEOUT_SEC),
            '--output', 'json',
        ],
        capture_output=True,
        text=True,
        timeout=SQS_WAIT_TIME_SEC + 15,
        check=False,
    )
    if result.returncode != 0:
        logger.error('sqs receive-message 실패: %s', result.stderr[:300])
        return None
    messages = (json.loads(result.stdout or '{}').get('Messages') or [])
    if not messages:
        return None
    msg = messages[0]
    receipt = msg['ReceiptHandle']
    try:
        body = json.loads(msg.get('Body') or '{}')
    except (json.JSONDecodeError, ValueError):
        logger.error('SQS 메시지 Body 파싱 실패 — 폐기한다.')
        _sqs_delete(queue_url, receipt)
        return None
    return body, receipt


def _sqs_delete(queue_url: str, receipt: str) -> None:
    subprocess.run(
        [_aws_cli(), 'sqs', 'delete-message', '--queue-url', queue_url, '--receipt-handle', receipt],
        capture_output=True,
        timeout=30,
        check=False,
    )


def _sqs_change_visibility(queue_url: str, receipt: str, timeout: int) -> None:
    """처리 중 메시지의 visibility를 연장한다(하트비트). 실패해도 본 흐름엔 영향 없다."""
    subprocess.run(
        [
            _aws_cli(), 'sqs', 'change-message-visibility',
            '--queue-url', queue_url,
            '--receipt-handle', receipt,
            '--visibility-timeout', str(timeout),
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )


# ---------------------------------------------------------------------------
# S3 멱등/취소 마커 (DynamoDB 미사용). 존재=상태. 강일관성(read-after-write)에 의존.
# ---------------------------------------------------------------------------
def _marker_key(job_id: str, name: str) -> str:
    return f'jobs/{job_id}/{name}'


def _marker_exists(job_id: str, name: str) -> bool:
    """jobs/{job_id}/{name} 마커 존재 여부. 버킷 미설정이면 항상 False(로컬/테스트)."""
    if not WORKSPACE_S3_BUCKET:
        return False
    result = subprocess.run(
        [_aws_cli(), 's3api', 'head-object', '--bucket', WORKSPACE_S3_BUCKET, '--key', _marker_key(job_id, name)],
        capture_output=True,
        timeout=30,
        check=False,
    )
    return result.returncode == 0


def _put_marker(job_id: str, name: str) -> None:
    """jobs/{job_id}/{name} 마커(빈 객체)를 쓴다. 버킷 미설정이면 no-op."""
    if not WORKSPACE_S3_BUCKET:
        return
    subprocess.run(
        [_aws_cli(), 's3api', 'put-object', '--bucket', WORKSPACE_S3_BUCKET, '--key', _marker_key(job_id, name)],
        capture_output=True,
        timeout=30,
        check=False,
    )


def reset_workspace() -> None:
    """잡 경계 위생: /workspace 내용과 Claude 프로젝트 디렉토리(대화 상태+memory)를 비운다.

    워밍 풀에서 한 워커가 여러 유저의 잡을 처리하므로, 잡 시작 전 매번 호출해 유저 간
    파일·대화·메모리 오염을 차단한다. 이후 memory는 해당 유저 기준으로 S3에서 다시 동기화된다.
    """
    for entry in os.listdir(WORKSPACE_DIR):
        full = os.path.join(WORKSPACE_DIR, entry)
        if os.path.isdir(full) and not os.path.islink(full):
            shutil.rmtree(full, ignore_errors=True)
        else:
            try:
                os.remove(full)
            except OSError:
                pass
    shutil.rmtree(PROJECT_DIR, ignore_errors=True)
    os.makedirs(INPUT_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(MEMORY_DIR, exist_ok=True)


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


def process_job(job: dict, *, heartbeat=None, task_arn: str | None = None) -> None:
    """잡 1건을 처리한다.

    heartbeat: 진행률 갱신(10초)마다 호출되는 콜백(SQS visibility 연장용).
    task_arn: 취소 버튼에 담을 자기 task ARN. None이면 메타데이터로 조회한다.
    """
    job_id = job['job_id']
    channel = job['channel']
    thread_ts = job['thread_ts']
    waiting_msg_ts = job['waiting_msg_ts']
    slack_user_id = job.get('user_id', '')
    prompt_s3_key = job['prompt_s3_key']
    input_files = job.get('input_files') or []
    # 봇이 메시지를 받은 시점(epoch). 실행 시간 = (워커 종료 시각 - 이 값)으로 디스패치/cold start 포함.
    request_epoch = float(job.get('request_epoch') or 0)

    slack = WebClient(token=os.environ['SLACK_BOT_TOKEN'])

    os.makedirs(INPUT_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(MEMORY_DIR, exist_ok=True)

    # 진행률 업데이트 시 취소 버튼을 유지한다. value는 자기 task ARN + job_id를 인코딩한 것.
    # ARN을 못 구하면(로컬 등) 버튼 없이 텍스트만 갱신한다.
    if task_arn is None:
        task_arn = _self_task_arn()
    cancel_value = encode_cancel_value(task_arn, job_id) if task_arn else None

    def _progress_callback(progress_text: str) -> None:
        if heartbeat is not None:
            try:
                heartbeat()
            except Exception:
                logger.warning('SQS visibility 연장(하트비트) 실패', exc_info=True)
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

    # 봇은 취소 버튼을 달지 못한다(어느 워커가 집을지 미정). 워커가 시작 즉시 한 번
    # 진행 메시지를 갱신해 자기 task ARN으로 취소 버튼을 노출한다.
    if cancel_value:
        _progress_callback(_progress_waiting_text(0, CLAUDE_TIMEOUT))

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


def main_loop() -> None:
    """워밍 풀 모드: SQS FIFO를 long-poll 하며 잡을 소비한다. MAX_JOBS/MAX_LIFETIME마다 은퇴한다.

    메시지 수명: 성공(=process_job이 정상 반환, 에러 메시지 게시 포함) 시에만 done 마커 후 삭제한다.
    process_job이 예외로 죽거나 워커가 급사하면 메시지를 남겨 두어 visibility 만료 후 재배달=재시도된다.
    """
    queue_url = TABRIS_QUEUE_URL
    task_arn = _self_task_arn()
    jobs_done = 0
    started = time.monotonic()
    logger.info(
        '워커 루프 시작 queue=%s task=%s max_jobs=%d max_lifetime=%ds',
        queue_url, task_arn, MAX_JOBS, MAX_LIFETIME_SEC,
    )
    while True:
        # 은퇴 판정은 잡 사이에서만 — 처리 중에는 절대 빠지지 않는다.
        if jobs_done >= MAX_JOBS:
            logger.info('MAX_JOBS(%d) 도달 — 은퇴(exit 0), ECS가 교체한다.', MAX_JOBS)
            return
        if time.monotonic() - started >= MAX_LIFETIME_SEC:
            logger.info('MAX_LIFETIME(%ds) 도달 — 은퇴(exit 0), ECS가 교체한다.', MAX_LIFETIME_SEC)
            return

        received = _sqs_receive_one(queue_url)
        if not received:
            continue
        job, receipt = received
        job_id = job.get('job_id') or ''
        if not job_id:
            logger.error('job_id 없는 메시지 — 폐기한다.')
            _sqs_delete(queue_url, receipt)
            continue

        # 종료 상태 가드: 취소됐거나 이미 완료(재배달)된 잡이면 스킵.
        if _marker_exists(job_id, 'cancel') or _marker_exists(job_id, 'done'):
            logger.info('job %s 종료상태 마커 존재 — 스킵하고 메시지 삭제.', job_id)
            _sqs_delete(queue_url, receipt)
            continue

        def _heartbeat(_receipt=receipt) -> None:
            _sqs_change_visibility(queue_url, _receipt, SQS_VISIBILITY_TIMEOUT_SEC)

        try:
            reset_workspace()
            process_job(job, heartbeat=_heartbeat, task_arn=task_arn)
        except Exception:
            # 인프라성 예외(S3/네트워크 등) — 삭제하지 않아 visibility 만료 후 재배달=재시도.
            logger.exception('job %s 처리 실패 — 메시지 보존(재배달).', job_id)
            continue

        # 정상 반환(성공/claude 에러 메시지 게시 포함) → done 마커 후 삭제.
        _put_marker(job_id, 'done')
        _sqs_delete(queue_url, receipt)
        jobs_done += 1
        logger.info('job %s 완료 — jobs_done=%d', job_id, jobs_done)


def main() -> None:
    if not TABRIS_QUEUE_URL:
        raise RuntimeError('TABRIS_QUEUE_URL must be set')
    main_loop()


if __name__ == '__main__':
    main()
