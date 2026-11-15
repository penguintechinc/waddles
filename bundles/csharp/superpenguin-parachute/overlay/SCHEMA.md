# Parachute envelope data (`extra.data`, envelope v1)

Fixed envelope fields (`v, game:"parachute", kind:"settled", seed, actor, stake, payout`): `../../superpenguin-dropgame-core/overlay/SCHEMA.md`.
Pushed once per settled drop through `host.Overlay.Push("parachute", FullScreen, {"extra": ...})`. All positions/velocities are ints in
1/10000 board units (x and y both 0..1 of the board; the board is a fixed 1920x1080), times are 60 Hz ticks. Property order below is the emit order.

| Field | Type | Meaning |
|---|---|---|
| `board` | object | `w, h` (1920, 1080), `tickRate` (60), `totalTicks` (baseline glide length), `targetCenterX`, `targetHalfWidth`, `bullseyeHalfWidth` |
| `drop` | object | `startX`, `initialVx`, `initialVy` of the glide (start y is 400, landing y is 9400) |
| `landingX`, `landingTick` | int | where and when the player touched down |
| `score` | int | 0 missed the platform, 1 outer edge .. 100 dead centre; already halved/zeroed for a crash landing per the config |
| `hit`, `bullseye`, `win`, `destroyed` | bool | on the platform / score 100 / score > 0 (pays) / canopy destroyed by a rocket |
| `freeFallTick` | int | tick the canopy was destroyed (-1 never) |
| `dropletHits`, `rocketHits` | int | impulses applied to the player |
| `multiplierPct` | int | payout / stake in percent (350 = 3.5x) |
| `dropletPattern`, `rocketPattern` | string | `TopRain/Targeted/SideWind/Fountain`; `Targeted/Random/Barrage` or `""` when no rocket volley |
| `keyframes` | `[[tick,x,y,vx,vy]...]` | player path, every 6 ticks plus every event tick and the landing; linear interpolation between keyframes (as upstream's `GetParachutePositionAtTick`) |
| `collisions` | `[[tick,kind,source,x,y,dvx,dvy]...]` | impulses: `kind` 0 droplet, 1 rocket on canopy, 2 rocket on body; `source` = droplet/rocket `i` |
| `droplets` | `[{i,t,x,y,vx,vy,g,r,m,size,speed}]` | wave: spawn tick `t`, start, velocity (per second), gravity (overlay multiplies by 1.5), radius, knock-back mass |
| `rockets` | `[{i,t,x,y,tx,ty,vx,vy,ay,fuse,color,pattern,targeted,explodeTick,ex,ey,hit}]` | volley with the effective (re-aimed) launch values; `hit` 0 none/1 canopy/2 body; `explodeTick` -1 = still flying at touchdown |

A client replays the droplets/rockets with upstream's own overlay physics for looks and applies the recorded `collisions` and `keyframes`
for the player (upstream's `trajectoryUpdates`/`collisions` mechanism), so what it shows always matches the settled result.
Typical `data` is about 2.7 KB (max 3.7 KB over 20,000 drops).
