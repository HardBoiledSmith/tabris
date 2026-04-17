import json
import logging
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_markdown_parser import convert_markdown_to_slack_payloads

from const import TEAM_ACCESS_DENIED_TEXT

sys.path.append('/etc/tabris')
from settings_local import ALLOWED_TEAM_IDS
from settings_local import ANTHROPIC_API_KEY
from settings_local import BOT_USER_ID
from settings_local import CLAUDE_TIMEOUT
from settings_local import DOCKER_IMAGE
from settings_local import JIRA_API_KEY
from settings_local import JIRA_API_USERNAME
from settings_local import MAX_WORKERS
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


@app.middleware
def log_all_requests(body, next):
    logger.info('RAW incoming body: %s', json.dumps(body, ensure_ascii=False)[:500])
    return next()


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
    """메시지가 발생한 Slack 워크스페이스(team)가 ALLOWED_TEAM_IDS에 포함되는지 본다.

    ALLOWED_TEAM_IDS가 비어 있으면 검사하지 않는다(기존·로컬 호환).
    team_id를 알 수 없으면 안전하게 거부한다.
    """

    allowed = ALLOWED_TEAM_IDS
    if not allowed:
        return True
    ids = {t for t in allowed if t}
    if not ids:
        return True
    tid = _normalize_slack_team_id(event.get('team_id') or event.get('team'))
    if not tid:
        logger.warning(
            'Slack event without team_id; denying. user=%s event_keys=%s',
            event.get('user'),
            list(event.keys()),
        )
        return False
    return tid in ids


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


def run_claude(context: str, request: str, thread_ts: str) -> str:
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
            '--read-only',
            '--tmpfs',
            '/tmp',
            '-v',
            f'{workspace}:/workspace:ro',
            '-e',
            f'ANTHROPIC_API_KEY={ANTHROPIC_API_KEY}',
            '-e',
            f'JIRA_API_KEY={JIRA_API_KEY}',
            '-e',
            f'JIRA_API_USERNAME={JIRA_API_USERNAME}',
            '-e',
            f'SLACK_BOT_TOKEN={SLACK_BOT_TOKEN}',
            '--workdir',
            '/workspace',
            DOCKER_IMAGE,
            'claude',
            '-p',
            prompt,
            '--dangerously-skip-permissions',
            '--output-format',
            'json',
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=CLAUDE_TIMEOUT)

        if result.returncode != 0:
            logger.error('Claude exited with code %d: %s', result.returncode, result.stderr)
            return f'⚠️ 실행 오류:\n```{result.stderr[:300]}```'

        try:
            output = json.loads(result.stdout)
            return output.get('result') or output.get('content') or result.stdout
        except json.JSONDecodeError:
            return result.stdout.strip() or '⚠️ 응답을 파싱할 수 없습니다.'

    except subprocess.TimeoutExpired:
        return f'⚠️ 작업 시간 초과 ({CLAUDE_TIMEOUT}초)'
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
    user_request = event.get('text', '').replace(f'<@{BOT_USER_ID}>', '').strip()

    msg_type = 'DM' if is_dm else 'mention'
    logger.info(
        '[%s] channel=%s thread_ts=%s team_id=%s user=%s text=%r',
        msg_type,
        channel,
        thread_ts,
        _normalize_slack_team_id(event.get('team_id') or event.get('team')),
        event.get('user'),
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
    answer = run_claude(context, user_request, thread_ts)

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
    logger.info('app_mention received: user=%s channel=%s', event.get('user'), event.get('channel'))
    _submit(event, client)


@app.event('message')
def on_dm(event, client):
    if event.get('channel_type') != 'im':
        return
    if event.get('subtype'):
        return
    if event.get('bot_id'):
        return
    logger.info('DM received: user=%s channel=%s', event.get('user'), event.get('channel'))
    _submit(event, client)


if __name__ == '__main__':
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
