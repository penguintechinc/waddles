# Wheel overlay assets

Upstream: [Psychoboy/PenguinTwitchBot](https://github.com/Psychoboy/PenguinTwitchBot) (MIT, `Copyright (c) 2023 Psychoboy`),
revision `df27ab1aaeaff0c6297f0e1fb8c3188b3be8328f`. Every shipped file is declared with its sha256 and origin in
`bundle.yaml` / `hub-manifest.yaml` (`overlay_assets`), generated from `manifest.src.yaml`.

## Shipped (7 files)

| File | Origin | Notes |
|---|---|---|
| `index.html` | `wwwroot/wheel.html`, adapted | no CDN scripts, no jQuery, no websocket client |
| `wheel-overlay.js` | `wwwroot/js/wheel.js`, adapted | live channel is SSE; WebAudio tick; textContent only |
| `wheel.css` | original | replaces the inline upstream styles |
| `img/wheel.svg` | `wwwroot/img/wheel.svg` | wheel overlay image, markup only (no scripts or external refs) |
| `img/wheelBackground.svg` | `wwwroot/img/wheelBackground.svg` | optional backdrop (`?backdrop=1`) |
| `vendor/spin-wheel-iife.js` | npm `spin-wheel@5.0.2`, MIT (CrazyTim) | sha256 `f02a1704248a9b069f2624b2770d0d67dcf81a073d43bfb270bd72e3b528706c` |
| `vendor/spin-wheel.LICENSE.txt` | spin-wheel `LICENSE.md` | sha256 `7bdaf7726a7cfc61ea37169deb9e6a4e384a4c010eb48bff1a4ee29fb3b89598` |

## Not shipped (provenance unclear; the repository MIT notice does not state that third-party media inside it is MIT)

| Upstream file | Why not | Replacement |
|---|---|---|
| `wwwroot/sfx/wheel.ogg` (5,536 B) | binary sound, authorship unknown | 25 ms WebAudio square-wave tick synthesized in `wheel-overlay.js` |
| `wwwroot/js/ws-client.js`, `ws-status.js`, `reconnecting-websocket/*` | bot websocket transport, not applicable | SSE `EventSource` |
| soundjs (`code.createjs.com/1.0.0/soundjs.min.js`) | third-party CDN dependency | not used |
| jQuery CDN script | third-party CDN dependency | not used |
| Google Fonts `Rubik` stylesheet | third-party network resource | system font stack (`Rubik` used only if installed) |
