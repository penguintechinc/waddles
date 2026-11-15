# superpenguin-deathcounter

C# port of PenguinTwitchBot `Misc/DeathCounters.cs` (MIT, Psychoboy; used with permission - see `bundle.yaml` `notice`).
Collection `superpenguintv`, author `github/psychoboy`. Gated by PostHog flag `waddles.command-superpenguin-deathcounter` (default OFF).

`!death` reports the streamer's death count; moderators and the broadcaster change it.

## Commands

- `!death` - show the counter
- `!death +` / `-` / `reset` / `set N` - moderators and the broadcaster
- `!death game NAME` - set the active game label (bare `!death game` clears it)

## Documented deviations and limits

- The original keys the counter on the live Twitch game; a bundle cannot read it, so a moderator sets the active game label.
- The reply says "The streamer" (no broadcaster identity reaches a bundle).
- No overlay text file / SignalR push; use the platform overlay service.

## App/service boundary

The app owns command policy and formatting; shared capabilities come from SERVICE bundles through `tool.invoke` (never app to app).
No secrets, tokens or names enter the guest; replies name people with the host `{user:uuid}` placeholder.
State is community-scoped kv (keys use `.`, never `:`). Logs carry ids, counts and outcomes only.

## Build and test

- `make test-superpenguin-deathcounter` - xUnit + 90% line-coverage floor
- `make build-superpenguin-deathcounter-bundle` - pinned Dockerfile to a real `.wasm` component
- See `docs/sdk-cs.md` and `docs/superpenguin-port-inventory.md`.
