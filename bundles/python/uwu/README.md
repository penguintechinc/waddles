# uwu bundle (`waddles.core.example.uwu`)

`!uwu <text>` -> uwu-ifies the text (r/l -> w, 'nya' endings, a deterministic uwu/owo flourish). Python/WASM component modeled on `bundles/python/echo`.

- Deterministic pure text transform; input capped at 200 characters; empty input -> usage reply.
- Activation-gated only (installed -> enabled -> activated); no feature flag.
- Permissions: `chat.send:discord`, `chat.send:twitch` only. Stateless: no `storage.kv`, no DB, no egress, no AI.
- Replies that would start with `/`, `.` or `!` are refused (stops a caller making the bot run chat commands).
- Logs carry only platform, shape, input length and community id -- never the message text or actor.
- Test: `python3 -m pytest bundles/python/uwu/tests`

## Output safety

The echoed text is user-typed, so it passes through `waddle_sdk.safe_text.sanitize_user_text` last (after the transform): forged `{user:<token>}` placeholders are escaped and mass-mention markup is defanged. The transform destroys any mention placeholder, so no `allow_tokens` are passed.
