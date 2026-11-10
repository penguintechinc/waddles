# superpenguin-wheel (C#)

`!wheel` picker with a browser-source overlay, ported from PenguinTwitchBot's `WheelSpin/*`
(`WheelService.cs`, `WheelSpinComplete*`; [Psychoboy/PenguinTwitchBot](https://github.com/Psychoboy/PenguinTwitchBot), MIT).
Author `github/psychoboy`, collection `superpenguintv`, MIT notice verbatim in `bundle.yaml` `notice`.
App bundle (policy lives here), WASI component on `stage-next-tools`, flag `waddles.command-superpenguin-wheel` (default OFF).

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!wheel define <name> <label[*weight]\|...>` | mod | 2..24 items, weight 1..100 (default 1), label <= 24 chars; max 20 wheels |
| `!wheel delete <name>` / `!wheel list` | mod / anyone | |
| `!wheel message <name> <text>` | mod | winning message, `{label}` is replaced (default `Congratulations {label}!`) |
| `!wheel show <name>` / `!wheel hide` | mod | puts the wheel on / takes it off the overlay |
| `!wheel spin` | mod | draws the winner (weighted), records it, tells the overlay to spin |
| `!wheel result` | anyone | announces the winner once 10 s have passed on the host clock, else "still spinning" |
| `!wheel name open` / `!wheel join` (or `name join`) | mod / anyone | viewer wheel signup (humans only, once each, max 200) |
| `!wheel name show` / `spin` / `close` | mod | show the viewer wheel (needs >= 2, locks joining) / spin it / remove it |

Viewer names are shown on the overlay as `Entrant 1..N`; the winner is announced in chat with the host `{user:uuid}` placeholder.
Mod gate = the payload's `is_mod`/`is_broadcaster` literal `true`, fail-closed.

## Fairness and replay

The winning slice is drawn in the bundle (prefix sums over the integer weights, `System.Random.Shared`, unbiased) and recorded in
kv under the event's replay key BEFORE the overlay push; a redelivered event re-announces the same slice (the push carries a
`spin_id`, and the page ignores a repeated id). The browser only animates to the index it is given.

## Overlay (browser source)

1. Serve `overlay/` over https (any static host), or open it from disk in OBS.
2. Add an OBS Browser Source with URL
   `<host>/index.html?origin=https://<presentation-host>&code=<overlay-code>&key=<view-key>` (add `&backdrop=1` for the background art), 1920x1080.
3. The page opens `GET {origin}/{code}/alert_box/live?key=...` (svc-presentation SSE) and reacts to alert types
   `superpenguin.wheel.show|spin|hide`; any other alert is ignored.

The bundle pushes on the existing `alert-box` surface with `overlay.push` (documented `AlertPayload`: `alert_type`, open `amount`
JSON); no change to svc-presentation is needed. Assets and what was deliberately not shipped: `overlay/_ASSETS.md`.

## Host needs (fail loud, no fallback)

| Need | Today |
|---|---|
| `overlay` host capability handler in svc-process/svc-action | **not implemented**: the executor answers `not-implemented`, so `show`/`spin`/`hide` surface a `WaddleTransportException` (no silent chat-only fallback) |
| Overlay call quota | catalog quota for `overlay.media` is 1 call / 10 s; `show` then `spin` inside 10 s needs at least 2-3 calls per spin cycle, so the quota must be raised for this app (not worked around here) |
| Overlay widget registration (`register-widget`) | not used (standalone browser source; no quota spent) |
| Config JSON forwarding to apps | unused here; all state is kv |

## Not ported / deviations

- Upstream's `spinwheel` default-command trigger event after a result: no cross-app calls in Waddles; the `!wheel result` chat line is the output.
- The page cannot report the stop back (the live channel is read-only); the result is time-based on the host clock.
- Viewer wheel locks joining once shown (upstream re-showed the live wheel on each join).
- kv has no compare-and-swap: two simultaneous `!wheel join`s can lose one entry. No scheduler: nothing happens until a command arrives.

## Build and test

```bash
make test-superpenguin-wheel            # xUnit + 90% line-coverage floor
make build-superpenguin-wheel-bundle    # pinned Dockerfile -> /tmp/superpenguin-wheel-out/superpenguin_wheel.wasm
python3 -I scripts/gen-superpenguin-manifests.py wheel   # regenerate both manifests from manifest.src.yaml
```
