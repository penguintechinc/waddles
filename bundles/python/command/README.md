# command (Python)

`!command` -- generic, per-community custom TEXT commands and (config-only) timers. Mods define
`!greet` with some text; viewers invoke it. First-party (`provider: builtin`,
`app_id: waddles.core.example.command`), kv-only, WASI component.

| | |
|---|---|
| Flag | `waddles.command-customcommands` (default OFF) |
| Platforms | Twitch, Discord (`chat.message`) |
| State | `kv`, community-scoped; **community is required** (no tenant-wide fallback) |
| Catalog | registered in `bundles/core-bundles.yaml` |

## Commands

| Command | Verb | Who | Behavior |
|---|---|---|---|
| `!command set !<name> <text>` | `set` | mod/broadcaster | Upserts `name -> text`. `$(username)` in the text renders as the caller. |
| `!command remove !<name>` | `remove` | mod/broadcaster | Deletes `name`; "no custom command named" if absent. |
| `!command list` | `list` | anyone | Sorted names, capped at 15 (`...and N more`). |
| `!command timer !<name> set <interval>` | `timer ... set` | mod/broadcaster | Stores interval `30s`/`5m`/`1h` (10s-24h); requires the command to exist. |
| `!command timer !<name> enable\|disable` | `timer` | mod/broadcaster | Flips `enabled`; requires a prior `set`. |
| `!<name>` | -- | anyone | Replies the stored text. Unknown/invalid names return `None` (not ours). |

Names: lowercase letters/digits/`-`/`_`, 1-32, `command` reserved. Bare `!command`, unknown
subcommands and malformed arguments reply with usage -- never silently dropped.

```text
!command set !greet hi $(username)   (mod) -> command saved: !greet
!greet                                     -> hi viewer-1
!command list                              -> custom commands: !greet
!command timer !greet set 5m         (mod) -> timer set for !greet every 300s -- saved, but periodic firing is not wired yet (tracked: gh-613)
```

**Timer firing gap (gh-613).** `timer` persists config only: no scheduled/periodic trigger
exists in the `stage` WIT world, so nothing fires. Every timer reply says so explicitly.

## Permissions (V2)

| id | why |
|---|---|
| `flags.read` | gates the command behind `waddles.command-customcommands` |
| `storage.kv` | persists the per-community custom command registry |

## State

| Key | Scope | Value |
|---|---|---|
| `command.registry.<community>` | per-community | JSON `{name: text}` |
| `command.timers.<community>` | per-community | JSON `{name: {interval_seconds, enabled}}` |

Colon-free keys (gh-631) -- the `kv` host rejects `:`. `transform()` does one read-only
registry `kv.get` per `!<name>` token (a dynamic set cannot be declared in the manifest's static
prefix filter); all writes happen in `dispatch()`.

## Behavior notes

- **Mod gate fails closed.** `set`/`remove`/`timer` need truthy `is_mod`/`is_broadcaster`; absent
  fields (Discord today) are denied. `list` and direct invocation are open to anyone.
- **Fail loud.** A `kv` backend error or corrupt (non-JSON) registry logs
  `command.{transform,dispatch}.kv_error` at ERROR and replies "temporarily unavailable" --
  never a silent drop, and corrupt bytes are never overwritten by a subsequent write.
- **PII-free logs.** Logs carry the command token, action and host error text -- never the
  actor or any typed/stored text (gh-674). `$(username)` is rendered transiently into the reply
  only.

## Test

```bash
cd bundles/python/command
python3 -m pytest --cov=app --cov-branch --cov-report=term-missing
```

`conftest.py` wires `src/` and `sdk/waddle-sdk/src` onto `sys.path`; no WASM build needed.
