# superpenguin-defuse (C#)

`!defuse <wire>` bomb-defusal game, ported from [PenguinTwitchBot](https://github.com/Psychoboy/PenguinTwitchBot)'s
`PastyGames/defuse.cs` by superpenguintv (Psychoboy), MIT-licensed, used with permission -- see `bundle.yaml`'s `notice`. A C# app
bundle on `sdk/waddle-sdk-cs` (world `stage-next-tools`), same pattern as `superpenguin-roll`; listed as a marketplace
**alternative** (`category: alternatives`). Design, economy rules and the shared helper contract: `docs/sdk-cs.md`.

## Behavior

The player pays **500** and cuts one of `red`, `blue`, `yellow`; one wire chosen uniformly at random defuses the bomb and pays a reward
drawn uniformly from **cost x 3 -/+ cost / 3** (1334..1666 at the default cost; integer division, bounds inclusive), any other wire
explodes and the cost is lost (expected return = exactly the cost). 10 s per-user cooldown (silently dropped inside the window). A
missing/unknown wire (matched exactly, as the original) lists the wires without charging or cooling down; a user who cannot afford the
cost gets the original "costs {Cost}" message. Message templates keep the original tokens (`{Wires}`, `{Wire}`, `{Cost}`, `{PointType}`,
`{Name}`, `{Points}`) and defaults.

| Deviation (forced by the economy / host) | Why |
|---|---|
| Cost and reward settle in ONE atomic `economy.wager(cost, reward)` (reward 0 on a loss) | only the actor is debited; replaces the original's separate remove/add. The cost must fit `economy.max-bet` |
| Multiplier bounded to 2..50 and cost to <= 10,000,000 | keeps the reward within the host's 100x payout cap and the 32-bit draw |
| Reply appends the balance; name = host `{user:uuid}` placeholder (no "name with title" lookup); currency = economy `points_view` alias | no name lookup / per-game point type exists |
| `defuse.gif` / `detonated.gif` overlay alerts and default-command-trigger events are not ported | no overlay/trigger surface for this bundle |

## Config (all optional, original defaults) -- `stages.process.config`

`wires` (2..20 distinct, may contain spaces), `cost` 500, `win_multiplier` 3, `cooldown_seconds` 10, and the templates `no_args`,
`not_enough`, `starting`, `success`, `failure`. An invalid config fails loud.

## Idempotency and failure policy

The cut wire, winning wire and reward are recorded in `kv` BEFORE the single `wager`, keyed by a hash of **message id + platform +
actor uuid + command**, with actor, command and raw text stored in the record; a redelivery reprints and re-pays it only on an exact
match (anything else is validated and cooled down like a new command). An empty/blank message id is refused loudly. User-actionable
refusals reply in chat and release the cooldown; `denied`/`unavailable`/`backend`/`kv` failures propagate.

## Permissions

| Permission | Why |
|---|---|
| `storage.kv` | cooldown + per-event outcome record |
| `economy.wager` (`max_bet: 500`) | `wager` and `max-bet` |
| `identity.resolve` | actor -> community user uuid |
| `tool.call:waddles.core.service.economy:points_view` | currency alias |
| `chat.send:twitch` / `chat.send:discord` | relay the reply |

Gated behind the PostHog flag `waddles.command-superpenguin-defuse` (default OFF), checked after the command match and before the cooldown.
Logs carry ids/op/outcome/counts only; kv keys are `.`-separated hashes.

## Build and test

```bash
make test-superpenguin-defuse           # xUnit + 90% line-coverage floor (scripts/test-superpenguin-bundle.sh defuse)
make build-superpenguin-defuse-bundle   # pinned Dockerfile -> /tmp/superpenguin-defuse-out/superpenguin_defuse.wasm
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
