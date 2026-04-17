---
name: slack_fetch
description: >
  Fetch Slack messages with full blocks, attachments, file links, and raw JSON structures using the Slack Web API directly with a Bot Token.
  Use this skill whenever the user wants to read Slack messages, extract block kit JSON, retrieve attachments, download file links, scrape channel history, or read threads from Slack — even if they just say "get messages from Slack", "pull Slack data", or "read a Slack channel". Always use this skill for any Slack data retrieval task that requires structured message content beyond plain text.
---

# Slack Fetch Skill

Fetches Slack messages including **text**, **blocks (Block Kit JSON)**, **attachments**, and **file/image links** via the Slack Web API.

## Prerequisites

- A Slack **Bot Token** (`xoxb-...`) with the following scopes:
  - `channels:history` — public channel messages
  - `groups:history` — private channel messages
  - `im:history` — DM messages
  - `mpim:history` — group DM messages
  - `files:read` — file metadata and links
- The bot must be **invited to the channel** before it can read messages.

## Setup

The user must provide their Bot Token. Store it as an environment variable or accept it as a parameter:

```bash
export SLACK_BOT_TOKEN="xoxb-your-token-here"
```

Or pass it directly in the script call.

## Core Operations

### 1. Fetch Channel History

Use `scripts/fetch_channel.py` to pull messages from a channel.

```bash
python scripts/fetch_channel.py \
  --token $SLACK_BOT_TOKEN \
  --channel C1234567890 \
  --limit 50 \
  --output messages.json
```

Returns full message objects including `text`, `blocks`, `attachments`, `files`.

### 2. Fetch a Thread

Use `scripts/fetch_thread.py` to pull all replies in a thread.

```bash
python scripts/fetch_thread.py \
  --token $SLACK_BOT_TOKEN \
  --channel C1234567890 \
  --thread_ts 1234567890.123456 \
  --output thread.json
```

### 3. Search Messages

Use `scripts/search_messages.py` to search and retrieve matching messages with full structure.

```bash
python scripts/search_messages.py \
  --token $SLACK_BOT_TOKEN \
  --query "keyword" \
  --output results.json
```

> ⚠️ Search requires a **user token** (`xoxp-...`) with `search:read` scope, not a bot token.

## Output Format

Every script outputs a JSON array of message objects. Each message looks like:

```json
{
  "ts": "1713000000.000100",
  "user": "U12345678",
  "text": "Plain text fallback",
  "blocks": [
    {
      "type": "section",
      "text": { "type": "mrkdwn", "text": "*Hello*" }
    }
  ],
  "attachments": [
    {
      "fallback": "Attachment fallback",
      "color": "#36a64f",
      "title": "Attachment Title",
      "text": "Attachment body"
    }
  ],
  "files": [
    {
      "id": "F12345678",
      "name": "image.png",
      "mimetype": "image/png",
      "url_private": "https://files.slack.com/files-pri/..."
    }
  ]
}
```

## Extracting Specific Data

After fetching, use Claude to analyze the JSON or use `scripts/extract_fields.py` to pull specific fields:

```bash
# Extract only blocks and attachments
python scripts/extract_fields.py messages.json --fields blocks attachments

# Extract all file download URLs
python scripts/extract_fields.py messages.json --fields files.url_private
```

## Pagination

All fetch scripts support `--oldest` and `--latest` (Unix timestamps) to scope the time range, and automatically paginate using Slack's cursor-based pagination (`next_cursor`).

```bash
python scripts/fetch_channel.py \
  --token $SLACK_BOT_TOKEN \
  --channel C1234567890 \
  --oldest 1700000000 \
  --latest 1713000000 \
  --limit 200
```

## Downloading Files

Files in Slack require authenticated download. Use `scripts/download_files.py`:

```bash
python scripts/download_files.py \
  --token $SLACK_BOT_TOKEN \
  --input messages.json \
  --output-dir ./downloads/
```

## Error Reference

See `references/errors.md` for common Slack API errors and how to fix them.

## Notes

- `blocks` is populated when messages use Block Kit (modern Slack apps/bots). Older messages or simple user messages may only have `text`.
- `attachments` is the legacy format — still widely used by many integrations and bots.
- `url_private` file links require the Bot Token in the `Authorization` header to download.
- Rate limits: Slack Tier 3 = 50 req/min. Scripts include automatic retry with backoff.
