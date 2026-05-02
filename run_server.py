import logging
import os
import queue
import shutil
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_markdown_parser import convert_markdown_to_slack_payloads
from slack_sdk.errors import SlackApiError

from const import TEAM_ACCESS_DENIED_TEXT

sys.path.append('/etc/tabris')
from settings_local import ALLOWED_TEAM_ID
from settings_local import ANTHROPIC_API_KEY
from settings_local import AWS_ACCESS_KEY_ID
from settings_local import AWS_DEFAULT_REGION
from settings_local import AWS_SECRET_ACCESS_KEY
from settings_local import BOT_USER_ID
from settings_local import CLAUDE_TIMEOUT
from settings_local import DOCKER_IMAGE
from settings_local import JIRA_API_KEY
from settings_local import JIRA_API_USERNAME
from settings_local import MAX_WORKERS
from settings_local import NERV_MCP_TOKEN
from settings_local import SENTRY_AUTH_TOKEN
from settings_local import SLACK_APP_TOKEN
from settings_local import SLACK_BOT_TOKEN

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ARTIFACT_MAX_FILES = 10
ARTIFACT_MAX_BYTES_PER_FILE = 1_048_576  # 1 MiB
ARTIFACT_MAX_TOTAL_BYTES = 5_242_880  # 5 MiB
# Slack 파일 업로드는 이 하위만 스캔한다. 중간 산출은 컨테이너 `/tmp` 등에 두도록 CLAUDE.md로 안내한다.
WORKSPACE_OUTPUT_SUBDIR = 'output'

# Docker 이미지에 포함된 Claude 설정(MCP 등). 워크스페이스 마운트와 무관하다.
CLAUDE_CONFIG_IN_CONTAINER = '/home/claude/.claude.json'
# Dockerfile `useradd -u 1001 claude`와 동기화. 바인드 마운트는 호스트 inode 권한을 따른다.
CLAUDE_CONTAINER_UID = 1001
CLAUDE_CONTAINER_GID = 1001

app = App(token=SLACK_BOT_TOKEN)
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)


def build_context(messages: list, is_dm: bool) -> str:
    lines = []
    for msg in messages:
        text = msg.get('text', '').strip()
        if not text:
            continue

        is_bot_msg = bool(msg.get('bot_id'))
        is_mention = f'<@{BOT_USER_ID}>' in text

        if is_dm or is_bot_msg or is_mention:
            role = 'Assistant' if is_bot_msg else 'User'
            clean_text = text.replace(f'<@{BOT_USER_ID}>', '').strip()
            if clean_text:
                lines.append(f'{role}: {clean_text}')

    return '\n'.join(lines)


