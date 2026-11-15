# superpenguin-plinko

`!plinko amount|all|max`: C# app bundle ported from PenguinTwitchBot's Plinko Bounce (`Bot/Commands/Games/Plinko/PlinkoDropService.cs`,
`PlinkoSimulationEngine.cs`, overlay `wwwroot/plinko.html` + `js/plinko.js`; github.com/Psychoboy/PenguinTwitchBot, MIT, used with
permission). Built on the shared `../superpenguin-dropgame-core` library: this bundle only maps a seed to a drop.

## Commands

| Command | Behaviour |
|---|---|
| `!plinko <amount\|all\|max>` | Drop one puck for that stake (clamped to 500). Cooldown 10 s per user (upstream's). Flag `waddles.command-superpenguin-plinko` (default OFF). |

## Money (documented mapping; upstream is free play)

Exactly one atomic `economy.wager(stake, payout)` per drop. Payout by landing bucket:

| Bucket | Payout |
|---|---|
| losing (`IsWin` false) | 0 |
| winning with `Score > 0` | `stake * Score / 100` (Score is read as percent of stake; 250 = 2.5x) |
| winning with `Score == 0` (upstream default WINNER slot) | `stake * 500 / 100` (5x) |

Capped at 100x (host ledger ceiling). With upstream's default 5-bucket board (MISS 25, MISS 25, WINNER 15, MISS 25, MISS 25 weights) the physics
lands the centre WINNER slot about 18.6% of drops (20,000-seed run: 4721/3497/3715/3353/4714), return to player about 0.93.
Pay-per-score / free play (stake 0) is NOT supported: the host refuses a zero wager; deferred (would need a credit-only path).
Max bet 500 (manifest `economy.wager.max_bet`). Redelivery (same message id, platform, actor, command, args) re-runs the simulation from the
recorded seed: same reply, no second ledger movement. InsufficientFunds/OverCap answer in chat and release the cooldown; denied/unavailable/backend and kv failures propagate.

## Simulation

`PlinkoSimulation.Run(seed, config)` is pure and deterministic (xoshiro256** from the seed; only + - * / sqrt): weighted bucket pick ->
release position (nearest top-row peg +/-1, micro offset; upstream's rule) -> upstream's puck physics (gravity 1800 px/s^2, drag, peg
restitution/friction, apex kick, dividers, walls, floor, settle rule) at 240 Hz on a fixed 1920x1080 board. The landing bucket is whatever
the physics decides (upstream's overlay reports the same way); the weights only bias the release. Output: bucket, keyframes every 8 steps
(`[ms, x, y]` in 1/10000 board units), peg contacts (`[ms, row, col]`). Golden tests pin three seeds exactly.

## Overlay

After settlement the bundle pushes `{"extra": {v, game:"plinko", kind:"settled", seed, actor, stake, payout, data}}` through
`host.Overlay.Push("plinko", FullScreen, ...)` and registers the widget once a day. `data` = `{board{w,h,rows,pegColumns,pegRadius,puckRadius,bucketHeight,buckets[{label,score,win,weight,color}]},
startX, bucket, label, win, bullseye, score, multiplierPct, timedOut, keyframes[[ms,x,y]...], pegHits[[ms,row,col]...]}`. Envelope: `../superpenguin-dropgame-core/overlay/SCHEMA.md`.
A failed push is warn-logged and never alters the result. Typical envelope 3-8 KB (about 160 keyframes on average, 425 at most in 20,000 drops).

### Browser source (interim) and host gap

Vendored upstream material (MIT) is under `overlay/` (`plinko.html`, `js/plinko.js`, `media/plinko_{drop,peg,win}.wav`, see `overlay/ASSETS.json`)
and the shared files under `../superpenguin-dropgame-core/overlay/common/`. The host has no static-asset section for bundles and no sandboxed
bundle-widget runtime (#458), so nothing renders them yet: serve those directories statically and feed the page the pushed `extra` envelope
(its `data.keyframes`/`bucket` replace upstream's WebSocket `plinko_drop` message; `plinko.js` is vendored unmodified). Not vendored (listed in
`ASSETS.json` under `third_party_or_unclear`): upstream WebSocket scripts, the reconnecting-websocket library, Google Fonts, and the theme/emote images (runtime data).

## Deviations from upstream

- Server-side deterministic physics (replayable/auditable) instead of in-browser Matter-style physics; same constants and rules.
- Stake/payout instead of free-play scoring; no emote/theme arguments, timed sessions, per-session drop limits or player-vs-player collisions (need overlay/host support).
- Default board only (rows 4, gravity 1.0, bounciness 0.75); the config type supports other boards but no streamer settings UI exists.

## Build and test

```
bash scripts/test-superpenguin-bundle.sh plinko          # 41 tests, 99.25% line coverage
bash scripts/build-superpenguin-bundle.sh plinko         # pinned Dockerfile -> /tmp/superpenguin-plinko-out/superpenguin_plinko.wasm
```

Host needs: `storage.kv`, `identity.resolve`, `chat.send:discord|twitch`, economy tool calls (`points_view`, `get_balance`), `economy.wager`, `overlay.media`.
wasm (stage-next-tools, not committed): 7,673,473 bytes, sha256 0c472c59c2df9bf48799d04c64dba993bedfad424f2a86acf691765fb466e2de; exports process-stage, action-stage, streaming-lifecycle.
