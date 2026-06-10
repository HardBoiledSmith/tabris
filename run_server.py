import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from const import TEAM_ACCESS_DENIED_TEXT
from const import USER_ACCESS_DENIED_TEXT
from tabris_slack_utils import ARTIFACT_MAX_BYTES_PER_FILE
from tabris_slack_utils import ARTIFACT_MAX_FILES
from tabris_slack_utils import ARTIFACT_MAX_TOTAL_BYTES
from tabris_slack_utils import THREAD_ATTACHMENTS_LIST_MAX
from tabris_slack_utils import WORKSPACE_INPUT_SUBDIR
from tabris_slack_utils import _sanitize_slack_attachment_filename
from tabris_slack_utils import _slack_private_file_url
from tabris_slack_utils import decode_cancel_value

sys.path.append('/etc/tabris')
from settings_local import ALLOWED_TEAM_IDS
from settings_local import ALLOWED_USER_IDS
from settings_local import BOT_USER_ID
from settings_local import ECS_CLUSTER
from settings_local import MAX_WORKERS
from settings_local import SLACK_APP_TOKEN
from settings_local import SLACK_BOT_TOKEN
from settings_local import WORKSPACE_S3_BUCKET

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 토큰/비용 집계용 JSON 이벤트 로그(RESPONSE 등)는 실제 usage가 발생하고 CloudWatch로
# 수집되는 샌드박스(sandbox_worker.py)에서 남긴다. 봇은 사람용 key=value 로그만 남긴다.

# ARTIFACT_MAX_*, WORKSPACE_OUTPUT/INPUT_SUBDIR는 tabris_slack_utils로 이동(상단에서 import).
# DM에서 파일 공유만 있는 메시지 subtype. 그 외 subtype은 무시한다.
SLACK_DM_ALLOWED_FILE_MESSAGE_SUBTYPES = frozenset({'file_share'})

# 봇이 멤버인 1:1 DM('im')과 그룹 DM('mpim')을 DM류로 동일 취급한다.
SLACK_DM_CHANNEL_TYPES = frozenset({'im', 'mpim'})


def _is_dm_channel(event: dict) -> bool:
    return event.get('channel_type') in SLACK_DM_CHANNEL_TYPES


EC2_IMDS_BASE = 'http://169.254.169.254/latest'
EC2_IMDS_TOKEN_TTL_SECONDS = '21600'
AWS_DEFAULT_REGION = 'ap-northeast-2'
# IMDS 조회 실패 시(=로컬/Vagrant 개발환경) 폴백으로 사용할 aws CLI 프로파일.
# prod EC2에서는 IMDS가 성공하므로 이 경로는 실행되지 않는다.
AWS_FALLBACK_PROFILE = 'hbsmith-dv'


app = App(token=SLACK_BOT_TOKEN)
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)


def build_context(messages: list, is_dm: bool) -> str:
    lines = []
    for msg in messages:
        text = msg.get('text', '').strip()
        files = msg.get('files') or []

        is_bot_msg = bool(msg.get('bot_id')) or msg.get('user') == BOT_USER_ID
        is_mention = f'<@{BOT_USER_ID}>' in text

        # 텍스트가 없고 파일만 있는 메시지도 첨부 표기를 위해 포함한다.
        if not text and not files:
            continue
        if not (is_dm or is_bot_msg or is_mention):
            continue

        role = 'Assistant' if is_bot_msg else 'User'
        clean_text = text.replace(f'<@{BOT_USER_ID}>', '').strip()
        attach_names = [
            _sanitize_slack_attachment_filename(str(f.get('name') or 'attached')) for f in files if isinstance(f, dict)
        ]
        attach_note = f' [첨부: {", ".join(attach_names)}]' if attach_names else ''
        if clean_text or attach_note:
            lines.append(f'{role}: {clean_text}{attach_note}'.rstrip())

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

