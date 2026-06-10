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
            data = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f'Failed to download {url}: 404 — 파일이 삭제되었거나 접근 불가', file=sys.stderr)
        else:
            print(f'Failed to download {url}: {e.code} {e.reason}', file=sys.stderr)
        return False
    except urllib.error.URLError as e:
        print(f'Failed to download {url}: {e.reason}', file=sys.stderr)
        return False
    # files.slack.com은 인증 실패 시 200 + HTML 로그인 페이지를 반환할 수 있다.
    head = data[:64].lstrip().lower()
    if head.startswith(b'<!doctype') or head.startswith(b'<html'):
        print(f'Failed to download {url}: 인증 실패(HTML 응답) — 토큰/권한 확인 필요', file=sys.stderr)
        return False
    os.makedirs(os.path.dirname(dest_path) or '.', exist_ok=True)
    with open(dest_path, 'wb') as f:
        f.write(data)
    return True


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
    parser.add_argument('--input', help='Input JSON file (output from fetch scripts)')
    parser.add_argument('--url', help='Single file url_private(_download) to fetch directly')
    parser.add_argument('--name', help='Filename to save as when using --url (default: derived from url)')
    parser.add_argument('--output-dir', default='./downloads', help='Directory to save files')
    parser.add_argument('--manifest', help='Save a download manifest JSON to this path')
    args = parser.parse_args()

    if bool(args.input) == bool(args.url):
        parser.error('exactly one of --input or --url is required')

    if args.url:
        name = args.name or os.path.basename(args.url.split('?', 1)[0]) or 'attached'
        files = [{'id': None, 'name': name, 'mimetype': None, 'url': args.url, 'ts': None}]
    else:
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
    # 한 건이라도 실패하면 비정상 종료해 호출자(에이전트)가 실패를 감지하게 한다.
    if success_count < len(files):
        sys.exit(1)


if __name__ == '__main__':
    main()
