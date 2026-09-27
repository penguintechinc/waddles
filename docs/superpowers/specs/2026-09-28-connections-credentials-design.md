# Waddles v3 CONNECTIONS Registry, Credentials & Relay Authorization — Design

**Date:** 2026-09-28 (rev. 5 — Twitch redesigned on EventSub webhook transport built on the existing `core/svc_ingest/eventsub.py` receiver, generic per-app webhook receiver added with asymmetric-first signing (RFC 9421/ES256, Standard Webhooks v1a/Ed25519, HMAC v1 as opt-in fallback), outbound webhook signing, IRC scope narrowed to a bounded pool, subscription management moved to a control-plane reconciler)
**Status:** Proposed design
**Scope:** `hub_api` (registry RW + credential broker + Twitch subscription reconciler), `core/svc_ingest` (both webhook receivers + IRC pool + Discord Gateway), `core/svc_action` (outbound relay/authz + Twitch Send Chat Message calls), `core/svc_process` (registry RO consumption), `core/bundle_executor` (relay-authz enforcement point)
**Principle:** hub-api is the only read-write path for connections and credentials. The data plane reads connection metadata from a read-only replica/role and never sees plaintext secrets directly — it resolves live credentials through a broker call.

**Sizing target:** ~300 tenants, ~20,000 communities, ~60,000 app-bundle installs, ~10,000 connections/sources split (illustratively) 6,000 Discord guilds / 3,500 Twitch channels / 500 other platforms, with explicit headroom to 10,000 total Twitch channels.

---

## 1. Registry schema evolution: `ingest_sources` → `connections`

`ingest_sources` (migration 0020) is inbound-only, has no `direction`, `status`, `external_kind`, or `created_by`, and its `secret_ciphertext`/`secret_iv` columns sit in the same table the RO data-plane roles will eventually be granted `SELECT` on. `connections` generalizes and replaces it — including a new `kind='webhook'` row shape for the generic per-app webhook receiver (§4.2).

```sql
-- migration 0026_connections_registry
CREATE TABLE connections (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    community_id INTEGER REFERENCES communities(id),      -- NULL = tenant-wide
    platform VARCHAR(50) NOT NULL,                         -- 'discord' | 'twitch' | 'webhook' | ...
    external_kind VARCHAR(20) NOT NULL,                    -- 'guild' | 'channel' | 'account' | 'webhook'
    external_id VARCHAR(255) NOT NULL,                     -- guild id / twitch broadcaster id / opaque webhook_id
    label VARCHAR(255) NOT NULL,
    direction VARCHAR(10) NOT NULL
        CHECK (direction IN ('inbound', 'outbound', 'both')),
    status VARCHAR(20) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'healthy', 'degraded', 'error', 'disabled')),
    last_health_at TIMESTAMPTZ,
    last_health_error TEXT,
    shard_assignment JSONB,        -- {"conduit_id":..,"shard_id":..,"replica":"svc-ingest-1"} -- svc-ingest-owned
    rate_limit_meta JSONB,         -- platform-specific budget bookkeeping (EventSub cost observed, etc.)
    credential_id BIGINT REFERENCES connection_credentials(id),  -- NULL = shared platform credential
    mapping JSONB,                 -- for kind='webhook': bound app_id, event_type mapping
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_by VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, platform, external_id, direction)
);

CREATE INDEX ix_connections_shard_scan ON connections (platform, enabled, id);
CREATE INDEX ix_connections_tenant_community ON connections (tenant_id, community_id);
```

**Decisions**

- **D1 — `connections` supersedes `ingest_sources`; migrate, don't dual-write.** Backfill every `ingest_sources` row (`direction='inbound'`), then `ingest_source_service.py`'s callers move to `connection_service.py`. `ingest_sources` drops in a follow-up migration.
- **D2 — `credential_id` is a nullable FK, not inline columns** — keeps secrets in one narrow table (§2), never in the table admin UIs list/paginate.
- **D3 — `external_kind` disambiguates the id namespace per platform** (including `'webhook'` for opaque webhook ids, §4.2) so adding a platform/receiver type never means a migration.
- **D4 — watermark poll, not a trigger-maintained counter** — mirrors `core/bundle_active_set`'s `read_watermark`/`WatermarkTracker`.
- **No partitioning at this scale** — ~10,000 `connections` rows, orders of magnitude below where Postgres indexing degrades.

