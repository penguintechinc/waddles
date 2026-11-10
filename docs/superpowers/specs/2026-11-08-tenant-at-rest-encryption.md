# Tenant At-Rest Encryption for Bundle Data (Data-Plane Enforced)

**Date:** 2026-11-08 | **Status:** Proposed | **Scope:** at-rest only: bundle kv/db/objects, service-bundle (economy/reputation) storage, and queue/stream payloads. Transit separation is v4.0.x backlog; transit TLS (pod to Postgres/Valkey, pod to pod) is handled by the separate transit audit (doc PR pending), referenced not duplicated here.
**Requirement (user, 2026-11-08):** storage is cryptographically separated per tenant; access gates are per community; the data plane and service bundles encrypt/decrypt for app bundles; hub-api provisions per-tenant keys on tenant add and at startup for the default (0) and system tenants.
**Extends, does not replace:** `docs/superpowers/specs/2026-09-28-tenant-envelope-encryption-design.md` (hierarchy, AAD, rotation, BYOK, tombstones all stand). The one deliberate change: that doc §5 said "hub-api decrypts, everyone else asks"; the user decided (2026-11-08, confirmed) the **data plane holds the unwrapped tenant DEK in memory** and does the AEAD itself. Handling rules: TTL cache only, `zeroize` on drop, redacted `Debug`, never serialized, logged or put in telemetry, never written to disk or Valkey.

## 1. Existing crypto to reuse (do not invent)

| Piece | File (origin/release/v3.0.X) | Reuse |
|---|---|---|
| Key store + BYOK schema | `alembic/versions/0054_tenant_external_kms.py` (`keystore.tenant_encryption_keys`, `tenant_kms_configs`) | as-is; rows per (tenant, dek_version), `kek_kind` platform/customer_kms |
| DEK lifecycle service | `hub_api/services/envelope/service.py` (`ensure_tenant_key` :663, `rotate_dek` :674, `rewrap_tenant`, cache+TTL, revoked-vs-outage) | provisioning + rotation; **has no callers outside tests** (gap, see 2) |
| Primitives | `hub_api/services/envelope/crypto.py` (HKDF subkey, AAD, AES-256-GCM, local wrap, `PURPOSE_TENANT_DEK`) | Python side |
| Platform KEK | `hub_api/services/envelope/platform_kek.py` (`TENANT_KEK_HEX`, `TENANT_KEK_HEX_PREVIOUS`) | KEK + rotation overlap |
| BYOK adapters | `hub_api/services/envelope/{aws_kms,gcp_kms,azure_key_vault,gate}.py`, `hub_api/blueprints/v1/tenant_kms.py`, `docs/guides/external-kms-byok.md` | Enterprise upsell on top; unchanged |
| Rust AEAD + DEK cache | `core/svc_ingest/src/identity_crypto.rs` (`build_aad`, AES-GCM envelope, HKDF, `DekProvider`, `TtlCachedDekProvider`, `HubApiDekProvider`, `InvalidationSource`); mirrored in `core/svc_process/src/identity_crypto.rs`; golden vector `core/svc_ingest/tests/fixtures/identity_crypto_golden_vector.json` | lift into one shared crate (see 3); `LocalDevDekProvider` is the dev provider (`_LocalFakeDekProvider` is the Python test double in `core/svc_ingest` tests, e.g. `test_badge_flags_regression`) |
| Broker wire | `HubApiDekProvider` calls `POST /api/v1/internal/keys/tenant-dek` (ECDH-wrapped response); **hub-api endpoint is not on release** | build the hub side |
| Field-level AES-GCM today | `alembic/versions/0035/0058/0064` (`platform_connections`, `sealed_secret`, AAD `conn:public_id:platform`), `core/connection_resolver` | stays; migrate onto the tenant DEK later (already planned in the 09-28 doc) |
| Object SSE | `hub_api/services/object_storage_kms.py` (SSE-S3 baseline, SSE-KMS Enterprise), SeaweedFS | baseline floor for objects; body-level crypto phase 3 |

## 2. Key hierarchy and provisioning

