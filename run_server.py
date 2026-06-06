import base64
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
from tabris_slack_utils import WORKSPACE_INPUT_SUBDIR
from tabris_slack_utils import _build_cancel_blocks
from tabris_slack_utils import _sanitize_slack_attachment_filename

sys.path.append('/etc/tabris')
from settings_local import ALLOWED_TEAM_IDS
from settings_local import ALLOWED_USER_IDS
from settings_local import ANTHROPIC_API_KEY
from settings_local import ARTIFACTS_BASE_URL
from settings_local import ARTIFACTS_S3_BUCKET
from settings_local import BOT_USER_ID
from settings_local import CLAUDE_TIMEOUT
from settings_local import DOCUMENTS_S3_BUCKET
from settings_local import ECS_ASSIGN_PUBLIC_IP
from settings_local import ECS_CLUSTER
from settings_local import ECS_SANDBOX_TASK_DEFINITION
from settings_local import ECS_SECURITY_GROUP_ID
from settings_local import ECS_SUBNET_IDS
from settings_local import GITHUB_PAT
from settings_local import JIRA_API_KEY
from settings_local import JIRA_API_USERNAME
from settings_local import MAX_WORKERS
from settings_local import MEMORY_S3_BUCKET
from settings_local import NERV_MCP_TOKEN
from settings_local import SENTRY_AUTH_TOKEN
from settings_local import SLACK_APP_TOKEN
from settings_local import SLACK_BOT_TOKEN
from settings_local import WORKSPACE_S3_BUCKET


def _as_subnet_list(raw) -> list[str]:
    """ECS_SUBNET_IDS를 list[str]로 정규화한다(CSV 문자열/list 모두 허용)."""
    if isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = str(raw or '').split(',')
    return [s.strip() for s in items if s and s.strip()]


# Atlassian MCP Basic auth: echo -n "user:api_key" | base64
ATLASSIAN_ROVO_MCP_TOKEN = base64.b64encode(f'{JIRA_API_USERNAME}:{JIRA_API_KEY}'.encode()).decode('ascii')

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
    result = subprocess.run(
        cmd, input=body, env=_aws_creds_env(creds), capture_output=True, timeout=120, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f's3 cp 실패(s3://{bucket}/{key}): {result.stderr.decode("utf-8", "replace")[:300]}')


def _ecs_run_task(network_config: dict, overrides: dict, creds: dict) -> dict:
    """aws CLI로 ECS RunTask를 호출하고 응답 JSON을 반환한다(boto3 미사용)."""
    cmd = [
        _aws_cli_executable(), 'ecs', 'run-task',
        '--cluster', ECS_CLUSTER,
        '--task-definition', ECS_SANDBOX_TASK_DEFINITION,
        '--launch-type', 'FARGATE',
        '--count', '1',
        '--network-configuration', json.dumps(network_config),
        '--overrides', json.dumps(overrides),
        '--output', 'json',
    ]
    result = subprocess.run(
        cmd, env=_aws_creds_env(creds), capture_output=True, text=True, timeout=60, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f'ecs run-task 실패: {result.stderr[:300]}')
    return json.loads(result.stdout or '{}')


def _ecs_stop_task(task_arn: str, reason: str, creds: dict) -> bool:
    """aws CLI로 ECS StopTask를 호출한다(취소). 성공 여부를 반환한다."""
    cmd = [
        _aws_cli_executable(), 'ecs', 'stop-task',
        '--cluster', ECS_CLUSTER,
        '--task', task_arn,
        '--reason', reason,
        '--output', 'json',
    ]
    result = subprocess.run(
        cmd, env=_aws_creds_env(creds), capture_output=True, text=True, timeout=30, check=False
    )
    if result.returncode != 0:
        logger.error('ecs stop-task 실패(task=%s): %s', task_arn, result.stderr[:300])
        return False
    return True


def _upload_slack_files_to_s3(event: dict, thread_ts: str) -> list[dict]:
    """트리거 메시지 첨부를 S3 `runs/{thread_ts}/input/`에 올린다.

    반환: [{'filename': '<name>', 's3_key': 'runs/.../input/<name>'}] 목록.
    Fargate 샌드박스가 이 목록으로 `/workspace/input/`에 내려받는다.
    """
    files = event.get('files') or []
    if not files:
        return []

    creds = _resolve_aws_credentials()
    uploaded: list[dict] = []
    used_names: set[str] = set()
    total_bytes = 0
    for file_obj in files:
        if len(uploaded) >= ARTIFACT_MAX_FILES:
            logger.warning('Slack input files: max file count %d reached', ARTIFACT_MAX_FILES)
            break
        if not isinstance(file_obj, dict):
            continue
        url = _slack_private_file_url(file_obj)
        if not url:
            logger.warning('Slack file object has no private URL; skipping id=%r', file_obj.get('id'))
            continue
        blob = _read_slack_private_url(url, SLACK_BOT_TOKEN, ARTIFACT_MAX_BYTES_PER_FILE)
        if not blob:
            continue
        if total_bytes + len(blob) > ARTIFACT_MAX_TOTAL_BYTES:
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
        s3_key = f'runs/{thread_ts}/input/{candidate}'
        try:
            _s3_put_bytes(WORKSPACE_S3_BUCKET, s3_key, blob, creds)
        except Exception:
            logger.warning('Failed to upload Slack input file to S3: %s', s3_key, exc_info=True)
            continue
        total_bytes += len(blob)
        uploaded.append({'filename': candidate, 's3_key': s3_key})

    return uploaded


