# count (Python)

`!count` -- dynamic, per-community named counters. `!count add die` creates a counter; once it
exists, `!die` is a live command (read / `add` / `sub` / `set`). First-party
(`provider: builtin`, `app_id: waddles.core.example.count`), kv-only, WASI component.

| | |
|---|---|
| Flag | `waddles.command-count` (default OFF) |
| Platforms | Twitch, Discord (`chat.message`) |
| State | `kv`, community-scoped (names + integer values) |
| Catalog | registered in `bundles/core-bundles.yaml` |

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!count add <name>` | mod/broadcaster | Creates counter `name` at 0 (`!die` and `die` both yield `die`). |
| `!count remove <name>` | mod/broadcaster | Deletes the counter and its value. |
| `!count list` | anyone | Lists every counter, sorted. |
| `!<name>` | anyone | Reads the current value (`die: 3`). |
| `!<name> add [N]` / `sub [N]` | mod/broadcaster | Adds/subtracts `N` (default 1) atomically (`kv.increment`). |
| `!<name> set <N>` | mod/broadcaster | Sets the value (required, whole number, s64 range). |

Counter names: lowercase letters/digits/`_`/`-`, max 32, `count` reserved. Unknown subcommand,
bad/out-of-range number or unknown operation replies with an error -- never silently dropped.
A `!`-token that is not `!count` and not a registered counter returns `None` (not ours).

```text
!count add !die     (mod) -> Created counter 'die' (starting at 0). Use !die to read it.
!die add 2          (mod) -> die: 2
!die                      -> die: 2
!die set abc        (mod) -> 'abc' isn't a whole number
!die add 1       (viewer) -> Only the broadcaster or a moderator can change this counter.
```

## Permissions (V2)

| id | why |
|---|---|
| `flags.read` | gates the command behind `waddles.command-count` |
| `storage.kv` | persists per-community named counters and their registry |

## State

| Key | Scope | Value |
|---|---|---|
| `count.registry` | per-community | JSON array of counter names |
| `count.value.<name>` | per-community | integer as ASCII text |

Colon-free keys (gh-631) -- the `kv` host rejects `:`. All state is channel-wide; no actor
identity is stored. Cost: every `!`-prefixed non-`!count` message pays one `kv.get` of the
registry (a dynamic name cannot be declared in the manifest's static prefix filter); text with
no leading `!` costs zero kv ops.

## Behavior notes

- **Mod gate fails closed.** Needs a real `bool` `is_mod`/`is_broadcaster`; if neither is
  present (Discord today) every mutation is denied and `count.role_info_unavailable` is logged.
- **Fail loud.** A `kv` backend error, a corrupt registry (bad JSON / not a string array) or a
  corrupt counter value logs `count.kv_failure` at ERROR and replies "something went wrong".
- **PII-free logs.** Logs never include the actor or a typed amount/argument (gh-674); only
  command shape, action and the (charset-validated) counter name are logged.
- Business logic lives in `transform()` (the registry lookup *is* the routing decision);
  `dispatch()` is a pure relay of the text `transform()` built.

## Test

```bash
cd bundles/python/count
python3 -m pytest --cov=app --cov-branch --cov-report=term-missing
```

`conftest.py` wires `src/` and `sdk/waddle-sdk/src` onto `sys.path`; no WASM build needed.
