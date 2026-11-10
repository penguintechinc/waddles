# Tenant At-Rest Encryption: Implementation Plan

Spec: `docs/superpowers/specs/2026-11-08-tenant-at-rest-encryption.md`. Transit TLS is covered by the separate transit audit (out of scope here). Dispatch: Sonnet implementers, each in its own worktree off `release/v3.0.X` (disjoint file sets; own `CARGO_TARGET_DIR` subdir on the shared bounded cache). Every task ships docs, README, unit + regression + real-path integration tests, OTel, and is flag-gated (`waddles.disable-tenant-at-rest-encryption`, unseen = ON). Review gate: architect plan-approved before dispatch.

## Phase 1 (Monday alpha slice)

| ID | Task | Owns (only these files) | Depends | Parallel |
|---|---|---|---|---|
| T1 | hub-api provisioning: call `ensure_tenant_key` in tenant create (same unit of work, fail the create on error); startup hook ensuring tenant 0 + `system` (idempotent, after bootstrap); `scripts/` backfill for existing tenants + make target | `hub_api/blueprints/v1/superadmin.py`, `hub_api/services/free_tier_caps.py`, `hub_api/app.py` (startup block only), new `hub_api/services/tenant_key_provisioning.py`, `hub_api/tests/test_tenant_key_provisioning.py` | none | yes |
| T2 | hub-api broker endpoint `POST /api/v1/internal/keys/tenant-dek` (machine JWT, per-service tenant allow-list, ECDH-sealed per `HubApiDekProvider` wire, audit, OTel, no key in logs) + `dek-rotated` publish | new `hub_api/blueprints/v1/internal_keys.py`, `hub_api/tests/test_internal_keys.py`, blueprint registration line in `hub_api/app.py` (coordinate with T1: T2 owns only the register line) | none | yes |
| T3 | `core/bundle_host_crypto` crate: lift AEAD/AAD/HKDF/`DekProvider`/`TtlCachedDekProvider`/`HubApiDekProvider` from `core/svc_ingest/src/identity_crypto.rs`; envelope header with magic + dek_version; `seal/open/blind_index`; golden vectors cross-checked against `hub_api/services/envelope/crypto.py` (Python/Rust parity); metrics; `deny.toml`; CI workflow | new `core/bundle_host_crypto/**`, `.github/workflows/rust-bundle-host-crypto.yml` | none | yes |
| T4 | kv value encryption: `KvHost` takes a crypto handle; seal on set/cas, open on get/get_versioned/list values; legacy-plaintext tolerant read; `key_unavailable` typed error; CAS digest over ciphertext; flag | `core/bundle_host_kv/src/{backend,lib}.rs`, `core/bundle_host_kv/Cargo.toml`, `core/bundle_host_kv/tests/**` | T3 API (stub trait agreed up front) | after T3 merges or against its published trait |
| T5 | wire crypto into stages: construct provider + pass to `KvHost` in svc_process and svc_action; DEK invalidation subscription; health/readiness reports key-provider state | `core/svc_process/src/{capabilities,lib,config}.rs` (kv construction + config only), `core/svc_action/src/{capabilities,lib,config}.rs` (same) | T3, T4 | after T4 |
| T6 | e2e real-path test: two tenants, same app+key; assert ciphertext in Valkey (raw `GET`), cross-tenant swap fails to open, DEK unavailable fails closed, rotation reads old + new, plaintext legacy value still readable; telemetry counts asserted (log ≥1, metric ≥1, histogram ≥1) | new `core/connector_e2e/tests/tenant_at_rest_kv.rs` | T1-T5 | last |
| T7 | docs: `docs/bundle-storage-v2.md` encryption section (coordinate: owned by T7 only after #1139/#1151 merge; until then a new `docs/tenant-at-rest-encryption.md`), CHANGELOG, ops runbook (key unavailable, rotation, kill-switch) | new `docs/tenant-at-rest-encryption.md`, `CHANGELOG.md` | none | yes |

Phase-1 ship gate: T1+T2+T3 green first (keys exist), then T4+T5, then T6. If T4/T5 slip, ship T1-T3 only (provisioning is harmless alone).

## Phase 2 (after #1139/#1151 merge)

| ID | Task | Owns | Depends |
|---|---|---|---|
| T8 | db v2 column modes `sealed/eq/plain`, `__bidx` columns, planner rule updates, `row_uuid`, registry `mode`, hub-api approval-time validation (low-entropy `eq` rejected unless acked) | `core/bundle_host_db/src/v2/**`, `typed.rs`, `alembic/versions/0083_bundle_data_encryption.py`, `hub_api/services/bundle_data_v2.py`, `hub_api/services/bundle_data_provisioner.py` | T3, #1139, #1151 |
| T9 | numeric `KvScope` (legacy keys are slug/name-keyed: resolver handles names + numeric-looking segments, quarantines the rest; also moves erasure to `bundlekv2`) (`bundlekv2:{tenant_id}:{community_id}:...`), dual-read, sweeper (SCAN legacy, name to id resolver endpoint, PTTL preserve, verify, delete, rebuild `:live`/lex), per-tenant `migrated` marker then fail-closed on plaintext | `core/bundle_host_kv/src/{scope,backend}.rs` (after T4), new `core/bundle_host_kv/src/migrate.rs`, hub-api resolver `hub_api/blueprints/v1/internal_scope_resolve.py`, `hub_api/services/{erasure_external,erasure_registry}.py` | T4 |
| T10 | gate gaps G1-G4: kv through `GrantSnapshot`, svc_action parity test, tenant-wide grant, numeric community from verified envelope | `core/bundle_host_kv/src/authorize.rs`, `core/bundle_capability_gate/src/{gate,grant}.rs`, svc_*/`capabilities.rs` gate call sites | T9 (scope type) |
| T11 | v1 `db` re-encrypt job + background DEK-rotation sweeper | new `hub_api/services/reencrypt_sweeper.py`, `scripts/db/reencrypt_bundle_data.py` | T3, T8 |

## Phase 2b: queues (after P1, alpha first)

| ID | Task | Owns | Depends |
|---|---|---|---|
| T12 | `bundle_host_crypto::queue` seal/open helpers (`v,t,dv,p` entry shape, AAD, dual-accept, dead-letter, metrics) | `core/bundle_host_crypto/src/queue.rs` | T3 |
| T13 | svc_ingest producer + outbound relay sealing | `core/svc_ingest/src/{publish,outbound}.rs` | T12 |
| T14 | svc_process consume/produce sealing, bundle-admin transport | `core/svc_process/src/{spine,bundle_admin}.rs` | T12 |
| T15 | svc_action consume/relay/hop sealing | `core/svc_action/src/{dispatch,hop,host_api}.rs`, relay parts of `capabilities.rs` (coordinate with T5: T5 owns kv section only) | T12 |
| T16 | e2e: raw `XRANGE` shows no plaintext, cross-tenant swap fails, mixed sealed/legacy window, DEK-down leaves entries pending, counts asserted | new `core/connector_e2e/tests/tenant_at_rest_queues.rs` | T13-T15 |

T13/T14/T15 are parallel-safe (disjoint files) once T12 merges.

## Phase 2c: economy + reputation (after #1150 merges)

| ID | Task | Owns | Depends |
|---|---|---|---|
| T17 | economy sealed columns + host RMW under advisory lock, sealed aggregates, sealed top-K board + bounded rebuild, migration `0084_economy_sealed.py` | `core/bundle_host_economy/src/{postgres,postgres_ops,lib}.rs`, `alembic/versions/0084_economy_sealed.py` | T3, #1150 |
| T18 | reputation same pattern | `core/bundle_host_reputation/src/postgres.rs`, `alembic/versions/0085_reputation_sealed.py` | T3 |
| T19 | backfill job + real-path tests (concurrent debit/credit under lock, idempotent replay, cap enforcement, leaderboard parity vs plaintext oracle in test only) | hub-api/script + `core/bundle_host_economy/tests/**` | T17, T18 |

T17 and T18 are parallel-safe (disjoint crates).

## Phase 3 (post-beta)
Object body encryption (`core/bundle_host_storage/src/{backend,host}.rs`); interaction store (svc-gateway); `waddles:usage` per R2; `platform_connections` onto tenant DEK; BYOK data-plane test; optional kv key HMAC; erasure/pseudonym-merge docs/code moved to `bundlekv2` (ships with T9: `hub_api/services/erasure_external.py`, `erasure_registry.py`).

## Verification
Per task: unit + integration pass with counts reported, `cargo clippy`/`ruff`, `make test-security`; pre-commit green; no `|| true`; golden-vector parity test must be shown failing once on a flipped byte. Ciphertext-at-rest assertion (raw store read contains no plaintext marker) is mandatory in T6/T8.
