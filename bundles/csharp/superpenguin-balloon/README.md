# superpenguin-balloon

`!balloon amount|all|max`: C# app bundle ported from PenguinTwitchBot's Balloon Ascent (`Bot/Commands/Games/Balloon/BalloonAscentService.cs`,
`BalloonAscentSimulationEngine.cs`, `BalloonHazardPatternGenerator.cs`, the `BalloonAscent`/`BalloonHazards` sub-action types, overlay
`wwwroot/balloon.html` + `js/balloon.js`; github.com/Psychoboy/PenguinTwitchBot, MIT, used with permission). Built on the shared
`../superpenguin-dropgame-core` library: this bundle only maps a seed to a flight.

## Commands

| Command | Behaviour |
|---|---|
| `!balloon <amount\|all\|max>` | Launch one balloon for that stake (clamped to 500). Cooldown 10 s per user (upstream's). Flag `waddles.command-superpenguin-balloon` (default OFF). |

## Money (documented mapping; upstream is free play)

Exactly one atomic `economy.wager(stake, payout)` per flight. A balloon either docks on the moving cloud (score 1..100, 98+ = golden bullseye),
flies past it (miss), or is popped by a dart (always a loss). Payout percent of the stake:

| Result | Payout |
|---|---|
| miss, pop, timeout | 0 |
| dock, score 1..97 | `stake * (250 + 4 * score) / 100` (2.54x .. 6.38x) |
| golden bullseye (score 98+) | `stake * 1000 / 100` (10x) |

Capped at 100x (host ledger ceiling). Measured over 20,000 seeds with the default config (3 hazards): docks 19.5%, pops 8.0%, bullseyes 0.57%,
average dock score 52.2, **return to player 0.916**. A clear sky (0 hazards) would be 1.02 and 6 hazards 0.80, so the hazard wave carries the
house edge; `BalloonConfig` carries every knob (`HazardCount`, hazard kinds, cloud width/centre/speed, buoyancy, payout percents) but no streamer
settings UI exists yet. Pay-per-score / free play (stake 0) is NOT supported: the host refuses a zero wager; deferred (would need a credit-only path).
Max bet 500 (manifest `economy.wager.max_bet`). Redelivery (same message id, platform, actor, command, args) re-runs the simulation from the
recorded seed: same reply, no second ledger movement. InsufficientFunds/OverCap answer in chat and release the cooldown; denied/unavailable/backend
and kv failures propagate.

## Simulation

`BalloonSimulation.Run(seed, config)` is pure and deterministic (xoshiro256** from the seed; only + - * / sqrt, sines by polynomial) at 60 Hz on a
fixed 1920x1080 field. Faithful to upstream: flight time `5.8 s / buoyancy^0.25` (3.2..8.5 s), cannon launch at x 0.5 with `sin(aim) * 0.075 +- 0.010`
sideways speed, per-flight speed (0.88..1.12) and size (0.90..1.10) variation, the steady climb with wall rebounds (x 0.05..0.95, restitution 0.75),
the cloud sweeping `base + sin(phase + 0.14 * speed * t) * span`, hazard flight (gravity, canopy collision in px), dart pop (free fall at 2.2 gravity,
score 0), anvil slam (+0.08 down, 0.9 s stall), nudge/wind knock (+-0.12 sideways, 0.45 s stall), arrival at the cloud (score
`round(100 - 99 * distance / halfWidth)`, bullseye at 98+), and upstream's hazard patterns (`TopRain`, `Targeted`, `SideWind`; wind gusts always side-on),
speed presets (Slow/Normal/Fast/Hyper) and physics radii. Draw order (fixed): cannon phase, cannon jitter, speed, size, cloud phase, wave tick,
wave pattern, wave speed, then per hazard its kind and trajectory, then pop spin/jitter and knock directions as they happen. Output: keyframes every 6
ticks, the wave (with each hazard's `hitTick`), the cloud motion and the result. Golden tests pin six seeds plus a dart-wave pop exactly.

## Overlay

After settlement the bundle pushes `{"extra": {v, game:"balloon", kind:"settled", seed, actor, stake, payout, data}}` through
`host.Overlay.Push("balloon", FullScreen, ...)` and registers the widget once a day. `data` is documented in `overlay/SCHEMA.md` (field, launch,
cloud, wave + hazards, keyframes, endTick, popped/popTick/popSpin, hit, score, bullseye, timedOut, multiplierPct); the shared envelope in
`../superpenguin-dropgame-core/overlay/SCHEMA.md`. A failed push is warn-logged and never alters the result. Typical envelope 2-4 KB (about 50 keyframes).

### Browser source (interim) and host gap

Vendored upstream material (MIT) is under `overlay/` (`balloon.html`, `js/balloon.js`, `media/balloon_{anvil,launch,nudge,pop,win}.wav`, see
`overlay/ASSETS.json`) and the shared files under `../superpenguin-dropgame-core/overlay/common/`. The host has no static-asset section for bundles and
no sandboxed bundle-widget runtime (#458), so nothing renders them yet: serve those directories statically and feed the page the pushed `extra`
envelope. `balloon.js` is vendored unmodified and still decides outcomes itself in the browser (wall clock, `Math.random`) from upstream's
`balloon_drop`/`balloon_hazards` WebSocket messages; a host-supported overlay runtime must instead replay the bundle's keyframes (the server result is
authoritative, never the browser). Not vendored (listed in `ASSETS.json` under `third_party_or_unclear`): upstream WebSocket scripts, the
reconnecting-websocket library, Google Fonts, and the theme/emote/canopy/cloud images (runtime data).

## Deviations from upstream

- Server-side deterministic physics (replayable/auditable) instead of in-browser physics (upstream trusts the browser's reported score); same constants and rules.
- Stake/payout instead of free-play scoring; popped balloons always score 0 (upstream's overlay path; its `PoppedScoreMode` half/preserve options only affect a baseline calculation the overlay never uses).
- Hazards: upstream waves are triggered by a streamer/moderator (`!balloonhazards`, sub-actions) at an arbitrary moment; here each flight faces ONE seeded wave
  (default 3 hazards, random pattern/speed/kinds, spawn within the first 3 s). `Targeted` aims at the balloon's real launch path (upstream's server only knows a straight-up baseline because the cannon shot is chosen in the browser).
- Single balloon per play: no balloon-balloon collisions, no timed sessions (`!balloon start|stop`), no round leader, no emote/theme arguments (need overlay/host support).
- Only `!balloon` is registered, not `!float`/`!ascent`: the shared cooldown and replay key use the command word, so an alias would bypass the per-user cooldown (core change: key them by the game's first command).

## Build and test

```
bash scripts/test-superpenguin-bundle.sh balloon          # 54 tests, 99.41% line coverage
bash scripts/build-superpenguin-bundle.sh balloon         # pinned Dockerfile -> /tmp/superpenguin-balloon-out/superpenguin_balloon.wasm
```

Host needs: `storage.kv`, `identity.resolve`, `chat.send:discord|twitch`, economy tool calls (`points_view`, `get_balance`), `economy.wager`, `overlay.media`.
wasm (stage-next-tools, not committed): 7,774,553 bytes, sha256 989d833c8158a4acdf18849a85d8e828947b18dc6cf0b8d4d69f748f09ccc0f3; exports process-stage, action-stage, streaming-lifecycle.
