# hello bundle (`waddles.core.example.hello`)

`!hello` (aliases `!hi`/`!hey`) -> a friendly greeting. Python/WASM component modeled on `bundles/python/boop`.

- Gated behind PostHog flag `waddles.command-hello` (default OFF); needs only the `flags.read` permission.
- Stateless: no `storage.kv`, no DB, no egress.
- Logs carry only platform + reply shape -- never the message text, actor, or typed arguments.
- Test: `python3 -m pytest bundles/python/hello/tests`
