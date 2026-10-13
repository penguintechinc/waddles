# rank (Python)

`!rank` -- a community-scoped chat XP/level ledger. Community-scoped fun/engagement content,
first-party (`provider: builtin`, `app_id: waddles.core.example.rank`), **inspired by**
superpenguintv (Psychoboy)'s [PenguinTwitchBot](https://github.com/Psychoboy/PenguinTwitchBot)
chat rank feature -- not a literal port. No original source code or text is reused; the command
grammar, data model, level formula, optimistic-concurrency update loop, and leaderboard rendering
are written fresh for this bundle. See `bundle.yaml`'s `author`/`notice` fields for the credit,
and contrast `bundles/csharp/superpenguin-roll`, which *is* a line-for-line port and carries the
full verbatim MIT notice because it reuses original code.

**DB-backed**, built on the structured `db` capability (`insert`/`get`/`query`/`update`/`delete`,
#623, already on `release/v3.0.X`) -- the same template `bundles/python/loyalty` (PR #630)
establishes for a `db`+`kv` bundle; this bundle mirrors that shape end to end.

## Commands

| Command | Grammar shape | Behavior |
|---|---|---|
| `!rank` | bare | Caller's own level + XP. |
| `!rank <user>` | documented grammar extension (see `src/app.py`) | That user's level + XP. Level 1 / 0 XP if they have never earned any. |
| `!rank top` | `top` sub-module | Leaderboard: top 10 ranks, highest XP first. |
| `!rank add <amount> <user>` | `add` verb | Broadcaster/moderator only. Adds `<amount>` XP to `<user>`. |
| `!rank sub <amount> <user>` | `sub` verb | Broadcaster/moderator only. Removes `<amount>` XP from `<user>`, clamped at `0` (never negative). |

Any other grammar-legal verb (`enable`/`disable`/`remove`/`reset`) or a malformed `!rank ...`
replies with usage text -- never silently dropped.

## Level formula

`level(xp) = 1 + isqrt(xp // 100)` -- a quadratic curve, integer-only (no float rounding at a
boundary): level 1 spans `0`-`99` XP, level 2 `100`-`399`, level 3 `400`-`899`, level 4
`900`-`1599`, etc. `_xp_for_level(level)` is the exact inverse (`100 * (level - 1) ** 2`),
proven by direct test (`test_xp_for_level_is_the_exact_inverse_of_level_for_xp`). No level column
is persisted -- only `xp`, so the curve can be retuned later without a migration.

## Data model (`db` + `kv`)

One app-owned `db` table, `rank_progress` (declared in `bundle.yaml`'s `data.tables` -- the
signal that grants this bundle the `db` capability), rows scoped per-community automatically by
the host. Two columns: `actor_hash` (SHA-256 hex pseudonym of the ranked actor -- never a raw
username) and `xp` (integer total).

The committed `wit/waddle-bundle/stage.wit` `db` interface has no column-equality lookup -- only
`insert`/`get(row_id)`/`query(limit, offset, order_by)`/`update`/`delete`. This bundle therefore
also uses `kv` (`storage.kv`) as a lookup index, **`rank.rowid.<pseudonym> -> row_id`** (`.`
separated, **never `:`** -- the real `kv` host capability rejects a colon as a guest-key byte,
gh-631; this bundle's own test suite uses the shared charset-enforcing fake,
`waddle_sdk.testing.install_fake_kv_host`, specifically so a colon regression here fails the test
suite immediately instead of only in production), so a single user's rank lookup/adjustment is an
O(1) kv read + an exact `db.get`/`db.update` rather than an unbounded table scan (`db.query()`'s
`limit` is host-clamped to 200 rows). The leaderboard is the one operation that genuinely needs
`db.query(order_by="xp", descending=True)`.

Writes use optimistic concurrency (`expected_version`, from `db.py`'s `update()`), with a bounded
fetch-mutate-update retry loop (`_db_update_with_retry`, 5 attempts) on `db.ConflictError`.

**Leaderboard entries show a `player-<hash prefix>` tag, not a display name.** `actor_hash` is a
one-way SHA-256 hash -- there is no reverse lookup from a stored row back to a chat-visible name
without a hub-side detokenization step this bundle has no access to (PII tokenization: raw PII
lives only inside the hub/API server). `!rank`/`!rank <user>` CAN show a real name because the
live chat event's own actor/typed-target text is only ever echoed back into that same reply,
never persisted. See `src/app.py`'s module docstring for the full rationale.

## Examples

```text
viewer> !rank
bot>    viewer is level 1 (0 XP).
mod>    !rank add 150 viewer
bot>    Added 150 XP to viewer. Now level 2 (150 XP).
viewer> !rank top
bot>    Top rank: 1. player-3f2a9c1e: level 2 (150 XP)
viewer> !rank add 5 viewer
bot>    only moderators/broadcasters can adjust rank XP
```

## Permissions (V2 structured)

| Id | Why |
|---|---|
| `storage.kv` | Persists the per-user `rank.rowid.<pseudonym> -> row_id` lookup index. |
| `flags.read` | Gates the command behind its `waddles.command-rank` feature flag. |

`data.tables: [rank_progress]` additionally grants the `db` capability (derived by
`bundle_approval_service._derive_capabilities`; no separate permission id). No egress. Mod gate:
`add`/`sub` require a real `is_mod` or `is_broadcaster` `True`; **absent** badge fields (e.g. the
Discord normalizer today) are **denied** (fail closed) with zero `kv`/`db` access.

## Platforms

`consumes` **Twitch** and **Discord** `chat.message` with `command_prefix: ["!rank"]`; replies go
back to the event's own origin platform + channel. A `community` context is required (no
tenant-wide fallback): without one `dispatch` logs `rank.missing_community` and raises
`ValueError`.

## Failure semantics (fail loud)

Any `kv`/`db` backend failure logs `rank.backend_error` at **ERROR** with only
`{op, error: <exception type>}`, replies `rank is temporarily unavailable, try again shortly.`, and
raises `RuntimeError("rank <op> failed: <Type>")`. A kv index pointing at a missing row is
`index_stale` (never silently re-created, which would orphan/duplicate the user's row). An
`add`/`sub` that loses the optimistic-concurrency race 5 times in a row fails loud as
`db_update_retry`.

## Logging / PII

Logs carry only `command`, `op`, `role_signal` and exception type names -- never a typed target,
amount, grammar text or `event.actor`. Regression:
`tests/test_backfill.py::test_no_log_line_in_any_flow_contains_a_typed_target_or_the_raw_actor`.

## Known limitation: self vs. target identity normalization

`!rank add|sub|<user>` hash the **lower-cased, `@`-stripped typed name**, while bare `!rank`
hashes the caller's **raw `event.actor`**. They agree when the platform actor is already
lower-case (Twitch logins) but a mixed-case actor (`Alice`) will not see XP a mod granted via
`!rank add 150 Alice` (stored under `alice`). Resolved once the PII-tokenization pipeline (#429)
supplies opaque actor tokens; until then prefer lower-case actors.

## Feature flag

Gated behind `waddles.command-rank`, defaulted OFF (`critical-rules.md` Feature Flags & License
Tiers) -- checked in `transform()` after the cheap `!rank` command-name match and before the real
grammar parse.

## Follow-up (not implemented here): automatic per-message XP accrual

Today XP only changes via the `add`/`sub` admin commands above -- there is no message-received
hook awarding XP per chat line. That needs a `process-stage` path running on every chat message
(not just `!rank ...`) plus a rate-limit/cooldown policy, both open design questions out of scope
for this PR. See `src/app.py`'s module docstring for the full note -- this is a documented
follow-up, not a silently-stubbed hook.

## Known, pre-existing platform gap (not fixed by this PR)

The richer declarative `data.table.columns[]` schema (`hub_api/services/bundle_data_schema.py`)
that would let hub-api provision this bundle's table columns automatically is, by that module's
own docstring, "not wired into onboarding yet." This bundle declares its table the same way every
other `db`-capable bundle can today (`data.tables: [rank_progress]`); wiring the richer schema is
a separate, already-tracked platform follow-on (same gap `loyalty`'s own README documents).

## Files

| File | Role |
|---|---|
| `bundle.yaml` | Manifest -- app id, consumes rules, limits, data table, attribution metadata |
| `hub-manifest.yaml` | hub-api install-pipeline manifest (separate schema consumer, see its own header comment) |
| `src/app.py` | `transform`/`dispatch` -- the full command grammar, level formula, data model, and backend wrappers |
| `src/_entry_wiring.py` | Static `bundle_compiler`-shaped entry wiring (see `pyping`'s own) |
| `tests/` | Host-native pytest suite (fake `wit_world`: flags/kv (shared charset-enforcing fake)/db/relay/log, no wasmtime) |

## Build

```bash
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$PWD":/repo -w /repo python:3.13-slim-bookworm \
  bash -c "pip install --no-cache-dir componentize-py==0.25.1 && \
    componentize-py -d wit/waddle-bundle -w stage \
      componentize -p sdk/waddle-sdk/src -p bundles/python/rank/src \
      waddle_sdk._component_entry -o /tmp/rank.wasm"
```

Confirmed building a valid wasm component.

## Test

```bash
cd bundles/python/rank
python3.13 -m venv .venv && . .venv/bin/activate
pip install pytest==8.3.3 pytest-cov==5.0.0
pytest --cov=src --cov-branch --cov-report=term-missing   # 100% line + branch
```

`tests/conftest.py` puts `src/` and `sdk/waddle-sdk/src` on `sys.path`, so no package install is
needed. `test_app.py` covers grammar/CRUD/level math; `test_backfill.py` adds the mod-gate matrix,
amount-parsing edges, PII-free-log regression, flag fail-closed and `_entry_wiring` checks.

## Activation

Registered in `bundles/core-bundles.yaml` (`waddles.core.example.rank`, batch 3).
