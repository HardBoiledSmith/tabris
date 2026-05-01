#!/usr/bin/env python3
"""
Extract specific fields from fetched Slack message JSON.
Supports dot-notation for nested fields (e.g. files.url_private).
"""

import argparse
import json
import sys


def get_nested(obj, path: str):
    """Traverse a dot-notation path in a dict/list."""
    parts = path.split('.')
    result = obj
    for part in parts:
        if isinstance(result, dict):
            result = result.get(part)
        elif isinstance(result, list):
            result = [get_nested(item, part) for item in result if isinstance(item, dict)]
        else:
            return None
        if result is None:
            return None
    return result


def extract_from_messages(messages: list, fields: list) -> list:
    extracted = []
    for msg in messages:
        entry = {'ts': msg.get('ts'), 'user': msg.get('user')}
        for field in fields:
            value = get_nested(msg, field)
            if value is not None:
                entry[field] = value
        extracted.append(entry)
    return extracted


def main():
    parser = argparse.ArgumentParser(description='Extract specific fields from Slack message JSON.')
    parser.add_argument('input', help='Input JSON file (output from fetch scripts)')
    parser.add_argument(
        '--fields',
        nargs='+',
        required=True,
        help='Fields to extract (e.g. blocks attachments files.url_private text)',
    )
    parser.add_argument('--output', help='Output JSON file (default: stdout)')
    parser.add_argument('--pretty', action='store_true', help='Pretty-print JSON')
    parser.add_argument(
        '--skip-empty',
        action='store_true',
        help='Skip messages where all requested fields are empty/null',
    )
    args = parser.parse_args()

    with open(args.input, encoding='utf-8') as f:
        messages = json.load(f)

    extracted = extract_from_messages(messages, args.fields)

    if args.skip_empty:
        extracted = [e for e in extracted if any(e.get(field) for field in args.fields)]

    indent = 2 if args.pretty else None
    output = json.dumps(extracted, ensure_ascii=False, indent=indent)

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(output)
        print(f'Extracted {len(extracted)} messages to {args.output}', file=sys.stderr)
    else:
        print(output)


if __name__ == '__main__':
    main()
