import base64
import json
import logging
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_markdown_parser import build_fallback_text_from_blocks
from slack_markdown_parser import convert_markdown_to_slack_blocks
from slack_sdk.errors import SlackApiError

from const import TEAM_ACCESS_DENIED_TEXT
from const import USER_ACCESS_DENIED_TEXT

sys.path.append('/etc/tabris')
from settings_local import ALLOWED_TEAM_IDS
from settings_local import ALLOWED_USER_IDS
from settings_local import ANTHROPIC_API_KEY
from settings_local import ARTIFACTS_BASE_URL
from settings_local import ARTIFACTS_S3_BUCKET
from settings_local import BOT_USER_ID
from settings_local import CLAUDE_TIMEOUT
from settings_local import DOCKER_IMAGE
from settings_local import DOCUMENTS_S3_BUCKET
from settings_local import GITHUB_PAT
from settings_local import JIRA_API_KEY
from settings_local import JIRA_API_USERNAME
from settings_local import MAX_WORKERS
from settings_local import MEMORY_S3_BUCKET
from settings_local import MEMORY_S3_SYNC_TIMEOUT
from settings_local import NERV_MCP_TOKEN
from settings_local import SENTRY_AUTH_TOKEN
from settings_local import SLACK_APP_TOKEN
from settings_local import SLACK_BOT_TOKEN

# Atlassian MCP Basic auth: echo -n "user:api_key" | base64
ATLASSIAN_ROVO_MCP_TOKEN = base64.b64encode(f'{JIRA_API_USERNAME}:{JIRA_API_KEY}'.encode()).decode('ascii')

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ARTIFACT_MAX_FILES = 10
ARTIFACT_MAX_BYTES_PER_FILE = 1_073_741_824  # 1 GiB
ARTIFACT_MAX_TOTAL_BYTES = 5_368_709_120  # 5 GiB
# Slack 파일 업로드는 이 하위만 스캔한다. 중간 산출은 컨테이너 `/tmp` 등에 두도록 CLAUDE.md로 안내한다.
WORKSPACE_OUTPUT_SUBDIR = 'output'
# 트리거 메시지의 Slack 첨부만 호스트가 받아 컨테이너 `/workspace/input/`에 둔다.
WORKSPACE_INPUT_SUBDIR = 'input'
# DM에서 파일 공유만 있는 메시지 subtype. 그 외 subtype은 무시한다.
SLACK_DM_ALLOWED_FILE_MESSAGE_SUBTYPES = frozenset({'file_share'})

# 봇이 멤버인 1:1 DM('im')과 그룹 DM('mpim')을 DM류로 동일 취급한다.
SLACK_DM_CHANNEL_TYPES = frozenset({'im', 'mpim'})


def _is_dm_channel(event: dict) -> bool:
    return event.get('channel_type') in SLACK_DM_CHANNEL_TYPES


# Docker 이미지에 포함된 Claude 설정(MCP 등). 워크스페이스 마운트와 무관하다.
CLAUDE_CONFIG_IN_CONTAINER = '/home/claude/.claude.json'
# Dockerfile `useradd -u 1001 claude`와 동기화. 바인드 마운트는 호스트 inode 권한을 따른다.
CLAUDE_CONTAINER_UID = 1001
CLAUDE_CONTAINER_GID = 1001

EC2_IMDS_BASE = 'http://169.254.169.254/latest'
EC2_IMDS_TOKEN_TTL_SECONDS = '21600'
AWS_DEFAULT_REGION = 'ap-northeast-2'
# IMDS 조회 실패 시(=로컬/Vagrant 개발환경) 폴백으로 사용할 aws CLI 프로파일.
# prod EC2에서는 IMDS가 성공하므로 이 경로는 실행되지 않는다.
AWS_FALLBACK_PROFILE = 'hbsmith-dv'

# 사용자별 Claude memory 호스트 경로. runs/는 스레드별 임시 workspace.
SANDBOX_ROOT = '/tmp/claude-sandbox'
SANDBOX_USERS_DIR = f'{SANDBOX_ROOT}/users'
SANDBOX_RUNS_DIR = f'{SANDBOX_ROOT}/runs'
WORKSPACE_MEMORY_SUBDIR = 'memory'
# workdir이 /workspace이므로 Claude Code 프로젝트 ID는 -workspace.
CLAUDE_MEMORY_CONTAINER_PATH = '/home/claude/.claude/projects/-workspace/memory'

# 사용자별 memory S3 sync 직렬화용 lock.
_user_memory_locks: dict[str, threading.Lock] = {}
_user_memory_locks_guard = threading.Lock()


def _user_memory_lock(user_id: str) -> threading.Lock:
    """user_id별 threading.Lock을 반환한다. 없으면 생성."""
    with _user_memory_locks_guard:
        if user_id not in _user_memory_locks:
            _user_memory_locks[user_id] = threading.Lock()
        return _user_memory_locks[user_id]


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


def _enrich_event_team_id_for_acl(event: dict, context) -> dict:
    """이벤트에 팀 식별자가 없으면 Bolt `context`의 워크스페이스 ID를 `team_id`로 넣은 사본을 돌려준다.

    `subtype: file_share` 등 일부 `message` 페이로드는 `team_id`/`team`이 빠져
    `is_allowed_slack_team`이 거부되는 경우가 있다. Socket Mode에서 Bolt가 채운
    `context.team_id`를 보강한다.
    """

    if _normalize_slack_team_id(event.get('team_id') or event.get('team')):
        return event
    if context is None:
        return event
    tid = context.team_id or context.actor_team_id
    if not tid:
        return event
    merged = dict(event)
    merged['team_id'] = tid
    return merged


