# superpenguin-raidtracker

C# port of PenguinTwitchBot `Misc/RaidTracker.cs` (MIT, Psychoboy; used with permission - see `bundle.yaml` `notice`).
Collection `superpenguintv`, author `github/psychoboy`. Gated by PostHog flag `waddles.command-superpenguin-raidtracker` (default OFF).

Records incoming raids (`channel.raid`) per raider and lists the top raiders.

## Commands

- `!raids` - top five raiders
- `!raid TARGET` - broadcaster only: refused loudly (no platform raid tool exists)

## Documented deviations and limits

- Outgoing raids need a platform raid tool and Twitch credentials (host-side); not available, so refused loudly.
- The online-status poll, last game and prune UI need Twitch lookups and a scheduler.
- Only the 50 most recent raiders are kept; unattributable raids go to an aggregate tally.

## App/service boundary

The app owns command policy and formatting; shared capabilities come from SERVICE bundles through `tool.invoke` (never app to app).
No secrets, tokens or names enter the guest; replies name people with the host `{user:uuid}` placeholder.
State is community-scoped kv (keys use `.`, never `:`). Logs carry ids, counts and outcomes only.

## Build and test

- `make test-superpenguin-raidtracker` - xUnit + 90% line-coverage floor
- `make build-superpenguin-raidtracker-bundle` - pinned Dockerfile to a real `.wasm` component
- See `docs/sdk-cs.md` and `docs/superpenguin-port-inventory.md`.
