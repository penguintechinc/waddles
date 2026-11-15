# superpenguin-alias

C# port of PenguinTwitchBot `Alias/Alias.cs + Alias/Handlers/*` (MIT, Psychoboy; used with permission - see `bundle.yaml` `notice`).
Collection `superpenguintv`, author `github/psychoboy`. Gated by PostHog flag `waddles.command-superpenguin-alias` (default OFF).

Short aliases for existing commands.

## Commands

- `!alias set NAME !target [args]` - moderators
- `!alias remove NAME` - moderators
- `!alias list`
- `!NAME args` - answers with the resolved `!target args`

## Documented deviations and limits

- No WIT world lets a bundle re-inject an event or call another app bundle, so an alias relays the resolved command text instead of executing it.
- Single-step resolution; a target may not be `!alias` or another alias (no loops).
- Needs every command message delivered (no command-prefix filter); a non-`!` message costs no kv operation.

## App/service boundary

The app owns command policy and formatting; shared capabilities come from SERVICE bundles through `tool.invoke` (never app to app).
No secrets, tokens or names enter the guest; replies name people with the host `{user:uuid}` placeholder.
State is community-scoped kv (keys use `.`, never `:`). Logs carry ids, counts and outcomes only.

## Build and test

- `make test-superpenguin-alias` - xUnit + 90% line-coverage floor
- `make build-superpenguin-alias-bundle` - pinned Dockerfile to a real `.wasm` component
- See `docs/sdk-cs.md` and `docs/superpenguin-port-inventory.md`.
