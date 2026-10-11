# countdown (Python)

`!countdown` -- one per-community countdown target. Set a time (ISO-8601 or relative like `3d2h`)
and an optional label; bare `!countdown` reports time remaining (or how long ago it passed).
First-party (`provider: builtin`, `app_id: waddles.core.example.countdown`), kv-only, WASI
component. No scheduler: "remaining" is recomputed from the host clock on every report.

| | |
|---|---|
| Flag | `waddles.command-countdown` (default OFF) |
| Platforms | Twitch, Discord (`chat.message`) |
| State | `kv`, community-scoped; **community is required** (no tenant-wide fallback) |
| Catalog | registered in `bundles/core-bundles.yaml` |

## Commands

| Command | Verb | Who | Behavior |
|---|---|---|---|
| `!countdown` | bare | anyone | `2d 3h 14m until <label>.` or `<label> happened 1h 5m ago.`; "No countdown set" if none. |
| `!countdown set <time> [label]` | `set` | anyone (v1: no mod gate) | Overwrites the stored target. `<time>` = ISO-8601 (naive = UTC) or `d`/`h`/`m`/`s` components (`3d`, `2h30m`). |
| `!countdown reset` | `reset` | anyone | Clears the target. (The shared verb set has no `clear`; `reset` is the accepted spelling.) |

Any other grammar-legal verb, extra arguments after `reset`, a bad time token, or a malformed
command replies with usage -- never silently dropped. A target in the past is accepted and
reported as already happened.

```text
!countdown set 3d2h New Year      -> Countdown set: 3d 2h 0m until New Year.
!countdown                        -> 3d 1h 59m until New Year.
!countdown set 2026-12-25T00:00:00Z Xmas
!countdown reset                  -> Countdown cleared -- the next !countdown set starts a fresh one.
!countdown set nonsense           -> 'nonsense' is not a valid ISO-8601 timestamp or relative duration (e.g. 3d2h) Usage: ...
```

## Permissions (V2)

| id | why |
|---|---|
| `flags.read` | gates the command behind `waddles.command-countdown` |
| `storage.kv` | persists the per-community countdown target time |

## State

| Key | Scope | TTL | Value |
|---|---|---|---|
| `countdown.target` | per-community | none | JSON `{"target_ms": int, "label": str \| null}` |

Colon-free key (gh-631) -- the `kv` host rejects `:`.

## Behavior notes

- **Fail loud on backend errors.** A `kv` get/set/delete error logs `countdown.kv_error` at
  ERROR (error class only), replies "temporarily unavailable", then raises.
- **Corrupt stored target is logged, not raised.** Non-JSON, wrong shape or non-int `target_ms`
  logs `countdown.state_corrupt` at ERROR and the command replies as if no target is set; the
  next `set` overwrites it. (A deliberate self-heal inherited from `first`.)
- **PII-free logs.** Logs carry action, community id, target epoch and the exception *class* --
  never the actor, the typed time token or the label (gh-674: usage-error logging once echoed
  `str(exc)`).

## Test

```bash
cd bundles/python/countdown
python3 -m pytest --cov=app --cov-branch --cov-report=term-missing
```

`conftest.py` wires `src/` and `sdk/waddle-sdk/src` onto `sys.path`; no WASM build needed.
