import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_markdown_parser import convert_markdown_to_slack_payloads

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

# Docker 컨테이너에 마운트할 Claude 워크스페이스 템플릿(스킬, CLAUDE.md 등).
WORKSPACE_TEMPLATE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    '_provisioning',
    'configuration',
    'docker',
    'workspace',
)

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


def _stream_json_progress_snippets(obj: dict) -> list[str]:
    """stream-json 한 레코드를 jq 예시와 같은 사용자 친화 한 줄들로 변환한다."""
    out: list[str] = []
    t = obj.get('type')
    if t == 'assistant':
        msg = obj.get('message') or {}
        content = msg.get('content')
        if not isinstance(content, list):
            return out
        for block in content:
            if not isinstance(block, dict):
                continue
            bt = block.get('type')
            if bt == 'tool_use':
                name = block.get('name') or '?'
                out.append(f'⚙️ [{name}] 실행 중...')
            elif bt == 'text':
                text = (block.get('text') or '').strip()
                if text:
                    if len(text) > 120:
                        text = f'{text[:117]}…'
                    out.append(f'💬 {text}')
    elif t == 'result':
        result = obj.get('result')
        if result is not None:
            s = str(result).strip()
            if len(s) > 200:
                s = f'{s[:197]}…'
            out.append(f'✅ 완료: {s}')
    return out


def _build_progress_text(
    elapsed_sec: int,
    stream_lines: list[str],
    stderr_lines: list[str],
    is_silent: bool = False,
) -> str:
    """중간 진행 상태를 Slack waiting 메시지에 표시할 텍스트를 만든다."""
    header = f'⏳ 처리 중... ({elapsed_sec}s 경과)'
    if is_silent:
        if stream_lines:
            tail = '\n'.join(stream_lines[-12:])
            return f'{header}\n(새 스트림 이벤트 대기 중)\n{tail}'
        return f'{header}, 새 로그 대기 중'
    if stream_lines:
        recent = '\n'.join(stream_lines[-12:])
        return f'{header}\n{recent}'
    if stderr_lines:
        recent = '\n'.join(stderr_lines[-12:])
        return f'{header}\n```{recent}```'
    return header


