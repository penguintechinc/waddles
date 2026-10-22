# steal (Python)

`!steal @user` -- a risk/reward points steal over the shared community economy. First-party
(`provider: builtin`, `app_id: waddles.core.example.steal`).

> Attribution: inspired by PenguinTwitchBot by SuperPenguinTV (with permission) -- ideas/design
> only, no code copied.

| Command | Behavior |
|---|---|
| `!steal @user` | Attempt a steal: 40% success. Success -> `+loot` (random, bounded by `min(100, target balance, remaining caps)`); fail -> you pay a 10-point penalty to the victim. 60s per-actor cooldown (atomic; a refused attempt restarts it). |
| `!steal list` | Your own attempts/successes. |
| `!steal set cooldown <0-3600>` | Broadcaster/moderator only (fail-closed on absent badges). |

No mention, self-target, an unlinked/ambiguous target, an empty victim, an actor with < 10
points or an exhausted cap all reply a clear message and move no money.

## Economy caveat: winnings are minted, not taken

The host's `economy.transfer`/`wager` only move the *invoking actor's* money (`actor_mismatch`
otherwise), so a literal target->actor transfer is impossible by design. Success is therefore
paid as an actor `wager` payout (host-bounded by the daily mint cap) with the victim's balance as
the loot *bound*; the failure penalty is a real `transfer` actor -> victim. **The victim loses
nothing on a successful steal -- the loot is house-minted.** An uncapped steal is therefore +EV
farming, and two colluding accounts (one parked with a balance as the loot bound, one farming it)
are a currency faucet. The caps below bound that; they do not make it zero-sum.

**Follow-up (needs a host change): a true zero-sum steal.** Debiting the victim requires a new host
economy capability (an actor-from-target transfer, e.g. a policy-gated `economy.seize`) -- the
bundle cannot do it. Until then the capped/idempotent version below is what ships. A bundle-only
alternative is a pot-backed steal like `jackpot` (failure penalties burn into a kv pot, wins are
paid from that pot), which conserves currency but changes the game.

## Anti-collusion caps (gross loot minted by winning steals; a loss never replenishes a cap)

| Cap | Default | Rule |
|---|---|---|
| Per-pair (actor -> target) | `PAIR_WINDOW_CAP = 100` per window | one pair cannot be farmed past the cap |
| Reciprocal block | one window | if the target stole from you (loot reserved or won) this or the previous window, you cannot steal back |
| Per-actor budget | `ACTOR_WINDOW_BUDGET = 300` per window | across all targets |

- Windows are fixed buckets of `WINDOW_SECONDS = 3600`; the bucket index is in the kv key, so a
  window resets with no cleanup and a refused attempt never extends it. A pair can therefore see up
  to 2x a cap across a bucket boundary.
- Caps are **reserved atomically before the roll** (`kv.increment` the worst-case loot, clamp to
  what is left, give the excess back) and settled after it, so concurrent steals cannot overshoot.
  Both directions of an unordered pair share one counter (low/high 32-bit halves), so two
  simultaneous opposite steals cannot both pass. A host refusal gives the reservation back; an
  ambiguous `BackendError` keeps it (a refund could re-mint).
- **Bounded, not stopped**: N sybil accounts still mint N x `ACTOR_WINDOW_BUDGET` per window (a
  community-wide budget is a follow-up). Corrupt counters fail closed.

## Cooldown and replay safety

- The cooldown is one `kv.increment` on a TTL'd key (`kv.increment` is the only compare-and-swap):
  exactly one of any number of concurrent steals by one actor wins the window. `set cooldown 0`
  disables the gate; replay safety does not depend on it.
- The pipeline is at-least-once. `transform` claims the platform `message_id`
  (`waddle_sdk.event_dedupe`) after the cooldown and **before any mutation** with ONE atomic
  `kv.increment` on a per-event, TTL'd marker (`1` = first delivery, `> 1` = redelivery or a
  concurrent duplicate), and skips a redelivery silently (no money, stat, cap or reply). The
  increment is race-free with or without a cooldown and has no 16-event horizon. At-most-once by
  design: a crash after the claim loses one attempt, never repeats it.
- The marker's TTL is the horizon, closed by an age fence: an event whose ingest stamp
  (`occurred_at`) is older than `event_dedupe.MAX_EVENT_AGE_SECONDS` (10 min) is dropped silently,
  never processed, so a redelivery is deduped or dropped but never replayed. A payload with no
  `message_id`, or no valid `occurred_at`, fails loud.
- The claim comes after the cooldown on purpose: an actor on cooldown never creates a marker.

## Key quota (residual DoS -- read this)

kv allows 10,000 live keys per (app, community); a key's quota unit is reclaimed when its TTL
lapses (host fix `4ad3eaae`, **a prerequisite** -- without it every TTL'd key here leaks a unit
and the bundle dies after 10,000 events). Every cap key is TTL-bounded to its window (a budget
key lapses at the end of its window, a pair key at the end of the next, plus a 60 s settle grace),
and a replay marker lives 15 min. A full quota fails **closed**: new keys are refused, steals reply
"unavailable", no money moves, and it heals as keys lapse. What is NOT closed: an attacker with
many linked, funded accounts can still create live keys faster than they lapse (a pair key per
distinct target, ~2 h each) -- bounded per account by the cooldown (60 s default => <= 60
steals/h) and, with `set cooldown 0`, only by the platform's chat rate limit. The per-actor
`steal.attempts.<p>` / `steal.wins.<p>` counters have no TTL (2 permanent keys per player, ~5,000
players fills the quota for good). The proper fix is host-level: a per-actor kv new-key /
write-rate limit (only the host knows the actor); tracked as a follow-up.

## Permissions (V2)

`storage.kv`, `economy.read`, `economy.wager`, `economy.transfer`,
`identity.resolve`, `chat.send:discord`, `chat.send:twitch`.

## State and privacy

kv keys (`steal.cd.<p>`, `steal.attempts.<p>`, `steal.wins.<p>`, `steal.budget.<window>.<p>`,
`steal.pair.<window>.<h>`, `steal.seen.<d>`, `steal.config.cooldown`) use `.` separators (never
`:`) with `p = sha256(community:actor_uuid)`, `h = sha256(community|sorted pair)` and
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
