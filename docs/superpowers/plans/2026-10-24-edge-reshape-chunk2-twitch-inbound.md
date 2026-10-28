# Edge-Reshape Chunk 2, Stream B2: Per-Community Twitch Inbound Sessions — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make svc-ingest's Twitch IRC receiver connection-aware: discover every paired community's Twitch connection via the Phase A `connection_resolver` crate and run one supervised IRC session per community (keyed by numeric community id), reconciled every ~60 s — replacing the single static `TWITCH_IRC_NICK/CHANNEL/OAUTH_TOKEN` identity that breaks multitenancy.

**Architecture:** A new `twitch_sessions` module in `core/svc_ingest` holds a `SessionManager`. Each reconcile tick (1) pages `list_bindings("twitch", …)` to a complete desired set, (2) diffs it against running sessions, (3) starts/stops/restarts sessions. Each session reuses the existing `ingest::twitch` reconnect loop (extracted to a public `run_with_connector`) driven by a `ResolvingConnector` that calls `resolve()` on **every** (re)connect — credentials are never cached. A Valkey lease per `(community_id, connection_id)` keeps replicas from double-publishing. Scope (tenant/community) for published events stays with the existing `ScopeResolver` keyed by the channel login, so dynamic sessions require `INGEST_DYNAMIC_CONNECTIONS` ON.

**Tech Stack:** Rust (Tokio, `redis =1.7.0`, `prometheus =0.14.0`, `tracing`), `core/connection_resolver` (Phase A, path dep), existing `penguin-connector-twitch` rev `ecf906a`.

**Spec:** `docs/superpowers/specs/2026-10-23-edge-reshape-gateway-dispatcher-design.md` (§ gateway as stateful platform edge, per-community connection model). Recon: memories `twitch-ingest-connection-gap`, `edge-reshape-chunk2-recon`. Phase A: `docs/superpowers/plans/2026-10-24-edge-reshape-chunk2-connection-store.md` (Task 7 = the contract consumed here).

## Global Constraints

- **penguin-spine is pinned (rev `90060a93`, `=0.2.0`) — DO NOT bump, no `:gateway` stage. Do NOT rename the `waddles.ingest.*` app_id namespace** and do not rename svc-ingest→svc-gateway. This plan changes no envelope/spine/`StageEnvelope` code.
- **svc-ingest is data-plane + security-sensitive → Rust only, never Go.** No `.unwrap()`/`.expect()` outside tests; `cargo fmt`/`clippy -D warnings`/`deny` clean; exact `=` pins, no PRC crates.
- **Multitenancy: NO single global Twitch connection.** Sessions are keyed by numeric `community_id` (+ numeric `connection_id`) — never slug/handle/channel name as a key. The static-env bridge is REJECTED as a real path. **Ruling:** the static `TWITCH_IRC_*` inbound path survives only as the flag-OFF (`INGEST_DYNAMIC_CONNECTIONS` unset/false) single-tenant dev fallback, logged at WARN; with the flag ON, `TWITCH_IRC_NICK`/`TWITCH_IRC_CHANNEL` being set is a **fatal startup error** (fail loud, never both). Deleting the fallback is a follow-up after alpha proves the dynamic path.
- **Credentials only via `ConnectionResolver::resolve()`, per connect, never cached** (`ResolvedConnection` is not `Clone`; no field on any B2 type holds a token past one `IrcConfig` build). The Twitch chat token is `ConnectionSecret::OAuthAccessToken`; any other secret variant is a loud `RegistrationRejected`-class failure, not a fallback. No static secrets; tokens never CLI args; masked in logs.
- **Resilience:** every `list_bindings`/`resolve` call is bounded by a timeout (`TWITCH_RESOLVE_TIMEOUT_SECS`, default 10) with exponential backoff + jitter on transient errors. **A transient hub error (`Transient`/`CircuitOpen`/`Auth`/timeout, or an incomplete discovery) must never tear down a healthy session** — only a complete, successful discovery that omits a binding stops its session.
- **PII-free telemetry:** a Twitch channel login and bot login are usernames = PII. Logs/spans/metric labels carry `community_id`, `connection_id`, op, outcome, counts only — never channel/bot login, token, or message text. The existing `ingest::twitch::run_loop` logs `source_id = tw-<channel>`; Task 1 removes that from log fields (stream key/workstream derivation still use it — it is an infra key, not a log field).
- **Entity identity:** numeric ids across the data-plane internal path; int enum codes, never strings, for outcomes in code (`SessionEnd`); metric label values are a closed set of static strings.
- **90%+ coverage; real-path tests** — a real TCP IRC session lifecycle against a local in-process IRC server (plaintext, `use_tls=false`), and a reconcile test that adds/removes/changes a binding and asserts session start/stop by observing real socket connects/closes; no-stubs/fail-loud — an unconfigured resolver with the flag ON, a missing metadata key, or a non-OAuth secret is a loud error. The only fake is the `ConnectionResolve` trait boundary (hub I/O), plus a live `#[ignore]` hub test (Task 9).
- **OTel logs + metrics + traces** (Rust = `tracing` + Prometheus registry per `telemetry.rs`; OTLP from `OTEL_*` env, never hardcoded; dead exporter never breaks the app). A gauge for live per-community session count is mandatory.
- **Every feature path behind a flag defaulted OFF:** reuses `waddles.ingest-dynamic-connections` (env baseline `INGEST_DYNAMIC_CONNECTIONS`, read via the existing `cli.dynamic_connections`, not `penguin_licensing`, because the `*.waddles.app` domain bypass evaluates every licensing flag ON — see `try_install_scope_resolver` doc, `lib.rs:~326`).
- **Base for execution:** fresh worktree off `origin/release/v3.0.X` tip `cfa003fe` (the orchestrator checkout is stale). Re-verify every path/line below with Read/Grep first; Phase A must be merged (or its branch stacked) so `core/connection_resolver` exists.

