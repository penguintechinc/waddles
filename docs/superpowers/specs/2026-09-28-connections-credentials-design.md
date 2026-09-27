# Waddles v3 CONNECTIONS Registry, Credentials & Relay Authorization — Design

**Date:** 2026-09-28
**Status:** Proposed design
**Scope:** `hub_api` (registry RW + credential broker), `core/svc_ingest`, `core/svc_action`, `core/svc_process` (registry RO consumption), `core/bundle_executor` (relay-authz enforcement point)
**Principle:** hub-api is the only read-write path for connections and credentials. The data plane (`svc_ingest`, `svc_action`, `svc_process`) reads connection metadata from a read-only replica/role and never sees plaintext secrets directly — it resolves live credentials through a broker call.

**Sizing target (design math below is built to this):** ~300 tenants, ~20,000 communities, ~60,000 app-bundle installs, ~10,000 connections/sources split (illustratively) 6,000 Discord guilds / 3,500 Twitch channels / 500 other platforms.

---

## 1. Registry schema evolution: `ingest_sources` → `connections`

`ingest_sources` (migration 0020) is inbound-only, has no `direction`, `status`, `external_kind`, or `created_by`, and its `secret_ciphertext`/`secret_iv` columns sit in the same table the RO data-plane roles will eventually be granted `SELECT` on — a plaintext-adjacent column can never live in a table a replica-scoped role reads. `connections` generalizes and replaces it; outbound-only rows (e.g. a Twitch bot account with no inbound IRC receiver) and platform-shared rows (bot tokens configured once, not per-tenant) both need a home this table didn't have.

```sql
-- migration 0026_connections_registry
CREATE TABLE connections (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    community_id INTEGER REFERENCES communities(id),      -- NULL = tenant-wide
    platform VARCHAR(50) NOT NULL,
    external_kind VARCHAR(20) NOT NULL,                    -- 'guild' | 'channel' | 'account'
    external_id VARCHAR(255) NOT NULL,                     -- guild id / twitch login / channel id
    label VARCHAR(255) NOT NULL,
    direction VARCHAR(10) NOT NULL
        CHECK (direction IN ('inbound', 'outbound', 'both')),
    status VARCHAR(20) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'healthy', 'degraded', 'error', 'disabled')),
    last_health_at TIMESTAMPTZ,
    last_health_error TEXT,
    shard_assignment JSONB,        -- {"shard_id":3,"replica":"svc-ingest-1"} -- svc-ingest-owned, hub-api never interprets it
    rate_limit_meta JSONB,         -- platform-specific budget bookkeeping (join budget, EventSub cost used, etc.)
    credential_id BIGINT REFERENCES connection_credentials(id),  -- NULL = shared platform credential, not per-connection
    mapping JSONB,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_by VARCHAR(255) NOT NULL,   -- OIDC sub that registered it (admin or seeder service account)
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, platform, external_id, direction)
);

CREATE INDEX ix_connections_shard_scan ON connections (platform, enabled, id);      -- svc-ingest's sharding-assignment poll
CREATE INDEX ix_connections_tenant_community ON connections (tenant_id, community_id);  -- admin list/webui
```

**Decisions**

- **D1 — `connections` supersedes `ingest_sources`; migrate, don't dual-write.** Migration 0026 creates `connections`, backfills every `ingest_sources` row (`direction='inbound'`, `external_kind` derived from `platform`), then `ingest_source_service.py`'s callers move to a new `connection_service.py` in the same migration's PR. `ingest_sources` is dropped in a follow-up migration once `workstreams`' FK is repointed — kept one release behind as a safety window, never as a second source of truth.
- **D2 — `credential_id` is a nullable FK, not inline columns.** Keeps secret material and rotation metadata in one narrow table (`connection_credentials`, §2) instead of spreading `secret_ciphertext`/`secret_iv`/expiry columns across `connections`, which is the table admin UIs list, filter and paginate — the RO role's grant surface for `connections` (metadata) must never overlap the grant surface for `connection_credentials` (secrets).
- **D3 — `external_kind` disambiguates the id namespace per platform** (Discord guild id vs. channel id vs. a Twitch login) instead of a platform-specific column set, so adding a platform never means a migration.
- **D4 — watermark poll, not a trigger-maintained counter.** Mirrors `core/bundle_active_set`'s `read_watermark`/`WatermarkTracker` (hash a cheap `(id, updated_at, enabled)` projection, compare to the last-seen hash, skip the full reload when unchanged) rather than inventing a second mechanism — `ix_connections_shard_scan` covers that projection query.
- **Indexes at this scale need no partitioning.** ~10,000 `connections` rows and ~60,000-150,000 `app_source_bindings`-style rows (§4) are two to three orders of magnitude below where Postgres B-tree/partial-index performance degrades; partitioning is a documented future increment (§6, Increment 7), not a day-one need.

