# Tenant key provisioning and the DEK broker (hub-api)

Phase 1 (T1 + T2) of `docs/superpowers/specs/2026-11-08-tenant-at-rest-encryption.md`: every tenant
owns an at-rest data-encryption key (DEK) before any data is written, and the data plane fetches
it over gRPC. No new crypto: provisioning calls the existing
`TenantEnvelopeService.ensure_tenant_key`; sealing uses the construction already in
`core/svc_ingest/src/identity_crypto.rs` (`hpke_seal_open`).

## Provisioning (T1)

| Trigger | Where | Behaviour |
|---|---|---|
| Tenant create | `POST /api/v1/superadmin/tenants` -> `services/tenant_key_provisioning.provision_new_tenant` | Key created right after the row insert. On failure the new tenant row is deleted and the call returns **503 `TENANT_KEY_UNAVAILABLE`** (no tenant without a key). The key store lives on the envelope runtime's own connection, so this is a compensating delete, not one SQL transaction; a failed delete is logged at ERROR and raised. |
| Startup | `app.py` `before_serving` -> `run_startup_provisioning` | Runs only after the schema bootstrap (which holds its own advisory lock) reaches READY, under advisory lock `PROVISION_LOCK_KEY` so replicas serialise. Creates the reserved `system` tenant row if absent (`free_tier_caps.ensure_system_tenant`, cap-exempt), ensures keys for the global (`is_global`) and `system` tenants, then backfills every other tenant. Retries 5x with backoff; then marks hub-api FAILED. `/ready` returns 503 (`provisioning_tenant_keys` / `failed`) until it succeeds. |
| Backfill | `make backfill-tenant-keys` (`python3 -m cli.backfill_tenant_keys`) | Idempotent; prints `{examined, created, already_present}`; exit 1 on any failure or zero tenants examined. |

Metrics: `waddles_tenant_key_provisioning_total{trigger,result}`,
`waddles_tenant_key_provisioning_duration_seconds{trigger}`; span `tenant_key.provision`.

## DEK broker (T2): `KeyService.GetTenantDek` (gRPC, TLS)

Proto: `proto/waddles/hub/internal/v1/key.proto`; served by hub-api's existing internal gRPC server
(`hub_api/grpc_internal`). REST was rejected: all intra-cluster calls are gRPC.

**Request** `GetTenantDekRequest`: `uint32 tenant_id` (numeric, >= 1; never a slug), `TenantDekPurpose purpose`
(`AT_REST=1` only -- `INGEST_STREAM` keys come from `GetStreamDek`; 0 is invalid), `uint32 dek_version`
(0 = active; non-zero = a specific, possibly retired version), `bytes caller_ephemeral_public_key`
(32 raw bytes, X25519, fresh per call).

**Response** `GetTenantDekResponse`: `bytes enc` (32, server one-time X25519 public key),
`bytes ciphertext` (48 = key + GCM tag), `uint32 dek_version`, `uint32 ttl_seconds`.

**One broker, not two.** `GetTenantDek` is served by the same `StreamDekBroker`
(`hub_api/services/stream_dek_broker.py`) as `GetStreamDek`: same authorization, sealing and audit
path, different key lineage (`at-rest` via `runtime.service`, `ingest-stream` via
`runtime.stream_service`). The raw DEK never leaves hub-api: the data plane receives the
HKDF-derived data subkey (`TenantEnvelopeService.data_key`), which is domain-separated per lineage,
so an at-rest key can never open stream ciphertext and vice versa.

**Seal** (identical to `GetStreamDek` and `hpke_seal_open`): `shared = X25519(client_secret, enc)`;
`key||nonce = HKDF-SHA256(salt none, ikm shared, info context)` (44 bytes: 32 + 12);
`key = AES-256-GCM-open(key, nonce, aad = context, ciphertext)`;
`context = "<service_id>|<tenant_id>|at-rest|<dek_version>"` where `service_id` is the caller's
service name (last SPIFFE path segment of the JWT `sub`, e.g. `svc-process`) and `dek_version` is the
version in the response.

**Auth**: scope `keys:tenant-dek:read` in the machine JWT (`authorization: Bearer`); the `sub` must be
a SPIFFE id in this deployment's env (`SPIFFE_ENV`) naming `svc-ingest`, `svc-gateway`,
`svc-process` or `svc-action`; a tenant-bound token may only ask for its own tenant, the `system`
tenant for any existing one. Helm grants the scope to the data-plane identities only
(`serviceJwt.identities`). A client deadline <= 5 s is required (server-wide rule).

**Status codes**: `UNAUTHENTICATED` (bad/missing token or scope), `PERMISSION_DENIED` (service not
allowed / tenant mismatch), `INVALID_ARGUMENT` (bad tenant, purpose or public key), `NOT_FOUND`
(no such key version), `UNAVAILABLE` (KMS down or revoked, key store unwired, or the audit write
failed -- retryable, no fallback key, key withheld), `RESOURCE_EXHAUSTED` (per-caller bucket
`GRPC_TENANT_DEK_RPS`=5 / `GRPC_TENANT_DEK_BURST`=30).

Every decision (issued or refused) writes `keystore.stream_dek_issuances` (alembic 0100/0110: caller
service, tenant id, lineage, version, outcome); a key is returned only after its `issued` row
commits. Key material, the caller's public key and sealed bytes are never logged. Metrics and spans
are the broker's (`waddles_stream_dek_requests`, `waddles_stream_dek_duration_ms`, span
`stream_dek.issue`) plus the gRPC OTel interceptor's per-method latency.
