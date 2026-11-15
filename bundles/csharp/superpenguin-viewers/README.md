# superpenguin-viewers

C# port of PenguinTwitchBot `Features/ViewerFeature.cs (presence + lurk policy)` (MIT, Psychoboy; used with permission - see `bundle.yaml` `notice`).
Collection `superpenguintv`, author `github/psychoboy`. Gated by PostHog flag `waddles.command-superpenguin-viewers` (default OFF).

Active-viewer and lurker presence, as in the original's viewer feature.

## Commands

- `!lurk` - mark yourself a lurker (silent)
- `!viewers` - active viewer count

## Documented deviations and limits

- The viewer table, sub/mod/vip flags and follower lookups belong to the platform identity service and Twitch service tools; not duplicated.
- At most 200 viewers tracked; refreshes throttled to 30 s per viewer.
- Needs every chat message delivered (no command-prefix filter).

## App/service boundary

The app owns command policy and formatting; shared capabilities come from SERVICE bundles through `tool.invoke` (never app to app).
No secrets, tokens or names enter the guest; replies name people with the host `{user:uuid}` placeholder.
State is community-scoped kv (keys use `.`, never `:`). Logs carry ids, counts and outcomes only.

## Build and test

- `make test-superpenguin-viewers` - xUnit + 90% line-coverage floor
- `make build-superpenguin-viewers-bundle` - pinned Dockerfile to a real `.wasm` component
- See `docs/sdk-cs.md` and `docs/superpenguin-port-inventory.md`.
