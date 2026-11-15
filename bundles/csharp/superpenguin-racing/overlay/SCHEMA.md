# Racing overlay envelopes (envelope v1)

Racing pushes the shared drop-game envelope (`../../superpenguin-dropgame-core/overlay/SCHEMA.md`) through
`host.Overlay.Push("racing", FullScreen, {"extra": {...}})`, after the money moved. Fixed fields (`v, game, kind, seed, actor, stake, payout, data`)
are the core's; this file documents the racing `kind`s and their `data`. A failed push is warn-logged (`racing.overlay_push_failed`) and
never reverses a stake or a payout. Widget id and asset prefix: `racing`, `drop-games/racing/`; registered once a day (`racing.overlay_register_failed` on failure).

Unlike the single-shot games, a race spans many chat events, so `seed` is the RACE seed (the same on every frame of one race) and the
idempotency rule differs: a client drops a `race`/`void` frame whose `seed` it already played, and a `join` frame by `(seed, data.lane)`.
No frame carries a name; `actor` is a community user uuid.

## kind `join` (one per seated racer, pushed right after their stake is taken)

`actor` = the racer, `stake` = their stake, `payout` = 0.

```
data: {"raceId": "<32 hex>", "phase": "lobby", "closesAt": <epoch ms>, "lane": <1-based grid position>, "racers": <seated so far>, "maxRacers": 12, "laps": 3}
```

## kind `race` (the finished race, pushed by whichever event settles the window)

`actor` = the winner, `stake` = the POT (sum of stakes), `payout` = the winner's payout. Positions are track units x10 (track length 1000
units per lap = 10000), times are milliseconds from the green flag, so the overlay needs no floating point.

```
data: {
  "raceId": "<32 hex>",
  "track":  {"type": "perimeter", "length": 1000, "laps": 3, "sampleMs": 1000, "totalMs": <int>,
             "debris": [<pos x10 within one lap>...], "boosts": [<pos x10 within one lap>...]},
  "pot":    <int>,
  "racers": [ {"lane": 0, "actor": "<uuid>", "color": "#RRGGBB", "stake": <int>, "powerMilli": <int>,
               "place": 1, "payout": <int>, "finishMs": <int, -1 if DNF>, "dnf": false,
               "keyframes": [[ms, pos10], ...]}, ... ],        // one per lane in grid order, ascending ms, ends at the finish
  "events": [[ms, lane, "spin"|"boost"], ...],                  // spinouts and boost-pad hits, for effects/sound
  "order":  [lane, ...]                                         // finishing order, first to last
}
```

The overlay interpolates each lane's `keyframes` (linear is exact enough at 1 s) and draws `debris`/`boosts` at those positions on every lap. Upstream's
`racing.js` computes the race itself in the browser and reports back; the vendored copy is shipped unmodified (see README), the envelope above is
what a thin player needs instead. Size estimate from the golden seeds (179 and 704 keyframes, 60 and 229 events): about 4 KB for 3 racers, about 16 KB for 12.

## kind `void` (a refunded window)

`actor` = the first refunded racer, `stake` = the pot, `payout` = that racer's refund.

```
data: {"raceId": "<32 hex>", "reason": "too_few" | "late", "refunds": [{"actor": "<uuid>", "amount": <int>}, ...]}
```

`too_few`: fewer than 2 racers. `late`: the window was picked up more than 15 minutes after its deadline (tick-on-event, see README).

## Reproducibility

`data` of a `race` frame is fully determined by `seed` + config `racing.v1` + the roster (racer count, in grid order): re-running
`RacingSimulation.Run(Xoshiro256(seed), racers, config)` is byte-identical. The race record in `kv` stores the opening message id, the platform, the
host-RNG entropy and the derived seed (`SeedDerivation.Derive(raceId, platform, "race", "race", entropy)`) before the first stake is taken.
