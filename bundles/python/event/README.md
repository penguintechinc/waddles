# event bundle (`waddles.core.example.event`)

`!event create <name> <when>` / `list` / `view <id>` / `remove <id>` and `!rsvp <id> yes|no` -- light community events + RSVPs in kv. NOT the beta-trio event-sync feature (shares nothing with it).

- Gated behind PostHog flag `waddles.command-event` (default OFF); permissions: `flags.read`, `storage.kv`.
- create/remove are mod/broadcaster-only (fail-closed when the platform sends no badge fields).
- RSVPs keyed by user UUID only; a non-UUID `event.actor` is refused loudly, never stored or logged.
- Logs never contain event names/times, actors, or RSVP answers.
- Test: `python3 -m pytest bundles/python/event/tests`