---

## 2. Credentials

**Never plaintext in a replica-exposed table.**

| Option | Verdict |
|---|---|
| A. Envelope encryption, RO-readable ciphertext | **Rejected** — RO role still has a path to plaintext given a decrypt step. |
| B. Secret references (Vault/K8s Secret per connection) | **Rejected at scale** — 10,000 independently-rotated Vault paths/Secrets is its own sprawl problem, and no clean rewrite-every-few-hours story for OAuth refresh tokens. |
| **C. Narrow credentials table, RO role zero grant, credential broker mediates** | **Chosen** — matches `rbac-matrix.yaml`'s already-checked-in `privileges: []` for `svc_ingest`/`svc_action` on `ingest_sources`. |

```sql
CREATE TABLE connection_credentials (
    id BIGSERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
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

Envelope encryption: per-tenant DEK wrapped by an app-wide KEK (platform-managed KMS baseline; Enterprise upsell = customer/external KMS wraps the KEK, additive).

### 2.1 Credential broker — HA, stateless replicas, no split-brain

The broker is a hub-api blueprint, not a singleton — hub-api runs ≥2 replicas with a `PodDisruptionBudget`. **Every replica is stateless w.r.t. credentials**: `connection_credentials` (Postgres) plus the KMS-wrapped DEK is the sole shared source of truth; no replica holds state another lacks, so there is no split-brain to resolve — every replica independently derives the same answer from the same row plus the same KMS unwrap.

Two cache layers, both pure read-through, both invalidated by the **same Valkey pub/sub bus** the relay-authz fast path uses (§6.1): hub-api's per-replica DEK cache (`dek-rotated:{tenant_id}`, §2.2) and each data-plane replica's per-connection secret cache (`connection-revoked:{connection_id}`). A missed event is caught by the bounded watermark-poll fallback (§3.3) — no replica-to-replica RPC or leader election needed.

Credential resolution happens only at **connect-time and refresh-ahead time**, never per message (relay-authz, the per-message path, is fully client-side cached — §6):

- **Per-replica stale-while-revalidate cache.** On broker-call failure, serve past TTL up to `min(now + max_stale_window, expires_at)`; `max_stale_window` = 2× the refresh-ahead interval.
- **Batched cold-start resolve** — one `POST /internal/v1/credentials/resolve-batch` per replica instead of N individual calls.
- **Circuit breaking** — standard breaker; an open breaker serves stale-cache, never blocks the caller.

### 2.2 DEK cache: versioned, bounded, fails closed

- Cache key includes `dek_version`, not just `tenant_id` — a rotation produces a clean miss, never a version-mismatched decrypt.
- Rotation publishes `dek-rotated:{tenant_id}` on the invalidation bus at re-wrap time — every hub-api replica evicts immediately, not on next TTL tick.
- **Bounded TTL regardless** (10 min) — no indefinite plaintext caching even absent a rotation event.
- **Decrypt failure:** one re-resolve (fresh KMS unwrap + fresh row read, bypassing cache); if that also fails, `resolve()` fails closed — an error, never a stale/partial secret.

### 2.3 OAuth refresh & rotation

Rotation fan-out is O(tenants), never O(connections). Refresh-ahead at ~75% of TTL with per-connection jitter + single-flight lock per `(tenant_id, connection_id)`. Every resolve/refresh audit-logs `{tenant_id, connection_id, caller_service, cache_hit}` — no secret material in the body.

### 2.4 Why not a Rust sidecar

Credential resolution never sits on the per-message hot path (§2.1, §6) — a Rust sidecar would solve a latency problem that doesn't exist once relay-authz is excluded from the broker's path entirely.

---

## 3. Data-plane consumption at scale

### 3.1 Discord Gateway sharding — verified

Source: [Discord Developer Docs — Gateway, Sharding](https://discord.com/developers/docs/events/gateway), fetched 2026-09-28.

- **Hard cap:** 2,500 guilds/shard max (`4010 Invalid Shard` on breach) — a **floor**, `min_shards = ceil(guild_count / 2500)`, not a ratio to hit exactly.
- **max_concurrency** = IDENTIFYs/5s; `rate_limit_key = shard_id % max_concurrency`. Standard bots: `max_concurrency: 1`.
- **IDENTIFY:** 120 events/connection/60s; global **1000/24h**, breach **terminates every session and resets the bot token**.

8 shards for 6,000 guilds (750/shard, ≪2,500 cap) is **compliant** — more shards than the floor is always allowed.

| Quantity | Value |
|---|---|
| Guild-backed connections | 6,000 |
| Minimum shards | 3 (`ceil(6000/2500)`) |
| Operational shards | 8 (≈750/shard) |
| Cold-start (8 shards, `max_concurrency=1`) | 40s |
| Shard→replica | `shard_id % replica_count`; 8/4 = 2/replica |

**Discord's second inbound path — Interactions webhooks.** Slash-command interactions arrive as their own signed HTTPS webhook (Ed25519 signature over `X-Signature-Ed25519`/`X-Signature-Timestamp`), entirely separate from the Gateway socket. It is a **platform-ingest webhook** in the same family as Twitch EventSub (§4.1) — same shape (platform-issued signature, platform-managed subscription/registration, fixed URL), different crypto (Ed25519, not HMAC-SHA256) and different trigger (a user invoking a command, not a chat/channel event). Out of this revision's implementation scope, but the receiver design in §4.1/§4.4 (stateless, horizontally scaled, shared hardening) is meant to house it without a new architecture when it lands.

### 3.2 Twitch — redesigned on EventSub webhook transport (production), Conduits, IRC bounded, WebSocket dev/test only

Sources: [Twitch — Handling Conduit Events](https://dev.twitch.tv/docs/eventsub/handling-conduit-events/), [Twitch Forum — "Available today: Twitch Chat on EventSub, an API for sending chat, and the Conduit transport method"](https://discuss.dev.twitch.com/t/available-today-twitch-chat-on-eventsub-an-api-for-sending-chat-and-the-conduit-transport-method-for-eventsub/54596), [Twitch — Managing EventSub Subscriptions](https://dev.twitch.tv/docs/eventsub/manage-subscriptions/), [Twitch Forum — cost-based system / RFC 0014](https://discuss.dev.twitch.com/t/eventsub-subscription-limit-cost-based-system-and-limit-field-deprecation/31377), [Twitch Forum — dropped subscriptions and token changes](https://discuss.dev.twitch.com/t/eventsub-managing-dropped-subscriptions-and-token-changes/64089), [Twitch — IRC rate limits](https://dev.twitch.tv/docs/irc/), [Twitch Forum — concurrent join limits for IRC and EventSub](https://discuss.dev.twitch.com/t/giving-broadcasters-control-concurrent-join-limits-for-irc-and-eventsub/54997). Fetched 2026-09-28.

**Build on the existing receiver, don't design a new one.** `core/svc_ingest/eventsub.py`'s `TwitchEventSubHandler`, mounted at `POST /eventsub/twitch/webhook` (`app.py`), already does real HMAC-SHA256 signature verification (constant-time `hmac.compare_digest`, `sha256=` + HMAC over `message_id + timestamp + body`), the `webhook_callback_verification` challenge handshake, and event normalization/fan-out via `fanout.fan_out_event`. This is production-shaped webhook transport already — Conduits/webhook-shards route notifications to this same URL, they don't require a new endpoint. **Gaps against the requirements, called out by the module's own docstring or by inspection, planned as increments (§8):**

| Gap | Today | Fix |
|---|---|---|
| Message-id dedup | Explicitly **not ported** ("Deliberately does NOT port that legacy module's own duplicate-message-id in-memory cache") | Valkey `SET NX PX <ttl>` on `Twitch-Eventsub-Message-Id`, ~15 min TTL (covers Twitch's own redelivery window) |
| Replay window | Not enforced — `timestamp` header is read but never bounds-checked | Reject `Twitch-Eventsub-Message-Timestamp` older than **10 minutes** |
| Revocation handling | Logs a WARN and acks 200; no downstream effect | On `revocation`, mark the `connections` row `status='error'`, publish `connection-revoked:{connection_id}` on the invalidation bus (§6.1) |
| Secret sourcing | Single `TWITCH_EVENTSUB_SECRET` env var, single-tenant (`Config.RUNNER_TENANT_SLUG`) | Per-connection secret from the credential broker (§2), keyed by the subscription's `broadcaster_user_id` → `connections.tenant_id` |
| Subscription management | Explicitly out of scope ("subscription management is a one-time setup operation... out of scope for this MVP") | Control-plane reconciler, hub-api-owned (below) — never per-replica |
| Rust port | `core/svc_ingest/src/ingest/twitch.rs` is IRC-only; EventSub is an open `// TODO(M5)` seam | Port `eventsub.py`'s verified logic (signature, challenge, normalization) to the Rust receiver as part of M5, carrying the fixes above forward rather than porting the gaps too |

