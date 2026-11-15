# superpenguin-top

C# port of PenguinTwitchBot `Misc/Top.cs + Misc/TopPointTypeSelection.cs` (MIT, Psychoboy; used with permission - see `bundle.yaml` `notice`).
Collection `superpenguintv`, author `github/psychoboy`. Gated by PostHog flag `waddles.command-superpenguin-top` (default OFF).

Leaderboards from the economy and reputation SERVICE bundles.

## Commands

- `!top5` / `!top10 [currency]` - richest members
- `!toprep` - reputation leaders

## Documented deviations and limits

- `!toptime`, `!loudest`, `!toptournaments`, `!toptickets` are not ported: no service exposes watch time, message counts or tournament standings. Nothing is faked.
- A community has one currency; the optional argument must name it.

## App/service boundary

The app owns command policy and formatting; shared capabilities come from SERVICE bundles through `tool.invoke` (never app to app).
No secrets, tokens or names enter the guest; replies name people with the host `{user:uuid}` placeholder.
State is community-scoped kv (keys use `.`, never `:`). Logs carry ids, counts and outcomes only.

## Build and test

- `make test-superpenguin-top` - xUnit + 90% line-coverage floor
- `make build-superpenguin-top-bundle` - pinned Dockerfile to a real `.wasm` component
- See `docs/sdk-cs.md` and `docs/superpenguin-port-inventory.md`.
