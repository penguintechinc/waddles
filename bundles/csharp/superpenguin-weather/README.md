# superpenguin-weather

C# port of PenguinTwitchBot `Misc/Weather.cs` (MIT, Psychoboy; used with permission - see `bundle.yaml` `notice`).
Collection `superpenguintv`, author `github/psychoboy`. Gated by PostHog flag `waddles.command-superpenguin-weather` (default OFF).

`!weather [location]` from WeatherAPI.com. No weather service bundle exists, so the app calls the provider through the host http capability.

## Commands

- `!weather London`
- `!weather` - uses the bundle config `default_location` when the host forwards config

## Documented deviations and limits

- Operator must provision the `WEATHER_API_KEY` secret; the key is injected host-side into the `key` header and never enters the guest.
- The host does not forward app config yet, so a location argument is normally required.

## App/service boundary

The app owns command policy and formatting; shared capabilities come from SERVICE bundles through `tool.invoke` (never app to app).
No secrets, tokens or names enter the guest; replies name people with the host `{user:uuid}` placeholder.
State is community-scoped kv (keys use `.`, never `:`). Logs carry ids, counts and outcomes only.

## Build and test

- `make test-superpenguin-weather` - xUnit + 90% line-coverage floor
- `make build-superpenguin-weather-bundle` - pinned Dockerfile to a real `.wasm` component
- See `docs/sdk-cs.md` and `docs/superpenguin-port-inventory.md`.
