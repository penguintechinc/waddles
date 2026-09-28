# Connector Bundles — Platform Receivers & Senders as WASM — Design

**Status:** Proposed design (no code in this doc).
**Date:** 2026-09-28
**Scope:** new `wit/waddle-connector/connector.wit`, `core/svc_ingest` (host transport layer, replaces native Discord/Twitch receivers), `core/svc_action` (host transport layer for senders, replaces `handle_relay`/`handle_discord_relay`), `hub_api` (internal gRPC service, permission catalog, artifact signing), new `bundles/rust/connector-*` reference bundles.
**Driver:** Justin's decision — every platform receiver (Discord, Twitch IRC/EventSub, Kick, Slack, YouTube, Echo; later Teams/Google Chat/Mattermost) and every platform sender (Discord, Slack, YouTube, Kick, Twitch) becomes a WASM app bundle run by `svc-ingest`/`svc-action`, exactly like every other bundle. The existing native Rust Discord/Twitch receivers migrate to this model too. Python leaves the live-traffic path entirely. Ships in v3.0.
**Builds on (read, reconciled, not re-litigated):**
- `wit/waddle-bundle/stage.wit` (v1.0) — the `types`/`http`/`kv`/`flags`/`log`/`clock` interfaces this design reuses verbatim where a connector needs them.
- `docs/superpowers/specs/2026-09-28-wit-stage-v1-1-design.md` — per-component `Linker` (registers only a component's declared imports), action-stage-only capability pattern (`relay`/`moderation`), shadow-then-enforce rollout discipline. This design's sender world follows the same action-stage-only shape.
- `docs/superpowers/specs/2026-09-28-bundle-permissions-and-capability-gate.md` (PR #419, open) — the permission catalog (§1), `authorize()` gate, `InvokeScope` confused-deputy prevention, Ed25519 artifact signing (§5.6), and **§10's PII invariant this design must reconcile with** (bundles see UUIDs, never PII) — see §3.3 below.
- `docs/superpowers/specs/2026-09-28-connections-credentials-design.md` (PR #399, merged) — `sources`/`community_connections` split, credential broker, webhook receiver hardening (EventSub HMAC, generic `/hooks/v1/{id}` ES256/Ed25519), Discord/Twitch capacity math. This design's host transport layer is the concrete implementation of that spec's receivers, now bundle-shaped.
- `docs/superpowers/specs/2026-09-28-dataplane-scale-design.md` — 2048-partition fixed hashing keyed by `hash(source_id)`, opaque `source_id` (never a channel name) as the only identity in the stream key.
- PR #429 (`feature/ingest-pii-tokenization`, open) — inbound PII tokenization pass; PR #443 (`feature/rust-ingest-identity-encryption`, open) — envelope-encrypts `actor` on the ingest→process Valkey stream. Both target `core/svc_ingest::normalize.rs`/`core/svc_process`, the exact code this design replaces with WASM — §3.7 states precisely which parts of each remain needed once connector bundles land.
- PR #434 (`feature/hub-users-identity-uuid`, merged) — adds `hub_users.uuid`, the `community_member_identities` view, and the existing `waddles_bundle_reader` RO role's **column-scoped, PII-free** grant (`SELECT (uuid, id) ON hub_users`). §3.3 defines a second, distinct RO role for connector PII access — `waddles_bundle_reader` itself is unchanged and stays PII-free.
- **Justin's decision on PII (supersedes this design's original §3.3 draft and, for connector bundles specifically, PR #419 §10's "bundles never see PII" framing):** *"For platform integration services they should have access [to] pii data from read only replicas."* Connector bundles are the privileged class (§3.2's Option A) **and** may read PII directly from a read-only Postgres replica for identity lookups — platform-handle/display-name ↔ `hub_users.uuid` — for both inbound tokenization and rendering outbound mentions. Ordinary `stage`/`stage-v1_1` app bundles are entirely unaffected: zero PII, unchanged.
- `core/svc_ingest/src/ingest/{discord,twitch,twitch_eventsub}.rs`, `core/svc_ingest/src/normalize.rs`, `core/svc_ingest/src/outbound.rs`, `core/svc_action/src/capabilities.rs` — the current native implementation this design migrates. Confirmed by reading the code: Discord gateway is a single unsharded connection with **no true `OP_RESUME`** yet (every reconnect is a fresh `IDENTIFY`, disciplined only by `Backoff`/`STABILITY_WINDOW`); Twitch IRC is one connection per channel (not yet pooled to Twitch's 100-channel cap); `svc_action::capabilities.rs` has exactly two compiled-in relay providers (`twitch` via Valkey `LPUSH` queue, `discord` via direct REST send) — Slack/YouTube/Kick are unwired (`// TODO(M5)`/`PendingSeam`).
- **Internal service calls are gRPC, not REST (Justin's constraint).** Every host→hub-api call this design introduces — credential broker resolve, DEK fetch, identity tokenization — goes through hub-api's internal gRPC service, `waddles.hub.internal.v1`, being built on `feature/hub-api-internal-grpc`. The proto already exists at `libs/grpc_protos/hub_internal.proto` (`HubInternalService`, today only `RecordActivity`/`RecordMessage`) and gains the RPCs this design needs (§2.4). This corrects connections-credentials' `POST /internal/v1/credentials/resolve-batch` and PR #443's `POST /api/v1/internal/keys/tenant-dek` to gRPC for every call this design's host transport layer makes — those two specs' REST sketches predate this constraint.

---

## 1. WIT world `connector@1.0.0`

A second world, alongside `stage`/`stage-v1_1` (same package-versioning approach as wit-v1.1 §5: one file, multiple worlds, `wasm-tools component wit` resolves each independently).

```wit
package waddle:connector@1.0.0;

interface types {
  /// Immutable, host-constructed. Never guest-influenced (same
  /// confused-deputy rule as InvokeScope, PR #419 S5.1).
  record connection-ctx {
    platform: string,
    /// Opaque source id -- NEVER a channel/guild name (scale design's
    /// "opaque source ids in stream keys" rule extends to every value a
    /// connector bundle can see).
    source-id: string,
    /// Host-assigned, stable per logical session (survives a transport
    /// reconnect if the host RESUMEs; new value on a fresh session).
    connection-id: string,
    /// Opaque session handle for RESUME-capable transports (Discord).
    /// The bundle never parses this -- it round-trips it back into
    /// build-identify on reconnect; the host is the only reader.
    session-token: option<string>,
  }

  record raw-frame {
    ctx: connection-ctx,
    bytes: list<u8>,
    received-at: string,          // RFC 3339 UTC, ms precision
  }

  /// Same shape as waddle:bundle/types.platform-event, except `actor`
  /// (and any target-user field inside payload-json) is ALREADY a
  /// canonical UUID or ephemeral pseudonym by the time on-frame returns
  /// it -- see S3.3. `payload-json` may itself carry `{user:<uuid>}`
  /// placeholders for any additional mentioned users (same grammar as
  /// PR #419 S10.4's outbound placeholders, reused inbound).
  record normalized-event {
    platform: string,
    event-type: string,
    actor: option<string>,
    payload-json: string,
    occurred-at: string,
  }

  /// Returned by on-connect. `secret-refs` names header/field slots the
  /// HOST fills with the real credential immediately before the socket
  /// write -- the exact `http::request.secret-refs` pattern from
  /// stage.wit:89, extended to raw transport frames.
  record handshake-payload {
    bytes: list<u8>,
    secret-refs: list<tuple<string, string>>,
  }

  variant frame-error { unsupported(string), malformed(string), backend(string) }
}

/// Host-mediated identity lookup, reading the RO Postgres replica through
/// the dedicated `waddles_connector_pii_reader` role (S3.3) -- column-
/// scoped to identity columns only. **Capability: `connector.pii.read`,
/// dangerous, global-approved/first-party-core-only (S3.1/S3.2) -- never
/// exposed to `stage`/`stage-v1_1`, so an ordinary app bundle has no
/// linker path to it at all (S3.3).** This is the one place a connector
/// bundle may hold raw PII (a handle/display name) in guest memory,
/// justified by S3.2's privileged-trust-tier argument.
interface identity {
  variant identity-key {
    /// Resolve a UUID back to its displayable identity (outbound mention
    /// rendering, sender.build-request).
    uuid(string),
    /// Resolve a raw platform identity to its canonical UUID (inbound
    /// tokenization, receiver.on-frame). `(platform, platform-user-id)`.
    platform-identity(tuple<string, string>),
  }

  record identity-record {
    uuid: string,
    /// false = no real hub_users row exists; `uuid` is a deterministic
    /// ephemeral pseudonym (PR #419 S10.3's UUIDv5 scheme) and `handle`/
    /// `display-name` are always `none` -- never mints a real row just
    /// because a platform identity was looked up.
    linked: bool,
    handle: option<string>,
    display-name: option<string>,
  }

  variant error { denied(string), not-found, rate-limited(u32), backend(string) }

  /// `not-found`: the target was erased (S3.3's erasure-invalidation
  /// rule) or never existed. `rate-limited`: the per-connector-digest
  /// token bucket (S3.3) is exhausted -- callers must back off, never spin.
  lookup: func(key: identity-key) -> result<identity-record, error>;
}

interface receiver {
  use types.{connection-ctx, raw-frame, normalized-event, handshake-payload, frame-error};

  /// Called once per new/resumed connection. Returns the platform's
  /// IDENTIFY/AUTH/subscribe frame; `ctx.session-token` is populated on a
  /// resume attempt so the bundle can build a RESUME payload instead of a
  /// fresh IDENTIFY, when the platform supports it.
  on-connect: func(ctx: connection-ctx) -> result<handshake-payload, frame-error>;

  /// The hot path. One raw transport frame in, zero or more normalized
  /// events out (a single Discord dispatch, IRC line, EventSub delivery,
  /// or polled batch may yield 0..N events -- return-side batching, not
  /// call-side, S4).
  on-frame: func(frame: raw-frame) -> result<list<normalized-event>, frame-error>;

  /// The host calls this on its own heartbeat/keepalive timer -- NEVER
  /// gated on on-frame's latency (S4). `none` means "host sends its
  /// platform-default heartbeat unchanged"; `some` lets a bundle whose
  /// platform needs a non-trivial heartbeat body (e.g. a sequence number)
  /// supply one.
  on-heartbeat-due: func(ctx: connection-ctx) -> result<option<list<u8>>, frame-error>;

  on-disconnect: func(ctx: connection-ctx, reason: string);
}

interface sender {
  use types.{frame-error};

  record header { name: string, value: string }

  record action {
    kind: string,               // e.g. "chat.send", "moderation.ban" -- platform-agnostic verb
    target-source-id: string,   // opaque, resolved server-side from app_outbound_bindings
    payload-json: string,       // may carry {user:<uuid>} placeholders from an app bundle's
                                 // relay.push, or a raw target the connector itself resolves
                                 // via identity.lookup -- see S3.3
  }

  /// The templated request the HOST will send. `secret-refs` (credential
  /// injection) still resolves host-side, immediately before transmission
  /// -- but placeholder/mention RENDERING now happens inside build-request
  /// itself, via identity.lookup (S3.3, Justin's PII decision): the
  /// connector is the privileged, sink-adjacent component, so it may
  /// render `{user:<uuid>}` into a real handle/display name before
  /// returning this record. The host still owns the actual socket write
  /// -- build-request (not a bundle-owned send) stays preferred so no
  /// bundle ever opens a socket, not because the bundle can't touch PII.
  record http-request-tpl {
    method: string,
    url: string,
    headers: list<header>,
    body: option<list<u8>>,
    secret-refs: list<tuple<string, string>>,
  }

  /// Preferred: the bundle shapes the platform-specific request -- including
  /// resolving any {user:<uuid>} placeholder via identity.lookup and
  /// applying per-sink escaping (IRC-safe, HTML-entity, etc.) -- and the
  /// host transmits it. No socket/HTTP client in the guest.
  build-request: func(action: action) -> result<http-request-tpl, frame-error>;
}

world connector {
  import identity;
  import http;     // reused from waddle:bundle@1.0.0 -- guarded, for a
                    // sender's fallback path (S3.4) and any receiver-side
                    // REST call (e.g. Twitch subscription bookkeeping
                    // stays host-side, not bundle-side -- see S2).
  import log;
  import clock;
  import %flags;

  export receiver;
  export sender;
}
```

**Bundles have no sockets — the host owns every transport.** No `wasi:sockets`, no `wasi:http` outbound in this world's imports; the only network-shaped capability is the existing guarded `http` import (rate-limited, host-allowlisted, same as every `stage` bundle) and it is optional for a receiver — the transport connection itself is never guest-visible.

---

## 2. Host transport abstraction (`svc-ingest`)

One `Transport` trait per family; the connector bundle only ever sees `receiver`/`sender` calls — connect/reconnect/backoff/session/credential plumbing is 100% host code, unchanged in spirit from today's `ingest::discord`/`ingest::twitch` modules, generalized to drive a WASM `on-frame` instead of a native `normalize_*` function.

| Transport | Platforms | Host owns | Gaps this design closes vs. today's native code |
|---|---|---|---|
| **WebSocket** | Discord Gateway | Connect/reconnect (`Backoff`/`STABILITY_WINDOW`, unchanged), **session storage** (`session_id`+`seq` in Valkey, keyed by `source_id`) for a **real `OP_RESUME`** on reconnect, shard supervision (watermark-poll hot add/remove per scale-design §3.1/3.3), the IDENTIFY rate budget (`max_concurrency`/5s, 120/60s/conn, 1000/24h global) | Today's `discord.rs` has **no `OP_RESUME`** — every reconnect is a fresh `IDENTIFY`. This design adds it because the host transport layer is being rebuilt anyway; not doing so would ship the same gap a second time. |
| **IRC** | Twitch chat | Connect/reconnect, **channel pooling to Twitch's 100-channel/connection cap** (today: one connection per channel), JOIN/PART batching, 20 joins/10s pacing | Pooling is new — today's `twitch.rs` is 1:1 connection:channel, fine at current scale, not at the 10k-channel target (connections-credentials §3.2). |
| **Webhook** | Twitch EventSub, Slack Events API, generic `/hooks/v1/{id}` | Signature verification **before** the bundle ever sees the body — HMAC-SHA256 (EventSub, Slack's signing secret) or ES256/Ed25519 (`/hooks/v1`, connections-credentials §4.2) — replay window, dedup, 2xx/202 response. A bundle's `on-frame` receives only a body that already passed verification; it never holds or checks a signature. | None — today's EventSub receiver already verifies host-side; this design keeps that property and extends it to Slack/`/hooks/v1`. |
| **Polling** | YouTube (Live Chat) | Poll schedule, quota-aware backoff, dedup on the platform's own page/continuation token — each poll response is handed to `on-frame` as one `raw-frame`, unifying polling with push transports at the bundle boundary | New transport family for this codebase. |
| **Pusher** | Kick | Pusher app-key handshake, channel subscribe/heartbeat framing — structurally a WebSocket variant (persistent socket, ping/pong), implemented as its own `Transport` impl reusing the WebSocket family's reconnect/backoff primitives | New transport family; reuses WS reconnect discipline rather than duplicating it. |

**Leasing, reconnects, and backoff are host-side, full stop.** A connector bundle never sees a lease, a partition, or a reconnect attempt — `on-connect`/`on-disconnect`/`on-heartbeat-due` are called by host code that already decided a connection is live; `Backoff`/`STABILITY_WINDOW` (`core/svc_ingest/src/ingest/mod.rs`, unchanged) still govern the host's own retry loop.

### 2.1 Credential injection

Bundles never see tokens. `on-connect`'s returned `handshake-payload.secret-refs` names which byte ranges (or, for webhook/HTTP shapes, which header) the host fills from the credential broker's resolved value immediately before the frame leaves the host — the same `secret-refs` pattern `http::request` already uses in `stage.wit`, extended to raw socket writes. The connector bundle constructs the *shape* of an IDENTIFY/AUTH frame; it never constructs, stores, or logs the credential *value*.

### 2.2 `source_id` is the only identity a connector ever sees for routing

Per the dataplane-scale design, `connection-ctx.source-id` is the opaque `sources.id`-derived identifier — never a guild/channel name. The host resolves `source_id → (platform, external_id, credential)` from the `sources`/`community_connections` registry (PR #399) before a connection is even opened; the connector bundle's job starts after that resolution, not before it.

### 2.3 Instance pooling — connector bundles are always-pinned, never LRU-evicted

Unlike tenant/vendor app bundles (dataplane-scale §3, lazy-load + decayed-score LRU), a connector bundle is core, global, and load-bearing for every tenant's ingest on a replica — it is loaded at replica startup and held resident for the replica's lifetime, exempt from `PIN_MAX_IDLE`/byte-cap eviction entirely (a new `pinned: always` flag on the bundle's manifest, checked by the executor's cache, distinct from the decayed-score pinning tier). A small fixed pool of `Store` instances per (platform, connector digest) — sized to the replica's executor worker-thread count — lets concurrent `on-frame` calls proceed across shards/connections without serializing all frames of one platform through a single `Store`; the compiled `Component` is shared read-only across the pool (wasmtime's standard multi-instance-per-module pattern), so pool growth costs a `Store`, not a recompile.

### 2.4 hub-api calls go through internal gRPC, not REST

Every host call from `svc-ingest`/`svc-action` into hub-api for **credential/secret** resolution — never available to a replica, connections-credentials §2.1's "resolution always reads the primary" rule — is a `waddles.hub.internal.v1` gRPC call (branch `feature/hub-api-internal-grpc`, extending the existing `libs/grpc_protos/hub_internal.proto`/`HubInternalService`, today only `RecordActivity`/`RecordMessage`). This design adds:

| RPC | Replaces (REST sketch in prior specs) | Called by |
|---|---|---|
| `ResolveCredential` / `ResolveCredentialBatch` | `POST /internal/v1/credentials/resolve-batch` (connections-credentials §2.1) | Host transport layer, connect-time + refresh-ahead (unchanged cadence) |
| `ResolveTenantDek` | `POST /api/v1/internal/keys/tenant-dek` (PR #443's `HubApiDekProvider`) — kept only for the residual case §3.7 identifies; not needed for connector-originated events' `actor` field | The migration-window/residual `identity_crypto` path (§3.7), never `identity.lookup` |

**`identity.lookup` (§1, §3.3) is deliberately NOT a gRPC call.** Justin's decision routes connector PII access through a **direct RO-replica Postgres connection** using the new `waddles_connector_pii_reader` role (§3.3) — the same pattern `waddles_bundle_reader` already uses for the Rust data plane's own RO reads (PR #434), not a hub-api proxy hop. This is lower-latency (no extra service hop) and consistent with treating identity/display-name data as bounded-staleness-tolerant (a replica lag of a few seconds on a display name is immaterial), unlike credentials/secrets, which must never tolerate replica staleness and therefore stay gRPC-to-primary.

Per `backend.md`, every RPC still carries `api_version`/routes on proto package version; mTLS/SPIFFE or a short-lived machine JWT per `security.md` Service-to-Service Auth, unchanged from the rest of the platform's service-to-service posture.

---

## 3. Security

### 3.1 Permission catalog additions (PR #419 §1)

| id | Risk | Notes |
|---|---|---|
| `connector.receive:<platform>` | **dangerous** | One id per platform (`connector.receive:discord`, `:twitch`, `:kick`, `:slack`, `:youtube`). Touches every tenant's traffic on the platform — the single most powerful grant in the catalog. |
| `connector.send:<platform>` | **dangerous** | Same shape, outbound side. Subsumes `chat.send:<platform>`'s existing relay grant for platforms that migrate to a connector sender bundle (§5) — `chat.send:<platform>` is retired for a platform once its sender cuts over, not run in parallel. |
| `connector.pii.read` | **dangerous** | Gates `identity.lookup` (§1, §3.3) — the one capability in the entire catalog that returns raw PII (handle, display name) to a guest. **Global-approved, first-party-core-only, no exceptions** — never offered to a vendor bundle, never restrictable/grantable at tenant or community tier (same carve-out as `connector.receive`/`connector.send`, below). |
| `net.http` | dangerous (existing) | Unchanged — a connector's `http` import for any REST bookkeeping call (e.g. an EventSub subscription-management call the host makes, not the bundle) still goes through the existing egress allowlist. |

**No 3-tier consent flow for connector bundles.** PR #419's global→tenant→community consent ladder is for tenant-installed app bundles; a connector bundle is platform infrastructure, not something a tenant activates. Its permission grant is a **global-tier-only, platform-ops action** (enable this connector digest for a given platform/shard rollout) — `app_tenant_permission_restrictions`/`community_permission_grants` never apply to a `connector.*` permission id. This is a deliberate carve-out from §3 of PR #419, not an oversight: there is no tenant/community layer to ask, because the connector serves every tenant on that platform at once.

### 3.2 Global-approval-only; vendor eligibility deferred

**Only GLOBAL-approved connector bundles run, and for v3.0 that means first-party/core-only (`waddles.core.*`) — no vendor connector bundles.**

Justification:
- **Blast radius.** A compromised or buggy tenant app bundle affects one community. A compromised connector bundle affects every tenant on that platform simultaneously — it sits ahead of the tenant/community boundary entirely.
- **Trust tier mismatch with today's vendor pipeline.** PR #419 §5.6's Ed25519 artifact-signing (hub-api signs the approved digest; executor verifies before instantiating) covers *any* bundle, but the *review* behind that signature for a connector is materially deeper than a normal capability review — it is the same code that would otherwise live in `core/svc_ingest`'s own crate. Extending vendor-submission review to that depth is out of scope for v3.0.
- **PII exposure (§3.3).** A connector bundle sees raw platform frames *and* has a standing, capability-gated ability to read PII from the RO replica (`identity.lookup`, §3.3) — Justin's decision. That is only acceptable because it is first-party, signed, core-namespaced code replacing already-trusted platform code; the argument does not extend to a third party.

**Deferred, not rejected:** a future "verified connector vendor" tier is plausible post-v3.0 (narrower connector shapes that don't need raw-frame access, e.g. a webhook-only social connector using host-side pre-extraction instead of §3.3's model) — flagged as an open question (§7), not designed further here.

### 3.3 PII in raw frames — Justin's decision: connector bundles read PII from the RO replica

**The tension, stated precisely.** A receiver bundle's `on-frame` necessarily sees the **raw platform frame** — a Discord `MESSAGE_CREATE` payload with `author.username`, a Twitch IRC `PRIVMSG` line with its sender tag, an EventSub JSON body with a chatter's login. That is unavoidable: parsing the wire format *is* the bundle's job. `critical-rules.md`'s PII Tokenization rule and PR #419 §10.1 both state the platform's standing invariant just as plainly for ordinary bundles: **a bundle only ever receives tokenized identity, never PII.** Two ways to reconcile this for connector bundles were considered:

| Option | Shape | Verdict |
|---|---|---|
| **A. Connector bundles are a privileged class running *inside* the PII boundary** | The bundle is trusted, signed, core-only code that is *functionally* `svc_ingest` relocated into WASM — not a tenant/vendor "app." | **Decided — Justin: *"For platform integration services they should have access [to] pii data from read only replicas."*** |
| **B. Host pre-extracts identity fields before the bundle ever sees the frame** | The host parses each platform's raw wire format itself to redact/tokenize identity fields before handing a scrubbed frame to `on-frame`. | **Rejected.** Requires the host to duplicate the platform-specific parsing knowledge this design exists to move *into* bundles — defeats the point of WASM-izing receivers at all. |

**The decision is broader than the original draft's "tokenize immediately, never hold a mapping" refinement — connector bundles may hold and use actual PII (handles, display names), not just produce a token from it, for two purposes:**
1. **Inbound tokenization** — resolving a raw platform identity encountered in a frame to its canonical `hub_users.uuid` (or an ephemeral pseudonym for an unlinked identity).
2. **Outbound mention rendering** — resolving a `{user:<uuid>}` placeholder (emitted by an app bundle via `relay.push`, or by a connector's own `action.payload-json`) back into a real handle/display name when building the outbound request, inside `sender.build-request` (§1) — the connector, not the host, is now the trusted "component that owns the sink" that PR #419 §10.4 requires detokenization to happen inside, since it is the privileged trust tier adjacent to the actual platform call either way.

Both go through one gated host capability, `identity.lookup` (§1) — never ad hoc SQL in the bundle (there is no SQL in the bundle; it's WASM), and never a raw `db`/`kv` capability repurposed for this.

**Dedicated Postgres role: `waddles_connector_pii_reader`.** A new RO-replica role, distinct from `waddles_bundle_reader` (PR #434's existing RO role for the Rust data plane, column-scoped to `hub_users.uuid`/`id` only and **unchanged, still PII-free** by this design):

| Aspect | `waddles_bundle_reader` (existing, PR #434) | `waddles_connector_pii_reader` (new, this design) |
|---|---|---|
| Used by | Ordinary data-plane RO reads (`stage` world's implicit reads, `sources`/`community_connections` polling, etc.) | Only the `identity.lookup` host-import implementation, only for connector-world invocations |
| Grant shape | `SELECT (uuid, id) ON hub_users` — column-scoped, no PII column reachable | Column-scoped `SELECT` on **only** the identity columns a connector needs: `hub_users.uuid`, and the platform-identity/handle/display-name columns (`platform`, `platform_user_id`, `handle`/`login`, `display_name`) on `community_members`/the identity view — never any other PII column (email, IP, payment, address) and never any non-identity table |
| Reachable from | Any Rust data-plane connection | **Only** the connector host-import code path — a regular `stage`/`stage-v1_1` bundle's connection never authenticates as this role, and the WIT `identity` interface (with `identity.lookup`) does not exist in those worlds at all (no linker path, §1) |
| PII exposure | None — deliberately | By design, for connector bundles only, capability-gated (`connector.pii.read`, §3.1) |

**Ordinary app bundles get zero PII, unchanged.** `identity.lookup`/`connector.pii.read` exist only in the `connector` world; `stage`/`stage-v1_1`'s `Linker` (wit-v1.1 §5's per-component linker isolation) never registers them, so an app bundle has no path to this capability even if its manifest somehow claimed it — enforced the same way PR #419 already enforces every other capability boundary. **Tokens, not PII, are what leaves a connector:** `receiver.on-frame`'s returned `normalized-event.actor` is still UUID/pseudonym-only (§1) — the PII a connector reads via `identity.lookup` is consumed internally (to compute that token, or to render outbound text) and never itself placed in a field that crosses into the multi-tenant partition stream or an app-bundle-visible record. That is the actual boundary this design preserves: not "no bundle ever touches PII," but "no PII crosses from a connector into the partition stream, `stage`/`stage-v1_1`, or any app bundle."

**Why RO-replica, not primary (unlike credentials, §2.4).** Identity/display-name data tolerates bounded staleness — a display name a few seconds stale is a cosmetic non-issue, unlike a credential or DEK, where staleness is a correctness/security bug (connections-credentials §2.1's "never a replica" rule is specific to secrets, not identity attributes). Reading the RO replica directly also avoids adding a hub-api round trip to a call that sits adjacent to the hot per-frame path.

### 3.4 `identity.lookup`: audit, rate limits, caching

| Control | Design |
|---|---|
| **Audit trail** | Every `lookup` call writes an audit row — `{connector_platform, connector_digest, identity_key (uuid or platform+platform_user_id — never the returned handle/display-name value), cache_hit, occurred_at}` — same "no secret/PII material in the audit body" posture as the credential broker's own audit log (connections-credentials §2.3). Counted via `waddles_connector_pii_lookup_total{platform, cache_hit}`. |
| **Rate limits** | Per-`(connector_digest)` token bucket (`UsageBatcher`, the same primitive `relay`/`moderation`/`reputation` already use), default budget sized to a multiple of that connector's expected per-frame invocation rate, independent of the `on-frame` invoke budget itself. Exhausted → `rate-limited` (§1's WIT error variant), denial audited/counted like any other capability denial (PR #419 §5.4) — never a silent drop. Bounds a compromised/buggy connector from bulk-scraping identities even though it is a trusted first-party bundle — defense in depth, not a trust statement. |
| **Caching** | Per-replica `{identity-key -> identity-record}` cache, **TTL default 5 minutes** (matching PR #419 §10.4's existing rename-invalidation cache default). Most `on-frame`/`build-request` calls hit cache, not the RO replica — bounds steady-state read load the same way the credential broker's per-replica cache bounds KMS-unwrap load (connections-credentials §2.1). |
| **Erasure invalidation** | On a DSAR/erasure request, hub-api publishes `identity-erased:{uuid}` on the same Valkey pub/sub invalidation bus this codebase already uses for `dek-rotated`/`community-connection-revoked` (connections-credentials §2.2, §6.1) — every replica evicts that cache entry immediately, sub-second typical. A `lookup` for an erased uuid returns `not-found` (§1), never stale cached PII; the TTL is the fallback bound for a missed invalidation message, not the primary mechanism — same push-fast/poll-fallback shape as every other invalidation path in this platform. |
| **Rename invalidation** | Same push mechanism, `identity-renamed:{uuid}`, so a changed handle/display name doesn't serve stale PII for up to the full TTL. |

### 3.5 Resource limits per frame

| Limit | Value | Mechanism |
|---|---|---|
| Frame size | Platform-specific ceiling (e.g. Discord gateway ~4KB typical, EventSub/webhook bodies capped per connections-credentials §4.2's 256KB) | Host rejects oversize frames before invoking `on-frame` — never a guest-side check |
| `on-frame` invoke deadline | Reuses the existing per-invoke epoch deadline (PR #406, `new_bounded_store`) | Unchanged mechanism, connector world included |
| Events per frame | `MAX_EVENTS_PER_FRAME` (default 64) | Host truncates `on-frame`'s returned list past the cap, logs `WARN`, counts a metric — bounds a pathological poll/batch response from fanning out unbounded downstream work |
| `identity.lookup` rate | Per-connector-digest token bucket, §3.4 | Independent of the frame-invoke budget — a lookup burst never blocks frame processing, and vice versa |
| Heartbeat isolation | `on-heartbeat-due` runs on the connection's own liveness timer, on a **separate executor slot** from the connection's `on-frame` dispatch queue | A slow/hanging `on-frame` invocation must never delay a HEARTBEAT/PING the transport needs to keep the socket alive — see §4's latency budget |

### 3.6 Interaction with PR #429 and PR #443 — what remains needed

§3.3's decision changes where tokenization happens: a connector bundle can call `identity.lookup` and tokenize `actor` **at the source**, inside `on-frame`, before the event ever touches the ingest→process Valkey stream. For a connector-originated event, this means the stream never carries raw PII in `actor` at all — which is exactly the property PR #429 (tokenize in `svc_process`) and PR #443 (encrypt `actor` on the wire in the meantime) exist to guarantee, just achieved one hop earlier and without ever putting plaintext PII on the wire in between.

| PR | Scope | Still needed once connectors land? |
|---|---|---|
| **#429 — `actor` tokenization in `svc_process`** | (a) Tokenizing `platform-event.actor` at ingest. (b) The pre-dispatch pass resolving **unstructured free-text mentions** (Twitch/IRC `@handle`, a bare `!so someuser` argument) after `consumes`/`command_prefix` filter matching. | **(a) superseded for cut-over platforms, kept as a defense-in-depth no-op/assertion.** Once a platform's connector is enforced (§5.2), its events arrive with `actor` already tokenized — #429's actor-tokenize step becomes a pass-through for that platform, but stays live as a second check (belt-and-suspenders on a hard PII invariant, not removed) and remains the **only** tokenization path for any platform still on its native/shadow path during migration. **(b) still fully needed, unconditionally.** Unstructured mention resolution requires the target app's own `consumes`/`command_prefix` filter, which a connector — platform-facing, not app-aware — has no way to evaluate; it can only run in `svc_process`, downstream of the connector, exactly as PR #429 designed it. |
| **#443 — envelope-encrypt `actor` on the ingest→process stream** | Protects `actor` in transit between `svc_ingest` and `svc_process` while it can still be plaintext PII. | **Narrows, doesn't disappear.** For a **cut-over connector platform**, `actor` is a token before it ever reaches the stream — encrypting an already-non-PII field is moot for that field, on that platform. It remains needed for: (1) **every platform still in its migration/shadow window** (§5.2) — the native path still writes raw `actor` until that platform's connector is enforced and the native code is decommissioned (§6); (2) **the residual free-text-mention case (#429(b) above)** — the raw mention text embedded in `payload-json` still crosses the stream in the clear until `svc_process`'s pre-dispatch pass resolves it, so `payload-json`'s mention-bearing text fields still need the same protection #443 designed for `actor`, indefinitely, regardless of connector cutover. **Practical effect:** #443's scope shifts from "protect `actor`" to "protect `payload-json`'s free-text fields, and protect `actor` only for not-yet-cut-over platforms" — worth a scoping note on that PR once this design lands, not a reason to drop it. |

---

## 4. Performance at 10k channels

**Per-frame WASM invocation cost.** With §2.3's always-pinned instance pool (no cold start on the hot path) and the component model's warm-call overhead (low single-digit microseconds for a `Store` with a pre-instantiated `Component`), an `on-frame` call itself is not the bottleneck — network I/O, Valkey `XADD`, and the `identity.lookup` round trip (§3.3/§3.4, a direct RO-replica read behind a per-replica cache, not a gRPC hop) dominate.

**Latency budget:**

| Stage | Target (p99) | Note |
|---|---|---|
| `on-frame` invoke (pinned instance, warm) | < 250 µs | Component-call overhead only; excludes any `http`/`identity` import the bundle calls |
| `identity.lookup` round trip | < 1 ms (cache hit), < 10 ms (cache miss, direct RO-replica read) | Per-replica `{identity-key -> identity-record}` cache (§3.4), TTL 5 min, push-invalidated on rename/erasure — no gRPC hop, unlike credential resolution |
| End-to-end: transport read → `on-frame` → `XADD` onto the partition stream | < 5 ms | Matches the existing partition-stream design's "receiver's own p99 bounded by Valkey round trips, not fan-out width" property (connections-credentials §4.1) |
| Heartbeat send, independent of `on-frame` queue depth | Platform's own interval (Discord ~41s, unaffected) | Enforced by §3.5's separate-executor-slot isolation, not a shared budget |

**Batching: return-side yes, call-side not required for Discord's hot path.** `on-frame -> list<normalized-event>` already lets one host call yield many events (a Discord `GUILD_CREATE` burst, a multi-notification EventSub delivery, a YouTube poll page) without multiple WASM invocations — this is the batching that matters. **Call-side batching (coalescing several distinct raw frames from one connection into a single `on-frame` call) is not needed at 10k-channel scale**: pinning already removes cold-start cost, and per-call overhead (µs) is far below the Valkey/network cost that already dominates the path. It remains a documented v3.1+ option, gated on actually observing per-call overhead become material under real load — not a day-one requirement, consistent with `critical-rules.md`'s "measure before optimizing a structural corner" posture implicit in this platform's other perf work (dataplane-scale's own capacity tables are all measurement-driven, not speculative).

**Instance pooling recap (§2.3):** fixed pool size = replica's executor worker-thread count, per (platform, connector digest); shared compiled `Component`, per-call fresh/reused `Store` from the pool — standard wasmtime multi-instance sharing, no new primitive.

---

## 5. Migration: order, parity, cutover

**All of it ships in v3.0.** Teams/Google Chat/Mattermost receivers are explicitly **out of scope for v3.0** (Justin's framing: "later") — the WIT world (§1) is shaped to accommodate them (webhook-transport family, same as Slack) without a breaking change, but no v3.0 phase below builds them.

### 5.1 Order

| Order | Connector | Why here |
|---|---|---|
| 1 | **Echo** (receiver+sender, `waddles.core.example.echo`-style reference) | Proves the entire connector pipeline (host transport skeleton, `identity` import, pinning, signing) against a synthetic, zero-real-traffic connector before touching any live platform. |
| 2 | **Twitch EventSub** (receiver) | Stateless webhook transport — no session/RESUME/sharding complexity. The already-correct `eventsub.py`/Rust-port logic (connections-credentials §3.2 Increment 4) targets the connector WASM shape **directly** — this design supersedes that spec's "port to native Rust first" step; porting straight to WASM avoids a double migration. |
| 3 | **Twitch IRC** (receiver) | Persistent connection, but no sharding/RESUME — validates the WebSocket-family transport's heartbeat/keepalive isolation (§3.5) and IRC channel pooling (§2) before Discord's harder case. |
| 4 | **Discord Gateway** (receiver) | Hardest: sharding, IDENTIFY budget, and — new in this design — real `OP_RESUME`/session storage, closing a gap the native implementation never had. |
| 5 | **Slack** (receiver) | Webhook-based (Events API + signing secret) — same transport family as EventSub, low incremental risk once §3.2 lands. |
| 6 | **Senders: Twitch, Discord, Slack** | Share credential/identity plumbing with their just-landed receivers; migrate `handle_relay`/`handle_discord_relay` off `svc_action::capabilities.rs` onto `sender.build-request`. |
| 7 | **Kick** (receiver+sender) | New transport family (Pusher) — sequenced after the WebSocket family is proven, reusing its reconnect/backoff primitives rather than building a third from scratch. |
| 8 | **YouTube** (receiver+sender) | New transport family (polling) — independent of Kick's Pusher work, can run in parallel with it. |

### 5.2 Parity testing

Every connector's WASM `on-frame` output is verified against the native/Python implementation it replaces **before** any live traffic depends on it:

- **Golden-vector fixture per platform** — a fixed corpus of real captured raw frames (same pattern as PR #443's cross-language golden vector), run through both the old normalizer and the new connector bundle; assert semantically-equivalent `normalized-event` output (byte-identical is not the bar, since `actor` is now tokenized one hop earlier — assert the *resolved identity* matches, not the raw string).
- **Shadow mode, not a big-bang cutover.** The WASM connector runs alongside the existing native/Python receiver on live traffic, its output discarded, logged/counted at `WARN` on any divergence — the same shadow-then-enforce discipline as relay-authz (connections-credentials §6.2) and wit-v1.1's moderation rollout, 2–4 week soak window.
- **Cutover is a kill-switch, not a rewrite.** `waddles.core.disable-<platform>-connector-bundle` (opt-out, ON by default once shadow parity holds) — flipping it reverts instantly to the native/Python path without a deploy. The flag and its native-path branch are deleted once every platform has soaked clean and the native code is decommissioned (§6, final phase) — never left as a permanent lever, per `critical-rules.md`'s no-kill-switch-in-steady-state rule for security-relevant paths; this one is availability/parity-relevant, not security-relevant, so a temporary opt-out during migration is appropriate, but it is removed at decommission, not kept indefinitely.

---

## 6. Phased implementation plan (agent-sized, ≤30 min each)

| Phase | Task | Component | Depends on |
|---|---|---|---|
| 0 | `wit/waddle-connector/connector.wit`: define `types`/`identity`/`receiver`/`sender` interfaces + `connector` world (§1); `wasm-tools component wit` parses clean | WIT | none |
| 0 | `hub_internal.proto`: add `ResolveCredential`/`ResolveCredentialBatch`/`ResolveTenantDek` RPCs to `HubInternalService` (§2.4) — no identity RPC; `identity.lookup` is a direct RO-replica read (§3.3), not gRPC | proto | `feature/hub-api-internal-grpc` branch exists |
| 0 | Migration: `waddles_connector_pii_reader` RO role, column-scoped `SELECT` on identity columns only (§3.3) | hub-api (raw migration, RO replica) | none |
| 0 | Permission catalog: add `connector.receive:<platform>`/`connector.send:<platform>`/`connector.pii.read` ids to the PR #419 catalog module, global-tier-only (no tenant/community rows accepted, §3.1) | Rust (`bundle_capability_gate`) | PR #419 merged |
| 1 | `Transport` trait skeleton in `core/svc_ingest`: connect/reconnect/backoff hook points, `on-frame` dispatch queue on its own executor slot separate from heartbeat (§3.5) | Rust (svc_ingest) | Phase 0 |
| 1 | Always-pinned connector instance pool in the bundle executor (§2.3): `pinned: always` manifest flag, fixed `Store` pool sized to worker-thread count | Rust (`bundle_executor`) | Phase 0 |
| 1 | `identity.lookup` host import: connects as `waddles_connector_pii_reader` against the RO replica, per-replica `{identity-key -> identity-record}` cache (TTL 5 min), rate limiter (`UsageBatcher`), audit-log writer (§3.3/§3.4) | Rust (svc_ingest) | Phase 0's role-migration task |
| 1 | `identity-erased:{uuid}`/`identity-renamed:{uuid}` pub/sub invalidation: hub-api publishes on erasure/rename, the `identity.lookup` cache subscribes and evicts immediately (§3.4) | hub-api + Rust (svc_ingest) | previous task |
| 2 | Echo connector bundle (`waddles.core.example.echo`-style): `on-connect`/`on-frame`/`on-disconnect` over a synthetic in-memory transport; end-to-end pipeline smoke test | Rust bundle | Phase 1 |
| 2 | Shadow-mode harness: run a connector bundle alongside a native receiver on the same frames, diff `normalized-event` output, count divergences | Rust (svc_ingest test harness) | Phase 2 (Echo) |
| 3 | Twitch EventSub connector bundle: `on-frame` ports `eventsub.py`'s normalization logic (signature verification stays host-side, §2, before the bundle sees anything) | Rust bundle | Phase 1 |
| 3 | Wire the EventSub webhook receiver's `Transport` impl to dispatch into the connector bundle instead of `normalize_twitch_eventsub` | Rust (svc_ingest) | previous task |
| 3 | Golden-vector parity test, Twitch EventSub, shadow mode | Rust tests | previous task |
| 4 | IRC channel pooling (≤100/connection, JOIN/PART batching) in the `Transport` impl | Rust (svc_ingest) | Phase 1 |
| 4 | Twitch IRC connector bundle: `on-frame` ports `normalize_twitch_irc` | Rust bundle | Phase 1 |
| 4 | Golden-vector parity test, Twitch IRC, shadow mode | Rust tests | previous two tasks |
| 5 | Discord session storage (Valkey `session_id`+`seq` keyed by `source_id`) + real `OP_RESUME` on reconnect | Rust (svc_ingest) | Phase 1 |
| 5 | Discord shard supervisor (watermark-poll hot add/remove, scale-design §3.1/3.3) | Rust (svc_ingest) | previous task |
| 5 | Discord Gateway connector bundle: `on-connect` builds IDENTIFY/RESUME, `on-frame` ports `normalize_discord` | Rust bundle | Phase 5's session-storage task |
| 5 | Golden-vector parity test, Discord, shadow mode | Rust tests | previous task |
| 6 | `sender` world wiring in `svc_action`: `build-request` dispatch replacing `handle_relay`'s Twitch path | Rust (svc_action) | Phase 1, Phase 4 |
| 6 | `sender` world wiring in `svc_action`: `build-request` dispatch replacing `handle_discord_relay` | Rust (svc_action) | Phase 1, Phase 5 |
| 6 | Output-detokenization inside `sender.build-request` via `identity.lookup` (placeholder render immediately before the request template is returned, §3.3) — no host-side rendering path needed | Rust bundle | previous two tasks |
| 6 | Slack sender connector bundle + `build-request` wiring | Rust bundle + svc_action | Phase 7 (Slack receiver) |
| 7 | Slack webhook `Transport` impl (Events API signing-secret verification, host-side) | Rust (svc_ingest) | Phase 1 |
| 7 | Slack connector bundle: `on-frame` normalizes Events API payloads | Rust bundle | previous task |
| 7 | Golden-vector parity test, Slack, shadow mode | Rust tests | previous task |
| 8 | Pusher `Transport` impl (Kick): app-key handshake, subscribe/heartbeat framing, reusing WS reconnect/backoff | Rust (svc_ingest) | Phase 1 |
| 8 | Kick connector bundle (receiver+sender) | Rust bundle | previous task |
| 8 | Golden-vector parity test, Kick, shadow mode | Rust tests | previous two tasks |
| 9 | Polling `Transport` impl (YouTube): quota-aware backoff, continuation-token dedup | Rust (svc_ingest) | Phase 1 |
| 9 | YouTube connector bundle (receiver+sender) | Rust bundle | previous task |
| 9 | Golden-vector parity test, YouTube, shadow mode | Rust tests | previous two tasks |
| 10 | Flip each platform's `waddles.core.disable-<platform>-connector-bundle` off (WASM path live) once its shadow soak is clean | Ops (per platform) | Phases 3–9's respective parity tests |
| 10 | Decommission: delete `core/svc_ingest/src/ingest/{discord,twitch,twitch_eventsub}.rs`'s native normalize path, `core/svc_action`'s `handle_relay`/`handle_discord_relay`, and every per-platform kill-switch, once all platforms have soaked clean | Rust (svc_ingest, svc_action) | previous task, all platforms |
| 11 | OTel metrics for `on-frame`/`identity.lookup` latency (histograms, §4's budget), `waddles_connector_pii_lookup_total{platform,cache_hit}` (§3.4), connector-specific `authorize()` denial counters | Rust (both) | Phase 1 |

---

## 7. Open questions (not blockers, flagged for follow-up)

- **Verified vendor connector tier** (§3.2) — a future, narrower connector shape (no raw-frame/PII access, host-pre-extraction model) for third-party platform integrations. Not designed here.
- **Teams/Google Chat/Mattermost** — explicitly deferred past v3.0; the WIT world (§1) is shaped to accept a webhook-family transport for them without a breaking change, but no implementation phase above builds them.
- **Call-side batching of `on-frame`** (§4) — v3.1+ option if real load shows per-call overhead becoming material; not a v3.0 requirement.
- **`connector.receive:<platform>` periodic security review cadence** — the permission is pre-granted (system-approved, PR #419 §3.6 pattern) rather than human-reviewed per install; needs a recurring audit cadence analogous to that section's "flagged distinctly for periodic security review" note, not specified further here.