def _parse_ids(raw) -> frozenset:
    """쉼표 구분 문자열 또는 (tuple/list/set) ID 모음을 정규화된 frozenset으로 만든다.

    프로비저닝은 'U1,U2' 형태 문자열로 주입하지만, 기존 튜플 설정도 그대로 수용한다.
    """
    if raw is None:
        return frozenset()
    items = raw if isinstance(raw, (list, tuple, set, frozenset)) else str(raw).split(',')
    return frozenset(s.strip() for s in items if s and str(s).strip())


# 운영 설정에 아직 없을 수 있으므로 방어적으로 import 한다(미반영 시에도 기동되게).
try:
    from settings_local import ALLOWED_ALL_USER_TEAM_IDS
except ImportError:
    ALLOWED_ALL_USER_TEAM_IDS = ''


_ALLOWED_TEAM_IDS = _parse_ids(ALLOWED_TEAM_IDS)
_ALLOWED_USER_IDS = _parse_ids(ALLOWED_USER_IDS)
_ALLOWED_ALL_USER_TEAM_IDS = _parse_ids(ALLOWED_ALL_USER_TEAM_IDS)
# 팀 게이트는 두 집합의 합집합 — 전원허용 팀을 ALLOWED_TEAM_IDS에 중복 기재할 필요 없다.
_ALL_ALLOWED_TEAMS = _ALLOWED_TEAM_IDS | _ALLOWED_ALL_USER_TEAM_IDS


def is_allowed_slack_team(event: dict) -> bool:
    """메시지가 발생한 Slack 워크스페이스(team)가 허용 팀에 포함되는지 본다.

    허용 팀(ALLOWED_TEAM_IDS ∪ ALLOWED_ALL_USER_TEAM_IDS)이 비어 있으면 검사하지 않는다(기존·로컬 호환).
    team_id를 알 수 없으면 안전하게 거부한다.
    """

    if not _ALL_ALLOWED_TEAMS:
        return True
    tid = _normalize_slack_team_id(event.get('team_id') or event.get('team'))
    if not tid:
        logger.warning(
            'Slack event without team_id; denying. user=%s event_keys=%s',
            event.get('user') or event.get('bot_id'),
            list(event.keys()),
        )
        return False
    return tid in _ALL_ALLOWED_TEAMS


def _normalize_slack_user_id(raw):
    """이벤트에서 읽은 user 필드를 문자열 User ID로 정규화한다."""
    if raw is None:
        return None
    return str(raw).strip() or None


def is_allowed_slack_user(event: dict) -> bool:
    """사람(user) 발신자가 허용되는지 본다. 봇은 handle_request에서 호출 전 제외된다.

    - 발신 팀이 ALLOWED_ALL_USER_TEAM_IDS면 그 팀 전원 허용.
    - 그 외에는 ALLOWED_USER_IDS로 검사한다(비어 있으면 검사 생략, 기존·로컬 호환).
    - user를 알 수 없으면 안전하게 거부한다.
    """

    tid = _normalize_slack_team_id(event.get('team_id') or event.get('team'))
    if tid and tid in _ALLOWED_ALL_USER_TEAM_IDS:
        return True
    if not _ALLOWED_USER_IDS:
        return True
    uid = _normalize_slack_user_id(event.get('user'))
    if not uid:
        logger.warning(
            'Slack event without user; denying. team=%s event_keys=%s',
            tid,
            list(event.keys()),
        )
        return False
    return uid in _ALLOWED_USER_IDS


def _is_self_event(event: dict, context=None) -> bool:
    """이벤트가 이 봇 자신이 보낸 메시지면 True. 무한루프(자기 응답에 재반응) 방지.

    Slack Bolt가 주입하는 context의 bot_id(B...)/bot_user_id(U...)와
    이벤트의 bot_id/user를 대조한다. context가 없으면 BOT_USER_ID로만 방어한다.
    """
    self_bot_id = context.get('bot_id') if context else None
    self_user_id = (context.get('bot_user_id') if context else None) or BOT_USER_ID

    ev_bot_id = event.get('bot_id')
    if self_bot_id and ev_bot_id and ev_bot_id == self_bot_id:
        return True

    ev_user_id = _normalize_slack_user_id(event.get('user'))
    if self_user_id and ev_user_id and ev_user_id == self_user_id:
        return True

    return False


def _post_access_denied(client, channel: str, thread_ts: str, text: str) -> None:
    try:
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
    except Exception:
        logger.exception('Failed to post access denied message')


SLACK_MAX_BLOCKS_PER_MESSAGE = 50
SLACK_MSG_REDIRECT_NOTICE = '메시지가 길어 새 글로 포스팅합니다.'
SLACK_MSG_FILE_NOTICE = '답변이 너무 길어져 파일로 첨부합니다.'


def _is_msg_too_long(exc: Exception) -> bool:
    return isinstance(exc, SlackApiError) and exc.response.get('error') == 'msg_too_long'


