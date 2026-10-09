# highfive bundle (`waddles.core.example.highfive`)

`!highfive [target]` -> a lighthearted reply that gives people high fives. Python/WASM component, modeled on `bundles/python/slap`.

- Gated behind PostHog flag `waddles.command-highfive` (default OFF); needs the `flags.read` permission.
- Stateless: no `storage.kv`, no DB, no egress.
- Logs carry only platform + reply shape (`solo`/`targeted`/`usage`) -- never the raw message, actor, or target.
- Test: `python3 -m pytest bundles/python/highfive/tests`
