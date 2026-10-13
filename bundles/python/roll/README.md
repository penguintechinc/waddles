# roll (Python)

`!roll [NdM]` -- a bounds-validated dice roll. Stateless chat fun, first-party
(`provider: builtin`, `app_id: waddles.core.example.roll`, `author: PenguinTech/waddles`,
Apache-2.0), **original Waddles content**: not a port and no third-party code or text reused.
Contrast `bundles/csharp/superpenguin-roll`, the *line-for-line* port of superpenguintv
(Psychoboy)'s [PenguinTwitchBot](https://github.com/Psychoboy/PenguinTwitchBot) roll command
(MIT, verbatim notice in its manifest) -- this Python bundle only borrows that bundle's
"command match vs. spec validation" split.

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!roll` | anyone | One twenty-sided die (`1d20`). |
| `!roll NdM` | anyone | Roll `N` dice of `M` sides, e.g. `!roll 2d6`. Replies with each roll and the total. |

Bounds (inclusive): **1-100 dice**, **2-1000 sides**; `N` and `M` are 1-3 and 1-4 digit runs
(`007d006` is accepted, `1000d6` is not). `d`/`D` are interchangeable and the command itself is
case-insensitive (`!ROLL 2D6`). An invocation with a malformed or out-of-bounds spec
(`!roll 0d6`, `1d1`, `-1d6`, `2d`, `abc`) **replies with the usage text** -- never silently
dropped:

```text
viewer> !roll 2d6
bot>    🎲 rolls [3, 5] (total 8)
viewer> !roll 0d6
bot>    Usage: !roll [NdM] -- e.g. !roll 2d6 (1-100 dice, 2-1000 sides).
```

**Known limitation (pinned by a test):** `!roll 2d6 extra` (more than one token after the
command) does not match the command pattern at all and produces no reply.

Randomness is the stdlib `random.randint` (a game, not a security boundary -- no WIT binding is
needed).

## Permissions (V2 structured)

| Id | Why |
|---|---|
| `flags.read` | Reads the `waddles.command-roll` PostHog flag via `feature_enabled` to gate the command. |

No `storage.kv` / `db` (stateless, `data.tables: []`) and no egress (`egress: []`): replies go
out over the `relay` host import to the event's own origin platform.

## Feature flag

`waddles.command-roll`, **default OFF**. Checked in `transform()` *after* the cheap command match
and *before* any dice logic. The flag is requested with `default=False`, so a flag-server outage
(host echoes the default) or a missing `wit_world` keeps the command off -- fail-closed. When off,
the bundle neither rolls nor logs.

## Platforms

`consumes` **Twitch** and **Discord** `chat.message` with `command_prefix: ["!roll"]`; the reply
is relayed to `envelope.event.platform` (never a fixed provider).

## Logging / PII

Logs carry only `platform`. The user-typed spec and `event.actor` are never logged (the actor may
still be a raw username ahead of the PII-tokenization pipeline). Regression:
`tests/test_backfill.py::test_logs_never_contain_user_typed_text_or_actor`.

## Files

| File | Role |
|---|---|
| `bundle.yaml` / `hub-manifest.yaml` | Manifest + hub-api install-pipeline manifest |
| `src/app.py` | `transform` (parse + roll) / `dispatch` (relay) |
| `src/_entry_wiring.py` | Static entry wiring (see `pyping`'s) |
| `tests/` | Host-native pytest suite (fake `wit_world`; no wasmtime) |

## Test

```bash
cd bundles/python/roll
python3.13 -m venv .venv && . .venv/bin/activate
pip install pytest==8.3.3 pytest-cov==5.0.0
pytest --cov=src --cov-branch --cov-report=term-missing   # 100% line + branch
```