## Review Focus

- **Transient hub failure treated as "no bindings":** one flaky `list_bindings` stops every community's chat. (Task 5 `transient_list_error_keeps_all_sessions`, `empty_complete_discovery_stops_all_sessions`.)
- **Truncated paging read as the full set:** a page cap / mid-pagination error silently drops later communities. (Task 2 `mid_pagination_error_is_discovery_failure_not_a_shorter_set`, `page_cap_exceeded_is_failure`.)
- **Duplicate delivery:** two communities (or a stale+new connection) binding the same channel, or two svc-ingest replicas, publishing every message twice. (Task 2 `duplicate_channel_keeps_lowest_community_id_and_counts`, Task 4 `second_replica_cannot_acquire_a_held_lease`, Task 8 `two_managers_one_server_one_connection`.)
- **PII/secret leak:** channel/bot login or token in logs, spans, `Debug`, or metric labels. (Task 1 `run_loop_logs_carry_no_channel_login`, Task 3 `resolving_connector_error_and_debug_carry_no_token`, Task 6 `metric_label_values_are_a_closed_set`.)
- **Dead-session limbo / hot loop:** a revoked token ends the loop non-retryably; the session must be re-resolved and restarted on a later tick (self-heal once hub refreshes) with a cooldown — not left dead forever, not restarted every tick; likewise an edited channel must restart, not keep the old join. (Task 5 `finished_session_is_restarted_after_cooldown_with_fresh_resolve`, `channel_change_restarts_session`.)

---

### Task 1: Extract the per-session IRC loop (`run_with_connector`) and de-PII its logs

**Files:**
- Modify: `core/svc_ingest/src/ingest/twitch.rs` (`run_loop` `:87`, `run` `:245`, log fields throughout the loop)
- Test: `core/svc_ingest/src/ingest/twitch.rs` `#[cfg(test)]` (existing fakes at `:281-380`)

**Interfaces:**
- Consumes: existing `IrcConnector`/`IrcChannel` traits, `Backoff`, `STABILITY_WINDOW`, `publish_event`.
- Produces:

```rust
/// Identity attached to every log/span line of one session — numeric ids only.
#[derive(Clone, Copy, Debug)]
pub struct SessionLabel { pub community_id: i64, pub connection_id: i64 }  // static path uses {0,0}

pub async fn run_with_connector<C, A, M>(
    connector: C, configured_nick: &str, channel: &str, label: SessionLabel,
    appender: &A, metrics: &M, keyring: &KeyRing, active_kid: &str, scope: &Scope,
    shutdown: oneshot::Receiver<()>,
) where C: IrcConnector, A: EventAppender, M: SpineMetrics;
```
`run()` becomes a thin wrapper (builds `TwitchIrcReceiver`, `SessionLabel{0,0}`) — behavior-preserving for the static path.

- [ ] **Step 1: Write failing tests**

```rust
#[tokio::test] async fn run_with_connector_publishes_and_stops_on_shutdown() {}   // scripted connector, 2 msgs -> 2 appends; send shutdown -> future completes
#[tokio::test] async fn run_loop_logs_carry_no_channel_login() {}                  // tracing capture layer: run a connect-fail + closed + msg cycle for channel "secretchan"; assert no captured line/field contains "secretchan" or "tw-secretchan"; assert community_id field present
#[test] fn session_label_default_is_zero_for_static_path() {}
```
- [ ] **Step 2: Run to verify fail** — `cargo test --manifest-path core/svc_ingest/Cargo.toml ingest::twitch` → FAIL (items missing).
- [ ] **Step 3: Implement** — add `SessionLabel`, rename the pub entry `run_with_connector` (computes `source_id`/workstream from `channel` internally), replace every `source_id = %source_id` log field with `community_id = label.community_id, connection_id = label.connection_id`; keep `source_id` in publish calls. `run` delegates.
- [ ] **Step 4: Run** the full `ingest::twitch` suite (all pre-existing tests green, unchanged) → PASS.
- [ ] **Step 5: Commit** — `refactor(svc_ingest): extract per-session Twitch loop, drop channel login from logs`

