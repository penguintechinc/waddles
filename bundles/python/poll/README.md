# poll (Python)

`!poll` -- community polls in chat: moderators create a poll with up to 10 options, anyone votes by
number, moderators close it and the final results are posted. First-party (`provider: builtin`,
`app_id: waddles.core.example.poll`, `author: PenguinTech/waddles`, Apache-2.0), **original Waddles
content**: a strangler extraction of the bot_process monolith's
`core/svc_process/builtin_handlers/community_polls_process.py` -- not a third-party port, so no
upstream attribution applies.

**Status (1.0.2): working end to end.** 1.0.0/1.0.1 imported `waddle_sdk.db.create_dal`, which was
removed when the structured `db` capability landed (gh-675 is the same defect class for `quote`),
so the bundle could neither load nor build. 1.0.2 is a port onto the structured `db` +
`community_kv` APIs -- see [Data model](#data-model-db--kv).

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!poll add "title" "opt1" "opt2" ...` | broadcaster / mod | Creates a poll: title <= 200 chars, **2-10 options** of <= 100 chars each (case-insensitively unique). Replies with the poll id and how to vote. |
| `!poll set <poll_id> <option_number>` | anyone | Votes for an option. **Approval voting:** you may vote for several options of one poll; voting for the same option again is a no-op ("You already voted for option N on poll P."). |
| `!poll remove <poll_id>` | broadcaster / mod | **Ends** the poll and posts the final per-option results. Idempotent: closing a closed poll just re-posts its results. Never deletes data. |
| `!poll list` | anyone | The 10 newest active polls (looked up among the 50 newest). |
| `!poll list <poll_id>` | anyone | One poll with live counts (or final counts once closed). |

The shared command grammar (`waddle_sdk.command`) has no `create`/`vote`/`close`/`view` verb, so
the five domain actions map onto its fixed vocabulary (`add`/`set`/`remove`/`list`). Malformed
input (bad quoting, non-numeric id, missing option) replies with a usage line -- never silently
dropped. Poll ids and option numbers are ASCII digits only.

```text
mod>    !poll add "Best snack?" "Tacos" "Pizza" "Sushi"
bot>    Poll created! ID: 5
        Title: Best snack?
        Options:
          1. Tacos
          2. Pizza
          3. Sushi
        Vote with: `!poll set 5 <option_number>`
viewer> !poll set 5 2
bot>    Vote recorded for option 2 on poll 5!
viewer> !poll set 5 2
bot>    You already voted for option 2 on poll 5.
viewer> !poll list 5
bot>    Poll 5: Best snack? [Active]
          1. Tacos (0 votes)
          2. Pizza (1 vote)
          3. Sushi (0 votes)
mod>    !poll remove 5
bot>    Poll 5 closed: Best snack?
        Results:
          1. Tacos (0 votes)
          2. Pizza (1 vote)
          3. Sushi (0 votes)
```

## Permissions (V2 structured)

| Id | Why |
|---|---|
| `storage.kv` | The poll id counter, the id -> row index, per-option vote tallies and the one-vote-per-voter markers. |
| `flags.read` | Reads the `waddles.command-poll` PostHog feature flag via `feature_enabled` to gate the command. |

The `db` capability is granted by the manifest's `data.tables: [poll_records]`. No egress.

**Mod gate (fail closed):** `add`/`remove` require a real boolean `is_mod` or `is_broadcaster`
`True`. A **string** badge such as `"false"` is not trusted (1.0.2 fixed a bypass where
`bool("false")` let non-moderators create and close polls); badge fields that are absent
(e.g. a normalizer that emits none) are denied. `set`/`list` are open to anyone.

## Data model (`db` + `kv`)

The structured `db` interface gives a bundle **exactly one app-owned table** (no `table`
parameter, no column-equality filter), so the three legacy shared tables are replaced by:

- **`poll_records`** (`db`) -- one row per poll: `poll_id` (chat-visible number), `title`,
  `options` (JSON array), `is_active`, `created_by_hash` (SHA-256 of the creator, never a
  username) and `results` (final counts, written at close).
- **`kv`** (`community_kv`, all keys `.`-separated) -- `poll.seq.counter` (atomic id allocation),
  `poll.rowid.<id>` (id -> row), `poll.tally.<id>.<n>` (atomic per-option counters) and
  `poll.voted.<id>.<n>.<voter>` (one-vote markers claimed with an atomic `increment`, so two
  simultaneous identical votes can never both count). Tallies and markers carry a 30-day TTL;
  **closing a poll snapshots the final counts into `results`**, so closed-poll results are
  permanent regardless of TTL.

A two-step write that fails half-way is compensated before the error surfaces (an inserted row
whose index write failed is deleted; a claimed vote marker whose tally increment failed is
released), so a failure never strands an unreachable poll or a vote that was claimed but never
counted. A vote landing in the same instant a poll is closed may be excluded from the final
snapshot -- the snapshot is authoritative.

Scoping: rows and `kv` are per-community. A tenant-wide activation (`community: null`, alpha's
only shape today) is scoped under the `"0"` sentinel the same way `lurk` and `community_kv` do; an
empty-string community is a caller bug and raises.

## Feature flag

`waddles.command-poll`, **default OFF**. Registered in `bundles/core-bundles.yaml`, but the legacy
`bot_process._FEATURE_MODULES` path is untouched -- cut-over and activation are a separate change.

## Platforms

`consumes` **Twitch** and **Discord** `chat.message` with `command_prefix: ["!poll"]`; replies go
back to the event's own origin platform + channel.

## Failure semantics (fail-loud)

| Condition | Behavior |
|---|---|
| Any `kv`/`db` call fails | ERROR `poll.backend_error` (`op` + the error class **name** only), chat reply "polls are temporarily unavailable, try again shortly.", then `RuntimeError`. Exactly one relay. |
| Corrupt row (bad `options`/`results` JSON, non-boolean `is_active`, closed poll with no snapshot), unparseable tally, non-UTF-8 index | Loud failure (`row_decode`/`tally_decode`/`index_decode`) -- never treated as "not found", never repaired. |
| Index points at a row `db.get` can no longer find | Loud `index_stale` failure. |
| Concurrent close | Version-gated; the loser re-reads and reports the winner's snapshot. Exhausting 5 conflict retries is a loud `db_update_retry` failure. |
| Missing `channel_id`, empty-string community, unknown action | `ValueError`. |

**Logging / PII:** log lines carry only `op`/`action`/option counts/exception class -- never a
title, option text, argument or the actor. The creator and every voter are stored only as SHA-256
digests (voters truncated to 64 bits inside the marker key). `tests/test_app.py::TestHygiene`
drives every command and failure with sentinel strings and asserts their absence.

## Files

| File | Role |
|---|---|
| `bundle.yaml` / `hub-manifest.yaml` | Manifest (+ the one data table) / hub-api install-pipeline manifest |
| `src/app.py` | `transform` (recognize + forward badge signal) / `dispatch` (all `kv`/`db` work + relay) |
| `src/_entry_wiring.py` | Static entry wiring (see `pyping`'s) |
| `tests/` | `test_app.py` + `wit_fake_db.py` (structured in-memory `db` fake; the real `waddle_sdk` runs over a fake `wit_world`) |

## Test

```bash
cd bundles/python/poll
python3.13 -m venv .venv && . .venv/bin/activate
pip install pytest==8.3.3 pytest-cov mypy==1.14.1 ruff==0.14.1
pytest --cov=src --cov-branch --cov-report=term-missing   # 122 tests, 100% line + branch
mypy --strict src
```

The suite covers: the module loads and uses only structured `db` ops (a static guard against any
retired `waddle_sdk.db` symbol), the full create -> vote -> view -> close lifecycle through
`transform` -> `dispatch`, the mod-gate matrix including the string-badge bypass, validation
bounds, every failure and compensation path, tenant-wide scope, per-invocation op budgets
(<= 64 `kv` / `db` ops), PII-free logs and kv-key charset.