def _clear_waiting_for_redirect(client, channel: str, update_ts: str) -> None:
    """대기 메시지를 안내 평문으로 갱신하고 취소 버튼 블록을 제거한다."""
    try:
        client.chat_update(
            channel=channel,
            ts=update_ts,
            text=SLACK_MSG_REDIRECT_NOTICE,
            blocks=[],
        )
    except Exception:
        logger.warning('_clear_waiting_for_redirect failed', exc_info=True)


def _upload_answer_as_file(
    client, channel: str, thread_ts: str, content: str, filename: str = 'claude-response.md'
) -> None:
    """응답 본문을 파일로 업로드한다. 실패해도 로그만 남긴다."""
    try:
        client.files_upload_v2(
            channel=channel,
            thread_ts=thread_ts,
            filename=filename,
            content=content.encode('utf-8'),
            title=filename,
        )
    except SlackApiError as exc:
        err = exc.response.get('error') if exc.response else None
        if err == 'missing_scope':
            logger.warning(
                'files_upload_v2 skipped: missing scope %r (add Bot scope "files:write")',
                exc.response.get('needed'),
            )
        else:
            logger.warning('files_upload_v2 failed', exc_info=True)
    except Exception:
        logger.warning('files_upload_v2 failed', exc_info=True)


def _post_with_degrade(
    client,
    channel: str,
    thread_ts: str,
    *,
    text: str,
    blocks: list[dict],
    source_text: str,
) -> None:
    """3단계 degrade ladder로 스레드에 메시지를 게시한다.

    1단계: blocks + text (Block Kit)
    2단계: text-only (원문 plain)
    3단계: 안내 메시지 + 파일 첨부
    msg_too_long 이외의 오류는 그대로 raise한다.
    """
    # 1단계: blocks + text
    kwargs: dict = {'text': text}
    if blocks:
        kwargs['blocks'] = blocks
    try:
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, **kwargs)
        return
    except Exception as exc:
        if not _is_msg_too_long(exc):
            logger.warning('chat_postMessage failed (stage 1)', exc_info=True)
            raise
        logger.warning(
            'chat_postMessage msg_too_long (stage 1), falling back to text-only. text_len=%d blocks=%d source_len=%d',
            len(text),
            len(blocks),
            len(source_text),
        )

    # 2단계: text-only (원문)
    try:
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=source_text)
        return
    except Exception as exc:
        if not _is_msg_too_long(exc):
            logger.warning('chat_postMessage failed (stage 2)', exc_info=True)
            raise
        logger.warning(
            'chat_postMessage msg_too_long (stage 2), falling back to file upload. source_len=%d',
            len(source_text),
        )

    # 3단계: 파일 첨부
    try:
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=SLACK_MSG_FILE_NOTICE)
    except Exception:
        logger.warning('chat_postMessage notice for file upload failed', exc_info=True)
    _upload_answer_as_file(client, channel, thread_ts, source_text)


def _update_waiting_with_degrade(
    client,
    channel: str,
    thread_ts: str,
    update_ts: str,
    *,
    text: str,
    blocks: list[dict],
    source_text: str,
) -> None:
    """대기 메시지를 갱신하고, msg_too_long 시 안내 stub 후 _post_with_degrade로 넘긴다."""
    kwargs: dict = {'text': text}
    if blocks:
        kwargs['blocks'] = blocks
    try:
        client.chat_update(channel=channel, ts=update_ts, **kwargs)
        return
    except Exception as exc:
        if _is_msg_too_long(exc):
            logger.warning(
                'chat_update msg_too_long, redirecting to new message. text_len=%d blocks=%d source_len=%d',
                len(text),
                len(blocks),
                len(source_text),
            )
            _clear_waiting_for_redirect(client, channel, update_ts)
        else:
            logger.warning('chat_update failed, falling back to new message', exc_info=True)
    _post_with_degrade(client, channel, thread_ts, text=text, blocks=blocks, source_text=source_text)


