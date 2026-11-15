# superpenguin-dailycounter

C# port of PenguinTwitchBot `Misc/DailyCounter.cs` (MIT, Psychoboy; used with permission - see `bundle.yaml` `notice`).
Collection `superpenguintv`, author `github/psychoboy`. Gated by PostHog flag `waddles.command-superpenguin-dailycounter` (default OFF).

A streamer-facing daily counter with a `{value}` / `{nextgoal}` format, as in the original.

## Commands

- `!dailycounter` - show the rendered text
- `!setdailycountertext FORMAT` - broadcaster
- `!setdailyvalue N` - broadcaster
- `!dailyadd [N]` - moderators (stand-in for sub events)

## Documented deviations and limits

- The original increments on subscription events and writes Data/DailyCounter.txt; bundles get chat only and have no file system, so `!dailyadd` stands in until a subscription event contract exists.
- No compare-and-swap in kv: two simultaneous `!dailyadd` messages can lose one update.

## App/service boundary

The app owns command policy and formatting; shared capabilities come from SERVICE bundles through `tool.invoke` (never app to app).
No secrets, tokens or names enter the guest; replies name people with the host `{user:uuid}` placeholder.
State is community-scoped kv (keys use `.`, never `:`). Logs carry ids, counts and outcomes only.

## Build and test

- `make test-superpenguin-dailycounter` - xUnit + 90% line-coverage floor
- `make build-superpenguin-dailycounter-bundle` - pinned Dockerfile to a real `.wasm` component
- See `docs/sdk-cs.md` and `docs/superpenguin-port-inventory.md`.
