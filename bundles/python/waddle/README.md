# waddle bundle (`waddles.core.example.waddle`)

`!waddle` -> a random penguin-themed reply. Python/WASM component modeled on `bundles/python/boop`.

- Gated behind PostHog flag `waddles.command-waddle` (default OFF); needs only the `flags.read` permission.
- Stateless: no `storage.kv`, no DB, no egress.
- Logs carry only platform + reply shape -- never the message text, actor, or typed arguments.
- Test: `python3 -m pytest bundles/python/waddle/tests`
