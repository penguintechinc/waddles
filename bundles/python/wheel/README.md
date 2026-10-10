# wheel (Python)

`!wheel` -- a community-scoped, `kv`-backed spin-the-wheel random picker. First-party
(`provider: builtin`, `app_id: waddles.core.example.wheel`, `author: PenguinTech/waddles`,
Apache-2.0), **original Waddles content** (not a port; no third-party attribution applies).

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!wheel` / `!wheel spin` | anyone | Picks one option uniformly at random (`random.choice`): `🎡 The wheel lands on: tacos!` An empty wheel is rejected with a message, never a crash. |
| `!wheel add <option>` | broadcaster / mod | Adds `<option>` (trimmed, multi-word OK, max **200** chars, max **100** options, case-insensitive duplicates rejected). |
| `!wheel remove <option>` | broadcaster / mod | Removes an option (case-insensitive match; the reply names the stored spelling). |
| `!wheel list` | anyone | `Wheel options: a, b, c` (or the empty-wheel message). |
| `!wheel reset` | broadcaster / mod | Clears every option (destructive, so gated like `add`/`remove`). |

The command word matches case-insensitively and only as a whole token (`!wheelbarrow` does not
match). An unrecognized sub-command replies `Unknown !wheel subcommand '<token>'. Usage: ...` and
touches no state -- never silently dropped. The shared parser (`parse_command`) is intentionally
not used: the flat `!wheel <verb> [arg]` shape needs none of its sub-module machinery.

```text
mod>    !wheel add tacos
bot>    Added 'tacos' to the wheel (1 option).
viewer> !wheel
bot>    🎡 The wheel lands on: tacos!
mod>    !wheel reset
bot>    The wheel has been cleared.
```

## State (`kv`, community-scoped)

| Key | Scope | Purpose |
|---|---|---|
| `wheel.options` | per-community (`c.<community_id>.wheel.options`) | JSON array of option strings, insertion order preserved, TTL `0` (durable). |

`.`-separated, never `:` (gh-631). `community_id` comes from the bound bundle context; with **no
community** the command replies `!wheel requires a community context and cannot be used
tenant-wide` and never falls back to a shared bucket. **All `kv` work happens in `transform()`**
(every operation, including the pick, needs its result in the reply text); `dispatch()` is a pure
relay of the text `transform()` built.

### Failure semantics

| Condition | Behavior |
|---|---|
| `kv` backend error | ERROR `wheel.kv_failure` with a static `op` (`kv_get`/`kv_set`) and the failure's **class name** only -- never the host's free-form message -- then the chat reply "Something went wrong updating the wheel storage - please try again." |
| Corrupt stored options (non-UTF-8, non-JSON, not an array of strings) | Same loud error path (`op` = `options_decode`/`options_shape`, class name only -- the stored blob is made of user-typed option text and is never echoed) -- the blob is **never silently reset** to `[]` (a later `add` would otherwise destroy the evidence). |
| Missing `channel_id` / `text` on `dispatch` | `ValueError`. |

## Permissions (V2 structured)

| Id | Why |
|---|---|
| `storage.kv` | Persists the per-community wheel options list. |
| `flags.read` | Gates the command behind its `waddles.command-wheel` feature flag. |

No `db`, no egress. Mod gate: `add`/`remove`/`reset` require a real `is_mod` or `is_broadcaster`
boolean `True` (a string badge such as `"false"` is **not** truthy here -- 1.0.3 fixed a bypass where
`bool("false")` let non-moderators mutate the wheel); when **neither** badge field is present as a `bool` (e.g. the Discord normalizer today) the
caller is **denied** (fail closed), with a DEBUG `wheel.role_info_unavailable` log.

## Feature flag

`waddles.command-wheel`, **default OFF**, checked after the command-text match and before any `kv`
access, requested with `default=False` (flag outage / missing `wit_world` -> off). While off nothing
is parsed, stored or logged.

## Platforms

`consumes` **Twitch** and **Discord** `chat.message` with `command_prefix: ["!wheel"]`; replies go
back to the event's own origin platform + channel.

## Logging / PII

Logs carry only a **static** command name (`spin`/`add`/`remove`/`list`/`reset`, or the literal
`unknown` for anything else), option *counts*, `platform` and `community` -- never option text, the
typed sub-command token, or `event.actor`. (The matched-command field used to echo the
lower-cased typed token for unrecognized sub-commands; the PII-free-log regression suite in
`tests/test_backfill.py` caught it and `_LOGGED_TOKENS` now guards it.) The ERROR line for a
storage failure carries only `op` and the exception class name (1.0.3; it used to carry the
exception text). `tests/test_regressions.py` drives a full session plus failure cases with a
sentinel string and asserts the exact per-message field allowlist. The bundle stores no
per-caller state at all.

## Files

| File | Role |
|---|---|
| `bundle.yaml` / `hub-manifest.yaml` | Manifest + hub-api install-pipeline manifest |
| `src/app.py` | `transform` (all logic + `kv` I/O) / `dispatch` (relay) |
| `src/_entry_wiring.py` | Static entry wiring (see `pyping`'s) |
| `tests/` | Host-native pytest suite (shared charset-enforcing `kv` fake) |

## Test

```bash
cd bundles/python/wheel
python3.13 -m venv .venv && . .venv/bin/activate
pip install pytest==8.3.3 pytest-cov==5.0.0
pytest --cov=src --cov-branch --cov-report=term-missing   # 100% line + branch
```
