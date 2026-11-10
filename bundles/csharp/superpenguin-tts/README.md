# superpenguin-tts (C#)

`!tts` / `!ttsvoice` command policy with a browser-source player, ported from PenguinTwitchBot's `TTS/TTSService.cs`
([Psychoboy/PenguinTwitchBot](https://github.com/Psychoboy/PenguinTwitchBot), MIT).
Author `github/psychoboy`, collection `superpenguintv`, MIT notice verbatim in `bundle.yaml` `notice`.
App bundle (policy lives here), WASI component on `stage-next-tools`, flag `waddles.command-superpenguin-tts` (default OFF).

**This bundle cannot run until the media service provides `tts_synthesize` and the host overlay capability exists** (see Host needs).
Until then it refuses loudly; it never falls back to another voice engine.

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!tts <message>` | anyone | speaks `{user:uuid} says <message>` in the user's voice, else a random registered voice; 30 s per-user cooldown (a repeat is dropped silently) |
| `!ttsvoice list` | anyone | registered voice ids |
| `!ttsvoice set <id>` / `clear` | anyone | pick one of the registered voices / go back to random |
| `!ttsvoice add <id>` / `remove <id>` | mod | register/unregister (ids `[A-Za-z0-9_.-]{1,48}`, max 50) |

Mod gate = the payload's `is_mod`/`is_broadcaster` literal `true`, fail-closed. Text is stripped of control characters and braces
(a braced token could smuggle a host `{user:uuid}` placeholder), whitespace is collapsed, empty text and text over 200 characters are refused.

## Synthesis contract (service tool, pinned by tests)

`tool.invoke` target `waddles.core.service.media`, tool `tts_synthesize`:

```
args   {"text": "<{user:uuid} says message>", "voice": "<voice id>"}      (idempotency key = hash(message id, play id))
result {"url": "https://..."}                                               (a bare {"handle"} is rejected: this app has no media.generate to resolve it)
```

| Outcome | Behavior |
|---|---|
| tool unavailable / not granted / forbidden | Error log `tts.synthesis_unavailable`, chat: "TTS is unavailable: the media service does not provide tts_synthesize.", cooldown released, nothing recorded |
| no voices registered | Error log `tts.refused`, chat: "TTS is unavailable: no TTS voices are registered." (no cooldown spent) |
| result without an https `url`, timeout, any other tool error | exception propagates (host retry / visible failure) |
| success | overlay alert `superpenguin.tts.play {url, voice, play_id}`, no chat reply |

## Overlay (browser source)

1. Serve `overlay/` over https. 2. OBS Browser Source URL
`<host>/index.html?origin=https://<presentation-host>&code=<overlay-code>&key=<view-key>` with "Control audio via OBS" on.
3. The page opens `GET {origin}/{code}/alert_box/live?key=...`, plays `amount.url` (https only) one clip at a time and ignores repeated `play_id`s.
Pushed on the existing `alert-box` surface (documented `AlertPayload`, open `amount` JSON); no svc-presentation change is needed.
Assets and what was not ported: `overlay/_ASSETS.md`.

## Host needs

| Need | Today |
|---|---|
| Media service tool `tts_synthesize` in the tool catalog (`bundles/rust/...` media service; args/result above) | **does not exist**; the grant `tool.call:waddles.core.service.media:tts_synthesize` cannot resolve and the bundle refuses |
| Media service renders `{user:uuid}` placeholders in `text` before synthesis (the PII boundary: a bundle never sees names) | not specified yet; without it the voice would read the placeholder |
| `overlay` host capability handler in svc-process/svc-action | **not implemented**: the executor answers `not-implemented`, so a play surfaces a `WaddleTransportException` |
| Overlay call quota | catalog quota for `overlay.media` is 1 call / 10 s; plays inside 10 s are refused by the host until it is raised |

## Not ported

Piper/Kokoro/Google synthesis, the TTS player/queue/file cleanup, the voices web page and per-voice sex/locale metadata
(duplicate infrastructure per the inventory rule). Upstream's `SayMessage` voice fallback to a locale-derived Kokoro voice is
replaced by the explicit "no voices registered" refusal. kv has no compare-and-swap: two simultaneous `!ttsvoice add`s can lose one registration.

## Build and test

```bash
make test-superpenguin-tts            # xUnit + 90% line-coverage floor
make build-superpenguin-tts-bundle    # pinned Dockerfile -> /tmp/superpenguin-tts-out/superpenguin_tts.wasm
python3 -I scripts/gen-superpenguin-manifests.py tts   # regenerate both manifests from manifest.src.yaml
```
