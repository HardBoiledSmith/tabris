#!/usr/bin/env python3
"""
Search Slack messages and return full message objects.
NOTE: Requires a user token (xoxp-...) with search:read scope.
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
        error = data.get('error')
        if error == 'not_allowed_token_type':
            print(
                'Error: search.messages requires a USER token (xoxp-...), not a bot token.',
                file=sys.stderr,
            )
        else:
            print(f'Slack API error: {error}', file=sys.stderr)
        sys.exit(1)
    return data


def search_messages(token: str, query: str, count: int = 100) -> list:
    results = []
    page = 1

    while len(results) < count:
        params = {
            'query': query,
            'count': min(count - len(results), 100),
            'page': page,
            'sort': 'timestamp',
            'sort_dir': 'desc',
        }
        data = slack_get(token, 'search.messages', params)
        messages_data = data.get('messages', {})
        matches = messages_data.get('matches', [])
        results.extend(matches)

        print(f'Fetched {len(results)} results so far...', file=sys.stderr)

        paging = messages_data.get('paging', {})
        if page >= paging.get('pages', 1):
            break
        page += 1
        time.sleep(1.2)

    return results[:count]


def main():
    parser = argparse.ArgumentParser(description='Search Slack messages (requires user token xoxp-).')
    parser.add_argument('--token', required=True, help='Slack User Token (xoxp-...)')
    parser.add_argument('--query', required=True, help='Search query string')
    parser.add_argument('--count', type=int, default=100, help='Max results to fetch')
    parser.add_argument('--output', help='Output JSON file (default: stdout)')
    parser.add_argument('--pretty', action='store_true', help='Pretty-print JSON')
    args = parser.parse_args()

    results = search_messages(
        token=args.token,
        query=args.query,
        count=args.count,
    )

    indent = 2 if args.pretty else None
    output = json.dumps(results, ensure_ascii=False, indent=indent)

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(output)
        print(f'Saved {len(results)} results to {args.output}', file=sys.stderr)
    else:
        print(output)


if __name__ == '__main__':
    main()