---

## 2. Credentials

**Never plaintext in a replica-exposed table.** Three options considered:

| Option | Mechanism | Verdict |
|---|---|---|
| A. Envelope encryption, RO-readable ciphertext | Per-tenant DEK wraps each secret; ciphertext lives in `connections`/a joined table any RO role can `SELECT` | **Rejected.** Ciphertext-at-rest is necessary but not sufficient — a compromised RO credential still exfiltrates every tenant's ciphertext plus (if the DEK-unwrap key is ever cached client-side) a path to plaintext. Defense in depth requires the RO role to have no path to plaintext at all, not just an extra decrypt step. |
| B. Secret references (Vault/K8s Secret per connection) | `connections` stores a Vault path/K8s Secret name; data plane authenticates to Vault directly | **Rejected at this scale.** 10,000 connections × credential rotation/refresh means 10,000 Vault paths or K8s Secrets, each independently ACL'd, mounted, and rotated — Vault policy sprawl and K8s Secret count both become their own operational problem before the connection count does, and per-tenant OAuth refresh-token rotation has no clean K8s-Secret story (a Secret is not designed to be rewritten every few hours by a broker). |
| **C. Narrow credentials table, RO role has zero grant, credential broker mediates** | `connection_credentials` (envelope-encrypted at rest, defense in depth) exists only inside hub-api's schema; the RO role is never granted `SELECT` on it, full stop; svc-ingest/svc-action never touch it directly — they call hub-api's internal credential-broker endpoint | **Chosen.** |

**Why C, concretely:** the current `rbac-matrix.yaml` already grants `svc_ingest`/`svc_action` `privileges: []` on `ingest_sources` (lines 227-235) — the RO-role-can't-read-secrets posture is already the checked-in intent, just unimplemented. C completes that intent instead of walking it back to grant-then-decrypt (Option A).

```sql
CREATE TABLE connection_credentials (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    kind VARCHAR(20) NOT NULL,           -- 'bot_token' | 'oauth' | 'webhook_secret'
    dek_version INTEGER NOT NULL,        -- which per-tenant DEK version wrapped this row
    ciphertext BYTEA NOT NULL,
    iv BYTEA NOT NULL,
    oauth_refresh_ciphertext BYTEA,      -- NULL for non-OAuth kinds
    oauth_refresh_iv BYTEA,
    expires_at TIMESTAMPTZ,              -- access-token expiry, drives refresh-ahead scheduling
    rotated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- rbac-matrix.yaml: hub_api gets SELECT/INSERT/UPDATE/DELETE; every other role, including
-- svc_ingest/svc_action/webui/migration_runner's usual RW, gets privileges: [] -- no exceptions.
```

**Envelope encryption, one level in:** each tenant has one active DEK, itself wrapped by an app-wide KEK. Baseline (every environment) = platform-managed KEK (KMS default-key envelope, `security.md` Storage baseline). **Enterprise upsell** = customer/external KMS wraps the KEK instead (`critical-rules.md` Feature Flags & License Tiers) — additive to the baseline, not a substitute for it.

**Credential broker flow** (new internal-only hub-api blueprint, `POST /internal/v1/credentials/resolve`, JWT-authenticated service-to-service, tenant-scoped):

```
svc_ingest/svc_action                    hub-api (broker)                 connection_credentials
      |  resolve(connection_id) -------------->|                                    |
      |                                        | cache hit (TTL)? -----return------->|
      |                                        | cache miss: SELECT row ------------>|
      |                                        | KMS.Decrypt(DEK, dek_version)       |
      |                                        | AESGCM.decrypt(ciphertext, DEK)     |
      |  <---- plaintext secret (in-memory, never persisted at the caller)-----------|
```

- **Least privilege:** svc-ingest/svc-action hold a short-lived service JWT (scope `credentials:resolve`, tenant-scoped) and the connection id — never a DB credential that could read `connection_credentials`.
- **Caching TTL:** broker caches the **decrypted DEK** (not the per-connection plaintext secret) in memory, 10 min TTL, LRU-bounded — resolving a connection re-runs the cheap AES-GCM unwrap locally but skips the KMS round trip on a warm DEK. Per-connection plaintext is never cached beyond the single resolve call's response.
- **Rotation:** DEK rotation re-wraps every `connection_credentials` row for that tenant under the new `dek_version` (a background job, hub-api-owned) and bumps the broker's cache-invalidation watermark; it never touches per-connection secret material.
- **OAuth refresh (Twitch/Discord):** the broker refreshes ahead of `expires_at` (~75% of TTL) with per-connection jitter (§3.4) and a single-flight lock per `(tenant_id, connection_id)` so concurrent resolves never issue duplicate refresh calls to the platform's token endpoint.
- **Audit:** every `resolve()` call logs `{tenant_id, connection_id, caller_service, cache_hit}` at INFO (OTel span + penguin logging) — no secret material in the log body (`security.md`).

