# Golf overlay envelope data (v1)

The fixed envelope (`v, game, kind, seed, actor, stake, payout, data`) is documented in
`../../superpenguin-dropgame-core/overlay/SCHEMA.md`. For Golf: `game = "golf"`, `kind = "settled"`, widget id `golf`
(`Golf Chip and Putt`, FullScreen, 1920x1080 default). This file documents `data`.

One envelope per settled `!golf` play: one wagered single-stroke tee shot, already paid. Everything is an integer or a string/bool:
course fractions, speeds and heights are integers in 1/10000 units (so `8400` = 0.84 of the course), so the payload carries no
floating point and is byte-identical for the same seed.

```json
{"extra":{"v":1,"game":"golf","kind":"settled","seed":"<16 hex>","actor":"<community user uuid>","stake":100,"payout":195,
 "data":{
  "course":{"strip":"Bottom","slide":"LeftToRight","tee":500,"target":8400,"cupRadius":140,"greenRadius":350,"maxScore":100,"yardsPerCourse":100},
  "condition":"FastLinks","conditionLabel":"Fast Links","club":"Driver","strokeNumber":1,"maxStrokes":1,
  "power":79,"effectivePower":7912,"launchSpeed":6004,
  "rest":8675,"distanceFromPin":275,"sunk":false,"lipOut":true,"win":true,"holeInOne":false,
  "score":36,"multiplierPct":195,"totalTicks":210,
  "keyframes":[[0,500,5000,0],[...],[210,8675,4400,0]]}}}
```

| `data` field | Meaning |
|---|---|
| `course` | Static layout: `tee` start and `target` pin position along the strip, `cupRadius` (sunk zone), `greenRadius` (scoring zone), `maxScore` (score for a sunk ball), `yardsPerCourse` (distance in yards = fraction x this). Fixed: bottom strip, left to right (upstream's defaults). |
| `condition` / `conditionLabel` | Green condition drawn from the seed: `StandardGreen`, `FastLinks` (drag x0.78), `HeavyDew` (x1.28), `CrossBreeze` (x1.05 + sideways push). |
| `club`, `strokeNumber`, `maxStrokes` | Always `Driver`, `1`, `1`: single-stroke mode (see README "Deviations"). |
| `power` | The swing power used, 1-100 (the viewer's, or upstream's default draw 65-85). |
| `effectivePower` | `power` after the +/-3% variance, as power/100 x 10000 (`7912` = 79.12). |
| `launchSpeed` | Initial launch speed x 10000. |
| `rest` | Where the ball stopped along the strip (the cup position `target` when sunk). |
| `distanceFromPin` | Distance from the pin at rest (0 when sunk). |
| `sunk` / `holeInOne` | Ball dropped in the cup (single stroke, so the same). |
| `lipOut` | The ball rolled over the cup too fast, caught the rim and deflected past it. |
| `win` | Finished on the green or in the cup (score above 0). |
| `score` | Closest-to-the-pin score (upstream's `calculateGolfScore`, no stroke penalty): sunk = `maxScore`, on the green 1..`maxScore` by distance, off the green 0. |
| `multiplierPct` | Payout as a percent of the stake (`300` = 3x), see README "Money". |
| `totalTicks` | Shot duration in 60 Hz ticks. |
| `keyframes` | `[tick, axis, lateral, height]` rows: 6 for an airborne shot (tee, apex 1, bounce 1, apex 2, bounce 2, rest), 2 for a very short ground roll. `axis` runs along the strip, `lateral` across it (0.5 = centre line), `height` is the ball above the turf. Interpolate between rows. |

## Mapping onto the vendored `golf.js`

Upstream's page is client-authoritative: it rolls its own dice from a `golf_shot` message `{dropToken, power, strokeNumber, condition,
targetDistancePercent, powerVariancePercent, ...}`. An adapter that feeds it MUST NOT let it re-roll the result: the server result
above is the settled one (money was paid on it). Use `seed` as `dropToken`, `power` as `power`, `condition` as `condition`,
`course.target / 100` as `targetDistancePercent`, `0` as `powerVariancePercent` and then drive the ball along `keyframes` instead of
its own physics, or render `rest`/`sunk`/`lipOut`/`score` directly. No host renders either today (see README "Host gap").
