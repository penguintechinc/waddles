# emojify bundle (`waddles.core.example.emojify`)

`!emojify <text>` -> sprinkles a relevant emoji after matching words (keyword map, fallback sparkle). Python/WASM component modeled on `bundles/python/echo`.

- Deterministic pure text transform; input capped at 200 characters; empty input -> usage reply.
- Feature-flag gated (`waddles.command-emojify`, default OFF), checked after the command matches.
- Permissions: `flags.read`, `chat.send:discord`, `chat.send:twitch` only. Stateless: no `storage.kv`, no DB, no egress, no AI.
- Replies that would start with `/`, `.` or `!` are refused (stops a caller making the bot run chat commands).
- Logs carry only platform, shape, input length and community id -- never the message text or actor.
- Test: `python3 -m pytest bundles/python/emojify/tests`
