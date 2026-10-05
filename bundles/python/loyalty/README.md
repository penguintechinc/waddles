# loyalty (Python)

`!points` -- a community-scoped points/currency ledger. Community-scoped fun/engagement
content, first-party (`provider: builtin`, `app_id: waddles.core.example.loyalty`), **inspired
by** superpenguintv (Psychoboy)'s [PenguinTwitchBot](https://github.com/Psychoboy/PenguinTwitchBot)
loyalty-points feature -- not a literal port. No original source code or text is reused; the
command grammar, data model, optimistic-concurrency update loop, and leaderboard rendering are
written fresh for this bundle. See `bundle.yaml`'s `author`/`notice` fields for the credit, and
contrast `bundles/csharp/superpenguin-roll`, which *is* a line-for-line port and carries the full
verbatim MIT notice because it reuses original code.

**DB-backed (depends on #623, not yet merged).** This bundle is built on top of
`feature/db-capability-production` (merged into this branch directly, since #623 itself hasn't
landed on `release/v3.0.X` yet) for the structured `db` capability (`insert`/`get`/`query`/
`update`/`delete`). **This bundle cannot merge into `release/v3.0.X` until #623 lands there
first** -- it is expected to rebase cleanly on top once #623 merges (confirmed: this branch's own
merge of `feature/db-capability-production` was conflict-free). Until then this PR stays draft.

## Commands

| Command | Grammar shape | Behavior |
|---|---|---|
| `!points` | bare | Caller's own balance. |
| `!points <user>` | documented grammar extension (see `src/app.py`) | That user's balance. `0` if they have never received points. |
| `!points top` | `top` sub-module | Leaderboard: top 10 balances, highest first. |
| `!points add <amount> <user>` | `add` verb | Broadcaster/moderator only. Adds `<amount>` points to `<user>`. |
| `!points sub <amount> <user>` | `sub` verb | Broadcaster/moderator only. Removes `<amount>` points from `<user>`, clamped at `0` (never negative). |

Any other grammar-legal verb (`enable`/`disable`/`remove`/`reset`) or a malformed `!points ...`
replies with usage text -- never silently dropped.

## Data model (`db` + `kv`)

One app-owned `db` table, `loyalty_balances` (declared in `bundle.yaml`'s `data.tables` -- the
signal that grants this bundle the `db` capability), rows scoped per-community automatically by
the host. Two columns: `actor_hash` (SHA-256 hex pseudonym of the balance-holder -- never a raw
username) and `balance` (integer point total).

The committed `wit/waddle-bundle/stage.wit` `db` interface has no column-equality lookup -- only
`insert`/`get(row_id)`/`query(limit, offset, order_by)`/`update`/`delete`. This bundle therefore
also uses `kv` (`storage.kv`) as a lookup index, `loyalty:rowid:<pseudonym> -> row_id`, so a
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

Confirmed building a valid wasm component (`21MB`, `wasm32` component binary) from this branch.
**Not yet wired into `bundles/Dockerfile.core-bundles`** -- same genericization-PR dependency
`fish`'s own README documents; this bundle's CI wasm build is blocked on that PR landing.

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

Current result: 81 tests, `src/app.py` at 100% statement+branch coverage, `ruff check .` and
`mypy --strict src` both clean.

## Activation

**Not yet added to `bundles/core-bundles.yaml`** -- batched registration, per this PR's own
description (also gated on #623 landing first). `bundles/Dockerfile.core-bundles` is likewise
untouched here.
