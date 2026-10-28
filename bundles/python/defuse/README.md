# defuse (Python)

`!defuse [amount|all]` -- a solo wager game over the shared community economy. First-party
(`provider: builtin`, `app_id: waddles.core.example.defuse`).

> Attribution: inspired by PenguinTwitchBot by SuperPenguinTV (with permission) -- ideas/design
> only, no code copied.

| Command | Behavior |
|---|---|
| `!defuse` / `!defuse <amount>` / `!defuse all` | Stake (default 10; `all` = host `max_bet`); win with `odds`% (default 45) for 2x payout (net `+stake`), else lose the stake. 10s cooldown. |
| `!defuse list` | Your own plays/wins. |
| `!defuse set odds <1-95>` | Broadcaster/moderator only (fail-closed on absent badges). |

Each game settles in one atomic `economy.wager`. Bad/zero stake, insufficient funds, over-cap
and unlinked accounts reply a clear message and move nothing (funds/cap are pre-checked before the
cooldown is claimed, so a refusal never burns it).

## Cooldown and replay safety

- The cooldown is one `kv.increment` on a TTL'd key (`kv.increment` is the only compare-and-swap):
  exactly one of any number of concurrent plays by one actor wins the window. The host re-applies
  the TTL on every call, so a refused attempt restarts the window (the reply says so).
- The pipeline is at-least-once. `transform` claims the platform `message_id`
  (`waddle_sdk.event_dedupe`) after the cooldown and **before the roll, the wager and the stat
  counters** with ONE atomic `kv.increment` on a per-event, TTL'd marker (`1` = first delivery,
  `> 1` = redelivery or a concurrent duplicate), and skips a redelivery silently (no re-roll, no
  second wager, no double-counted plays/wins, no reply). The increment is race-free and has no
  16-event horizon. At-most-once by design: a crash after the claim loses one play, never repeats
  it.
- The marker's TTL is the horizon, closed by an age fence: an event whose ingest stamp
  (`occurred_at`) is older than `event_dedupe.MAX_EVENT_AGE_SECONDS` (10 min) is dropped silently,
  never processed -- a redelivery is deduped or dropped, never replayed. A payload with no
  `message_id`, or no valid `occurred_at`, fails loud and moves nothing. The claim comes after the
  cooldown, so an actor on cooldown never creates a marker.
- Quota (residual): markers live 15 min under kv's 10,000-key quota, whose units are reclaimed on
  TTL expiry (host fix `4ad3eaae`, **a prerequisite**). A full quota fails closed (the play is
  refused, no money moves) and heals as markers lapse; a swarm of linked, funded accounts can
  still hold it full (each is bounded by the 10 s cooldown). The proper bound is a host-level
  per-actor kv new-key / write-rate limit (follow-up).

## Permissions (V2)

`storage.kv`, `economy.read`, `economy.wager`, `identity.resolve`,
`chat.send:discord`, `chat.send:twitch`.

## State and privacy

kv keys (`defuse.cd.<p>`, `defuse.plays.<p>`, `defuse.wins.<p>`, `defuse.seen.<d>`,
`defuse.config.odds`) use `.` separators (never `:`) with `p = sha256(community:actor_uuid)` and
`d = sha256(platform|message_id)[:32]`. No usernames, uuids or platform message ids in keys or
logs.

## Gating

Activation-gated only -- no feature flag (installed -> enabled -> activated).

## Output safety

`event.actor` is interpolated into replies. The host-minted `{user:<token>}` actor stays live (egress renders the display name); any other value, and every typed echo, goes through `waddle_sdk.safe_text.sanitize_user_text`.

## Stage split

The host serves `economy` and `identity` to the **process** stage only (`core/svc_process`;
`core/svc_action` has no handler for them) and `relay` to the **action** stage only. So `transform`
does the whole command -- identity resolution, balance/limit reads, wagers, kv state, the replay
claim -- and `dispatch` only relays the finished reply. A backend failure cannot be relayed from
the process stage, so it travels as a `failure` marker on the reply event: `dispatch` relays the
"unavailable" reply and then raises. If `dispatch` fails after `transform` settled the money, the
redelivery is deduped by the replay claim: the reply may be lost, the money is never moved twice.
The tests run against `waddle_sdk.testing.StageGuard` fakes (a capability called from the wrong
stage fails the test) and `scripts/ci/tests/test_bundle_stage_gate.py` fails the build if
`dispatch` can reach an economy/identity call.
