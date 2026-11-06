# SPEC: Connector runtime and inbound manager (productionizing WireRig)

Date: 2026-11-05. Status: design. Branch `docs/connector-runtime-spec`. Milestone v3.0.x.

## 0. Ground truth

Verified facts on origin/release/v3.0.X (537c9026):
- `WireRig` (core/connector_e2e/src/lib.rs) is the only working wiring. It builds `Executor::new`, `pin_connector(manifest, wasm, limits, None)`, a `ConnectorRegistry` of `PinnedConnector::new(platform, app_id, digest, max_frame_bytes)`, an `ExecutorLink::new()`, and a `WireConnectorInvoker::new(link, registry)`. A duplex pipe stands in for mTLS. `bridge` is `None`.
- `ExecutorLink::attach` replaces the previous session ("one executor holds one connection").
- `WireConnectorInvoker` answers EVERY executor `host-call` with `HOST_CALL_DENIED` (wire_invoker.rs:417).
- `AppState::new` sets `webhook: None`, so `/intake/webhook/{route_key}` always answers 503.
- The legacy Twitch path discovers bindings with `twitch_sessions::spec::discover(r, timeout)` over `ConnectionResolve::list_bindings(PROVIDER, Some("chat_ingest"), after, PAGE_LIMIT=200)`. It has `MAX_PAGES=100`, a stuck-cursor check, a fail-closed rule for a channel claimed by several communities, a `SessionLease`, and `RECONCILE_INTERVAL=60s`.
- Overlay manifests import `identity`, `http`, `log`, `clock` and `flags`. Their `receiver` (`on-connect`/`on-frame`/`on-heartbeat-due`/`on-disconnect`) and `sender.build-request` are exports. The overlays are `world connector-tools`, so they also import `tool`.
- Overlay flags are read only inside the guests via `flags.enabled`.
- PR #1009 (`outbound_boot.rs`): the executor DIALS svc-ingest (`HostApiTls{server_cert,server_key,client_ca}`, port 8301, `SENDER_EXEC_DIGEST_<P>`, `CONNECTOR_TOOLS_ENABLED`, `CachedFlagGate`, `OVERLAY_FLAGS`). The production executor pins nothing and dials only `STAGE_HOST_API_ADDR`.
- `SpineEventSink::new(appender, metrics, keyring, kid, scope, dek_provider, identity_metrics)` takes ONE fixed `Scope`. A per-community runtime therefore needs one sink per connection.

## 1. Config model

**Decision 1.1. Source of "run overlay X for connection Y" is the hub-api binding feed. A platform-static `OverlayProfile` is compiled into svc-ingest.**
- Why: mirrors `twitch_sessions::discover`. URLs, slot prefixes, gateway rules and so on are security-relevant host constants and must never come from guest or binding data.
- No env override of gateway URLs in prod. Tests use `allow_loopback_insecure` + `egress_allow`.

```rust
// core/svc_ingest/src/connector_runtime/feed.rs
#[derive(Clone, PartialEq, Eq, Hash, Debug)]
pub struct ConnKey { pub platform: &'static str, pub community_id: i64, pub connection_id: i64 }

pub struct DesiredConnection { pub key: ConnKey, pub tenant_id: i64, pub plan: TransportPlan, pub fingerprint: u64 }

pub enum TransportPlan { Ws(Box<WsConfig>), Poll(Box<PollConfig>), Webhook(Box<WebhookPlan>) }

pub struct WebhookPlan { pub route: WebhookRoute /* connector::webhook */, pub authorized_community: i64 }

#[allow(async_fn_in_trait)]
pub trait BindingFeed: Send + Sync {
    /// Full desired set for one platform. Pages to completion (cap MAX_PAGES), dedups,
    /// fail-closed on cross-community claims. Err means "keep current set" (never "stop all").
    async fn desired(&self, platform: &'static str) -> Result<Vec<DesiredConnection>, FeedError>;
}
pub struct HubBindingFeed<R: ConnectionResolve, P: OverlayProfile> { /* resolver, profile */ }
```

```rust
// connector_runtime/plan.rs
pub trait OverlayProfile: Send + Sync {
    fn platform(&self) -> &'static str;                 // "twitch" | "discord" | "youtube"
    fn app_id(&self) -> &'static str;                   // waddles.core.service.<p>
    fn provider(&self) -> &'static str;                 // hub-api provider name
    fn capability(&self) -> &'static str;               // "chat_ingest"
    fn plan(&self, b: &BindingSummary, env: &RuntimeEnv) -> Result<Vec<DesiredConnection>, PlanError>;
}
pub struct RuntimeEnv { pub environment: String, pub max_frame_bytes: usize, pub egress_allow: Vec<IpAddr>, pub heartbeat_enabled: bool }
```

