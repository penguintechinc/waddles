# UFO Claw overlay envelope

Pushed once per settled `!ufo` play (and again, byte-identical, on a redelivery) with
`host.Overlay.Push("ufoclaw", FullScreen, {"extra": <envelope>})`, AFTER the payout settled. A failed push is warn-logged and never
reverses or hides the payout. The fixed envelope fields (`v`, `game`, `kind`, `seed`, `actor`, `stake`, `payout`, `data`) are
documented in `../../superpenguin-dropgame-core/overlay/SCHEMA.md`; `kind` is `settled`, `seed` is the idempotency id (play a seed once).

## `data` (game specific)

| Field | Type | Meaning |
|---|---|---|
| `scene` | `{w,h}` | design canvas, 1920x1080 (upstream `ufo.html` canvas) |
| `rules` | object | `tease` (bool), `slipChancePercent` 0-100, `maxSuspenseDrops` 0-5, `durationSeconds` 3-120, `revealSeconds` 1-60, `movementSpeed` 0.25-3: upstream `UfoClawSettings`, same ranges |
| `pool` | array | the pit, one ball per drawable prize in config order: `label`, `multiplierPct` (payout percent of the stake, 0 = dud), `weight`, `color` (`#rrggbb`) |
| `winningIndex` | int | index into `pool` of the ball the claw finally lifts to the UFO (decided before any animation) |
| `label`, `multiplierPct`, `win` | string, int, bool | the winning prize; `win` is `multiplierPct > 0` |
| `slip` | bool | the claw first picks up decoys and drops them (upstream "tease"); false = one straight grab |
| `feints` | int[] | pool indexes of the decoys, in pick-up order; empty when `slip` is false; never contains `winningIndex` |

## Replacing upstream's server calls

Upstream `ufo.js` boots from `GET /api/overlay/ufoclaw-state` / the `ufoclaw` websocket topic and derives the tease from a hash of the
grab id in the browser (`shouldSlip`, `suspenseSequence`). Here the host pushes the envelope instead and `slip`/`feints` come from the
recorded seed (same distribution and the same ranges, different generator), so a client should use them verbatim in place of
`UfoClawScene.shouldSlip/suspenseSequence`. Upstream's `POST /api/overlay/ufoclaw-complete` acknowledgement has no counterpart: the
result is already settled and nothing waits for the animation.
Ball positions/timing stay the overlay's own pure function of `pool`, `winningIndex`, `slip`, `feints` and `rules` (`UfoClawScene.layout/frameAt`).
