# riddle (Python)

Static-list riddles; answers revealed by number so no state is stored. First-party (`provider: builtin`, `app_id: waddles.core.example.riddle`), stateless WASI component, modeled on `eightball`.

| | |
|---|---|
| Gating | Activation only (installed -> enabled -> activated); no feature flag |
| Platforms | Twitch, Discord (`chat.message`) |
| State | none (no `kv`, no DB, no egress, no external API) |
| Catalog | NOT yet registered in `bundles/core-bundles.yaml` (follow-up) |

## Commands

| Command | Behavior |
|---|---|
| `!riddle` | Random numbered riddle from a static list (answer not shown). |
| `!riddle answer <n>` | Reveals riddle n's answer (stateless: n comes from the riddle post). |

## Permissions (V2)

| id | why |
|---|---|
| `chat.send:discord` / `chat.send:twitch` | reply on the origin platform via `relay.push` |

## Behavior notes

- **PII-free logs.** Only platform/op are logged -- never the actor or the user's text.
- Grammar via `waddle_sdk.command.parse_command`/`CommandSpec` (bare = action); free-text args are taken from the raw message since the shared grammar only knows verbs.

## Test

```bash
cd bundles/python/riddle
python3 -m pytest --cov=app --cov-branch --cov-report=term-missing
```
