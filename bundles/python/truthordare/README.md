# truthordare

`!truthordare [truth|dare]` replies with a random SFW truth or dare prompt (bare = random kind).
`truth`/`dare` are `CommandSpec` sub-modules parsed by `waddle_sdk.command.parse_command`; they
are always available (no `SubModuleGate`, since the bundle is stateless). Malformed input gets a
usage reply and is never echoed.

- Feature flag: `waddles.command-truthordare` (default OFF), checked after the command matches.
- Permissions: `flags.read`, `chat.send:discord`, `chat.send:twitch`.
- Logging is PII-free: only the platform name is logged.
- Tests: `cd bundles/python/truthordare && python3 -m pytest -q`.
