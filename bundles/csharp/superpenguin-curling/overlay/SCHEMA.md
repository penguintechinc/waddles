# Curling envelope data (`extra.data`, envelope v1)

The fixed envelope fields (`v, game, kind, seed, actor, stake, payout, data`) are documented in
`../../superpenguin-dropgame-core/overlay/SCHEMA.md`. For this bundle `game` is `"curling"` (also the widget id and the asset prefix
`drop-games/curling/`), `kind` is `"settled"` and `data` is the object below. One envelope per settled `!curl` (aliases `!curling`,
`!slide`, `!shuffle`), pushed after the payout is settled; a redelivery re-pushes the identical envelope (same `seed`).

Positions are integers in 1/10000 of the strip (`scale` below); ticks are 1/60 s. `x`/`y` are screen axes: on a horizontal strip
(`Bottom`/`Top`) the slide axis is `x` and the lane is `y`; on a vertical strip (`Left`/`Right`) it is the other way round.

| Field | Type | Meaning |
|---|---|---|
| `sheet` | object | The sheet the shot was played on (below). |
| `condition` | string | `Normal` \| `Slick` \| `HeavyFrost` \| `CrossDrift` (rolled per shot from the seed). |
| `conditionLabel` | string | `Normal Ice` \| `Slick Ice` \| `Heavy Frost` \| `Cross-Drift`. |
| `power` | number | The viewer's raw power 1-100 (or the seed-drawn default, uniform 65..85). |
| `effectivePower` | number | Power after variance (+/- 7 points), 4 decimals. The launch speed derives from it (`speed = round(eff/100 * 0.76, 4)`). |
| `start` | int | Launch position along the slide axis (500 forward, 9500 reversed). |
| `restX`, `restY` | int | Where the stone stopped (screen axes). |
| `totalTicks` | int | Tick of the final keyframe (upstream's `max(30, round(v0 / decel * 60))`). |
| `wallHit` | bool | The stone ran into the back board (rest clamped to 200..9800) instead of stopping on its own. |
| `score` | int | House: 100 / 75 / 50 / 25 / 0. Shuffleboard: 30 / 20 / 10 / 0, or -10 for the (optional) kitchen penalty. |
| `isBullseye` | bool | Button (house) or zone 30 (shuffleboard). |
| `isWin` | bool | `score > 0`. Upstream's `IsWin`; the payout is `multiplierPct` of the stake, see below. |
| `multiplierPct` | int | Payout as a percent of the stake (0 = loss). `payout = stake * multiplierPct / 100`, rounded down. |
| `keyframes` | `[[tick, x, y, v], ...]` | The path, sampled every 6 ticks plus a final keyframe at `totalTicks`. `v` is the signed velocity along the slide axis (negative when reversed), 0 at rest. Axis position is the exact constant-deceleration solution `start + dir * (v0 t - a t^2 / 2)`; the lane stays constant. |

`sheet`:

| Field | Type | Meaning |
|---|---|---|
| `stripPosition` | string | `Bottom` \| `Top` \| `Left` \| `Right` (default `Bottom`). |
| `slideDirection` | string | `LeftToRight` (default) \| `RightToLeft` \| `TopToBottom` \| `BottomToTop` \| `Alternating` \| `CenterTarget`. |
| `isReversed` | bool | The shot runs from the far end (start 9500). |
| `scoringType` | string | `CurlingHouse` (default) \| `Shuffleboard`. |
| `enableShuffleboardKitchenPenalty` | bool | Shuffleboard only. |
| `collisionMode` | string | Always `LockOnRest`: a wagered shot is one stone, so there is no stone-on-stone contact to resolve. |
| `targetDistancePercent` | number | The configured house position (default 84). |
| `targetRatio` | int | The house centre along the slide axis, mirrored when reversed (default 8400). |
| `powerVariancePercent` | number | Variance used (default 7). |
| `tickRateHz` | int | 60. |
| `scale` | int | 10000. |

House geometry (fractions of the strip, around `targetRatio`): button 0.016, 4-foot 0.048, 8-foot 0.082, 12-foot 0.125. Shuffleboard
band width 0.038: zone 30 is +/- half a band around the target, 20 and 10 the next bands short of it, the kitchen the 1.2 bands past it.

## Reproducibility

`seed` + the config revision (`curling.v1`, recorded with the play) fully determine `data`: `CurlingSimulation.Run(seed, config, power)`
draws, from one xoshiro256** stream and in this order, (1) the default power (always consumed), (2) the ice (when dynamic),
(3) the power variance, (4) the alternating-direction coin (only for `Alternating`), (5) the cross-drift side or the lane jitter.

## Relation to the vendored upstream overlay

`overlay/js/curling.js` is vendored unmodified. It consumes upstream's `curling_slide` WebSocket message (`power`, `effectivePower`,
`condition`, `targetDistancePercent`, `isReversed`, `stripPosition`, `scoringType`, ...) and simulates the stone itself with a
per-stone pebble-friction jitter and curl seeded from the drop token, so its animation can end a few percent away from the server's
`restX`/`restY`; the envelope's `score`/`multiplierPct` are authoritative for the payout. Mapping an envelope onto that message
(`data.power`, `data.effectivePower`, `data.condition`, `data.sheet.*`) and replaying `keyframes` instead of re-simulating is the
overlay-side adapter that waits on host support (see the README).
