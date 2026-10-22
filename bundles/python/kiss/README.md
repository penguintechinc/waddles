# kiss (Python)

`!kiss [@user]` -- a lighthearted flavor-text reply: the caller blows a kiss to someone. First-party
(`provider: builtin`, `app_id: waddles.core.example.kiss`), stateless relay, WASI component.

| | |
|---|---|
| Gating | Activation + PostHog flag `waddles.command-kiss` (default OFF, checked after the command matches) |
| Platforms | Twitch, Discord (`chat.message`) |
| State | none (no `kv`, no DB, no egress) |

## Commands

| Command | Behavior |
|---|---|
| `!kiss` | Solo reply (one of 4 lines) about the caller. |
| `!kiss @user` | Targeted reply (one of 4); exactly one @mention. |
| anything else | `Usage: !kiss [@user]` -- never silently dropped. |

The head is validated with `waddle_sdk.command.parse_command` + `CommandSpec` (bare = the action);
the target is the single `{user:<token>}` placeholder `waddle_sdk.identity.mention_tokens`
extracts.

## Permissions (V2) -- minimal, no `storage.kv`

| id | why |
|---|---|
| `flags.read` | feature flag gate |
| `chat.send:discord` | relay reply on Discord |
| `chat.send:twitch` | relay reply on Twitch |

## PII

Users are referenced by TOKEN only. Replies emit `{user:<token>}` placeholders that the egress
detokenizer renders; no raw handle is resolved, stored, or logged. Logs carry platform + reply
shape only. A non-token `event.actor` is replaced by "Someone".

## Test

```bash
cd bundles/python/kiss
python3 -m pytest --cov=app --cov-branch --cov-report=term-missing
```
