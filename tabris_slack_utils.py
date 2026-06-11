"""run_server.py(EB/Vagrant 봇)와 sandbox_worker.py(Fargate 샌드박스)가 공유하는 유틸.

이 모듈은 settings_local import나 slack_bolt App 생성 같은 사이드 이펙트가 없어야 한다.
sandbox_worker.py가 봇 전체를 import하지 않고 Slack 게시/아티팩트 업로드 로직만
재사용할 수 있도록 분리한 것이다.
"""

import json
import logging
import os
import re
import stat
import urllib.error
import urllib.request

from slack_markdown_parser import build_fallback_text_from_blocks
from slack_markdown_parser import convert_markdown_to_slack_blocks
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)

ARTIFACT_MAX_FILES = 10
ARTIFACT_MAX_BYTES_PER_FILE = 1_073_741_824  # 1 GiB
ARTIFACT_MAX_TOTAL_BYTES = 5_368_709_120  # 5 GiB
# Slack 파일 업로드는 이 하위만 스캔한다. 중간 산출은 컨테이너 `/tmp` 등에 두도록 CLAUDE.md로 안내한다.
WORKSPACE_OUTPUT_SUBDIR = 'output'
# 트리거 메시지의 Slack 첨부만 워커가 받아 컨테이너 `/workspace/input/`에 둔다.
WORKSPACE_INPUT_SUBDIR = 'input'
# 프롬프트에 나열하는 스레드 과거 첨부 개수 상한. 초과분(오래된 것부터)은 생략을 명시한다.
THREAD_ATTACHMENTS_LIST_MAX = 50

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


def _format_duration(seconds: int) -> str:
    """초를 읽기 쉬운 한국어 시간 표현으로 변환한다. 0인 단위는 생략한다."""
    m, s = divmod(seconds, 60)
    if m and s:
        return f'{m}분 {s}초'
    if m:
        return f'{m}분'
    return f'{s}초'


def _progress_waiting_text(elapsed_sec: int, timeout_sec: int, model: str | None = None) -> str:
    """Claude 실행 중 Slack 대기 메시지: 경과/최대 대기(분초) + (있으면) 수행 모델."""
    text = f'⏳ 처리 중… ({_format_duration(elapsed_sec)} / {_format_duration(timeout_sec)})'
    if model:
        text += f' · 🤖 {model}'
    return text


def _format_usd(cost: float) -> str:
    """USD 비용 표기. 1달러 미만의 소액(토큰 비용 대부분)은 4자리, 이상은 2자리 소수."""
    return f'${cost:.2f}' if cost >= 1 else f'${cost:.4f}'


def _build_result_meta_text(elapsed_sec: int, usage: dict | None) -> str:
    """결과 메시지 하단 context 텍스트: 실행 시간 + (있으면) 실제 모델·토큰·비용.

    usage는 sandbox_worker가 claude CLI JSON에서 파싱한
    {model, total_cost_usd, input_tokens, output_tokens}. 누락 필드는 항목째 생략한다.
    """
    parts = [f'⏱️ 실행 시간: {_format_duration(elapsed_sec)}']
    u = usage or {}
    if u.get('model'):
        parts.append(f'🤖 {u["model"]}')
    tokens = [
        f'{label} {u[key]:,}'
        for label, key in (('입력', 'input_tokens'), ('출력', 'output_tokens'))
        if u.get(key) is not None
    ]
    if tokens:
        parts.append(f'🔢 토큰: {" / ".join(tokens)}')
    if u.get('total_cost_usd') is not None:
        parts.append(f'💰 {_format_usd(u["total_cost_usd"])}')
    return ' · '.join(parts)


def encode_cancel_value(task_arn: str | None, job_id: str | None) -> str:
    """취소 버튼 value: task ARN(StopTask 대상) + job_id(cancel 마커 키)를 JSON 한 줄로 인코딩한다.

    워밍 풀에서는 봇이 디스패치 시점에 task ARN을 모르고(어느 워커가 집을지 미정), 워커가 잡을
    집은 뒤 자기 ARN으로 버튼을 채운다. 봇 취소 핸들러는 이 값을 디코드해 마커를 먼저 쓰고 StopTask 한다.
    """
    return json.dumps({'task_arn': task_arn or '', 'job_id': job_id or ''}, separators=(',', ':'))


def decode_cancel_value(value: str) -> tuple[str, str | None]:
    """취소 버튼 value를 (task_arn, job_id)로 디코드한다. 레거시(평문 ARN) value도 그대로 수용한다."""
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, ValueError, TypeError):
        return value, None  # 레거시: value 통째가 task ARN, job_id 없음
    if isinstance(parsed, dict):
        return parsed.get('task_arn') or '', (parsed.get('job_id') or None)
    return value, None


def _build_cancel_blocks(text: str, value: str) -> list[dict]:
    """대기/진행 메시지에 취소 버튼을 포함한 Block Kit blocks를 만든다.

    value는 취소 동작 식별자다. fargate 1회용은 task ARN, 워밍 풀은 encode_cancel_value(ARN+job_id).
    """
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
                    'value': value,
                }
            ],
        },
    ]
