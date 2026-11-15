# superpenguin-ufoclaw

`!ufo amount|all|max` (alias `!ufoclaw`): C# app bundle ported from PenguinTwitchBot's UFO Claw (`Bot/Commands/Games/UfoClaw/UfoClawService.cs`,
`UfoClawModels.cs`, `UfoClawStore.cs`, sub-action `UfoClawControlType.cs`, overlay `wwwroot/ufo.html` + `js/ufo.js` + `media/ufo_claw_*.wav`,
help `wwwroot/help/ufoclaw/*.md`; github.com/Psychoboy/PenguinTwitchBot, MIT, used with permission). Built on the shared
`../superpenguin-dropgame-core` library: this bundle only maps a seed to a claw grab.

## Decision: single wagered command on `WageredGameBase`

Upstream's grab is decided entirely server-side before the animation: a weighted draw over the eligible pool (`UfoClawDraw.Select`), a
winner that "always wins", and an animation that is a pure function of grab id + pool. That needs no persisted state to decide, so one
`!ufo <stake>` = one deterministic grab = one `economy.wager` fits the base exactly. Upstream's persisted control/draw model is
operator tooling for a free stream draw and is deferred (below).

## Commands

| Command | Behaviour |
|---|---|
| `!ufo <amount\|all\|max>` / `!ufoclaw ...` | One claw grab for that stake (clamped to 500). Cooldown 10 s per user (upstream's `!joinclaw` was 2 s; a wagered grab is 10 s like Plinko). Flag `waddles.command-superpenguin-ufoclaw` (default OFF). |

## Money (documented mapping; upstream awards nothing)

Upstream grabs only name a prize (label/value feed trigger variables, "not an automatic payout"). Here each pit ball carries a multiplier
percent of the stake; exactly one atomic `economy.wager(stake, payout)` per grab, `payout = stake * pct / 100` rounded down (capped at the host's
100x ceiling). Upstream ships no default prize set, so this table is this port's own (weights are per mille, as `UfoClawEntry.Weight`):

| Ball | Weight | Payout | Colour |
|---|---|---|---|
| Dud | 320 | 0 | `#7a7f87` |
| Pebble | 250 | 0.5x | `#2ec4b6` |
| Fish | 200 | 1x (got back) | `#3a86ff` |
| Cookie | 130 | 1.5x | `#ff9f1c` |
| Gem | 60 | 2x | `#9b5de5` |
| Crown | 30 | 4x | `#f15bb5` |
| Golden Penguin | 10 | 15x | `#ffd60a` |

Return to player 91.0% (tested), hit rate (payout > stake) 23%. Rounding down means a tiny stake can pay less than its fraction (stake 1 on a Pebble
pays 0). Max bet 500 (manifest `economy.wager.max_bet`). Redelivery (same message id, platform, actor, command, args) re-runs the simulation from the
recorded seed: same reply and overlay envelope, no second ledger movement. InsufficientFunds/OverCap answer in chat and release the cooldown;
denied/unavailable/backend and kv failures propagate. Free play / pay-per-grab with stake 0 is NOT done (the host refuses a zero wager); deferred.

## Simulation

`UfoClawSimulation.Run(seed, config)` is pure and deterministic (xoshiro256** from the seed, draws in a fixed order: winner sample, slip, decoy
count, decoy shuffle). The pit is every prize with weight > 0, in config order. The winner is upstream's weighted draw (cumulative `target < cumulative`,
exact decimal; upstream's boundary goldens 0 / 0.249 / 0.25 / 0.999 are tests). The tease follows upstream's rules: it only happens when `tease` is on
and `SlipChancePercent > unit * 100`; the decoy count is `1 + floor(unit * MaxSuspenseDrops)` capped at the number of non-winning balls; decoys are a
shuffle of the non-winning balls and never include the winner. Config ranges are upstream's `ValidateSettings` ranges (clamped; a bad prize or an empty
pool throws). The slip never changes the winner. Golden seeds are pinned (generated and cross-checked with an independent Python xoshiro256**).

## Overlay

