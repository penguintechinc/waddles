# superpenguin-curling

`!curl amount|all|max [power 1-100]` (aliases `!curling`, `!slide`, `!shuffle`): C# app bundle ported from PenguinTwitchBot's Curling /
Shuffleboard (`Bot/Commands/Games/Curling/CurlingGameService.cs`, `CurlingSimulationEngine.cs`, `Bot/Commands/Games/Common/SlideShotPhysics.cs`,
the CurlingSlide / CurlingStartSession / CurlingStopSession sub-action types, overlay `wwwroot/curling.html` + `js/curling.js` +
`media/curling_*.wav`; github.com/Psychoboy/PenguinTwitchBot, MIT, used with permission). Built on the shared `../superpenguin-dropgame-core`
library: this bundle only maps a seed to a slide and a score to a payout.

## Commands

| Command | Behaviour |
|---|---|
| `!curl <amount\|all\|max> [power]` | Slide one stone for that stake (clamped to 500). `power` is a whole number 1-100; omitted = a seed-drawn default in 65..85 (upstream's default range). Cooldown 10 s per user (upstream's). Flag `waddles.command-superpenguin-curling` (default OFF). |

`!curling`, `!slide` and `!shuffle` are the same command. Upstream's `!curling start|stop` session controls are not ported (see Deviations).

## Money (documented mapping; upstream is free play)

Exactly one atomic `economy.wager(stake, payout)` per slide. Payout is a percent of the stake by where the stone rests:

| Curling house (default) | Score | Payout |
|---|---|---|
| Button (radius 0.016) | 100 | 400% |
| 4-foot ring (0.048) | 75 | 200% |
| 8-foot ring (0.082) | 50 | 125% |
| 12-foot ring (0.125) | 25 | 75% |
| Outside the house / back board | 0 | 0% |

| Shuffleboard (config only) | Score | Payout |
|---|---|---|
| Zone 30 (centre band) | 30 | 550% |
| Zone 20 | 20 | 225% |
| Zone 10 | 10 | 110% |
| Short, out the back, or the optional -10 kitchen | 0 / -10 | 0% |

Capped by construction well under the host ledger's 100x ceiling (max 6x). The ice is rolled per shot from the seed (uniform over Normal,
Slick, Heavy Frost, Cross-Drift) and is only announced in the reply, so a viewer cannot pick the power for the ice. Monte-Carlo
estimates over 20,000 seeded slides per power (python oracle of the same simulation): house return to player peaks at about 0.91 (power 82),
0.70 with the default power draw; shuffleboard peaks about 0.88, 0.62 by default; every power below 1.0. A test pins "below 1.0 for powers 70..90 on both
sheets" and "default draw between 0.45 and 0.85".
Pay-per-score / free play (stake 0) is NOT supported: the host refuses a zero wager; deferred (would need a credit-only path).
Max bet 500 (manifest `economy.wager.max_bet`). Redelivery (same message id, platform, actor, command, args) re-runs the simulation from the
recorded seed: same reply, no second ledger movement. InsufficientFunds/OverCap answer in chat and release the cooldown; denied/unavailable/backend and kv failures propagate.

## Simulation

