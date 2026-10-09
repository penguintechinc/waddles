# boop bundle (`waddles.core.example.boop`)

`!boop [target]` -> a lighthearted reply that boops people on the nose. Python/WASM component, modeled on `bundles/python/slap`.

- Gated behind PostHog flag `waddles.command-boop` (default OFF); needs the `flags.read` permission.
- Stateless: no `storage.kv`, no DB, no egress.
- Logs carry only platform + reply shape (`solo`/`targeted`/`usage`) -- never the raw message, actor, or target.
- Test: `python3 -m pytest bundles/python/boop/tests`
