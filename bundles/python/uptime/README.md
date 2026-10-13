# uptime (Python)

`!uptime` -- humanized time since this community first activated the bundle (`up 3d 4h 12m`).
First-party (`provider: builtin`, `app_id: waddles.core.example.uptime`), kv-only, WASI component.

| | |
|---|---|
| Flag | `waddles.command-uptime` (default OFF) |
| Platforms | Twitch, Discord (`chat.message`) |
| State | `kv`, community-scoped (one key: the start timestamp) |
| Catalog | registered in `bundles/core-bundles.yaml` |

## Semantics: "time since first activation"

A WASM bundle cannot see the OS process start, so this is **not** process or pod uptime. The
first `!uptime` in a community with no stored start records `clock.now_millis()` under
`uptime.start` and reports `up 0s`; every later call reports `now - uptime.start`. Resetting
means deleting that key (no in-chat reset command -- state is never silently overwritten).

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!uptime` | anyone | `up 3d 4h 12m` (`up 45s` under a minute; leading zero units dropped) |
| `!uptime <anything>` | anyone | Replies with usage (`Usage: !uptime`) -- never silently dropped. |

No sub-modules. Grammar via `waddle_sdk.command` (`CommandSpec(name="uptime")`).

## Permissions (V2)

| id | why |
|---|---|
| `storage.kv` | persists the per-community start timestamp |
| `flags.read` | gates the command behind `waddles.command-uptime` |
| `platform.clock` | reads the current time to compute elapsed time (`clock` is not a catalog id) |

## State

| Key | Scope | Value |
|---|---|---|
| `uptime.start` | per-community | start epoch milliseconds as ASCII integer |

Colon-free key (gh-631 -- the `kv` host rejects `:`). Host scopes by tenant/community/app_id.

## Behavior notes

- **Fail loud.** A `kv` backend error or a corrupt stored value logs `uptime.kv_failure` at
  ERROR and replies "something went wrong" -- the stored start is never overwritten on corruption.
- **PII-free logs.** Only op names and exception types; never message text or the actor.
- **Clock skew.** A stored start in the future clamps to `up 0s`.
- Eager host imports (`kv`, `log`, `relay`, `clock`, `feature_enabled`) at module top, required
  for componentize-py wizening (`docs/reference/bundle-host-imports.md`).
- `transform()` builds the reply; `dispatch()` is a pure relay.

## Test

```bash
cd bundles/python/uptime
uv venv -p 3.13 .venv
uv pip install --python .venv/bin/python3 -e ../../../sdk/waddle-sdk -e ../../../libs/flask_core pytest-cov mypy ruff
.venv/bin/python3 -m pytest --cov=app --cov-branch --cov-report=term-missing
```

`conftest.py` wires `src/` and `sdk/waddle-sdk/src` onto `sys.path`; no WASM build needed.