# 워밍 풀 디스패치 큐. 봇은 이 SQS FIFO 큐로 잡을 적재하고, 상주 워커 풀이 소비한다.
# 필수 설정 — 누락되거나 비어 있으면 기동을 거부한다.
try:
    from settings_local import SQS_QUEUE_URL
except ImportError as exc:
    raise RuntimeError('SQS_QUEUE_URL must be set in settings_local') from exc
if not SQS_QUEUE_URL:
    raise RuntimeError('SQS_QUEUE_URL must be set in settings_local')


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


def _fargate_job_id(thread_ts: str, msg_id: str) -> str:
    """thread_ts + msg_id로부터 Fargate 잡 식별자를 만든다(로그·이벤트 상관용)."""
    return f'{thread_ts}-{msg_id}'.replace('.', '-')


def _resolve_aws_credentials() -> dict[str, str]:
    """IMDS(prod EC2) → aws CLI 프로파일(로컬/Vagrant) 순으로 임시 자격증명을 조회한다.

    S3 put / ECS RunTask 등 봇의 모든 aws CLI 호출이 이 자격증명을 공유한다. 둘 다 실패하면 예외를 전파한다.
    """
    try:
        return fetch_ec2_instance_role_credentials()
    except (OSError, urllib.error.HTTPError, urllib.error.URLError, RuntimeError, KeyError) as exc:
        logger.warning('IMDS 자격증명 실패, 프로파일 폴백 시도(%s): %s', AWS_FALLBACK_PROFILE, exc)
        return fetch_credentials_via_aws_profile(AWS_FALLBACK_PROFILE)


def _aws_creds_env(creds: dict) -> dict:
    """resolve된 임시 자격증명을 aws CLI 서브프로세스용 환경변수 dict로 만든다."""
    env = os.environ.copy()
    env['AWS_ACCESS_KEY_ID'] = creds['AWS_ACCESS_KEY_ID']
    env['AWS_SECRET_ACCESS_KEY'] = creds['AWS_SECRET_ACCESS_KEY']
    env['AWS_SESSION_TOKEN'] = creds['AWS_SESSION_TOKEN']
    env['AWS_DEFAULT_REGION'] = AWS_DEFAULT_REGION
    return env


def _s3_put_bytes(bucket: str, key: str, body: bytes, creds: dict) -> None:
    """aws CLI로 객체 하나를 업로드한다(boto3 미사용). stdin 파이프로 임시파일 없이 처리."""
    cmd = [_aws_cli_executable(), 's3', 'cp', '-', f's3://{bucket}/{key}', '--only-show-errors']
    result = subprocess.run(cmd, input=body, env=_aws_creds_env(creds), capture_output=True, timeout=120, check=False)
    if result.returncode != 0:
        raise RuntimeError(f's3 cp 실패(s3://{bucket}/{key}): {result.stderr.decode("utf-8", "replace")[:300]}')


def _ecs_stop_task(task_arn: str, reason: str, creds: dict) -> bool:
    """aws CLI로 ECS StopTask를 호출한다(취소). 성공 여부를 반환한다."""
    cmd = [
        _aws_cli_executable(),
        'ecs',
        'stop-task',
        '--cluster',
        ECS_CLUSTER,
        '--task',
        task_arn,
        '--reason',
        reason,
        '--output',
        'json',
    ]
    result = subprocess.run(cmd, env=_aws_creds_env(creds), capture_output=True, text=True, timeout=30, check=False)
    if result.returncode != 0:
        logger.error('ecs stop-task 실패(task=%s): %s', task_arn, result.stderr[:300])
        return False
    return True


