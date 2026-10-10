# time bundle (`waddles.core.example.time`)

`!time` -> current UTC time from the host clock capability. Python/WASM component modeled on `bundles/python/boop`.

- Gated behind PostHog flag `waddles.command-time` (default OFF); needs only the `flags.read` permission.
- Stateless: no `storage.kv`, no DB, no egress.
- Logs carry only platform + reply shape -- never the message text, actor, or typed arguments.
- Test: `python3 -m pytest bundles/python/time/tests`
