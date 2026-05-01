#!/usr/bin/env python3
import argparse
import os
import re

import requests
from requests.auth import HTTPBasicAuth

JIRA_BASE_URL = 'https://hbsmith.atlassian.net'
AGILE_API = f'{JIRA_BASE_URL}/rest/agile/1.0'
REST_API = f'{JIRA_BASE_URL}/rest/api/3'

_session: requests.Session | None = None


def _s() -> requests.Session:
    assert _session is not None, 'Session not initialized'
    return _session


def _get(url: str, **params) -> dict:
    r = _s().get(url, params=params or None)
    r.raise_for_status()
    return r.json()


def _post(url: str, body: dict) -> dict:
    r = _s().post(url, json=body)
    r.raise_for_status()
    return r.json()


def parse_args():
    p = argparse.ArgumentParser(description='Daily Jira AS-IS / TO-BE summary for a board assignee.')
    p.add_argument(
        '--days', type=int, default=3, help='Look back N days by updated timestamp (0 = disabled). Default: 3.'
    )
    p.add_argument('--sprint-name', default=None, help='Sprint name (omit to auto-detect the active sprint).')
    p.add_argument('--assignee', default='제상 윤', help='Assignee display name. Default: 제상 윤.')
    g = p.add_mutually_exclusive_group(required=False)
    g.add_argument('--board-id', type=int, help='Numeric board ID.')
    g.add_argument('--board-name', default='Dev Board', help='Board name (exact match). Default: Dev Board.')
    return p.parse_args()


def find_board_id(name: str) -> int:
    start = 0
    while True:
        data = _get(f'{AGILE_API}/board', name=name, startAt=start, maxResults=50)
        matches = [b for b in data.get('values', []) if b.get('name') == name]
        if len(matches) == 1:
            return int(matches[0]['id'])
        if len(matches) > 1:
            raise ValueError(f"Board name '{name}' is ambiguous; use --board-id.")
        if data.get('isLast', True):
            raise ValueError(f"Board '{name}' not found.")
        start += len(data.get('values', []))


def active_sprint_name(board_id: int) -> str:
    data = _get(f'{AGILE_API}/board/{board_id}/sprint', state='active', maxResults=10)
    sprints = data.get('values', [])
    if not sprints:
        raise ValueError(f'No active sprint on board {board_id}.')
    if len(sprints) > 1:
        names = ', '.join(f"'{s.get('name', s['id'])}'" for s in sprints)
        raise ValueError(f'Multiple active sprints: {names} — use --sprint-name.')
    name = sprints[0].get('name')
    if not name:
        raise ValueError(f'Active sprint on board {board_id} has no name.')
    return name


def done_status_names(board_config: dict) -> list[str]:
    """Return Done-column status names via a single bulk status fetch."""
    ids = {
        str(s['id'])
        for col in board_config.get('columnConfig', {}).get('columns', [])
        for s in col.get('statuses', [])
        if 'id' in s
    }
    if not ids:
        return []
    r = _s().get(f'{REST_API}/status')
    r.raise_for_status()
    return sorted(
        s['name'] for s in r.json() if str(s.get('id')) in ids and s.get('statusCategory', {}).get('key') == 'done'
    )


_ORDER_BY = re.compile(r'\bORDER\s+BY\b', re.IGNORECASE)


def _split_order_by(jql: str) -> tuple[str, str | None]:
    m = _ORDER_BY.search(jql)
    if not m:
        return jql.strip(), None
    return jql[: m.start()].strip(), jql[m.start() :].strip()


def _q(v: str) -> str:
    return v.replace('"', '\\"')


def build_jql(base: str, order_by: str | None, sprint: str, assignee: str, status_clause: str, days: int) -> str:
    parts = [f'({base})'] if base else []
    parts += [
        f'Sprint = "{_q(sprint)}"',
        f'assignee = "{_q(assignee)}"',
        status_clause,
    ]
    if days > 0:
        parts.append(f'updated >= -{days}d')
    body = ' AND '.join(parts)
    return f'{body} {order_by}' if order_by else f'{body} ORDER BY Rank ASC'


def search_issues(jql: str) -> list[dict]:
    data = _post(f'{REST_API}/search/jql', {'jql': jql, 'fields': ['key', 'summary']})
    return data.get('issues', [])


def print_issues(issues: list[dict]) -> None:
    for issue in issues:
        print(f'- {issue["key"]} {issue["fields"]["summary"]}')


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f'Environment variable {name} is required.')
    return value


def main():
    global _session

    admin_id = require_env('JIRA_API_USERNAME')
    token = require_env('JIRA_API_KEY')

    _session = requests.Session()
    _session.auth = HTTPBasicAuth(admin_id, token)
    _session.headers.update({'Accept': 'application/json', 'Content-Type': 'application/json'})

    args = parse_args()

    board_id = args.board_id or find_board_id(args.board_name)

    if args.sprint_name:
        sprint = args.sprint_name
    else:
        sprint = active_sprint_name(board_id)
        print(f'[Active sprint: {sprint}]')

    config = _get(f'{AGILE_API}/board/{board_id}/configuration')

    swimlane = config.get('swimlane', {}).get('type')
    if swimlane and swimlane != 'assignee':
        raise ValueError(f"Board {board_id} uses '{swimlane}' swimlanes; only 'assignee' is supported.")

    filter_id = config.get('filter', {}).get('id')
    if not filter_id:
        raise ValueError(f'Board {board_id} configuration exposes no filter id.')

    raw_jql = _get(f'{REST_API}/filter/{filter_id}').get('jql', '')
    if not raw_jql:
        raise ValueError(f'Filter {filter_id} has no JQL.')
    base, order_by = _split_order_by(raw_jql)

    done_statuses = done_status_names(config)
    done_clause = (
        'statusCategory = Done'
        if not done_statuses
        else 'status in ({})'.format(', '.join(f'"{_q(s)}"' for s in done_statuses))
    )
    inprogress_clause = 'statusCategory = "In Progress"'

    print('\n## AS-IS')
    print_issues(search_issues(build_jql(base, order_by, sprint, args.assignee, done_clause, args.days)))

    print('\n## TO-BE')
    print_issues(search_issues(build_jql(base, order_by, sprint, args.assignee, inprogress_clause, args.days)))


if __name__ == '__main__':
    main()
