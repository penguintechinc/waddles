# superpenguin-quotes

C# port of PenguinTwitchBot `Misc/QuoteSystem.cs` (MIT, Psychoboy; used with permission - see `bundle.yaml` `notice`).
Collection `superpenguintv`, author `github/psychoboy`. Gated by PostHog flag `waddles.command-superpenguin-quotes` (default OFF).

A community quote book: random, by number, or by text.

## Commands

- `!quote [N|#N|text]`
- `!addquote TEXT` (alias `!quoteadd`) - moderators
- `!delquote N` (alias `!quotedel`) - moderators

## Documented deviations and limits

- Quote numbers are permanent (a delete leaves a tombstone); the original renumbers.
- No game name (Twitch lookup) and the author is a `{user:uuid}` placeholder.
- Text search covers the 48 newest quotes (host kv has no listing and a 64-op cap per invoke).
- The original's banned-user filter is replaced by moderator deletes.

## App/service boundary

The app owns command policy and formatting; shared capabilities come from SERVICE bundles through `tool.invoke` (never app to app).
No secrets, tokens or names enter the guest; replies name people with the host `{user:uuid}` placeholder.
State is community-scoped kv (keys use `.`, never `:`). Logs carry ids, counts and outcomes only.

## Build and test

- `make test-superpenguin-quotes` - xUnit + 90% line-coverage floor
- `make build-superpenguin-quotes-bundle` - pinned Dockerfile to a real `.wasm` component
- See `docs/sdk-cs.md` and `docs/superpenguin-port-inventory.md`.
