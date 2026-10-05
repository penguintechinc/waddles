# inventory (Python)

`!inventory`/`!inv` -- a community-scoped, per-user item collection. Community-scoped fun/
engagement content, first-party (`provider: builtin`, `app_id: waddles.core.example.inventory`),
**inspired by** the general shape common to Twitch economy bots, including superpenguintv
(Psychoboy)'s [PenguinTwitchBot](https://github.com/Psychoboy/PenguinTwitchBot) -- not a literal
port. No original source code or text is reused; the command grammar, data model, optimistic-
concurrency transfer logic, and listing rendering are written fresh for this bundle. See
`bundle.yaml`'s `author`/`notice` fields for the credit, and contrast
`bundles/csharp/superpenguin-roll`, which *is* a line-for-line port and carries the full verbatim
MIT notice because it reuses original code.

**DB-backed (depends on #623, not yet merged).** This bundle is built on top of
`feature/db-capability-production` (merged into this branch directly, since #623 itself hasn't
landed on `release/v3.0.X` yet) for the structured `db` capability (`insert`/`get`/`query`/
`update`/`delete`). **This bundle cannot merge into `release/v3.0.X` until #623 lands there
first** -- it is expected to rebase cleanly on top once #623 merges. Until then this PR stays
draft. Mirrors `bundles/python/loyalty`'s (#630) own identical dependency note.

## Commands

| Command | Grammar shape | Behavior |
|---|---|---|
| `!inventory` / `!inv` | bare (two aliases) | Caller's own item collection. |
| `!inv <user>` | documented grammar extension (see `src/app.py`) | That user's item collection. "has no items" if they own nothing. |
| `!inv give <item> <user>` | documented grammar extension | Transfers 1 unit of `<item>` from the caller to `<user>`. Fails (no-op reply) if the caller has none, or if `<user>` is the caller themself. |
| `!inv add <item> <user>` | `add` verb | Broadcaster/moderator only. Grants 1 unit of `<item>` to `<user>`. |
| `!inv remove <item> <user>` | `remove` verb | Broadcaster/moderator only. Revokes 1 unit of `<item>` from `<user>`, clamped at `0` (never negative); a no-op reply if they have none. |

Any other grammar-legal verb (`set`/`enable`/`disable`/`delete`/`list`/`reset`) or a malformed
`!inventory`/`!inv ...` replies with usage text -- never silently dropped. `add`/`remove` always
act on exactly 1 unit per invocation -- there is no `<amount>` token in this bundle's grammar
(contrast `loyalty`'s `!points add <amount> <user>`); a mod wanting to grant more calls it
multiple times.

## Data model (`db` + `kv`)

One app-owned `db` table, `inventory_items` (declared in `bundle.yaml`'s `data.tables` -- the
signal that grants this bundle the `db` capability), rows scoped per-community automatically by
the host. Three columns: `actor_hash` (SHA-256 hex pseudonym of the item-holder -- never a raw
username), `item` (normalized item name), and `quantity` (integer count). One row per
`(actor_hash, item)` pair -- a user can own many distinct items, unlike `loyalty`'s single
balance-per-user row.

The committed `wit/waddle-bundle/stage.wit` `db` interface has no column-equality lookup -- only
`insert`/`get(row_id)`/`query(limit, offset, order_by)`/`update`/`delete`. `loyalty` solved its
single-row-per-user case with one `kv` key per user pointing at one `row_id`; this bundle owns
*multiple* rows per user, so its `kv` (`storage.kv`) index value is itself a small JSON object,
`{item: row_id, ...}`, one key per user: `inventory:dir:<pseudonym>`. This directory is the only
way this bundle can answer "list everything this user owns" without an unbounded `db.query()`
scan across the whole community (also wrong at scale: `query()`'s `limit` is host-clamped to 200
rows), and it gives O(1) by-item lookup for `give`/`add`/`remove` too.

Writes use optimistic concurrency (`expected_version`), with a bounded fetch-mutate-update retry
loop (`_db_update_with_retry`, 5 attempts) on `db.ConflictError` -- same pattern as `loyalty`.
Decrements (`remove`/`give`) that reach quantity `0` delete the row and drop it from the
directory (`_maybe_cleanup_zero_row`) -- but never at the cost of destroying a concurrently-
replenished row: a `db.ConflictError` on the cleanup delete is treated as "someone else touched
it first" and simply skipped, leaving a harmless zero-quantity row behind (listing already
filters `quantity > 0`).

**Known, honestly-documented concurrency limitation.** `kv.set` has no compare-and-swap -- two
concurrent *first-time* grants of two *different* items to the same user can race on writing the
directory value, and whichever `kv.set` wins last can drop the other's new directory entry (the
underlying `db` row still exists -- nothing is lost from the ledger, only that item's entry in
the user's own at-a-glance listing, until the next write for that user touches it again). This is
the same class of gap `loyalty`'s own module docstring documents for its single-value index, just
the multi-item shape of the identical limitation -- not a new weakness this bundle introduces.

**Listing cannot show real display names for an arbitrary user.** Like `loyalty`'s leaderboard,
there is no reverse lookup from a stored `actor_hash` back to a chat-visible name (PII
tokenization: raw PII lives only inside the hub/API server). `!inventory`/`!inv <user>` always
have a live, chat-typed name available and echo it straight back into the reply -- never
persisted.

## Feature flag

Gated behind `waddles.command-inventory`, defaulted OFF (`critical-rules.md` Feature Flags &
License Tiers) -- checked in `transform()` after the cheap alias match and before the real
grammar parse.

## Known, pre-existing platform gap (not fixed by this PR)

The richer declarative `data.table.columns[]` schema (`hub_api/services/bundle_data_schema.py`)
that would let hub-api provision this bundle's table columns automatically is, by that module's
own docstring, "not wired into onboarding yet." This bundle declares its table the same way every
other `db`-capable bundle can today (`data.tables: [inventory_items]`); wiring the richer schema
is a separate, already-tracked platform follow-on. Identical note to `loyalty`'s own.

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
      componentize -p sdk/waddle-sdk/src -p bundles/python/inventory/src \
      waddle_sdk._component_entry -o /tmp/inventory.wasm"
```

Confirmed building a valid wasm component (`21MB`, `wasm32` component binary) from this branch.
**Not yet wired into `bundles/Dockerfile.core-bundles`** -- same genericization-PR dependency
`fish`/`loyalty`'s own READMEs document; this bundle's CI wasm build is blocked on that PR
landing.

## Test

```bash
cd bundles/python/inventory
python3.13 -m venv .venv && . .venv/bin/activate
pip install -e ../../../sdk/waddle-sdk
pip install pytest==8.3.3 pytest-cov mypy==1.14.1 ruff==0.14.1
pytest --cov=src --cov-branch --cov-report=term-missing
mypy --strict src
ruff check .
```

Current result: 101 tests, `src/app.py` at 100% statement+branch coverage, `ruff check .` and
`mypy --strict src` both clean.

## Activation

**Not yet added to `bundles/core-bundles.yaml`** -- batched registration, per this PR's own
description (also gated on #623 landing first). `bundles/Dockerfile.core-bundles` is likewise
untouched here.
