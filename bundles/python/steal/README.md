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
  disables the gate (and with it the serialization the replay ring relies on -- see below).
- The pipeline is at-least-once. `transform` forwards the platform `message_id`; `dispatch`
  records it (`waddle_sdk.event_dedupe`, one ring key per actor) after the cooldown and **before any
  mutation**, and skips a redelivery silently (no money, stat, cap or reply). At-most-once by
  design: a crash after the claim loses one attempt, never repeats it. A payload with no
  `message_id` fails loud. The ring remembers the last 16 events per actor and is read-modify-write,
  so with the cooldown disabled two same-instant deliveries of one event can race it (the host's
  per-event wager/transfer idempotency still bounds the money).

## Permissions (V2)

`storage.kv`, `economy.read`, `economy.wager`, `economy.transfer`,
`identity.resolve`, `chat.send:discord`, `chat.send:twitch`.

## State and privacy

kv keys (`steal.cd.<p>`, `steal.attempts.<p>`, `steal.wins.<p>`, `steal.budget.<window>.<p>`,
`steal.pair.<window>.<h>`, `dedupe.steal.<p>`, `steal.config.cooldown`) use `.` separators (never
`:`) with `p = sha256(community:actor_uuid)` and `h = sha256(community|sorted pair)`. No usernames,
uuids or platform message ids in keys or logs.

## Gating

Activation-gated only -- no feature flag (installed -> enabled -> activated).

## Output safety

`event.actor` is interpolated into replies. The host-minted `{user:<token>}` actor stays live (egress renders the display name); any other value, and every typed echo, goes through `waddle_sdk.safe_text.sanitize_user_text`.
