# Waddles v3 CONNECTIONS Registry, Credentials & Relay Authorization — Design

**Date:** 2026-09-28 (rev. 7 — registry split into `sources` (physical, platform-side, ingested once) + `community_connections` (per-community M:N link, own config/credentials) so the same Twitch channel/Discord guild can be linked to multiple communities, even across tenants, without duplicating platform ingest cost; every downstream section — credentials, capacity math, webhook receivers, relay-authz — updated to the new FK shape; final revision, design now APPROVED-WITH-CONDITIONS, §9)
**Status:** Approved with conditions (§9) — remaining verification happens in implementation PR reviews
**Scope:** `hub_api` (registry RW + credential broker + Twitch subscription reconciler), `core/svc_ingest` (both webhook receivers + IRC pool + Discord Gateway), `core/svc_action` (outbound relay/authz + Twitch Send Chat Message calls), `core/svc_process` (registry RO consumption), `core/bundle_executor` (relay-authz enforcement point)
**Principle:** hub-api is the only read-write path for sources, community connections, and credentials. The data plane reads registry metadata from a read-only replica/role and never sees plaintext secrets directly — it resolves live credentials through a broker call.

**Sizing target:** ~300 tenants, ~20,000 communities, ~60,000 app-bundle installs, ~10,000 **physical sources** split (illustratively) 6,000 Discord guilds / 3,500 Twitch channels / 500 other platforms, with explicit headroom to 10,000 total Twitch channels, and community-connection **links** running higher than the source count (§3.5) since one source can serve several communities.

---

## 1. Registry schema evolution: `ingest_sources` → `sources` + `community_connections`

`ingest_sources` (migration 0020) assumes a 1:1 relationship between a tenant and a platform resource (`UNIQUE(tenant_id, platform, source_id)`) that doesn't hold in practice: the same physical Twitch channel or Discord guild is routinely linked to **multiple communities — even within the same tenant** (a streamer's own community, a team community, a sponsor community) **and across tenants** (a public channel two unrelated customers both want events from). One row per tenant means either duplicating the physical ingest per linking community — tripling Twitch cost, duplicating Discord Gateway/EventSub registrations — or breaking when a second community tries to link an already-registered source.

**Split into two tables:** `sources` (the physical, platform-side resource — ingested exactly once per credential identity) and `community_connections` (the per-community M:N link, config, and credentials).

```sql
-- migration 0026_sources_and_community_connections

-- The physical thing on the platform side. No tenant_id/community_id --
-- a source is not owned by any one tenant, it is *linked* to communities
-- via community_connections below. Deduped by (platform, external_id,
-- credential_identity): the same channel ingested under the SAME
-- credential is exactly one row, however many communities link to it;
-- ingested under a DIFFERENT credential (a tenant's own custom bot /
-- whitelabel / broadcaster grant instead of our shared default) is a
-- deliberate second row -- a distinct ingest identity with its own
-- subscription/shard, not a per-community override of the first.
CREATE TABLE sources (
    id BIGSERIAL PRIMARY KEY,
    platform VARCHAR(50) NOT NULL,
    external_kind VARCHAR(20) NOT NULL,       -- 'guild' | 'channel' | 'account' | 'webhook'
    external_id VARCHAR(255) NOT NULL,        -- guild id / twitch broadcaster id / opaque webhook_id
    credential_identity VARCHAR(50) NOT NULL DEFAULT 'platform-shared',
        -- 'platform-shared' (our default app/bot credential) or a
        -- tenant-scoped identifier for a custom-bot/whitelabel ingest identity
    status VARCHAR(20) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'healthy', 'degraded', 'error', 'disabled')),
    last_health_at TIMESTAMPTZ,
    last_health_error TEXT,
    shard_assignment JSONB,        -- {"conduit_id":..,"shard_id":..,"replica":"svc-ingest-1"} -- keyed by the PHYSICAL source, ingested once regardless of link count
    rate_limit_meta JSONB,         -- EventSub cost, join budget, etc. -- also physical-source-scoped
    credential_id BIGINT REFERENCES connection_credentials(id),  -- the shared/custom INGEST credential; NULL/platform-owned = the default
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (platform, external_id, credential_identity)
);

-- The M:N link: which community consumes which source, with that
-- community's own config and (optionally) its own outbound credential.
CREATE TABLE community_connections (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    community_id INTEGER NOT NULL REFERENCES communities(id),  -- 0 = tenant-wide, same sentinel as app_active_versions (migration 0022)
    source_id BIGINT NOT NULL REFERENCES sources(id),
    label VARCHAR(255) NOT NULL,
    direction VARCHAR(10) NOT NULL
        CHECK (direction IN ('inbound', 'outbound', 'both')),
    status VARCHAR(20) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'healthy', 'degraded', 'error', 'disabled')),
    mapping JSONB,                  -- per-community config: for kind='webhook' sources, the bound app_id/event_type mapping
    credential_id BIGINT REFERENCES connection_credentials(id),
        -- THIS community's own credential (per-community webhook secret; a
        -- custom outbound identity distinct from the source's shared ingest
        -- credential) -- NULL = outbound falls back to sources.credential_id.
        -- Encrypted under the link's own tenant's single DEK (§2) --
        -- communities share their tenant's DEK, never get their own; what's
        -- per-community is the row/secret *value* and the mapping, not the key.
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_by VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (community_id, source_id)
);

CREATE INDEX ix_sources_shard_scan ON sources (platform, status, id);        -- svc-ingest's sharding-assignment poll -- physical sources only
CREATE INDEX ix_community_connections_tenant_community ON community_connections (tenant_id, community_id);
CREATE INDEX ix_community_connections_source ON community_connections (source_id);  -- fan-out: source -> linked communities
```

**Decisions**

