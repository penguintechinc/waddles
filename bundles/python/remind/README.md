# remind (Python)

`!remind` -- a per-user, per-community reminder store. `kv`-only, first-party
(`provider: builtin`, `app_id: waddles.core.example.remind`, `author: PenguinTech/waddles`,
Apache-2.0), **original Waddles content** (not a port; no third-party attribution applies).

> **Delivery is not implemented, by design.** The `stage` WIT world gives a bundle no scheduler /
> cron hook and no way to push a message outside replying to an inbound chat event, so nothing can
> fire a reminder the moment it comes due. The bundle says so in every confirmation and overdue
> line instead of faking it ("`no auto-delivery yet -- check !remind list`"). Real delivery is
> future work tracked against a scheduler/tick capability (gh-613).

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!remind` / `!remind list` | anyone | Lists the caller's own reminders, soonest first: `#1 "text" -- due in 1h 55m` or `-- overdue by 4m (not delivered)`. Pure read. |
| `!remind set <duration> <text>` | anyone | Stores a reminder for the caller. `<duration>` = `d`/`h`/`m`/`s` components **in that order**, case-insensitive, strictly positive (`3d`, `2h30m`, `45m`, `90s`); `<text>` is everything after, kept verbatim. |
| `!remind remove <id>` | anyone | Cancels one of the **caller's own** reminders by numeric id. |

The shared SDK verb vocabulary has no `cancel`, so the verb is `remove`. Any other grammar-legal
verb (`add`/`sub`/`enable`/...), `list` with trailing arguments, a missing argument, or a malformed
duration/id replies with usage text -- never silently dropped.

```text
viewer> !remind set 45m stretch
bot>    Reminder #1 set for in 45m: "stretch" (no auto-delivery yet -- check !remind list).
viewer> !remind list
bot>    #1 "stretch" -- due in 44m
viewer> !remind remove 1
bot>    Reminder #1 removed.
```

## State (`kv`, community- AND user-scoped)

All keys go through `waddle_sdk.community_kv` (`c.<community_id>.` prefix) -- never global or
tenant-wide -- and carry a **SHA-256 pseudonym** of `event.actor`, never the raw identifier.

| Key | TTL | Purpose |
|---|---|---|
| `remind.items.<pseudonym>` | 30 d, refreshed on write | JSON array of `{id, due_ms, text, created_ms}`. |
| `remind.counter.<pseudonym>` | 30 d | Per-user monotonic id source (`increment`). |

Keys are `.`-separated, never `:` (gh-631). Reminders more than 30 days past due are dropped the
next time the caller's list is **written** (`set`/`remove`) -- housekeeping only, never a claim of
delivery; `list` never mutates, so an ancient overdue reminder still shows until then.

### Failure semantics

| Condition | Behavior |
|---|---|
| `kv` backend error | ERROR `remind.kv_error` (`op` + exception type only), chat reply "remind is temporarily unavailable", then `RuntimeError("remind kv <op> failed: <Type>")` -- fail loud. |
| Corrupt stored list (non-UTF-8, non-JSON, wrong shape, items missing `id`/`due_ms`/`text`) | ERROR `remind.state_corrupt` (context + community + pseudonym) and treated as empty; `list` leaves the blob untouched, the next `set` replaces it. Logged loudly, never silent. |
| No `community` on the envelope | ERROR `remind.missing_community` + `ValueError` (no tenant-wide fallback). |
| Missing `channel_id` / unknown action | `ValueError`. |

## Permissions (V2 structured)

| Id | Why |
|---|---|
| `storage.kv` | Persists per-community reminders and their counters. |
| `flags.read` | Gates the command behind its `waddles.command-remind` feature flag. |

The bundle also reads the host `clock` (`now_millis`) to compute due times. No `db`
(`data.tables: []`), no egress, no moderator-gated verb (every caller manages only their own
reminders).

## Feature flag

`waddles.command-remind`, **default OFF**, checked after the cheap command match and before the
grammar parse, requested with `default=False` (flag outage / missing `wit_world` -> off). While off
nothing is parsed, stored or logged.

## Platforms

`consumes` **Twitch** and **Discord** `chat.message` with `command_prefix: ["!remind"]`; replies go
back to the event's own origin platform + channel.

## Logging / PII

Logs carry `action`, `op`, `community`, reminder ids, exception type names, and (corruption path
only) the one-way pseudonym -- never the reminder text, the typed duration, or the raw actor.
Regression: `tests/test_backfill.py::test_no_log_line_in_any_flow_contains_reminder_text_duration_or_actor`.

## Files

| File | Role |
|---|---|
| `bundle.yaml` / `hub-manifest.yaml` | Manifest + hub-api install-pipeline manifest |
| `src/app.py` | `transform` (recognize/parse) / `dispatch` (all `kv` I/O + relay) |
| `src/_entry_wiring.py` | Static entry wiring (see `pyping`'s) |
| `tests/` | Host-native pytest suite (shared charset-enforcing `kv` fake) |

## Test

```bash
cd bundles/python/remind
python3.13 -m venv .venv && . .venv/bin/activate
pip install pytest==8.3.3 pytest-cov==5.0.0
pytest --cov=src --cov-branch --cov-report=term-missing   # 100% line + branch
```