- `PlanError` carries no handle text, so malformed bindings are skipped and counted (`skipped_malformed`), never logged with PII.
- `fingerprint` is a hash of the derived config plus the connection's secret version, so reconcile can tell whether to restart. It excludes secrets.

**Decision 1.2. Per-platform derivation (README tables, `docs/connector-host-capability.md`).**
- Twitch IRC, `WsConfig`:
  - Fixed values: `url = wss://irc-ws.chat.twitch.tv:443`, `slot_prefixes = ["PASS oauth:", "NICK ", "JOIN #"]`, `auto_replies = [PING->PONG]`, `self_echo = Some(SelfEchoFilter{..})`, `heartbeat_interval` from the profile, `dedup_window = 0`.
  - `handles = {pass, nick, channel}`, all mapped to the community's `ConnectionRef`.
  - `TransportSecrets` resolves handle to field: `pass` from the secret, `nick` from `metadata.bot_login`, `channel` from `metadata.channel`. Raw logins exist only inside this transient resolution. They are never logged (use `channel_fingerprint`) and never reach the guest; the guest sees slots only.
  - A channel claimed by more than one community is skipped fail-closed, as in the legacy `discover`.
- Discord Gateway, `WsConfig`:
  - Fixed values: `url = wss://gateway.discord.gg/?v=10&encoding=json`, `slot_prefixes = ["\"token\":\""]`, `handles = {"discord-bot" -> conn}`.
  - Session rules: `reconnect_url` (`allowed_hosts ["*.discord.gg"]`), `heartbeat_ack{op 11, max_unacked 1}`, `handshake_after: FirstFrame`, `jitter_first_heartbeat`, plus `close_code` classification from README 1b / host-capability doc.
  - `session-token` JSON is host-maintained. `self_user_id` MUST be populated from READY or the bot ingests its own messages. The F2 owner implements the `SessionRules` capture rules; the e2e asserts the self-filter.
  - Intents stay 33281 and there is a single shard. `metadata.guild_id` identifies the source.
- YouTube `PollConfig`:
  - Fixed values: `url = https://www.googleapis.com/youtube/v3/liveChat/messages?...`, `auth = Bearer via PollAuth{header:"Authorization",prefix:"Bearer ",handle:"youtube.oauth"}`, `cursor_param = pageToken`, `cursor_pointer = /nextPageToken`.
  - The `liveChatId` comes from the binding metadata `live_chat_id`. This depends on #1008 and PR-B.
  - `interval` comes from `PollLifecycle` (`pollingIntervalMillis`). The `liveChatEnded` case parks the connection, and the next reconcile picks up a new `live_chat_id`.
- Webhooks (section 4): Twitch EventSub and Discord interactions are `TransportPlan::Webhook`, enabled per platform by `CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS`.

**Decision 1.3. Platform-level static config is env, set from helm values** (`pipeline.rustDataPlane.svcIngest.connectorRuntime.*`):