def post_claude_markdown_to_thread(
    client,
    channel: str,
    thread_ts: str,
    markdown_text: str,
    update_ts: str,
    suffix_blocks: list[dict] | None = None,
) -> None:
    """Claude Code 마크다운을 Block Kit(markdown/table)으로 변환해 단일 메시지로 게시한다.

    50블록 초과 시에만 메시지를 나눈다. 첫 덩어리는 대기 메시지를 갱신하고
    나머지는 같은 스레드에 연속 게시한다.
    suffix_blocks가 주어지면 마지막 메시지의 블록 끝에 추가한다.
    msg_too_long 시 3단계 degrade(blocks → text-only → 파일)로 fallback한다.
    """
    text = markdown_text if markdown_text is not None else ''
    all_blocks = convert_markdown_to_slack_blocks(text, preserve_visual_blank_lines=True)

    if not all_blocks:
        all_blocks = []

    if suffix_blocks:
        all_blocks.extend(suffix_blocks)

    messages: list[dict] = []
    for i in range(0, max(len(all_blocks), 1), SLACK_MAX_BLOCKS_PER_MESSAGE):
        chunk = all_blocks[i : i + SLACK_MAX_BLOCKS_PER_MESSAGE]
        fallback = build_fallback_text_from_blocks(chunk).strip() if chunk else ''
        messages.append(
            {
                'text': fallback or text.strip() or ' ',
                'blocks': chunk,
            }
        )

    first = messages[0]
    _update_waiting_with_degrade(
        client,
        channel,
        thread_ts,
        update_ts,
        text=first['text'],
        blocks=first['blocks'],
        source_text=text,
    )

    for extra in messages[1:]:
        _post_with_degrade(
            client,
            channel,
            thread_ts,
            text=extra['text'],
            blocks=extra['blocks'],
            source_text=text,
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


def _sanitize_slack_attachment_filename(raw_name: str) -> str:
    """Slack 첨부 파일명을 단일 경로 세그먼트로 정규화한다(경로·제어문자 제거)."""

    name = os.path.basename(str(raw_name or 'attached').replace('\\', '/'))
    name = re.sub(r'[\x00-\x1f]', '', name).strip()
    name = name.replace('/', '_')
    if not name or name in {'.', '..'}:
        name = 'attached'
    max_len = 200
    if len(name) > max_len:
        root, ext = os.path.splitext(name)
        name = root[: max_len - len(ext)] + ext
    return name or 'attached'


def _slack_private_file_url(file_obj: dict) -> str | None:
    """Slack file 객체에서 Bot 토큰으로 GET 가능한 비공개 URL을 고른다."""

    return file_obj.get('url_private_download') or file_obj.get('url_private')


def _read_slack_private_url(url: str, bot_token: str, max_bytes: int) -> bytes | None:
    """Slack `url_private*` GET. `max_bytes`를 넘기면 None."""

    req = urllib.request.Request(
        url,
        headers={'Authorization': f'Bearer {bot_token}'},
        method='GET',
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            cl = resp.headers.get('Content-Length')
            if cl is not None:
                try:
                    if int(cl) > max_bytes:
                        logger.warning(
                            'Skipping Slack attachment: Content-Length %s exceeds %d',
                            cl,
                            max_bytes,
                        )
                        return None
                except ValueError:
                    pass
            data = resp.read(max_bytes + 1)
    except (OSError, urllib.error.HTTPError, urllib.error.URLError) as exc:
        logger.warning('Slack attachment download failed: %s', exc, exc_info=True)
        return None
    if len(data) > max_bytes:
        logger.warning('Slack attachment exceeds max_bytes after read')
        return None
    return data


def download_slack_message_files_to_input(event: dict, workspace: str, bot_token: str) -> list[str]:
    """트리거 메시지 `event['files']`만 내려받아 `{workspace}/input/`에 저장한다.

    반환: 워크스페이스 기준 POSIX 상대 경로(예: `input/a.txt`). ARTIFACT_MAX_* 한도를 적용한다.
    """

    files = event.get('files') or []
    if not files:
        return []

    input_dir = os.path.join(workspace, WORKSPACE_INPUT_SUBDIR)
    os.makedirs(input_dir, exist_ok=True)

    saved: list[str] = []
    used_names: set[str] = set()
    total_bytes = 0
    max_files = ARTIFACT_MAX_FILES
    max_per = ARTIFACT_MAX_BYTES_PER_FILE
    max_total = ARTIFACT_MAX_TOTAL_BYTES

    for file_obj in files:
        if len(saved) >= max_files:
            logger.warning('Slack input files: max file count %d reached', max_files)
            break
        if not isinstance(file_obj, dict):
            continue
        url = _slack_private_file_url(file_obj)
        if not url:
            logger.warning(
                'Slack file object has no private URL; skipping id=%r',
                file_obj.get('id'),
            )
            continue
        size_hint = file_obj.get('size')
        if size_hint is not None:
            try:
                if int(size_hint) > max_per:
                    logger.warning(
                        'Skipping Slack attachment %r: declared size exceeds per-file limit',
                        file_obj.get('name'),
                    )
                    continue
            except (TypeError, ValueError):
                pass
        blob = _read_slack_private_url(url, bot_token, max_per)
        if not blob:
            continue
        if total_bytes + len(blob) > max_total:
            logger.warning('Slack input files: total byte limit %d reached', max_total)
            break
        base = _sanitize_slack_attachment_filename(str(file_obj.get('name') or 'attached'))
        candidate = base
        n = 2
        while candidate in used_names:
            root, ext = os.path.splitext(base)
            candidate = f'{root}_{n}{ext}'
            n += 1
        used_names.add(candidate)
        dest_path = os.path.join(input_dir, candidate)
        try:
            with open(dest_path, 'wb') as out_fp:
                out_fp.write(blob)
        except OSError:
            logger.warning('Failed to write Slack input file %s', dest_path, exc_info=True)
            continue
        total_bytes += len(blob)
        saved.append(f'{WORKSPACE_INPUT_SUBDIR}/{candidate}'.replace(os.sep, '/'))

    return saved


def _format_duration(seconds: int) -> str:
    """초를 읽기 쉬운 한국어 시간 표현으로 변환한다. 0인 단위는 생략한다."""
    m, s = divmod(seconds, 60)
    if m and s:
        return f'{m}분 {s}초'
    if m:
        return f'{m}분'
    return f'{s}초'


def _progress_waiting_text(elapsed_sec: int, timeout_sec: int) -> str:
    """Claude 실행 중 Slack 대기 메시지: 경과/최대 대기(분초)."""
    return f'⏳ 처리 중… ({_format_duration(elapsed_sec)} / {_format_duration(timeout_sec)})'


def _docker_container_name(thread_ts: str) -> str:
    """thread_ts로부터 Docker --name에 쓸 고유 컨테이너 이름을 만든다."""
    return f'claude-{thread_ts}'.replace('.', '-')


def _build_cancel_blocks(text: str, container_name: str) -> list[dict]:
    """대기/진행 메시지에 취소 버튼을 포함한 Block Kit blocks를 만든다."""
    return [
        {
            'type': 'section',
            'text': {'type': 'mrkdwn', 'text': text},
        },
        {
            'type': 'actions',
            'elements': [
                {
                    'type': 'button',
                    'text': {'type': 'plain_text', 'text': '🛑 실행 취소'},
                    'action_id': 'cancel_claude_run',
                    'value': container_name,
                }
            ],
        },
    ]


def fetch_ec2_instance_role_credentials() -> dict[str, str]:
    """EC2 IMDSv2로 인스턴스 프로파일(IAM role)의 임시 AWS 자격증명을 조회한다."""

    token_req = urllib.request.Request(
        f'{EC2_IMDS_BASE}/api/token',
        method='PUT',
        headers={'X-aws-ec2-metadata-token-ttl-seconds': EC2_IMDS_TOKEN_TTL_SECONDS},
    )
    with urllib.request.urlopen(token_req, timeout=5) as token_resp:
        imds_token = token_resp.read().decode('utf-8').strip()

    imds_headers = {'X-aws-ec2-metadata-token': imds_token}
    role_req = urllib.request.Request(
        f'{EC2_IMDS_BASE}/meta-data/iam/security-credentials/',
        headers=imds_headers,
        method='GET',
    )
    with urllib.request.urlopen(role_req, timeout=5) as role_resp:
        role_name = role_resp.read().decode('utf-8').strip().splitlines()[0].strip()
    if not role_name:
        raise RuntimeError('EC2 IMDS: IAM role name is empty')

    creds_req = urllib.request.Request(
        f'{EC2_IMDS_BASE}/meta-data/iam/security-credentials/{role_name}',
        headers=imds_headers,
        method='GET',
    )
    with urllib.request.urlopen(creds_req, timeout=5) as creds_resp:
        payload = json.loads(creds_resp.read().decode('utf-8'))

    if payload.get('Code') != 'Success':
        raise RuntimeError(f'EC2 IMDS: credential response Code={payload.get("Code")!r}')

    return {
        'AWS_ACCESS_KEY_ID': payload['AccessKeyId'],
        'AWS_SECRET_ACCESS_KEY': payload['SecretAccessKey'],
        'AWS_SESSION_TOKEN': payload['Token'],
    }


def fetch_credentials_via_aws_profile(profile: str) -> dict[str, str]:
    """aws CLI 프로파일로 임시 AWS 자격증명을 조회한다(로컬/Vagrant 개발환경 폴백).

    `aws configure export-credentials`는 CLI의 자격증명 resolution(SSO 포함)을 그대로 태운다.
    SSO 세션이 만료된 경우 `aws sso login --profile <profile>`이 필요하다.
    """
    result = subprocess.run(
        [_aws_cli_executable(), 'configure', 'export-credentials', '--profile', profile, '--format', 'process'],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f'aws export-credentials 실패(profile={profile}). '
            f'SSO 만료일 수 있음 → `aws sso login --profile {profile}` 확인 필요. {result.stderr.strip()[:300]}'
        )
    payload = json.loads(result.stdout)
    return {
        'AWS_ACCESS_KEY_ID': payload['AccessKeyId'],
        'AWS_SECRET_ACCESS_KEY': payload['SecretAccessKey'],
        'AWS_SESSION_TOKEN': payload['SessionToken'],
    }


def _aws_cli_executable() -> str:
    """호스트에 설치된 aws CLI 경로를 반환한다."""
    path = shutil.which('aws')
    if path:
        return path
    return '/usr/bin/aws'


def _memory_dir_has_files(memory_dir: str) -> bool:
    """memory_dir 아래 일반 파일이 하나라도 있으면 True."""
    for _root, _dirs, files in os.walk(memory_dir):
        if files:
            return True
    return False


def _aws_s3_sync(src: str, dst: str, creds: dict, *, delete: bool = False) -> None:
    """aws s3 sync로 src → dst를 동기화한다. delete=True이면 dst에서 src에 없는 객체를 제거한다."""
    env = os.environ.copy()
    env['AWS_ACCESS_KEY_ID'] = creds['AWS_ACCESS_KEY_ID']
    env['AWS_SECRET_ACCESS_KEY'] = creds['AWS_SECRET_ACCESS_KEY']
    env['AWS_SESSION_TOKEN'] = creds['AWS_SESSION_TOKEN']
    env['AWS_DEFAULT_REGION'] = AWS_DEFAULT_REGION
    cmd = [_aws_cli_executable(), 's3', 'sync', src, dst, '--only-show-errors']
    if delete:
        cmd.append('--delete')
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=MEMORY_S3_SYNC_TIMEOUT, check=False)
    if result.returncode != 0:
        logger.error('aws s3 sync failed: src=%s dst=%s stderr=%s', src, dst, result.stderr[:500])
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)


