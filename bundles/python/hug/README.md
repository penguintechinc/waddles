# hug bundle (`waddles.core.example.hug`)

`!hug [target]` -> a lighthearted reply that wraps people in comically wholesome hugs. Python/WASM component, modeled on `bundles/python/slap`.

- Gated behind PostHog flag `waddles.command-hug` (default OFF); needs the `flags.read` permission.
- Stateless: no `storage.kv`, no DB, no egress.
- Logs carry only platform + reply shape (`solo`/`targeted`/`usage`) -- never the raw message, actor, or target.
- Test: `python3 -m pytest bundles/python/hug/tests`