def _normalize_slack_team_id(raw):
    """이벤트에서 읽은 team 필드를 문자열 Team ID로 정규화한다."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return (raw.get('id') or raw.get('team_id') or '').strip() or None
    return str(raw).strip() or None


def is_allowed_slack_team(event: dict) -> bool:
    """메시지가 발생한 Slack 워크스페이스(team)가 ALLOWED_TEAM_ID와 일치하는지 본다.

    ALLOWED_TEAM_ID가 비어 있으면 검사하지 않는다(기존·로컬 호환).
    team_id를 알 수 없으면 안전하게 거부한다.
    """

    if not ALLOWED_TEAM_ID:
        return True
    tid = _normalize_slack_team_id(event.get('team_id') or event.get('team'))
    if not tid:
        logger.warning(
            'Slack event without team_id; denying. user=%s event_keys=%s',
            event.get('user'),
            list(event.keys()),
        )
        return False
    return tid == ALLOWED_TEAM_ID


def post_claude_markdown_to_thread(
    client,
    channel: str,
    thread_ts: str,
    markdown_text: str,
    update_ts: str,
) -> None:
    """Claude Code 마크다운을 Block Kit(markdown/table)으로 변환해 게시한다.

    테이블이 여러 개면 라이브러리가 메시지를 나누므로, 첫 덩어리는 대기 메시지를
    갱신하고 나머지는 같은 스레드에 연속 게시한다.
    """

    def _payload_kwargs(payload: dict) -> dict:
        kwargs: dict = {'text': payload['text']}
        blocks = payload.get('blocks') or []
        if blocks:
            kwargs['blocks'] = blocks
        return kwargs

    text = markdown_text if markdown_text is not None else ''
    payloads = list(
        convert_markdown_to_slack_payloads(
            text,
            preserve_visual_blank_lines=True,
        )
    )
    if not payloads:
        payloads = [{'text': text.strip() or ' ', 'blocks': []}]

    first = payloads[0]
    try:
        client.chat_update(
            channel=channel,
            ts=update_ts,
            **_payload_kwargs(first),
        )
    except Exception:
        logger.warning('chat_update failed, falling back to new message', exc_info=True)
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            **_payload_kwargs(first),
        )

    for extra in payloads[1:]:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            **_payload_kwargs(extra),
        )


def _collect_workspace_files_for_upload(workspace: str) -> list[tuple[str, bytes]]:
    """호스트 `{workspace}/output`만 스캔한다. 반환 경로는 output 기준 상대 경로(슬랙 파일명용).

    디렉터리·숨김 파일·심볼릭 링크·비일반 파일은 건너뛴다. ARTIFACT_MAX_* 한도를 적용한다.
    """

    max_files = ARTIFACT_MAX_FILES
    max_per_file = ARTIFACT_MAX_BYTES_PER_FILE
    max_total = ARTIFACT_MAX_TOTAL_BYTES

    output_dir = os.path.join(workspace, WORKSPACE_OUTPUT_SUBDIR)
    if not os.path.isdir(output_dir):
        return []

    output_dir = os.path.abspath(output_dir)
    out: list[tuple[str, bytes]] = []
    total_bytes = 0

    for dirpath, dirnames, filenames in os.walk(output_dir, topdown=True):
        dirnames.sort()
        filenames.sort()
        for name in filenames:
            if len(out) >= max_files:
                logger.warning(
                    'Artifact collection stopped: max file count %d reached',
                    max_files,
                )
                return out
            if name.startswith('.'):
                continue
            full_path = os.path.join(dirpath, name)
            rel_path = os.path.relpath(full_path, output_dir)
            rel_posix = rel_path.replace(os.sep, '/')
            try:
                if os.path.islink(full_path):
                    continue
                st_mode = os.stat(full_path).st_mode
                if not stat.S_ISREG(st_mode):
                    continue
                size = os.path.getsize(full_path)
            except OSError:
                logger.warning('Skipping unreadable artifact path %s', full_path, exc_info=True)
                continue
            if size > max_per_file:
                logger.warning(
                    'Skipping artifact %s: size %d exceeds per-file limit %d',
                    rel_posix,
                    size,
                    max_per_file,
                )
                continue
            if total_bytes + size > max_total:
                logger.warning(
                    'Artifact collection stopped: total byte limit %d would be exceeded',
                    max_total,
                )
                return out
            try:
                with open(full_path, 'rb') as artifact_fp:
                    blob = artifact_fp.read()
            except OSError:
                logger.warning('Failed to read artifact %s', full_path, exc_info=True)
                continue
            total_bytes += len(blob)
            out.append((rel_posix, blob))

    return out


def post_workspace_artifacts_to_thread(client, channel: str, thread_ts: str, workspace: str) -> None:
    """`{workspace}/output`만 스캔해 Slack에 파일로 올린다."""

    items = _collect_workspace_files_for_upload(workspace)
    for rel_name, content in items:
        safe_title = rel_name.replace('/', '_')
        initial_comment = f'아티팩트: {safe_title}'
        try:
            client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                filename=safe_title,
                content=content,
                title=safe_title,
                initial_comment=initial_comment,
            )
        except SlackApiError as exc:
            body = exc.response
            err = body.get('error') if body else None
            if err == 'missing_scope':
                logger.warning(
                    'files_upload_v2 skipped for %s: Slack app missing scope(s) %r '
                    '(e.g. add Bot scope "files:write" and reinstall the app)',
                    rel_name,
                    body.get('needed'),
                )
            else:
                logger.warning('files_upload_v2 failed for %s', rel_name, exc_info=True)
        except Exception:
            logger.warning('files_upload_v2 failed for %s', rel_name, exc_info=True)


def _progress_waiting_text(elapsed_sec: int, timeout_sec: int) -> str:
    """Claude 실행 중 Slack 대기 메시지: 경과/최대 대기(초)."""
    return f'⏳ 처리 중… {elapsed_sec}s/{timeout_sec}s'


def run_claude(event: dict, context: str, request: str, progress_callback=None) -> str:
    thread_ts = event.get('thread_ts') or event.get('ts')
    msg_id = event.get('client_msg_id') or event.get('ts')
    is_dm = event.get('channel_type') == 'im'
    workspace = f'/tmp/claude-sandbox/{thread_ts}'
    os.makedirs(workspace, exist_ok=True)
    output_dir = os.path.join(workspace, WORKSPACE_OUTPUT_SUBDIR)
    os.makedirs(output_dir, exist_ok=True)
    try:
        os.chown(workspace, CLAUDE_CONTAINER_UID, CLAUDE_CONTAINER_GID)
        os.chown(output_dir, CLAUDE_CONTAINER_UID, CLAUDE_CONTAINER_GID)
    except OSError:
        try:
            os.chmod(workspace, 0o777)
            os.chmod(output_dir, 0o777)
        except OSError:
            logger.warning(
                'Could not chown/chmod workspace %s for container UID',
                workspace,
                exc_info=True,
            )

    try:
        if context:
            prompt = f'## 이전 대화\n{context}\n\n## 현재 요청\n{request}'
        else:
            prompt = f'## 현재 요청\n{request}'

        cmd = [
            '/usr/bin/docker',
            'run',
            '--rm',
            '--memory',
            '512m',
            '--cpus',
            '1.0',
            '--cap-drop',
            'ALL',
            '--tmpfs',
            '/tmp',
            '-v',
            f'{workspace}:/workspace:rw',
            '-e',
            f'ANTHROPIC_API_KEY={ANTHROPIC_API_KEY}',
            '-e',
            f'AWS_ACCESS_KEY_ID={AWS_ACCESS_KEY_ID}',
            '-e',
            f'AWS_SECRET_ACCESS_KEY={AWS_SECRET_ACCESS_KEY}',
            '-e',
            f'AWS_DEFAULT_REGION={AWS_DEFAULT_REGION}',
            '-e',
            f'JIRA_API_KEY={JIRA_API_KEY}',
            '-e',
            f'JIRA_API_USERNAME={JIRA_API_USERNAME}',
            '-e',
            f'SLACK_BOT_TOKEN={SLACK_BOT_TOKEN}',
            '-e',
            f'SENTRY_AUTH_TOKEN={SENTRY_AUTH_TOKEN}',
            '-e',
            f'NERV_MCP_TOKEN={NERV_MCP_TOKEN}',
            '--workdir',
            '/workspace',
            DOCKER_IMAGE,
            'claude',
            '-p',
            prompt,
            '--mcp-config',
            CLAUDE_CONFIG_IN_CONTAINER,
            '--dangerously-skip-permissions',
            '--output-format',
            'text',
        ]

        process = subprocess.Popen(
            cmd,
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

        stdout_thread = threading.Thread(
            target=_enqueue_stream,
            args=(process.stdout, 'stdout'),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_enqueue_stream,
            args=(process.stderr, 'stderr'),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        started_at = time.time()
        last_progress_at = started_at
        progress_interval_sec = 10

        while True:
            now = time.time()
            elapsed = int(now - started_at)
            if elapsed > CLAUDE_TIMEOUT:
                process.kill()
                return f'⚠️ 작업 시간 초과 ({CLAUDE_TIMEOUT}초)'

            try:
                stream_name, chunk = output_queue.get(timeout=1)
                now = time.time()
                if stream_name == 'stdout':
                    stdout_chunks.append(chunk)
                else:
                    stderr_chunks.append(chunk)
            except queue.Empty:
                pass

            now = time.time()
            elapsed = int(now - started_at)
            if progress_callback and now - last_progress_at >= progress_interval_sec:
                progress_callback(_progress_waiting_text(elapsed, CLAUDE_TIMEOUT))
                last_progress_at = now

            if process.poll() is not None and output_queue.empty():
                break

        stdout = ''.join(stdout_chunks)
        stderr = ''.join(stderr_chunks)

        if process.returncode != 0:
            logger.error('Claude exited with code %d: %s', process.returncode, stderr)
            return f'⚠️ 실행 오류:\n```{stderr[:300]}```'

        text_out = stdout.strip()
        if not text_out:
            return '⚠️ 응답이 비어 있습니다.'
        output_length = len(text_out)
        logger.info(
            '[RESPONSE] type=%s team_id=%s channel=%s thread_ts=%s user=%s msg_id=%s output_length=%d',
            'DM' if is_dm else 'mention',
            _normalize_slack_team_id(event.get('team_id') or event.get('team')),
            event.get('channel'),
            thread_ts,
            event.get('user'),
            msg_id,
            output_length,
        )
        return text_out
    except Exception as e:
        logger.exception('Unexpected error in run_claude')
        return f'⚠️ 오류: {e}'


def handle_request(event: dict, client):
    try:
        channel = event['channel']
    except KeyError:
        logger.error("handle_request: event missing 'channel': %s", event)
        return

    is_dm = event.get('channel_type') == 'im'
    thread_ts = event.get('thread_ts') or event.get('ts')
    msg_id = event.get('client_msg_id') or event.get('ts')
    user_request = event.get('text', '').replace(f'<@{BOT_USER_ID}>', '').strip()

    msg_type = 'DM' if is_dm else 'mention'
    logger.info(
        '[REQUEST] type=%s team_id=%s channel=%s thread_ts=%s user=%s msg_id=%s text=%r',
        msg_type,
        _normalize_slack_team_id(event.get('team_id') or event.get('team')),
        channel,
        thread_ts,
        event.get('user'),
        msg_id,
        user_request,
    )

    if not is_allowed_slack_team(event):
        try:
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=TEAM_ACCESS_DENIED_TEXT)
        except Exception:
            logger.exception('Failed to post team access denied message')
        return

    if not user_request:
        return

    waiting_msg = client.chat_postMessage(channel=channel, thread_ts=thread_ts, text='⏳ 처리 중...')

    try:
        replies = client.conversations_replies(channel=channel, ts=thread_ts)
        history_msgs = replies.get('messages', [])[:-1]
    except Exception:
        logger.warning('Failed to fetch thread history', exc_info=True)
        history_msgs = []

    context = build_context(history_msgs, is_dm)

    def _progress_callback(progress_text: str) -> None:
        try:
            client.chat_update(channel=channel, ts=waiting_msg['ts'], text=progress_text)
        except Exception:
            logger.warning('Failed to post progress update', exc_info=True)

    workspace = f'/tmp/claude-sandbox/{thread_ts}'
    try:
        answer = run_claude(event, context, user_request, progress_callback=_progress_callback)

        try:
            post_claude_markdown_to_thread(
                client,
                channel=channel,
                thread_ts=thread_ts,
                markdown_text=answer,
                update_ts=waiting_msg['ts'],
            )
        except Exception:
            logger.exception('Block Kit post failed, falling back to plain text')
            try:
                client.chat_update(channel=channel, ts=waiting_msg['ts'], text=answer)
            except Exception:
                logger.warning('chat_update failed, falling back to new message', exc_info=True)
                client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=answer)

        post_workspace_artifacts_to_thread(client, channel, thread_ts, workspace)
    except Exception:
        logger.exception('Failed to process request', exc_info=True)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def _submit(event, client):
    future = executor.submit(handle_request, event, client)
    future.add_done_callback(
        lambda f: (
            logger.exception('handle_request raised an exception', exc_info=f.exception()) if f.exception() else None
        )
    )


@app.event('app_mention')
def on_mention(event, client):
    _submit(event, client)


@app.event('message')
def on_dm(event, client):
    if event.get('channel_type') != 'im':
        return
    if event.get('subtype'):
        return
    if event.get('bot_id'):
        return
    _submit(event, client)


if __name__ == '__main__':
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
