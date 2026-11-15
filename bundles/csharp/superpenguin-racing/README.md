# superpenguin-racing

`!race amount|all|max` (aliases `!racing`, `!kart`, `!grandprix`): C# app bundle ported from PenguinTwitchBot's Racing game
(`Bot/Commands/Games/Racing/RacingGameService.cs`, `RacingMessageSettings.cs`, sub-actions RacingStartSession/Join/StopSession, overlay `wwwroot/racing.html` +
`js/racing.js`; github.com/Psychoboy/PenguinTwitchBot, MIT, used with permission). Built on the shared `../superpenguin-dropgame-core` library (Xoshiro256,
SeedDerivation, StakeParser, DropReplayGuard, OverlayEnvelope/OverlayPublisher, DropSupport); the single-shot `WageredGameBase` does not fit a multi-player
window, so this bundle uses the core's pieces directly. No core change was needed.

## Commands

| Command | Behaviour |
|---|---|
| `!race <amount\|all\|max>` | Join the open race with that stake (clamped to 500). The first joiner opens a 60 s join window. 5 s per-user cooldown (upstream's). Flag `waddles.command-superpenguin-racing` (default OFF). |
| `!race` | Shows the open window (racers, seconds left) or how to start one. |

Grid 2..12 racers (upstream's `MaxRacersPerHeat`), 3 laps. One stake per racer per race.

## Money: settlement after a delay

| Step | Primitive | Why |
|---|---|---|
| Join | ONE actor-bound `economy.wager(stake, 0)` per join | A wager with payout 0 is how the host ledger books a stake that is paid out later; it debits the stake atomically, is bound to the invoking actor, and the host de-duplicates it per (event, call). A "stake at join, payout later" shape is otherwise impossible: a wager settles in one call. |
| Payout | Economy service `adjust_balance` credit (`EconomyServiceClient.Credit`) in the settling event | Pot split by place: 2 racers 100 %; 3 racers 70/30; 4+ racers 60/30/10; rounding dust to first. The credits always sum to the pot exactly (zero-sum, nothing minted). |
| Void | Same credit as a refund of each stake | Fewer than 2 racers (`too_few`), or the window was picked up more than 15 min after its deadline (`late`). |

Exactly once per racer, even across a crash or a retry from a different event:

1. The result (places, payouts) is decided once and FROZEN in a result record (`race.<id>.res`) before any money moves; the race status goes `open -> settling -> done|void`.
2. Every credit uses a deterministic idempotency key bound to the race's OPENING message id (`EconomyServiceClient(tools, openMessageId, "race.pay"|"race.refund")`),
   so the key is the same whichever event runs the settlement; the host de-duplicates the write by that key. Progress (`Paid`) is saved after each credit, so a resumed
   settlement skips what is paid.
3. A failed settlement throws (denied/unavailable/backend/kv propagate, never an unpaid "success"); the due task is put back on the board and the current-race record is the
   backstop (a `settling` or overdue race is resumed on the next command). Insufficient funds / over the bet cap at join answer in chat, undo the join and release the cooldown.
4. A joiner who cannot be seated (window closed or frozen, grid full after the pre-check) is refunded once (`race.late-refund`, marker) and told so.
5. A redelivered join (same message id, platform, actor, command, raw arguments) finishes the same join: the replay record (race id, stake, race seed) is written BEFORE the
   wager, the wager is de-duplicated by the host, the roster slot is claimed once; same reply, no second ledger movement.

## Deadline: TickScheduler, tick-on-event (documented limits)

The join window deadline is a `WaddleSdk.Scheduling.TickScheduler` task (board `race`, id = race id). There is no timer or scheduler export for app bundles yet, so the deadline is
only noticed when the NEXT racing command reaches the bundle (`TakeDue()` at the top of every `transform`, exactly once across racing events via the per-task claim).
A bundle only receives events matching its own `consumes` filters (`!race`, `!racing`, `!kart`, `!grandprix`):

- A window nobody touches again is settled late. `LateByMillis` (and the record's deadline) decide: up to 15 minutes late the race still runs and pays; later it is VOIDED and every
  stake refunded, so a stale window can never surprise-play an animation or pay out an hour later.
- The result announcement and the overlay frame are produced by whichever event settles the window, and answer in that event's own channel/platform (a Twitch race can be announced
  in a Discord reply). No message is sent at the deadline itself.
- Settlement runs on the process stage only: economy, identity and tool calls are process-only (`docs/sdk-cs.md` section 6 "Needs from S0": economy in ACTION stage), so it cannot run from
  the action stage or a timer. Needs the host `scheduled-stage` export to settle at the deadline.
- Limits of the scheduler on `kv`: one board holds 64 tasks, and `Schedule` is a best-effort read-modify-write (kv has no CAS): two first joiners in the same instant can open two windows and one
  task can be lost; the lost window is only recovered if it is the current race (backstop). The race record lives 7 days; a window nobody triggers within 7 days cannot be refunded.
- A host-kv op budget of 64 per invocation applies; a 12-racer settlement plus a join is about 55 kv ops.

## Simulation (replaces upstream's in-browser physics)

`RacingSimulation.Run(seed, racers, config)` is pure and deterministic: ONE `Xoshiro256` seeded from the race seed, fixed draw order, only + - * / and floor. 50 ms step, 1000 units per lap,
18 s per clear lap. Per car: power 0.94-1.06 and form +-4 % redrawn every second; slipstream +6 % within 25 units behind another car; rubber-banding 0.25 (clamped +-10 %); 3 boost pads per lap
(+25 % for 1.5 s); 6 debris hazards per lap (55 % spinout); random spinout 1.2 %/s; a spinout crawls at 15 % speed for 1 s. Placement = finish time (interpolated inside the step), ties by lane;
a hard 10 min limit ranks the unfinished by distance as DNF (never reached by defaults). The seed is `SeedDerivation.Derive(raceId, platform, "race", "race", entropy)`, race id = hash of the opening
(message id, platform, actor); the entropy is ONE host-RNG value recorded in the race record before the first stake is taken. Re-running the sim from seed + config `racing.v1` + roster
reproduces the result byte for byte (golden tests pin two races and the hazard layout).

## Overlay

Pushed after the money moved: `join` (lobby frame per racer), `race` (per-lane keyframes, events, placements, payouts) and `void`; schema in `overlay/SCHEMA.md`, envelope in
`../superpenguin-dropgame-core/overlay/SCHEMA.md`. The widget is registered once a day. A failed push/registration is warn-logged and never alters a stake or payout.

### Browser source (interim) and host gap

Vendored upstream material (MIT) under `overlay/`: `racing.html`, `js/racing.js`, `media/racing_{boost,countdown,crash,end,engine,screech,start,win}.wav`, the upstream help page `help/drop-games/racing.md`
(11 files, `overlay/ASSETS.json`: sha256, size, upstream path, commit `df27ab1a`), shared files in `../superpenguin-dropgame-core/overlay/common/`. The host has no static-asset section for bundles and no sandboxed
bundle-widget runtime (#458), so nothing renders them yet: serve those directories statically and feed a player the pushed `extra` envelope. `racing.js` is vendored unmodified and runs the race in the
browser from upstream WebSocket messages (`race_lobby_start`/`race_start`), so it is NOT a drop-in player for this envelope; a thin player that interpolates `racers[].keyframes` is future overlay work
(Gemini owns svc-presentation). Not vendored (listed under `third_party_or_unclear`): upstream WebSocket scripts, reconnecting-websocket (own license), Google Fonts, runtime theme/emote images,
`tools/generate_racing_sounds.py` (build tooling).

## Deviations from upstream

- Stake-funded zero-sum pot instead of free-play point awards (1st/2nd/3rd/participation points; no participation payout); server-side simulation instead of the overlay being authoritative.
- Not ported (no host surface in this world): moderator commands (`!race start|derby|launch|stop`), the Start/Join/Stop sub-actions, solo `!timetrial` (free play: a zero stake is refused by the ledger, deferred),
  Destruction Derby / Elimination / multi-heat tournament modes, track layouts other than the perimeter ring, car collisions, emote/theme arguments, lobby queueing, `MaxLobbyRacers` unlimited.
- The first joiner opens the window (as upstream's auto-start on `!race`); lobby 60 s, 3 laps, max 12 are fixed defaults (the config type supports others, no streamer settings UI exists).

## Build and test

```
bash scripts/test-superpenguin-bundle.sh racing          # 75 tests, 98.9% line coverage
bash scripts/build-superpenguin-bundle.sh racing         # pinned Dockerfile -> /tmp/superpenguin-racing-out/superpenguin_racing.wasm
```

Host needs: `storage.kv`, `identity.resolve`, `chat.send:discord|twitch`, economy tool calls (`points_view`, `get_balance`, `adjust_balance`), `economy.wager`, `overlay.media`.
wasm (stage-next-tools, not committed): 8,358,275 bytes, sha256 79abe69b9bbf96d9b12de06ef5007d5c6a5ff3ce465dc800fbbed71834b19245; exports process-stage, action-stage, streaming-lifecycle.
