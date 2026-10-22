# reverse bundle (`waddles.core.example.reverse`)

`!reverse <text>` -> reverses the text (hello -> olleh). Python/WASM component modeled on `bundles/python/echo`.

- Deterministic pure text transform; input capped at 200 characters; empty input -> usage reply.
- Feature-flag gated (`waddles.command-reverse`, default OFF), checked after the command matches.
- Permissions: `flags.read`, `chat.send:discord`, `chat.send:twitch` only. Stateless: no `storage.kv`, no DB, no egress, no AI.
- Replies that would start with `/`, `.` or `!` are refused (stops a caller making the bot run chat commands).
- Logs carry only platform, shape, input length and community id -- never the message text or actor.
- Test: `python3 -m pytest bundles/python/reverse/tests`
