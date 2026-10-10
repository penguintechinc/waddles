# bot bundle (`waddles.core.example.bot`)

`!bot` -> static bot name/version/links. Python/WASM component modeled on `bundles/python/boop`.

- Gated behind PostHog flag `waddles.command-bot` (default OFF); needs only the `flags.read` permission.
- Stateless: no `storage.kv`, no DB, no egress.
- Logs carry only platform + reply shape -- never the message text, actor, or typed arguments.
- Test: `python3 -m pytest bundles/python/bot/tests`
