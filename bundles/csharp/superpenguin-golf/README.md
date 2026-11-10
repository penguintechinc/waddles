# superpenguin-golf

`!golf amount|all|max [power]`: C# app bundle ported from PenguinTwitchBot's Golf (Chip and Putt) game
(`Bot/Commands/Games/Golf/GolfGameService.cs`, `GolfSimulationEngine.cs`, `PlayerGolfRoundState.cs`, `Common/SlideShotPhysics.cs`,
sub-actions `GolfPutt`/`GolfStartSession`/`GolfStopSession`, overlay `wwwroot/golf.html` + `js/golf.js`; github.com/Psychoboy/PenguinTwitchBot,
MIT, used with permission). Built on the shared `../superpenguin-dropgame-core` library: this bundle maps a seed and a swing power to one tee shot.

## Commands

| Command | Behaviour |
|---|---|
| `!golf <amount\|all\|max> [power 1-100]` | One wagered tee shot (stake clamped to 500). `power` omitted = upstream's default draw, uniform 65-85, from the seed. Cooldown 5 s per user (upstream's). Flag `waddles.command-superpenguin-golf` (default OFF). |

## Single stroke per command (upstream round shape)

Upstream's default is `MaxStrokesPerPlayer = 1`: each viewer gets one Driver shot per entry, scored closest-to-the-pin. This port is exactly
that mode, one wagered shot per command, settled in one `economy.wager`. Upstream's optional multi-stroke mode (2-5 strokes, ball stays on
the course, Wedge/Putter follow-ups, stroke penalty) is NOT ported: it needs per-user round state across commands and an overlay that keeps the
ball, neither of which exists. A whole multi-stroke round could not be settled from one seed anyway, because upstream's later strokes depend on
the player's chosen power per stroke.

## Money (documented mapping; upstream is free play)

Exactly one atomic `economy.wager(stake, payout)` per shot. The closest-to-the-pin score is upstream's (`calculateGolfScore`, single stroke so
no penalty): sunk = 100, on the green 1..100 by distance from the pin, off the green 0. Payout:

| Result | Payout |
|---|---|
| ball sunk in the cup (hole-in-one) | `stake * 300 / 100` (3x) |
| on the green, score `S` (1..100) | `stake * (S * 2) / 100` (0.02x..2x) |
| off the green (score 0) | 0 |

Integer percent math, floored (so a 1-star bet on a score-1 ball pays 0). Max payout 3x, far below the host ledger's 100x ceiling. Max bet 500
(manifest `economy.wager.max_bet`). Return to player, measured over 20,000 seeds per power on the default config: a skilled player who picks power
~80 returns about 0.91 of the stake (best case; the test asserts the best power over 1..100 stays below 1.0 over 4,000 seeds each); upstream's
random default power (omit `[power]`) returns about 0.49. Power matters: the pin is 84% down the course and a ball reaches it around power 76-81,
but the green condition (drawn per shot from the seed: Standard, Fast Links x0.78 drag, Heavy Dew x1.28, Cross-Breeze x1.05 + sideways push)
moves the ideal power by up to 10 and is only known after the shot, and power gets +/-3% variance.
Pay-per-score / free play (stake 0) is NOT supported: the host refuses a zero wager; deferred (would need a credit-only path).
Redelivery (same message id, platform, actor, command, args) re-runs the simulation from the recorded seed: same reply, no second ledger movement.
InsufficientFunds/OverCap answer in chat and release the cooldown; denied/unavailable/backend and kv failures propagate.

## Simulation

`GolfEngine.Shoot(seed, power, config)` is pure and deterministic (xoshiro256** from the seed; only + - * / sqrt): draws, in a fixed order, the
green condition, the default power (if omitted), the +/-3% power variance, then a breeze/rim direction bit when needed. Physics is upstream's
`GolfSimulationEngine.SimulateGolfShot` for stroke 1 / Driver: `v0 = power/100 * 0.76`, travel `d = v0^2 / (2 * 0.22 * drag)`, tee at 0.05, pin at
0.84, cup radius 0.014, green radius 0.035, cup capture at rolling speed <= 0.070 else a lip-out to `pin + 0.014 * (1.35 .. 2.8)`, the 2.5D
six-keyframe flight (apex, bounce, apex, bounce, roll) and the 1.1-4.2 s duration clamp. `SlideShotPhysics.cs` is this bundle's own vendored copy of
upstream's shared slide-shot helpers (the Curling bundle carries its own). Golden tests pin three seeds exactly and hand-derived variance-free shots
(power 76/77/78/79/60) check the formulas.

## Overlay

After settlement the bundle pushes `{"extra": {v, game:"golf", kind:"settled", seed, actor, stake, payout, data}}` through
`host.Overlay.Push("golf", FullScreen, ...)` and registers the `golf` widget (Golf Chip and Putt) once a day. Schema: `overlay/SCHEMA.md`
(envelope: `../superpenguin-dropgame-core/overlay/SCHEMA.md`). A failed push is warn-logged and never alters the result.

### Browser source (interim) and host gap

Vendored upstream material (MIT) is under `overlay/` (`golf.html`, `js/golf.js`, `media/golf_{bounce,cup,lipout,strike,win}.wav`, see
`overlay/ASSETS.json`, 7 files) and the shared files under `../superpenguin-dropgame-core/overlay/common/`. The host has no static-asset section for
bundles and no sandboxed bundle-widget runtime (#458), so nothing renders them yet: serve those directories statically and feed the page the pushed
`extra` envelope. `golf.js` is vendored unmodified and is client-authoritative upstream (it rolls its own physics from a `golf_shot` message);
an adapter must drive it from the envelope's `keyframes`/`rest`/`score` instead of letting it re-roll, because the money was paid on the server result
(mapping in `overlay/SCHEMA.md`). Not vendored (`ASSETS.json` `third_party_or_unclear`): upstream WebSocket scripts, the reconnecting-websocket library,
Google Fonts, and theme/ball/flag/emote images (runtime data).

## Deviations from upstream

- Server-side deterministic shot from a recorded seed (replayable/auditable) instead of in-browser physics with `Math.random`.
- Stake/payout instead of free-play trigger variables; single stroke only (see above).
- Green condition drawn from the seed per shot (upstream: once per session); fixed bottom strip, left to right, pin 84% (upstream defaults; `Alternating` direction
  hashes a per-process string and is not replayable).
- No `!golf start|stop` timed sessions, `!ball`/`!putt`/`!chip`/`!swing` aliases, emote/theme arguments (a trailing non-number is refused with the usage text),
  per-session drop limits, outcome automations or the Win/Bullseye/Lose triggers (need session state and host support that does not exist).
- Upstream rounds `Math.round` half up; the port uses away-from-zero rounding for the score, the same on positive values.

## Build and test

```
bash scripts/test-superpenguin-bundle.sh golf          # 62 tests, 99.53% line coverage
bash scripts/build-superpenguin-bundle.sh golf         # pinned Dockerfile -> /tmp/superpenguin-golf-out/superpenguin_golf.wasm
```

Host needs: `storage.kv`, `identity.resolve`, `chat.send:discord|twitch`, economy tool calls (`points_view`, `get_balance`), `economy.wager`, `overlay.media`.
Needed core changes (reported, not made here): a `golf` entry in `bundles/core-bundles.yaml`/`.ci` and the Makefile generic targets if not already generic;
nothing in the core library.

wasm (stage-next-tools, not committed): 7,601,293 bytes, sha256 457b264ed7556a10ff66161b56c2ceb4e9e89efbe0bee8217e4bf892971fcb94; exports process-stage, action-stage, streaming-lifecycle.