def run_claude(event: dict, context: str, request: str, progress_callback=None) -> str:
    thread_ts = event.get('thread_ts') or event.get('ts')
    msg_id = event.get('client_msg_id') or event.get('ts')
    is_dm = event.get('channel_type') == 'im'
    workspace = f'/tmp/claude-sandbox/{thread_ts}'
    os.makedirs(workspace, exist_ok=True)

    try:
        if not os.path.isdir(WORKSPACE_TEMPLATE):
            raise Exception('Workspace template missing: %s', WORKSPACE_TEMPLATE)
        shutil.copytree(WORKSPACE_TEMPLATE, workspace, dirs_exist_ok=True)

        ctx_file = os.path.join(workspace, 'context.md')
        with open(ctx_file, 'w', encoding='utf-8') as f:
            if context:
                f.write(f'## 이전 대화\n{context}\n\n')
            f.write(f'## 현재 요청\n{request}')

        with open(ctx_file, encoding='utf-8') as f:
            prompt = f.read()

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
            f'{workspace}:/workspace:ro',
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
            '--dangerously-skip-permissions',
            '--verbose',
            '--output-format',
            'stream-json',
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
        stream_display_lines: list[str] = []
        stderr_lines: list[str] = []
        final_result_obj: dict | None = None
        last_stream_notify_at = 0.0
        stream_notify_min_sec = 2.0
        stream_notify_force_min_sec = 1.0

        def _notify_progress(now_t: float, *, force: bool = False) -> None:
            """Slack 대기 메시지 갱신. 스트림 이벤트는 force로 더 자주 반영(과도한 API 호출 방지)."""
            nonlocal last_stream_notify_at
            if not progress_callback:
                return
            min_gap = stream_notify_force_min_sec if force else stream_notify_min_sec
            if (now_t - last_stream_notify_at) < min_gap:
                return
            last_stream_notify_at = now_t
            elapsed_i = int(now_t - started_at)
            progress_callback(
                _build_progress_text(elapsed_i, stream_display_lines, stderr_lines),
            )

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
        last_activity_at = started_at
        last_progress_at = started_at
        progress_interval_sec = 10
        silent_notice_sec = 60

        while True:
            now = time.time()
            elapsed = int(now - started_at)
            if elapsed > CLAUDE_TIMEOUT:
                process.kill()
                return f'⚠️ 작업 시간 초과 ({CLAUDE_TIMEOUT}초)'

            try:
                stream_name, chunk = output_queue.get(timeout=1)
                if stream_name == 'stdout':
                    stdout_chunks.append(chunk)
                    line = chunk.strip()
                    if line:
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            if len(line) > 300:
                                line = f'{line[:297]}…'
                            stderr_lines.append(line)
                            if len(stderr_lines) > 50:
                                stderr_lines[:] = stderr_lines[-50:]
                        else:
                            if obj.get('type') == 'result':
                                final_result_obj = obj
                            snippets = _stream_json_progress_snippets(obj)
                            if snippets:
                                stream_display_lines.extend(snippets)
                                if len(stream_display_lines) > 80:
                                    stream_display_lines[:] = stream_display_lines[-80:]
                                _notify_progress(now, force=True)
                else:
                    stderr_chunks.append(chunk)
                    line = chunk.strip()
                    if line:
                        stderr_lines.append(line)
                        if len(stderr_lines) > 50:
                            stderr_lines[:] = stderr_lines[-50:]

                last_activity_at = now

                if progress_callback and now - last_progress_at >= progress_interval_sec:
                    _notify_progress(now, force=False)
                    last_progress_at = now
            except queue.Empty:
                pass

            now = time.time()
            if (
                progress_callback
                and now - last_activity_at >= silent_notice_sec
                and now - last_progress_at >= progress_interval_sec
            ):
                progress_callback(
                    _build_progress_text(
                        int(now - started_at),
                        stream_display_lines,
                        stderr_lines,
                        is_silent=True,
                    ),
                )
                last_progress_at = now

            if process.poll() is not None and output_queue.empty():
                break

        stdout = ''.join(stdout_chunks)
        stderr = ''.join(stderr_chunks)

        if process.returncode != 0:
            logger.error('Claude exited with code %d: %s', process.returncode, stderr)
            return f'⚠️ 실행 오류:\n```{stderr[:300]}```'

        if final_result_obj is not None:
            output = final_result_obj
        else:
            output = None
            for ln in reversed([x.strip() for x in ''.join(stdout_chunks).splitlines() if x.strip()]):
                try:
                    cand = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if cand.get('type') == 'result':
                    output = cand
                    break
            if output is None:
                return stdout.strip() or '⚠️ 응답을 파싱할 수 없습니다.'

        if output.get('is_error'):
            err_txt = str(output.get('result') or '')
            return f'⚠️ 실행 오류:\n```{err_txt[:500]}```'

        usage = output.get('usage') or {}
        token_used = (
            (usage.get('input_tokens') or 0)
            + (usage.get('cache_creation_input_tokens') or 0)
            + (usage.get('cache_read_input_tokens') or 0)
            + (usage.get('output_tokens') or 0)
        )
        logger.info(
            '[RESPONSE] type=%s team_id=%s channel=%s thread_ts=%s user=%s msg_id=%s token_used=%d',
            'DM' if is_dm else 'mention',
            _normalize_slack_team_id(event.get('team_id') or event.get('team')),
            event.get('channel'),
            thread_ts,
            event.get('user'),
            msg_id,
            token_used,
        )
        return output.get('result') or output.get('content') or stdout
    except Exception as e:
        logger.exception('Unexpected error in run_claude')
        return f'⚠️ 오류: {e}'
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


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
