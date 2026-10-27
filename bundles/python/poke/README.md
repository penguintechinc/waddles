# poke (Python)

`!poke [@user]` -- a lighthearted flavor-text reply: the caller pokes someone to get their attention. First-party
(`provider: builtin`, `app_id: waddles.core.example.poke`), stateless relay, WASI component.

| | |
|---|---|
| Gating | Activation only (installed -> enabled -> activated); no feature flag |
| Platforms | Twitch, Discord (`chat.message`) |
| State | none (no `kv`, no DB, no egress) |

## Commands

| Command | Behavior |
|---|---|
| `!poke` | Solo reply (one of 4 lines) about the caller. |
| `!poke @user` | Targeted reply (one of 4); exactly one @mention. |
| anything else | `Usage: !poke [@user]` -- never silently dropped. |

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
cd bundles/python/poke
python3 -m pytest --cov=app --cov-branch --cov-report=term-missing
```