def _run_claude_fargate(
    event: dict, prompt: str, thread_ts: str, waiting_msg_ts: str, slack_input_files: list[dict], received_at: float
):
    """ECS Fargate에 sandbox 태스크를 RunTask로 띄운다(fire & forget).

    시크릿/설정은 env override로 직접 주입한다(Secrets Manager 미사용). 이후 모든 Slack
    업데이트·결과 게시는 샌드박스 태스크(sandbox_worker.py)가 담당한다.
    """
    msg_id = event.get('client_msg_id') or event.get('ts')
    job_id = _fargate_job_id(thread_ts, msg_id)
    slack_user_id = event.get('user') or event.get('bot_id') or ''
    is_dm = _is_dm_channel(event)
    channel = event['channel']

    subnets = _as_subnet_list(ECS_SUBNET_IDS)
    if not subnets or not ECS_SECURITY_GROUP_ID:
        raise RuntimeError(
            'Fargate 네트워크 설정 누락: ECS_SUBNET_IDS / ECS_SECURITY_GROUP_ID를 settings_local.py에 설정하세요.'
        )

    # 1. prompt를 S3에 저장 (env override 크기 한도를 피하려고 본문은 S3 경유)
    creds = _resolve_aws_credentials()
    prompt_key = f'runs/{thread_ts}/prompt.txt'
    _s3_put_bytes(WORKSPACE_S3_BUCKET, prompt_key, prompt.encode('utf-8'), creds)

    # 2. ECS RunTask — job 파라미터 + 시크릿/설정을 env override로 주입
    env_override = [
        {'name': 'TABRIS_JOB_ID', 'value': job_id},
        {'name': 'TABRIS_SLACK_CHANNEL', 'value': channel},
        {'name': 'TABRIS_SLACK_THREAD_TS', 'value': thread_ts},
        {'name': 'TABRIS_SLACK_WAITING_MSG_TS', 'value': waiting_msg_ts},
        {'name': 'TABRIS_SLACK_USER_ID', 'value': slack_user_id},
        {'name': 'TABRIS_SLACK_IS_DM', 'value': str(is_dm).lower()},
        {'name': 'TABRIS_PROMPT_S3_KEY', 'value': prompt_key},
        {'name': 'TABRIS_INPUT_FILES_JSON', 'value': json.dumps(slack_input_files)},
        # 봇이 메시지를 받은 시점(epoch). 샌드박스는 이 값 기준으로 총 실행 시간을 계산한다.
        {'name': 'TABRIS_REQUEST_EPOCH', 'value': f'{received_at:.3f}'},
        # 시크릿/토큰 (RunTask env override 직접 주입)
        {'name': 'ANTHROPIC_API_KEY', 'value': ANTHROPIC_API_KEY},
        {'name': 'SLACK_BOT_TOKEN', 'value': SLACK_BOT_TOKEN},
        {'name': 'NERV_MCP_TOKEN', 'value': NERV_MCP_TOKEN},
        {'name': 'ATLASSIAN_ROVO_MCP_TOKEN', 'value': ATLASSIAN_ROVO_MCP_TOKEN},
        {'name': 'GITHUB_PAT', 'value': GITHUB_PAT},
        {'name': 'SENTRY_AUTH_TOKEN', 'value': SENTRY_AUTH_TOKEN},
        {'name': 'SLACK_USER_ID', 'value': slack_user_id},
        # 버킷/설정 (task def에도 있으나 봇 설정과 일치시키려 override로 한 번 더 명시)
        {'name': 'WORKSPACE_S3_BUCKET', 'value': WORKSPACE_S3_BUCKET},
        {'name': 'MEMORY_S3_BUCKET', 'value': MEMORY_S3_BUCKET},
        {'name': 'ARTIFACTS_S3_BUCKET', 'value': ARTIFACTS_S3_BUCKET},
        {'name': 'ARTIFACTS_BASE_URL', 'value': ARTIFACTS_BASE_URL},
        {'name': 'DOCUMENTS_S3_BUCKET', 'value': DOCUMENTS_S3_BUCKET},
        {'name': 'CLAUDE_TIMEOUT', 'value': str(CLAUDE_TIMEOUT)},
    ]

    network_config = {
        'awsvpcConfiguration': {
            'subnets': subnets,
            'securityGroups': [ECS_SECURITY_GROUP_ID],
            'assignPublicIp': ECS_ASSIGN_PUBLIC_IP,
        }
    }
    overrides = {'containerOverrides': [{'name': 'tabris-sandbox', 'environment': env_override}]}
    resp = _ecs_run_task(network_config, overrides, creds)
    failures = resp.get('failures') or []
    if failures:
        raise RuntimeError(f'ECS RunTask 실패: {failures}')
    task_arn = (resp.get('tasks') or [{}])[0].get('taskArn')
    logger.info('[FARGATE] RunTask job_id=%s task=%s', job_id, task_arn)
    return job_id, task_arn


