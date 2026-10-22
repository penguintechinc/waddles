# jackpot (Python)

`!jackpot <stake>` -- an accumulating community jackpot. First-party (`provider: builtin`,
`app_id: waddles.core.example.jackpot`). **Inspired by PenguinTwitchBot by SuperPenguinTV (with
permission) -- ideas/design only, no code copied** (same convention as `fish`/`heist`).

| Command | Behavior |
|---|---|
| `!jackpot <stake>` | Stake points. `10%` chance to win the whole pot (plus your stake back) and the pot resets to `100`; otherwise your stake joins the pot. Per-actor cooldown (10s). |
| `!jackpot` | Show the current pot and the odds. |

Invalid stake / insufficient funds / over the host max-bet reply with the reason and move nothing.
Activation-gated only -- no feature flag (installed -> enabled -> activated).

## Money and the pot (read this)

- Stakes and payouts go through the economy SDK (`balance`/`max_bet`/`wager`); the actor comes from
  `identity.resolve_actor()` (host-resolved uuid), never the event's raw name.
- The pot is `community_kv` key `jackpot.pot` holding the *accrued* amount above the base (shown
  pot = `100 + accrued`). `kv.increment` is the only CAS, so every mutation is an increment:
  - loss: `wager(stake, 0)` then `increment(+stake)`.
  - win: read, claim with `increment(-observed)`; `before = after + observed` is exact for that
    caller, so concurrent winners can never double-claim and the over-subtracted part is
    restored exactly (contributions racing a claim are conserved -- see the interleaving tests).
  - refused/over-cap win: claim restored / excess returned. An ambiguous `BackendError` retries the
    identical call once, then fails loud *without* restoring (never risks a double payout).

## Replay safety (at-most-once)

The pipeline is at-least-once. The host dedupes the `wager` per event, but the pot credit is ours:
a redelivered loss used to credit the pot a second time, and a pot credited twice pays out money
that was debited once. `transform` claims the platform `message_id`
(`waddle_sdk.event_dedupe`) after the atomic cooldown and **before any wager or pot mutation**
with ONE atomic `kv.increment` on a per-event, TTL'd marker (`1` = first delivery, `> 1` =
redelivery or a concurrent duplicate), and skips a redelivery silently (no wager, no credit, no
reply). The increment is race-free and has no 16-event horizon. A crash after the claim loses one
stake into the void (never a double credit).

The marker's TTL is the horizon, closed by an age fence: an event whose ingest stamp
(`occurred_at`) is older than `event_dedupe.MAX_EVENT_AGE_SECONDS` (10 min) is dropped silently,
never processed -- so a redelivery is deduped or dropped, never replayed. A payload with no
`message_id`, or no valid `occurred_at`, fails loud and moves nothing. The claim comes after the
cooldown, so an actor on cooldown never creates a marker.

Quota (residual): markers live 15 min under kv's 10,000-key quota, whose units are reclaimed on
TTL expiry (host fix `4ad3eaae`, **a prerequisite** -- without it every TTL'd key leaks a unit). A
full quota fails closed (the play is refused, no money moves) and heals as markers lapse; a swarm
of linked, funded accounts can still hold it full (each is bounded by the 10 s cooldown). The
proper bound is a host-level per-actor kv new-key / write-rate limit (follow-up).

## Permissions (V2)

| id | why |
|---|---|
| `storage.kv` | pot + cooldown counters |
| `economy.read` / `economy.wager` (`max_bet: 500`) | balance check; atomic stake debit + payout |
| `identity.resolve` | actor uuid required by the economy import |
| `chat.send:discord` / `chat.send:twitch` | replies |

Uses `economy`/`identity` (`world stage-next` imports) -- build against that world.

## PII

kv keys: `jackpot.pot`, `jackpot.cd.<sha256(community|uuid)>`, `jackpot.seen.<d>` where `d` is a
truncated SHA-256 of the platform-prefixed message id (never the id) only (`.`-joined, never `:`).
Logs carry `op`/`outcome`/`amount_bucket`/error-case only -- no names, uuids or message text.

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

## Tests

`python3 -m pytest bundles/python/jackpot/tests` -- accumulation, win-resets-pot, concurrent
increments/claims (kv facade yields between read and write), cooldown, over-cap, backend-error paths,
redelivery (loss / win / flipped re-roll / inside the cooldown / concurrent copies / missing id).
