# connection_resolver

Shared per-send connection resolver client (edge-reshape Chunk 2). The public API is the contract svc-action (B1) and svc-ingest (B2) build on.

| Call | Endpoint | Token |
|---|---|---|
| `resolve(ConnectionRef)` | gRPC `ProviderConnectionService.ResolveConnection` (hub-api internal listener) | community-bound (`connections:resolve[:static]`), `BoundMachineJwtClient` |
| `list_bindings(provider, capability, after, limit)` | gRPC `ProviderConnectionService.ListBindings` | unbound `connections:list` |

- One hub call per `resolve`; no credential cache (only the bearer JWT is cached)
- `SecretString` zeroizes on drop; `Debug` is `[REDACTED]`; `ResolvedConnection` is not `Clone`
- Errors carry fixed codes only, never a hub body; `is_transient()` is true only for `Transient`/`CircuitOpen`
- One long-lived lazily re-dialling tonic channel per resolver; TLS pinned to `HUB_API_GRPC_CA_FILE`; plaintext `http://` endpoint accepted only when `environment == "alpha"`; must be built inside a tokio runtime
- gRPC statuses map to the closed `ResolveError` taxonomy by CODE only (`not_usable:<reason>` detail on `FAILED_PRECONDITION`); server text never reaches an error
- `test-support` feature: `test_support` = a real in-process tonic hub for tests (never in a service build)
- Contract-pinned to `hub_api/tests/fixtures/resolve_v2_*.json` (`tests/contract.rs`) and to `proto/waddles/hub/internal/v1/provider_connection.proto`
- Gates: `cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo deny check && cargo llvm-cov --fail-under-lines 90`
