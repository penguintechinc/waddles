# jackpot (Python)

`!jackpot <stake>` -- an accumulating community jackpot. First-party (`provider: builtin`,
`app_id: waddles.core.example.jackpot`). **Inspired by PenguinTwitchBot by SuperPenguinTV (with
permission) -- ideas/design only, no code copied** (same convention as `fish`/`heist`).

| Command | Behavior |
|---|---|
| `!jackpot <stake>` | Stake points. `10%` chance to win the whole pot (plus your stake back) and the pot resets to `100`; otherwise your stake joins the pot. Per-actor cooldown (10s). |
| `!jackpot` | Show the current pot and the odds. |

Invalid stake / insufficient funds / over the host max-bet reply with the reason and move nothing.
Gated by feature flag `waddles.command-jackpot` (default OFF, checked after the command matches).

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

## Permissions (V2)

| id | why |
|---|---|
| `flags.read` | feature flag gate |
| `storage.kv` | pot + cooldown counters |
| `economy.read` / `economy.wager` (`max_bet: 500`) | balance check; atomic stake debit + payout |
| `identity.resolve` | actor uuid required by the economy import |
| `chat.send:discord` / `chat.send:twitch` | replies |

Uses `economy`/`identity` (`world stage-next` imports) -- build against that world.

## PII

kv keys: `jackpot.pot`, `jackpot.cd.<sha256(community|uuid)>` only (`.`-joined, never `:`). Logs carry
`op`/`outcome`/`amount_bucket`/error-case only -- no names, uuids or message text.

## Tests

`python3 -m pytest bundles/python/jackpot/tests` -- accumulation, win-resets-pot, concurrent
increments/claims (kv facade yields between read and write), cooldown, over-cap, backend-error paths.
