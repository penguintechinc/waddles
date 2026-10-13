# quote (Python)

`!quote` -- a per-community quote book. DB-backed, first-party (`provider: builtin`,
`app_id: waddles.core.example.quote`, `author: PenguinTech/waddles`, Apache-2.0), **original
Waddles content**: a strangler extraction of the `!quote` command from the legacy
`core/svc_process/bundles/social_quote_process.py` monolith module -- not a third-party port, so
no upstream attribution applies. Ported off the retired `create_dal()` facade onto the structured
`db` capability in `ad38a8f7` (gh-675); modeled on `bundles/python/rank`'s `db` + `kv`-index shape.

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!quote add <text>` | broadcaster / mod | Saves `<text>` (max **500** chars, trimmed) and replies `Saved as quote #N.` |
| `!quote <id>` | anyone | Shows quote `#<id>`, or `Quote #<id> not found.` |
| `!quote random` | anyone | One random quote, or `No quotes found.` |
| `!quote list` | anyone | The 10 most recent quote numbers (`Recent quotes: #7, #6, ...`) or `No quotes have been saved yet.` |
| `!quote remove <id>` | broadcaster / mod | **Hard-deletes** the quote (and its index entry). |

`add`/`list`/`remove` go through the shared grammar (`waddle_sdk.command.parse_command`);
`random` and a bare numeric `<id>` are two deliberate pre-checks outside the fixed `VERBS`
vocabulary (the same layering `rank <user>` uses). Matching is on the exact first token
(`!quotex` / `!quoter` do not match), case-insensitive. Bare `!quote`, a grammar error, or an
unimplemented verb (`!quote set foo`) replies with the usage line -- never silently dropped:

```text
mod>    !quote add the cake is a lie
bot>    Saved as quote #1.
viewer> !quote 1
bot>    #1: "the cake is a lie" — unknown
viewer> !quote add nope
bot>    Only the broadcaster or a moderator can add quotes.
```

Every quote renders `— unknown` for the author on purpose: no author/username column is stored
(raw usernames are PII and live only in the API server's identity table).

## Data model (`db` + `kv`)

One app-owned `db` table, **`quote_entries`** (`bundle.yaml` `data.tables` -- the signal that grants
the `db` capability), columns `seq` + `quote_text`; community scoping is automatic server-side.
Because `db.get` takes only a host-assigned UUID `row_id`, a community-scoped `kv` index maps the
short chat-typeable number to it:

| kv key (`c.<community>.` prefixed by `community_kv`) | Purpose |
|---|---|
| `quote.seq.counter` | Atomic (`increment`) sequence source for `add`. |
| `quote.rowid.<seq>` | `seq -> db row_id` lookup for `<id>` / `remove`. |

Keys are `.`-separated, never `:` (gh-631 -- the real `kv` host rejects `:`; the test suite uses
the shared charset-enforcing fake so a regression fails immediately).

### Failure semantics (fail loud)

Any `kv`/`db` backend failure logs `quote.backend_error` at **ERROR** with only
`{op, error: <exception type>}`, replies `Something went wrong accessing quote storage - please try
again.`, and raises `RuntimeError("quote <op> failed: <Type>")`. A kv index pointing at a missing
row raises `index_stale` rather than answering "not found". Corrupt state (non-UTF-8 index bytes,
a row without a numeric `seq`) raises -- it is never replaced by a default.

**Known limitation:** `add` takes the sequence number, inserts the row, then writes the index; a
`kv` failure *between* the insert and the index write leaves an unindexed row that `random`/`list`
can show but `<id>`/`remove` cannot reach.

## Permissions (V2 structured)

| Id | Why |
|---|---|
| `storage.kv` | Persists the per-community quote store index and its id sequence. |
| `flags.read` | Gates the command behind its `waddles.command-quote` feature flag. |

`data.tables: [quote_entries]` additionally grants the `db` capability (derived by
`bundle_approval_service._derive_capabilities`; no separate permission id). No egress.

Mod gate: `add`/`remove` require a real `is_mod` or `is_broadcaster` `True`; **absent** badge
fields (e.g. the Discord normalizer today) are **denied** (fail closed) with zero `kv`/`db` access.

## Feature flag

`waddles.command-quote`, **default OFF** -- checked after the cheap command match and before the
grammar parse, requested with `default=False` (flag outage / missing `wit_world` -> off). While off
nothing is parsed, logged, or stored.

## Platforms

`consumes` **Twitch** and **Discord** `chat.message` with `command_prefix: ["!quote"]`; replies go
to the event's own origin platform. A `community` context is required (no tenant-wide fallback):
without one `dispatch` logs `quote.missing_community` and raises `ValueError`.

## Logging / PII

Logs carry only `command`, `op`, `platform`, and exception type names -- never quote text, ids,
typed arguments, or the actor. Regression:
`tests/test_backfill.py::test_no_log_line_in_any_flow_contains_user_typed_text`.

## Files

| File | Role |
|---|---|
| `bundle.yaml` / `hub-manifest.yaml` | Manifest (+ data table) / hub-api install-pipeline manifest |
| `src/app.py` | `transform` (parse) / `dispatch` (permission, `kv`/`db` I/O, relay) |
| `src/_entry_wiring.py` | Static entry wiring (see `pyping`'s) |
| `tests/` | Host-native pytest suite (shared charset-enforcing `kv` fake + structured `db` fake) |

## Test

```bash
cd bundles/python/quote
python3.13 -m venv .venv && . .venv/bin/activate
pip install pytest==8.3.3 pytest-cov==5.0.0
pytest --cov=src --cov-branch --cov-report=term-missing   # 100% line + branch
```
