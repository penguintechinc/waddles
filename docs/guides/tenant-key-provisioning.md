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

**Request** `GetTenantDekRequest`: `uint32 tenant_id` (numeric, >= 1), `TenantDekPurpose purpose`
(`AT_REST=1`, `INGEST_STREAM=2`; 0 invalid), `uint32 dek_version` (0 = active; non-zero = a specific,
possibly retired version), `bytes caller_ephemeral_public_key` (32 raw bytes, X25519, fresh per call).

**Response** `GetTenantDekResponse`: `bytes enc` (32, server one-time X25519 public key),
`bytes ciphertext` (48 = DEK + GCM tag), `uint32 dek_version`, `uint32 ttl_seconds`.

**Seal** (identical to `hpke_seal_open`): `shared = X25519(client_secret, enc)`;
`key||nonce = HKDF-SHA256(salt none, ikm shared, info context)` (44 bytes: 32 + 12);
`DEK = AES-256-GCM-open(key, nonce, aad = context, ciphertext)`;
`context = "<service_id>|<tenant_id>|<purpose_label>|1"` where `service_id` is the caller's service
name (last SPIFFE path segment of the JWT `sub`, e.g. `svc-process`), `purpose_label` is `at-rest` or
`ingest-stream`, and the trailing `1` is fixed.

**Auth**: scope `keys:tenant-dek:read` + `tenant == system` in the machine JWT (`authorization:
Bearer`), caller allow-list (`svc-ingest`, `svc-process`, `svc-action`; `KEYS_BROKER_ALLOWED_SERVICES`
overrides), and, when the listener runs mTLS (`GRPC_TLS_CLIENT_CA_PATH`), the client X.509-SVID URI SAN
must equal the JWT `sub`. Helm grants the scope to those three identities only
(`serviceJwt.identities`). A client deadline <= 5 s is required (server-wide rule).

**Status codes**: `UNAUTHENTICATED` (bad/missing token or scope), `PERMISSION_DENIED` (not
allow-listed / non-system tenant / SVID mismatch), `INVALID_ARGUMENT`, `NOT_FOUND` (no usable key;
the broker never provisions), `UNAVAILABLE` (KMS down or revoked: retryable, no fallback key),
`RESOURCE_EXHAUSTED` (per-caller bucket `GRPC_TENANT_DEK_RPS`=5 / `GRPC_TENANT_DEK_BURST`=30),
`INTERNAL` (audit write failed; key withheld).

Every release writes an `audit_log` row (`keys.tenant_dek.release`: caller, tenant id, version,
outcome) before the key is returned. Key material, the caller's public key and sealed bytes are
never logged or put in telemetry. Metrics: `waddles_tenant_dek_broker_requests_total{result}`,
`waddles_tenant_dek_broker_duration_seconds{result}`; span `tenant_dek.release`; the existing gRPC
OTel interceptor adds per-method latency.

`GetStreamDek` (#442) is unchanged and still `UNIMPLEMENTED`.
