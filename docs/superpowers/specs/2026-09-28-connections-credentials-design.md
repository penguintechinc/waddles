# Waddles v3 CONNECTIONS Registry, Credentials & Relay Authorization — Design

**Date:** 2026-09-28 (rev. 2 — platform limits verified against official docs, broker HA, revocation latency, multi-tenant isolation, kill-switch flags)
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
- **Indexes at this scale need no partitioning.** ~10,000 `connections` rows and ~60,000-150,000 `app_source_bindings`-style rows (§3) are two to three orders of magnitude below where Postgres B-tree/partial-index performance degrades; partitioning is a documented future increment (§7), not a day-one need.

---

## 2. Credentials

**Never plaintext in a replica-exposed table.** Three options considered:

| Option | Mechanism | Verdict |
|---|---|---|
| A. Envelope encryption, RO-readable ciphertext | Per-tenant DEK wraps each secret; ciphertext lives in `connections`/a joined table any RO role can `SELECT` | **Rejected.** Ciphertext-at-rest is necessary but not sufficient — a compromised RO credential still exfiltrates every tenant's ciphertext, and defense in depth requires the RO role to have no path to plaintext at all, not just an extra decrypt step. |
| B. Secret references (Vault/K8s Secret per connection) | `connections` stores a Vault path/K8s Secret name; data plane authenticates to Vault directly | **Rejected at this scale.** 10,000 connections × credential rotation/refresh means 10,000 Vault paths or K8s Secrets, each independently ACL'd and rotated — policy/Secret sprawl becomes its own operational problem, and per-tenant OAuth refresh-token rotation (rewritten every few hours) has no clean K8s-Secret story. |
| **C. Narrow credentials table, RO role has zero grant, credential broker mediates** | `connection_credentials` (envelope-encrypted at rest, defense in depth) exists only inside hub-api's schema; the RO role is never granted `SELECT` on it; svc-ingest/svc-action never touch it directly — they call hub-api's internal credential-broker endpoint | **Chosen.** |

**Why C, concretely:** `rbac-matrix.yaml` already grants `svc_ingest`/`svc_action` `privileges: []` on `ingest_sources` (lines 227-235) — the RO-role-can't-read-secrets posture is already the checked-in intent, just unimplemented. C completes that intent instead of walking it back to grant-then-decrypt (Option A).