def _sqs_send_message(queue_url: str, group_id: str, dedup_id: str, body: dict, creds: dict) -> dict:
    """aws CLI로 SQS FIFO 큐에 메시지를 보낸다(boto3 미사용).

    group_id: FIFO MessageGroupId(직렬화 경계 = 유저별 memory 미러 → user_id).
    dedup_id: MessageDeduplicationId(job_id) — 5분 콘텐츠 중복 제거.
    """
    cmd = [
        _aws_cli_executable(),
        'sqs',
        'send-message',
        '--queue-url',
        queue_url,
        '--message-group-id',
        group_id,
        '--message-deduplication-id',
        dedup_id,
        '--message-body',
        json.dumps(body),
        '--output',
        'json',
    ]
    result = subprocess.run(cmd, env=_aws_creds_env(creds), capture_output=True, text=True, timeout=30, check=False)
    if result.returncode != 0:
        raise RuntimeError(f'sqs send-message 실패: {result.stderr[:300]}')
    return json.loads(result.stdout or '{}')


def _put_cancel_marker(job_id: str, creds: dict) -> None:
    """jobs/{job_id}/cancel 마커(빈 객체)를 쓴다. 워커가 재배달 메시지를 보고 스킵하게 한다."""
    _s3_put_bytes(WORKSPACE_S3_BUCKET, f'jobs/{job_id}/cancel', b'', creds)


def _collect_current_message_files(event: dict) -> list[dict]:
    """트리거 메시지 첨부의 메타데이터를 수집한다(다운로드·S3 업로드 없음).

    반환: [{'filename': '<name>', 'url': '<url_private*>', 'size': <bytes>}] 목록.
    Fargate 워커가 이 목록으로 Slack에서 직접 받아 `/workspace/input/`에 둔다.
    크기 검증은 Slack file 객체의 size 메타로 사전 컷하고, 실측 컷은 워커가 한다.
    """
    files = event.get('files') or []

    collected: list[dict] = []
    used_names: set[str] = set()
    total_bytes = 0
    for file_obj in files:
        if len(collected) >= ARTIFACT_MAX_FILES:
            logger.warning('Slack input files: max file count %d reached', ARTIFACT_MAX_FILES)
            break
        if not isinstance(file_obj, dict):
            continue
        url = _slack_private_file_url(file_obj)
        if not url:
            logger.warning('Slack file object has no private URL; skipping id=%r', file_obj.get('id'))
            continue
        size = int(file_obj.get('size') or 0)
        if size > ARTIFACT_MAX_BYTES_PER_FILE:
            logger.warning('Slack input files: size %d over per-file limit; skip id=%r', size, file_obj.get('id'))
            continue
        if total_bytes + size > ARTIFACT_MAX_TOTAL_BYTES:
            logger.warning('Slack input files: total byte limit %d reached', ARTIFACT_MAX_TOTAL_BYTES)
            break
        base = _sanitize_slack_attachment_filename(str(file_obj.get('name') or 'attached'))
        candidate = base
        n = 2
        while candidate in used_names:
            root, ext = os.path.splitext(base)
            candidate = f'{root}_{n}{ext}'
            n += 1
        used_names.add(candidate)
        total_bytes += size
        collected.append({'filename': candidate, 'url': url, 'size': size})

    return collected


def _collect_thread_attachments(messages: list, exclude_file_ids: set[str]) -> tuple[list[dict], bool]:
    """스레드 히스토리의 첨부 메타데이터를 모은다(멘션 여부 무관, 봇 출력물 포함).

    file id로 dedupe하고, 현재 메시지 첨부(exclude_file_ids — 이미 eager로 내려감)는 제외한다.
    반환: ([{'name', 'size', 'mimetype', 'source', 'msg_ts', 'url'}], truncated 여부).
    개수가 THREAD_ATTACHMENTS_LIST_MAX를 넘으면 최근 것 우선으로 자른다.
    """
    seen_ids: set[str] = set(exclude_file_ids)
    attachments: list[dict] = []
    for msg in messages:
        is_bot_msg = bool(msg.get('bot_id')) or msg.get('user') == BOT_USER_ID
        for file_obj in msg.get('files') or []:
            if not isinstance(file_obj, dict):
                continue
            file_id = file_obj.get('id')
            if not file_id or file_id in seen_ids:
                continue
            url = _slack_private_file_url(file_obj)
            if not url:
                # 삭제된 파일(tombstone)·접근 제한 파일은 url_private*가 없다.
                continue
            seen_ids.add(file_id)
            attachments.append(
                {
                    'name': _sanitize_slack_attachment_filename(str(file_obj.get('name') or 'attached')),
                    'size': int(file_obj.get('size') or 0),
                    'mimetype': str(file_obj.get('mimetype') or ''),
                    'source': 'Assistant' if is_bot_msg else 'User',
                    'msg_ts': str(msg.get('ts') or ''),
                    'url': url,
                }
            )

    truncated = len(attachments) > THREAD_ATTACHMENTS_LIST_MAX
    if truncated:
        attachments = attachments[-THREAD_ATTACHMENTS_LIST_MAX:]
    return attachments, truncated


