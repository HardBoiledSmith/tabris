#!/usr/bin/env python3
"""
Fetch Slack channel history with full message structure:
text, blocks, attachments, files.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def slack_get(token: str, method: str, params: dict) -> dict:
    """Call a Slack Web API method (GET-style via query params)."""
    url = f'https://slack.com/api/{method}?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f'HTTP error: {e.code} {e.reason}', file=sys.stderr)
        sys.exit(1)

    if not data.get('ok'):
        print(f'Slack API error: {data.get("error")}', file=sys.stderr)
        sys.exit(1)
    return data


def fetch_channel_history(
    token: str,
    channel: str,
    limit: int = 100,
    oldest: str = None,
    latest: str = None,
) -> list:
    messages = []
    cursor = None

    while True:
        params = {
            'channel': channel,
            'limit': min(limit - len(messages), 200),
            'inclusive': 'true',
        }
        if cursor:
            params['cursor'] = cursor
        if oldest:
            params['oldest'] = oldest
        if latest:
            params['latest'] = latest

        data = slack_get(token, 'conversations.history', params)
        batch = data.get('messages', [])
        messages.extend(batch)

        print(f'Fetched {len(messages)} messages so far...', file=sys.stderr)

        # Pagination
        meta = data.get('response_metadata', {})
        next_cursor = meta.get('next_cursor', '')
        if not next_cursor or len(messages) >= limit:
            break
        cursor = next_cursor

        # Respect rate limits (Tier 3: 50 req/min)
        time.sleep(1.2)

    return messages[:limit]


def main():
    parser = argparse.ArgumentParser(description='Fetch Slack channel messages with blocks, attachments, and files.')
    parser.add_argument('--token', required=True, help='Slack Bot Token (xoxb-...)')
    parser.add_argument('--channel', required=True, help='Channel ID (e.g. C1234567890)')
    parser.add_argument('--limit', type=int, default=100, help='Max messages to fetch')
    parser.add_argument('--oldest', help='Oldest timestamp (Unix seconds)')
    parser.add_argument('--latest', help='Latest timestamp (Unix seconds)')
    parser.add_argument('--output', help='Output JSON file (default: stdout)')
    parser.add_argument('--pretty', action='store_true', help='Pretty-print JSON output')
    args = parser.parse_args()

    messages = fetch_channel_history(
        token=args.token,
        channel=args.channel,
        limit=args.limit,
        oldest=args.oldest,
        latest=args.latest,
    )

    indent = 2 if args.pretty else None
    output = json.dumps(messages, ensure_ascii=False, indent=indent)

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(output)
        print(f'Saved {len(messages)} messages to {args.output}', file=sys.stderr)
    else:
        print(output)


if __name__ == '__main__':
    main()