After settlement the bundle pushes `{"extra": {v, game:"ufoclaw", kind:"settled", seed, actor, stake, payout, data}}` through
`host.Overlay.Push("ufoclaw", FullScreen, ...)` and registers the widget once a day (1920x1080). `data` = `{scene, rules, pool[{label,multiplierPct,weight,color}],
winningIndex, label, multiplierPct, win, slip, feints[]}`, see `overlay/SCHEMA.md`; envelope: `../superpenguin-dropgame-core/overlay/SCHEMA.md`.
A failed push is warn-logged and never alters the result.

### Browser source (interim) and host gap

Vendored upstream material (MIT) is under `overlay/`: `ufo.html`, `js/ufo.js`, `media/ufo_claw_{catch,drop,grab,lift,motor,reveal}.wav`, and the upstream
help pages `help/ufoclaw/*.md` (11 files, see `overlay/ASSETS.json`; `ufo.js` is unmodified). The host has no static-asset section for bundles and no
sandboxed bundle-widget runtime (#458), so nothing renders them yet: serve `overlay/` statically and feed the page the pushed `extra` envelope. The page
as vendored still boots from upstream's `GET /api/overlay/ufoclaw-state` + `ufoclaw` websocket and posts `/api/overlay/ufoclaw-complete`; an adapter must
supply `entries` = `data.pool`, `grab.winningIndex`, and use `data.slip`/`data.feints` in place of `shouldSlip/suspenseSequence` (see `overlay/SCHEMA.md`).
Not vendored (`ASSETS.json` `third_party_or_unclear`): `reconnectingWS.min.js` (third party), `ws-client.js`/`ws-status.js` (shared upstream bot scripts), Google Fonts Rubik.

## Deviations from upstream and deferred control actions

- Free draw -> stake/payout; a `Dud` ball exists so the game has losing grabs (upstream "every accepted grab wins").
- Winner/tease randomness from the recorded seed (replayable, auditable) instead of `RandomNumberGenerator` + a browser-side hash of the grab id.
- Default pit only (no streamer prize-set editor, per-prize images, vessel image, custom sounds, pit background / safe-gap settings); the config type
  carries the pool and upstream's tease/duration/speed rules, but no settings UI exists.
- **Deferred control actions** (`UfoClawControlType` operations `show`, `hide`, `open`, `close`, `reset`, and `grab` from the action system): they drive
  a persisted, operator-run draw document (visible flag, current prize set, grab lifecycle with overlay acknowledgement and auto-hide) that needs bundle
  state transactions plus a sub-action/trigger surface Waddles does not expose to app bundles; a wagered grab is self-contained and needs none of it.
- **Deferred viewer-draw mode** (`!joinclaw`, open/close entries, `RemoveWinners`, removed-winner sets, the `UfoClawResult` trigger and its `%variables%`):
  the draw picks a viewer, not a payout, so it has no wager to settle; it would be a separate free-draw bundle on the action system.
- The overlay-completion acknowledgement (`POST /api/overlay/ufoclaw-complete`) has no counterpart: the result is settled before the animation plays.

## Build and test

```
bash scripts/test-superpenguin-bundle.sh ufoclaw          # unit tests + coverage gate
bash scripts/build-superpenguin-bundle.sh ufoclaw         # pinned Dockerfile -> /tmp/superpenguin-ufoclaw-out/superpenguin_ufoclaw.wasm
```

Host needs: `storage.kv`, `identity.resolve`, `chat.send:discord|twitch`, economy tool calls (`points_view`, `get_balance`), `economy.wager`, `overlay.media`.
Needs from the orchestrator (not edited here): a `core-bundles.yaml` entry and Makefile/CI wiring for `ufoclaw` (generic `test-superpenguin-%` / build targets already apply to the name).
wasm (stage-next-tools, not committed): 7,789,432 bytes, sha256 456da07347d5917998a86390eb98a05e8bbf2c86a3c26cebf87660bbb5dc70b0; exports process-stage, action-stage, streaming-lifecycle.
Tests: 66, 97.54% line coverage.
