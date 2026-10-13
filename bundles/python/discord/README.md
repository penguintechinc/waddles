# discord (Python)

`!discord` -- a per-community Discord invite link. Anyone can read it; moderators set/clear it.
First-party (`provider: builtin`, `app_id: waddles.core.example.discord`), kv-only, WASI component.

| | |
|---|---|
| Flag | `waddles.command-discord` (default OFF) |
| Platforms | Twitch, Discord (`chat.message`) |
| State | `kv`, community-scoped (one key: the invite URL) |
| Catalog | registered in `bundles/core-bundles.yaml` |

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!discord` | anyone | Replies `Join our Discord: <url>`, or a friendly "no link set" message. |
| `!discord set <url>` | mod/broadcaster | Validates and stores the invite link. |
| `!discord reset` | mod/broadcaster | Clears the link (idempotent). |

URL rules: well-formed `http://`/`https://` with a host, no whitespace/control chars, no embedded
credentials (`user@host` spoofing), max 512 chars. Validation is generic -- a community may use
a vanity domain -- so a link whose host isn't `discord.gg`/`discord.com`/`discordapp.com` is
still stored, with a soft warning in the reply. Bad input gets a clear reply, nothing is stored.

```text
!discord                       -> No Discord link has been set yet. A moderator can set one ...
!discord set https://discord.gg/abc123   (mod) -> Discord link updated.
!discord                       -> Join our Discord: https://discord.gg/abc123
!discord set discord.gg/abc    (mod) -> The invite link must start with https:// (or http://).
!discord set https://discord.gg/abc   (viewer) -> Only the broadcaster or a moderator can ...
!discord reset                 (mod) -> Discord link cleared.
```

## Permissions (V2)

| id | why |
|---|---|
| `flags.read` | gates the command behind `waddles.command-discord` |
| `storage.kv` | persists the per-community Discord invite link |

## State keys

| Key | Scope | Value |
|---|---|---|
| `discord.invite` | per-community | invite URL, UTF-8 |

Literal key; the host scopes it by `(tenant, community, app_id)`. Dot-separated, never `:`
(gh-631 -- the `kv` host rejects it). No actor identity is stored.

## Behavior notes

- **Mod gate fails closed.** Needs a strict `bool` `True` in `is_mod`/`is_broadcaster`; missing,
  string (`"true"`) or other truthy non-bool values are refused. Discord messages carry no role
  info today, so `set`/`reset` there are refused until ingest forwards it (same as `count`).
- **Fail loud.** A `kv` error or a corrupt stored value logs `discord.kv_failure` at ERROR and
  replies "something went wrong" -- never a silent success.
- **PII-free logs.** Only platform/op/exception-type are logged -- never the URL, actor or text.
- Cost: one `kv` op per invocation; messages whose first token isn't `!discord` cost zero.
- Grammar via `waddle_sdk.command` (`CommandSpec(name="discord")`); a parser usage error is
  replied with its message plus usage.

## Test

```bash
cd bundles/python/discord
python3 -m pytest --cov=app --cov-branch --cov-report=term-missing
```

`conftest.py` wires `src/` and `sdk/waddle-sdk/src` onto `sys.path`; no WASM build needed.
Per-worktree venv recipe: `docs/APP_BUNDLE_AUTHORING.md` Appendix.
