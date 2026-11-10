# Audio command overlay assets

Upstream: [Psychoboy/PenguinTwitchBot](https://github.com/Psychoboy/PenguinTwitchBot) (MIT, `Copyright (c) 2023 Psychoboy`),
revision `df27ab1aaeaff0c6297f0e1fb8c3188b3be8328f`. Every shipped file is declared with its sha256 and origin in
`bundle.yaml` / `hub-manifest.yaml` (`overlay_assets`), generated from `manifest.src.yaml`.

## Shipped (2 files, both written for Waddles)

| File | Origin | Notes |
|---|---|---|
| `index.html` | original | audio-only browser source, nothing is drawn |
| `audio-overlay.js` | original | SSE live channel, sequential playback, de-dupes by `play_id`, re-validates file names, builds URLs from its own `sounds` parameter |

Upstream plays sounds through its generic action queue (`PlaySoundType` sub-action) and its alert page, which are bot-internal
infrastructure rather than overlay assets, so there was no upstream overlay file to port for this module.

## Not shipped

| Upstream file | Why not |
|---|---|
| `wwwroot/audio/03_Food_Please.mp3` (24,411 B) | sample clip, recording of unknown origin / licence (the repository MIT notice does not cover third-party media) |
| `wwwroot/audio/AngryScottish.mp3` (21,929 B) | sample clip, recording of unknown origin / licence |
| `wwwroot/gifs/drinks.mp3` (66,993 B) | clip of unknown origin / licence (belongs to the drop-games module, not this bundle) |
| `wwwroot/alerts.html`, `wwwroot/js/alert.js` | generic follow/sub alert page tied to the bot's websocket; superseded by the svc-presentation `alert_box` surface |

Communities supply their own sound files and serve them from their own storage; the page reads them from its `sounds` base URL.
