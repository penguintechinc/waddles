# lottery (Python)

`!lottery` -- a periodic ticket lottery. First-party (`provider: builtin`,
`app_id: waddles.core.example.lottery`), net-new Waddles content (a generic ticket-lottery
mechanic; no attribution required). Points only; no real-money ties. Activation-gated only -- no
feature flag.

| Command | Who | Behavior |
|---|---|---|
| `!lottery buy <n>` (verb form `!lottery add <n>`) | anyone | Buy `n` tickets (1-10) at 10 points each. The first buy after a draw opens the next round. |
| `!lottery draw` | mod any time, anyone once due | Draw one winner, weighted by tickets. A moderator's early draw first closes sales and asks to repeat once the settle window passed. |
| `!lottery claim [round]` | the winner | Collect the pot. |
| `!lottery` / `!lottery list` | anyone | Pot, tickets sold, players, time left. |

Rounds: sales close 1 h after a round opens (`DRAW_INTERVAL_SECONDS`). The platform has no
scheduler capability, so an elapsed interval simply makes `!lottery draw` callable by EVERYONE (the
draw is deterministic-random and election-guarded, so who triggers it does not matter). A draw
needs at least 2 players; with fewer, sales are extended by another interval.

Handled with a reply and no state change: bad / out-of-range count, insufficient funds, over the
host `max_bet`, per-player cap (20 tickets/round), sold-out round (soft cap 1000), sales closed,
cooldown (5 s), unlinked actor, draw with nothing running / already drawn / too early / too few
players, claim by a non-winner. Mod gating reads the strict boolean `is_mod` / `is_broadcaster`
badge (`is True`, fail closed).

## Money and integrity (read this)

**The pot is claimed, not pushed.** The host only moves the invoking actor's own account
(`economy.wager`'s `user` must be the actor), and the draw is triggered by someone else, so it
cannot credit the winner. Tickets are debited when bought (`wager(cost, 0)`; the points live on only
as the `sold` counter in `community_kv`) and the winner collects with their OWN `claim`: one
`wager(f, chunk + f)` on their account, net `+chunk`, where `f = ceil(chunk / 99)` is the smallest
stake the host's `payout <= 100 x stake` rule lets carry the payout (paid straight back in the same
call -- the winner needs 1 point in hand). A pot above `99 x max_bet` is collected over several
claims; a per-round `paid` counter tracks it.

- **Conserved, bounded.** `pot = sold x 10`, and `sold` is incremented only AFTER the matching
  debit, so `pot <= points burned`: the bundle never mints more than was spent on tickets (a
  `MAX_POT` clamp bounds even a corrupted counter). Bounds: 10 tickets per purchase, 20 per player
  per round, 1000 per round (soft: concurrent buyers can overshoot by a purchase or two and the pot
  still equals what was paid), declared `max_bet: 500` per call, and the host's daily payout (mint)
  caps on top (`quota_exceeded` -> "try again later", reservation released).
- **Debit first.** Purchase = debit, then allocate ticket positions, then write the entry, then
  count the sale. A crash in between strands paid points or leaves a ghost position (skipped by the
  draw); it can never create an unpaid ticket or inflate the pot.
- **Time fence, not a lock.** kv has no transaction, so sales are refused from `close` and a draw
  is refused until `close + 10 s` (longer than any in-flight 2 s purchase).
- **Idempotent draw.** `lottery.drawing.<round>` is a 60 s `kv.increment` election (one drawer);
  the result is written once to `lottery.win.<round>`; a later `draw` (another moderator, a second
  try, a redelivery) just reports the stored winner and changes nothing. A crash mid-draw heals
  when the election lapses.
