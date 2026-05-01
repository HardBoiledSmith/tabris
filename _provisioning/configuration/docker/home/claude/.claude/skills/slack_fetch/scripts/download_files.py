#!/usr/bin/env python3
"""
Download files attached to Slack messages.
Slack file URLs require authentication via Bearer token.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def download_file(token: str, url: str, dest_path: str) -> bool:
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
    try:
        with urllib.request.urlopen(req) as resp:
            os.makedirs(os.path.dirname(dest_path) or '.', exist_ok=True)
            with open(dest_path, 'wb') as f:
                f.write(resp.read())
        return True
    except urllib.error.HTTPError as e:
        print(f'Failed to download {url}: {e.code} {e.reason}', file=sys.stderr)
        return False


def extract_files(messages: list) -> list:
    """Pull all file entries from message list."""
    files = []
    for msg in messages:
        for file_obj in msg.get('files', []):
            url = file_obj.get('url_private') or file_obj.get('url_private_download')
            if url:
                files.append(
                    {
                        'id': file_obj.get('id'),
                        'name': file_obj.get('name', file_obj.get('id')),
                        'mimetype': file_obj.get('mimetype'),
                        'url': url,
                        'ts': msg.get('ts'),
                    }
                )
    return files


def main():
    parser = argparse.ArgumentParser(description='Download files from Slack messages.')
    parser.add_argument('--token', required=True, help='Slack Bot Token (xoxb-...)')
    parser.add_argument('--input', required=True, help='Input JSON file (output from fetch scripts)')
    parser.add_argument('--output-dir', default='./downloads', help='Directory to save files')
    parser.add_argument('--manifest', help='Save a download manifest JSON to this path')
    args = parser.parse_args()

    with open(args.input, encoding='utf-8') as f:
        messages = json.load(f)

    files = extract_files(messages)
    print(f'Found {len(files)} files to download.', file=sys.stderr)

    manifest = []
    for file_info in files:
        safe_name = file_info['name'].replace('/', '_')
        dest = os.path.join(args.output_dir, safe_name)
        success = download_file(args.token, file_info['url'], dest)
        manifest.append({**file_info, 'local_path': dest if success else None, 'success': success})
        status = '✓' if success else '✗'
        print(f'  {status} {safe_name}', file=sys.stderr)

    if args.manifest:
        with open(args.manifest, 'w', encoding='utf-8') as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        print(f'Manifest saved to {args.manifest}', file=sys.stderr)

    success_count = sum(1 for m in manifest if m['success'])
    print(f'\nDownloaded {success_count}/{len(files)} files.', file=sys.stderr)


if __name__ == '__main__':
    main()