def _enqueue_claude_job(
    event: dict, prompt: str, thread_ts: str, waiting_msg_ts: str, slack_input_files: list[dict], received_at: float
) -> str:
    """워밍 풀: prompt를 S3에 올리고 잡을 SQS FIFO 큐에 적재한다(워커가 소비). job_id를 반환한다.

    직렬화 경계는 유저별 memory 미러이므로 MessageGroupId=user_id로 잡아, 같은 유저의 잡을
    직렬화한다(memory 레이스·풀 과점유 방지). user_id가 비면 thread 기반으로 폴백한다.
    취소 버튼은 봇이 여기서 달지 않는다 — 어느 워커가 집을지 모르므로, 워커가 자기 ARN으로 부착한다.
    """
    msg_id = event.get('client_msg_id') or event.get('ts')
    job_id = _fargate_job_id(thread_ts, msg_id)
    slack_user_id = event.get('user') or event.get('bot_id') or ''
    channel = event['channel']

    creds = _resolve_aws_credentials()
    # prompt 본문은 메시지 크기 한도를 피하려고 S3 경유(워커가 prompt_s3_key로 내려받음).
    prompt_key = f'runs/{thread_ts}/prompt.txt'
    _s3_put_bytes(WORKSPACE_S3_BUCKET, prompt_key, prompt.encode('utf-8'), creds)

    body = {
        'job_id': job_id,
        'channel': channel,
        'thread_ts': thread_ts,
        'waiting_msg_ts': waiting_msg_ts,
        'user_id': slack_user_id,
        'is_dm': _is_dm_channel(event),
        'prompt_s3_key': prompt_key,
        # [{filename, url, size}] — 워커가 Slack에서 직접 받아 /workspace/input/에 둔다.
        'input_files': slack_input_files,
        # 봇이 메시지를 받은 시점(epoch). 워커는 이 값 기준으로 총 실행 시간을 계산한다.
        'request_epoch': round(received_at, 3),
    }
    group_id = slack_user_id or f'thread:{thread_ts}'
    _sqs_send_message(SQS_QUEUE_URL, group_id, job_id, body, creds)
    logger.info('[SQS] enqueue job_id=%s group=%s', job_id, group_id)
    return job_id


def _stop_task_fargate(task_arn: str, job_id: str | None, user_id: str, channel: str, msg_ts: str, client) -> str:
    """ECS StopTask로 샌드박스 태스크를 즉시 종료한다(취소). 결과 문자열을 반환한다.

    풀 모드에선 StopTask가 워커를 SIGKILL 하므로 워커가 메시지를 삭제하지 못해 재배달될 수 있다.
    그래서 StopTask **전에** cancel 마커를 먼저 써, 재배달된 잡을 다른 워커가 보고 스킵하게 한다.
    """
    try:
        creds = _resolve_aws_credentials()
        # 마커 먼저(좀비 재실행 방지) → StopTask 나중. job_id는 워밍 풀 경로에서만 채워진다.
        if job_id:
            try:
                _put_cancel_marker(job_id, creds)
            except Exception:
                logger.warning('cancel 마커 기록 실패(job=%s)', job_id, exc_info=True)
        ok = _ecs_stop_task(task_arn, f'cancelled by Slack user {user_id}', creds) if task_arn else False
    except Exception:
        logger.exception('Failed to stop task %s', task_arn)
        ok = False

    if ok:
        cancel_text = f'🛑 <@{user_id}>님이 작업을 취소했습니다.'
        result = 'stopped'
    else:
        cancel_text = '⚠️ 취소 실패: 이미 종료되었거나 태스크를 찾을 수 없습니다.'
        result = 'error'
    try:
        client.chat_update(channel=channel, ts=msg_ts, text=cancel_text, blocks=[])
    except Exception:
        logger.warning('Failed to update message after stop-task', exc_info=True)
    return result


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


