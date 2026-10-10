# roulette (Python)

`!roulette` -- a flavor-only russian-roulette chat minigame. Community-scoped fun content,
first-party (`provider: builtin`, `app_id: waddles.core.example.roulette`). Net-new Waddles
content -- no inspiration source, no attribution/notice required (contrast `bundles/python/fish`,
which credits superpenguintv (Psychoboy) for its catch-and-track concept).

Uses the shared command-grammar parser (`waddle_sdk.command.parse_command`/`CommandSpec`,
merged in #618), same pattern as `slots`/`fish`'s own `app.py`.

**No real chat timeout/ban.** An "out" outcome is flavor text and a stat increment ONLY. This
bundle never calls a moderation/timeout/ban API on any platform -- it has no such capability
(`permissions` in `bundle.yaml` are `storage.kv` + `flags.read` only; `relay.push` only ever
delivers a chat message). Actually removing a user from chat needs mod-scope capabilities this bundle does not
request. See `src/app.py`'s own module docstring for the full rationale.

**No real-money/currency ties**, same rule as `slots`: no wallet, balance, transfer, or
redemption path anywhere in this bundle.

## Commands

| Command | Grammar shape | Behavior |
|---|---|---|
| `!roulette` | bare (no verb) | **Pull the trigger.** Spins a six-chamber cylinder with one round loaded -- a 1-in-6 "out", 5-in-6 "survive". Updates the caller's total pull count and survive/out totals. Subject to the per-caller cooldown. |
| `!roulette list` | `list` verb, no args | Reads the caller's own survive/out record: total pulls, survives, outs, survival rate. |
| `!roulette set cooldown <seconds>` | `set` verb | Broadcaster/moderator only. Sets the community's pull cooldown, `0`-`3600` seconds (default `30`). `0` disables the cooldown entirely. |

Any other grammar-legal verb (`add`/`sub`/`enable`/`disable`/`remove`/`reset`) or a malformed
`!roulette ...` replies with usage text -- never silently dropped.

## Odds

Classic six-chamber revolver, one round loaded, each pull independent (`random.randint(1, 6)`):

| Outcome | Odds | Effect |
|---|---|---|
| Survive (`*click*`) | 5/6 | Flavor message, `survives` counter incremented. |
| Out (`*BANG!*`) | 1/6 | Flavor message, `outs` counter incremented. **No real timeout/ban applied.** |

## State (kv, community-scoped only)

All state goes through `waddle_sdk.community_kv`, keyed by `community_id` -- never global or
tenant-wide. Per-caller keys use a SHA-256 hash of the actor as the pseudonym (never the raw
username/actor id -- see `src/app.py::_pseudonym()`), ahead of the PII-tokenization pipeline
(#427/#429) in case `event.actor` is still a raw username.

**Keys use `.` as their separator, never `:`** (gh-631) -- the real `kv` host capability rejects
`:` as a guest-key byte (its own reserved namespace separator). This is a deliberate correction
from `slots`'s own `slots:spins:<pseudonym>`-shaped keys, which predate that fix.

| Key | Scope | TTL | Purpose |
|---|---|---|---|
| `roulette.lastpull.<pseudonym>` | per-(community, caller) | `cooldown` seconds | Cooldown gate -- presence + elapsed time decide allow/deny. Not written at all when `cooldown == 0` (see below). |
| `roulette.pulls.<pseudonym>` | per-(community, caller) | none | Running total pulls (`kv.increment`). |
| `roulette.survives.<pseudonym>` | per-(community, caller) | none | Running total survives (`kv.increment`). |
| `roulette.outs.<pseudonym>` | per-(community, caller) | none | Running total outs (`kv.increment`). |
| `roulette.config.cooldown` | per-community | none | Admin-configured pull cooldown in seconds (`0`-`3600`). |

`cooldown == 0` skips reading/writing `roulette.lastpull.<pseudonym>` entirely, rather than
writing it with `ttl_seconds=0` (which means "never expires" in `waddle_sdk.kv`) -- an unbounded
key for a cooldown that by definition never fires.

## Deferred to v2 (not stubbed)

**Cross-community leaderboards.** Every key above is scoped to one community (`community_kv`'s
own rule: reputation and user-details are the platform's only two cross-community exceptions,
and this bundle is neither). A leaderboard needs a query that spans many communities/rows --
`kv` has no scan/list-keys primitive, so this needs a real `db` capability with an
`order_by`/pagination surface. **Deferred until the in-flight #623 `db` order_by API lands** --
no `!roulette leaderboard` command is declared, and nothing here is a stand-in stub for it.

Also out of scope: any real moderation/timeout/ban action (see module docstring), and any
wallet/points-economy integration -- neither partially stubbed.

## Examples

```text
viewer> !roulette
bot>    🔫 viewer spins the cylinder and pulls the trigger... *click* -- lucky! Try your luck again sometime. (pull #1)
viewer> !roulette
bot>    🔫 slow down, viewer! try again in 30s.
viewer> !roulette list
bot>    Pulls: 1. Survived: 1. Out: 0 (100% survival rate).
mod>    !roulette set cooldown 10
bot>    roulette cooldown set to 10s
```

## Permissions (V2 structured)

| Id | Why |
|---|---|
| `storage.kv` | Persists per-community roulette tallies and the cooldown timestamps/config. |
| `flags.read` | Gates the command behind its `waddles.command-roulette` feature flag. |

No `db` (`data.tables: []`), no egress, no moderation capability (see "No real chat timeout/ban").
Mod gate: `set cooldown` requires a real `is_mod` or `is_broadcaster` `True`; **absent** badge
fields (e.g. the Discord normalizer today) are **denied** (fail closed) with zero `kv` access.
Everything else is open to any caller.

## Platforms

`consumes` **Twitch** and **Discord** `chat.message` with `command_prefix: ["!roulette"]`; replies
go back to the event's own origin platform + channel. A `community` context is required (no
tenant-wide fallback): without one `dispatch` logs `roulette.missing_community` and raises
`ValueError`.

## Failure semantics (fail loud)

| Condition | Behavior |
|---|---|
| `kv` backend error | ERROR `roulette.kv_error` (`op` + exception type only), chat reply "the chamber is jammed, try again shortly.", `RuntimeError("roulette kv <op> failed: <Type>")`. |
| Corrupt cooldown config / cooldown timestamp / a counter | ERROR `roulette.*_corrupt` (community only) and treated as the default / no cooldown / `0` -- logged loudly, never silent. |
| Missing `channel_id` / unknown command | `ValueError`. |

## Logging / PII

Logs carry only `command`, `op`, `community`, `role_signal`, `platform` and exception type names --
never typed arguments, grammar text or `event.actor`. Regression:
`tests/test_backfill.py::test_no_log_line_in_any_flow_contains_typed_text_or_the_raw_actor`.

## Feature flag

Gated behind `waddles.command-roulette`, defaulted OFF (`critical-rules.md` Feature Flags &
License Tiers) -- checked in `transform()` after the cheap `!roulette` command-name match and
before the real grammar parse.

## Files

| File | Role |
|---|---|
| `bundle.yaml` | Manifest -- app id, consumes rules, limits, attribution metadata |
| `hub-manifest.yaml` | hub-api install-pipeline manifest (separate schema consumer, see its own header comment) |
| `src/app.py` | `transform`/`dispatch` -- the full game |
| `src/_entry_wiring.py` | Static `bundle_compiler`-shaped entry wiring (see `pyping`'s own) |
| `tests/` | Host-native pytest suite (fake `wit_world`, no wasmtime) -- uses the shared, charset-enforcing `waddle_sdk.testing.install_fake_kv_host` fake for `kv` (first bundle to do so) |

## Build

```bash
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$PWD":/repo -w /repo python:3.13-slim-bookworm \
  bash -c "pip install --no-cache-dir componentize-py==0.25.1 && \
    componentize-py -d wit/waddle-bundle -w stage \
      componentize -p sdk/waddle-sdk/src -p bundles/python/roulette/src \
      waddle_sdk._component_entry -o /tmp/roulette.wasm"
```

## Test

```bash
cd bundles/python/roulette
python3.13 -m venv .venv && . .venv/bin/activate
pip install pytest==8.3.3 pytest-cov==5.0.0
pytest --cov=src --cov-branch --cov-report=term-missing   # 100% line + branch
```

`tests/conftest.py` puts `src/` and `sdk/waddle-sdk/src` on `sys.path`, so no package install is
needed. `test_app.py` covers grammar/game logic; `test_backfill.py` adds the mod-gate matrix,
cooldown bounds/TTL semantics, PII-free-log regression, flag fail-closed and `_entry_wiring`.

## Activation

Registered in `bundles/core-bundles.yaml` (`waddles.core.example.roulette`).
