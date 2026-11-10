# superpenguin-lastseen

C# port of PenguinTwitchBot `Misc/LastSeen.cs + Features/ViewerFeature.cs (last-seen stamp)` (MIT, Psychoboy; used with permission - see `bundle.yaml` `notice`).
Collection `superpenguintv`, author `github/psychoboy`. Gated by PostHog flag `waddles.command-superpenguin-lastseen` (default OFF).

`!lastseen [@mention]` reports how long ago a community member last chatted. Every chat message stamps the speaker.

## Commands

- `!lastseen` - your own previous activity
- `!lastseen @name` - that member's last activity

## Documented deviations and limits

- Targets are @mentions (UUIDs); the original looks up by Twitch username, which a bundle never sees.
- Stamps expire after 90 days; an expired stamp reads as never seen.
- Needs every chat message delivered (no command-prefix filter in the manifest).

## App/service boundary

The app owns command policy and formatting; shared capabilities come from SERVICE bundles through `tool.invoke` (never app to app).
No secrets, tokens or names enter the guest; replies name people with the host `{user:uuid}` placeholder.
State is community-scoped kv (keys use `.`, never `:`). Logs carry ids, counts and outcomes only.

## Build and test

- `make test-superpenguin-lastseen` - xUnit + 90% line-coverage floor
- `make build-superpenguin-lastseen-bundle` - pinned Dockerfile to a real `.wasm` component
- See `docs/sdk-cs.md` and `docs/superpenguin-port-inventory.md`.
