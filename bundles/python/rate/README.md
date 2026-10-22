# rate (Python)

Deterministic, SFW X/10 ratings with tasteful flavor text; `@` is defused so a rating cannot mass-ping. First-party (`provider: builtin`, `app_id: waddles.core.example.rate`), stateless WASI component, modeled on `eightball`.

| | |
|---|---|
| Gating | Feature flag `waddles.command-rate` (default OFF, checked after the command matches) |
| Platforms | Twitch, Discord (`chat.message`) |
| State | none (no `kv`, no DB, no egress, no external API) |
| Catalog | NOT yet registered in `bundles/core-bundles.yaml` (follow-up) |

## Commands

| Command | Behavior |
|---|---|
| `!rate <thing>` | `I rate <thing> X/10 -- <flavor>`; X is deterministic per (case/space-normalized) input. |
| `!rate` | Usage reply. |

## Permissions (V2)

| id | why |
|---|---|
| `flags.read` | feature-flag gate |
| `chat.send:discord` / `chat.send:twitch` | reply on the origin platform via `relay.push` |

## Behavior notes

- **PII-free logs.** Only platform/op are logged -- never the actor or the user's text.
- Grammar via `waddle_sdk.command.parse_command`/`CommandSpec` (bare = action); free-text args are taken from the raw message since the shared grammar only knows verbs.

## Test

```bash
cd bundles/python/rate
python3 -m pytest --cov=app --cov-branch --cov-report=term-missing
```