**Deliverable:** one reusable, PII-free session loop; static path behavior unchanged.

---

### Task 2: Binding discovery → `SessionSpec` (paged, complete-or-fail, deduped)

**Files:**
- Create: `core/svc_ingest/src/twitch_sessions/mod.rs`, `core/svc_ingest/src/twitch_sessions/spec.rs`
- Modify: `core/svc_ingest/Cargo.toml` (`connection_resolver = { path = "../connection_resolver", version = "=<Phase A version>" }`), `core/svc_ingest/deny.toml`, `core/svc_ingest/src/lib.rs` (`pub mod twitch_sessions;`)
- Test: `core/svc_ingest/src/twitch_sessions/spec.rs` `#[cfg(test)]`; fixture check against Phase A's `hub_api/tests/fixtures/` binding-list golden JSON

**Interfaces:**
- Consumes: `ConnectionResolve::list_bindings(provider, capability, after, limit)`, `BindingSummary { community_id, connection_id, kind, metadata, .. }`, `ConnectionKind::PlatformOauth`, `ResolveError`.
- Produces:

```rust
pub const META_CHANNEL: &str = "channel";        // Twitch channel login (twitch adapter resource_id)
pub const META_BOT_LOGIN: &str = "bot_login";    // IRC nick the token authenticates as
pub const PAGE_LIMIT: u32 = 200; pub const MAX_PAGES: u32 = 100;

#[derive(Clone, PartialEq, Eq)]                  // Debug redacts channel/bot_login
pub struct SessionSpec { pub community_id: i64, pub connection_id: i64, pub channel: String, pub bot_login: String }
impl SessionSpec { pub fn key(&self) -> (i64, i64); pub fn from_summary(s: &BindingSummary) -> Result<Self, SpecError>; }

pub struct Discovery { pub specs: Vec<SessionSpec>, pub skipped_malformed: u32, pub skipped_duplicate: u32 }
pub enum DiscoverError { Resolve(ResolveError), PageCapExceeded, Timeout }
pub async fn discover<R: ConnectionResolve>(r: &R, timeout: Duration) -> Result<Discovery, DiscoverError>;
```
Channel/bot login normalized lowercase, leading `#` stripped, validated with a module-level `OnceLock<Regex>`-free hand check (`^[a-z0-9_]{1,25}$` via byte loop — no regex dep).

- [ ] **Step 1: Write failing tests**

```rust
#[tokio::test] async fn discover_pages_until_next_after_none() {}                       // 3 pages -> all items, calls = 3, `after` advances
#[tokio::test] async fn mid_pagination_error_is_discovery_failure_not_a_shorter_set() {} // page2 Transient -> Err(Resolve(Transient)); no partial Ok
#[tokio::test] async fn page_cap_exceeded_is_failure() {}                                // endless next_after -> Err(PageCapExceeded) after MAX_PAGES
#[tokio::test] async fn discover_times_out_per_call() {}                                 // hung resolver -> Err(Timeout) (start_paused)
#[test] fn from_summary_missing_channel_is_error() {}                                    // also missing bot_login, invalid chars "bad name", empty
#[test] fn from_summary_normalizes_hash_and_case() {}                                    // "#PenguinzPlays" -> "penguinzplays"
#[test] fn from_summary_rejects_non_oauth_kind() {}
#[test] fn session_spec_debug_redacts_logins() {}
#[tokio::test] async fn duplicate_channel_keeps_lowest_community_id_and_counts() {}      // two communities same channel -> one spec (lowest community_id), skipped_duplicate==1
#[tokio::test] async fn malformed_binding_is_skipped_and_counted_not_fatal() {}
#[test] fn contract_fixture_summary_parses_to_a_spec() {}                                // Phase A golden binding JSON -> SessionSpec; FAIL LOUD (message names the missing key) if Phase A's twitch adapter does not publish channel/bot_login — escalate to orchestrator, do not invent keys
```
- [ ] **Step 2: Run to verify fail** — `cargo test --manifest-path core/svc_ingest/Cargo.toml twitch_sessions::spec` → FAIL (module missing).
- [ ] **Step 3: Implement** `SessionSpec`, `from_summary`, `discover` (wrap each call in `tokio::time::timeout`; any error aborts the whole discovery).
- [ ] **Step 4: Run** → PASS; `cargo deny check` + `clippy -D warnings`.
- [ ] **Step 5: Commit** — `feat(svc_ingest): complete-or-fail Twitch binding discovery`

**Deliverable:** `discover()` returns the full desired set or a typed failure — never a silently partial set.

---

### Task 3: `ResolvingConnector` — credentials resolved on every connect

**Files:**
- Create: `core/svc_ingest/src/twitch_sessions/connector.rs`
- Test: same file `#[cfg(test)]`

**Interfaces:**
- Consumes: `ConnectionResolve::resolve`, `ConnectionRef`, `ConnectionSecret::OAuthAccessToken`, `ResolvedConnection.expires_at` (epoch secs), `ingest::twitch::{irc_config, IrcConnector, IrcChannel}`, `penguin_connector_twitch::TwitchError`.
- Produces:

