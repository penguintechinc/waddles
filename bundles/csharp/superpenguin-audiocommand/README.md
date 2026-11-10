# superpenguin-audiocommand (C#)

`!sfx` sound commands with a browser-source player, ported from PenguinTwitchBot's `AudioCommand/*`
(`AudioCommands.cs`, `RunAudioCommandHandler.cs`; [Psychoboy/PenguinTwitchBot](https://github.com/Psychoboy/PenguinTwitchBot), MIT).
Author `github/psychoboy`, collection `superpenguintv`, MIT notice verbatim in `bundle.yaml` `notice`.
App bundle (policy lives here), WASI component on `stage-next-tools`, flag `waddles.command-superpenguin-audiocommand` (default OFF).

## Commands

| Command | Who | Behavior |
|---|---|---|
| `!sfx <name>` | anyone | pushes the sound to the overlay; silent in chat. Disabled sound and cooldown hits are dropped silently (as upstream); an unknown name gets a hint |
| `!sfx list` | anyone | enabled sound names (truncated to 400 chars) |
| `!sfx add <name> <file> [userCooldown] [globalCooldown] [volume]` | mod | cooldowns 0..86400 s (0 = none), volume 0..100 (default 100); max 200 sounds; same name updates |
| `!sfx remove\|enable\|disable <name>` | mod | |

Mod gate = the payload's `is_mod`/`is_broadcaster` literal `true`, fail-closed. File names: `[A-Za-z0-9_.-]{1,64}`, extension mp3/ogg/wav,
no leading dot, no `..`; the overlay receives only that name (never a path or URL). Upstream registered each sound as its own chat
command; consumes filters are static, so sounds play through the single root `!sfx <name>`.

## Overlay (browser source)

1. Serve `overlay/` over https and host your own sound files (the upstream sample clips are not shipped, see `overlay/_ASSETS.md`).
2. OBS Browser Source URL:
   `<host>/index.html?origin=https://<presentation-host>&code=<overlay-code>&key=<view-key>&sounds=https://<your-sound-host>/sounds`
   with "Control audio via OBS" on.
3. The page opens `GET {origin}/{code}/alert_box/live?key=...` and plays `{sounds}/{file}` for alert type
   `superpenguin.audiocommand.play` (one at a time, repeated `play_id`s ignored, queue capped at 20).

Pushed on the existing `alert-box` surface (documented `AlertPayload`, open `amount` JSON): no svc-presentation change is needed.

## Replay and cooldowns

The play is recorded under the event's replay key after the cooldowns are claimed and before the push, so a redelivered event
re-pushes the same `play_id` and claims no second cooldown. A user-cooldown hit does not spend the global slot, and a global-cooldown
hit hands the user's slot back. Cooldowns use atomic kv claims (`StateCooldown`).

## Host needs (fail loud, no fallback)

| Need | Today |
|---|---|
| `overlay` host capability handler in svc-process/svc-action | **not implemented**: the executor answers `not-implemented`, so a play surfaces a `WaddleTransportException` (nothing is silently dropped) |
| Overlay call quota | catalog quota for `overlay.media` is 1 call / 10 s; plays inside 10 s of each other are refused by the host until the quota is raised for this app |
| Config JSON forwarding to apps | unused; the registry is kv state |

## Not ported / deviations

- Upstream's global semaphore that serialised overlapping sounds: replaced by the page's sequential queue.
- Per-command permission ranks, shared-chat checks and the `SayCooldown` chat reply: no role data beyond the mod badge reaches bundles; cooldown hits are silent.
- Sound files and the web UI audio file picker: the community hosts its own files; this bundle only validates names.
- kv has no compare-and-swap: two simultaneous `!sfx add`s can lose one registration. No scheduler is needed.

## Build and test

```bash
make test-superpenguin-audiocommand            # xUnit + 90% line-coverage floor
make build-superpenguin-audiocommand-bundle    # pinned Dockerfile -> /tmp/superpenguin-audiocommand-out/superpenguin_audiocommand.wasm
python3 -I scripts/gen-superpenguin-manifests.py audiocommand   # regenerate both manifests from manifest.src.yaml
```