`CurlingSimulation.Run(seed, config, power)` is pure and deterministic (xoshiro256** from the seed; only + - * / sqrt): default power (uniform
65..85, always drawn) -> ice (dynamic) -> power variance (+/- 7 points) -> launch speed `round(eff/100 * 0.76, 4)` -> constant deceleration
`0.22 * drag` (Slick 0.78, Heavy Frost 1.28, Cross-Drift 1.05) -> rest = tee 0.05 + `v^2 / 2a`, clamped to the back board at 0.98 -> lane (0.5, plus
cross-drift pull or a variance jitter) -> score against the house at 0.84 -> keyframes every 6 ticks (60 Hz) plus the rest keyframe.
`SlideShotPhysics.cs` is this bundle's own copy of upstream's shared slide physics (the Golf port vendors its own); the only changes are
the seeded PRNG and a seed-bit coin for the Alternating direction (upstream's `string.GetHashCode()` is not stable). Golden tests pin
seeds exactly (rest position, ticks, keyframe checksum) against an independent python implementation of the same algorithm.
Collisions: upstream's engine has none (the browser overlay resolves stone-on-stone contact between concurrent stones, `LockOnRest` by default);
a wagered slide is one stone, so the only contact modelled is the back board (`wallHit`).

## Overlay

After settlement the bundle pushes `{"extra": {v, game:"curling", kind:"settled", seed, actor, stake, payout, data}}` through
`host.Overlay.Push("curling", FullScreen, ...)` and registers the widget once a day. `data` = `{sheet{...}, condition, conditionLabel, power, effectivePower,
start, restX, restY, totalTicks, wallHit, score, isBullseye, isWin, multiplierPct, keyframes[[tick,x,y,v]...]}`: full table in
`overlay/SCHEMA.md`; envelope: `../superpenguin-dropgame-core/overlay/SCHEMA.md`. A failed push is warn-logged and never alters the result. Typical envelope about 2 KB (20-40 keyframes).

### Browser source (interim) and host gap

Vendored upstream material (MIT) is under `overlay/` (`curling.html`, `js/curling.js`, `media/curling_{foul,hit,score,slide,sweep,win}.wav`, see
`overlay/ASSETS.json`; 8 files) plus the shared files under `../superpenguin-dropgame-core/overlay/common/`. The host has no static-asset section for
bundles and no sandboxed bundle-widget runtime (#458), so nothing renders them yet: serve those directories statically and feed the page the pushed
`extra` envelope (`curling.js` is vendored unmodified; it animates from upstream's `curling_slide` message, which the envelope fields map onto, and
re-simulates the stone with its own jitter, so its picture can end slightly off the server's `restX`/`restY`; the envelope score/payout is authoritative).
Not vendored (listed in `ASSETS.json` under `third_party_or_unclear`): upstream WebSocket scripts, the reconnecting-websocket library, Google Fonts,
and the theme/stone/house/emote images (runtime data in upstream's settings database).

## Deviations from upstream

- Server-side deterministic slide (replayable/auditable) instead of in-browser physics; same constants, scoring and rules.
- Stake/payout instead of free-play scoring. Ice condition is rolled per shot from the seed (upstream rolls per timed round).
- CurlingSlide sub-action: replaced by the `!curl` wager command (power argument kept; emote/decal and theme arguments not ported, they need overlay/host support).
- CurlingStartSession / CurlingStopSession sub-actions, timed rounds, round queue, last-call cutoff, per-user round caps, round chat announcements and the
  `CurlingStart/End/Win/Lose/Bullseye` triggers: deferred. A wagered slide settles on its own and has no round to open or close; a shared round needs overlay
  session state and host support that do not exist yet.
- Stone-on-stone collisions (`CollisionMode`): not applicable to a single wagered stone.
- Default sheet only (bottom strip, left to right, house at 84%, house scoring): the config type supports the other strips, directions, shuffleboard
  scoring and kitchen penalty (tested), but no streamer settings UI exists.

## Build and test

```
bash scripts/test-superpenguin-bundle.sh curling          # 113 tests, 99.5% line coverage
bash scripts/build-superpenguin-bundle.sh curling         # pinned Dockerfile -> /tmp/superpenguin-curling-out/superpenguin_curling.wasm
```

Host needs: `storage.kv`, `identity.resolve`, `chat.send:discord|twitch`, economy tool calls (`points_view`, `get_balance`), `economy.wager`, `overlay.media`.
wasm (stage-next-tools, not committed): 7,662,318 bytes, sha256 a209ad15941e41f9b4e4cdf356fb256c8a958255664423815ce17ee8398a25f5; exports process-stage, action-stage, streaming-lifecycle.
