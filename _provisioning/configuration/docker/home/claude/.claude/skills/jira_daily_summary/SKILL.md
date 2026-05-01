---
name: jira_daily_summary
description: Generate daily Jira AS-IS (Done) and TO-BE (In Progress) issue lists for the current active sprint and assignee. Auto-detects the active sprint from the board — no need to specify a sprint name. Use when asked to run a daily Jira summary, daily standup report, or board status for an assignee.
---

# Jira Daily Summary

## Overview

Produce two issue lists for the active sprint and assignee that mirror an assignee-based board's swimlanes:
- AS-IS: Done statuses (derived from board columns; falls back to statusCategory = Done)
- TO-BE: In Progress statuses (statusCategory = "In Progress")

The active sprint is fetched automatically from the board. You do not need to specify a sprint name unless you want to override it.

## Preconditions

- Required environment variables:
  - `JIRA_API_USERNAME`
  - `JIRA_API_KEY`
- Network access to `https://hbsmith.atlassian.net` is required.
- The target board must use assignee swimlanes.

## Post-deploy setup

1) Set credentials in environment variables
- `export JIRA_API_USERNAME="your-email@example.com"`
- `export JIRA_API_KEY="your-api-token"`

## Workflow

1) Decide board identifier
- Prefer `--board-id` if known to avoid name ambiguity.
- Use `--board-name` only when the board name is an exact match.

2) Specify assignee (and optionally override sprint or lookback window)
- `--assignee` is required.
- `--sprint-name` is optional; omit to auto-detect the board's active sprint.
- `--days` is optional; use `0` to disable the updated-time filter.

3) Run the script

## Commands

Auto-detect active sprint by board name:
```bash
python3 jira_daily_summary/scripts/create_daily.py \
  --assignee "Assignee Name" \
  --board-name "Board Name" \
  --days 5
```

Auto-detect active sprint by board id:
```bash
python3 jira_daily_summary/scripts/create_daily.py \
  --assignee "Assignee Name" \
  --board-id 123 \
  --days 5
```

Override sprint name explicitly:
```bash
python3 jira_daily_summary/scripts/create_daily.py \
  --assignee "제상 윤" \
  --board-name "Dev Board" \
  --sprint-name "Dev 2026 W3/4/5" \
  --days 5
```

## Output

- If the sprint was auto-detected, prints `[Auto-detected active sprint: <name>]` first.
- Prints two sections: `## AS-IS` and `## TO-BE`.
- Each section lists issues as `- KEY Summary`.

## Troubleshooting

- "No active sprint found" → the board has no sprint in `state=active`; specify `--sprint-name` manually.
- "Multiple active sprints found" → use `--sprint-name` to disambiguate.
- "Board name is ambiguous" → use `--board-id`.
- "Board swimlane type is ..." → only assignee-based swimlanes are supported.
- HTTP 401/403 → check `JIRA_API_USERNAME` / `JIRA_API_KEY`.

## Resources

- `scripts/create_daily.py`: Queries Jira and prints AS-IS / TO-BE lists.
