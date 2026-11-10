# superpenguin-slots (C#)

`!slot` / `!slots` three-reel slot machine, ported from
[PenguinTwitchBot](https://github.com/Psychoboy/PenguinTwitchBot)'s `PastyGames/Slots.cs` by superpenguintv (Psychoboy),
MIT-licensed, used with permission -- see `bundle.yaml`'s `notice`. A C# app bundle on `sdk/waddle-sdk-cs` (world
`stage-next-tools`), same pattern as `superpenguin-roll`; listed as a marketplace **alternative** (`category: alternatives`).
Design, economy rules and the shared helper contract: `docs/sdk-cs.md`.

## Behavior

`!slots [amount | max | all | N%]` spins three reels of five emotes (Kappa, PogChamp, LUL, 4Head, BloodTrail), each uniformly random.
Faithful to the original: 3 of a kind pays **3.5x** the stake, the first two equal **2.5x**, any other pair **1.5x**, no match loses;
minimum bet **25**; `!slot` and `!slots` each have their own **600 s** per-user cooldown (a command inside the window is silently
dropped); the 20 win / 62 lose messages are verbatim (`SlotsMessages.cs`, `{NAME_HERE}` -> the player). Max bet = the host's
`economy.max-bet`. A bare `!slots` stakes the minimum bet.

| Deviation (forced by the economy) | Why |
|---|---|
| A bare `!slots` stakes the minimum bet instead of paying a free fixed prize | a free prize needs a MINT (`economy.credit`), not requested |
| Payout = stake x multiple through ONE atomic `economy.wager`; multipliers capped at 100 | only the actor is debited; host payout cap is 100x the stake |
| Default odds return 102% of the stake | the original's own table is player-favourable; tune in config |
| Lose line index drawn over the whole 62-line pool | the original drew over the WIN pool's length (only the first 20 ever showed) |
| Loss line states the stake lost; reply appends the balance | clarity; name = host `{user:uuid}` placeholder, currency = economy `points_view` alias |
| Default-command-trigger event hooks, `Tax` timer | no equivalent surface; `Tax` is an offline inactivity timer, not a slots feature |

## Config (all optional, original defaults) -- `stages.process.config`

```json
{"min_bet": 25, "three_of_a_kind": 3.5, "first_two": 2.5, "last_two": 1.5, "cooldown_seconds": 600,
 "emotes": ["Kappa", "PogChamp", "LUL", "4Head", "BloodTrail"]}
```

Multipliers must be in (0, 100], emotes 2..20 non-blank, `min_bet` >= 1. An invalid config fails loud (never "surprising odds").

## Idempotency and failure policy

The reels and message picks are recorded in `kv` BEFORE the single `wager`, keyed by a hash of **message id + platform + actor uuid +
command**, with actor, command and raw text stored in the record; a redelivery reprints it only on an exact match (anything else is
validated and cooled down like a new command). An empty/blank message id is refused loudly. User-actionable refusals (not linked, bad
bet, insufficient funds) reply in chat and release the cooldown; `denied`/`unavailable`/`backend`/`kv` failures propagate.

## Permissions

| Permission | Why |
|---|---|
| `storage.kv` | cooldown + per-event outcome record |
| `economy.wager` (`max_bet: 500`) | `wager` and `max-bet` |
| `identity.resolve` | actor -> community user uuid |
| `tool.call:waddles.core.service.economy:points_view` | currency alias |
| `chat.send:twitch` / `chat.send:discord` | relay the reply |

Gated behind the PostHog flag `waddles.command-superpenguin-slots` (default OFF), checked after the command match and before the cooldown.
Logs carry ids/op/outcome/counts only (no actor, uuid or event id); kv keys are `.`-separated hashes.

## Build and test

```bash
make test-superpenguin-slots           # xUnit + 90% line-coverage floor (scripts/test-superpenguin-bundle.sh slots)
make build-superpenguin-slots-bundle   # pinned Dockerfile -> /tmp/superpenguin-slots-out/superpenguin_slots.wasm
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
