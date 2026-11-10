# TTS overlay assets

Upstream: [Psychoboy/PenguinTwitchBot](https://github.com/Psychoboy/PenguinTwitchBot) (MIT, `Copyright (c) 2023 Psychoboy`),
revision `df27ab1aaeaff0c6297f0e1fb8c3188b3be8328f`. Every shipped file is declared with its sha256 and origin in
`bundle.yaml` / `hub-manifest.yaml` (`overlay_assets`), generated from `manifest.src.yaml`.

## Shipped (2 files, both written for Waddles)

| File | Origin | Notes |
|---|---|---|
| `index.html` | original | audio-only browser source, nothing is drawn |
| `tts-overlay.js` | original | SSE live channel, plays `amount.url` (https only), sequential, de-dupes by `play_id` |

The upstream repository has no TTS web asset (a search of its `wwwroot` for `tts` finds nothing): its player is a bot-side
service (`TTSPlayerService`) that plays through the bot's own action queue. Nothing was left unported for lack of a licence.

## Not shipped

| Upstream item | Why not |
|---|---|
| Piper / Kokoro / Google synthesis (`PiperService.cs`, `TTSService.cs` voice backends) and the NuGet-bundled Kokoro voice files | duplicate synthesis infrastructure; synthesis is the media service's job (`tts_synthesize`) |
| `TTSPlayerService`, `TtsCleanupJob`, TTS file storage | duplicate player/queue/cleanup infrastructure |
