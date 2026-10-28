# level (Python)

`!level` -- passive chat XP and levels. Every chatter earns XP for talking, a level is derived
from the XP, and `!level leaderboard` shows who is on top. First-party (`provider: builtin`,
`app_id: waddles.core.example.level`), **kv-only**, per community, activation-gated (no feature
flag -- the installed -> enabled -> activated check is the only gate).

Not the same as [`rank`](../rank/README.md): `rank` is a DB-backed ledger whose XP only changes
when a moderator runs `!rank add/sub`; `level` accrues XP automatically from chat. They keep
separate state and can be activated together.

## Commands

| Command | Behavior |
|---|---|
| `!level` | The caller's XP and level: `{user:..} is level 3 with 450 XP (450 XP to level 4).` |
| `!level @user` | The mentioned user's XP and level (level 1 / 0 XP if they have none yet). |
| `!level leaderboard` (alias `top`) | Top 5 chatters by XP, names rendered from mention tokens. |

Anything else after `!level` (`add`, `reset`, free text, extra words) replies with usage and never
echoes what was typed. Every chat line -- commands included -- earns XP.

## XP and levels

* `XP_PER_MESSAGE = 5`, at most one award per `COOLDOWN_SECONDS = 30` per chatter.
* `level(xp) = 1 + isqrt(xp // 100)`: level 1 is 0-99 XP, level 2 is 100-399, level 3 is 400-899,
  level 4 is 900-1599. Integer-only, so exact at every boundary; only XP is stored, so the curve
  can be retuned without a migration. `xp_for_level` is its exact inverse (pinned by test).

## Anti-farm cooldown

The last award time lives in `level.ts.<pseudonym>` and is compared against the event's own
ingest stamp (`occurred_at`), not the wall clock:

| Case | Result |
|---|---|
| 300 messages in one second | one award |
| message exactly 30 s after the last award | award; at 29.9 s refused |
| chatter posting every 20 s | awarded every 40 s (never starved) |
| redelivered / replayed / out-of-order older event | no award, stamp never rewinds |
| two parallel messages from one chatter | exactly one award |

A refused message costs a single `kv.get`. Why not a TTL'd `kv.increment` claim like other
bundles: `kv.increment` re-applies its TTL on every call (a steady chatter would be refused
forever), and a lapsed TTL key that is re-created leaks one unit of the host's 10,000-key quota
each time (`core/bundle_host_kv/src/backend.rs` module docs). Parallel awards are serialized by a
short-lived lock key (`level.lock.<pseudonym>`: `kv.increment` returning 1 = holder, deleted in
`finally`, 10 s TTL only as a crash backstop) with a double-checked cooldown read. The stamp is
written BEFORE the XP increment, so a failure in between under-awards, never over-awards.

## Data model (`kv`, host-scoped per `(tenant, community, app_id)`)

| Key | Value | Written by |
|---|---|---|
| `level.xp.<pseudonym>` | XP total (`kv.increment`, the source of truth) | award |
| `level.ts.<pseudonym>` | last award, epoch seconds | award |
| `level.lock.<pseudonym>` | transient lock | award |
| `level.board` | JSON `[[id, xp], ...]`, top 5, best first | award, when the top 5 changes |

`<pseudonym>` is `sha256("level|<id>")` hex. All keys are `[A-Za-z0-9_.-]` (never `:`, gh-631).
Because XP only grows, top-K is maintained exactly from the board floor. The board
read-modify-write is not atomic across different chatters: a lost update is repaired by that
chatter's next award and never touches the XP counters. A corrupt board is logged at ERROR and
rebuilt on the next award.

**Capacity:** two permanent keys per chatter against the host's 10,000 keys per
`(tenant, community, app)` -- about 4,900 distinct chatters per community. Beyond that the host
rejects new keys, awards fail loudly (`level.kv_failure`) and existing chatters are unaffected.

## PII

* The chatter is the pipeline's `{user:<id>}` token; the bundle never sees a name.
* Keys carry a one-way pseudonym only. The leaderboard VALUE stores the opaque `<id>` because the
  egress detokenizer can only render a name from a `{user:<id>}` placeholder.
* A non-token actor (raw username, no actor, escaped/forged `\{user:..\}`) earns nothing and is
  never keyed, stored or logged. Mentions of unlinked users carry ephemeral ids, so their XP
  does not follow them.
* Logs carry only op names, platform and exception type names. Replies are built from validated
  tokens and integers, then passed through `sanitize_user_text` with exactly those tokens
  allow-listed.

## Permissions

`storage.kv`, `chat.send:discord`, `chat.send:twitch` -- nothing else (no `flags.read`, no
economy/identity capability). Registration in `bundles/core-bundles.yaml` is intentionally not
part of this change.

## Tests

`cd bundles/python/level && python3 -m pytest tests` -- the suite runs on the charset-enforcing
`waddle_sdk.testing` kv fake, including the interleaving races (gated `kv` calls) and a hostile
input matrix. No `wit_fake_db`: the bundle declares no `data.tables`.