def sync_memory_from_s3(user_id: str, memory_dir: str, creds: dict) -> None:
    """S3 → 로컬 memory 디렉터리로 동기화한다. MEMORY_S3_BUCKET 미설정이면 no-op."""
    if not MEMORY_S3_BUCKET:
        return
    s3_uri = f's3://{MEMORY_S3_BUCKET}/users/{user_id}/'
    logger.info('Syncing memory from S3: %s -> %s', s3_uri, memory_dir)
    _aws_s3_sync(s3_uri, memory_dir, creds)


def sync_memory_to_s3(user_id: str, memory_dir: str, creds: dict) -> None:
    """로컬 memory → S3 미러 동기화. MEMORY_S3_BUCKET 미설정이면 no-op. 로컬에 없는 S3 객체는 --delete로 제거한다."""
    if not MEMORY_S3_BUCKET:
        return
    if not _memory_dir_has_files(memory_dir):
        logger.warning('Skipping memory upload: empty memory_dir for user %s', user_id)
        return
    s3_uri = f's3://{MEMORY_S3_BUCKET}/users/{user_id}/'
    logger.info('Syncing memory to S3: %s -> %s', memory_dir, s3_uri)
    _aws_s3_sync(memory_dir, s3_uri, creds, delete=True)


def run_claude(event: dict, context: str, request: str, progress_callback=None) -> tuple[bool, str]:
    thread_ts = event.get('thread_ts') or event.get('ts')
    msg_id = event.get('client_msg_id') or event.get('ts')
    is_dm = _is_dm_channel(event)
    slack_user_id = event.get('user') or event.get('bot_id')
    if not slack_user_id:
        logger.error('run_claude: event missing user ID and bot_id')
        return False, '⚠️ Slack user ID 없음: 요청을 처리할 수 없습니다.'

    run_workspace = f'{SANDBOX_RUNS_DIR}/{thread_ts}'
    os.makedirs(run_workspace, exist_ok=True)
    output_dir = os.path.join(run_workspace, WORKSPACE_OUTPUT_SUBDIR)
    input_dir = os.path.join(run_workspace, WORKSPACE_INPUT_SUBDIR)
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(input_dir, exist_ok=True)

    memory_dir = f'{SANDBOX_USERS_DIR}/{slack_user_id}/{WORKSPACE_MEMORY_SUBDIR}'
    os.makedirs(memory_dir, exist_ok=True)

    for path in (run_workspace, output_dir, input_dir, memory_dir):
        try:
            os.chown(path, CLAUDE_CONTAINER_UID, CLAUDE_CONTAINER_GID)
        except OSError:
            try:
                os.chmod(path, 0o777)
            except OSError:
                logger.warning(
                    'Could not chown/chmod %s for container UID',
                    path,
                    exc_info=True,
                )

    try:
        input_relpaths = download_slack_message_files_to_input(event, run_workspace, SLACK_BOT_TOKEN)
        if input_relpaths:
            lines = '\n'.join(f'- `/workspace/{p}`' for p in input_relpaths)
            input_note = (
                '## 이번 Slack 메시지 첨부\n'
                '아래 파일은 이번 사용자 메시지에 붙은 첨부를 봇이 복사해 둔 것이다. '
                '필요하면 읽어서 활용할 것.\n'
                f'{lines}\n\n'
            )
        else:
            input_note = ''
        if context:
            prompt = f'{input_note}## 이전 대화\n{context}\n\n## 현재 요청\n{request}'
        else:
            prompt = f'{input_note}## 현재 요청\n{request}'

        try:
            aws_creds = fetch_ec2_instance_role_credentials()
        except (OSError, urllib.error.HTTPError, urllib.error.URLError, RuntimeError, KeyError) as exc:
            # IMDS 실패 = 로컬/Vagrant 개발환경. aws CLI 프로파일로 폴백한다.
            logger.warning('IMDS 자격증명 실패, 프로파일 폴백 시도(%s): %s', AWS_FALLBACK_PROFILE, exc)
            try:
                aws_creds = fetch_credentials_via_aws_profile(AWS_FALLBACK_PROFILE)
            except (subprocess.SubprocessError, OSError, RuntimeError, KeyError, ValueError) as fb_exc:
                logger.exception('Failed to fetch AWS credentials from both IMDS and aws profile')
                return False, f'⚠️ AWS 자격증명 조회 실패(IMDS·프로파일 모두): {fb_exc}'

        with _user_memory_lock(slack_user_id):
            try:
                sync_memory_from_s3(slack_user_id, memory_dir, aws_creds)
            except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired) as exc:
                logger.exception('Failed to restore memory from S3 for user %s', slack_user_id)
                return False, f'⚠️ memory 복원(S3) 실패: {exc}'

            returncode, text_out = _run_claude_docker(
                run_workspace, memory_dir, aws_creds, prompt, progress_callback, thread_ts, event, msg_id, is_dm
            )

            if returncode == 0:
                try:
                    sync_memory_to_s3(slack_user_id, memory_dir, aws_creds)
                except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
                    logger.exception('Failed to backup memory to S3 for user %s', slack_user_id)
                    # Claude 응답은 그대로 반환하고 경고만 로깅한다.

        return returncode == 0, text_out

    except Exception as e:
        logger.exception('Unexpected error in run_claude')
        return False, f'⚠️ 오류: {e}'


