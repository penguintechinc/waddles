# example (JS/TS)

`waddles.core.example.echo` -- the Tier-1 JS/TS **SDK conformance example**: `!echo <text>`
replies with the text plus an incrementing counter (`hello (echo #3)`). Behaviorally identical
to the Python/Rust Tier-1 examples, authored against `@waddles/waddle-sdk-js`'s `defineBundle`
API. **Not published, not installed, not a shipped command bundle.**

| | |
|---|---|
| Role | WIT conformance example (`private: true`; not in `bundles/core-bundles.yaml`) |
| Command | `!echo <text>` (bare `!echo` with no text is not matched) |
| Verbs | none -- the whole argument is free text |
| PostHog flag | none (an example, never activated) |
| Platforms | Discord (`chat.message`, per `bundle.yaml`) |
| Permissions (V2) | `[]` declared -- note the code calls `kv.increment`/`log.info`, so a real manifest would declare `storage.kv` |
| State | `kv` key `echo_count` (counter, no TTL) |

```text
!echo hello    -> hello (echo #1)
!echo hello    -> hello (echo #2)
!echo          -> (no reply)
```

## Behavior notes

- `dispatch` is a no-op returning `{ ok: true, status: 200 }` -- the example has no action stage.
- Logs carry only the running count (`{ count }`) -- never the actor or the echoed text.
- The reply echoes the caller's text back into the same channel (that is the command's purpose).
- No automated tests or coverage gate exist for this example (TypeScript source, built against
  a prebuilt `sdk/waddle-sdk-js/dist`); it is exercised by the WIT conformance suite
  described in `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md`.
