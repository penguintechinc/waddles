# Overlay assets: superpenguin-giveaway

## Shipped (declared with sha256 and origin in `bundle.yaml` / `hub-manifest.yaml` `overlay_assets`)

| File | Origin | License |
|---|---|---|
| `index.html` | original (written for Waddles) | MIT (this bundle's `LICENSE`) |
| `giveaway.js` | original (written for Waddles) | MIT |

Psychoboy/PenguinTwitchBot has **no giveaway or raffle overlay**: the upstream giveaway UI is Razor viewer pages fed by a SignalR
hub (`Pages/`, `Bot/Hubs/MainHub`), which are bot web-app pages, not browser-source overlays, and depend on that app's hub and
authentication. Nothing was copied; the component is a new, minimal browser source.

## Examined upstream, not shipped

| Upstream item | Reason |
|---|---|
| `wwwroot/js/ws-client.js`, `reconnecting-websocket`, `signalr` | transport for the upstream bot's own websocket/SignalR; Waddles uses the presentation service's alert-box live channel |
| `wwwroot/alerts.html`, `wwwroot/js/alert.js` | generic upstream alert page tied to that websocket; not giveaway-specific |
| any binary media (mp3, gif, fonts) | none is used by giveaway/raffle; unclear-provenance media is never shipped |

Browser-source setup is in `README.md`.