```rust
pub const RECYCLE_MARGIN: Duration = Duration::from_secs(300);
pub struct ResolvingConnector<R: ConnectionResolve> { /* Arc<R>, spec, host, port, use_tls, timeout, clock fn */ }
impl<R: ConnectionResolve> IrcConnector for ResolvingConnector<R> { type Channel = DeadlineChannel<IrcConnection<IrcStream>>; /* resolve -> irc_config -> connect */ }
/// recv() returns Ok(None) (clean close -> loop reconnects + re-resolves) once `recycle_at` passes.
pub struct DeadlineChannel<C: IrcChannel> { /* inner, recycle_at: Option<Instant> */ }
pub(crate) fn map_resolve_error(e: &ResolveError) -> TwitchError;
```
Error mapping (class name only, never message body): `Transient|CircuitOpen|Auth` → `TwitchError::Connection` (retryable — hub trouble is not a dead session); `NotFound|NotUsable|Malformed` and non-OAuth secret → `TwitchError::RegistrationRejected` (non-retryable → loop ends; manager decides). **Ruling (token refresh):** IRC authenticates only at `PASS`, so a live session is recycled by clean-close at `expires_at − RECYCLE_MARGIN` (never mid-connect), forcing a fresh `resolve()` that returns the hub-refreshed token; `expires_at: None` ⇒ no recycle.

- [ ] **Step 1: Write failing tests**

```rust
#[tokio::test] async fn every_connect_calls_resolve_again() {}                       // connect x3 -> resolver.resolve count == 3 (nothing cached)
#[tokio::test] async fn resolved_token_reaches_irc_pass_with_oauth_prefix() {}       // local one-shot TCP listener captures "PASS oauth:<tok>" ; token never logged
#[test] fn map_resolve_error_table() {}                                              // each ResolveError -> expected retryable/non-retryable
#[tokio::test] async fn non_oauth_secret_is_rejected_loudly() {}                     // Bearer{..} -> RegistrationRejected
#[tokio::test(start_paused = true)] async fn deadline_channel_closes_cleanly_at_recycle_point() {}   // expires_at = now+301s -> recv Ok(None) after ~1s; margin respected
#[test] fn expires_at_none_or_past_margin_floor_never_busy_loops() {}                // expires_at already past -> recycle_at >= now+MIN_SESSION (30s floor), not 0
#[test] fn resolving_connector_error_and_debug_carry_no_token() {}                   // format!("{:?}") of connector + every mapped error contains no token/login
```
- [ ] **Step 2: Run to verify fail** — `cargo test … twitch_sessions::connector` → FAIL.
- [ ] **Step 3: Implement** — `connect()`: `timeout(resolve)` → build `IrcConfig` (drop the `ResolvedConnection` immediately after) → `TwitchIrcReceiver::new(cfg).connect()` → wrap in `DeadlineChannel`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `feat(svc_ingest): resolve Twitch creds per connect with pre-expiry recycle`

**Deliverable:** a connector that never holds a credential beyond one connect and recycles before expiry.

---

### Task 4: Per-connection session lease (replica singleton)

**Files:**
- Create: `core/svc_ingest/src/twitch_sessions/lease.rs`
- Test: same file; template = `RedisReplayGuard` (`ingest/twitch_eventsub.rs:214-260`, `SET key 1 NX PX`), client via `outbound::build_redis_client`

