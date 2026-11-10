# Balloon envelope data (`extra.data`, game `balloon`, kind `settled`)

The fixed envelope fields (`v, game, kind, seed, actor, stake, payout, data`) are documented in
`../../superpenguin-dropgame-core/overlay/SCHEMA.md`; this file documents `data` for `game = "balloon"`.
Built by `BalloonLogic.DataJson` after the payout settles. Positions and velocities are integers in 1/10000 of the field (x right,
y down, `0..10000`; a velocity is 1/10000 field per second). Time is in ticks at `field.tickHz` (60). No names, no PII.

The whole flight is reproducible: `seed` + config revision `balloon.v1` fully determine `data` and `payout`
(`BalloonSimulation.Run(seed, config)`, one xoshiro256** drawn in a fixed order). A client replays the keyframes; it never decides anything.

## Fields

| Field | Type | Meaning |
|---|---|---|
| `field` | `{w,h,tickHz}` | Virtual field `1920 x 1080` px the physics ran on, and the tick rate (60). |
| `launch` | object | `x`,`y` start (cannon at `5000`, `9400`); `vx`,`vy` the avatar's launch velocity (`vy` negative = up; already includes the per-flight speed variation); `seconds` nominal flight time; `totalTicks`; `size` (0.90..1.10 x10000) canopy/offset scale; `speed` (0.88..1.12 x10000) speed variation. |
| `cloud` | object | The moving target platform: centre at time `t` seconds = `base + sin(phase + omega * t) * span` (all x10000; `phase` radians x10000, `omega` rad/s x10000), `halfWidth` (the dock window; the golden bullseye is the inner quarter, score 98+), `y` platform height, `xAtEnd` the centre at `endTick`. A stationary cloud has `omega = 0`. |
| `wave` | object | The flight's one hazard wave: `pattern` (`TopRain`\|`Targeted`\|`SideWind`), `speed` preset (`Slow`\|`Normal`\|`Fast`\|`Hyper`), `spawnTick` (all hazards appear at this tick), `hazards[]`. |
| `wave.hazards[]` | object | `kind` (`Nudge`\|`HeavyPush`\|`WindGust`\|`DartPop`), spawn state `x`,`y`,`vx`,`vy`, gravity `g`, collision radius `r`, and `hitTick` (tick it struck the balloon, `-1` if it never did). A hazard flies ballistically from its spawn state (`vy += g*dt; x += vx*dt; y += vy*dt` per tick) and is removed at `hitTick` or when it leaves the field. |
| `keyframes` | `[[tick,x,y],...]` | The avatar position every 6 ticks plus tick 0 and the last tick (`endTick`). Interpolate linearly; the canopy is drawn `0.165 * size` above the avatar. |
| `endTick` | int | Tick the flight resolved (docked, missed, fell off the field, or timed out). |
| `popped` | bool | A dart struck the balloon: `popTick` is its tick, `popSpin` (rad/s x10000, signed) the avatar's tumble; after the pop the avatar free-falls and the flight scores 0. `popTick` is `-1` otherwise. |
| `hit` | bool | The balloon reached the cloud's y inside `halfWidth`: a dock. |
| `score` | int | Dock score 1..100 (`round(100 - 99 * distance / halfWidth)`, half up); 0 for a miss or a pop. |
| `bullseye` | bool | Dock with score 98 or more (upstream overlay's rule). |
| `timedOut` | bool | The 40 s tick watchdog ended an unresolved flight (counted as a miss; never seen with a sane config). |
| `multiplierPct` | int | Payout as a percent of the stake (`payout * 100 / stake`; 0 for a pop or miss). |

## Payout mapping (this port; upstream is free play)

`payout = stake * percent / 100`, `percent` = 0 unless `hit`; `bullseye` -> `BullseyePercent` (1000); otherwise `HitBasePercent + score * PercentPerPoint`
(250 + 4 per point). Capped at 10000 percent (host ledger's 100x ceiling). See the bundle README for the measured return to player.

## Reading the flight

1. t = 0: the avatar is at `launch.x/y` with velocity `launch.vx/vy`; the cloud is at `cloud.base + sin(cloud.phase) * span`.
2. At `wave.spawnTick` every hazard appears at its spawn state and flies; a hazard with `hitTick >= 0` disappears there with an effect by `kind`
   (`DartPop` pops the balloon, `HeavyPush` slams it down and stalls the climb 0.9 s, `Nudge`/`WindGust` knock it sideways and stall 0.45 s).
3. The avatar follows `keyframes`; at `endTick` it docks on the cloud (`hit`), flies past (miss) or has fallen off the bottom (`popped`).
