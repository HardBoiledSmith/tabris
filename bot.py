import os
import sys
import json
import shutil
import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

sys.path.append('/etc/ireul')
from settings_local import BOT_USER_ID
from settings_local import ANTHROPIC_API_KEY
from settings_local import MCP_CONFIG_PATH
from settings_local import DOCKER_IMAGE
from settings_local import CLAUDE_TIMEOUT


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = App(token=os.environ["SLACK_BOT_TOKEN"])
executor = ThreadPoolExecutor(max_workers=int(os.environ.get("MAX_WORKERS", "5")))


@app.middleware
def log_all_requests(body, next):
    logger.info("RAW incoming body: %s", json.dumps(body, ensure_ascii=False)[:500])
    return next()


def build_context(messages: list, is_dm: bool) -> str:
    lines = []
    for msg in messages:
        text = msg.get("text", "").strip()
        if not text:
            continue

        is_bot_msg = bool(msg.get("bot_id"))
        is_mention = f"<@{BOT_USER_ID}>" in text

        if is_dm or is_bot_msg or is_mention:
            role = "Assistant" if is_bot_msg else "User"
            clean_text = text.replace(f"<@{BOT_USER_ID}>", "").strip()
            if clean_text:
                lines.append(f"{role}: {clean_text}")

    return "\n".join(lines)


def run_claude(context: str, request: str, thread_ts: str) -> str:
    workspace = f"/tmp/claude-sandbox/{thread_ts}"
    os.makedirs(workspace, exist_ok=True)

    try:
        ctx_file = os.path.join(workspace, "context.md")
        with open(ctx_file, "w", encoding="utf-8") as f:
            if context:
                f.write(f"## 이전 대화\n{context}\n\n")
            f.write(f"## 현재 요청\n{request}")

        with open(ctx_file, "r", encoding="utf-8") as f:
            prompt = f.read()

        mcp_dest = os.path.join(workspace, "mcp.json")
        if os.path.exists(MCP_CONFIG_PATH):
            shutil.copy(MCP_CONFIG_PATH, mcp_dest)
        else:
            with open(mcp_dest, "w") as f:
                json.dump({"mcpServers": {}}, f)

        cmd = [
            "docker", "run", "--rm",
            "--add-host", "host.docker.internal:host-gateway",
            "--network", "bridge",
            "--memory", "512m",
            "--cpus", "1.0",
            "--cap-drop", "ALL",
            "--read-only",
            "--tmpfs", "/tmp",
            "--tmpfs", "/root/.claude",
            "--tmpfs", "/root/.config",
            "-v", f"{workspace}:/workspace:ro",
            "-e", f"ANTHROPIC_API_KEY={ANTHROPIC_API_KEY}",
            "--workdir", "/workspace",
            DOCKER_IMAGE,
            "claude",
            "-p", prompt,
            "--mcp-config", "/workspace/mcp.json",
            "--dangerously-skip-permissions",
            "--output-format", "json",
        ]

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=CLAUDE_TIMEOUT
        )

        if result.returncode != 0:
            logger.error("Claude exited with code %d: %s", result.returncode, result.stderr)
            return f"⚠️ 실행 오류:\n```{result.stderr[:300]}```"

        try:
            output = json.loads(result.stdout)
            return output.get("result") or output.get("content") or result.stdout
        except json.JSONDecodeError:
            return result.stdout.strip() or "⚠️ 응답을 파싱할 수 없습니다."

    except subprocess.TimeoutExpired:
        return f"⚠️ 작업 시간 초과 ({CLAUDE_TIMEOUT}초)"
    except Exception as e:
        logger.exception("Unexpected error in run_claude")
        return f"⚠️ 오류: {e}"
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def handle_request(event: dict, client):
    try:
        channel = event["channel"]
    except KeyError:
        logger.error("handle_request: event missing 'channel': %s", event)
        return

    is_dm = event.get("channel_type") == "im"
    thread_ts = event.get("thread_ts") or event.get("ts")
    user_request = event.get("text", "").replace(f"<@{BOT_USER_ID}>", "").strip()

    msg_type = "DM" if is_dm else "mention"
    logger.info("[%s] channel=%s thread_ts=%s user=%s text=%r", msg_type, channel, thread_ts, event.get("user"), user_request)

    if not user_request:
        return

    waiting_msg = client.chat_postMessage(
        channel=channel, thread_ts=thread_ts, text="⏳ 처리 중..."
    )

    try:
        replies = client.conversations_replies(channel=channel, ts=thread_ts)
        history_msgs = replies.get("messages", [])[:-1]
    except Exception:
        logger.warning("Failed to fetch thread history", exc_info=True)
        history_msgs = []

    context = build_context(history_msgs, is_dm)
    answer = run_claude(context, user_request, thread_ts)

    try:
        client.chat_update(channel=channel, ts=waiting_msg["ts"], text=answer)
    except Exception:
        logger.warning("chat_update failed, falling back to new message", exc_info=True)
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=answer)


def _submit(event, client):
    future = executor.submit(handle_request, event, client)
    future.add_done_callback(
        lambda f: logger.exception("handle_request raised an exception", exc_info=f.exception())
        if f.exception() else None
    )


@app.event("app_mention")
def on_mention(event, client):
    logger.info("app_mention received: user=%s channel=%s", event.get("user"), event.get("channel"))
    _submit(event, client)


@app.event("message")
def on_dm(event, client):
    if event.get("channel_type") != "im":
        return
    if event.get("subtype"):
        return
    if event.get("bot_id"):
        return
    logger.info("DM received: user=%s channel=%s", event.get("user"), event.get("channel"))
    _submit(event, client)


if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