**Interfaces:**
- Consumes: Valkey (real test instance via the crate's existing `testing`/test Valkey harness; skip-with-FAIL, never silent-skip, when `VALKEY_URL` is unset in the live gate).
- Produces:

```rust
#[allow(async_fn_in_trait)]
pub trait SessionLease: Send + Sync {
    async fn acquire(&self, key: (i64, i64), ttl: Duration) -> Result<bool, LeaseError>;   // SET waddles:ingest:twitch:session:{community}:{connection} <replica_id> NX PX
    async fn renew(&self, key: (i64, i64), ttl: Duration) -> Result<bool, LeaseError>;     // compare-and-PEXPIRE (Lua) — false if no longer ours
    async fn release(&self, key: (i64, i64)) -> Result<(), LeaseError>;                    // compare-and-DEL
}
pub struct ValkeyLease { /* conn, replica_id: String (uuid v4 at boot) */ }
```
Lease key carries numeric ids only; value = random replica uuid (not a hostname/handle).

- [ ] **Step 1: Write failing tests**

```rust
#[tokio::test] async fn acquire_is_exclusive_and_renewable() {}
#[tokio::test] async fn second_replica_cannot_acquire_a_held_lease() {}
#[tokio::test] async fn renew_by_non_owner_returns_false_and_does_not_extend() {}
#[tokio::test] async fn release_only_deletes_own_lease() {}
#[tokio::test] async fn lease_expires_after_ttl_and_can_be_taken() {}
#[tokio::test] async fn valkey_error_is_lease_error_not_acquired() {}      // unreachable server -> Err(LeaseError), never Ok(true)
```
- [ ] **Step 2: Run to verify fail** → FAIL (module missing).
- [ ] **Step 3: Implement** `ValkeyLease` with two small Lua scripts (compare-and-pexpire / compare-and-del), per-command response timeout like `outbound::connect_with_response_timeout`.
- [ ] **Step 4: Run** against a real Valkey → PASS.
- [ ] **Step 5: Commit** — `feat(svc_ingest): valkey lease so one replica owns each Twitch session`

**Deliverable:** at-most-one live session per binding across replicas; lost lease is observable.

---

### Task 5: `SessionManager` — the reconcile engine

**Files:**
- Create: `core/svc_ingest/src/twitch_sessions/manager.rs`, `core/svc_ingest/src/twitch_sessions/metrics.rs` (trait + enums only)
- Modify: `core/svc_ingest/src/twitch_sessions/mod.rs` (re-exports)
- Test: same file `#[cfg(test)]` with a `SessionRunner` seam (below) for fast unit cases; the real-socket cases are Task 8

**Interfaces:**
- Consumes: `discover`, `SessionSpec`, `SessionLease`, `ConnectionResolve`, Task 3 connector, Task 1 `run_with_connector`; `SessionMetrics` trait (defined here in `twitch_sessions/metrics.rs`, Prometheus impl lands in Task 6; Task 5 tests use a recording impl).
- Produces (**the public interface of B2**):

```rust
pub const RECONCILE_INTERVAL: Duration = Duration::from_secs(60);
pub const LEASE_TTL: Duration = Duration::from_secs(180);            // 3x interval: survives two missed renews
pub const RESTART_COOLDOWN: Duration = Duration::from_secs(120);     // after a session ends on its own

#[derive(Clone, Copy, PartialEq, Eq, Debug)] pub enum SessionEnd { Shutdown = 1, NonRetryable = 2, LeaseLost = 3 }  // int-coded

pub struct ReconcileReport { pub desired: u32, pub started: u32, pub stopped: u32, pub restarted: u32, pub kept: u32, pub lease_denied: u32, pub discovery_failed: bool }

pub struct SessionManager<R: ConnectionResolve + 'static, L: SessionLease + 'static> { /* sessions: HashMap<(i64,i64), RunningSession{spec, handle, shutdown_tx, ended_at}> */ }
impl SessionManager {
    pub fn new(resolver: Arc<R>, lease: Arc<L>, deps: SessionDeps /* appender, keyring, active_kid, scope, irc host/port/tls, SessionMetrics */) -> Self;
    /// One reconcile pass; never panics, never returns Err (failures are in the report + metrics).
    pub async fn reconcile(&mut self) -> ReconcileReport;
    /// Loop: reconcile every RECONCILE_INTERVAL (+ ≤10% jitter) until `shutdown`; then stop all, release leases.
    pub async fn run(self, shutdown: oneshot::Receiver<()>);
    pub fn live_session_count(&self) -> usize;
}
```
Rules: key = `(community_id, connection_id)`; desired⊃running → keep (renew lease; renew false ⇒ stop that session, `LeaseLost`); new key → `acquire` then spawn `run_with_connector`; running⊄desired (only after a **successful** discovery) → shutdown+release; same key but `channel`/`bot_login` changed → stop + start (`restarted`); finished task (`ended_at` set) → eligible for restart only after `RESTART_COOLDOWN`, re-resolving fresh; `discovery_failed` ⇒ no start/stop/restart decisions at all, lease renewals still attempted.

- [ ] **Step 1: Write failing tests**

```rust
#[tokio::test] async fn new_binding_starts_a_session() {}
#[tokio::test] async fn removed_binding_stops_and_releases_its_session() {}
#[tokio::test] async fn transient_list_error_keeps_all_sessions() {}                // discovery Err -> report.discovery_failed, started=stopped=0, live count unchanged
#[tokio::test] async fn empty_complete_discovery_stops_all_sessions() {}            // Ok(empty) is a real answer
#[tokio::test] async fn channel_change_restarts_session() {}                        // same key, new channel -> old shut down, new started, restarted==1
#[tokio::test(start_paused = true)] async fn finished_session_is_restarted_after_cooldown_with_fresh_resolve() {}  // NonRetryable end -> not restarted at +60s, restarted at +121s; resolve count increments
#[tokio::test] async fn lease_denied_does_not_start_and_is_counted() {}
#[tokio::test] async fn lease_renew_false_stops_the_session() {}
#[tokio::test] async fn one_community_failure_does_not_block_others() {}            // lease Err for A, B still starts
#[tokio::test] async fn run_shutdown_stops_every_session_and_releases_leases() {}
#[tokio::test] async fn reconcile_is_idempotent_when_nothing_changed() {}           // 2nd pass: started=0 stopped=0 kept=N
#[tokio::test] async fn sessions_keyed_by_numeric_ids_not_channel() {}              // two connection_ids same community different channels -> 2 sessions
```
- [ ] **Step 2: Run to verify fail** — `cargo test … twitch_sessions::manager` → FAIL.
- [ ] **Step 3: Implement** — introduce `trait SessionRunner` (spawn one session → `JoinHandle<SessionEnd>`) with the real impl calling `run_with_connector`; unit tests inject a recording runner, Task 8 uses the real one. Reconcile is a pure diff + side-effect pass.
- [ ] **Step 4: Run** → PASS; clippy `-D warnings`.
- [ ] **Step 5: Commit** — `feat(svc_ingest): per-community Twitch session manager with reconcile loop`

**Deliverable:** `SessionManager` — desired-state reconciliation that is failure-safe by construction.

---

### Task 6: Prometheus impl of `SessionMetrics`, spans, and the live-session gauge

**Files:**
- Modify: `core/svc_ingest/src/telemetry.rs` (`IngestMetrics` `:109`, `register_ingest_metrics`)
- Modify: `core/svc_ingest/src/twitch_sessions/metrics.rs` (trait created in Task 5 Step 3)
- Test: `core/svc_ingest/src/twitch_sessions/metrics.rs`, `core/svc_ingest/src/telemetry.rs` tests

**Interfaces:**
- Produces:

```rust
pub trait SessionMetrics: Send + Sync {
    fn set_live_sessions(&self, n: usize);
    fn reconcile_done(&self, outcome: ReconcileOutcome, secs: f64);     // ok|discovery_failed
    fn session_event(&self, ev: SessionEventKind);                      // started|stopped|restarted|lease_denied|lease_lost
    fn resolve_failed(&self, class: &'static str);                      // transient|circuit_open|auth|not_found|not_usable|malformed|timeout
    fn discovery_skipped(&self, why: &'static str);                     // malformed|duplicate
}
// impl SessionMetrics for IngestMetrics. New prometheus series:
//   svc_ingest_twitch_sessions_live (IntGauge)
//   svc_ingest_twitch_reconcile_total{outcome} (IntCounterVec)
//   svc_ingest_twitch_reconcile_duration_seconds (Histogram)
//   svc_ingest_twitch_session_events_total{event} (IntCounterVec)
//   svc_ingest_twitch_resolve_errors_total{class} (IntCounterVec)
```
Spans: `twitch.reconcile` (fields: `desired`, `started`, `stopped`, `discovery_failed`), `twitch.session.connect` (`community_id`, `connection_id`, `attempt`) — numeric only. Also drive existing `receiver_connection_healthy{receiver="twitch_irc"}` as 1 iff ≥1 live session.

- [ ] **Step 1: Write failing tests**

```rust
#[test] fn gauge_tracks_live_sessions() {}                    // set_live_sessions(3) -> rendered text "svc_ingest_twitch_sessions_live 3"
#[test] fn reconcile_histogram_and_counters_render() {}       // ≥1 histogram sample after reconcile_done
#[test] fn metric_label_values_are_a_closed_set() {}          // fuzz resolve_failed/session_event with every enum variant; assert every rendered label value ∈ allow-list and none contains a login/token
#[test] fn register_ingest_metrics_registers_new_series_once() {}   // no duplicate-registration panic
#[tokio::test] async fn manager_updates_gauge_on_start_and_stop() {}
```
- [ ] **Step 2: Run to verify fail** → FAIL.
- [ ] **Step 3: Implement** counters/gauge/histogram + `impl SessionMetrics for IngestMetrics`; add `#[tracing::instrument(skip_all, fields(…))]` on `reconcile`.
- [ ] **Step 4: Run** → PASS; smoke render via `render_metrics`.
- [ ] **Step 5: Commit** — `feat(svc_ingest): twitch session gauge, reconcile + resolve-error metrics`

**Deliverable:** operators can see live per-community session count, reconcile health, and resolve failure classes — PII-free.

---

### Task 7: Config + startup wiring (dynamic mode vs flag-OFF static fallback)

**Files:**
- Modify: `core/svc_ingest/src/config.rs` (`CliConfig` near `twitch_irc_nick :145`; new fields; `Config::twitch_irc_enabled :302`), `core/svc_ingest/src/lib.rs` (`run_with_shutdown :257-292`, `try_start_twitch_irc :393`, `ReceiverReadiness`)
- Create: `core/svc_ingest/src/twitch_sessions/boot.rs` (`try_start_twitch_sessions`)
- Test: `core/svc_ingest/src/config.rs` tests, `core/svc_ingest/tests/twitch_sessions_boot.rs`

**Interfaces:**
- Consumes: `cli.dynamic_connections`, `service_auth::MachineJwtClient` (as `try_install_scope_resolver` builds it), `scope_resolver::check_transport`, `connection_resolver::ConnectionResolver` constructor (Phase A Task 7 — scopes `connections:list` unbound + `connections:resolve` community-bound).
- Produces: env `TWITCH_RECONCILE_INTERVAL_SECS` (default 60, min 10), `TWITCH_RESOLVE_TIMEOUT_SECS` (default 10); `fn validate_twitch_mode(cfg:&Config) -> Result<TwitchMode, ConfigError>` with `enum TwitchMode { Dynamic, StaticDev, Off }`; `pub fn try_start_twitch_sessions(config, metrics, readiness) -> anyhow::Result<()>`.

Mode table (ruling): flag ON + `TWITCH_IRC_NICK|CHANNEL` set → **Err** (fatal, message names both settings, no token value); flag ON + no env → `Dynamic`; flag OFF + env complete → `StaticDev` (existing path, WARN "single-tenant static identity — dev fallback"); flag OFF + no env → `Off` (today's behavior). `Dynamic` requires the scope resolver installed (`publish::SCOPE_RESOLVER` set) — otherwise Err (per-community scope comes from it). `TWITCH_IRC_OAUTH_TOKEN` is still read for the **outbound** relay only (see Open Items).

- [ ] **Step 1: Write failing tests**

```rust
#[test] fn flag_on_with_static_nick_or_channel_is_fatal() {}              // both orderings; error text contains no token
#[test] fn flag_on_without_static_env_is_dynamic() {}
#[test] fn flag_off_with_complete_static_env_is_static_dev_and_warns() {}
#[test] fn flag_off_without_env_is_off() {}
#[test] fn reconcile_interval_below_minimum_is_rejected() {}
#[test] fn dynamic_without_service_jwt_endpoint_is_fatal() {}              // fail loud, mirrors try_install_scope_resolver
#[test] fn dynamic_with_plaintext_hub_outside_alpha_is_fatal() {}          // check_transport reuse
#[tokio::test] async fn dynamic_mode_does_not_start_static_receiver() {}   // readiness.twitch_irc enabled by the manager task, no static spawn
#[tokio::test] async fn manager_task_ends_on_process_shutdown() {}
```
- [ ] **Step 2: Run to verify fail** → FAIL.
- [ ] **Step 3: Implement** — `validate_twitch_mode`; in `run_with_shutdown` replace the `try_start_twitch_irc(...)` call with a `match` on mode (`Dynamic` → `try_start_twitch_sessions`, `StaticDev` → existing fn, `Off` → log). Spine connect uses the existing `connect_spine_with_retry`; keyring via `resolve_binding_keyring`.
- [ ] **Step 4: Run** `cargo test --manifest-path core/svc_ingest/Cargo.toml` (incl. `tests/run.rs`, `tests/main_binary.rs`) → PASS.
- [ ] **Step 5: Commit** — `feat(svc_ingest): wire connection-aware Twitch sessions behind INGEST_DYNAMIC_CONNECTIONS`

**Deliverable:** with the flag ON svc-ingest runs per-community sessions and refuses the static identity; flag OFF is unchanged.

---

### Task 8: Real-path tests — IRC session lifecycle + reconcile against a live local IRC server

**Files:**
- Create: `core/svc_ingest/tests/support/irc_server.rs` (in-process tokio IRC server), `core/svc_ingest/tests/twitch_sessions_real.rs`
- Test: those files

**Interfaces:**
- Consumes: Task 5 `SessionManager` with the real `SessionRunner`, Task 3 connector (`use_tls=false`, host/port → test server), a `ConnectionResolve` implemented over a mutable in-memory binding table (the hub boundary; Phase A's `ResolvedConnection` built with a known token), recording `EventAppender`, real Valkey for `ValkeyLease`.
- Produces: `IrcTestServer { addr, connections() -> Vec<ObservedConn{pass_has_token_for:(i64) via token→community map, nick, joined:String}>, send_privmsg(conn_idx, user, text), drop_conn(idx), live_conn_count() }` — speaks `PASS/NICK/JOIN`, replies `001` + `JOIN`, can reject (`464`/`ERROR`) a given token.

- [ ] **Step 1: Write failing tests**

```rust
#[tokio::test] async fn session_lifecycle_connect_receive_publish_drop_reconnect() {}  // real socket: server sees PASS+NICK+JOIN, PRIVMSG -> exactly 1 appended event; drop_conn -> reconnects and a 2nd resolve() happens
#[tokio::test] async fn reconcile_adds_session_for_new_binding_and_server_sees_the_join() {}
#[tokio::test] async fn reconcile_removing_binding_closes_the_socket() {}               // live_conn_count 1 -> 0 after the removing tick
#[tokio::test] async fn two_communities_two_independent_sessions_with_their_own_tokens() {}  // server maps PASS token -> community; each joins only its own channel; msg to A never published under B's source
#[tokio::test] async fn hub_outage_mid_run_keeps_sockets_open_and_recovers() {}          // resolver returns Transient for 3 ticks: live_conn_count unchanged, events still flow; then normal
#[tokio::test] async fn revoked_token_session_ends_then_self_heals_after_cooldown() {}   // server rejects token (ERROR) -> non-retryable end; after hub swaps token + cooldown tick -> reconnects
#[tokio::test] async fn two_managers_one_server_one_connection() {}                      // same bindings, shared Valkey, 2 managers -> exactly 1 server connection; one PRIVMSG -> 1 event
#[tokio::test] async fn token_never_appears_in_captured_tracing_output() {}              // capture layer over the whole lifecycle; assert no token, no channel login, no bot login
#[tokio::test] async fn session_recycles_before_token_expiry_with_fresh_resolve() {}     // expires_at = now+301s with paused-time wrapper; reconnect observed, 2nd token used
```
- [ ] **Step 2: Run to verify fail** — `cargo test --manifest-path core/svc_ingest/Cargo.toml --test twitch_sessions_real` → FAIL (harness missing).
- [ ] **Step 3: Implement** the IRC test server + tests; assert counts examined (connections observed ≥1, events appended ≥1) — a zero denominator FAILS (`Verification Integrity`).
- [ ] **Step 4: Run** tests ×3 (flake check) → PASS; confirm one test fails when `reconcile` stop-path is temporarily disabled (gate can fail), revert.
- [ ] **Step 5: Commit** — `test(svc_ingest): real IRC-socket lifecycle + reconcile tests for per-community sessions`

**Deliverable:** session start/stop/reconnect/isolation proven on real sockets, not mocks.

---

### Task 9: Chart grants, live hub e2e, docs, and the gate

**Files:**
- Modify: `k8s/helm/waddlebot/` svc-ingest values/templates (`INGEST_DYNAMIC_CONNECTIONS`, `TWITCH_RECONCILE_INTERVAL_SECS`, `TWITCH_RESOLVE_TIMEOUT_SECS`; remove `TWITCH_IRC_NICK`/`TWITCH_IRC_CHANNEL` from the alpha values where the flag is ON; svc-ingest machine-JWT scopes `connections:list` + `connections:resolve` in `_service_jwt.tpl`/`service-jwt-rbac.yaml` + its render test; `CiliumNetworkPolicy` egress svc-ingest → hub-api), `core/svc_ingest/README.md` (or the repo's existing svc-ingest doc), `Makefile`
- Create: `core/svc_ingest/tests/twitch_sessions_live_hub.rs` (`#[ignore]`), `make test-svc-ingest-twitch-live` target
- Test: Helm render test; live test

**Interfaces:**
- Consumes: Phase A live harness (`scripts/ci/connection-resolver-live.sh`: real hub-api + Postgres + a seeded Twitch connection pointing at the local IRC test server).
- Produces: documented runbook (connect a bot in webui → session appears within ≤ one interval; disconnect → stops); `make test-svc-ingest-twitch-live`.

- [ ] **Step 1: Write failing tests**

```rust
#[tokio::test] #[ignore] async fn live_hub_binding_created_via_api_starts_a_session_within_one_interval() {}   // real hub-api list_bindings/resolve; asserts IRC server saw a JOIN; counts printed
#[tokio::test] #[ignore] async fn live_hub_binding_deleted_via_api_stops_the_session() {}
```
Helm test: `helm template` for alpha renders `INGEST_DYNAMIC_CONNECTIONS=true`, no `TWITCH_IRC_NICK`/`CHANNEL`, and svc-ingest JWT scopes include both connection scopes; beta/prod values unchanged.
- [ ] **Step 2: Run to verify fail** — live test FAIL (target missing); `make helm-test` render assertion FAIL.
- [ ] **Step 3: Implement** chart + values + docs; Makefile target.
- [ ] **Step 4: Run the full gate** — `make lint test-security`, `cargo test/clippy/deny` for `svc_ingest` + `connection_resolver`, coverage ≥ 90 % on `twitch_sessions/*` (report numbers), `no-stubs` gate clean, `make test-svc-ingest-twitch-live`, telemetry smoke (log records ≥1, metric points ≥1 incl. `svc_ingest_twitch_sessions_live`, histogram ≥1 — counts printed).
- [ ] **Step 5: Commit** — `feat(chart,svc_ingest): grant connection scopes, enable dynamic Twitch sessions on alpha, docs`

**Deliverable:** deployable, documented, and verified end to end against a real hub-api.

---

## Open Items (for the orchestrator, not plan tasks)

- **Outbound relay is still single-identity.** `try_start_twitch_outbound` (`lib.rs:~583`) and `outbound.rs:125` `TwitchOutboundIdentity` read one static nick/token. B2 does not regress it (token env stays) but it is the same multitenancy gap on the send side — needs its own stream (per-community identity resolved per send, likely folded into B1's destination dispatch).
- **Metric label PII (pre-existing):** `svc_ingest_events_published_total{source_id}` carries `tw-<channel>`; B2 cannot remove it without touching spine-metrics call sites. Recommend a follow-up replacing it with a numeric community label once the scope resolver supplies it.
- **Phase A dependency:** B2 assumes the Twitch binding's `metadata` carries `channel` and `bot_login` (Task 2 `contract_fixture_summary_parses_to_a_spec` fails loud if not).