```sql
CREATE TABLE connection_credentials (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    kind VARCHAR(20) NOT NULL,           -- 'bot_token' | 'oauth' | 'webhook_secret' | 'twitch_broadcaster_grant'
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

### 2.1 Credential broker — HA, not a SPOF

The broker is a blueprint on hub-api, not a separate singleton service — hub-api already runs ≥2 replicas behind a Service with a `PodDisruptionBudget` (standard ops baseline), so no single pod loss interrupts it. The real risk is the **whole hub-api deployment** degrading (DB contention, KMS outage, rollout) while thousands of connections need to (re)resolve credentials around a redeploy. Because credential resolution happens only at **connect-time and refresh-ahead time**, never per message (relay-authz, the actual per-message path, is fully client-side cached — §5), the broker's availability bar is "connect/refresh cadence," not "message-rate," which the following absorb:

- **Per-replica local cache with stale-while-revalidate.** Each svc-ingest/svc-action replica caches its own resolved secrets in memory alongside a hard TTL and a `known_good_until`. On a broker-call failure, serve the cached secret past its TTL up to an explicit **max-staleness bound**: `min(now + max_stale_window, expires_at)` — `max_stale_window` defaults to 2× the normal refresh-ahead interval (§2.2). A bot token with no `expires_at` can be served stale indefinitely during an outage (it never expires on its own); an OAuth access token is never served past its own `expires_at` — an expired token is useless regardless of cache policy.
- **Bulk/batched resolve on cold start.** A new or restarting replica resolves its whole assigned connection set via one `POST /internal/v1/credentials/resolve-batch` (list of `connection_id`s) instead of N individual calls — turns a fleet-wide cold start into "one batched call per replica," not "10,000 calls in a burst."
- **Circuit breaking.** The broker client wraps calls in a standard breaker (open after N consecutive failures, half-open probe, closed on success); an open breaker serves from the stale-while-revalidate cache and never blocks the caller — same "a dead exporter never breaks the app" posture `critical-rules.md` Observability already mandates for telemetry, applied to the broker too.

### 2.2 OAuth refresh & rotation

- **Rotation fan-out is O(tenants), not O(connections).** DEK rotation re-wraps every `connection_credentials` row for a tenant under a new `dek_version` (hub-api-owned background job) and bumps the broker's cache-invalidation watermark; per-connection secret material is untouched.
- **Refresh-ahead with jitter.** The broker refreshes each OAuth token at ~75% of its TTL, with per-connection random jitter spread across the remaining TTL window, plus a single-flight lock per `(tenant_id, connection_id)` so concurrent resolves never issue duplicate refresh calls — this is what prevents a refresh storm at 10,000s of connections (§4).
- **Audit.** Every `resolve()`/refresh call logs `{tenant_id, connection_id, caller_service, cache_hit}` at INFO (OTel span + penguin logging) — no secret material in the log body.

### 2.3 Why not a Rust sidecar

A Rust credential-resolution sidecar would only pay for itself if credential lookups sat on the per-message hot path — they don't, by design (§2.1, §5). The Python/hub-api broker only gates connect/reconnect and refresh-ahead cadence, both already absorbed by the per-replica cache + batch resolve + circuit breaker above. Moving it to Rust would add a second deployable, a second RBAC surface, and a second place `connection_credentials` grants must be audited, to solve a latency problem that doesn't exist once relay-authz (the thing that actually runs per message) is excluded from the broker's path entirely.

---

## 3. Data-plane consumption at scale

### 3.1 Discord Gateway sharding — verified against official docs

Source: [Discord Developer Docs — Gateway, Sharding](https://discord.com/developers/docs/events/gateway) (canonical URL; redirects to `docs.discord.com/developers/events/gateway`), fetched 2026-09-28.

- **Hard cap, confirmed:** "Each shard can only support a maximum of 2500 guilds" — apps exceeding that ratio on connect get a `4010 Invalid Shard` close code. This is a **maximum guilds-per-shard** (i.e. a **minimum shard count**), not a fixed ratio to hit exactly: `min_shards = ceil(guild_count / 2500)`.
- **max_concurrency** = "the number of identify requests allowed per 5 seconds"; shards bucket via `rate_limit_key = shard_id % max_concurrency` and must IDENTIFY "by bucket, in order." Standard bots get `max_concurrency: 1`; larger buckets (e.g. 16) require Discord's large-bot-sharding approval.
- **IDENTIFY limits, two layers:** 120 gateway events/connection/60s, and a global **1000 IDENTIFY calls per 24h**; exceeding the global limit **terminates every active session for the app and resets the bot token** — materially worse than a soft budget, so reconnect logic must prefer RESUME over a fresh IDENTIFY on any recoverable disconnect.

**Correction to the reviewer's claim:** 8 shards for 6,000 guilds is **compliant, not a violation** — 6,000/8 = 750 guilds/shard, well under the 2,500 cap. The cap only sets a *floor* (`ceil(6000/2500) = 3` shards minimum); choosing more shards than the floor for headroom is always allowed, it just means fewer guilds per shard, never more. The math from the original draft stands:

| Quantity | Value | Basis |
|---|---|---|
| Guild-backed connections | 6,000 | sizing assumption |
| Discord-mandated minimum shards | `ceil(6000/2500)` = **3** | Discord API floor (cited above) |
| Operational shard count (headroom) | **8** (≈750 guilds/shard, ≪ 2,500 cap) | keeps per-shard event volume down and leaves room to grow to ~20,000 guilds before the *mandatory* floor even reaches 8 |
| `max_concurrency` | 1 (standard bucket) | one IDENTIFY per 5s per bucket |
| Cold-start time, 8 shards, `max_concurrency=1` | 8 × 5s = **40s** | serialized IDENTIFYs |
| IDENTIFY budget | 1000/24h (token-wide); breach = full session reset + bot token reset | reconnects must prefer RESUME; an 8-shard full restart costs 8 IDENTIFYs |
| Shard→replica mapping | `shard_id % replica_count`; 8 shards / 4 replicas = 2 shards/replica | matches the existing `bundle_loader` per-replica ownership model |
| Rebalance trigger | crossing the next 2,500-guild floor increase, or a deliberate replica-count change | step-function, not per-connection — reassigning a shard means dropping/reconnecting that websocket, so it's a planned deploy-time action |

### 3.2 Twitch — redone: IRC alone does not scale to 3,500 channels

Sources: [Twitch — IRC guide, rate limits](https://dev.twitch.tv/docs/irc/), [Twitch Developer Forums — "Giving broadcasters control; concurrent join limits for IRC and EventSub"](https://discuss.dev.twitch.com/t/giving-broadcasters-control-concurrent-join-limits-for-irc-and-eventsub/54997), [Twitch — Managing EventSub Subscriptions](https://dev.twitch.tv/docs/eventsub/manage-subscriptions/), [Twitch Developer Forums — RFC 0014, EventSub Subscription Limit Changes](https://discuss.dev.twitch.com/t/rfc-0014-eventsub-subscription-limit-changes/30312). Fetched 2026-09-28.

**The load-bearing correction:** the original draft only modeled the IRC **JOIN rate** (20/10s unverified, 2,000/10s verified) as the constraint. Twitch separately enforces a **concurrent join limit of 100 chat rooms per bot account, total** — "As of May 15th 2024, the limit is set to 100" — and per the forum announcement, **verified-bot status no longer exempts an account from this cap**; the only exemptions are joining as broadcaster/moderator, or a channel where the **broadcaster has explicitly authorized the bot** (granting the `channel:bot` scope for a `channel.chat.message` EventSub subscription created with an **App Access Token**). The reviewer's "~100-channel ceiling" is correct and is the real constraint, not IRC's join-rate throughput. Plain IRC (or unauthorized EventSub) is therefore architecturally insufficient by ~35× at 3,500 channels — this is a design change, not just a numbers correction.

**Redone plan:**

- **Connection registration requires broadcaster authorization.** Registering a Twitch `connections` row (direction inbound or both) requires the broadcaster to grant `channel:bot` via OAuth at registration time in the `/api/v1/connections` flow; the resulting authorization is recorded as a `connection_credentials` row (`kind='twitch_broadcaster_grant'`) tied to that specific `connections.id`. Channels without a completed grant are **not eligible to activate** past the shared 100-channel unauthorized pool (kept as a small always-available buffer for trial/quick-add flows, tracked and capped by hub-api at registration time so we never discover the 100 ceiling by hitting it at connect time).
- **Ingest via EventSub, App Access Token, not raw IRC JOIN, for authorized channels.** Each authorized channel becomes a `channel.chat.message` EventSub subscription over a WebSocket transport, created with the app's own App Access Token — these do not count against the 100-channel cap and are billed against the **app-wide subscription cost budget**, not the crippling per-user-token budget (a user-access-token subscription defaults to `max_total_cost: 10` per client-user pair; App Access Token subscriptions default to **10,000** total cost per client — confirmed via RFC 0014/Managing Subscriptions).
- **WebSocket fan-out:** each EventSub WebSocket connection supports **300 enabled subscriptions max**. 3,500 channels / 300 ≈ **12 WebSocket connections**, spread across e.g. 4 svc-ingest replicas (3/replica) — the same connection-pool-per-replica shape the original IRC plan used, just re-pointed at EventSub sockets instead of IRC sockets.
- **Cost budget:** at cost 1/subscription (the commonly documented per-subscription cost for standard channel-scoped types — **verify the exact `cost` field for `channel.chat.message` in the current subscription-types table before locking capacity**, since Twitch's docs example payloads were inconclusive on this point at fetch time), 3,500 subscriptions ≈ 3,500 cost, within the 10,000 default budget. Adding further per-channel subscription types (follow/subscribe/raid) multiplies this — if total cost would exceed 10,000, split subscription types across multiple registered Twitch client-ids or request a Twitch cost-budget increase, rather than assume headroom that may not exist.
- **IRC is retained only** for the ≤100-channel unauthorized pool and as a fallback transport for a broadcaster who declines authorization; it is not the scaling path.

### 3.3 Hot-add/remove: watermark poll + supervisor

Same pattern as `svc_action::bundle_loader` (§1 D4): each svc-ingest replica polls `ix_connections_shard_scan`'s cheap projection on an interval (default 300s, 5s floor — `bundle_config_poll_seconds`/`bundle_config_poll_interval`, the existing config knob this reuses), hashes it via `WatermarkTracker`, and only re-runs the full shard/subscription-assignment recompute when the hash moves. A newly registered connection is picked up on the next poll tick.

### 3.4 svc-action outbound connection selection

For outbound (Twitch EventSub-authorized send / `LPUSH` where IRC is still in play / Discord bot REST send), svc-action resolves the connection to use from `app_outbound_bindings` (§5) — never from bundle-supplied identifiers — then calls the credential broker (§2) for that connection's live token, with the refresh-ahead jitter (§2.2) spread across each token's TTL window.

### 3.5 Capacity table

| Dimension | Target | Design number |
|---|---|---|
| Tenants | 100s | 300 |
| Communities | 10,000s | 20,000 |
| App-bundle installs (`app_source_bindings`-style rows) | 10,000s | ~60,000 rows |
| Connections (`connections` rows) | ~10,000 | 6,000 Discord / 3,500 Twitch / 500 other |
| Discord shards | — | 8 (750 guilds/shard, floor is 3), 4 replicas, 40s cold-start |
| Twitch EventSub WebSocket connections | — | 12 (300 subs/conn), 4 replicas; IRC only for ≤100-channel unauthorized pool |
| Twitch EventSub cost used (chat-only) | ≤10,000 default budget | ~3,500 (verify per-type cost before adding more subscription types) |
| KMS unwrap calls, steady state | — | ~300 tenants / 10-min DEK TTL ≈ 0.5 calls/sec |
| KMS unwrap calls, cold start (batched resolve, 3 broker-serving replicas) | — | ≤900, one-time burst — jitter warm-up over 30s |
| DEK rotation fan-out | — | O(tenants) = 300 re-wraps per rotation event, never O(connections) |
| Relay-authz cache entries (§5) | — | 20,000-100,000, a few MB/replica |
| Relay-authz DB hits per message | must be O(1)/cached | **0** — in-memory hash-map lookup |
| Revocation latency, fast path (Valkey pub/sub) | — | sub-second, typical |
| Revocation latency, degraded/fallback (poll only) | — | bounded by `max(replica_lag, poll_interval)`; poll interval proposed at 15s for connections/bindings (tighter than bundle_loader's 300s default — security-relevant, not just config-freshness) |

---

## 4. Multi-tenant isolation per pod

A single svc-ingest/svc-action replica multiplexes many tenants' shards/connections and `invoke`s (per `capabilities.rs`'s own module doc: "one host-API connection multiplexes many invokes, potentially for different (tenant, community, app_id) activations"). Without per-tenant bounds, one noisy tenant degrades every other tenant sharing that replica.

- **Per-tenant connection quotas.** Enforced at registration time by hub-api (`connections` INSERT), tied to license tier — a hard cap on connections/tenant prevents one tenant from consuming a disproportionate share of the 10,000-connection budget.
- **Per-tenant outbound rate limiting.** `svc_action`'s existing `UsageBatcher` (`capabilities.rs`, already recording relay-call usage per tenant/app) is extended from metering-only to a token-bucket limiter keyed by `tenant_id` — independent of the platform-level Twitch/Discord limits (§3), this protects other tenants' outbound throughput from one tenant's bundle saturating a shared IRC connection or eating into the shared Discord IDENTIFY budget via reconnect storms.
- **Fair shard/connection scheduling.** Shard→replica and EventSub-connection→replica assignment (§3.1, §3.2) is computed by the same watermark-poll supervisor but weighted by each connection's recent message-volume metric (greedy least-loaded-replica assignment), not a blind `id % replica_count` — so one very active tenant's guilds/channels don't concentrate on a single replica. Rebalance remains deploy-time only (§3.1), never a live migration.
- **Noisy-neighbor isolation in the executor.** A per-tenant semaphore bounds in-flight `invoke`s per svc-action replica, alongside the existing per-`app_id` `EgressGuard` bucket (`capabilities.rs`, `http.send` policy) — extend the same bucket-per-`app_id` pattern to relay pushes, with a tenant-level ceiling layered on top of the app-level one, so one tenant's slow/backed-up bundle can't starve other tenants' invokes on the same executor pool.

---

## 5. Relay authorization

**Today:** `core/svc_action/src/capabilities.rs::handle_relay` already has `InvokeScope{tenant, community, app_id}` resolved per-call (never per-connection, per the module's own post-M3 fix) — but for Twitch it takes `channel` straight from the bundle's own `message_json` with zero ownership check. Discord already avoids the worst of this by construction (`scope.origin_channel_id`, reply-only, never bundle-chosen) — Twitch, and any future non-reply-shaped provider, does not.

**Design:** extend `handle_relay` with a stage-side authorization check before the send, using an outbound-binding join generalizing migration 0025's inbound pattern:

```sql
CREATE TABLE app_outbound_bindings (
    tenant_id INTEGER NOT NULL,
    community_id INTEGER NOT NULL DEFAULT 0,
    app_id VARCHAR(255) NOT NULL,
    platform VARCHAR(50) NOT NULL,
    connection_id BIGINT NOT NULL REFERENCES connections(id),
    PRIMARY KEY (tenant_id, community_id, app_id, platform, connection_id)
);
```

`handle_relay`'s check becomes: `(scope.tenant, scope.community, scope.app_id, provider, channel)` → is there a `connections` row for this tenant with `external_id == channel` AND an `app_outbound_bindings` row joining it to `scope.app_id`? **Deny + audit-log otherwise** (`denied("channel_not_bound", ...)`, matching the existing `HostResultError` shape).

**O(1) lookup:** svc-action loads this join into an in-memory map, `(tenant_id, community_id, app_id, provider, external_id) → bool`, via the watermark-poll supervisor (§3.3). `handle_relay` becomes a hash-map lookup on the hot path, never a synchronous query.

### 5.1 Revocation latency — bounded, with a fast path

A poll-only design bounds revocation latency at `max(replica_lag, poll_interval)` — at `bundle_loader`'s existing 300s default this is far too slow for a security-relevant disable/revoke; even a tightened 15s connections/bindings poll (§3.5) is not "seconds" in the worst case. Two additions:

- **Fast-path invalidation.** hub-api publishes to a Valkey pub/sub channel (or a small revocation `XADD` stream) on every connection disable and every `app_outbound_bindings` delete. svc-action/svc-ingest subscribe and evict the affected cache entry immediately — **typical revocation latency is sub-second**, independent of Postgres replica lag or the poll cycle. The poll remains the self-healing fallback if a pub/sub message is ever missed (subscriber restart, network blip).
- **Fail-closed on cache uncertainty, asymmetrically.** If the fast-path channel's health is uncertain (missed heartbeat, subscriber disconnected), **ALLOW verdicts get a short freshness bound** — an allow entry older than one poll interval is no longer trusted and is re-verified on next poll before being served again, rather than trusted indefinitely. **DENY verdicts remain valid indefinitely** — a stale deny just means a legitimately-rebound app is briefly blocked (an availability cost), never a security cost. This is the concrete fail-closed rule: uncertainty narrows what's trusted, it never widens it.

---

## 6. Feature flag rollout — opt-out kill-switches, not default-OFF

Per the house convention already in this codebase (`core/svc_action/src/flags.rs::DISABLE_DB_BUNDLE_CONFIG_FLAG`, `waddles.core.disable-db-bundle-config` — "inverted... the DB-driven path is the default, and this raw flag being ON is what opts back OUT of it"), every mechanism below ships as an **opt-out kill-switch, ON by default (mechanism enabled), flag raw-ON disables it** — not an opt-in flag defaulted OFF:

| Flag | Mechanism it can disable | Default state |
|---|---|---|
| `waddles.core.disable-connections-registry` | svc-ingest/svc-action read from `connections`/broker; raw-ON reverts to legacy env/secret config | mechanism ON |
| `waddles.core.disable-credential-broker` | broker-mediated resolution; raw-ON reverts to legacy env-sourced tokens | mechanism ON |
| `waddles.core.disable-relay-authz` | the stage-side relay ownership check (§5); raw-ON reopens the M4 gap | mechanism ON — **security-sensitive: raw-ON must alert, not just log**, since it is a live authorization bypass, not a routine rollback |
| `waddles.core.disable-fast-revocation` | Valkey pub/sub fast-path invalidation (§5.1); raw-ON falls back to poll-only revocation latency | mechanism ON |
| `waddles.core.disable-tenant-quotas` | per-tenant rate limiting/fair scheduling (§4); raw-ON reverts to unweighted assignment | mechanism ON |

License-server/PostHog unreachable ⇒ fail-closed to the flag's own raw `false` per `penguin_licensing::LicenseClient`'s existing semantics, which negates to "mechanism enabled" — consistent with every other kill-switch in this crate, and correct here too: an unreachable flag server must never silently reopen `disable-relay-authz`'s gap.

---

## 7. Increment plan

Security-critical items first; each step is independently shippable and independently reviewable.

1. **`connection_credentials` table + RBAC grants (`privileges: []` for every non-hub_api role)** — no broker yet, just the RO-can't-read boundary established at the schema/grant level.
2. **Credential broker endpoint in hub-api** — HA-ready from day one: per-replica stale-while-revalidate cache, batch resolve, circuit breaker (§2.1) — behind `waddles.core.disable-credential-broker`.
3. **`connections` table + migration/backfill from `ingest_sources`; `connection_service.py`** — registry RW lands; `/api/v1/connections` (feature/connections-api) and the core-bundle seeder (feature/core-bundle-seeder) build against this shape — behind `waddles.core.disable-connections-registry`.
4. **Relay-authz stage-side check** (`app_outbound_bindings` + `handle_relay` deny-by-default + audit log) + fast-path revocation (Valkey pub/sub) — closes the M4 gap — behind `waddles.core.disable-relay-authz` and `waddles.core.disable-fast-revocation` (both alerting on raw-ON).
5. **svc-ingest/svc-action wired to the broker**; Twitch broadcaster-authorization flow (`channel:bot` OAuth grant at connection registration) replacing the plain-IRC assumption (§3.2).
6. **Discord shard supervisor + Twitch EventSub-connection-pool supervisor**, watermark-poll hot-add/remove, weighted fair scheduling across replicas (§4) — behind `waddles.core.disable-tenant-quotas` for the weighting specifically.
7. **Scale hardening (deferred, not day-one):** `connections`/`app_outbound_bindings` partitioning if per-tenant row counts approach 10^6; EventSub multi-client-id cost-budget sharding if subscription-type coverage grows past the 10,000-cost default; verify the exact per-type EventSub `cost` values and per-connection WebSocket subscription cap against Twitch's current docs before locking final capacity numbers.

---

*Illustrative sizing numbers (300 tenants, 6,000/3,500/500 platform split, etc.) are assumptions for capacity math, not committed targets. Discord and Twitch limits above are cited to official docs/forum posts fetched 2026-09-28; the exact per-subscription-type EventSub cost value was not conclusively confirmed at fetch time and must be re-verified against the current subscription-types table before implementation locks final Twitch capacity numbers.*