```
platform KEK (TENANT_KEK_HEX | customer KMS for Enterprise)
  -> per-tenant DEK  (keystore.tenant_encryption_keys, wrapped, versioned)
       -> HKDF "bundle-data-v1"  -> AES-256-GCM data subkey
       -> HKDF "bundle-bidx-v1"  -> HMAC-SHA256 blind-index subkey
```

- **Provisioning (hub-api, new wiring of existing code):** call `TenantEnvelopeService.ensure_tenant_key(tenant_id)` (a) in the tenant-create path (`hub_api/blueprints/v1/superadmin.py:836 create_tenant`, `hub_api/services/free_tier_caps.py:241 create_tenant_capped`) in the same unit of work, failing the create if it fails (no tenant without a key); (b) at startup after bootstrap/migrations for tenant 0 (default/global) and the `system` tenant (`free_tier_caps.py:64 SYSTEM_TENANT_SLUG`), idempotent; (c) a one-shot backfill for every existing tenant. Startup is the hook in `hub_api/app.py` (envelope runtime is built at :385).
- **Where keys live:** only the wrapped DEK in `keystore.*` (hub_api role only). Data plane holds unwrapped DEKs in process memory only, TTL cache (10 min default in `TtlCachedDekProvider`), never on disk/Valkey/logs.
- **Distribution:** new hub-api internal endpoint `POST /api/v1/internal/keys/tenant-dek` (machine JWT, SPIFFE-ready, tenant from the caller's grant, response ECDH-sealed per `HubApiDekProvider`). Per-service machine identities (svc_process, svc_action) may fetch only DEKs for tenants they serve; audit-logged with tenant_id + version, never key bytes.
- **Rotation/versioning:** unchanged from the 09-28 doc: new `dek_version` active, old retired and readable; every ciphertext carries its `dek_version` in the envelope header; lazy re-encrypt on write + bounded background sweep (7 d); `dek-rotated:{tenant_id}` invalidation on the existing Valkey channel (`InvalidationSource`) plus TTL self-heal; per-DEK 2^30 encryption cap triggers rotation. KEK rotation = re-wrap only.
- **Tenant deletion = crypto-shred** with tombstone (09-28 §4). Sharing note: `connection_credentials.dek_version` is the same DEK.

## 3. Where encryption happens

App bundles never see keys or ciphertext: encryption sits inside the host capability implementations, below the WIT boundary, so frozen kv/db interfaces and v2 WIT are unchanged.

New crate `core/bundle_host_crypto` (lifted from `identity_crypto.rs` + golden vectors, shared by svc_process and svc_action, replacing the duplicated copy): `seal(scope, purpose, plaintext)`, `open(...)`, `blind_index(...)`, `DekProvider` plumbing. Envelope header = `magic | alg | dek_version | nonce`; the magic lets reads distinguish legacy plaintext during migration (reads of legacy plaintext allowed only until phase-2 sweep completes and a per-tenant `migrated` marker is set; after that a non-envelope value is an integrity error, fail closed).

| Surface | Crate | Encrypted | Stays queryable |
|---|---|---|---|
| kv | `core/bundle_host_kv/src/backend.rs` | the **value**; AAD = `tenant_id\|community_id\|app_id\|"kv"\|key\|dek_version` (key bound, so swapping values between keys fails) | guest **keys stay plaintext** (charset-restricted, already PII-free by rule; needed for `list-keys` lexicographic index and quotas). Revisit keyed key-HMAC only if key names are judged sensitive |
| db v2 | `core/bundle_host_db/src/v2/{backend,plan,schema,wire}.rs`, `typed.rs` | every column **not** in a declared index is stored sealed (`bytea`), AAD = `tenant_id\|community_id\|app_id\|table\|column\|row_uuid\|dek_version` | see below |
| object storage | `core/bundle_host_storage/src/{backend,host}.rs` | phase 3: chunked AES-GCM body encryption, per-object nonce prefix, AAD binds tenant/community/app/object key; SSE (baseline/KMS) stays under it | object key/prefix list stays plaintext |
| service bundles (economy, reputation) | `core/bundle_host_{economy,reputation}/src/postgres*.rs`, `bundles/rust/economy` | **sealed balances/amounts**: see 3a | leaderboards via sealed board (3a) |
| service bundles (platform_table, media) | `core/bundle_host_{platform_table,media}` | follow the db column-mode rules | |
| queues/streams | see 3b | whole payload | routing metadata only |

**db v2 query model with encryption** (the manifest column attr, enforced by hub-api approval and the host schema registry; `bundle_data.table_registry` gains a per-column `mode`):

| Column mode | Stored as | Supports | Cost / leakage |
|---|---|---|---|
| `sealed` (default for non-indexed) | AES-GCM ciphertext | read/write only | none queryable; minimal leakage |
| `eq` (index with equality / unique / IN) | ciphertext + `<col>__bidx` = HMAC(blind-index subkey, per-(community, app, table, column) context, value) | `=`, `IN`, unique constraints (unique index on bidx) | leaks equality + frequency within one tenant only; **never** allowed for low-entropy values (booleans, small enums, IPs) unless admin-acknowledged |
| `plain` (explicit opt-in) | cleartext | `< <= > >=`, `prefix`, `ORDER BY`, keyset cursors | leaks value; required for time/ordering columns; approval-time warning; PII denylist still applies |

Effects: `ORDER BY` and range filters work only on `plain` columns (OPE/deterministic order-preserving encryption rejected: weak). `prefix` only on `plain`. The existing "read must be index-supported" planner rule is unchanged because bidx/plain columns carry the indexes (`(tenant_id, community_id, <declared cols>)`). Keyset cursors are `(order column, row_id)` over a `plain` column: unchanged. Row quotas unchanged; byte quotas use `pg_column_size` of the stored row, so ciphertext adds ~28 B/column (document; raise defaults if needed). A new `row_uuid uuid` (host-generated) column goes into the 0082 table definitions so AAD can bind row identity before insert (additive; coordinate with #1139).

**kv CAS digest** (#1139: version = `1 + first 52 bits of sha1(stored value)`): the host seals first and the Lua CAS compares digests of the **stored ciphertext**, so no plaintext hash is exposed. Consequences: (1) version is opaque as documented; (2) rewriting the same plaintext now yields a new version (random nonce), which only makes CAS stricter; (3) any bundle that compared versions for "unchanged" detection must stop (none known). No script change needed; the host's `get_versioned` returns the digest of the ciphertext it read.

### 3a. Economy and reputation: sealed balances (decided: SEAL)

Today arithmetic is SQL: `UPDATE economy_balances SET balance = balance - $4 + $5 WHERE balance >= $4` (`core/bundle_host_economy/src/postgres.rs:444`), `SUM(amount)` over `economy_ledger` for daily caps (`:286`), `ORDER BY b.balance DESC` for leaderboards (`:689`); reputation: `bundle_reputation_scores.balance` + row lock + `SUM` over `bundle_reputation_adjustments` (`core/bundle_host_reputation/src/postgres.rs:163-252`). None of that works on ciphertext. Redesign, keeping the `EconomyStore`/reputation traits (`lib.rs`) so bundles and WIT are unchanged and only the Postgres impls change:

- **Columns:** `balance`, ledger `amount`/`balance_after`/wager fields, reputation `balance`/`delta` become `*_enc bytea` (AAD = `tenant_id|community_id|app_or_economy|table|column|user_uuid|ledger_or_row_uuid|dek_version`). Plain stay: ids, idempotency key, timestamps, kind/status enum codes, plus a new plain `ver bigint` row version.
- **Arithmetic in the host under the existing per-scope writer lock** (`pg_advisory_xact_lock(hashtextextended(scope))`, economy `:240`, reputation `:163`, then `ORDER BY user_uuid FOR UPDATE` as at `:602`): `SELECT` sealed rows, open, check (`balance >= stake`, clamp), compute, seal, `UPDATE ... SET ..., ver = ver + 1 WHERE ver = $seen` (the lock makes a mismatch a bug, surfaced as an error, never silent). Debit+credit (bets, `transfer_from`, pot payouts) stay one transaction; the idempotency-key replay path re-reads the sealed ledger row.
- **Caps/supply ceilings** (per-call, per-day, community supply): replace `SUM` over the ledger with sealed per-scope aggregate rows (`economy_aggregates`: day, issued_enc, debited_enc; community `supply_enc`) updated in the same locked transaction. Ledger rows remain an append-only audit trail (sealed amounts, readable only through the host).
- **Leaderboards without plaintext SQL ordering:** drop the `balance DESC` index. Maintain a **sealed top-K board row per (tenant, community)** (K = `MAX_LEADERBOARD_LIMIT` = 100: `[(user_uuid, balance)]`, one `board_enc` blob, updated incrementally inside the same locked transaction when a write enters/changes/leaves the board). If a removal leaves the board under K, rebuild by a **bounded scan-decrypt-sort** (page by `user_uuid`, hard cap 50 000 rows and 5 s; over the cap returns `leaderboard_unavailable` rather than an unbounded scan; metric `bundle_crypto_board_rebuild_total{result}`). Reads open one row; a host-side cache keyed by `(scope, ver)` is optional. Cost: O(1) per write, O(community size) only on underflow.
- **Reputation** takes the same pattern (sealed score, sealed daily aggregates, sealed board). Cross-community reputation (the one cross-community datum) is sealed under the same tenant DEK; cross-**tenant** reads remain impossible by construction.
- **Impact on #1150 (`transfer_from` + elevated grants):** its atomic SQL debit/credit and the per-app daily caps move into the host RMW + sealed aggregates above; its grant model (`0082_economy_elevated`, `bundle_capability_gate/src/gate_economy_tests.rs`) is untouched. Recommendation: merge #1150 as-is, then land the sealing refactor against the `EconomyStore` trait; do not ask #1150 to anticipate it. The economy service bundle (`bundles/rust/economy`) sees decrypted amounts through the host, no WIT change.
- **Migration:** additive `*_enc` columns, backfill by a host-run job under the same lock (seal then null the plaintext columns), switch the read path per tenant when `migrated`; the SQL `balance` index and plaintext columns are dropped in a later migration (destructive, separate, after verification).

### 3b. Queues and streams (at-rest payloads in Valkey)

Surfaces (from release): svc_ingest `core/svc_ingest/src/publish.rs` (event streams `waddles:t:{tenant}:c:{community}:src:...:events`), `core/svc_ingest/src/outbound.rs` (outbound relay), svc_process `core/svc_process/src/spine.rs` (process queue), svc_action `core/svc_action/src/{capabilities,dispatch,host_api,hop}.rs` (action queue `...:app:{app_id}:action`, relay queue), svc_action `usage.rs` (`waddles:usage` stream; carries ids/counters only, sealed too for uniformity or exempted with justification), bundle-admin transport (`core/svc_process/src/bundle_admin.rs`, #1142), and the interaction store (not on release: lives in the svc-gateway/edge-reshape work; the seal helper is built generic so it plugs in).

- **Entry shape:** `XADD key * v 1 t <tenant_id> dv <dek_version> p <sealed payload>`; the sealed payload holds everything else (event type, platform ids, text, interaction tokens, StageEnvelope body). Plaintext routing metadata is limited to: stream key (tenant/community/app routing, moving to numeric ids with the kv migration), `v`, `t`, `dv`, `event_id` UUID if dedupe/XCLAIM needs it. AAD = `tenant_id|stream_key|"queue"|event_id|dek_version`; ingest's existing per-field identity encryption (`identity_crypto.rs`) stays inside the sealed payload (defense in depth).
- **Consumers** open with the tenant DEK from the shared provider; the stage runner already derives tenant from the verified envelope and must match the stream key (`svc_action/src/hop.rs`), now additionally AAD-enforced.
- **Rollout:** consumers accept sealed or legacy-plaintext entries during a window (`p` absent = legacy); producers seal unless the kill-switch is ON; per-tenant `queues_sealed` marker, after which a plaintext entry is rejected to a sealed dead-letter stream. In-flight entries drain naturally (queues are short-lived); no re-encrypt job except for retained streams/PEL.
- **Failure:** DEK unavailable at produce = refuse the publish with a typed error (event dropped with metric, never plaintext); at consume = leave pending and retry with backoff (not acked, not dropped), dead-letter after N tries, alert. Valkey RDB/AOF and replicas then only ever hold ciphertext (relevant to the durability/SLA decision).
- **Cost:** one AES-GCM per entry (microseconds), DEK cached; payload grows ~28 B + base64 if text-encoded (use binary-safe `p`).

## 4. Numeric scoping and migration off name-keyed kv

**Determined from code (Q4):** the kv `tenant`/`community` strings are the **mutable slug and community name**, not numeric: svc_ingest checks `scope.tenant != resolved.tenant_slug || scope.community != resolved.community_name` (`core/svc_ingest/src/publish.rs:166`) and the schema-v3 envelope carries both names and numeric `tenant_id`/`community_id`. So existing `bundlekv:*` keys are slug/name-keyed and a rename already orphans data. Alpha seeded values were not inspectable from code, so the migration handles both shapes: names resolved via hub-api (slug/name to id, also historical names where recorded), numeric-looking segments accepted as ids, anything unresolvable is quarantined and reported, never guessed. Erasure (`hub_api/services/erasure_external.py`, `erasure_registry.py:603`, `docs/PRIVACY.md:121`) and `docs/design/pseudonym-merge-on-claim.md` reference the `bundlekv:{tenant}:{community}` pattern and must move to `bundlekv2` with the cutover.

Current state: `core/bundle_host_kv/src/scope.rs` builds `bundlekv:{tenant}:{community|_tenant}:{app_id}` from `KvScope{tenant:String, community:Option<String>}` (constructed at `core/svc_process/src/capabilities.rs:2871`, `core/svc_action/src/capabilities.rs:2463`), i.e. string, not the numeric ids. db is already numeric (`DbScope{tenant_id:i32, community_id:i32}`, `TENANT_WIDE_COMMUNITY_ID = 0`). Per the "data plane keys on numeric ids" decision, mutable names must not key data.

- New `KvScope` carries `tenant_id:i32, community_id:i32` (0 = tenant-wide) and builds `bundlekv2:{tenant_id}:{community_id}:{app_id}:...`. AAD always uses the numeric ids.
- **Migration (phase 2, also re-encrypts):** (1) dual-read: new key first, legacy key fallback (legacy values are plaintext; accepted only until marker); (2) every write goes to the new key sealed and deletes the legacy key; (3) background sweeper per (tenant, community, app): `SCAN` legacy prefix, resolve name to id via hub-api (unresolvable names are quarantined and reported, never guessed), preserve TTL (`PTTL`), write sealed under the new key, verify read-back + digest, then delete legacy and rebuild `:live`/lex index; (4) set per-tenant `migrated` marker, then plaintext reads fail closed. Sweeper is idempotent and resumable; bounded rate to protect Valkey. #1142 (fishing bundle) and other bundles are unaffected because keys the guest sees do not change.
- db v2: new tables are created sealed from day one (nothing to migrate). Any v1 `db` rows go through a hub-api-run re-encrypt job using the same crate (phase 2).

## 5. Per-community access gates: current state and gaps

Gates exist and are community-scoped for db: `bundle_capability_gate` (`gate.rs`, `grant.rs` `GrantScopeKey{tenant,community,app,version}`, tables from `alembic/versions/0041_bundle_permission_grants.py`, `community_permission_grants`), plus db v2 enforcing `tenant_id = $1 AND community_id = $2` and RLS via GUCs. svc_process calls the gate before kv (`capabilities.rs` ~2860, `PermissionId::StorageKv`). Gaps to close or verify (all on `path` below):

| # | Gap | Where | Action |
|---|---|---|---|
| G1 | `authorize_kv` + `CapabilitySnapshot` is **app-level only, keyed by `app_id`** (module doc admits cross-tenant collision) | `core/bundle_host_kv/src/authorize.rs:80,148` | route kv through the gate's `GrantSnapshot` keyed by the full scope; delete the interim path |
| G2 | svc_action kv/db gate parity unverified (v2 wiring just added in #1151) | `core/svc_action/src/capabilities.rs` ~2463, 2572 | add parity test: ungranted community denied on both stages |
| G3 | tenant-wide scope (`_tenant` / community 0) readable by any invocation of the app in that tenant | `scope.rs`, `scope.rs` db `TENANT_WIDE_COMMUNITY_ID` | require an explicit tenant-level grant for community 0; test a community-A invocation cannot read community-B keys |
| G4 | community derived from the envelope string for kv; must be the authenticated numeric id and match queue key | svc_process/svc_action `capabilities.rs` | switch to numeric id from the verified envelope (also fixes name keying) |
| G5 | crypto does not enforce community gating (key is per tenant) | by design | community isolation = gate + AAD binding to community_id (a ciphertext copied across communities fails to open); documented, not a per-community key |

## 6. Failure modes and telemetry

- DEK unavailable (broker down, revoked BYOK, unknown tenant, bad version): host call returns a typed `key_unavailable` denial; **no plaintext write, no read of ciphertext, no default key**. Cached DEK keeps serving within TTL for transient outage (stale-while-revalidate); an explicit revocation/denied evicts immediately (09-28 §6 posture, `service.py` `_on_revoked`). Never blocks other tenants (per-tenant state).
- AEAD/AAD failure = integrity error, logged with tenant_id/community_id/app_id/table, counted, never the value.
- Rotation: unknown newer `dek_version` triggers one forced broker refresh then fails closed.
- Kill-switch (confirmed by user; operator decision only, never an automatic fallback on key error): flag `waddles.disable-tenant-at-rest-encryption`; unseen/OFF = encryption ON (core-mechanism convention); ON = emergency rollback to legacy plaintext **writes** (kv, db, queues; sealed economy/reputation keeps sealing, since plaintext columns are removed after migration); the read path still opens envelopes.
- OTel (names, no key material, labels low-cardinality): metrics `bundle_crypto_ops_total{op,surface,result}`, `bundle_crypto_op_duration_seconds` (histogram), `bundle_crypto_dek_fetch_total{result}`, `bundle_crypto_dek_cache_hits_total`, `bundle_crypto_migration_pending{surface}` gauge, `bundle_crypto_key_unavailable_total`; spans `bundle.crypto.seal/open` and `crypto.dek.fetch` under the invocation trace (attributes tenant_id, dek_version; no values); logs via penguin logging: provisioning, rotation, key_unavailable, integrity failure, migration progress. Keys/DEKs/subkeys/plaintext never in spans, attrs, labels, logs, or `Debug` impls (zeroize + redacted repr, as `Dek` already does).
- Dead exporter never fails a call.

## 7. Phasing

| Phase | When | Content | Safe because |
|---|---|---|---|
| **P1** | before Mon 2026-11-09 alpha (alpha is disposable; wipe OK) | provisioning (tenant create, startup 0 + system, backfill) + broker endpoint; `core/bundle_host_crypto`; **kv value encryption** with legacy-tolerant reads; kill-switch; OTel. Queue sealing is NOT in P1 (wide blast radius across 4 stages) | keys exist before consumers ask; read path tolerant; no key renames; no db v2 dependency |
| **P2a** | after #1139/#1151 merge, target before beta Wed 11-11 | db v2 column modes + `row_uuid`; numeric kv scoping + sweeper + `migrated`; gate gaps G1-G4 | needs v2 registry on release |
| **P2b** | after P1; alpha first | queue/stream payload sealing (ingest to process to action, relay, bundle-admin) with dual-accept window | independent of db; stages already hold the DEK provider from P1 |
| **P2c** | after #1150 merges | economy + reputation sealing (3a) | trait-preserving refactor; heavy tests |
| **P3** | post-beta | object body encryption; interaction store; `platform_connections` onto tenant DEK; BYOK data-plane test; usage stream; optional kv key HMAC | |
| Out of scope | v4.0.x / transit audit | transit separation; pod-to-Postgres/Valkey/pod TLS (separate audit doc) | |

## 8. Decisions and remaining questions
Resolved 2026-11-08: Q1 data plane holds DEKs (TTL, zeroize, no logging); Q2 `waddles.disable-tenant-at-rest-encryption` operator-only, unseen/OFF = on; Q3 seal economy/reputation (3a); Q4 slug/name-keyed, migrate handling both (4); Q5 kv key names stay plaintext; queues in scope (3b).
Remaining: R1 leaderboard staleness/size caps (K=100, 50k-row rebuild cap) acceptable? R2 `waddles:usage` stream sealed or exempt (ids/counters only)? R3 where the interaction store lands (svc-gateway) so P3 can target it.