- **D1 — `sources`/`community_connections` supersede `ingest_sources`; migrate, don't dual-write.** For every `ingest_sources` row: create (or reuse, if an identical `(platform, external_id, 'platform-shared')` row already exists) a `sources` row, then a `community_connections` row carrying that row's old `tenant_id`/`community_id`/`label`/`direction`/secret. The old `UNIQUE(tenant_id, platform, source_id)` becomes two narrower constraints doing two different jobs: `sources.UNIQUE(platform, external_id, credential_identity)` (dedup the physical ingest) and `community_connections.UNIQUE(community_id, source_id)` (a community links a given source at most once). `ingest_sources` drops in a follow-up migration.
- **D2 — ingest once, fan out many: the platform-facing resource is `sources`, never `community_connections`.** Shard assignment, an EventSub subscription, a Discord guild registration — anything that talks to the *platform* — is keyed by `sources.id`, so linking a second, third, or Nth community to an already-ingested channel/guild creates a `community_connections` row only, never a second subscription or gateway registration; this is what keeps Twitch's `max_total_cost` and Discord's shard/IDENTIFY budgets from tripling as link count grows. "Ingest once" is a statement about the platform-facing resource — the existing internal stream fan-out (§3.2/§3.3, generalizing migration 0025's pattern) still writes the one received event into each linked community's own `(tenant, community, platform, source)`-keyed stream, a cheap internal Valkey `XADD` per link, not a platform API call.
- **D3 — credentials stay tenant-scoped, one DEK per tenant, never per community.** Every tenant has exactly one active DEK (§2) — communities never get their own. `community_connections.credential_id` rows are encrypted under **that link's own tenant's** DEK, same as every other `connection_credentials` row for that tenant; what's per-community is the **mapping** (which source, which config, which distinct secret *value*), never a separate key hierarchy. `sources.credential_id`, when it's the `'platform-shared'` default, is the one exception: it belongs to the platform, not to any tenant, and lives under a reserved platform-owned tenant row so it still goes through the same envelope-encryption machinery without a second scheme (§2).
- **D4 — `credential_identity` on `sources` is what makes "bring your own bot" a distinct ingest, not a per-community override.** A community wanting its own custom bot/whitelabel/broadcaster-grant identity for a channel waddles otherwise ingests via the shared default gets a **second `sources` row** (same `platform`/`external_id`, different `credential_identity`) and links to that one instead — a deliberate, separately-billed, separately-isolated ingest, not a flag on the shared row.
- **D5 — `external_kind` disambiguates the id namespace per platform** (including `'webhook'` for opaque webhook ids, §4.2) so adding a platform/receiver type never means a migration.
- **D6 — watermark poll, not a trigger-maintained counter** — mirrors `core/bundle_active_set`'s `read_watermark`/`WatermarkTracker`, now polling `sources` for physical shard/subscription assignment and `community_connections` for the link-level fan-out set.
- **No partitioning at this scale** — ~10,000 `sources` rows and a larger but still modest `community_connections` row count (§3.5) are orders of magnitude below where Postgres indexing degrades.

---

## 2. Credentials

**Never plaintext in a replica-exposed table.**

| Option | Verdict |
|---|---|
| A. Envelope encryption, RO-readable ciphertext | **Rejected** — RO role still has a path to plaintext given a decrypt step. |
| B. Secret references (Vault/K8s Secret per link) | **Rejected at scale** — thousands of independently-rotated Vault paths/Secrets is its own sprawl problem, and no clean rewrite-every-few-hours story for OAuth refresh tokens. |
| **C. Narrow credentials table, RO role zero grant, credential broker mediates** | **Chosen** — matches `rbac-matrix.yaml`'s already-checked-in `privileges: []` for `svc_ingest`/`svc_action` on `ingest_sources`. |

```sql
CREATE TABLE connection_credentials (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),  -- every row belongs to exactly one tenant's DEK, including the reserved platform-owned tenant (D3)
    kind VARCHAR(20) NOT NULL,    -- 'bot_token' | 'oauth' | 'webhook_secret' | 'twitch_broadcaster_grant'
    dek_version INTEGER NOT NULL,
    ciphertext BYTEA NOT NULL,
    iv BYTEA NOT NULL,
    oauth_refresh_ciphertext BYTEA,
    oauth_refresh_iv BYTEA,
    expires_at TIMESTAMPTZ,
    rotated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- hub_api: SELECT/INSERT/UPDATE/DELETE. Every other role: privileges: [] -- no exceptions.
```

Envelope encryption: **one DEK per tenant** (never per community), wrapped by an app-wide KEK (platform-managed KMS baseline; Enterprise upsell = customer/external KMS wraps the KEK, additive). **All communities within a tenant share that tenant's single DEK** — the thing that's scoped per community is the *mapping* (§1: which sources a community links, which app bindings, which active bundle versions, which outbound bindings — `community_connections`, `app_source_bindings`, `app_active_versions`, `app_outbound_bindings` are all community-scoped tables), never the encryption key itself. A `community_connections.credential_id` row is a distinct row with its own ciphertext, but it is wrapped by the *same* DEK as every other `connection_credentials` row for that link's tenant — rotating a tenant's DEK re-wraps every community's credential rows in that tenant in one pass (§2.3), not one rotation per community. The platform-owned `sources.credential_id` default (D3) is the sole exception, living under a reserved platform tenant so it never needs a second envelope scheme.

### 2.1 Credential broker — HA, stateless replicas, no split-brain

The broker is a hub-api blueprint, not a singleton — hub-api runs ≥2 replicas with a `PodDisruptionBudget`. **Every replica is stateless w.r.t. credentials**: `connection_credentials` (Postgres) plus the KMS-wrapped DEK is the sole shared source of truth; no replica holds state another lacks, so there is no split-brain to resolve — every replica independently derives the same answer from the same row plus the same KMS unwrap.

Two cache layers, both pure read-through, both invalidated by the **same Valkey pub/sub bus** the relay-authz fast path uses (§6.1): hub-api's per-replica DEK cache (`dek-rotated:{tenant_id}`, §2.2) and each data-plane replica's per-credential-row secret cache (`credential-revoked:{credential_id}`). Pub/sub is the *fast* path, not the *only* path — a missed event is bounded, never open-ended (§2.2's epoch check, and §3.3's watermark-poll fallback) — no replica-to-replica RPC or leader election needed.

**Resolution always reads the primary, never the RO replica.** The broker lives inside hub-api, which already holds the RW/primary Postgres connection for `connection_credentials` — `resolve()` and `resolve-batch` query that primary connection directly. This is deliberate, not an oversight: routing credential resolution through hub-api's own RO replica (if it had one) would let replication lag re-serve a just-rotated or just-revoked row's stale state, exactly the failure mode the fast-path/epoch design (§2.2) exists to close. Only the *data-plane's* view of `sources`/`community_connections` metadata (§1, §3.3) reads a replica — credentials never do.

Credential resolution happens only at **connect-time and refresh-ahead time**, never per message (relay-authz, the per-message path, is fully client-side cached — §6):

- **Per-replica stale-while-revalidate cache.** On broker-call failure, serve past TTL up to `min(now + max_stale_window, expires_at)`; `max_stale_window` = 2× the refresh-ahead interval.
- **Batched cold-start resolve** — one `POST /internal/v1/credentials/resolve-batch` per replica instead of N individual calls.
- **Circuit breaking** — standard breaker; an open breaker serves stale-cache, never blocks the caller.
- **Primary-connection hygiene.** Each hub-api replica holds a **capped connection pool** to the primary (bounded, never unbounded fan-out under load) and a **per-replica request rate limit** on its own resolve traffic, so a burst of cold-start batches from many data-plane replicas can't itself become a self-inflicted primary overload. On a lost primary connection, reconnection uses **jittered exponential backoff**, never a synchronized retry storm across replicas.
- **Primary failover.** During an actual primary failover (not just a transient blip), the broker serves already-cached credentials within their defined max-staleness bound (above) — new resolves that can't reach any primary within that window **fail closed**, an error, never a guess.

### 2.2 DEK cache: versioned, bounded, fails closed

- **Source of truth is the DB, not Valkey.** The tenant's `key_version` column (`tenant_encryption_keys`, or `connection_credentials.dek_version` for the wrapped-row side) is authoritative; the Valkey epoch below is a **fast cache of that value**, never an independent source. Cache key includes `dek_version` — a rotation produces a clean miss, never a version-mismatched decrypt.
- **Fast path:** rotation publishes `dek-rotated:{tenant_id}` on the invalidation bus at re-wrap time — every hub-api replica evicts immediately, not on next TTL tick. Because every community in a tenant shares that tenant's one DEK, a single rotation event and a single re-wrap pass cover every `community_connections`/`sources` credential row for that tenant — never a per-community fan-out.
- **Bounded fast-cache path (closes the "pub/sub message missed" gap):** every tenant has a monotonic **epoch counter** — a Valkey `INCR tenant-dek-epoch:{tenant_id}` bumped by the same rotation job, in the same operation that publishes `dek-rotated`, so the two can never disagree about whether a rotation happened. Every cached DEK entry carries the epoch it was resolved under. **On each cache use**, the broker does one cheap `GET tenant-dek-epoch:{tenant_id}` and compares it to the entry's stored epoch: mismatch ⇒ treat as a miss, re-resolve. **Cost:** one Valkey `GET` per resolve call (sub-millisecond) — negligible next to the KMS-unwrap cost a cache hit is avoiding.
- **If the epoch check itself fails** (Valkey unreachable, not just a miss), the cached DEK is **not** trusted indefinitely on the theory that "no answer means no rotation" — the broker re-verifies `key_version` directly against the **primary** DB, with a short TTL (**30s**) on that verified-good state: within 30s of a successful DB-verified check, a subsequent Valkey-unreachable resolve may still use the cache; past 30s without a fresh verification (DB or Valkey), the cached DEK is never reused without re-checking `key_version` against the primary first.
- **Bounded TTL regardless** (10 min) — no indefinite plaintext caching even absent a rotation event.
- **Decrypt failure:** one re-resolve (fresh KMS unwrap + fresh row read, bypassing cache); if that also fails, `resolve()` fails closed — an error, never a stale/partial secret.

### 2.3 OAuth refresh & rotation

Rotation fan-out is O(tenants), never O(communities) or O(links) — one tenant, one DEK, one re-wrap pass covers every community's credential rows under it. Refresh-ahead at ~75% of TTL with per-credential jitter + single-flight lock per `(tenant_id, credential_id)`. Every resolve/refresh audit-logs `{tenant_id, credential_id, caller_service, cache_hit}` — no secret material in the body.

### 2.4 Why not a Rust sidecar

Credential resolution never sits on the per-message hot path (§2.1, §6) — a Rust sidecar would solve a latency problem that doesn't exist once relay-authz is excluded from the broker's path entirely.

---

## 3. Data-plane consumption at scale

### 3.1 Discord Gateway sharding — verified

Source: [Discord Developer Docs — Gateway, Sharding](https://discord.com/developers/docs/events/gateway), fetched 2026-09-28.

- **Hard cap:** 2,500 guilds/shard max (`4010 Invalid Shard` on breach) — a **floor**, `min_shards = ceil(guild_count / 2500)`, not a ratio to hit exactly.
- **max_concurrency** = IDENTIFYs/5s; `rate_limit_key = shard_id % max_concurrency`. Standard bots: `max_concurrency: 1`.
- **IDENTIFY:** 120 events/connection/60s; global **1000/24h**, breach **terminates every session and resets the bot token**.

8 shards for 6,000 **physical guild `sources`** (750/shard, ≪2,500 cap) is **compliant** — more shards than the floor is always allowed, and guild count here means distinct guilds ingested, not distinct community links to them.

| Quantity | Value |
|---|---|
| Guild-backed `sources` | 6,000 |
| Minimum shards | 3 (`ceil(6000/2500)`) |
| Operational shards | 8 (≈750/shard) |
| Cold-start (8 shards, `max_concurrency=1`) | 40s |
| Shard→replica | `shard_id % replica_count`; 8/4 = 2/replica |

**Discord's second inbound path — Interactions webhooks.** Slash-command interactions arrive as their own signed HTTPS webhook (Ed25519 signature over `X-Signature-Ed25519`/`X-Signature-Timestamp`), entirely separate from the Gateway socket. It is a **platform-ingest webhook** in the same family as Twitch EventSub (§4.1) — same shape (platform-issued signature, platform-managed subscription/registration, fixed URL), different crypto (Ed25519, not HMAC-SHA256) and different trigger. Out of this revision's implementation scope, but the receiver design in §4.1/§4.4 is meant to house it without a new architecture when it lands.

### 3.2 Twitch — redesigned on EventSub webhook transport (production), Conduits, IRC bounded, WebSocket dev/test only

Sources: [Twitch — Handling Conduit Events](https://dev.twitch.tv/docs/eventsub/handling-conduit-events/), [Twitch Forum — "Available today: Twitch Chat on EventSub, an API for sending chat, and the Conduit transport method"](https://discuss.dev.twitch.com/t/available-today-twitch-chat-on-eventsub-an-api-for-sending-chat-and-the-conduit-transport-method-for-eventsub/54596), [Twitch — Managing EventSub Subscriptions](https://dev.twitch.tv/docs/eventsub/manage-subscriptions/), [Twitch Forum — cost-based system / RFC 0014](https://discuss.dev.twitch.com/t/eventsub-subscription-limit-cost-based-system-and-limit-field-deprecation/31377), [Twitch Forum — dropped subscriptions and token changes](https://discuss.dev.twitch.com/t/eventsub-managing-dropped-subscriptions-and-token-changes/64089), [Twitch — IRC rate limits](https://dev.twitch.tv/docs/irc/), [Twitch Forum — concurrent join limits for IRC and EventSub](https://discuss.dev.twitch.com/t/giving-broadcasters-control-concurrent-join-limits-for-irc-and-eventsub/54997). Fetched 2026-09-28.

**Build on the existing receiver, don't design a new one.** `core/svc_ingest/eventsub.py`'s `TwitchEventSubHandler`, mounted at `POST /eventsub/twitch/webhook` (`app.py`), already does real HMAC-SHA256 signature verification (constant-time `hmac.compare_digest`, `sha256=` + HMAC over `message_id + timestamp + body`), the `webhook_callback_verification` challenge handshake, and event normalization/fan-out via `fanout.fan_out_event`. This is production-shaped webhook transport already — Conduits/webhook-shards route notifications to this same URL, they don't require a new endpoint. One subscription per physical `sources` row (D2) is what "ingest once" means concretely here: however many communities link a channel, exactly one EventSub subscription exists for it. **Gaps against the requirements, planned as increments (§8):**

| Gap | Today | Fix |
|---|---|---|
| Message-id dedup | Explicitly **not ported** ("Deliberately does NOT port that legacy module's own duplicate-message-id in-memory cache") | Valkey `SET NX PX <ttl>` on `Twitch-Eventsub-Message-Id`, ~15 min TTL (covers Twitch's own redelivery window) |
| Replay window | Not enforced — `timestamp` header is read but never bounds-checked | Reject `Twitch-Eventsub-Message-Timestamp` older than **10 minutes** |
| Revocation handling | Logs a WARN and acks 200; no downstream effect | On `revocation`, mark the affected `sources` row `status='error'` (the ingest credential is what's revoked, so every community linked to that source is affected identically), publish `source-revoked:{source_id}` on the invalidation bus (§6.1) |
| Secret sourcing | Single `TWITCH_EVENTSUB_SECRET` env var, single-tenant (`Config.RUNNER_TENANT_SLUG`) | Per-source secret from the credential broker (§2), keyed by the subscription's `broadcaster_user_id` → `sources.external_id` — the secret belongs to the physical ingest, not any one linking tenant, since one subscription can serve links across multiple tenants |
| Subscription management | Explicitly out of scope ("subscription management is a one-time setup operation... out of scope for this MVP") | Control-plane reconciler, hub-api-owned (below) — never per-replica |
| Rust port | `core/svc_ingest/src/ingest/twitch.rs` is IRC-only; EventSub is an open `// TODO(M5)` seam | Port `eventsub.py`'s verified logic (signature, challenge, normalization) to the Rust receiver as part of M5, carrying the fixes above forward rather than porting the gaps too |

**Why webhook transport, not WebSocket, for production:** a WebSocket EventSub transport is a stateful, per-process persistent connection — losing it means every subscription on it needs re-establishing, and it does not horizontally scale past however many sockets one process/replica can hold. A webhook receiver is stateless HTTP — it scales the same way any other ingress-fronted service does (K8s HPA on request rate), survives pod restarts without losing subscriptions, and needs no reconnect/backoff/session bookkeeping. **WebSocket transport is retained only for local dev/test** (`waddles.core.twitch-websocket-devtest`) where standing up a public HTTPS endpoint isn't convenient.

- **Conduits + webhook shards.** One conduit; each shard's transport is a webhook pointed at the same `/eventsub/twitch/webhook` URL. Confirmed ceiling: **5 enabled conduits/client, 20,000 shards/conduit** — not binding at 3,500-10,000 physical channel `sources`.
- **Subscription management lives in hub-api, not per-replica, and is keyed by `sources`, not `community_connections`.** A new control-plane component (hub-api blueprint or a small dedicated reconciler job) owns: conduit/shard creation, `channel.chat.message` subscription create/renew/delete **per `sources` row** (one subscription regardless of link count), cost-budget tracking (reads `total_cost` back from `GET /eventsub/subscriptions`, alerts at 80% of `max_total_cost`), and `user.authorization.revoke` subscription handling — a dedicated EventSub topic, **subscribed once per client_id, firing per user** as each individual broadcaster revokes the app's authorization. svc-ingest's webhook receiver only *receives and validates deliveries* — it never creates/manages subscriptions.
- **Inline fail-safe, independent of the revoke topic.** Any **401/403 from a Helix or EventSub call made with a given source's ingest credential** (subscription create/renew) or a **community's own outbound credential** (a Send Chat Message call) is itself treated as a revocation signal — the reconciler immediately marks the affected `sources` or `community_connections` row `status='error'` (whichever credential failed) and deletes the affected subscription(s), the same as an explicit `user.authorization.revoke`/`authorization_revoked` notification. This covers the gap where a token becomes invalid without a notification ever arriving.
- **Cost model — verified but ambiguous at the edge, so tracked dynamically either way.** Twitch's documented rule: "by default, the cost of a subscription is 1, but is reduced to 0 if you have a user access token from the channel related to the subscription." Whether the broadcaster's `channel:bot` grant + an App Access Token reaches that cost-0 path, or cost-0 specifically requires the channel's own user access token, is **not conclusively resolved from available docs** — verify against a live subscription-creation response's `cost` field before finalizing budget headroom. The reconciler records every subscription's actual `cost` from Twitch's response (never assumes a constant) and re-reads `total_cost` on any revocation, rather than computing a local delta. Because subscriptions are per `sources` row (D2), the budget scales with **distinct channels ingested**, not with link count — exactly the property that makes "ingest once" a real cost control and not just an internal bookkeeping nicety.
- **Revocation — verified; the reviewer's cost-cascade claim does not match documented Twitch behavior, but the reconciler still acts defensively.** "The `authorization_revoked` status occurs when Twitch revokes your subscription because the user(s) in the condition object revoked their authorization... or changed their password" — a **per-subscription** teardown with its own status field, not a cost change cascading to sibling subscriptions. No documented mechanism produces that cascade. The reconciler nonetheless: (1) on either `user.authorization.revoke` or an `authorization_revoked` notification, **immediately calls the Delete Subscription API** on the affected subscription (idempotent) and marks the affected row `status='error'`; (2) **re-reads `total_cost` from Twitch** after the delete rather than computing a local delta, so the ledger self-corrects regardless of which cost model turns out to be true. **Cost-budget monitoring:** the reconciler polls `total_cost`/`max_total_cost` on a fixed interval (proposed 5 min), targets **≤80% headroom** as the alert threshold (paging, not just a log line), and the mitigation plan past that threshold is pre-committed, not improvised: request a Twitch cost-budget increase for the client id, or split subscription creation across a second registered Twitch client-id (each gets its own independent 10,000-default budget) — tracked as part of Increment 10 (§8), well before the 10,000-channel target.
- **IRC — bounded, and outbound moves off it entirely.** Twitch enforces a **100-concurrent-chat-room cap per bot account**, no longer waived for verified bots. IRC is scoped to exactly the **≤100-channel unauthorized/trial pool** (channels whose broadcaster hasn't yet completed OAuth authorization). **Outbound chat sends use the Send Chat Message Helix API** instead of IRC `PRIVMSG` — outbound scales with the same per-`community_connections` authorization model as inbound, not with IRC's join cap.
- **Capacity math, 3,500 physical channels with headroom to 10,000:**

| Quantity | Value |
|---|---|
| Conduits / shards | 1 conduit, 8-12 shards (ceiling: 5 × 20,000 — not binding) |
| `max_total_cost` used, 3,500 channels | ~3,500 worst case (cost=1); possibly less (cost-0 path unverified) — **scales with distinct channels, not link count** |
| `max_total_cost` used, 10,000-channel target | 10,000 worst case — **zero headroom**; request a budget increase or split across a 2nd client-id ahead of the 80% alert |
| Webhook receiver throughput | worst case ~10,000 channels × 1 msg/s peak ≈ 10,000 req/s across all replicas — sized via HPA on request-rate/CPU |
| IRC pool | ≤100 channels (Twitch-enforced cap), 20 joins/10s |
| Outbound | Send Chat Message API (stateless), not IRC |

### 3.3 Hot-add/remove: watermark poll + supervisor

Same pattern as `svc_action::bundle_loader`: poll `ix_sources_shard_scan`'s cheap projection (proposed 15s interval — tighter than `bundle_loader`'s 300s default, since this poll doubles as the revocation fallback, §6.1) for physical shard/subscription assignment, and `community_connections` for the link-level fan-out set; hash via `WatermarkTracker`, only recompute on change.

### 3.4 svc-action outbound connection selection

Resolves the linking `community_connections` row from `app_outbound_bindings` (§6) — never from bundle-supplied identifiers — then calls the credential broker for the live token: **`community_connections.credential_id` if that link has its own outbound credential, else `sources.credential_id`** (the shared ingest/default identity). This is the concrete answer to "which credential replies for an event on a shared source" — it's resolved per the triggering app's own community link, never a single global choice.

### 3.5 Capacity table

| Dimension | Target | Design number |
|---|---|---|
| Tenants | 100s | 300 |
| Communities | 10,000s | 20,000 |
| App-bundle installs | 10,000s | ~60,000 rows |
| **Physical `sources`** | ~10,000 | 6,000 Discord / 3,500 Twitch / 500 other |
| **`community_connections` links** | higher than source count (M:N) | illustrative ~1.5× average fan-out ≈ 15,000 links (most sources link to exactly one community; a minority — popular/shared channels, multi-community tenants — link to several) |
| Discord shards | — | 8 (750/shard, floor 3), 4 replicas, 40s cold-start — sized on distinct guild `sources`, not link count |
| Twitch conduits/shards | — | 1 conduit, 8-12 webhook shards (ceiling 5×20,000) — sized on distinct channel `sources` |
| Twitch `max_total_cost`, 3,500→10,000 channels | ≤10,000 default | 3,500 → 10,000 (zero headroom at 10k) — **channel count, not link count** |
| Twitch webhook receiver req/s, worst case | — | ~10,000 req/s across replicas, HPA-sized |
| IRC pool | Twitch-enforced ≤100 | unauthorized/trial channels only |
| KMS unwrap calls, steady state | — | ~300 tenants / 10-min DEK TTL ≈ 0.5/sec — **tenant count, not link or community count** |
| KMS unwrap calls, cold start (batched) | — | ≤900 one-time, 30s jittered warm-up |
| DEK rotation fan-out | — | O(tenants)=300, `dek-rotated`-event-driven, one rewrap pass covers every community's rows under that tenant |
| Relay-authz cache entries | — | 20,000-100,000, sized off `app_outbound_bindings` × `community_connections` join cardinality, a few MB/replica |
| Relay-authz DB hits/message | O(1) required | **0** |
| Revocation latency, fast path | — | sub-second |
| Revocation latency, degraded fallback | — | `max(replica_lag, 15s poll)` |

---

## 4. Inbound webhook receivers: platform-ingest vs. generic per-app

Two receivers, deliberately kept separate — different auth, different secrets, different rate limits, different trust model.

### 4.1 Platform-ingest webhooks (Twitch EventSub today; Discord Interactions, future)

**Path:** Twitch EventSub → `POST /eventsub/twitch/webhook` (existing, §3.2) → normalize → look up the delivering `sources` row → fan out to every linked `community_connections` row → every app bound to each via `app_source_bindings` (now keyed by `community_connection_id`, generalizing migration 0025's pattern — §6). **Trust model:** the sender is the platform itself (Twitch/Discord), signature-verified against a secret Twitch/Discord issued at subscription-creation time, subscription lifecycle managed by our own control-plane reconciler (§3.2) keyed by `sources`, not by any one link — the receiver never has to guess who's calling, only verify the signature, dedup/replay-guard the delivery, and fan it out to every current link.

**The receiver's request-handling path is fixed and deliberately thin: verify signature → dedup check → `XADD` to the partition stream(s) → return 2xx. No inline processing** — normalization, the source→community fan-out, bundle dispatch, and everything downstream happen out of the request's critical path, in the consumers reading the stream(s). This is what keeps the receiver's own p99 **well under Twitch's delivery deadline** regardless of how many communities are linked to the delivering source — the receiver's latency budget is bounded by Valkey round trips (dedup check + `XADD`), not by fan-out width.

### 4.2 Generic per-app inbound webhooks (new)

For a third-party sender a tenant wants to feed directly into one specific app bundle — not a pre-built platform connector.

- **Route:** `POST /hooks/v1/{webhook_id}` — `webhook_id` is an **opaque, unguessable ≥128-bit id** issued by hub-api at creation time. It resolves server-side, via a read-only cached view of `sources` (`external_kind='webhook'`, `external_id=webhook_id`) joined to its `community_connections` link, directly to `(tenant_id, community_id, app_id)` — **no caller-supplied tenant/app-id header is ever trusted for routing.** Each registration creates one `sources` row (`credential_identity` = the `webhook_id` itself, since a generic webhook has no shareable physical identity across tenants the way a Twitch channel does) and, ordinarily, exactly one `community_connections` link to the target app's community — the M:N model *permits* linking one such source to more than one community's app if a tenant deliberately wants the same third-party delivery fanned into several installs, but that is the exception, not the common case.
- **Auth — asymmetric-first, so a hub-api breach can never forge an inbound delivery** (we store only the sender's *public* key/verification material). Three modes, in preference order:
  - **Mode A (preferred): RFC 9421 HTTP Message Signatures, `ecdsa-p256-sha256` (ES256)** — FIPS-approved, the Enterprise/FedRAMP-required mode. Covered components, minimum: `@method`, `@target-uri` (or `@path`), `content-digest` (RFC 9530, over the raw body), `content-type`, and a `created` timestamp bound into the signature. `keyid` in the `Signature-Input` resolves to a registered key (below).
  - **Mode B: Standard Webhooks `v1a` (Ed25519)** — same `webhook-id`/`webhook-timestamp`/`webhook-signature` headers as `v1`, asymmetric signature instead of HMAC.
  - **Mode C (fallback only): Standard Webhooks `v1` HMAC-SHA256** over `{id}.{timestamp}.{body}`, for senders that genuinely cannot do asymmetric signing — **opt-in per webhook**, flagged as the weakest of the three modes in the admin UI and audit log. Secret sourced from the credential broker (§2, held on the `community_connections.credential_id` link — the per-community webhook secret) and never logged; constant-time compare.
  - **Shared regardless of mode:** **±5 minute replay window** on the signed timestamp; **dedup on the delivery id** via Valkey `SET NX` with TTL.
- **Key registration (hub-api, per webhook):** either **pinned public keys** (multiple concurrent, keyed by `kid`, zero-delivery-loss rotation) or a **pinned JWKS URL**. **Algorithm is fixed per registered key at registration time, never negotiated from the incoming request** — closes the classic algorithm-confusion hole. Mode C's HMAC secret is the only credential-broker-backed row here (on `community_connections`, per-community as required); A/B's public keys/JWKS URLs are not secrets and live directly on the `sources`/`community_connections` mapping.
- **The JWKS URL fetch is itself an SSRF surface and is hardened the same way `svc_action::egress`'s outbound guard already is** — HTTPS only; **resolve-then-connect** (DNS resolved once, connection pinned to that resolved address, never re-resolved between check and connect); the resolved address checked against the exact forbidden-range set `egress.rs::is_forbidden_address` already encodes — loopback, private, link-local, unspecified, multicast, cloud-metadata — **including disguised forms** via `embedded_ipv4` canonicalization (IPv4-mapped IPv6, NAT64). **The TLS handshake still validates the certificate against the original hostname** (SNI set to the hostname, not the pinned IP, certificate hostname verification against that same hostname) — pinning the connection defeats DNS-rebinding, a certificate check against a mismatched name would defeat HTTPS entirely. **Automatic redirects are disabled**; any redirect is re-validated through this same guard from scratch. **Response size capped** at 64KB; **timeouts ≤2s**; the fetched key set **cached with a TTL**.
- **Delivery:** the validated payload is wrapped as a `PlatformEvent` (`platform='webhook'`) and `XADD`'d to the partition stream the bound app consumes — no fan-out-by-tag, the binding is already effectively 1:1 (or the deliberate small fan-out above). **202 Accepted** once validation + `XADD` succeed. **Body-size limit** (e.g. 256KB) and **strict `Content-Type: application/json` allowlist** reject anything else before signature verification even runs. **Uniform 401/404 policy:** an unknown `webhook_id` and a known-but-badly-signed one return the **same** response shape and timing — the endpoint never reveals whether a given id exists.

### 4.3 Outbound platform webhooks (waddles → external endpoint)

Symmetric with §4.2's asymmetric-first inbound design: every outbound webhook is signed with **ES256 (RFC 9421 HTTP Message Signatures)** using a platform signing key. Private key material lives in **KMS/HSM** — prefer KMS-side asymmetric signing (the private key never leaves the KMS/HSM boundary; hub-api sends the digest, gets back a signature). The public verification key is published at a platform JWKS endpoint that receivers pin or fetch; **rotation** = publish under a new `kid` first, sign new deliveries with it, retire the old key only once receivers have rolled over — the same zero-delivery-loss shape as inbound key rotation (§4.2).

### 4.4 Shared hardening (all receivers)

Both are internet-facing and share a hardening baseline, layered per-path where the trust models diverge:

- **Rate limiting** — per-tenant + per-source/webhook token buckets (§5), plus a coarser global bucket per path at the ingress layer. Twitch's sender population is one known platform; `/hooks/v1`'s is arbitrary third parties, so its global bucket is stricter and its per-webhook bucket is mandatory.
- **Body-size limits and strict content-type allowlists** on both paths, checked before any signature computation.
- **Deployment topology:** both mount on svc-ingest — stateless HTTP handlers. Exposed as **two distinct Ingress path rules** (`/eventsub/*`, `/hooks/v1/*`) so rate limits/WAF rules are configured independently per path; **CiliumNetworkPolicy** scopes external ingress to exactly these two paths/ports, default-deny everything else, per `security.md`'s Kubernetes Network Security baseline.

---

## 5. Multi-tenant isolation per pod

- **Per-tenant source/link quotas**, enforced at registration time by hub-api, tied to license tier — capping both distinct `sources` a tenant's communities may originate/custom-ingest and total `community_connections` links.
- **Per-tenant outbound rate limiting** — `svc_action`'s existing `UsageBatcher` extended from metering-only to a token-bucket keyed by `tenant_id`, independent of platform-level limits (§3).
- **Per-webhook and per-tenant rate limits on `/hooks/v1`** (§4.4).
- **Fair shard/connection scheduling** — weighted by recent message-volume, not blind `id % replica_count`, so one active tenant's `sources` don't concentrate on one replica. Deploy-time rebalance only.
- **Noisy-neighbor isolation in the executor** — a per-tenant semaphore on in-flight `invoke`s, alongside the existing per-`app_id` `EgressGuard` bucket extended to relay pushes, tenant ceiling layered on top of the app-level one.

---

## 6. Relay authorization

**Today:** `handle_relay` resolves `InvokeScope{tenant, community, app_id}` per-call but takes Twitch `channel` straight from the bundle's `message_json`, unchecked. Discord already avoids this (`scope.origin_channel_id`, reply-only).

**Design:** `app_outbound_bindings`, keyed by `community_connections.id` (which itself carries the tenant/community/source):

```sql
CREATE TABLE app_outbound_bindings (
    tenant_id INTEGER NOT NULL,
    community_id INTEGER NOT NULL DEFAULT 0,
    app_id VARCHAR(255) NOT NULL,
    platform VARCHAR(50) NOT NULL,
    community_connection_id BIGINT NOT NULL REFERENCES community_connections(id),
    PRIMARY KEY (tenant_id, community_id, app_id, platform, community_connection_id)
);
```

`app_source_bindings` (migration 0025's inbound pattern) is re-keyed the same way, replacing its old `platform`/`source_id` string columns with a single FK: `PRIMARY KEY (tenant_id, community_id, app_id, community_connection_id)` — the platform/external-id values needed to build the stream key are resolved by joining through `community_connections` → `sources` rather than duplicated as denormalized strings.

Deny + audit-log unless `(tenant, community, app_id, provider, channel)` resolves to a `community_connections` row (joined to its `sources.external_id == channel`) that also has an `app_outbound_bindings` row for `scope.app_id`. **O(1)**: svc-action loads the join into an in-memory map via the watermark-poll supervisor (§3.3) — a hash-map lookup on the hot path, never a query.

### 6.1 Revocation latency — bounded, with a fast path

- **Fast-path invalidation** — hub-api publishes `community-connection-revoked:{community_connection_id}` (link-level: disable, binding-delete) and `source-revoked:{source_id}` (ingest-level: the shared credential itself was revoked, §3.2) on Valkey pub/sub; data-plane replicas evict the affected entries immediately (**sub-second typical**). The 15s poll (§3.3) is the self-healing fallback.
- **A Valkey blip must not clear the in-memory authz cache — availability of the *notification channel* is not availability of the *cache*.** The cache is populated by the watermark-poll supervisor (§3.3) and merely *refreshed early* by pub/sub when reachable; losing pub/sub degrades the cache to poll-only, it does not empty it. **Last-known-good ALLOW verdicts remain valid until their freshness bound — one poll interval, 15s** — measured from when the entry was last confirmed (pub/sub event or poll tick), not process start. **DENY verdicts remain valid indefinitely.** Only an ALLOW entry crossing its 15s bound *while* the poll itself is also failing (Postgres unreachable, not just Valkey) fails closed, pending re-verification.
- **Cold start populates from the RO-replica poll, never from Valkey.** A freshly started replica has no cache and no pub/sub history to trust — its first population is always a full poll read against the RO replica (§3.3); Valkey only ever tells a replica "go re-poll early," it is never the origin of a verdict.
- **Bounded cache, not unbounded growth.** Capped by both **max entry count** and **max byte size** (sized generously against §3.5's estimate, with headroom), evicted **LRU** past that cap.
- **Recovery is jittered.** On Valkey reconnect after an outage, a replica re-poll-populates first (as on cold start), then resumes pub/sub, jittered across replicas so a fleet-wide recovery doesn't stampede the RO replica.
- **An unknown key after cold start is evaluated via the poll path, not assumed.** A tuple absent from the map (never seen, or evicted) falls through to a bounded on-demand poll-path check; if that can't resolve it, the verdict is **deny**.

### 6.2 Rollout: shadow mode, not a permanent kill switch

**Security controls must never be switchable off in steady state** — relay-authz ships with **no `disable-relay-authz` flag, ever**:

1. **Shadow/log-only mode** (bounded soak, proposed 2-4 weeks): evaluates the check, **logs + alerts every would-be-deny at WARN**, never blocks.
2. **Enforcement** once the soak shows clean results — `handle_relay` starts returning `denied("channel_not_bound", ...)` for real.
3. **The shadow-mode flag and its branch are deleted from the codebase** at that point — never left in place as a lever.

---

## 7. Feature flag rollout — opt-out kill-switches, not default-OFF (except relay-authz, §6.2)

Per the house convention (`core/svc_action/src/flags.rs::DISABLE_DB_BUNDLE_CONFIG_FLAG`):

| Flag | Mechanism it can disable | Default |
|---|---|---|
| `waddles.core.disable-connections-registry` | svc-ingest/svc-action read from `sources`/`community_connections`/broker | ON |
| `waddles.core.disable-credential-broker` | broker-mediated resolution | ON |
| `waddles.core.disable-fast-revocation` | Valkey fast-path invalidation (§6.1); **latency only**, never the check itself | ON |
| `waddles.core.disable-tenant-quotas` | per-tenant rate limiting/fair scheduling (§5) | ON |
| `waddles.core.twitch-websocket-devtest` | dev/test-only WebSocket transport (§3.2) — **never enabled in beta/gamma/prod** | OFF |

Relay-authz has **no row here** — see §6.2.

---

## 8. Increment plan

Security-critical items first; each step independently shippable.

1. **`connection_credentials` table + RBAC grants** (`privileges: []` for every non-hub_api role).
2. **Credential broker, reading the primary** (§2.1) — versioned DEK cache with the epoch-check + `dek-rotated` fast-path invalidation (§2.2), per-replica stale-while-revalidate secret cache, batch resolve, circuit breaker — behind `waddles.core.disable-credential-broker`.
3. **`sources` + `community_connections` tables + migration/backfill from `ingest_sources`** (§1) — `connection_service.py` replaced by `source_service.py`/`community_connection_service.py` — behind `waddles.core.disable-connections-registry`.
4. **Twitch EventSub receiver, built in Rust from the start.** In-line of traffic per house rules, closes `core/svc_ingest/src/ingest/twitch.rs`'s open `TODO(M5)` seam directly, keyed by `sources` (one subscription per physical channel regardless of link count). Ports `eventsub.py`'s already-correct logic into Rust and builds the gaps in as Rust code: message-id dedup, the 10-minute replay window, revocation → `sources.status='error'` + fast-path invalidation, per-source broker-sourced secrets. Scheduled early — before the scale-oriented work below.
5. **Twitch control-plane reconciler** in hub-api: conduit/shard setup keyed by `sources`, subscription create/renew/delete, immediate Delete-on-revoke (§3.2), cost-budget tracking + 80%-headroom alert with the multi-client-id/limit-increase plan pre-committed, `user.authorization.revoke` handling, broadcaster `channel:bot` OAuth grant flow at `community_connections` registration.
6. **Generic per-app webhook receiver** (`POST /hooks/v1/{webhook_id}`, §4.2): RFC 9421/ES256 and Standard Webhooks `v1a`/Ed25519 as preferred asymmetric modes, HMAC `v1` as opt-in fallback; pinned-key/JWKS registration with fixed per-key algorithm and the SSRF-hardened JWKS fetch; replay window, dedup, 202-accept + XADD, uniform 401/404 policy. Outbound platform-webhook signing (§4.3) ships alongside it.
7. **Relay-authz stage-side check, shipped in shadow/log-only mode first** (§6.2), keyed by `community_connections`/`app_outbound_bindings` — no kill switch; bounded soak, then permanent enforcement with the shadow branch deleted. Fast-path revocation ships alongside it, behind `waddles.core.disable-fast-revocation`; the poll-driven cache (§6.1) ships in the same increment.
8. **Discord shard supervisor + Twitch conduit/shard supervisor**, watermark-poll hot-add/remove keyed by `sources`, weighted fair scheduling (§5) — behind `waddles.core.disable-tenant-quotas` for the weighting.
9. **IRC's Send Chat Message API swap-out for outbound** (§3.2).
10. **Scale hardening (deferred):** partitioning if per-tenant row counts approach 10^6; verify the Twitch cost-0 condition empirically ahead of the 80%-of-`max_total_cost` alert; Discord Interactions webhook receiver (§3.1) reusing §4.4's shared hardening.

---

## 9. Implementation conditions

This design is **approved with conditions** — the following are not optional cleanup, they are load-bearing parts of the design that implementation PRs must actually deliver, and PR review is where they get verified:

1. **Twitch — inline fail-safe is mandatory, not just the notification-based path.** Any 401/403 from a Helix/EventSub call using a `sources` or `community_connections` credential marks that row revoked and deletes its subscriptions immediately (§3.2) — a PR that wires only `user.authorization.revoke`/`authorization_revoked` handling without this inline check does not satisfy the design.
2. **DEK version — the epoch check must run on every cache use, not just exist in code.** The DB `key_version` is authoritative; Valkey's epoch is a fast cache with a defined 30s max-unverified window on epoch-check failure (§2.2) — a PR that treats the pub/sub event as sufficient on its own does not satisfy the design.
3. **Broker reads the primary — never wire it to an RO replica later "for load reasons."** Any change routing credential resolution through a replica reopens the exact staleness hole §2.1/§2.2 close; treat this as a hard constraint in review, not a tunable.
4. **Twitch EventSub receiver ships in Rust, not Python-first.** A PR extending `eventsub.py` further before the Rust port lands should be questioned against Increment 4's ordering (§8) — the in-line-of-traffic rule applies regardless of how convenient a Python patch is.
5. **JWKS fetch hardening is one guard, reused, not reimplemented.** The SSRF checks (§4.2) must call into (or be extracted alongside) `svc_action::egress`'s existing `is_forbidden_address`/`embedded_ipv4` logic — a parallel, hand-rolled private-range check in hub-api is a regression risk (a second place to keep the forbidden-range list in sync) and should be flagged in review.
6. **Relay-authz cache must demonstrate the stated availability bound under test**, not just describe it: a test that kills Valkey mid-run and asserts ALLOW verdicts keep serving for the 15s bound (and DENY indefinitely) before falling back to poll-driven behavior is a merge gate for Increment 7, not a nice-to-have.
7. **The `sources`/`community_connections` split must be exercised by a multi-community test before merge** — at minimum, one physical source linked to two communities in the same tenant and one linked across two different tenants, asserting exactly one platform-side subscription/shard exists in both cases and that each community's own `app_source_bindings`/`app_outbound_bindings` and outbound-credential resolution are independent.

---

*Illustrative sizing numbers are assumptions for capacity math, not committed targets. Discord/Twitch facts are cited to official docs/forum posts fetched 2026-09-28; the exact cost-0 condition for `channel:bot`-authorized conduit subscriptions was not conclusively confirmed and must be verified against a live subscription-creation response before finalizing the 10,000-channel cost budget.*
