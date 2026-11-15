# superpenguin-dropgame-core

Shared library for the SuperPenguinTV drop-game ports (Plinko, Balloon, Parachute, Curling, Golf; Racing and UfoClaw reuse its pieces).
A plain `net10.0` library over `waddle-sdk-cs`: no WIT types, no `System.Security.Cryptography` (throws on wasi-wasm), no reflection JSON.
Each game bundle references it with a `ProjectReference` (see `../superpenguin-plinko`).

| File | What |
|---|---|
| `Prng.cs` | `SplitMix64`, `Xoshiro256` (xoshiro256**, seeded via splitmix64): `NextULong/NextDouble/NextInt(min,max)/NextBool` |
| `SeedDerivation.cs` | `Derive(messageId, platform, actor, command, entropy)`, `NewEntropy()`, `ToHex/TryParseHex` |
| `StakeParser.cs` | `StakeParser.Parse(arg, held, maxBet)` -> `StakeResult(Stake, Refusal)` (`amount`/`all`/`max`, clamp to max bet) |
| `DropReplay.cs` | `DropReplayRecord`, `DropReplayGuard.KeyFor/TryReplay`, `DropCoreJsonContext` |
| `OverlayEnvelope.cs` | `OverlayEnvelope.Build/Descriptor`, `OverlayPublisher.Publish/EnsureWidget` |
| `DropSupport.cs` | `Reply(event, command, text)`, `Alias(host, actor, game)` |
| `WageredGameBase.cs` | `GameSpec`, `GameRound`, `WageredGameBase` (the single-shot wagered-game host) |
| `overlay/SCHEMA.md` | the envelope doc; `overlay/common/` the vendored shared overlay JS + sounds + `ASSETS.json` |
| `tests/` | xUnit (79 tests, 100% line coverage); `tests/support/DropTestWorld.cs` is the in-memory host the game test projects link |
| `tools/gen_assets.py` | writes a bundle's `overlay/ASSETS.json` (sha256/size/upstream path/commit, unclear list) |

## Building a single-shot game on it

Subclass `WageredGameBase` with a `GameSpec` and one pure function `Play(seed, stake, extraArgs) -> GameRound(payout, outcomeText, dataJson)`.
The base does flag (default OFF, after command match), community, identity, stake parse/clamp, cooldown, seed derivation from the invocation
plus one recorded host-entropy value, the replay record written BEFORE the single `Economy.Wager`, InsufficientFunds/OverCap refusals (record
discarded, cooldown released), the chat reply, the overlay envelope push + one-time `RegisterWidget`, and structured logs (ids/op/outcome only).
`Play` must be pure: same seed + stake + config -> same result. Payout above 100x stake is a loud bug (the host ledger refuses it).

## Games that do not fit the single-shot shape

Use the pieces directly: `Xoshiro256`/`SeedDerivation` for deterministic randomness, `DropReplayGuard` + `DropReplayRecord.Extra` for the
idempotent record, `StakeParser`, `OverlayEnvelope.Build(game, kind, ...)` with your own `kind` (`join`, `race`, `claw`, ...) and
`OverlayPublisher.Publish`, `DropSupport.Reply/Alias`. Racing's join window and deadline sit on `WaddleSdk.Scheduling.TickScheduler` (see
`docs/sdk-cs.md` section 4 for its late-settlement limits).

## Tests

`bash scripts/test-superpenguin-bundle.sh dropgame-core` (the project is named `SuperpenguinDropgame-core.Tests.csproj` so the existing script
derives the right path; no script change needed). A game's test project links `tests/support/DropTestWorld.cs`, references the core project,
and excludes `[DropGameCore]*` from its own coverage (the core has its own 90% gate): copy `../superpenguin-plinko/tests/*.csproj` and
`coverlet.runsettings`.

## Determinism rules for game simulations

Only `+ - * /` and `Math.Sqrt` are IEEE-exact on every platform; avoid `Math.Pow/Sin/Cos/Exp/Log` in anything that decides a result or a
keyframe (use `Math.Sqrt(Math.Sqrt(x))` for `x^0.25`). Draw randomness only from the one `Xoshiro256`, in a fixed order.
Source: MIT, ported/adapted from Psychoboy/PenguinTwitchBot (see each game's `bundle.yaml` notice).
