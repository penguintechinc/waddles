# echo bundle (`waddles.core.example.echo`)

`!echo <text>` -> echoes the text back (capped, never logged). Python/WASM component modeled on `bundles/python/boop`.

- Activation-gated only (no feature flag); declares no permissions.
- Stateless: no `storage.kv`, no DB, no egress.
- Logs carry only platform + reply shape -- never the message text, actor, or typed arguments.
- Test: `python3 -m pytest bundles/python/echo/tests`
