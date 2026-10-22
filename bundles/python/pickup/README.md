# pickup

`!pickup [@user]` replies with a cheesy SFW pickup line. The optional target is the pipeline's
`{user:<token>}` mention placeholder (`waddle_sdk.identity.mention_tokens`); the reply re-emits
the placeholder and the egress detokenizer renders the name. The bundle never sees, stores or
logs a raw handle, and needs no `identity.*` permission (it does not resolve the token). A raw
`@name` argument yields a usage reply and is not echoed.

- Feature flag: `waddles.command-pickup` (default OFF), checked after the command matches.
- Permissions: `flags.read`, `chat.send:discord`, `chat.send:twitch`.
- Tests: `cd bundles/python/pickup && python3 -m pytest -q`.
