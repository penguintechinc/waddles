# prediction (Python)

`!prediction` / `!predict` -- community predictions on a pari-mutuel point pool. First-party
(`provider: builtin`, `app_id: waddles.core.example.prediction`), net-new Waddles content (the
open / bet / resolve shape is a generic stream-community mechanic; no attribution required).
Points only; no real-money ties. Activation-gated only -- no feature flag.

| Command | Who | Behavior |
|---|---|---|
| `!prediction open "Will X?" yes no [...]` (verb form `!prediction add ...`) | mod | Open a prediction: 2-5 distinct outcomes (names, not bare numbers). One at a time. |
| `!predict <outcome> <points>` | anyone | Stake on ONE outcome (name or 1-based number). Top-ups allowed, hedging is not. |
| `!prediction lock` | mod | Close betting now (it also auto-closes 1 h after opening). |
| `!prediction resolve <outcome>` (verb form `!prediction set ...`) | mod | Declare the winner; winners split the whole pool pro rata. |
| `!prediction cancel` (verb form `!prediction remove`) | mod | Cancel: every stake becomes refundable. |
| `!predict claim [round]` / `!prediction claim [round]` | anyone | Collect winnings (or a refund). |
| `!prediction` / `!prediction list` | anyone | Show the question, live pools and shares. |

Handled with a reply and no state change: no open prediction, betting closed / already finished,
unknown outcome (lists the choices), bad amount, insufficient funds, over the host `max_bet`,
second outcome, entrant/stake caps, cooldown, double resolve, resolving the wrong phase. Mod
commands read the strict boolean `is_mod` / `is_broadcaster` badge (`is True`, fail closed -- a
platform that emits no badge, e.g. Discord today, cannot run them).

## Money and integrity (read this)

**Payouts are claimed, not pushed.** The host only moves the invoking actor's own account
(`economy.wager`'s `user` must be the actor), so a moderator's `resolve` cannot credit winners and
the bundle cannot debit anyone but the person typing. A stake is debited when placed
(`wager(stake, 0)`; the points live on only as `community_kv` pool counters) and each winner
collects with their own `claim`: one `wager(f, share + f)` on their account, net `+share`, where
`f = ceil(share / 99)` is the smallest stake the host's `payout <= 100 x stake` rule lets carry the
payout (it comes straight back in the same call -- a winner needs 1 point in hand). A share over
`99 x max_bet` is collected over several claims; a per-round `paid` counter tracks it.

- **Conserved, bounded.** `share = floor(stake x pool / winning_pool)` against totals frozen at
  resolve time, so `sum(shares) <= pool <= sum(stakes burned)` -- the bundle never mints more than
  was staked (floor division leaves dust, never extra). Each call mints at most `99 x max_bet`
  (declared `max_bet: 500`), and the host's daily payout (mint) caps still apply (`quota_exceeded` ->
  "try again later", reservation released). Caps: `MAX_STAKE_PER_ROUND = 1000` per bettor,
  `MAX_ENTRANTS = 200` per round (bounds the kv footprint to ~2 keys per bettor).
- **Pool-first order.** Bet = debit, then pool, then the bettor's stake. A crash in between
  strands stake (under-pays); it can never inflate a share.
- **Time fence, not a lock.** kv has no transaction, so resolve is refused until betting has been
  closed for `SETTLE_SECONDS = 10` (longer than any in-flight 2 s bet). Resolving an open round just
  closes betting and asks the moderator to repeat. A stake that races the lock is stopped by a
  re-read after the cooldown claim.
- **Idempotent on redelivery.** `transform` reads the platform `message_id`; every mutating
  command (bet, claim and the moderator ones) claims it via `waddle_sdk.event_dedupe` -- ONE atomic
  `kv.increment` on a per-event TTL'd marker (`1` = first delivery) -- AFTER the cheap prechecks and
  the atomic per-actor cooldown and BEFORE any stake, payout or state change. A redelivery is
  skipped silently. An event older than `event_dedupe.MAX_EVENT_AGE_SECONDS` is dropped, and a
  payload with no `message_id` / valid `occurred_at` fails loud and moves nothing. At-most-once: a
  crash after the claim loses that one operation (never repeats it).
- **Atomic CAS everywhere.** Opening (`opening.<n>`), finalizing (`finalizing.<n>`, shared by
  resolve and cancel) and the claim reservation (`paid.<n>.<who>`) are `kv.increment` elections --
  never read-then-set. Two moderators (or two events) resolving at once write exactly one result;
  the later `resolve` just reports the stored one. Election markers carry a 60 s TTL so a crash
  mid-way heals itself.
- **Ambiguous host failure.** A `BackendError` from `wager` retries the identical call once (the
  host dedupes by event), then fails loud WITHOUT releasing the claim reservation (never risk a
  double payout). Any other unexpected refusal (actor mismatch, idempotency conflict, unprovisioned
  economy) is logged at ERROR, answered with a generic error and raised.
- **Residuals.** (1) markers/round keys live under kv's 10,000-key quota (reclaimed on TTL expiry --
  host fix `4ad3eaae` is a prerequisite); a full quota fails closed and heals as keys lapse; a swarm
  of funded accounts is bounded only by the per-actor cooldown (a host per-actor kv rate limit is
  the follow-up). (2) All round keys live 14 days (the claim window); unclaimed winnings after that
  are gone.

## Permissions (V2)

| id | why |
|---|---|
| `storage.kv` | round, pools, stakes, reservations, cooldowns (pseudonymous keys) |
| `economy.read` / `economy.wager` (`max_bet: 500`) | balance + max stake checks; stake debit and claim payout |
| `identity.resolve` | actor uuid required by the economy import |
| `chat.send:discord` / `chat.send:twitch` | replies |

No `economy.transfer` (never issued). Uses `economy`/`identity` (`world stage-next` imports) --
build against that world (`wit_world: stage-next` catalog field). Not yet in
`bundles/core-bundles.yaml`.

## PII and output safety

kv keys: `prediction.<kind>.<round>[.<outcome>][.<pseudonym>]`, `prediction.cd.<pseudonym>`,
`prediction.seen.<digest>` -- `pseudonym` = SHA-256(community|resolved-uuid)[:32], `digest` = a
truncated SHA-256 of the platform-prefixed message id (never the id); `.`-joined, never `:`. Logs
carry `op`/`outcome`/`amount_bucket`/error-case only -- no names, uuids, titles, outcomes or text.
`event.actor` is interpolated into replies: the host-minted `{user:<token>}` stays live, anything
else and every echoed title/outcome goes through `waddle_sdk.safe_text.sanitize_user_text`.
Round definitions are stored as typed JSON (title/outcomes are stored raw, validated, and
sanitized at every echo).

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

## Tests

`python3 -m pytest bundles/python/prediction/tests` -- routing, open/lock/resolve/cancel, bet
validation, the full proportional-payout scenario with conservation asserted, randomized rounds
(sum of payouts <= pool), chunked and zero-balance claims, double-resolve / racing finalizers,
redelivery of every mutating command, concurrent duplicates (with the cooldown disabled so the
event claim alone is proven), crash-order, backend-error, quota, age fence, PII, sanitizer, kv op
budget (<= 64 per invocation).
