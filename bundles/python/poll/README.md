# poll (Python)

`!poll` -- chat-command community polls over the shared `community_polls` / `poll_options` /
`poll_votes` tables (migration 028; the same tables `hub_api/blueprints/v1/community_polls.py`'s
REST API reads and writes). First-party (`provider: builtin`, `app_id: waddles.core.example.poll`,
`author: PenguinTech/waddles`, Apache-2.0), **original Waddles content**: a strangler extraction of
the bot_process monolith's `core/svc_process/bundles/community_polls_process.py` -- not a
third-party port, so no upstream attribution applies.

> **STATUS: not buildable / not testable on `release/v3.0.X` -- needs a port to the structured
> `db` API.** `src/app.py` still does `from waddle_sdk.db import DALError, create_dal` (line ~88),
> but `create_dal()` / `TableProxy` / `AsyncQuerySet` were **removed** from `waddle-sdk`'s `db`
> module when the structured `insert/get/query/update/delete` capability was wired in
> (commit `2d404393`; design rule: "no bundle-supplied SQL, ever"). `import app` raises
> `ImportError: cannot import name 'create_dal'`, so this bundle's own `tests/` cannot even be
> collected, and a `componentize-py` build of it would fail at the same import. `quote` was ported
> off the retired facade in `ad38a8f7` (gh-675 tracks `quote` + `chat`; `poll` is the same defect
> and is **not** listed there). Because the structured `db` interface gives a bundle **exactly one
> app-owned table** (no `table` parameter, no column-equality filter, no cross-table access), a
> faithful port is a data-model decision -- not a mechanical swap: either one denormalized
> app-owned table plus a `community_kv` index (the `quote`/`rank` shape), or a new host capability
> for the shared poll tables that hub-api's REST API also uses. The behavior below is the
> **intended** behavior the existing code implements; nothing here has been re-verified by a
> running test on this branch.

The shared command grammar (`waddle_sdk.command`) has no `create`/`vote`/`close`/`view` verb, so the
five domain actions map onto its fixed vocabulary:

## Commands

| Command | Grammar verb | Who | Behavior |
|---|---|---|---|
| `!poll add "title" "opt1" "opt2" ...` | `add` | broadcaster / mod | Creates a poll (title + options). |
| `!poll set <poll_id> <option_number>` | `set` | anyone | Votes for an option (approval voting: a caller may vote for several options; re-voting for the same option just refreshes `voted_at`). |
| `!poll remove <poll_id>` | `remove` | broadcaster / mod | **Ends** the poll (`is_active` off) -- never deletes rows. |
| `!poll list` | `list` | anyone | Lists the community's active polls. |
| `!poll list <poll_id>` | `list` + 1 arg | anyone | Shows one poll with its per-option tallies. |

Malformed input (bad quoting, non-numeric id, missing option) replies with the usage line -- never
silently dropped.

```text
mod>    !poll add "Best snack?" "Tacos" "Pizza"
bot>    (poll created -- reply text lives in src/app.py)
viewer> !poll set 5 2
bot>    (vote recorded)
mod>    !poll remove 5
bot>    (poll ended)
```

## Data model (shared Postgres tables, via the retired DAL facade)

| Table | Used for |
|---|---|
| `community_polls` | One row per poll. Creator identity is **never** raw: `created_by` is made nullable by migration 097 and a SHA-256 `created_by_hash` is stored instead. |
| `poll_options` | One row per option (`sort_order` = display position). |
| `poll_votes` | One row per `(poll, option, voter)`; the voter is the SHA-256 pseudonym in `ip_hash` (reused column). |

`bundle.yaml` declares `data.tables: [community_polls, poll_options, poll_votes]` -- the non-empty
list is what grants the `db` capability (no `storage.kv` needed; this bundle never touches `kv`).

## Permissions (V2 structured)

| Id | Why |
|---|---|
| `flags.read` | Reads the `waddles.command-poll` PostHog feature flag via `feature_enabled` to gate the command. |

No egress, no `storage.kv`. Mod gate: `add`/`remove` require a real `is_mod` or `is_broadcaster`
`True`; **absent** badge fields (e.g. the Discord normalizer today) are **denied** (fail closed).
`set`/`list` are open to anyone.

## Feature flag

`waddles.command-poll`, **default OFF**. Registered in `bundles/core-bundles.yaml`, but the legacy
`bot_process._FEATURE_MODULES` path is untouched -- cut-over and activation are a separate change.

## Platforms

`consumes` **Twitch** and **Discord** `chat.message` with `command_prefix: ["!poll"]`; replies go
back to the event's own origin platform + channel. A `community` context is required.

## Failure semantics (intended)

Any `db` failure is logged at ERROR, replied to chat with a generic retry message, and re-raised
(fail loud). **PII:** the voter/creator are only ever persisted as non-reversible SHA-256
pseudonyms; log lines must carry only `action`/`op`/exception type, never a typed title, option or
the actor.

## Files

| File | Role |
|---|---|
| `bundle.yaml` / `hub-manifest.yaml` | Manifest (+ shared data tables) / hub-api install-pipeline manifest |
| `src/app.py` | `transform` (recognize + forward badge signal) / `dispatch` (all `db` work + relay) |
| `src/_entry_wiring.py` | Static entry wiring (see `pyping`'s) |
| `tests/` | `test_app.py` + `fake_wit_db.py` -- written against the **retired** raw-SQL facade; stale until the port lands |

## Test

```bash
cd bundles/python/poll
python3.13 -m venv .venv && . .venv/bin/activate
pip install pytest==8.3.3 pytest-cov==5.0.0
pytest --cov=src --cov-branch --cov-report=term-missing
# CURRENTLY FAILS at collection: ImportError: cannot import name 'create_dal' from 'waddle_sdk.db'
```

After the port, the suite should be rebuilt on the structured fake (`wit_fake_db.py`, as `quote`
and `rank` do) and carry the standard backfill set: mod-gate matrix, corrupt-state loudness,
PII-free-log regression, flag fail-closed default and `_entry_wiring`.
