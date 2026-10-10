# loyalty (Python)

`!points` -- a community-scoped points/currency ledger. Community-scoped fun/engagement
content, first-party (`provider: builtin`, `app_id: waddles.core.example.loyalty`), **inspired
by** superpenguintv (Psychoboy)'s [PenguinTwitchBot](https://github.com/Psychoboy/PenguinTwitchBot)
loyalty-points feature -- not a literal port. No original source code or text is reused; the
command grammar, data model, optimistic-concurrency update loop, and leaderboard rendering are
written fresh for this bundle. See `bundle.yaml`'s `author`/`notice` fields for the credit, and
contrast `bundles/csharp/superpenguin-roll`, which *is* a line-for-line port and carries the full
verbatim MIT notice because it reuses original code.

**DB-backed.** Built on the structured `db` capability (`insert`/`get`/`query`/`update`/`delete`,
#623) plus `kv`; merged into `release/v3.0.X` via #630 and registered in
`bundles/core-bundles.yaml`. A shared cross-bundle economy (loyalty/slots/duel/gamble/heist sharing
one balance) is a separate, open design item (#714).

## Commands

| Command | Grammar shape | Behavior |
|---|---|---|
| `!points` | bare | Caller's own balance. |
| `!points <user>` | documented grammar extension (see `src/app.py`) | That user's balance. `0` if they have never received points. |
| `!points top` | `top` sub-module | Leaderboard: top 10 balances, highest first. |
| `!points add <amount> <user>` | `add` verb | Broadcaster/moderator only. Adds `<amount>` points to `<user>`. |
| `!points sub <amount> <user>` | `sub` verb | Broadcaster/moderator only. Removes `<amount>` points from `<user>`, clamped at `0` (never negative). |

Any other grammar-legal verb (`enable`/`disable`/`remove`/`reset`) or a malformed `!points ...`
replies with usage text -- never silently dropped. `<amount>` must be a positive integer
(`0`, negatives, decimals and words are usage errors).

## Examples

```text
> !points add 50 alice                 (mod)
Added 50 points to alice. New balance: 50.
> !points alice
alice has 50 points.
> !points sub 80 alice                 (mod)
Removed 80 points from alice. New balance: 0.
> !points top
Top points: 1. player-3f7a9c21: 50, 2. player-91bd0e44: 10
> !points add 5 alice                  (regular viewer)
only moderators/broadcasters can adjust points
```

## Permissions (V2, `bundle.yaml` / `hub-manifest.yaml`)

| id | Why |
|---|---|
| `storage.kv` | `loyalty.rowid.<pseudonym> -> row_id` lookup index -- the `db` interface has no column-equality query, so this is the O(1) path to a user's balance row. |
| `flags.read` | Reads the `waddles.command-loyalty` feature flag that gates the command. |

The `db` capability itself is granted by the manifest's `data.tables: [loyalty_balances]` (not by a
`permissions` entry). No egress (`egress: []`).

## Moderator gate (fail-closed)

`add`/`sub` require `is_mod` **or** `is_broadcaster` on the normalized event
(`_caller_role_signal()`), decided in `dispatch` **before any `kv`/`db` access**: neither field
present (Discord today) => denied; present but falsy => denied; either true => allowed. Denial
logs `loyalty.adjust_denied` (command + role-signal state only). `!points`, `!points <user>` and
`!points top` never need a role.

## Platforms

Twitch and Discord `chat.message` events starting with `!points` (`stages.process.consumes`).
Discord events carry no mod badge today, so `add`/`sub` are denied there until its normalizer
supplies the fields.

## Failure behavior (fail-loud, never silent)

| Condition | Behavior |
|---|---|
| Any `kv`/`db` call fails (`kv_get`, `kv_set`, `db_get`, `db_insert`, `db_query`, `db_update`) | ERROR `loyalty.backend_error` (`op` + the error class **name**), chat reply "points are temporarily unavailable, try again shortly.", then `RuntimeError`. Exactly one relay. |
| Optimistic-concurrency conflicts | Retried up to 5 times with a fresh `db.get`; exhausted => loud `db_update_retry` failure. |
| `kv` index points at a row `db.get` can no longer find | Loud `index_stale` failure -- never silently re-created (that would orphan/duplicate the row). |
| Non-UTF-8 index value, or non-numeric `balance` column | Raises (never invents or renders a balance). |
| Missing `channel_id`, missing community (no tenant-wide fallback), unknown command, malformed forwarded payload | `ValueError`; missing community also logs ERROR `loyalty.missing_community`. |
| `relay.push` fails | Propagates; no success line is logged. |

**Known limitation:** first-time grants insert the `db` row and *then* write the `kv` index. If the
index write fails the bundle fails loud, but the row already exists un-indexed -- a retry inserts a
second row for the same user (the leaderboard would show both). Same insert-then-index shape
`inventory` documents; no cleanup of the orphan exists yet.

## Logging / PII

Every log message has a strict field allowlist (command, platform, op/error class) -- **never**
the raw message, target, typed amount text, or `event.actor` (regression: gh-674; the suite drives
every command and outcome with a sentinel string as actor and target, and asserts its absence plus
the exact per-message field set). Keys and the `actor_hash` column are SHA-256 pseudonyms, never
raw names.

## Data model (`db` + `kv`)

One app-owned `db` table, `loyalty_balances` (declared in `bundle.yaml`'s `data.tables` -- the
signal that grants this bundle the `db` capability), rows scoped per-community automatically by
the host. Two columns: `actor_hash` (SHA-256 hex pseudonym of the balance-holder -- never a raw
username) and `balance` (integer point total).

The committed `wit/waddle-bundle/stage.wit` `db` interface has no column-equality lookup -- only
`insert`/`get(row_id)`/`query(limit, offset, order_by)`/`update`/`delete`. This bundle therefore
also uses `kv` (`storage.kv`) as a lookup index, `loyalty.rowid.<pseudonym> -> row_id`, so a
single user's balance lookup/adjustment is an O(1) kv read + an exact `db.get`/`db.update`
rather than an unbounded table scan (`db.query()`'s `limit` is host-clamped to 200 rows). The
leaderboard is the one operation that genuinely needs `db.query(order_by="balance",
descending=True)` -- this is exactly why this bundle can't stay kv-only, unlike `fish`.

Writes use optimistic concurrency (`expected_version`, from `db.py`'s `update()`), with a
bounded fetch-mutate-update retry loop (`_db_update_with_retry`, 5 attempts) on
`db.ConflictError`.

**Leaderboard entries show a `player-<hash prefix>` tag, not a display name.** `actor_hash` is a
one-way SHA-256 hash -- there is no reverse lookup from a stored row back to a chat-visible name
without a hub-side detokenization step this bundle has no access to (PII tokenization: raw PII
lives only inside the hub/API server). `!points`/`!points <user>` CAN show a real name because
the live chat event's own actor/typed-target text is only ever echoed back into that same reply,
never persisted -- the same convention `fish` documents ("username IS rendered in the visible
chat reply, never logged"). See `src/app.py`'s module docstring for the full rationale.

## Feature flag

Gated behind `waddles.command-loyalty`, defaulted OFF (`critical-rules.md` Feature Flags &
License Tiers) -- checked in `transform()` after the cheap `!points` command-name match and
before the real grammar parse.

## Known, pre-existing platform gap (not fixed by this PR)

The richer declarative `data.table.columns[]` schema (`hub_api/services/bundle_data_schema.py`)
that would let hub-api provision this bundle's table columns automatically is, by that module's
own docstring, "not wired into onboarding yet." This bundle declares its table the same way
every other `db`-capable bundle can today (`data.tables: [loyalty_balances]`); wiring the richer
schema is a separate, already-tracked platform follow-on.

## Files

| File | Role |
|---|---|
| `bundle.yaml` | Manifest -- app id, consumes rules, limits, data table, attribution metadata |
| `hub-manifest.yaml` | hub-api install-pipeline manifest (separate schema consumer, see its own header comment) |
| `src/app.py` | `transform`/`dispatch` -- the full command grammar, data model, and backend wrappers |
| `src/_entry_wiring.py` | Static `bundle_compiler`-shaped entry wiring (see `pyping`'s own) |
| `tests/` | Host-native pytest suite (fake `wit_world`: flags/kv/db/relay/log, no wasmtime) |

## Build

```bash
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$PWD":/repo -w /repo python:3.13-slim-bookworm \
  bash -c "pip install --no-cache-dir componentize-py==0.25.1 && \
    componentize-py -d wit/waddle-bundle -w stage \
      componentize -p sdk/waddle-sdk/src -p bundles/python/loyalty/src \
      waddle_sdk._component_entry -o /tmp/loyalty.wasm"
```

In CI/release the wasm is built generically: `bundles/Dockerfile.core-bundles` runs
`bundles/build_python_bundles.py`, which builds every `language: python` entry in
`bundles/core-bundles.yaml` -- no per-bundle Dockerfile edit.

## Test

```bash
cd bundles/python/loyalty
python3.13 -m venv .venv && . .venv/bin/activate
pip install -e ../../../sdk/waddle-sdk
pip install pytest==8.3.3 pytest-cov mypy==1.14.1 ruff==0.14.1
pytest --cov=src --cov-branch --cov-report=term-missing
mypy --strict src
ruff check .
```

Current result: 130 tests, `src/app.py` at 100% statement+branch coverage.

## Activation

Registered in `bundles/core-bundles.yaml` (`waddles.core.example.loyalty`, activation target
`global`); dark until `waddles.command-loyalty` is turned on.
