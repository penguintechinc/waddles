# dance (Python)

`!dance [@user]` -- a lighthearted flavor-text reply: the caller busts out a dance move. First-party
(`provider: builtin`, `app_id: waddles.core.example.dance`), stateless relay, WASI component.

| | |
|---|---|
| Gating | Activation only (installed -> enabled -> activated); no feature flag |
| Platforms | Twitch, Discord (`chat.message`) |
| State | none (no `kv`, no DB, no egress) |

## Commands

| Command | Behavior |
|---|---|
| `!dance` | Solo reply (one of 4 lines) about the caller. |
| `!dance @user` | Targeted reply (one of 4); exactly one @mention. |
| anything else | `Usage: !dance [@user]` -- never silently dropped. |

The head is validated with `waddle_sdk.command.parse_command` + `CommandSpec` (bare = the action);
the target is the single `{user:<token>}` placeholder `waddle_sdk.identity.mention_tokens`
extracts.

## Permissions (V2) -- minimal, no `storage.kv`

| id | why |
|---|---|
| `chat.send:discord` | relay reply on Discord |
| `chat.send:twitch` | relay reply on Twitch |

## PII

Users are referenced by TOKEN only. Replies emit `{user:<token>}` placeholders that the egress
detokenizer renders; no raw handle is resolved, stored, or logged. Logs carry platform + reply
shape only. A non-token `event.actor` is replaced by "Someone".

## Test

```bash
cd bundles/python/dance
python3 -m pytest --cov=app --cov-branch --cov-report=term-missing
```