---

## 3. Data-plane consumption at scale

### 3.1 Discord Gateway sharding

Discord requires `shard_count ≥ ceil(guild_count / 2500)` at IDENTIFY time; that floor is a hard API rule, not a recommendation.

| Quantity | Value | Basis |
|---|---|---|
| Guild-backed connections | 6,000 | sizing assumption |
| Discord-mandated minimum shards | `ceil(6000/2500)` = **3** | Discord API floor |
| Operational shard count (headroom) | **8** (≈750 guilds/shard) | keeps per-shard event volume and `large_threshold` well under Discord's guidance; leaves room to grow to ~9,000 guilds before the next resize |
| `max_concurrency` | 1 (standard bot bucket; 16 only via Discord's large-bot-sharding approval) | one IDENTIFY per 5s per bucket |
| Cold-start time, 8 shards, `max_concurrency=1` | 8 × 5s = **40s** | serialized IDENTIFYs |
| IDENTIFY budget | 1000 / 24h (token-wide) | reconnects must prefer RESUME over fresh IDENTIFY; a full 8-shard restart costs 8 IDENTIFYs — budget supports ~100 full restarts/day before RESUME-preference is even needed |
| Shard→replica mapping | `shard_id % replica_count`; 8 shards / 4 svc-ingest replicas = 2 shards/replica | matches the existing `bundle_loader` per-replica ownership model |
| Rebalance trigger | crossing the next 2500-guild threshold, or a deliberate replica-count change | step-function, not per-connection — reassigning a shard means dropping and reconnecting that shard's websocket, so it is a planned deploy-time action, never a live migration |

### 3.2 Twitch IRC + EventSub

| Quantity | Value | Basis |
|---|---|---|
| Channel-backed connections | 3,500 | sizing assumption |
| IRC JOIN rate limit | 20 joins / 10s, **per bot account**, not per socket | opening more IRC connections under the same account does not raise this — parallelizing connections does not parallelize JOIN throughput |
| Cold fan-in time (all 3,500 channels from zero) | 3500 / 20 × 10s ≈ **29 min** | mitigated by never restarting the whole fleet at once — rolling, one-replica-at-a-time restarts keep any single JOIN burst to that replica's channel share |
| Channels per IRC connection (operational choice, not a protocol cap) | ~300 | bounds the blast radius of one dropped TCP connection (all its channels rejoin together) and per-socket message throughput |
| IRC connections needed | 3500 / 300 ≈ **12**, spread over 4 svc-ingest replicas (3/replica) | |
| EventSub per-app subscription cost budget | 10,000 (default, Twitch-side, raisable on request) | most channel-scoped subscription types cost 1 |
| EventSub cost if used for chat only | 3,500 × 1 = 3,500 | within budget |
| EventSub cost if used for chat + follow + subscribe + raid (4 types) | 3,500 × 4 = 14,000 | **exceeds** the default budget — requires either a Twitch cost-budget increase, splitting subscription types across multiple registered client-ids, or defaulting to IRC for chat and reserving EventSub only for event types IRC can't deliver |
| EventSub WebSocket subscriptions per connection | low hundreds (verify exact current cap against Twitch docs at implementation time — Twitch has changed this number across API revisions) | drives the same connection-pool fan-out as IRC above |

### 3.3 Hot-add/remove: watermark poll + supervisor

Same pattern as `svc_action::bundle_loader` (§1 D4): each svc-ingest replica polls `ix_connections_shard_scan`'s cheap projection on an interval, hashes it via `WatermarkTracker`, and only re-runs the full shard-assignment recompute when the hash moves — a newly registered connection is picked up on the next poll tick, not via a push mechanism, keeping the RO read path identical in shape to the existing bundle-config poll already in production.

### 3.4 svc-action outbound connection selection

For outbound (Twitch `LPUSH` / Discord bot REST send), svc-action resolves the connection to use from `app_source_bindings` (or its outbound-binding generalization, §4) — never from bundle-supplied identifiers — then calls the credential broker (§2) for that connection's live token, with the refresh-ahead jitter spread across each token's TTL window so 10,000 connections' refreshes never cluster into one burst against Twitch's OAuth endpoint.

### 3.5 Capacity table

| Dimension | Target | Design number |
|---|---|---|
| Tenants | 100s | 300 |
| Communities | 10,000s | 20,000 |
| App-bundle installs (`app_source_bindings`-style rows) | 10,000s | ~60,000 rows |
| Connections (`connections` rows) | ~10,000 | 6,000 Discord / 3,500 Twitch / 500 other |
| Discord shards | — | 8 (750 guilds/shard), 4 replicas |
| Twitch IRC connections | — | 12 (300 channels/conn), 4 replicas |
| KMS unwrap calls, steady state | — | ~300 tenants / 10-min DEK TTL ≈ 0.5 calls/sec |
| KMS unwrap calls, cold start (3 broker replicas) | — | ≤900, one-time burst — jitter warm-up over 30s |
| DEK rotation fan-out | — | O(tenants) = 300 re-wraps per rotation event, never O(connections) |
| Relay-authz cache entries (§4) | — | 20,000-100,000, a few MB/replica |
| Relay-authz DB hits per message | must be O(1)/cached | **0** — in-memory hash-map lookup, refreshed only on watermark change |

---

## 4. Relay authorization

**Today:** `core/svc_action/src/capabilities.rs::handle_relay` already has `InvokeScope{tenant, community, app_id}` resolved per-call (never per-connection, per the module's own post-M3 fix) — but for Twitch it takes `channel` straight from the bundle's own `message_json` with zero ownership check. Discord already avoids the worst of this by construction (`scope.origin_channel_id`, reply-only, never bundle-chosen) — Twitch, and any future non-reply-shaped provider, does not.

**Design:** extend `handle_relay` with a stage-side authorization check before the `LPUSH`/send, using the same `app_source_bindings` join already built for inbound (migration 0025) generalized to also cover outbound targets:

```sql
-- extends app_source_bindings' pattern to outbound; or a sibling
-- app_outbound_bindings table with an identical shape if inbound/outbound
-- binding lifecycles diverge enough to warrant separating them
CREATE TABLE app_outbound_bindings (
    tenant_id INTEGER NOT NULL,
    community_id INTEGER NOT NULL DEFAULT 0,
    app_id VARCHAR(255) NOT NULL,
    platform VARCHAR(50) NOT NULL,
    connection_id BIGINT NOT NULL REFERENCES connections(id),
    PRIMARY KEY (tenant_id, community_id, app_id, platform, connection_id)
);
```

`handle_relay`'s check becomes: `(scope.tenant, scope.community, scope.app_id, provider, channel)` → is there a `connections` row for this tenant with `external_id == channel` AND an `app_outbound_bindings` row joining it to `scope.app_id`? **Deny + audit-log otherwise** (`denied("channel_not_bound", ...)`, matching the existing `HostResultError` shape `handle_relay` already returns for `unknown_provider`/`invalid_args`).

**O(1) lookup, not a DB hit per message:** svc-action loads this join into an in-memory map, keyed by `(tenant_id, community_id, app_id, provider, external_id)`, via the identical watermark-poll supervisor pattern as `bundle_loader` (§3.3) — refreshed only when the watermark moves. `handle_relay` becomes a hash-map lookup on the hot path, never a synchronous query. A binding not yet in cache (just-approved app, poll hasn't ticked yet) **fails closed** — denied, not fetched inline — consistent with authorization checks defaulting to deny under any uncertainty (`security.md`); the next poll tick picks it up within the existing poll interval, same latency budget `app_source_bindings` already accepts for inbound consumer-group provisioning.

---

## 5. Increment plan

Security-critical items first; each step is independently shippable and independently reviewable.

1. **`connection_credentials` table + RBAC grants (`privileges: []` for every non-hub_api role)** — no broker yet, just the RO-can't-read boundary established at the schema/grant level, matching the already-checked-in intent on `ingest_sources`.
2. **Credential broker endpoint in hub-api** (`/internal/v1/credentials/resolve`), DEK-cache, KMS envelope encryption, audit logging — no data-plane caller wired yet, unit/integration tested standalone.
3. **`connections` table + migration/backfill from `ingest_sources`; `connection_service.py` replacing `ingest_source_service.py`** — registry RW lands, `/api/v1/connections` route (feature/connections-api) and core-bundle seeder (feature/core-bundle-seeder) build against this shape.
4. **Relay-authz stage-side check** (`app_outbound_bindings` + `handle_relay` deny-by-default + audit log) — closes the M4 gap (a vendor bundle naming any provider/channel) before any new outbound provider or higher-scale rollout ships.
5. **svc-ingest/svc-action wired to the broker** — replace env/secret-sourced Discord guild + outbound bot tokens with broker calls; OAuth refresh-ahead + jitter + single-flight.
6. **Discord shard supervisor + Twitch IRC connection-pool supervisor**, watermark-poll hot-add/remove, shard/connection→replica assignment recorded in `connections.shard_assignment`.
7. **Scale hardening (deferred, not day-one):** `connections`/`app_outbound_bindings` partitioning if per-tenant row counts approach 10^6; EventSub multi-client-id cost-budget sharding if subscription-type coverage grows past the 10,000-cost default.

---

*Illustrative sizing numbers (300 tenants, 6,000/3,500/500 platform split, etc.) are assumptions for capacity math, not committed targets — real distribution should be re-validated against actual seeded/production data before final shard/replica counts are locked in.*
