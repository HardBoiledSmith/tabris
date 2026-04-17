# Slack API Error Reference

## Common Errors and Fixes

### `not_in_channel`
Bot is not a member of the channel.
**Fix**: Invite the bot: `/invite @your-bot-name` in Slack.

### `channel_not_found`
Channel ID is wrong or the bot has no access.
**Fix**: Use the channel's ID (starts with `C` for public, `G` for private, `D` for DMs), not its name.

### `invalid_auth`
The token is invalid or revoked.
**Fix**: Regenerate the token from https://api.slack.com/apps → OAuth & Permissions.

### `missing_scope`
The token lacks a required permission scope.
**Fix**: Add the scope in Slack App settings → OAuth & Permissions → Bot Token Scopes, then reinstall the app.

| Operation | Required Scope |
|---|---|
| Public channel history | `channels:history` |
| Private channel history | `groups:history` |
| DM history | `im:history` |
| Group DM history | `mpim:history` |
| File metadata/download | `files:read` |
| Search messages | `search:read` (user token only) |

### `ratelimited`
Too many requests. Slack returns a `Retry-After` header.
**Fix**: Scripts automatically sleep 1.2s between requests. If you hit this, increase the sleep or reduce `--limit`.

### `not_allowed_token_type`
Using a bot token (`xoxb-`) for an endpoint that requires a user token (`xoxp-`).
**Fix**: `search.messages` only works with user tokens. Get one from OAuth & Permissions → User Token Scopes.

### `no_permission`
Token doesn't have access to the requested resource.
**Fix**: Check that the app has been installed to the workspace and the bot is in the channel.

## Token Types

| Token | Prefix | Use Case |
|---|---|---|
| Bot Token | `xoxb-` | Most operations (history, files) |
| User Token | `xoxp-` | Search, user-specific data |
| App-Level Token | `xapp-` | Socket Mode only |

## Rate Limits by Tier

| Tier | Limit | Affected Methods |
|---|---|---|
| Tier 1 | 1 req/min | Rarely used |
| Tier 2 | 20 req/min | `search.messages` |
| Tier 3 | 50 req/min | `conversations.history`, `conversations.replies` |
| Tier 4 | 100 req/min | Most other methods |
