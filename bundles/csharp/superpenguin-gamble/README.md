# superpenguin-gamble (C#)

`!gamble` and `!jackpot`, ported from [PenguinTwitchBot](https://github.com/Psychoboy/PenguinTwitchBot)'s `PastyGames/Gamble.cs` by
superpenguintv (Psychoboy), MIT-licensed, used with permission -- see `bundle.yaml`'s `notice`. A C# app bundle on
`sdk/waddle-sdk-cs` (world `stage-next-tools`), same pattern as `superpenguin-roll`; listed as a marketplace **alternative**
(`category: alternatives`). Design, economy rules and the shared helper contract: `docs/sdk-cs.md`.

## Behavior

`!gamble amount | max | all | N%` rolls a d100: **49..100 wins** (stake x **2**, gross), 69 is the **jackpot number**, a losing bet adds
**10%** of the stake to the jackpot pot (starts at **1000**, never below it), a jackpot win pays the win plus a share of the pot
proportional to the bet (a bet of `max_bet` takes it all). 180 s per-user cooldown on `!gamble` (silently dropped inside the window);
minimum bet 5; `!jackpot` shows the pot (no cooldown). Message templates keep the original tokens (`{Name}`, `{Rolled}`, `{Points}`,
`{PointType}`, `{jackpot}`, `{MaxBet}`, `{MinBet}`, `{Command}`) and defaults.

| Deviation (forced by the economy / host) | Why |
|---|---|
| Win + jackpot share settle in ONE atomic `economy.wager`; the share is truncated so the payout stays <= 100x the stake | only the actor is debited; host payout cap (never reached with defaults) |
| Share scaled by bet / `max_bet` (config, default 500) instead of bet / the user's max bet | `economy.max-bet` is bounded by holdings: a 5-point bet would otherwise claim the whole pot |
| The jackpot number pays even while the stream is offline | no stream-state capability in this world (original: offline = plain win) |
| Reply appends the balance; name = host `{user:uuid}` placeholder; currency = economy `points_view` alias | no name lookup / per-game point type exists |
| Default-command-trigger event hooks | no equivalent surface |

## Jackpot pool (kv)

The pot is an atomic `kv.increment` counter of what has accrued ABOVE the starting jackpot (kv has no compare-and-swap, so a
read-modify-write would lose updates). A loss adds its contribution; a jackpot win subtracts what it paid, clamped so the pot never
falls below the start (a lost race that overdraws is put back). Each pot change is applied at most once per event (`TryClaim`
marker) and only AFTER the wager succeeds. The counter's TTL is refreshed on every increment: after the host's maximum kv TTL with
no gambling, the pot falls back to the starting jackpot.

## Config (all optional, original defaults) -- `stages.process.config`

`min_bet` 5, `max_bet` 500, `jackpot_number` 69, `minimum_for_win` 48, `winning_multiplier` 2 (1..100), `jackpot_contribution` 0.10 (0..1),
`starting_jackpot` 1000, `cooldown_seconds` 180, and the message templates `win_message`, `lose_message`, `jackpot_message`,
`current_jackpot_message`, `incorrect_args`, `incorrect_bet`, `not_enough`. An invalid config fails loud.

## Idempotency and failure policy

The roll and jackpot share are recorded in `kv` BEFORE the single `wager`, keyed by a hash of **message id + platform + actor uuid +
command**, with actor, command and raw text stored in the record; a redelivery reprints and re-pays it only on an exact match
(anything else is validated and cooled down like a new command). An empty/blank message id is refused loudly. User-actionable refusals
reply in chat and release the cooldown; `denied`/`unavailable`/`backend`/`kv` failures propagate.

## Permissions

| Permission | Why |
|---|---|
| `storage.kv` | cooldown, per-event outcome record, jackpot counter |
| `economy.wager` (`max_bet: 500`) | `wager` and `max-bet` |
| `identity.resolve` | actor -> community user uuid |
| `tool.call:waddles.core.service.economy:points_view` | currency alias |
| `chat.send:twitch` / `chat.send:discord` | relay the reply |

Gated behind the PostHog flag `waddles.command-superpenguin-gamble` (default OFF), checked after the command match and before the cooldown.
Logs carry ids/op/outcome/counts only; kv keys are `.`-separated hashes.

## Build and test

```bash
make test-superpenguin-gamble           # xUnit + 90% line-coverage floor (scripts/test-superpenguin-bundle.sh gamble)
make build-superpenguin-gamble-bundle   # pinned Dockerfile -> /tmp/superpenguin-gamble-out/superpenguin_gamble.wasm
```

## Activation

Not in `bundles/core-bundles.yaml` (third-party `waddles.integrations.*` namespace; seeder placement and the vendor-validator
`streaming-lifecycle` export rule are pending S0 decisions -- see `docs/sdk-cs.md` section 6 and `superpenguin-roll`'s README).

## App/service boundary (2026-11-07)

This port remains C#. Game rules, wagering, and maximum-bet handling belong
to the app; atomic ledger settlement uses the app's granted host primitive.
The shared economy service is unchanged. Shared balance/credit/debit operations,
where used, invoke its existing tools via the C# SDK; no private ledger is created.
See `docs/superpenguin-port-inventory.md` for the port decisions and remaining work.
