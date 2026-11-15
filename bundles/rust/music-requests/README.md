# waddles.core.service.music-requests

The single per-community song-request queue, as a `kind: service` bundle (Rust, world `stage-tools`, exports
`tool-provider`). The music app owns the user commands (`!sr`, `!queue`, `!songqueue`); the optional AI DJ layer reads and
adds through these tools and never owns a queue. Music works fully with the DJ absent or off.

Contract (tables, scopes, tools, DMCA-safe mode, `web_base_url`): [`docs/music-requests-service.md`](../../../docs/music-requests-service.md).

## Layout

| file | role |
|---|---|
| `src/logic.rs` | all policy, host-testable behind `DbV2Transport` / `Clock` / `Platform` seams |
| `src/lib.rs` | wasm-only adapters over the generated `db-v2`, `clock`, `context-v2` imports |
| `src/tests.rs` | in-memory `db-v2` fake (filters, unique keys, atomic batches with rollback and guards) |
| `bundle.yaml` / `hub-manifest.yaml` | identical `exported_tools` and `data.schema` (checked by `hub_api/tests/test_bundle_manifest_v2.py`) |

## Guarantees

- The actor comes only from the host-injected `__ctx`; no tool takes a user argument. The requester is stored (`user_ref`,
  anonymized on erasure) but never returned or logged.
- `source_url` is https-only (no userinfo, printable ASCII, <= 500 bytes). The `provider` is derived from the URL host
  against `SAFE_SOURCES`; no argument can set it.
- Caps (queue size, per-user queued) are guarded inside the same transaction as the insert; write tools need the host
  idempotency key; logs carry tool name and outcome code only.

## Verify

```bash
CARGO_TARGET_DIR=/mnt/ssd/cargo-target cargo test --locked
CARGO_TARGET_DIR=/mnt/ssd/cargo-target cargo clippy --locked --all-targets -- -D warnings
cargo deny check
cargo component build --release --target wasm32-wasip2 --locked   # wasm seam
```
