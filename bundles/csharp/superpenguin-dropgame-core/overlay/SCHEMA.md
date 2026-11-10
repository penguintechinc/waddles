# Drop-game overlay envelope (v1)

Every SuperPenguinTV drop game pushes ONE structured payload per settled play, after the payout is settled:

```
host.Overlay.Push(widgetId, OverlaySurface.FullScreen, json)
json = {"extra": { ...envelope... }}
```

`extra` is `core/overlay_schema`'s `OverlayPush.extra`, the bundle-widget escape hatch: the host validates its size, never its shape.
Built by `OverlayEnvelope.Build` (`superpenguin-dropgame-core/OverlayEnvelope.cs`). Nothing else is ever pushed; there is no HTML/JS in
a push and no PII (the actor is a community user UUID, display names are rendered by the host/overlay from that UUID).

## Fixed fields

| Field | Type | Meaning |
|---|---|---|
| `v` | int | Envelope version (`1`). Bumped only on an incompatible change to these fixed fields. |
| `game` | string | Game id: `plinko`, `balloon`, ... Also the widget id and the asset prefix `drop-games/<game>/`. |
| `kind` | string | `settled` today (one play, already paid). Reserved for game-specific kinds (`session_start`, ...). |
| `seed` | string | 16 lower-case hex chars: the play's recorded seed. The envelope's idempotency id: a client plays a seed once and ignores a repeat (a redelivery re-pushes the same envelope). |
| `actor` | string | Community user UUID of the player. |
| `stake` | int | Amount wagered (after clamping to max bet). |
| `payout` | int | Amount paid back (0 = loss). Net change = `payout - stake`. |
| `data` | object | Game-specific result, documented per bundle in `<bundle>/overlay/SCHEMA.md` or the bundle README. |

Property order is fixed: `v, game, kind, seed, actor, stake, payout, data`.

## Reproducibility

`seed` + the game's config revision (`cfg` in the replay record, e.g. `plinko.v1`) fully determine `data` and `payout`: re-running the
game's pure simulation (`Xoshiro256(seed)`, xoshiro256** seeded through splitmix64) gives byte-identical output. The seed is
`SeedDerivation.Derive(messageId, platform, actorUuid, command, entropy)` where `entropy` is ONE host-RNG value recorded in the replay
record BEFORE the money call.

## Widget descriptor (registered once per bundle per day)

`OverlayEnvelope.Descriptor(game, w, h)`:

```json
{"size":{"w":1920,"h":1080},"anchor":"center","bindings":["extra"],"envelope_version":1,"asset_prefix":"drop-games/<game>/"}
```

Size/anchor are defaults for the designer palette, `bindings` names the data-binding field (`extra`), `asset_prefix` is where the bundle's
vendored overlay files (`<bundle>/overlay/`) are expected to be served.

## Failure semantics

A failed `RegisterWidget` or `Push` is logged at warn (`<game>.overlay_register_failed` / `<game>.overlay_push_failed`, with the
transport `code`) and never reverses, hides or alters the settled payout or the chat reply.

## Host gap (verified, not worked around)

The host has no static-asset section for bundles and nothing renders bundle JS yet (#458 sandboxed widgets are not built); `overlay` only
registers declarative widget descriptors. The vendored upstream overlay pages are therefore shipped in-repo and documented as an interim
"browser source": serve `<bundle>/overlay/` plus `superpenguin-dropgame-core/overlay/common/` statically and feed the page the pushed `extra`
envelope.

## Vendored common files

`common/js/drop-games-common.js`, `common/js/drop-games-audio.js`, `common/media/round_start.wav`, `common/media/round_end.wav`: upstream
shared overlay runtime and default session sounds (MIT, Psychoboy/PenguinTwitchBot); see `common/ASSETS.json` (sha256, size, upstream path/commit).
