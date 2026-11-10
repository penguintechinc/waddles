# superpenguin-timers

C# port of PenguinTwitchBot `Misc/AutoTimers.cs + Models/Timers/TimerGroup.cs` (MIT, Psychoboy; used with permission - see `bundle.yaml` `notice`).
Collection `superpenguintv`, author `github/psychoboy`. Gated by PostHog flag `waddles.command-superpenguin-timers` (default OFF).

Announcement timer groups with interval ranges, minimum chat messages, shuffle and repeat.

## Commands

- `!timer create NAME [minSec maxSec minMessages] [once]`
- `!timer addline NAME TEXT` / `delline NAME N`
- `!timer shuffle NAME on|off`
- `!timer enable|disable|delete|run NAME`
- `!timer list`

## Documented deviations and limits

- NO SCHEDULER: no WIT world gives an app bundle a timer, so due groups are checked on every chat message (tick-on-event, like the SDK TickScheduler). A quiet channel announces late; one announcement per event; it goes to the channel of the triggering message.
- The online-only flag and stream-start counter reset need the stream-online signal that no world exposes.
- Announcements are plain chat lines, not the original's action engine.
- Needs every chat message delivered (no command-prefix filter).

## App/service boundary

The app owns command policy and formatting; shared capabilities come from SERVICE bundles through `tool.invoke` (never app to app).
No secrets, tokens or names enter the guest; replies name people with the host `{user:uuid}` placeholder.
State is community-scoped kv (keys use `.`, never `:`). Logs carry ids, counts and outcomes only.

## Build and test

- `make test-superpenguin-timers` - xUnit + 90% line-coverage floor
- `make build-superpenguin-timers-bundle` - pinned Dockerfile to a real `.wasm` component
- See `docs/sdk-cs.md` and `docs/superpenguin-port-inventory.md`.
