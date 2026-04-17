#!/usr/bin/env python3
"""
Fetch all replies in a Slack thread with full message structure.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def slack_get(token: str, method: str, params: dict) -> dict:
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


def fetch_thread(token: str, channel: str, thread_ts: str) -> list:
    messages = []
    cursor = None

    while True:
        params = {
            'channel': channel,
            'ts': thread_ts,
            'limit': 200,
        }
        if cursor:
            params['cursor'] = cursor

        data = slack_get(token, 'conversations.replies', params)
        batch = data.get('messages', [])
        messages.extend(batch)

        print(f'Fetched {len(messages)} messages in thread...', file=sys.stderr)

        meta = data.get('response_metadata', {})
        next_cursor = meta.get('next_cursor', '')
        if not next_cursor:
            break
        cursor = next_cursor
        time.sleep(1.2)

    return messages


def main():
    parser = argparse.ArgumentParser(description='Fetch all replies in a Slack thread.')
    parser.add_argument('--token', required=True, help='Slack Bot Token (xoxb-...)')
    parser.add_argument('--channel', required=True, help='Channel ID')
    parser.add_argument('--thread_ts', required=True, help='Thread parent timestamp')
    parser.add_argument('--output', help='Output JSON file (default: stdout)')
    parser.add_argument('--pretty', action='store_true', help='Pretty-print JSON')
    args = parser.parse_args()

    messages = fetch_thread(
        token=args.token,
        channel=args.channel,
        thread_ts=args.thread_ts,
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