| Env (svc-ingest) | Default | Meaning |
|---|---|---|
| `CONNECTOR_RUNTIME_ENABLED` | false | master switch (also implied per platform by overlay baselines) |
| `OVERLAY_TWITCH_ENABLED`, `OVERLAY_DISCORD_ENABLED`, `OVERLAY_YOUTUBE_ENABLED` | false | env baseline for `waddles.overlay-<p>` (section 5) |
| `SENDER_EXEC_DIGEST_<P>` | none | pinned digest, shared with outbound (#1009); required per enabled platform |
| `CONNECTOR_RECONCILE_INTERVAL_SECS` | 60 | poll hub-api every N s, plus 0-20% jitter |
| `CONNECTOR_MAX_CONNECTIONS` | 500 | hard cap on supervised connections per replica |
| `CONNECTOR_START_CONCURRENCY` | 8 | bounded concurrent connects |
| `CONNECTOR_DISCORD_START_PACE_MS` | 5500 | min gap between Discord IDENTIFYs per process (the Valkey identify budget is deferred) |
| `CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS` | empty | csv of platforms whose webhook routes are registered |
| `CONNECTOR_ROUTE_KEY_SECRET` | none | HMAC key deriving route keys (secret env, never a flag) |
| `HOST_API_CLIENT_IDENTITY` | none | expected executor SAN (section 2) |

## 2. Executor link and loader

**Decision 2.1. The executor DIALS svc-ingest (as in #1009 and spec S6.6). svc-ingest listens on 8301 and the executor holds ONE connection to it.**
- Why: same socket direction as the svc-action stage link; the executor stays credential-less with no listening port.
- Executor: new `CONNECTOR_HOST_API_ADDR` (unset means no dial) and `CONNECTOR_HOST_API_IDENTITY`. A second dial loop runs next to the existing svc-action loop in `bundle_executor/src/lib.rs` (`connect_and_serve`). It uses `EXECUTOR_CONNECTOR_CONNECTIONS`, which is validated as exactly 1 because `attach` replaces the session (VERIFY: confirm the replace semantics in `wire_invoker.rs` and keep the validation).
- Executor config `validate()` must refuse `CONNECTOR_HOST_API_ADDR` without a pin list (below).

**Decision 2.2. mTLS identities with pinned peer SANs, on both sides.**
- svc-ingest has its OWN server cert/key (`svc-ingest-hostapi-tls` secret, SAN `spiffe://waddlebot/svc-ingest-hostapi`). It shares no key material with the svc-action stage cert.
- `HostApiTls` gains `client_identity: String`. `build_acceptor` wraps `WebPkiClientVerifier` in a verifier that, after chain validation, additionally requires the leaf SAN (URI or DNS) to exactly equal `HOST_API_CLIENT_IDENTITY` (the executor's SAN). A stage cert signed by the same CA is therefore refused and nothing attaches.
- Status (PR #1009, outbound boot wiring): svc-ingest already has its own server-only cert and client-identity pinning on its listener.
- Gap (executor side): the executor still needs a DISTINCT client cert. Today the executor client and the svc-action/svc-process stages share one cert whose SANs include the stage names, so SAN pinning on the stage listeners would accept a stage cert as the executor. PR-A1/H must provide a dedicated executor client cert with its own SAN (`HOST_API_CLIENT_IDENTITY`), and no stage cert may carry that SAN.
- The executor pins the server SAN to `CONNECTOR_HOST_API_IDENTITY` through the existing `tls.rs` pinning path (`HOST_API_STAGE_IDENTITY` precedent). It must not reuse the svc-action identity, so svc-action cannot impersonate svc-ingest.
- SSRF on dial: the executor dials only the configured address (a service DNS name, validated at config load); nothing from a frame ever selects an address.

**Decision 2.3. Pinning at executor startup.**
- New `core/bundle_executor/src/connector_boot.rs`. For each platform with `CONNECTOR_PIN_DIGEST_<P>=sha256:<hex>` set, it fetches `connectors/<platform>/<digest>.wasm` through the existing `ComponentSource` (bucket, signed via `BUNDLE_SIGNING_PUBLIC_KEYS`). It checks that the sha256 of the bytes equals the digest and verifies the signature. It builds `VerifiedManifest::new(app_id, digest, WitWorld::ConnectorTools, grants{connector.pii.read})`, then calls `pin_connector(.., ConnectorLimits::from_config, Some(bridge))`. Any failure aborts startup (fail loud).
- `app_id` is a host constant, as in `outbound_boot::app_id`.
- Helm renders one source of truth, `pipeline.connectors.digests.<p>`, into both `SENDER_EXEC_DIGEST_<P>` (svc-ingest) and `CONNECTOR_PIN_DIGEST_<P>` (executor).
- A digest mismatch surfaces as a typed unpinned error on the first call, which marks that platform not-ready (section 3). It is never papered over.

**Decision 2.4. The host-call bridge is an explicit allowlist in svc-ingest, a new `HostCallHandler` plugged into `ExecutorLink`.**

```rust
// connector/host_calls.rs
pub struct CallScope { pub platform: &'static str, pub community_id: i64, pub tenant_id: i64, pub connection_id: i64 }
#[allow(async_fn_in_trait)]
pub trait HostCallHandler: Send + Sync {
    async fn handle(&self, scope: &CallScope, call: &HostCallBody) -> Result<serde_json::Value, HostCallError>;
}
// ExecutorLink::with_host_calls(Arc<dyn HostCallHandler>) ; default stays "deny all".
```

- Scope: executor-side, `ExecState` stamps `args._scope = {connection_id}` into every host-call from the `ConnectionCtx` the host passed in the invoked export. The guest cannot alter it because the stamp is added after guest code. The svc-ingest handler maps `connection_id` to `CallScope` through the supervisor's ACTIVE-connection table (host-owned: `connection_id -> (community, tenant, platform)`). An unknown or stale id is `denied`. VERIFY whether `HostCallBody` carries a parent call id; if it does, prefer a keyed in-flight map.
- Allowlist, everything else gets `HOST_CALL_DENIED`:

| Capability / op | Behavior |
|---|---|
| `Flags.enabled` | key must be one of the three `waddles.overlay-<p>` flags and must match the scope's platform. The answer is the env baseline (`OVERLAY_<P>_ENABLED`), never a network flag lookup. Others get false (the default). |
| `Flags.tier` | `"free"`; overlays do not depend on it. |
| `Db identity.lookup` | Accept only `op = identity.lookup`. Require `args.tenant == scope.tenant_id` (executor cross-check, per connector_imports.rs layer 2). Set `waddles.tenant_id` from the scope, never from args. Echo `tenant` in the reply. Rate limit per digest: 200/s. |
| `Context get-context` | Return the host-derived tenant and community from `CallScope`. |
| `Http send` | Overlays use it for Discord guild/member permission lookups and YouTube/Twitch reads. Use `bundle_host_http::GuardedClient` (SSRF-guarded egress, #989). Allowed hosts per platform come from `approved_hosts(provider)`. Credentials are injected by slot via `connection_resolver`. Response body cap 256 KiB. |
| `Log` / `Clock` | Pass through, scrubbed (PII-free). Clock is `now`. |
| Everything else (kv, db, relay, ...) | denied; counted in `connector_host_call_total{capability,outcome="denied"}`. |

- Deadlines and admission: each host-call has a 2 s deadline (`EXECUTOR_CALL_TIMEOUT_MS`) and a per-digest semaphore of 16 in flight. Over cap gets typed `quota_exceeded`, not a queue. Per-scope fairness: at most 4 in flight per `connection_id`.
- `get-context` is required by `identity.lookup`'s `establish_tenant`; without it every actor-bearing event fails loud.

## 3. Lifecycle manager (svc_ingest `connector_runtime/`)

```rust
pub struct ConnectorRuntime<F: BindingFeed, I: ConnectorInvoker + 'static, S: EventSink + 'static, L: SessionLease + 'static> { .. }
pub trait RuntimeReady: Send + Sync { fn ready(&self, platform: &str) -> Readiness; }
pub enum Readiness { Disabled, NotReady(&'static str), Ready { live: u32, desired: u32 } }
impl ConnectorRuntime { pub async fn run(self, shutdown: oneshot::Receiver<()>); pub fn ready_handle(&self) -> Arc<dyn RuntimeReady>; }
```

- Reconcile loop: every `CONNECTOR_RECONCILE_INTERVAL_SECS` plus jitter, call `feed.desired(platform)` per enabled platform (10 s timeout). The diff between desired and running keys, plus a fingerprint change, yields start/restart/stop. A feed `Err` keeps the current set and flips `discovery_failed`; it never stops everything.
- Supervision: one tokio task per `ConnKey`, running `ConnectorDriver::run(transport, shutdown_rx)` with its own `Backoff`. Cap `CONNECTOR_MAX_CONNECTIONS`; excess desired connections are skipped and counted (`connector_runtime_capped_total`). Starts are bounded by `CONNECTOR_START_CONCURRENCY`, with `CONNECTOR_DISCORD_START_PACE_MS` for Discord.
- Leadership: `SessionLease` (twitch_sessions/lease.rs) keyed `connector:<platform>:<connection_id>`, `LEASE_TTL 250 s`. This allows multiple svc-ingest replicas with no duplicate connections.
- Per-community isolation: each connection has its own `TransportSecrets` bound to `community_id`, its own `SpineEventSink` (scope from the resolved numeric `community_id`/`tenant_id` via `scope_resolver`), its own backoff and its own task. A panic or error stays inside its task (`catch_unwind`/JoinHandle), counted and restarted after `RESTART_COOLDOWN 120 s`. Fatal `DriverError` and `RunEnd::Parked` go to a "parked" state until the fingerprint changes. Per-community rate caps live in `connector/ratelimit.rs`.
- Shutdown: send every `shutdown_rx`, join with a 10 s `STOP_TIMEOUT`, then abort. The lease is released.
- Metrics (bounded labels only: `platform` ∈ 3, `outcome` ∈ fixed enum; NEVER community or connection ids): `connector_runtime_desired{platform}`, `connector_runtime_live{platform}`, `connector_runtime_reconcile_total{platform,outcome}`, `connector_runtime_restarts_total{platform,reason}`, and the existing `ConnectorMetrics`. Logs and traces carry numeric ids only (community_id, connection_id), never raw handles.
- Readiness: `/readyz` (http/health.rs, `ReceiverReadiness`) adds fields per overlay platform: `enabled`, `executor_attached` (`ExecutorLink::is_attached`), `pinned_ok`, `desired`, `live`, `discovery_failed`. A platform is Ready when it is enabled, the executor is attached, the digest has answered at least one call, and the first reconcile has succeeded. `live` below `desired` for more than 5 minutes degrades to not-ready.
- Resource caps: max frame bytes (registry), an event-per-frame cap (`MAX_EVENTS_PER_FRAME`), an in-flight cap on `WireConnectorInvoker`, and a per-digest host-call semaphore (2.4).

## 4. Webhook route activation

- Boot builds `ConnectionResolverSecrets(Arc<resolver>)` (`WebhookSecretSource`), `RedisWebhookDedup::new(conn, ttl)` (existing Valkey connection, `DEDUP_TTL`) and `WebhookIngress::new(secrets, dedup, Some(&registry))`, and sets `state.webhook = Some(..)` BEFORE `http::router(state)`. Today `lib.rs` builds `AppState` and then the router without it, so the build must move earlier.
- Registry of trusted bindings (host-owned): for each `TransportPlan::Webhook`, `WebhookIngress::register(route, authorized_community)` and `bind_transport(route_key, owner)` so frames reach the driver through `WebhookTransport`. `register` is already fail-closed on community mismatch and cross-community takeover.
- `route_key = base64url(HMAC-SHA256(CONNECTOR_ROUTE_KEY_SECRET, "wh:v1:{platform}:{connection_id}"))` (43 chars, within 8-128). It is stable, unguessable, and carries no ids in the URL. hub-api (or the subscription creator) obtains the URL from svc-ingest by the same derivation. EventSub subscription creation is owned by svc-gateway (decision 1, resolved; see Scope above).
- Schemes: Twitch EventSub uses `Scheme::twitch_eventsub()` (HMAC, secret from the connection via `secret_ref`). Discord interactions use the Ed25519 `Ed25519Spec` (public key from the connection). The secret/public key stays inside `connection_resolver`; the guest never receives it.
- Scope (decision 1, resolved): Twitch EventSub subscription ownership belongs to svc-gateway (edge-reshape, `docs/superpowers/specs/2026-10-23-edge-reshape-gateway-dispatcher-design.md`). Until it exists, legacy keeps owning EventSub subscriptions and the Twitch overlay is IRC-only. `twitch` is NOT added to `CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS`. PR-D still builds the generic webhook activation (Discord interactions and future platforms).
- Dedup/replay: `replay_window <= DEDUP_TTL`, the Valkey dedup token store, the existing pre-auth/failure budgets (`MAX_FAILURES_PER_SOURCE`), and body caps (`HARD_MAX_BODY_BYTES 1 MiB`).
- Discord 3-second deadline: the route answers type-1 PING with `{"type":1}` itself and acks within the deadline (host-side, per README "Host-only concerns"). This is part of PR-D and needs an explicit test.
- 503 to live: `/intake/webhook/{route_key}` stays 503 (fail loud) while `state.webhook` is `None`; an unknown route key is a uniform 404/403 with no oracle. Rollout flag: `CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS=<csv>`. A platform not listed registers no routes, so a Twitch EventSub callback keeps its legacy handler and there is no doubling.

## 5. Host selector (PR0 from the T4 inventory)

**Decision 5.1. The selector reads an env baseline, NOT the PostHog client.**
- `*.waddles.app` licensing makes the flag client answer every flag ON, which would flip every environment at once. This follows the `CONNECTOR_TOOLS_ENABLED` and `INGEST_DYNAMIC_CONNECTIONS` precedent (default OFF).
- New `core/svc_ingest/src/connector_runtime/selector.rs`:

```rust
pub struct OverlaySelector { twitch: bool, discord: bool, youtube: bool }
impl OverlaySelector {
    pub fn from_env(lookup: &dyn Fn(&str) -> Option<String>) -> Result<Self, BootError>;
    pub fn overlay(&self, platform: &str) -> bool;      // overlay path active
    pub fn legacy(&self, platform: &str) -> bool { !self.overlay(platform) }
}
```

- Env names: `OVERLAY_TWITCH_ENABLED`, `OVERLAY_DISCORD_ENABLED`, `OVERLAY_YOUTUBE_ENABLED` (parsed strictly; a malformed value is fatal).

**Decision 5.2. Suppression, in `lib.rs run_with_shutdown`.** For each platform with the overlay ON, the legacy receivers and outbound drains for that platform are not started:

| Overlay ON for | Suppresses |
|---|---|
| twitch | `start_twitch_inbound` (static and dynamic `twitch_sessions`), `try_build_eventsub_state` only if `twitch` is in `CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS`, `try_start_twitch_outbound[_percommunity]` when `CONNECTOR_TOOLS_ENABLED` is ON |
| discord | `try_start_discord` (the external `penguin_connector_discord` path), `try_start_discord_outbound` when tools are ON |
| youtube | the Python `youtube_live_poll` receiver and legacy YouTube outbound |

- Python svc-ingest (`core/svc_ingest/app.py`): the Python receivers (`discord_gateway`, `twitch_irc`, `youtube_live_poll`) register on a `ReceiverSupervisor`. Add the same env baselines to the Python deployment (`OVERLAY_<P>_ENABLED`) and skip `supervisor.register` for that platform with one INFO line. Both services read the same helm value, so they cannot disagree. This is a small PR-E change in `app.py`.
- The outbound and inbound selectors are independent: `CONNECTOR_TOOLS_ENABLED` still governs the outbound drains (#1009); the overlay flag governs inbound. The README's outbound mode must remain consistent with `OVERLAY_FLAGS` (the executor path already answers the overlay flags true).

**Decision 5.3. Fail loud.** If any overlay is ON, startup validates, in a fixed order and naming the item but never a value: `INGEST_DYNAMIC_CONNECTIONS`, `HUB_API_GRPC_ENDPOINT`, `SERVICE_JWT_TOKEN_ENDPOINT`, `SENDER_EXEC_DIGEST_<P>` for each ON platform, host-API TLS files and `HOST_API_CLIENT_IDENTITY`, the spine config, and `CONNECTOR_ROUTE_KEY_SECRET` when any webhook platform is listed. A missing item is a startup error (`BootError`), like #1009. After boot, an unready runtime reports `/readyz` false with the platform and reason. It never silently falls back to legacy (the legacy path is already off) and never silently drops to nothing.

**Decision 5.4. Discord is blocked until the identify budget is ported (decision 5, resolved).**
- `OVERLAY_DISCORD_ENABLED=true` is a fatal boot error, naming item `I` (section 8), until the `IDENTIFY_BUDGET_READY` const/feature is true.
- Port: `core/svc_ingest/src/connector/identify_budget.rs` (see row I). `HandshakeGate` is awaited before IDENTIFY (not RESUME) in the WS driver.
- The start-pacing semaphore stays as defense in depth; it does not replace the budget.

## 6. Parity and e2e gates

- Extend `core/connector_e2e` with `ProdRig` (WireRig plus the real `ConnectorRuntime`, a fake `BindingFeed`/resolver, and a recording `EventSink`). Per platform:
  - Twitch: IRC and EventSub corpora from `tests/data_plane_parity/corpus/twitch/*.json` (23 vectors), through real wasm, driver, executor and sink. Assert normalized events equal the goldens, with only the declared divergences in `expected/twitch/divergences.json`.
  - Discord: 10/10 `MESSAGE_CREATE` vectors, plus a session-token and `self_user_id` self-echo test, plus the Ed25519 interaction path.
  - YouTube: poll corpus, plus a `liveChatEnded` park-then-rediscover test.
- Non-zero denominators: every parity test asserts `count > 0` of vectors, events, platforms and connections before comparing. A zero-input pass is a failure.
- Isolation tests: two communities, one failing, the other unaffected (events and backoff).
- Host-call tests: allowlist (denied capability gets `HOST_CALL_DENIED`), tenant echo mismatch, forged `_scope`.
- Suppression tests: with overlay ON the legacy starters are not called (a counting fake), and with it OFF the runtime is not built.
- Beta-critical trio as overlay-path acceptance, each run flag-ON against the real wasm: role sync (Discord role events to hub-api role sync), event sync Waddles to Discord (the outbound `outbound_exec.rs` path), and reputation (the `message` events driving P5 reputation). Each asserts a non-zero count and end-state.
- Gate: PR-G blocks T4 deletion and the alpha-to-beta flip.

## 7. Rollout

1. Deploy order: hub-api (feed fields) first, then svc-action (pin_sig v2 first, per the rollout note), then bundle-executor (pins, second dial), then svc-ingest (runtime plus selector). All overlay flags stay OFF at this point, which is a no-op.
2. Alpha OFF: verify `/readyz` unchanged and `connector_runtime_*` series are present at 0.
3. Alpha ON per platform, one at a time and in this order: youtube (poll, lowest blast radius), discord (single guild), twitch. Set `OVERLAY_<P>_ENABLED=true` in both svc-ingest deployments, then redeploy.
4. Alpha smoke proves: executor attached and `pinned_ok`; `desired == live` for the platform; a real message produces a spine event with a UUID actor and no doubling (compare against `svc_ingest_outbound_path` and legacy counters, which must be 0); webhook route 200 with a valid signature and 403 with a bad one; the trio passes.
5. Beta after a clean 48 h alpha soak.
6. Rollback: set `OVERLAY_<P>_ENABLED=false` in both deployments and redeploy. The legacy path restarts. Events in flight may be lost (acceptable on alpha).
- Per-environment env (helm): alpha ON per platform; beta and prod OFF until the trio passes there.

## 8. Task breakdown (file-disjoint)

Interfaces frozen so work can start now. All names below are normative.
- Env names: `CONNECTOR_HOST_API_ADDR`, `CONNECTOR_HOST_API_IDENTITY`, `EXECUTOR_CONNECTOR_CONNECTIONS` (must equal 1), `CONNECTOR_PIN_DIGEST_<P>`, `SENDER_EXEC_DIGEST_<P>`, `HOST_API_CLIENT_IDENTITY`, `OVERLAY_<P>_ENABLED`, and the table in 1.3.
- Types: `ConnKey`, `DesiredConnection`, `TransportPlan`, `BindingFeed`, `OverlayProfile`, `RuntimeEnv`, `RuntimeReady`/`Readiness`, `HostCallHandler`/`CallScope`, `OverlaySelector`.

| PR | Owner files | Notes |
|---|---|---|
| A1 | `core/bundle_executor/src/{config.rs,lib.rs,tls.rs,connector.rs,main.rs}`, new `connector_boot.rs` | second dial loop; boot pinning; `_scope` stamping into host-calls |
| A2 | `core/svc_ingest/src/connector/{host_api_listener.rs,wire_invoker.rs}`, new `connector/host_calls.rs` | SAN-pinning verifier; `ExecutorLink::with_host_calls`. Rebase onto #1009 (it owns `host_api_listener.rs`); coordinate to avoid conflicts |
| B | `hub_api/services/provider_connections/*` | depends on #1008; confirm `list_bindings` returns `guild_id`/`managed_roles` (#1002) and `live_chat_id` as runtime metadata |
| C | new `core/svc_ingest/src/connector_runtime/{mod,feed,plan,supervisor,metrics,ready}.rs` | no edits to existing files except `pub mod connector_runtime;` in `lib.rs` (a single line; coordinate with E, who owns the rest of `lib.rs`) |
| D | `connector_runtime/webhook_boot.rs`, `http/webhook.rs`, `http/mod.rs` | needs `WebhookPlan` from C |
| E | `core/svc_ingest/src/{lib.rs,config.rs}`, `connector_runtime/selector.rs`, `core/svc_ingest/app.py` | needs `RuntimeReady` from C; owns `lib.rs` (the `mod` line from C goes through E) |
| F1/F2/F3 | `connector_runtime/profiles/{twitch,discord,youtube}.rs` | each implements `OverlayProfile`; no shared file |
| G | `core/connector_e2e/**`, `tests/data_plane_parity/**` | after A1, A2, C, D, F* |
| H | helm templates and `values.yaml`; dedicated executor client cert (section 2.2) | names only; independent |
| I | new `core/svc_ingest/src/connector/identify_budget.rs`; port of `core/svc_ingest/src/ingest/discord_identify_budget.rs` (~1000 lines) | Valkey-backed global budget honoring `session_start_limit`/`max_concurrency`; `HandshakeGate` awaited before IDENTIFY (not RESUME) in the WS driver; host selector refuses `OVERLAY_DISCORD_ENABLED=true` until `IDENTIFY_BUDGET_READY` is true (section 5.4) |

Start now (against current release): A1, B, C, D, E, F1-F3, H, I. A2 starts now but rebases onto #1009. G waits.

## 9. Security review checklist (adversarial review)

- SAN pinning: a stage-CA cert is refused by the svc-ingest listener (no attach); an executor dialing a server with the wrong SAN is refused; svc-ingest's server cert is a separate key from svc-action's.
- Slot injection: a guest-influenced string containing `{{` cannot assemble a live slot (host substitutes last, `verify_slots`); `slot_prefixes` are host constants.
- Host-call forgery: a forged or stale `_scope`, a tenant arg that differs from scope, a cross-community `connection_id`, and denied capabilities (kv, db other than `identity.lookup`, relay).
- Replay: a replayed webhook within `replay_window` is deduped; a stale timestamp is refused; a dedup-store outage fails closed (no accept).
- Cross-community isolation: one community's panic, quota or backoff does not affect another; `register` rejects `secret_ref.community_id != authorized_community`; a contested Twitch channel is skipped.
- Secret handling: no secret reaches the guest, logs, labels or `Debug` (`Secret`/`SecretString` redaction); route-key secret and `CONNECTOR_ROUTE_KEY_SECRET` are env-only.
- SSRF: the dial address is config-only; `GuardedClient` plus egress allowlist for the `http` host-call; the WS and poll URLs are profile constants; redirects are not followed off-host.
- PII in logs, metrics and traces: no handles (use fingerprints), numeric ids only, no ids as metric labels, actor is a UUID.
- Fail-loud: overlay ON with a missing item is fatal; an unready runtime reports false and is never silent.
- Resource exhaustion: connection cap, start concurrency, frame and event caps, host-call semaphores.

## 10. Non-goals and deferrals

Sharding; the svc-gateway rename; removing the Python svc-ingest (only its overlay-platform receivers are suppressed); T4 deletion of the legacy code; Twitch EventSub subscription management in svc-ingest (owned by svc-gateway, decision 1); Slack/Kick/Spectrum overlays; multi-executor load balancing (one executor connection per svc-ingest replica for now); any new `penguin-libs` capability kind.

## Needs user decision (with recommendation)

1. **RESOLVED (user): svc-gateway owns EventSub subscriptions.** Legacy keeps ownership until svc-gateway exists; Twitch overlay is IRC-only; `twitch` NOT in `CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS`. See section 4 Scope. Original question: who creates Twitch EventSub subscriptions pointing at the generic route? Recommendation: keep legacy ownership for alpha, and make the overlay IRC-only until a hub-api subscription step exists. List `twitch` in `CONNECTOR_RUNTIME_WEBHOOK_PLATFORMS` only once it does.
2. **Python suppression mechanism.** Env baseline in both services (recommended, simple, symmetric) versus a hub-api flag read.
3. **Overlay ON with an unready runtime.** Fatal at boot (recommended for alpha and beta, matches #1009) versus serve `/readyz`=false only.
4. **Executor pairing.** One executor per svc-ingest replica (sidecar-like, recommended because `ExecutorLink` is single-session) versus a shared executor pool, which needs `ExecutorLink` multi-session support.
5. **RESOLVED (user): block Discord ON until the identify budget is ported** (section 5.4, task I). Original options: accept start pacing plus lease on alpha with the Valkey budget deferred (recommended), or block Discord ON until the budget is ported.

### Critical Files for Implementation
- /home/penguin/code/waddles/core/svc_ingest/src/lib.rs
- /home/penguin/code/waddles/core/svc_ingest/src/connector/wire_invoker.rs
- /home/penguin/code/waddles/core/bundle_executor/src/lib.rs
- /home/penguin/code/waddles/core/connector_e2e/src/lib.rs

## Status

- Design only — nothing in this spec is implemented yet as of 2026-11-05.
- Decision status (all five):

| # | Decision | Status |
|---|---|---|
| 1 | EventSub subscription ownership | RESOLVED by user: svc-gateway owns it; legacy keeps it until then; Twitch IRC-only; not in webhook platforms |
| 2 | Python suppression mechanism | ADOPTED per recommendation: env baseline in both services |
| 3 | Overlay ON with unready runtime | ADOPTED per recommendation: fatal at boot |
| 4 | Executor pairing | ADOPTED per recommendation: 1:1 executor per svc-ingest replica |
| 5 | Discord identify risk | RESOLVED by user: block Discord ON until identify budget is ported (task I) |
