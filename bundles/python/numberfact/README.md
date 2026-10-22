# numberfact (Python)

Static fun number facts; no external API. First-party (`provider: builtin`, `app_id: waddles.core.example.numberfact`), stateless WASI component, modeled on `eightball`.

| | |
|---|---|
| Gating | Activation only (installed -> enabled -> activated); no feature flag |
| Platforms | Twitch, Discord (`chat.message`) |
| State | none (no `kv`, no DB, no egress, no external API) |
| Catalog | Registered in `bundles/core-bundles.yaml` (`1.0.0`, default `stage` world) |

## Commands

| Command | Behavior |
|---|---|
| `!numberfact` | Random curated number fact. |
| `!numberfact <n>` | Curated fact if n is notable, else a deterministic fact computed from n's properties (|n| <= 10^12). |

## Permissions (V2)

| id | why |
|---|---|
| `chat.send:discord` / `chat.send:twitch` | reply on the origin platform via `relay.push` |

## Behavior notes

- **PII-free logs.** Only platform/op are logged -- never the actor or the user's text.
- Grammar via `waddle_sdk.command.parse_command`/`CommandSpec` (bare = action); free-text args are taken from the raw message since the shared grammar only knows verbs.

## Test

```bash
cd bundles/python/numberfact
python3 -m pytest --cov=app --cov-branch --cov-report=term-missing
```