- **Idempotent on redelivery.** `transform` forwards the platform `message_id`; every mutating
  command claims it via `waddle_sdk.event_dedupe` -- ONE atomic `kv.increment` on a per-event TTL'd
  marker (`1` = first delivery) -- AFTER the cheap prechecks and the atomic per-actor cooldown and
  BEFORE any ticket money, payout or state change; a redelivery is skipped silently. Events older
  than `event_dedupe.MAX_EVENT_AGE_SECONDS` are dropped; a payload without `message_id` / valid
  `occurred_at` fails loud and moves nothing. At-most-once: a crash after the claim loses that
  operation, never repeats it.
- **Atomic CAS everywhere.** Round opening, the drawer election and the claim reservation
  (`paid.<round>`) are `kv.increment` elections -- never read-then-set.
- **Ambiguous host failure.** A `BackendError` from `wager` retries the identical call once (the
  host dedupes by event), then fails loud WITHOUT releasing the claim reservation. Other
  unexpected refusals (actor mismatch, idempotency conflict, unprovisioned economy) are logged at
  ERROR, answered generically and raised.
- **Fair draw.** A position `r` is drawn uniformly over the allocated ticket positions and mapped
  to its purchase by probing up to `MAX_BUY` entry keys (each purchase owns a contiguous range
  ending at its key). A ghost position is re-drawn (up to 4 times, then fail loud + election
  released), so the winner is uniform over PAID tickets and a draw stays inside the host's 64
  kv-op budget.
- **Residuals.** (1) round keys (~1 entry key + 1 counter per purchase/player) live under kv's
  10,000-key quota, reclaimed on TTL expiry (host fix `4ad3eaae` is a prerequisite); a full quota
  fails closed and heals as keys lapse; a swarm of funded accounts is bounded only by the
  per-actor cooldown (a host per-actor kv rate limit is the follow-up). (2) Round keys live 14
  days (the claim window); an unclaimed pot after that is gone. (3) A draw cannot happen on its
  own: someone has to run `!lottery draw` (no scheduler capability exists yet).

## Permissions (V2)

| id | why |
|---|---|
| `storage.kv` | round, ticket ledger, totals, result, reservations, cooldowns (pseudonymous keys) |
| `economy.read` / `economy.wager` (`max_bet: 500`) | balance + max-spend checks; ticket debit and claim payout |
| `identity.resolve` | actor uuid required by the economy import |
| `chat.send:discord` / `chat.send:twitch` | replies |

No `economy.transfer` (never issued). Uses `economy`/`identity` (`world stage-next` imports) --
build against that world (`wit_world: stage-next` catalog field). Not yet in
`bundles/core-bundles.yaml`.

## PII and output safety

kv keys: `lottery.<kind>.<round>[.<position>|.<pseudonym>]`, `lottery.cd.<pseudonym>`,
`lottery.seen.<digest>` -- `pseudonym` = SHA-256(community|resolved-uuid)[:32], `digest` = a
truncated SHA-256 of the platform-prefixed message id (never the id); `.`-joined, never `:`. Logs
carry `op`/`outcome`/`amount_bucket`/error-case only -- no names, uuids or message text. A
buyer's host-minted `{user:<token>}` placeholder (an opaque token, never a name) is kept in their
entry so the draw can announce the winner; a raw handle is never stored (such a winner is
announced as "a ticket holder"), and the stored token is re-validated before it is rendered live.
`event.actor` in replies: the minted token stays live, anything else goes through
`waddle_sdk.safe_text.sanitize_user_text`.

## Tests

`python3 -m pytest bundles/python/lottery/tests` -- routing, purchase caps/validation, exact
position-to-buyer mapping and an exhaustive weighting proof, early/due draws, idempotent and
concurrent draws, ghost ranges (redraw / fail loud), conserved payout with randomized rounds,
chunked and zero-balance claims, redelivery of every mutating command, concurrent duplicates (with
the cooldown disabled so the event claim alone is proven), crash-order, backend-error, quota, age
fence, PII, sanitizer, tampered kv records, kv op budget (<= 64 per dispatch incl. the all-ghost
worst-case draw).
