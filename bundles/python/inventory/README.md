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

**DB-backed.** Built on the structured `db` capability (`insert`/`get`/`query`/`update`/`delete`,
#623) plus `kv`; merged into `release/v3.0.X` via #637 and registered in
`bundles/core-bundles.yaml`. Mirrors `bundles/python/loyalty`'s own shape.

## Commands

| Command | Grammar shape | Behavior |
|---|---|---|
| `!inventory` / `!inv` | bare (two aliases) | Caller's own item collection. |
| `!inv <user>` | documented grammar extension (see `src/app.py`) | That user's item collection. "has no items" if they own nothing. |
| `!inv give <item> <user>` | documented grammar extension | Transfers 1 unit of `<item>` from the caller to `<user>`. Fails (no-op reply) if the caller has none, if `<user>` is the caller themself, or if the recipient is at a cap or busy -- in every failure case the caller keeps the item. |
| `!inv add <item> <user>` | `add` verb | Broadcaster/moderator only. Grants 1 unit of `<item>` to `<user>`. |
| `!inv remove <item> <user>` | `remove` verb | Broadcaster/moderator only. Revokes 1 unit of `<item>` from `<user>`, clamped at `0` (never negative); a no-op reply if they have none. |

Any other grammar-legal verb (`set`/`enable`/`disable`/`delete`/`list`/`reset`) or a malformed
`!inventory`/`!inv ...` replies with usage text -- never silently dropped. `add`/`remove` always
act on exactly 1 unit per invocation -- there is no `<amount>` token in this bundle's grammar
(contrast `loyalty`'s `!points add <amount> <user>`); a mod wanting to grant more calls it
multiple times.

Known grammar-extension trade-off (same family `loyalty`/`love` document): a bare `!inv <word>` is a
target-user lookup, so the two verbs whose *bare* form the shared grammar rejects --
`!inv enable` / `!inv disable` -- fall through to "list the inventory of a user named `enable`"
rather than a usage reply (harmless: "enable has no items."). Every other bare verb
(`set`/`list`/`reset`/`delete`/`add`/`remove`) replies usage, and `!inv give` with a missing
item/user is a usage error, never a lookup of a user literally named `give`.

## Examples

```text
> !inv add sword alice                 (mod)
Gave 1 sword to alice. They now have 1.
> !inv alice
alice's inventory: sword x1
> !inv give sword bob                  (alice)
alice gave 1 sword to bob.
> !inv remove sword bob                (mod)
Removed 1 sword from bob. They now have 0.
> !inv add sword alice                 (regular viewer)
only moderators/broadcasters can adjust inventories
```

## Permissions (V2, `bundle.yaml` / `hub-manifest.yaml`)

| id | Why |
|---|---|
| `storage.kv` | Per-user `item -> row_id` directory (`inventory.dir.<pseudonym>`) -- the only way to list/lookup a user's rows, since the `db` interface has no column-equality query. |
| `flags.read` | Reads the `waddles.command-inventory` feature flag that gates the command. |

The `db` capability itself is granted by the manifest's `data.tables: [inventory_items]` (not by a
`permissions` entry). No egress (`egress: []`).

## Moderator gate (fail-closed)

`add`/`remove` require `is_mod` **or** `is_broadcaster` on the normalized event
(`_caller_role_signal()`), decided in `dispatch` **before any `kv`/`db` access**: neither field
present (Discord today) => denied; present but falsy => denied; either true => allowed. Denial
logs `inventory.grant_denied` (command + role-signal state only). `!inv`, `!inv <user>` and
`!inv give` (a viewer trading their own items) never need a role.

## Platforms

Twitch and Discord `chat.message` events starting with `!inventory` or `!inv`
(`stages.process.consumes`). Discord events carry no mod badge today, so `add`/`remove` are denied
there until its normalizer supplies the fields.

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
`{item: row_id, ...}`, one key per user: `inventory.dir.<pseudonym>`. This directory is the only
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

## Integrity guarantees (1.0.3)

| Risk | Guard |
|---|---|
| `give` destroyed the item when the credit step failed (the giver was already debited) | Debit, then credit, then clean up. A credit that cannot complete (backend failure, target at a cap, target locked) **refunds the giver's row** before reporting; a refund that itself fails is logged at ERROR (`inventory.give_refund_failed`) for the operator. Exactly one chat reply either way. |
| Two simultaneous first-time grants raced on the per-user directory and the loser's item dropped out of the listing | Every directory write (new item, zero-row cleanup) is serialized per user by a short-TTL lock (`inventory.lock.<pseudonym>`, `kv.increment` -- only the holder sees `1`, 10 s TTL). The new-item path re-reads the directory *under* the lock and adopts a concurrent creator's row. A caller that cannot get the lock replies "that inventory is being updated right now, try again in a few seconds." |
| Row inserted but its directory write failed -> unreachable orphan row | The orphan row is deleted and the lock released before the error is raised (a failed cleanup is logged: `inventory.orphan_cleanup_failed`). |
| Two simultaneous `give`s of someone's last unit | Quantity changes are version-gated (`expected_version`); the loser re-reads, sees `0`, and gets "has no <item> to give". |
| Unbounded item names / distinct items / quantities | Names are 1-32 word characters, `.` or `-` (no spaces, no `@`/`#` mention syntax); a user holds at most **50** distinct items (this also keeps `!inventory` -- one `kv.get` plus one `db.get` per item -- inside the host's 64-ops-per-invocation budget); a row caps at **1,000,000**. Out-of-range stored quantities are corruption and fail loud (`quantity_invalid`). |
| A mod-granted item invisible to its owner (`Alice` vs `alice` hashed differently) | The caller's own identity is normalized exactly like a typed `<user>` (strip `@`, lower-case), including the `give` self-check. |
| Non-moderator passing the mod gate with a string badge (`"false"` is truthy) | Badges are read with an identity check; only a real boolean `True` opens the gate. |

**Listing cannot show real display names for an arbitrary user.** Like `loyalty`'s leaderboard,
there is no reverse lookup from a stored `actor_hash` back to a chat-visible name (PII
tokenization: raw PII lives only inside the hub/API server). `!inventory`/`!inv <user>` always
have a live, chat-typed name available and echo it straight back into the reply -- never
persisted.

## Failure behavior (fail-loud, never silent)

| Condition | Behavior |
|---|---|
| Any `kv`/`db` call fails (`kv_get`, `kv_set`, `db_get`, `db_insert`, `db_update`, `db_delete`) | ERROR `inventory.backend_error` (`op` + the error class **name**), chat reply "inventory is temporarily unavailable, try again shortly.", then `RuntimeError`. Exactly one relay. |
| Optimistic-concurrency conflicts | Retried up to 5 times with a fresh `db.get`; exhausted => loud `db_update_retry` failure. A conflict on the zero-row cleanup delete is a benign skip (INFO `inventory.cleanup_skipped`), never a failure. |
| Corrupt per-user directory (not UTF-8, bad JSON, not a JSON object) | Loud `dir_decode` failure on **every** command that reads it; the corrupt bytes are never overwritten or silently reset. |
| Directory points at a row `db.get` can no longer find | Loud `directory_stale` failure -- never silently re-created (that would orphan/duplicate the row). |
| Non-numeric, negative or over-cap `quantity` column | Loud `quantity_invalid` failure (never rendered as garbage, never silently repaired). |
| Missing `channel_id`, missing community (no tenant-wide fallback), unknown command, malformed forwarded payload | `ValueError`; missing community also logs ERROR `inventory.missing_community`. |
| `relay.push` fails | Propagates; no success line is logged. |

## Logging / PII

Every log message has a strict field allowlist (command, platform, op/error class, a `db` row id,
a fixed reason) -- **never** the raw message, item name, target, or `event.actor` (regression:
gh-674; the suite drives every command and outcome with a sentinel string as actor, item and
target, and asserts its absence plus the exact per-message field set). Per-user keys and the
`actor_hash` column are SHA-256 pseudonyms, never raw names.

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
| `src/app.py` | `transform`/`dispatch` -- the full command grammar, data model, directory lock, lossless `give`, and backend wrappers |
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

In CI/release the wasm is built generically: `bundles/Dockerfile.core-bundles` runs
`bundles/build_python_bundles.py`, which builds every `language: python` entry in
`bundles/core-bundles.yaml` -- no per-bundle Dockerfile edit.

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

Current result: 245 tests, `src/app.py` at 100% statement+branch coverage.

## Activation

Registered in `bundles/core-bundles.yaml` (`waddles.core.example.inventory`, activation target
`global`); dark until `waddles.command-inventory` is turned on.
