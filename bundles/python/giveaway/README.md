# giveaway (Python)

Timed, FREE open-entry giveaway with one random draw: `!giveaway start 5m`, viewers `!giveaway enter`,
a mod runs `!giveaway draw`. First-party (`provider: builtin`,
`app_id: waddles.core.example.giveaway`), kv-only, WASI component.

| | |
|---|---|
| Activation | Installed -> tenant enabled -> community activated |
| Platforms | Twitch, Discord (`chat.message`) |
| State | `kv`, community-scoped (one key: the active giveaway) |
| Catalog | registered in `bundles/core-bundles.yaml` |

## Not the same as `raffle`

| | `giveaway` (this bundle) | `raffle` |
|---|---|---|
| Lifecycle | **timed** -- `start <duration>` sets a deadline (1s..24h) | mod `open`/`close` by hand, no clock |
| Entry | free, open to everyone, ends at the deadline | free, open while the mod has it open |
| Draw | exactly ONE winner, then the giveaway **closes** | picks one, entrant list stays in place |
| Concurrent | one per community (second `start` rejected) | one flat list per community |
| Commands | `!giveaway start/draw/status/cancel/enter` | `!raffle open/close/draw/list`, `!raffle`, `!enter` |

**Namespaced entry:** giveaway is entered with `!giveaway enter`; bare `!enter` belongs to raffle
only (renamed to resolve a command-trigger collision), so both bundles can be active together.

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!giveaway start <5m\|30s\|1h>` | mod/broadcaster | Opens a giveaway. Bad/zero/over-24h duration -> explicit reply; one already active (or ended and undrawn) -> rejected. |
| `!giveaway enter` | anyone | Enter once. Replies if none active, already entered, ended, or full (5000 max). |
| `!giveaway draw` | mod/broadcaster | One random winner announced, giveaway closed. No entries -> "no entries" reply, giveaway stays open. May run before or after the deadline. |
| `!giveaway status` | anyone | Entry count + time remaining (or "ended, waiting for a draw"). |
| `!giveaway cancel` | mod/broadcaster | Closes without drawing. Also clears a corrupt stored state. |
| anything else | anyone | Usage reply -- never silently dropped. |

Mod gate is strict-boolean and **fail-closed**: only a real `is_mod is True` /
`is_broadcaster is True` admits; absent badges (Discord's normalizer today), strings and ints
deny. Denied before any `kv` access.

`start`/`draw`/`cancel` are special-cased rather than parsed by `waddle_sdk.command.parse_command`
(they are not in the shared `VERBS`), same approach as `raffle`'s `open`/`close`/`draw`.

## Permissions (V2)

| id | why |
|---|---|
| `storage.kv` | persists the per-community active giveaway |
| `platform.clock` | enforces the deadline and seeds the draw (`clock` is not a catalog id) |

## State

| Key | Scope | Value |
|---|---|---|
| `giveaway.active` | per-community | JSON `{"ends_at": <epoch ms>, "entries": ["<sha256 hex>", ...]}` |

Colon-free key (gh-631). The host scopes by tenant/community/app_id. The key is deleted on
`draw`/`cancel`. Entrants are stored as `sha256(platform + author)` only -- never a raw id.

## Randomness

Python `random` like `slots`/`duel`/`raffle`, but via a private `random.Random` seeded from
`sha256(clock.now_millis() + entries)` (`_pick_winner`). componentize-py freezes module-level
`random` state at build time, so the explicit seed keeps each draw dependent on *when* it runs
and *who* entered. It is a community giveaway, not a security decision: the seed is not secret.
The winner is announced as the entrant's pseudonym tag (`hash[:8]`) -- only hashes are stored, so
a viewer recognizes their own tag; resolving it to a display name needs the PII-tokenization
pipeline (same trade-off as `raffle`).

## Behavior notes

- **Fail loud.** A `kv` backend error or corrupt stored value logs `giveaway.kv_failure` at ERROR
  (op + exception type) and replies "something went wrong"; the stored state is never overwritten
  on corruption. `!giveaway cancel` recovers it.
- **PII-free logs.** Only op names, counts and exception types; never message text or the actor.
- **No atomic update.** `kv` has no compare-and-swap, so two truly concurrent `!giveaway enter`s can lose
  one entry (the viewer re-enters; dedupe makes it safe).
- Eager host imports (`kv`, `log`, `relay`, `clock`) at module top, required
  for componentize-py wizening (`docs/reference/bundle-host-imports.md`).
- `transform()` runs the command and builds the reply; `dispatch()` is a pure relay.

## Test

```bash
uv venv -p 3.13 .venv   # at the repo/worktree root, never under bundles/
uv pip install --python .venv/bin/python3 -e sdk/waddle-sdk -e libs/flask_core pytest-cov mypy ruff
cd bundles/python/giveaway
../../../.venv/bin/python3 -m pytest --cov=app --cov-branch --cov-report=term-missing
```

`conftest.py` wires `src/` and `sdk/waddle-sdk/src` onto `sys.path`; no WASM build needed.
