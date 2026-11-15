# superpenguin-parachute

`!parachute amount|all|max`: C# app bundle ported from PenguinTwitchBot's Parachute Drop (`Bot/Commands/Games/Parachute/`
`ParachuteDropService.cs`, `ParachuteSimulationEngine.cs`, `DropletPatternGenerator.cs`, `RocketTrajectoryGenerator.cs`; the Parachute Drop /
Droplets / Rockets sub-actions; overlay `wwwroot/parachute.html` + `js/parachute.js`; github.com/Psychoboy/PenguinTwitchBot, MIT, used with
permission). Built on the shared `../superpenguin-dropgame-core` library: this bundle maps a seed to a drop and a score to a payout.

## Commands

| Command | Behaviour |
|---|---|
| `!parachute <amount\|all\|max>` | One drop for that stake (clamped to 500). Cooldown 10 s per user (upstream `!drop`'s). Flag `waddles.command-superpenguin-parachute` (default OFF). |

## Money (documented mapping; upstream is free play)

Exactly one atomic `economy.wager(stake, payout)` per drop. `payout = stake * score * 700 / 10000` (rounded down), score = upstream's 0..100 landing score
(0 missed, 1 outer platform edge, 100 dead centre): score 100 = 7x, 50 = 3.5x, 15 breaks even. A rocket-destroyed canopy that still lands on the platform
scores half (`CrashLandingScoreMode.HalfScore`; the config also supports upstream's `ZeroScore`/`PreserveScore`). Max 7x, far under the host's 100x cap.
Measured over 20,000 seeds with the default config: win (score > 0) 28.1%, return to player 0.927, canopy destroyed 12.6%, bullseye 0.1%.
Pay-per-score / free play (stake 0) is NOT supported: the host refuses a zero wager; deferred (needs a credit-only path).
Max bet 500 (manifest `economy.wager.max_bet`). Redelivery (same message id, platform, actor, command, args) re-runs the simulation from the recorded seed: same reply,
no second ledger movement. InsufficientFunds/OverCap answer in chat and release the cooldown; denied/unavailable/backend and kv failures propagate.

## Simulation

`ParachuteSimulationEngine.Run(seed, config)` is pure and deterministic (xoshiro256** from the seed; only + - * / sqrt, a Pade `tan`; 60 Hz ticks on a fixed 1920x1080 board).
Draw order: start x, glide angle (initial vx), wave start tick, droplet pattern + per-droplet size/speed/trajectory, a volley roll, rocket pattern/launch tick/specs, then rocket
kill rolls during the steps. Per tick: droplets (gravity, walls, platform, floor, contact knock-back with a 9-tick rate limit), rockets (a targeted rocket re-aims at the
live player with a lead, continuous canopy-then-body hit test, kill roll, knock-back, fuse/apex detonation), then the player (canopy drag and glide recovery, or free fall
at up to 0.98/s once destroyed; wall bounces; the one-time target-rim bounce) until touchdown. Constants and formulas are upstream's (generators, score, clamps).
Hazards: every drop gets a wave of 5 droplets (`Mixed` pattern = one of TopRain/Targeted/SideWind/Fountain, random size/speed) and a 20% chance of one `Targeted` rocket
(configurable, with Barrage/Random). Golden tests pin six seeds exactly (landing tick/x, score, hit counts, keyframe count and checksum).

## Overlay

After settlement the bundle pushes `{"extra": {v, game:"parachute", kind:"settled", seed, actor, stake, payout, data}}` through `host.Overlay.Push("parachute", FullScreen, ...)` and
registers the widget once a day. The `data` schema is in `overlay/SCHEMA.md` (envelope: `../superpenguin-dropgame-core/overlay/SCHEMA.md`). A failed push is warn-logged and never
alters the result.

### Browser source (interim) and host gap

Vendored upstream material (MIT) is under `overlay/` (`parachute.html`, `js/parachute.js`, `media/{parachute_drop,parachute_pop,droplet,droplet_hit,rocket_launch,rocket_explode,cannonball}.wav`,
`media/splat.mp3`, see `overlay/ASSETS.json`: 10 files) plus the shared files under `../superpenguin-dropgame-core/overlay/common/`. The host has no static-asset section for bundles and no
sandboxed bundle-widget runtime (#458), so nothing renders them yet: serve those directories statically and feed the page the pushed `extra` envelope (`data.keyframes`/`collisions`/`droplets`/`rockets`
replace upstream's WebSocket `parachute_drop`/`parachute_droplets`/`parachute_rockets` messages; `parachute.js` is vendored unmodified). Not vendored (`third_party_or_unclear`): upstream WebSocket
scripts, the reconnecting-websocket library, Google Fonts, and the theme/character images (runtime database data).

## Deviations from upstream

- Whole drop simulated server-side from a recorded seed (replayable/auditable); upstream runs droplet/rocket collisions live in the browser. Droplet-to-droplet bounces and random spin/jitter are not simulated.
- Stake/payout instead of free-play scoring; every drop carries its own seeded droplet wave and optional rocket volley instead of streamer/mod-fired ones. Crash landing scores half (upstream default: preserve).
- Command is `!parachute` (upstream `!drop`); no emote/theme arguments, timed sessions, `!drop start|stop` controls or multi-parachutist rounds (need overlay/host support).
- Only the default board (target centre 0.50, 18% wide, gravity 1.0, bounciness 0.85); the config type supports other values but no streamer settings UI exists.
- The manifest declares `overlay.media` without `allowed_hosts`/`max_duration_seconds`, like the other drop games; the hub manifest parser currently rejects that (see report).

## Build and test

```
bash scripts/test-superpenguin-bundle.sh parachute          # 96 tests, 99.51% line coverage
bash scripts/build-superpenguin-bundle.sh parachute         # pinned Dockerfile -> /tmp/superpenguin-parachute-out/superpenguin_parachute.wasm
```

Host needs: `storage.kv`, `identity.resolve`, `chat.send:discord|twitch`, economy tool calls (`points_view`, `get_balance`), `economy.wager`, `overlay.media`.

wasm (stage-next-tools, not committed): 7,812,671 bytes, sha256 78c0f4b19fb6579477cdda5613bd5b7a88983cce9f04028414a6597cd552fae5; exports process-stage, action-stage, streaming-lifecycle.