def _run_claude_docker(
    run_workspace: str,
    memory_dir: str,
    aws_creds: dict,
    prompt: str,
    progress_callback,
    thread_ts: str,
    event: dict,
    msg_id: str,
    is_dm: bool,
) -> tuple[int, str]:
    """Docker 컨테이너에서 Claude를 실행하고 (returncode, output_text)를 반환한다."""
    container_name = _docker_container_name(thread_ts)
    cmd = [
        '/usr/bin/docker',
        'run',
        '--rm',
        '--name',
        container_name,
        '--memory',
        '1g',
        '--cpus',
        '1.0',
        '--cap-drop',
        'ALL',
        '--tmpfs',
        '/tmp',
        '-v',
        f'{run_workspace}:/workspace:rw',
        '-v',
        f'{memory_dir}:{CLAUDE_MEMORY_CONTAINER_PATH}:rw',
        '-e',
        f'ANTHROPIC_API_KEY={ANTHROPIC_API_KEY}',
        '-e',
        f'AWS_ACCESS_KEY_ID={aws_creds["AWS_ACCESS_KEY_ID"]}',
        '-e',
        f'AWS_SECRET_ACCESS_KEY={aws_creds["AWS_SECRET_ACCESS_KEY"]}',
        '-e',
        f'AWS_SESSION_TOKEN={aws_creds["AWS_SESSION_TOKEN"]}',
        '-e',
        f'AWS_DEFAULT_REGION={AWS_DEFAULT_REGION}',
        '-e',
        f'ATLASSIAN_ROVO_MCP_TOKEN={ATLASSIAN_ROVO_MCP_TOKEN}',
        '-e',
        f'SLACK_BOT_TOKEN={SLACK_BOT_TOKEN}',
        '-e',
        f'SENTRY_AUTH_TOKEN={SENTRY_AUTH_TOKEN}',
        '-e',
        f'NERV_MCP_TOKEN={NERV_MCP_TOKEN}',
        '-e',
        f'GITHUB_PAT={GITHUB_PAT}',
        '-e',
        f'SLACK_USER_ID={event.get("user", "")}',
        '-e',
        f'ARTIFACTS_S3_BUCKET={ARTIFACTS_S3_BUCKET}',
        '-e',
        f'ARTIFACTS_BASE_URL={ARTIFACTS_BASE_URL}',
        '-e',
        f'DOCUMENTS_S3_BUCKET={DOCUMENTS_S3_BUCKET}',
        '-e',
        f'MEMORY_S3_BUCKET={MEMORY_S3_BUCKET}',
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
            return 1, f'⚠️ 작업 시간 초과 ({CLAUDE_TIMEOUT}초)'

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
        if progress_callback and now - last_progress_at >= progress_interval_sec:
            progress_callback(_progress_waiting_text(elapsed, CLAUDE_TIMEOUT))
            last_progress_at = now

        if process.poll() is not None and output_queue.empty():
            break

    stdout = ''.join(stdout_chunks)
    stderr = ''.join(stderr_chunks)

    if process.returncode in (137, 143):
        logger.info('Claude container cancelled by user (returncode=%d)', process.returncode)
        return process.returncode, '🛑 사용자에 의해 취소되었습니다.'

    if process.returncode != 0:
        logger.error('Claude exited with code %d: %s', process.returncode, stderr)
        return process.returncode, f'⚠️ 실행 오류:\n```{stderr[:300]}```'

    text_out = stdout.strip()
    if not text_out:
        return process.returncode, '⚠️ 응답이 비어 있습니다.'

    output_length = len(text_out)
    logger.info(
        '[RESPONSE] type=%s team_id=%s channel=%s thread_ts=%s user=%s msg_id=%s output_length=%d',
        'DM' if is_dm else 'mention',
        _normalize_slack_team_id(event.get('team_id') or event.get('team')),
        event.get('channel'),
        thread_ts,
        event.get('user') or event.get('bot_id'),
        msg_id,
        output_length,
    )
    return process.returncode, text_out


def handle_request(event: dict, client):
    try:
        channel = event['channel']
    except KeyError:
        logger.error("handle_request: event missing 'channel': %s", event)
        return

    is_dm = _is_dm_channel(event)
    thread_ts = event.get('thread_ts') or event.get('ts')
    msg_id = event.get('client_msg_id') or event.get('ts')
    user_request = event.get('text', '').replace(f'<@{BOT_USER_ID}>', '').strip()
    has_slack_files = bool(event.get('files'))

    if not user_request and not has_slack_files:
        return

    if not user_request and has_slack_files:
        user_request = '(이 메시지에는 텍스트가 없고 Slack 첨부만 있다. `/workspace/input/`의 파일을 읽고 처리해 달라.)'

    msg_type = 'DM' if is_dm else 'mention'
    logger.info(
        '[REQUEST] type=%s team_id=%s channel=%s thread_ts=%s user=%s msg_id=%s text=%r',
        msg_type,
        _normalize_slack_team_id(event.get('team_id') or event.get('team')),
        channel,
        thread_ts,
        event.get('user') or event.get('bot_id'),
        msg_id,
        user_request,
    )

    if not is_allowed_slack_team(event):
        _post_access_denied(client, channel, thread_ts, TEAM_ACCESS_DENIED_TEXT)
        return

    # 봇(bot_id 있음)은 팀 검사만 통과하면 허용한다(봇은 user_id가 없어 유저 목록 적용 불가).
    # 사람은 팀 검사에 더해 유저 목록까지 검사한다. DM·멘션 모두 동일하게 적용.
    if not event.get('bot_id') and not is_allowed_slack_user(event):
        _post_access_denied(client, channel, thread_ts, USER_ACCESS_DENIED_TEXT)
        return

    container_name = _docker_container_name(thread_ts)
    initial_text = '⏳ 처리 중...'
    waiting_msg = client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=initial_text,
        blocks=_build_cancel_blocks(initial_text, container_name),
    )

    try:
        replies = client.conversations_replies(channel=channel, ts=thread_ts)
        history_msgs = replies.get('messages', [])[:-1]
    except Exception:
        logger.warning('Failed to fetch thread history', exc_info=True)
        history_msgs = []

    context = build_context(history_msgs, is_dm)

    def _progress_callback(progress_text: str) -> None:
        try:
            client.chat_update(
                channel=channel,
                ts=waiting_msg['ts'],
                text=progress_text,
                blocks=_build_cancel_blocks(progress_text, container_name),
            )
        except Exception:
            logger.warning('Failed to post progress update', exc_info=True)

    run_workspace = f'{SANDBOX_RUNS_DIR}/{thread_ts}'
    try:
        run_start = time.time()
        success, answer = run_claude(event, context, user_request, progress_callback=_progress_callback)
        run_elapsed = int(time.time() - run_start)
        elapsed_display = _format_duration(run_elapsed)

        logger.info(
            '[COMPLETED] type=%s channel=%s thread_ts=%s user=%s success=%s elapsed=%s',
            msg_type,
            channel,
            thread_ts,
            event.get('user'),
            success,
            elapsed_display,
        )

        elapsed_suffix = None
        if success:
            elapsed_suffix = [
                {
                    'type': 'context',
                    'elements': [
                        {'type': 'mrkdwn', 'text': f'⏱️ 실행 시간: {elapsed_display}'},
                    ],
                }
            ]

        try:
            post_claude_markdown_to_thread(
                client,
                channel=channel,
                thread_ts=thread_ts,
                markdown_text=answer,
                update_ts=waiting_msg['ts'],
                suffix_blocks=elapsed_suffix,
            )
        except Exception:
            logger.exception('Block Kit post failed, falling back to plain text')
            _clear_waiting_for_redirect(client, channel, waiting_msg['ts'])
            _post_with_degrade(
                client,
                channel,
                thread_ts,
                text=answer,
                blocks=[],
                source_text=answer,
            )

        post_workspace_artifacts_to_thread(client, channel, thread_ts, run_workspace)
    except Exception:
        logger.exception('Failed to process request', exc_info=True)
    finally:
        shutil.rmtree(run_workspace, ignore_errors=True)


