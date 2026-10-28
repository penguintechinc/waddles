# connection_resolver

Shared per-send connection resolver client (edge-reshape Chunk 2). The public API is the contract svc-action (B1) and svc-ingest (B2) build on.

| Call | Endpoint | Token |
|---|---|---|
| `resolve(ConnectionRef)` | `POST /api/v1/internal/communities/{c}/provider-connections/{id}/resolve` | community-bound (`connections:resolve[:static]`), `BoundMachineJwtClient` |
| `list_bindings(provider, capability, after, limit)` | `GET /api/v1/internal/provider-connections` | unbound `connections:list` |

- One hub call per `resolve`; no credential cache (only the bearer JWT is cached)
- `SecretString` zeroizes on drop; `Debug` is `[REDACTED]`; `ResolvedConnection` is not `Clone`
- Errors carry fixed codes only, never a hub body; `is_transient()` is true only for `Transient`/`CircuitOpen`
- Plaintext `http://` hub URL accepted only when `environment == "alpha"`
- Contract-pinned to `hub_api/tests/fixtures/resolve_v2_*.json` (`tests/contract.rs`)
- Gates: `cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo deny check && cargo llvm-cov --fail-under-lines 90`
