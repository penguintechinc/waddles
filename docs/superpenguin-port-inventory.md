# SuperPenguinTV C# port inventory

Source: [Psychoboy/PenguinTwitchBot](https://github.com/Psychoboy/PenguinTwitchBot),
revision `df27ab1aaeaff0c6297f0e1fb8c3188b3be8328f` (inspected 2026-11-07).
Ports retain C#, the upstream MIT notice and `github/psychoboy` attribution,
and belong to collection `superpenguintv`. They use the third-party approval path;
they must not be added to the first-party core seeder.

## Selection rule

Skip a module only when its complete behavior is already supplied by a **service**
bundle. An existing **app** bundle is not a reason to skip its C# counterpart.
The app retains game/command/workflow policy and calls shared service tools.
No private points system, platform token handling, transport or AI client is ported.
A helper class is folded into its owning app, not published as an extra bundle.

## Current wave

| Module | C# app | Service calls | Status |
|---|---|---|---|
| PastyGames/Roll | superpenguin-roll | economy points_view; app owns wager/max-bet | Updated; native and WASM validation passed |
| PastyGames/Slots | superpenguin-slots | economy points_view; app owns wager/max-bet | Native and WASM validation passed |
| PastyGames/Gamble | superpenguin-gamble | economy points_view; app owns wager/max-bet | Native and WASM validation passed |
| PastyGames/defuse | superpenguin-defuse | economy points_view; app owns wager/max-bet | Native and WASM validation passed |
| TicketGames/Roulette | superpenguin-roulette | economy get_balance, points_view; app owns wagering | Native and WASM validation passed |
| TicketGames/First | superpenguin-first | economy adjust_balance, points_view | Resumed; UTC-day variant, reset refused |
| TicketGames/DuelGame | superpenguin-duel | economy get_balance, adjust_balance, points_view | Resumed; refunds/sweeps need per-operation retry keys |
| PastyGames/FFA | superpenguin-ffa | economy get_balance, adjust_balance, points_view; TickScheduler join window | Native + WASM validated; settles on the next command (no timer) |
| PastyGames/Heist | superpenguin-heist | economy get_balance, adjust_balance, points_view; TickScheduler join window | Native + WASM validated; story condensed to one reply |
| PastyGames/Steal | superpenguin-steal | economy get_balance, points_view, **transfer_from** (dangerous grant, contract fixture) | Native + WASM validated; fails loud ("steal not enabled") until the economy tool ships |

The economy service exports remain unchanged by these apps (Steal depends on the separate `transfer_from` work). Each C# app owns wagering and
maximum-bet policy and uses the atomic host ledger primitive for settlement.
Balance and credit/debit operations reuse existing economy service tools.
All write tools carry stable retry keys, and missing grants fail loudly.

## Remaining app ports (not complete)

| Upstream modules | Decision | Service dependencies / remaining work |
|---|---|---|
| Fishing/* | Port C# game, shop and tournament policy | Shared economy/inventory tools where available; durable state and scheduling |
| Games/Starship | Port C# story/voting app | Persisted state, scheduling, economy, overlay |
| Games/Balloon, Parachute, Plinko, Curling, Golf, Racing, UfoClaw | Port C# game logic and matching visual assets | Shared overlay and economy tools; scheduling for join windows |
| WheelSpin/* | Port C# picker/visual app | Shared overlay tools; Python wheel overlap is allowed |
| TicketGames/BonusTickets, AddActive | Port C# event-award policy | economy tools; stream/engagement event contracts |
| Features/GiveawayFeature, TicketGames/RaffleRuntimeService, GiveawayFairnessReport | Port C# giveaway policy, fairness and exclusions | Durable state; existing Python giveaway/raffle do not justify skipping |
| Alias/* | Port C# command alias policy | Shared command registry where available; Python alias overlap allowed |
| AudioCommand/* | Port C# command/sound policy | Shared media/overlay tools; audio files remain in shared storage |
| TTS/* | Port C# command policy only | Reuse media TTS tools; skip duplicate synthesis/player infrastructure |
| Misc/AutoTimers, timer groups | Port C# announcement policy | Shared scheduler tools; activity thresholds remain app policy |
| Misc/Weather | Port C# weather command | Reuse a weather service if available; Python weather is an app |
| Features/ViewerFeature, Misc/LastSeen, DeathCounters, DailyCounter, QuoteSystem, RaidTracker, Top | Port C# engagement/command policy | Service reads for economy/reputation where applicable |
| Shoutout/ClipService, Misc/ShoutoutSystem | Port C# shoutout/clip workflow | Twitch clip tools, shared AI and overlay; no tokens in app |
| Ai/ScAi | Port C# domain prompts and command policy | Shared AI service; no separate model client |
| Music/YtPlayer, banned songs/cooldowns, Metrics/SongRequests | Port C# request/queue policy | Shared music/media tools; no independent player transport |
| Actions recipes/triggers/variables | Port reusable C# app policy after mapping automation contracts | Do not copy host queue, process lifecycle or unrestricted script engine |
| ChannelPoints subactions | Port distinct redemption-to-action policy | Shared Twitch operations; skip duplicate redemption transport |
| Points/PointsSystem, PointType, UserPoints ledger | Skip duplicate ledger | Economy service owns balances, mint/debit, transfers and caps; distinct earn policy belongs to C# apps |
| Platform API/EventSub/OAuth clients | Skip duplicate platform transport | Platform service bundles/host own credentials and networking |
| Moderation command policy / ModSpam | Port only distinct policy | Reuse platform moderation tools; skip duplicate ban/filter authority |
| Queues/ActionQueue engine, setup/auth/DB/updater/diagnostics/OBS local connector | Do not port host infrastructure | Waddles runtime owns these responsibilities |

The historical scratchpad classified several apps as covered merely because a
Python app exists. That classification is superseded by the selection rule above.
No pending item in this table is represented by an empty implementation.

## Validation scope and remaining limits

The native game and SDK suites test game rules and service contracts. The real
executor tests use the production WASM loader, linker and wire codec with a
simulated stage host; they do not claim a live database or cluster rollout.
The host-context fix exposes only the signed envelope retry ID, not a
platform message ID supplied by an app.

Duel uses delayed expiry sweeps rather than a live timer. Its existing
nontransactional KV/ledger crash windows can leave claims or stakes requiring
operator recovery (see its README). Abort decisions are persisted before
releasing ownership, so failed cleanup cannot revive an aborted money plan.
First remains the documented UTC-day variant and refuses unverified manual reset.
Host context still does not forward app config JSON; these ports use their
default settings until the shared context configuration seam is wired.
These limitations and remaining port families are not marked complete.

## Checked first batch

- C# game tests: Roll59, Slots91, Gamble81, Defuse60, Duel48, First34, Roulette39.
- C# SDK:155 tests. All measured bundle/SDK line coverage is at least98%.
- All7 real WASI components build and expose process/action/lifecycle exports.
- Production executor:2 tests passed, covering Roll's refusal/replay scenarios plus
  all6 other apps with flag-off, flag-on and replay commands. The simulated stage
  refuses unmodeled operations and the harness fails on any such call.
- Host context:6 focused tests pass; all-target svc-process clippy is clean.
- Vendor validator + metadata/service-boundary contracts:28 tests pass.

Run `make test-superpenguin-ports-e2e` to rebuild every artifact and repeat the
real executor matrix. It uses the pinned Rust Docker image and read-only local
Rust caches; missing artifacts or partial/zero port coverage fail the gate.

Release validation remains blocked by pre-existing lint failures reproduced on
clean release10286de0e:3417 flake8 findings vs3230 allowed, and23 dead documentation
references vs21 allowed. No baseline was raised. These are not green merge gates.