@app.action('cancel_claude_run')
def on_cancel_claude_run(ack, body, client):
    """취소 버튼 클릭 → Docker 컨테이너를 중지한다. ALLOWED_TEAM_IDS 검사를 포함한다."""
    ack()

    action_team_id = (body.get('team') or {}).get('id')
    if not is_allowed_slack_team({'team_id': action_team_id}):
        user_id = (body.get('user') or {}).get('id')
        logger.warning(
            'cancel_claude_run denied: team_id=%s user=%s',
            action_team_id,
            user_id,
        )
        try:
            channel = body['channel']['id']
            message_ts = body['message']['ts']
            thread_ts = body['message'].get('thread_ts') or message_ts
            client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=TEAM_ACCESS_DENIED_TEXT,
            )
        except Exception:
            logger.exception('Failed to post team access denied message for cancel action')
        return

    container_name = body['actions'][0]['value']
    user_id = body['user']['id']
    channel = body['channel']['id']
    message_ts = body['message']['ts']

    try:
        result = subprocess.run(
            ['/usr/bin/docker', 'stop', '-t', '5', container_name],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            cancel_text = f'🛑 <@{user_id}>님이 작업을 취소했습니다.'
        else:
            cancel_text = '⚠️ 취소 실패: 이미 종료되었거나 컨테이너를 찾을 수 없습니다.'
    except Exception as exc:
        logger.exception('docker stop failed for %s', container_name)
        cancel_text = f'⚠️ 취소 중 오류: {exc}'

    try:
        client.chat_update(channel=channel, ts=message_ts, text=cancel_text, blocks=[])
    except Exception:
        logger.warning('Failed to update message after cancel', exc_info=True)


def _submit(event, client, context=None):
    event_for_worker = _enrich_event_team_id_for_acl(event, context)
    future = executor.submit(handle_request, event_for_worker, client)
    future.add_done_callback(
        lambda f: (
            logger.exception('handle_request raised an exception', exc_info=f.exception()) if f.exception() else None
        )
    )


@app.event('app_mention')
def on_mention(event, client, context=None):
    if _is_self_event(event, context):
        return
    _submit(event, client, context)


@app.event('message')
def on_dm(event, client, context=None):
    if not _is_dm_channel(event):  # 'im'(1:1) 또는 'mpim'(그룹 DM)
        return
    subtype = event.get('subtype')
    if subtype and subtype not in SLACK_DM_ALLOWED_FILE_MESSAGE_SUBTYPES:
        return
    # 자기 자신이 보낸 메시지만 차단(무한루프 방지). 그 외 봇(Slack workflow 등)은 허용.
    if _is_self_event(event, context):
        return
    _submit(event, client, context)


if __name__ == '__main__':
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