**Why webhook transport, not WebSocket, for production:** a WebSocket EventSub transport is a stateful, per-process persistent connection — losing it means every subscription on it needs re-establishing, and it does not horizontally scale past however many sockets one process/replica can hold. A webhook receiver is stateless HTTP — it scales the same way any other ingress-fronted service does (K8s HPA on request rate), survives pod restarts without losing subscriptions (Twitch redelivers on a missed 2xx, and Conduit shard reassignment is Twitch-side, not ours), and needs no reconnect/backoff/session bookkeeping. **WebSocket transport is retained only for local dev/test** (`waddles.core.twitch-websocket-devtest` — an internal dev convenience, never a production connection strategy) where standing up a public HTTPS endpoint isn't convenient.

- **Conduits + webhook shards.** One conduit; each shard's transport is a webhook pointed at the same `/eventsub/twitch/webhook` URL (Twitch's own conduit model routes by channel→shard affinity server-side — "an attempt is made to send notifications for a particular channel ID to the same shard for consistency" — but since every shard resolves to the same stateless receiver URL, shard identity matters for Twitch's own internal bookkeeping and shard-level health/retry, not for our routing). Confirmed ceiling: **5 enabled conduits/client, 20,000 shards/conduit** — not binding at 3,500-10,000 channels.
- **Subscription management lives in hub-api, not per-replica.** A new control-plane component (hub-api blueprint or a small dedicated reconciler job) owns: conduit/shard creation, `channel.chat.message` subscription create/renew/delete per authorized connection, cost-budget tracking (reads `total_cost` back from `GET /eventsub/subscriptions`, alerts at 80% of `max_total_cost`), and `user.authorization.revoke` subscription handling (a dedicated EventSub topic notifying when a user revokes the app's authorization — subscribed proactively rather than waiting to observe `authorization_revoked` on the affected subscription). svc-ingest's webhook receiver only *receives and validates deliveries* — it never creates/manages subscriptions.
- **Cost model — verified but ambiguous at the edge, so tracked dynamically either way.** Twitch's documented rule: "by default, the cost of a subscription is 1, but is reduced to 0 if you have a user access token from the channel related to the subscription." Whether the broadcaster's `channel:bot` grant + an App Access Token reaches that cost-0 path, or cost-0 specifically requires the channel's own user access token, is **not conclusively resolved from available docs** — verify against a live subscription-creation response's `cost` field before finalizing budget headroom. The reconciler records every subscription's actual `cost` from Twitch's response (never assumes a constant) and re-reads `total_cost` on any revocation, rather than computing a local delta.
- **Revocation — verified; the reviewer's cost-cascade claim does not match documented Twitch behavior.** "The `authorization_revoked` status occurs when Twitch revokes your subscription because the user(s) in the condition object revoked their authorization... or changed their password" — a **per-subscription** teardown with its own status field, not a cost change cascading to sibling subscriptions. No documented mechanism produces that cascade. Design still re-reads `total_cost` from Twitch after any revocation defensively (belt-and-suspenders given the cost-model ambiguity above), never trusting an assumed delta.
- **IRC — bounded, and outbound moves off it entirely.** Twitch enforces a **100-concurrent-chat-room cap per bot account**, and per the forum announcement this is **no longer waived for verified bots** — the only exemptions are joining as broadcaster/moderator or a broadcaster-authorized EventSub subscription. IRC is therefore scoped to exactly: the **≤100-channel unauthorized/trial pool** (channels whose broadcaster hasn't yet completed OAuth authorization) — comfortably within both the 100-room cap and the 20-joins/10s unverified rate limit for a slowly-growing pool. **Outbound chat sends use the Send Chat Message Helix API** (a stateless REST call, shipped alongside Conduits/EventSub-chat per the same Twitch announcement) instead of IRC `PRIVMSG` — this removes outbound's dependency on holding an IRC JOIN to every target channel entirely, so outbound scales with the same authorization model as inbound (broadcaster `channel:bot` grant), not with IRC's join cap.
- **Capacity math, 3,500 channels with headroom to 10,000:**

| Quantity | Value |
|---|---|
| Conduits / shards | 1 conduit, 8-12 shards (ceiling: 5 × 20,000 — not binding) |
| `max_total_cost` used, 3,500 channels | ~3,500 worst case (cost=1); possibly less (cost-0 path unverified) |
| `max_total_cost` used, 10,000-channel target | 10,000 worst case — **zero headroom**; request a budget increase or split across a 2nd client-id ahead of the 80% alert |
| Webhook receiver throughput (new bottleneck once socket-count no longer applies) | worst case ~10,000 channels × 1 msg/s peak ≈ 10,000 req/s across all replicas — sized via HPA on request-rate/CPU, not a fixed connection count |
| IRC pool | ≤100 channels (Twitch-enforced cap), 20 joins/10s | 
| Outbound | Send Chat Message API (stateless), not IRC | 

### 3.3 Hot-add/remove: watermark poll + supervisor

Same pattern as `svc_action::bundle_loader`: poll `ix_connections_shard_scan`'s cheap projection (proposed 15s interval — tighter than `bundle_loader`'s 300s default, since this poll doubles as the revocation fallback, §6.1), hash via `WatermarkTracker`, only recompute on change.

### 3.4 svc-action outbound connection selection

Resolves the connection from `app_outbound_bindings` (§6) — never from bundle-supplied identifiers — then calls the credential broker for the live token (Twitch: the App Access Token used for Send Chat Message calls, §3.2).

### 3.5 Capacity table

| Dimension | Target | Design number |
|---|---|---|
| Tenants | 100s | 300 |
| Communities | 10,000s | 20,000 |
| App-bundle installs | 10,000s | ~60,000 rows |
| Connections | ~10,000 | 6,000 Discord / 3,500 Twitch / 500 other |
| Discord shards | — | 8 (750/shard, floor 3), 4 replicas, 40s cold-start |
| Twitch conduits/shards | — | 1 conduit, 8-12 webhook shards (ceiling 5×20,000) |
| Twitch `max_total_cost`, 3,500→10,000 channels | ≤10,000 default | 3,500 → 10,000 (zero headroom at 10k) |
| Twitch webhook receiver req/s, worst case | — | ~10,000 req/s across replicas, HPA-sized |
| IRC pool | Twitch-enforced ≤100 | unauthorized/trial channels only |
| KMS unwrap calls, steady state | — | ~300 tenants / 10-min DEK TTL ≈ 0.5/sec |
| KMS unwrap calls, cold start (batched) | — | ≤900 one-time, 30s jittered warm-up |
| DEK rotation fan-out | — | O(tenants)=300, `dek-rotated`-event-driven |
| Relay-authz cache entries | — | 20,000-100,000, a few MB/replica |
| Relay-authz DB hits/message | O(1) required | **0** |
| Revocation latency, fast path | — | sub-second |
| Revocation latency, degraded fallback | — | `max(replica_lag, 15s poll)` |

---

## 4. Inbound webhook receivers: platform-ingest vs. generic per-app

Two receivers, deliberately kept separate — different auth, different secrets, different rate limits, different trust model.

### 4.1 Platform-ingest webhooks (Twitch EventSub today; Discord Interactions, future)

**Path:** Twitch EventSub → `POST /eventsub/twitch/webhook` (existing, §3.2) → normalize → `fanout.fan_out_event` by `consumes_tag` → every app bound to that platform/source via `app_source_bindings`. **Trust model:** the sender is the platform itself (Twitch/Discord), signature-verified against a secret Twitch/Discord issued at subscription-creation time, subscription lifecycle managed by our own control-plane reconciler (§3.2) — the receiver never has to guess who's calling, only verify the signature and dedup/replay-guard the delivery.

### 4.2 Generic per-app inbound webhooks (new)

For a third-party sender a tenant wants to feed directly into one specific app bundle — not a pre-built platform connector.

- **Route:** `POST /hooks/v1/{webhook_id}` — `webhook_id` is an **opaque, unguessable ≥128-bit id** (e.g. a UUIDv4 or 22-char base62 token) issued by hub-api at creation time. It resolves server-side, via a read-only cached view of `connections` (`kind='webhook'`), directly to `(tenant_id, app_id)` — **no caller-supplied tenant/app-id header is ever trusted for routing.**
- **Auth — asymmetric-first, so a hub-api breach can never forge an inbound delivery** (we store only the sender's *public* key/verification material; nothing an attacker steals from us lets them sign as the sender). Three modes, in preference order:
  - **Mode A (preferred): RFC 9421 HTTP Message Signatures, `ecdsa-p256-sha256` (ES256)** — FIPS-approved, the Enterprise/FedRAMP-required mode. Covered components, minimum: `@method`, `@target-uri` (or `@path`), `content-digest` (RFC 9530, over the raw body), `content-type`, and a `created` timestamp bound into the signature — not just present as a header, but part of what's signed, so it can't be swapped post-signing. `keyid` in the `Signature-Input` resolves to a registered key (below).
  - **Mode B: Standard Webhooks `v1a` (Ed25519)** — same `webhook-id`/`webhook-timestamp`/`webhook-signature` headers as `v1`, asymmetric signature instead of HMAC.
  - **Mode C (fallback only): Standard Webhooks `v1` HMAC-SHA256`** over `{id}.{timestamp}.{body}`, for senders that genuinely cannot do asymmetric signing — **opt-in per webhook row**, flagged as the weakest of the three modes in the admin UI and in `connections.rate_limit_meta`/audit log (a compromised secret *can* forge deliveries, unlike A/B). Secret sourced from the credential broker (§2) and never logged; constant-time compare (`hmac.compare_digest`-equivalent).
  - **Shared regardless of mode:** **±5 minute replay window** on the signed timestamp; **dedup on the delivery id** (`webhook-id`/RFC 9421's own id) via Valkey `SET NX` with TTL (mirrors §3.2's Twitch dedup).
- **Key registration (hub-api, per webhook):** either **pinned public keys** — multiple concurrent entries keyed by `kid`, so rotation is "register the new key, wait for the sender to switch, retire the old one" with zero delivery-loss window — or a **pinned JWKS URL** (HTTPS only, fetched and cached with a TTL, key looked up by `kid` in the fetched set). **Algorithm is fixed per registered key at registration time, never negotiated from the incoming request** — a key registered as ES256 or Ed25519 rejects any request claiming `HS256`/`none`/any other `alg`, closing the classic algorithm-confusion hole (an attacker can't downgrade an asymmetric-keyed webhook to "just HMAC it with the public key"). Mode C's HMAC secret is the only credential-broker-backed row here; A/B's public keys/JWKS URLs are not secrets and live directly on the `connections` row.
- **Model:** each webhook is a `connections` row, `kind='webhook'`, `direction='inbound'`, bound to **exactly one** app installation (a single `app_outbound_bindings`-shaped row is overkill here — the binding is 1:1 by construction, carried directly in `connections.mapping`, alongside the registered mode/keys). hub-api's RW routes (`/api/v1/connections`) handle create/rotate/disable, tenant-scoped like every other connection.
- **Delivery:** the validated payload is wrapped as a `PlatformEvent` (`platform='webhook'`, `event_type` from the webhook's configured mapping or a declared header) and `XADD`'d to the partition stream that only the bound app consumes — **no fan-out-by-tag**, unlike §4.1, because the binding is already 1:1. **202 Accepted** once validation + `XADD` succeed (async processing downstream, not synchronous). **Body-size limit** (e.g. 256KB) and **strict `Content-Type: application/json` allowlist** reject anything else before signature verification even runs (cheapest checks first). **Uniform 401/404 policy:** an unknown `webhook_id` and a known-but-badly-signed one return the **same** response shape and timing (constant-time lookup-or-compare) — the endpoint never reveals whether a given id exists.

### 4.3 Outbound platform webhooks (waddles → external endpoint)

Symmetric with §4.2's asymmetric-first inbound design, for the direction where waddles is the sender (e.g. a tenant-configured "notify my external system" delivery): every outbound webhook is signed with **ES256 (RFC 9421 HTTP Message Signatures)** using a platform signing key. The private key material lives in **KMS/HSM** — prefer KMS-side asymmetric signing (the private key never leaves the KMS/HSM boundary at all; hub-api sends the digest, gets back a signature) over loading key material into application memory. The public verification key is published at a platform JWKS endpoint (`https://waddles.app/.well-known/jwks.json` or product-equivalent) that receivers pin or fetch; **rotation** = publish the new key under a new `kid` first, sign new deliveries with it, keep the old key in the JWKS until receivers have rolled over, then retire it — the same zero-delivery-loss shape as inbound key rotation (§4.2).

### 4.4 Shared hardening (all receivers)

Both are internet-facing and share a hardening baseline, layered per-path where the trust models diverge:

- **Rate limiting** — per-tenant + per-connection/webhook token buckets (§5), plus a coarser global bucket per path at the ingress layer. Twitch's sender population is one known platform; `/hooks/v1`'s is arbitrary third parties, so its global bucket is stricter and its per-webhook bucket is mandatory, not optional.
- **Body-size limits and strict content-type allowlists** on both paths, checked before any HMAC computation (cheap rejects first, avoids wasting CPU on oversized/malformed bodies).
- **Deployment topology:** both mount on svc-ingest — stateless HTTP handlers, no reason to split into a separate edge service today, since neither needs anything svc-ingest doesn't already have (Valkey for dedup, the credential broker for secrets). Exposed as **two distinct Ingress path rules** (`/eventsub/*`, `/hooks/v1/*`) so rate limits/WAF rules are configured independently per path even though both terminate in the same backend Service; **CiliumNetworkPolicy** scopes external ingress to exactly these two paths/ports on svc-ingest, default-deny everything else, per `security.md`'s Kubernetes Network Security baseline. If load profiles diverge enough in practice (e.g. `/hooks/v1` traffic dwarfing platform-ingest traffic, or wanting independent HPA curves), splitting into a dedicated edge deployment is a follow-on, not a day-one requirement.

---

## 5. Multi-tenant isolation per pod

- **Per-tenant connection quotas**, enforced at registration time by hub-api, tied to license tier.
- **Per-tenant outbound rate limiting** — `svc_action`'s existing `UsageBatcher` extended from metering-only to a token-bucket keyed by `tenant_id`, independent of platform-level limits (§3).
- **Per-webhook and per-tenant rate limits on `/hooks/v1`** (§4.4) — the same isolation principle applied to inbound third-party traffic, not just outbound.
- **Fair shard/connection scheduling** — weighted by recent message-volume, not blind `id % replica_count`, so one active tenant's guilds/channels don't concentrate on one replica. Deploy-time rebalance only.
- **Noisy-neighbor isolation in the executor** — a per-tenant semaphore on in-flight `invoke`s, alongside the existing per-`app_id` `EgressGuard` bucket extended to relay pushes, tenant ceiling layered on top of the app-level one.

---

## 6. Relay authorization

**Today:** `handle_relay` resolves `InvokeScope{tenant, community, app_id}` per-call but takes Twitch `channel` straight from the bundle's `message_json`, unchecked. Discord already avoids this (`scope.origin_channel_id`, reply-only).

**Design:** `app_outbound_bindings` join (generalizing migration 0025's inbound pattern):

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

Deny + audit-log unless `(tenant, community, app_id, provider, channel)` has a matching `connections` + `app_outbound_bindings` row. **O(1)**: svc-action loads the join into an in-memory map via the watermark-poll supervisor (§3.3) — a hash-map lookup on the hot path, never a query.

### 6.1 Revocation latency — bounded, with a fast path

- **Fast-path invalidation** — hub-api publishes `connection-revoked:{connection_id}` on Valkey pub/sub on every disable/binding-delete; data-plane replicas evict immediately (**sub-second typical**). The 15s poll (§3.3) is the self-healing fallback.
- **Fail-closed on cache uncertainty, asymmetrically** — if the fast-path channel's health is uncertain, **ALLOW verdicts get a short freshness bound**; **DENY verdicts remain valid indefinitely** — uncertainty narrows what's trusted, never widens it.

### 6.2 Rollout: shadow mode, not a permanent kill switch

**Security controls must never be switchable off in steady state** — relay-authz ships with **no `disable-relay-authz` flag, ever**:

1. **Shadow/log-only mode** (bounded soak, proposed 2-4 weeks): evaluates the check, **logs + alerts every would-be-deny at WARN**, never blocks.
2. **Enforcement** once the soak shows clean results — `handle_relay` starts returning `denied("channel_not_bound", ...)` for real.
3. **The shadow-mode flag and its branch are deleted from the codebase** at that point — never left in place as a lever. A regression is fixed by shipping a fix, not by flipping a flag.

---

## 7. Feature flag rollout — opt-out kill-switches, not default-OFF (except relay-authz, §6.2)

Per the house convention (`core/svc_action/src/flags.rs::DISABLE_DB_BUNDLE_CONFIG_FLAG`):

| Flag | Mechanism it can disable | Default |
|---|---|---|
| `waddles.core.disable-connections-registry` | svc-ingest/svc-action read from `connections`/broker | ON |
| `waddles.core.disable-credential-broker` | broker-mediated resolution | ON |
| `waddles.core.disable-fast-revocation` | Valkey fast-path invalidation (§6.1); **latency only**, never the check itself | ON |
| `waddles.core.disable-tenant-quotas` | per-tenant rate limiting/fair scheduling (§5) | ON |
| `waddles.core.twitch-websocket-devtest` | dev/test-only WebSocket transport (§3.2) — **never enabled in beta/gamma/prod** | OFF |

Relay-authz has **no row here** — see §6.2.

---

## 8. Increment plan

Security-critical items first; each step independently shippable.

1. **`connection_credentials` table + RBAC grants** (`privileges: []` for every non-hub_api role).
2. **Credential broker** — versioned DEK cache with `dek-rotated` invalidation and decrypt-fail-closed handling (§2.2), per-replica stale-while-revalidate secret cache, batch resolve, circuit breaker — behind `waddles.core.disable-credential-broker`.
3. **`connections` table + migration/backfill; `connection_service.py`** — behind `waddles.core.disable-connections-registry`.
4. **Twitch EventSub receiver hardening** (§3.2 gap table): dedup, replay window, revocation → status update + fast-path invalidation, per-connection broker-sourced secrets replacing `TWITCH_EVENTSUB_SECRET`.
5. **Twitch control-plane reconciler** in hub-api: conduit/shard setup, subscription create/renew/delete, cost-budget tracking + 80% alert, `user.authorization.revoke` handling, broadcaster `channel:bot` OAuth grant flow at connection registration.
6. **Generic per-app webhook receiver** (`POST /hooks/v1/{webhook_id}`, §4.2): RFC 9421/ES256 and Standard Webhooks `v1a`/Ed25519 as the preferred asymmetric modes, HMAC `v1` as opt-in fallback; pinned-key/JWKS registration with fixed per-key algorithm (no `none`/`HS*` on an asymmetric key); replay window, dedup, 202-accept + XADD, uniform 401/404 policy. Outbound platform-webhook signing (§4.3: ES256/RFC 9421, KMS/HSM-backed platform key, JWKS publication) ships alongside it wherever outbound webhook delivery exists.
7. **Relay-authz stage-side check, shipped in shadow/log-only mode first** (§6.2) — no kill switch; bounded soak, then permanent enforcement with the shadow branch deleted. Fast-path revocation ships alongside it, behind `waddles.core.disable-fast-revocation`.
8. **Discord shard supervisor + Twitch conduit/shard supervisor**, watermark-poll hot-add/remove, weighted fair scheduling (§5) — behind `waddles.core.disable-tenant-quotas` for the weighting.
9. **Rust port of the EventSub receiver** (`core/svc_ingest/src/ingest/twitch.rs`'s open `TODO(M5)` seam) and IRC's Send Chat Message API swap-out for outbound.
10. **Scale hardening (deferred):** partitioning if per-tenant row counts approach 10^6; verify the Twitch cost-0 condition empirically ahead of the 80%-of-`max_total_cost` alert; register a second Twitch client-id if the 10,000-channel target needs the headroom; Discord Interactions webhook receiver (§3.1) reusing §4.4's shared hardening.

---

*Illustrative sizing numbers are assumptions for capacity math, not committed targets. Discord/Twitch facts are cited to official docs/forum posts fetched 2026-09-28; the exact cost-0 condition for `channel:bot`-authorized conduit subscriptions was not conclusively confirmed and must be verified against a live subscription-creation response before finalizing the 10,000-channel cost budget.*