def _build_thread_attachments_note(attachments: list[dict], truncated: bool) -> str:
    """스레드의 과거 첨부 목록을 프롬프트 섹션으로 렌더한다(필요할 때만 lazy 다운로드 안내)."""
    if not attachments:
        return ''
    header = (
        '## 스레드의 과거 첨부\n'
        '아래는 이 스레드의 이전 메시지에 올라온 파일들이다(이번 메시지 첨부는 위 `/workspace/input/`에 이미 있음). '
        '필요한 것만 아래 명령으로 `/tmp`에 받아 활용할 것:\n'
        '`python ~/.claude/skills/slack_fetch/scripts/download_files.py '
        "--token $SLACK_BOT_TOKEN --url '<url>' --name '<name>' --output-dir /tmp`\n"
        '받으려는 파일이 404 등으로 실패하면 사용자에게 해당 파일을 다시 올려달라고 안내할 것.\n'
    )
    rows = []
    for a in attachments:
        meta = ' / '.join(
            filter(
                None,
                [
                    f'출처: {a["source"]}',
                    f'{a["size"]} bytes' if a.get('size') else '',
                    a.get('mimetype') or '',
                    f'ts={a["msg_ts"]}' if a.get('msg_ts') else '',
                ],
            )
        )
        rows.append(f"- `{a['name']}` ({meta})\n  url: '{a['url']}'")
    note = header + '\n'.join(rows) + '\n'
    if truncated:
        note += f'(오래된 첨부 일부는 생략됨 — 최근 {THREAD_ATTACHMENTS_LIST_MAX}개만 표시)\n'
    return note + '\n'


def _build_prompt(input_relpaths: list[str], thread_attachments_note: str, context: str, request: str) -> str:
    """첨부 안내 + 스레드 과거 첨부 + 이전 대화 + 현재 요청을 합쳐 claude 프롬프트를 만든다."""
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
    prefix = input_note + thread_attachments_note
    if context:
        return f'{prefix}## 이전 대화\n{context}\n\n## 현재 요청\n{request}'
    return f'{prefix}## 현재 요청\n{request}'


def _prepare_prompt(client, event, channel, thread_ts, is_dm, user_request, waiting_msg_ts):
    """현재 첨부 메타 수집 + 스레드 히스토리/과거 첨부 + 현재 요청을 합쳐 (prompt, slack_input_files)를 만든다.

    스레드 replies는 1회만 fetch해 이전 대화 context와 과거 첨부 목록 양쪽에 쓴다.
    """
    slack_input_files = _collect_current_message_files(event)
    input_relpaths = [f'{WORKSPACE_INPUT_SUBDIR}/{f["filename"]}' for f in slack_input_files]
    current_file_ids = {f.get('id') for f in (event.get('files') or []) if isinstance(f, dict) and f.get('id')}

    all_msgs = _fetch_thread_messages(client, channel, thread_ts)
    # 트리거 메시지·대기 메시지는 "이전 대화"에서 제외한다(현재 요청과 중복 방지).
    trigger_ts = event.get('ts')
    exclude_ts = {ts for ts in (trigger_ts, waiting_msg_ts) if ts}
    history_msgs = [m for m in all_msgs if m.get('ts') not in exclude_ts]

    context = build_context(history_msgs, is_dm)
    attachments, truncated = _collect_thread_attachments(history_msgs, current_file_ids)
    thread_attachments_note = _build_thread_attachments_note(attachments, truncated)
    prompt = _build_prompt(input_relpaths, thread_attachments_note, context, user_request)
    return prompt, slack_input_files


