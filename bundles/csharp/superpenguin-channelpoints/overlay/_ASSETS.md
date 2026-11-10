# Overlay asset provenance (superpenguin-channelpoints)

Source: github.com/Psychoboy/PenguinTwitchBot @ `df27ab1aaeaff0c6297f0e1fb8c3188b3be8328f` (MIT, Copyright (c) 2023 Psychoboy).

## Shipped (declared in `bundle.yaml` / `hub-manifest.yaml` `overlay_assets`, sha256 pinned)

| File | Origin | Note |
|---|---|---|
| `index.html` | layout idea of `wwwroot/alerts.html` | Rewritten: strict CSP, no inline script/style/handlers, no CDN scripts |
| `redeem.css` | transparent body + status pill idea of `wwwroot/alerts.html` | Rules rewritten, system fonts only |
| `redeem.js` | original | Server-sent-events client for the alert-box live channel; `textContent` only |

## Examined, NOT shipped

| Upstream file | Why |
|---|---|
| `wwwroot/js/alert.js` | Bound to the upstream WebSocket protocol, jQuery, clip/gif/audio-hook handling and ajax file probing; none of it maps to this bundle |
| `wwwroot/js/ws-client.js`, `ws-status.js`, `reconnecting-websocket/*` | Upstream transport; Waddles uses the presentation service's SSE channel |
| `wwwroot/gifs/*.gif`, `drinks.mp3`, `wwwroot/audio/*.mp3` | Binary media with undocumented provenance; no per-file license, so repository-level MIT is not treated as proof the author owns them |
| `wwwroot/css/open-iconic/*` | Third-party icon font (own FONT-LICENSE/ICON-LICENSE files); not needed |
| `https://fonts.googleapis.com/...` (Rubik) | Runtime third-party network resource; the overlay uses system fonts |
