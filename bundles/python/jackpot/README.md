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
that was debited once. `transform` forwards the platform `message_id`; `dispatch` records it
(`waddle_sdk.event_dedupe`, one ring key per actor) after the atomic cooldown and **before any
wager or pot mutation**, and skips a redelivery silently (no wager, no credit, no reply). A crash
after the claim loses one stake into the void (never a double credit); a payload with no
`message_id` fails loud and moves nothing. The ring remembers the last 16 events per actor.

## Permissions (V2)

| id | why |
|---|---|
| `storage.kv` | pot + cooldown counters |
| `economy.read` / `economy.wager` (`max_bet: 500`) | balance check; atomic stake debit + payout |
| `identity.resolve` | actor uuid required by the economy import |
| `chat.send:discord` / `chat.send:twitch` | replies |

Uses `economy`/`identity` (`world stage-next` imports) -- build against that world.

## PII

kv keys: `jackpot.pot`, `jackpot.cd.<sha256(community|uuid)>`, `dedupe.jackpot.<sha256(community|uuid)>`
(truncated hashes of the platform message id, never the id) only (`.`-joined, never `:`). Logs carry
`op`/`outcome`/`amount_bucket`/error-case only -- no names, uuids or message text.

## Output safety

`event.actor` is interpolated into replies. The host-minted `{user:<token>}` actor stays live (egress renders the display name); any other value, and every typed echo, goes through `waddle_sdk.safe_text.sanitize_user_text`.

## Tests

`python3 -m pytest bundles/python/jackpot/tests` -- accumulation, win-resets-pot, concurrent
increments/claims (kv facade yields between read and write), cooldown, over-cap, backend-error paths,
redelivery (loss / win / flipped re-roll / inside the cooldown / concurrent copies / missing id).