def _fetch_thread_messages(client, channel: str, thread_ts: str) -> list[dict]:
    """스레드 전체 메시지를 cursor 페이지네이션으로 모은다. 실패 시 빈 리스트."""
    messages: list[dict] = []
    cursor = None
    try:
        while True:
            kwargs = {'channel': channel, 'ts': thread_ts, 'limit': 200}
            if cursor:
                kwargs['cursor'] = cursor
            replies = client.conversations_replies(**kwargs)
            messages.extend(replies.get('messages', []) or [])
            cursor = (replies.get('response_metadata') or {}).get('next_cursor') or ''
            if not cursor:
                break
    except Exception:
        logger.warning('Failed to fetch thread history', exc_info=True)
        return messages
    return messages


def _handle_request_pool(event, client, channel, thread_ts, is_dm, user_request, received_at):
    """워밍 풀 모드: ① 즉시 접수 메시지 → ② S3 업로드 → ③ SQS 적재. 취소 버튼은 워커가 단다.

    봇은 어느 워커가 잡을 집을지 모르므로(=task ARN 미정) 여기서 취소 버튼을 달지 않는다.
    워커가 잡을 집는 즉시 자기 task ARN + job_id로 버튼을 부착한다.
    """
    initial_text = '⏳ 접수됨 — 작업 처리 대기중입니다…'
    waiting_msg = client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=initial_text)
    try:
        prompt, slack_input_files = _prepare_prompt(
            client, event, channel, thread_ts, is_dm, user_request, waiting_msg['ts']
        )
        _enqueue_claude_job(event, prompt, thread_ts, waiting_msg['ts'], slack_input_files, received_at)
    except Exception as exc:
        logger.exception('Failed to enqueue job to SQS')
        try:
            client.chat_update(channel=channel, ts=waiting_msg['ts'], text=f'⚠️ 작업 접수 실패: {exc}', blocks=[])
        except Exception:
            logger.warning('Failed to update waiting message after enqueue error', exc_info=True)


def handle_request(event: dict, client):
    received_at = time.time()
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
    req_team_id = _normalize_slack_team_id(event.get('team_id') or event.get('team'))
    req_user = event.get('user') or event.get('bot_id')
    logger.info(
        '[REQUEST] type=%s team_id=%s channel=%s thread_ts=%s user=%s msg_id=%s text=%r',
        msg_type,
        req_team_id,
        channel,
        thread_ts,
        req_user,
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

    _handle_request_pool(event, client, channel, thread_ts, is_dm, user_request, received_at)


@app.action('cancel_claude_run')
def on_cancel_claude_run(ack, body, client):
    """취소 버튼 클릭 → cancel 마커 후 ecs StopTask로 샌드박스 태스크를 종료한다. ALLOWED_TEAM_IDS 검사 포함.

    버튼 value는 task ARN + job_id를 인코딩한 것(레거시 평문 ARN도 수용). job_id가 있으면(풀 모드)
    StopTask 전에 cancel 마커를 먼저 써 좀비 재실행을 막는다.
    """
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

    task_arn, job_id = decode_cancel_value(body['actions'][0]['value'])
    user_id = body['user']['id']
    channel = body['channel']['id']
    message_ts = body['message']['ts']
    thread_ts = body['message'].get('thread_ts') or message_ts
    cancel_team_id = _normalize_slack_team_id(action_team_id)

    cancel_result = _stop_task_fargate(task_arn, job_id, user_id, channel, message_ts, client)
    logger.info(
        '[CANCEL] team_id=%s channel=%s thread_ts=%s user=%s task=%s job=%s result=%s',
        cancel_team_id,
        channel,
        thread_ts,
        user_id,
        task_arn,
        job_id,
        cancel_result,
    )


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