def _stop_task_fargate(task_arn: str, user_id: str, channel: str, msg_ts: str, client) -> str:
    """ECS StopTask로 샌드박스 태스크를 즉시 종료한다(취소). 결과 문자열을 반환한다."""
    try:
        creds = _resolve_aws_credentials()
        ok = _ecs_stop_task(task_arn, f'cancelled by Slack user {user_id}', creds)
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


def _build_prompt(input_relpaths: list[str], context: str, request: str) -> str:
    """첨부 안내 + 이전 대화 + 현재 요청을 합쳐 claude 프롬프트를 만든다(docker·fargate 공용)."""
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
        return f'{input_note}## 이전 대화\n{context}\n\n## 현재 요청\n{request}'
    return f'{input_note}## 현재 요청\n{request}'


def _handle_request_fargate(event, client, channel, thread_ts, is_dm, user_request, received_at):
    """Fargate 모드: ① 즉시 접수 메시지(버튼 없음) → ② S3 업로드·RunTask → ③ ARN 취소 버튼 부착.

    취소 버튼 value에는 task ARN을 담아, 클릭 시 ecs StopTask로 태스크를 즉시 종료한다.
    태스크가 생기기 전(①~②)에는 취소할 대상이 없으므로 버튼을 달지 않는다.
    received_at: 봇이 메시지를 받은 시점(epoch). 샌드박스가 실행 시간 표시에 사용한다.
    """
    # ① 즉시 접수 메시지(버튼 없음) — cold start를 가리는 즉시 ACK.
    initial_text = '⏳ 접수됨 — 샌드박스를 시작하고 있습니다…'
    waiting_msg = client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=initial_text)

    try:
        slack_input_files = _upload_slack_files_to_s3(event, thread_ts)
        input_relpaths = [f'{WORKSPACE_INPUT_SUBDIR}/{f["filename"]}' for f in slack_input_files]

        try:
            replies = client.conversations_replies(channel=channel, ts=thread_ts)
            history_msgs = replies.get('messages', [])[:-1]
        except Exception:
            logger.warning('Failed to fetch thread history', exc_info=True)
            history_msgs = []
        context = build_context(history_msgs, is_dm)

        prompt = _build_prompt(input_relpaths, context, user_request)
        # ② RunTask → task ARN 획득
        _job_id, task_arn = _run_claude_fargate(
            event, prompt, thread_ts, waiting_msg['ts'], slack_input_files, received_at
        )
    except Exception as exc:
        logger.exception('Failed to dispatch Fargate task')
        try:
            client.chat_update(
                channel=channel,
                ts=waiting_msg['ts'],
                text=f'⚠️ 샌드박스 시작 실패: {exc}',
                blocks=[],
            )
        except Exception:
            logger.warning('Failed to update waiting message after dispatch error', exc_info=True)
        return

    # ③ ARN을 담은 취소 버튼 부착. 이후 진행률 업데이트 시 워커가 같은 ARN으로 버튼을 유지한다.
    if task_arn:
        try:
            client.chat_update(
                channel=channel,
                ts=waiting_msg['ts'],
                text=initial_text,
                blocks=_build_cancel_blocks(initial_text, task_arn),
            )
        except Exception:
            logger.warning('Failed to attach cancel button after dispatch', exc_info=True)


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

    _handle_request_fargate(event, client, channel, thread_ts, is_dm, user_request, received_at)


@app.action('cancel_claude_run')
def on_cancel_claude_run(ack, body, client):
    """취소 버튼 클릭 → ecs StopTask로 샌드박스 태스크를 종료한다. ALLOWED_TEAM_IDS 검사를 포함한다.

    버튼 value에는 task ARN이 들어 있다(봇 디스패치 후 부착 / 워커 진행률 갱신 시 유지).
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

    task_arn = body['actions'][0]['value']
    user_id = body['user']['id']
    channel = body['channel']['id']
    message_ts = body['message']['ts']
    thread_ts = body['message'].get('thread_ts') or message_ts
    cancel_team_id = _normalize_slack_team_id(action_team_id)

    cancel_result = _stop_task_fargate(task_arn, user_id, channel, message_ts, client)
    logger.info(
        '[CANCEL] team_id=%s channel=%s thread_ts=%s user=%s task=%s result=%s',
        cancel_team_id,
        channel,
        thread_ts,
        user_id,
        task_arn,
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
