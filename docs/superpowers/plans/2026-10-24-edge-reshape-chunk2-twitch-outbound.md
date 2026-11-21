# Edge-Reshape Chunk 2, Stream B3: Per-Community Twitch Outbound Send — Implementation Plan

> **Historical document.** Written against the repository layout at the time; some paths named below (shown as plain text, not links) have since moved, been renamed or been removed. Not maintained as current architecture.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a bundle's Twitch `chat.send` reply go out as the **community's own** Twitch bot identity into the **community's own** channel, resolved at send time through the Phase A `connection_resolver` — replacing the single static `TWITCH_IRC_NICK/OAUTH_TOKEN` identity (`outbound.rs:125` `TwitchOutboundIdentity`) and the bundle-supplied `channel` (`capabilities.rs:~1166`). Send-side sibling of B2 (inbound).

**Architecture:** svc-action stamps the numeric `community_id` (from the delivered envelope's `InvokeScope`, never from the bundle) onto the Twitch outbound queue envelope and stops emitting any bundle-named channel. svc-ingest's existing Twitch drain parses the new `community_id`, looks the community up in a TTL'd **binding directory** (built from B2's `discover()`; holds ids + non-secret metadata only), calls `resolve()` **per send**, builds an `IrcConfig` from the *binding's* `bot_login`/`channel` + the resolved OAuth token, sends over a fresh `TwitchIrcSender` connection (existing one-connection-per-message semantics), and drops the credential. All gated by `INGEST_DYNAMIC_CONNECTIONS` (svc-ingest) + a paired svc-action flag.

**Ruling — separate per-send resolve, NOT reuse of B2's live IRC sessions.** Reasons: (1) B2's session is a receive loop (`ingest/twitch.rs` `IrcChannel` exposes `recv` only) and the connector's only send on a live connection is `IrcConnection::send_and_quit` (`penguin-connector-twitch` `irc.rs:284`) which `QUIT`s — reuse needs a penguin-libs API change + rev bump (cross-repo, pinned `ecf906a`); (2) B2's lease (its Task 4) makes **one replica** own a community's session, but the outbound queue is a single `BRPOP` list any replica pops — the entry would routinely land on a replica that does not hold the session, forcing cross-replica routing; (3) the "extra connection" is transient (one per message, torn down after `QUIT`), not a second persistent connection per community, and matches today's real Twitch semantics; (4) identical credential contract (`resolve()` per use, never cached) keeps one audit story. Cost accepted: per-send connect latency + head-of-line blocking on the sequential drain (bounded by timeouts, Task 4); follow-up is a penguin-libs live-session send once a single-owner routing story exists.

**Tech Stack:** Rust (Tokio, `redis =1.7.0`, `prometheus =0.14.0`, `tracing`), `core/connection_resolver` (Phase A), B2's `twitch_sessions::{spec,connector,boot}`, existing `penguin-connector-twitch` rev `ecf906a`.

**Spec:** `docs/superpowers/specs/2026-10-23-edge-reshape-gateway-dispatcher-design.md`. Siblings: `…-chunk2-connection-store.md` (Phase A, Task 7 = consumed contract), `…-chunk2-twitch-inbound.md` (B2). Memories: `twitch-relay-channel-not-host-pinned`, `edge-reshape-chunk2-recon`, `provider-connection-framework`.

## Global Constraints

- **Stacks on Phase A (hard) and B2 (hard, for `discover`/`SessionSpec`/`map_resolve_error`/resolver boot helper).** Do not redefine them. If B2's names differ at execution time, adapt call sites only — never fork the logic.
- **penguin-spine pinned (`90060a93`, `=0.2.0`) — no bump, no `:gateway` stage; no `waddles.ingest.*` rename; no svc-ingest→svc-gateway rename.** No envelope/`StageEnvelope` change; only the Valkey outbound queue JSON (a plain list, versioned `v:1`) gains one additive field.
- **svc-ingest/svc-action are data-plane + security-sensitive → Rust only.** No `.unwrap()/.expect()` outside tests; `fmt`/`clippy -D warnings`/`deny` clean; exact `=` pins, no PRC crates.
- **No static secrets in dynamic mode.** Credentials only via `ConnectionResolver::resolve()`, **per send, never cached** (`ResolvedConnection` is not `Clone`; no field on any B3 type outlives one `IrcConfig` build). Non-`OAuthAccessToken` secret ⇒ loud failure. Flag ON + `TWITCH_IRC_OAUTH_TOKEN`/`TWITCH_IRC_NICK` set ⇒ fatal startup error (no static fallback while dynamic is on). Flag OFF = today's single-identity path, WARN-logged, unchanged.
- **Output-channel pinning (Chunk-1a invariant, extended to Twitch).** Under the flag the target channel comes **only** from the community's connection (binding metadata), never the bundle: svc-action emits no `channel`; svc-ingest's community path takes no channel parameter (pinning enforced by the *type signature*, Task 4) and ignores a stray `channel` on the entry. Today Twitch is NOT pinned (`capabilities.rs:~1166` reads `message.channel`) — this plan closes that.
- **Community scope is numeric and from the host, never the guest:** `community_id` = `InvokeScope.community_id` (`i32`, host-derived, `capabilities.rs:~94`); `0` = tenant-wide ⇒ no community ⇒ refused (a Twitch bot belongs to a community). Widened to `i64` on the wire/consumer. Never slug/channel/login as a key.
- **Resilience:** every `list_bindings`/`resolve`/IRC send bounded by a timeout (`TWITCH_RESOLVE_TIMEOUT_SECS` default 10 — shared with B2; `TWITCH_SEND_TIMEOUT_SECS` default 15) so one hung send cannot wedge the sequential drain. A failed send is dropped + counted (best-effort relay, as today — no new at-least-once claim), never retried against a credential/auth failure.
- **PII-free telemetry:** channel/bot login, token, message text never appear in logs, spans, `Debug`, or metric labels. Logs/spans carry `community_id`, `connection_id`, op, outcome-code only. Metric label values are a closed set of static strings.
- **Entity identity:** numeric ids internally; int-coded `SendOutcome`; strings only for the closed metric/ack label set.
- **90%+ coverage; real-path tests** — real TCP IRC lifecycle against a local in-process IRC server (plaintext, `use_tls=false`); only fakes are the `ConnectionResolve` boundary and the Valkey list (`RelaySource`) — plus a live `#[ignore]` hub test. No-stubs/fail-loud: unconfigured resolver with flag ON, missing metadata, non-OAuth secret, ambiguous or absent binding are loud, counted errors.
- **OTel logs + metrics + traces** (Rust = `tracing` + Prometheus registry per `telemetry.rs`; OTLP from `OTEL_*`, never hardcoded; dead exporter never breaks the app).
- **Flag reuse:** svc-ingest reuses `INGEST_DYNAMIC_CONNECTIONS` via `cli.dynamic_connections` (same dynamic path B2 ruled on; not `penguin_licensing`, the `*.waddles.app` bypass would force it ON — see `try_install_scope_resolver` doc). svc-action is a different process so it gets its own flag: `waddles.core.twitch-dynamic-outbound` (env baseline `SVC_ACTION_TWITCH_DYNAMIC_OUTBOUND`), defaulted OFF, wired like `egress_dispatcher_flag` (`capabilities.rs:~909`). The chart sets both together; a mixed state fails loud (Task 5).
- **Base:** fresh worktree off `origin/release/v3.0.X` tip `cfa003fe` + Phase A + B2 stacked. Re-verify every path/line with Read/Grep first (orchestrator checkout is stale; `core/connection_resolver` does not exist on `cfa003fe`).

## Review Focus

- **Bundle names the channel / forged channel reaches the wire:** a bundle (or a stale/forged queue entry) steers a bot into an arbitrary channel. (Task 2 `bundle_channel_is_never_emitted_under_flag`, Task 5 `stray_channel_on_community_entry_is_ignored`, Task 7 `forged_channel_never_reaches_irc_server`.)
- **Cross-community send:** community A's reply sent with community B's bot token/channel (directory key mix-up, `0`/tenant-wide, `i32`↔`i64`, dedup/ambiguity). (Task 2 `tenant_wide_community_zero_is_refused`, Task 3 `lookup_is_exact_community_id`, `two_bindings_for_one_community_is_ambiguous`, Task 7 `two_communities_two_channels_two_tokens`.)
- **Silent fallback to the static identity:** mixed flag state (producer ON/consumer OFF, or token env left set) quietly sends as the global bot. (Task 5 `community_entry_with_flag_off_is_rejected_not_sent_statically`, Task 6 `flag_on_with_static_token_is_fatal`.)
- **Credential/PII retention or leak:** token held past a send, or token/login in `Debug`/errors/spans/metric labels. (Task 4 `sender_holds_no_token_between_sends`, `debug_and_errors_carry_no_token_or_login`, Task 6 `metric_label_values_are_a_closed_set`.)
- **Stale directory / hung dependency:** a removed or edited binding keeps sending to the old channel; one hung resolve/IRC send wedges every community's chat. (Task 3 `resolve_not_found_evicts_directory_entry`, `refresh_error_serves_stale_within_grace_only`, Task 4 `hung_send_times_out_and_drain_continues`.)

---

### Task 1: Consumer wire schema — `community_id` + `ChatSendCommunity` action

**Files:**
- Modify: `core/svc_ingest/src/outbound_ops.rs` (`WireEnvelope :108`, `OutboundAction :55`, `parse_outbound_entry :184`, `dispatch_to :303`, `PlatformSender :279`, `OutboundEntry :137`)
- Test: same file `#[cfg(test)]` (existing parse tests `:519+`)

**Interfaces:**
- Consumes: existing schema (`v:1`, legacy text-only fallback).
- Produces:

```rust
// WireEnvelope gains:  #[serde(default)] community_id: Option<i64>,
// OutboundAction gains (additive variant):
/// Twitch-only: `chat.send` into the channel of `community_id`'s own connection. Carries NO channel by construction.
ChatSendCommunity { community_id: i64, text: String },
// PlatformSender gains a default method (existing impls untouched; Discord inherits the refusal):
async fn send_chat_community(&self, community_id: i64, text: &str) -> Result<(), SenderError> {
    Err(SenderError::Unsupported { platform: self.platform().to_string(), op: "chat.send.community" })
}
```
Rule: `op` absent/`chat.send` **and** `community_id` present ⇒ `ChatSendCommunity` (any `channel` field on the wire is discarded, never parsed into the action); `community_id <= 0` ⇒ `OutboundParseError::Malformed`; `community_id` on `chat.delete`/`dm.send` ⇒ ignored (not in B3 scope); `op()` returns `"chat.send"` for the new variant (metric/log continuity). Legacy and channel-bearing shapes unchanged when `community_id` absent.

- [ ] **Step 1: Write failing tests**

```rust
#[test] fn community_id_with_chat_send_parses_to_community_variant() {}        // {"v":1,"op":"chat.send","platform":"twitch","community_id":7,"text":"hi"} -> ChatSendCommunity{7,"hi"} (no channel needed)
#[test] fn stray_channel_on_community_entry_is_discarded_by_the_parser() {}   // same + "channel":"#evil" -> variant equal to the one above; no channel field exists on it
#[test] fn community_id_zero_or_negative_is_malformed() {}
#[test] fn community_entry_missing_text_is_missing_field() {}
#[test] fn entry_without_community_id_parses_exactly_as_before() {}           // legacy {"channel","text"} + channel-bearing v1 -> ChatSend (regression: golden strings from existing tests)
#[test] fn op_label_for_community_variant_is_chat_send() {}
#[tokio::test] async fn default_send_chat_community_is_unsupported_for_discord_sender() {}
```
- [ ] **Step 2: Run to verify fail** — `cargo test --manifest-path core/svc_ingest/Cargo.toml outbound_ops` → FAIL (variant/field missing).
- [ ] **Step 3: Implement** — field, variant, default trait method, `dispatch_to` arm, `op()` arm; fix every exhaustive `match` the compiler flags (`route`, tests).
- [ ] **Step 4: Run** the whole `outbound_ops` + `outbound` suites → PASS (pre-existing tests unchanged).
- [ ] **Step 5: Commit** — `feat(svc_ingest): additive community_id outbound schema, channel-less ChatSendCommunity`

**Deliverable:** the consumer can express "reply as community N" with no channel on the wire.

---

### Task 2: Producer — svc-action stamps `community_id`, drops the bundle channel (flag-gated)

**Files:**
- Modify: `core/svc_action/src/capabilities.rs` (`handle_twitch_relay :1156`, `EnvelopeFields :401`, `build_outbound_envelope :415`, `with_egress_dispatcher_flag :909` → add sibling), `core/svc_action/src/flags.rs` (new flag const + env baseline), `core/svc_action/src/main.rs`/wiring site that calls `with_egress_dispatcher_flag`
- Test: `capabilities.rs` `#[cfg(test)]` (existing relay tests `:3362`, fake `relay_queue`)

**Interfaces:**
- Consumes: `InvokeScope { community_id: i32, tenant_id, .. }`, `FeatureFlag`, existing `relay_queue.lpush`.
- Produces:

```rust
pub const TWITCH_DYNAMIC_OUTBOUND_FLAG: &str = "waddles.core.twitch-dynamic-outbound"; // flags.rs; env SVC_ACTION_TWITCH_DYNAMIC_OUTBOUND
impl CapabilitySet { pub fn with_twitch_dynamic_outbound_flag(mut self, flag: Arc<dyn FeatureFlag>) -> Self; pub(crate) async fn twitch_dynamic_outbound(&self) -> bool; }
// EnvelopeFields gains: community_id: Option<i64>   // emitted as a JSON number
```
Flag ON: require `scope.community_id >= 1` else `denied("no_community", …)`; envelope = `{v,op,platform,community_id,text}` — **no `channel` key**; `message.channel` is not required and is ignored (never forwarded, never logged by value; a mismatch/presence only increments a counter `svc_action_twitch_channel_ignored_total`). Flag OFF: byte-identical to today (regression).

- [ ] **Step 1: Write failing tests**

```rust
#[tokio::test] async fn flag_on_envelope_has_community_id_and_no_channel() {}              // capture lpush payload; assert keys == {v,op,platform,community_id,text}; community_id == scope.community_id
#[tokio::test] async fn bundle_channel_is_never_emitted_under_flag() {}                    // message_json channel "#evil": payload contains no "#evil", no "channel" key
#[tokio::test] async fn flag_on_missing_channel_in_message_is_fine() {}                    // channel no longer required
#[tokio::test] async fn tenant_wide_community_zero_is_refused() {}                         // scope.community_id==0 -> denied("no_community"); nothing queued
#[tokio::test] async fn flag_off_envelope_is_byte_identical_to_today() {}                  // golden string from existing test (:3362 shape)
#[tokio::test] async fn community_id_comes_from_scope_not_message_json() {}                // message_json {"community_id":999,...} ignored; payload carries scope's
#[tokio::test] async fn flag_unwired_reads_off() {}
#[test] fn envelope_golden_matches_svc_ingest_consumer_fixture() {}                        // shared JSON fixture file under core/svc_action/tests/fixtures + same file parsed by Task 1 test (cross-crate drift guard)
```
- [ ] **Step 2: Run to verify fail** — `cargo test --manifest-path core/svc_action/Cargo.toml relay` → FAIL.
- [ ] **Step 3: Implement** flag const/builder/reader; branch in `handle_twitch_relay` after sanitize/detokenize of `text` (detokenize + `sanitize_irc_component` still apply to text); extend `EnvelopeFields`/`build_outbound_envelope`; wire the flag at the construction site with the env baseline reader used for `egress_dispatcher_flag`.
- [ ] **Step 4: Run** full `svc_action` unit suite → PASS; clippy.
- [ ] **Step 5: Commit** — `feat(svc_action): host-pinned community_id on Twitch outbound, bundle channel dropped`

**Deliverable:** with the flag ON a bundle can no longer name a Twitch channel or a community.

---

### Task 3: `BindingDirectory` — community → binding (ids + non-secret metadata, no creds)

**Files:**
- Create: `core/svc_ingest/src/twitch_outbound/mod.rs`, `core/svc_ingest/src/twitch_outbound/directory.rs`
- Modify: `core/svc_ingest/src/lib.rs` (`pub mod twitch_outbound;`)
- Test: `directory.rs` `#[cfg(test)]`

**Interfaces:**
- Consumes: B2 `twitch_sessions::spec::{discover, SessionSpec, DiscoverError}`, `ConnectionResolve`.
- Produces:

```rust
pub const DIRECTORY_TTL: Duration = Duration::from_secs(30);
pub const STALE_GRACE: Duration = Duration::from_secs(120);       // serve-stale ceiling when refresh fails
pub const MISS_REFRESH_MIN_INTERVAL: Duration = Duration::from_secs(5); // on-miss refresh rate limit

pub enum LookupError { NoBinding, Ambiguous, DirectoryUnavailable }  // int-coded via .code() for metrics
pub struct BindingDirectory<R: ConnectionResolve> { /* Arc<R>, timeout, Mutex<Snapshot{by_community: HashMap<i64, Vec<SessionSpec>>, fetched_at: Instant}> — std Mutex NEVER held across .await */ }
impl<R: ConnectionResolve> BindingDirectory<R> {
    pub fn new(resolver: Arc<R>, timeout: Duration) -> Self;
    /// Exact numeric community match. Refreshes when TTL expired or on a (rate-limited) miss; exactly one binding or an error.
    pub async fn lookup(&self, community_id: i64) -> Result<SessionSpec, LookupError>;
    /// Drop a community's entry after `resolve()` says its connection is gone.
    pub fn evict(&self, community_id: i64);
}
```
Rules: complete-or-fail `discover` (B2) → replace the snapshot atomically; refresh failure keeps the old snapshot **only within `STALE_GRACE`**, then `DirectoryUnavailable`; >1 spec for a community ⇒ `Ambiguous` (never pick one — deterministic routing would be a silent mis-send; follow-up = carry `connection_id` from bundle context); a community deduped out by B2 (shared channel) reads as `NoBinding` (mirrors inbound suppression).

- [ ] **Step 1: Write failing tests**

```rust
#[tokio::test] async fn lookup_is_exact_community_id() {}                    // communities 7 and 70, 8 -> never prefix/neighbour match
#[tokio::test] async fn two_bindings_for_one_community_is_ambiguous() {}
#[tokio::test] async fn unknown_community_is_no_binding_after_one_rate_limited_refresh() {}   // 10 misses inside 5s -> 1 list_bindings round
#[tokio::test(start_paused = true)] async fn snapshot_refreshes_after_ttl() {}
#[tokio::test(start_paused = true)] async fn refresh_error_serves_stale_within_grace_only() {}  // +60s err -> stale ok; +121s err -> DirectoryUnavailable
#[tokio::test] async fn removed_binding_is_not_served_past_ttl() {}
#[tokio::test] async fn resolve_not_found_evicts_directory_entry() {}        // evict(7) -> next lookup refreshes
#[tokio::test] async fn concurrent_lookups_share_one_refresh() {}            // single-flight; 20 tasks -> 1 discover
#[test] fn directory_debug_redacts_logins_and_counts_only() {}
#[test] fn std_mutex_not_held_across_await() {}                              // compile-level: lookup future is Send + a clippy `await_holding_lock` deny
```
- [ ] **Step 2: Run to verify fail** — `cargo test --manifest-path core/svc_ingest/Cargo.toml twitch_outbound::directory` → FAIL.
- [ ] **Step 3: Implement** — snapshot swap under a short sync lock; refresh via an async single-flight guard (`tokio::sync::Mutex<()>`), snapshot read before/after the await (no sync lock across it).
- [ ] **Step 4: Run** → PASS; `cargo deny` + `clippy -D warnings`.
- [ ] **Step 5: Commit** — `feat(svc_ingest): community→Twitch binding directory for outbound sends`

**Deliverable:** an exact, bounded-staleness community→binding lookup that fails loud on absent/ambiguous.

---

### Task 4: `CommunityTwitchSender` — resolve per send, pinned channel, no retained secret

**Files:**
- Create: `core/svc_ingest/src/twitch_outbound/sender.rs`
- Modify: `core/svc_ingest/src/twitch_outbound/mod.rs` (re-exports)
- Test: `sender.rs` `#[cfg(test)]`

**Interfaces:**
- Consumes: Task 3 `BindingDirectory`, `ConnectionResolve::resolve`, `ConnectionSecret::OAuthAccessToken`, B2 `connector::map_resolve_error`, `ingest::twitch::irc_config`, `TwitchIrcSender`.
- Produces:

```rust
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum SendOutcome { Sent = 1, NoBinding = 2, Ambiguous = 3, DirectoryUnavailable = 4, ResolveFailed = 5, BadSecret = 6, IrcFailed = 7, Timeout = 8 }
impl SendOutcome { pub fn label(self) -> &'static str; }          // closed static set for metrics/ack codes

/// Seam over "connect fresh, PRIVMSG, QUIT" so tests substitute the transport; prod = TwitchIrcSender.
#[allow(async_fn_in_trait)] pub trait IrcSend: Send + Sync { async fn send(&self, cfg: IrcConfig, text: &str) -> Result<(), TwitchError>; }

/// The ONLY outbound API takes a community id and text — no channel parameter exists, so a caller cannot name one.
#[allow(async_fn_in_trait)] pub trait CommunitySend: Send + Sync { async fn send(&self, community_id: i64, text: &str) -> SendOutcome; }

pub struct CommunityTwitchSender<R: ConnectionResolve, I: IrcSend> { /* Arc<BindingDirectory<R>>, Arc<R>, irc: I, host, port, use_tls, resolve_timeout, send_timeout, metrics */ }
impl<R, I> CommunitySend for CommunityTwitchSender<R, I> { /* lookup -> resolve(ConnectionRef{community_id, spec.connection_id}) -> irc_config(host,port,spec.bot_login,spec.channel,token,tls) -> timeout(irc.send) -> drop cfg */ }
```
Flow detail: `resolve` `NotFound`/`NotUsable` ⇒ `directory.evict(community_id)` + `ResolveFailed`; `Transient|CircuitOpen|Auth` ⇒ `ResolveFailed`, no evict; non-OAuth secret ⇒ `BadSecret`; `TwitchError` non-retryable (auth) ⇒ `IrcFailed`, **no retry**; `expires_at` already past ⇒ still attempt once (hub is authoritative; a stale-expired token fails loudly as `IrcFailed`). `IrcConfig` (holds the token) is moved into `irc.send` and dropped; `ResolvedConnection` dropped right after the config build. Never retries internally (drain is best-effort).

- [ ] **Step 1: Write failing tests**

```rust
#[tokio::test] async fn send_resolves_on_every_call_nothing_cached() {}             // 3 sends -> resolver.resolve count == 3
#[tokio::test] async fn config_uses_binding_bot_login_channel_and_resolved_token() {}  // recording IrcSend sees nick==bot_login, channel==binding channel, password==oauth:<tok>
#[tokio::test] async fn sender_holds_no_token_between_sends() {}                    // size/field audit: no String/Secret field typed to hold a token; second send with a changed token uses the NEW token
#[tokio::test] async fn not_found_evicts_directory_and_reports_resolve_failed() {}
#[tokio::test] async fn transient_resolve_error_does_not_evict() {}
#[tokio::test] async fn non_oauth_secret_is_bad_secret_and_never_reaches_irc() {}
#[tokio::test] async fn no_binding_and_ambiguous_never_call_resolve() {}
#[tokio::test(start_paused = true)] async fn hung_send_times_out_and_drain_continues() {}   // IrcSend never returns -> Timeout after TWITCH_SEND_TIMEOUT_SECS; next call runs
#[tokio::test(start_paused = true)] async fn hung_resolve_times_out() {}
#[tokio::test] async fn irc_auth_failure_is_not_retried() {}                         // IrcSend called exactly once
#[test] fn debug_and_errors_carry_no_token_or_login() {}                             // format!("{:?}") of sender, every SendOutcome, every error string
#[test] fn send_outcome_labels_are_a_closed_static_set() {}
```
- [ ] **Step 2: Run to verify fail** — `cargo test … twitch_outbound::sender` → FAIL.
- [ ] **Step 3: Implement** (`RealIrcSend` wrapping `TwitchIrcSender::new(cfg).send(text)`); tracing span `twitch.outbound.send` with `community_id`, `connection_id`, `outcome` (code) only.
- [ ] **Step 4: Run** → PASS; clippy.
- [ ] **Step 5: Commit** — `feat(svc_ingest): per-send credential resolution for Twitch outbound`

**Deliverable:** a sender that can only target the community's own connection and keeps no credential.

---

### Task 5: Drain integration — route `ChatSendCommunity`, refuse mixed-config, metrics

**Files:**
- Modify: `core/svc_ingest/src/outbound.rs` (`drain_loop :~190`, `run :~275`, `RelaySource`/`IrcOutbound` unchanged), `core/svc_ingest/src/outbound_ops.rs` (`TwitchSender` impl `send_chat_community`), new `core/svc_ingest/src/twitch_outbound/metrics.rs`
- Test: `outbound.rs` `#[cfg(test)]` (existing fake `RelaySource`/`IrcOutbound` `:140-220`)

**Interfaces:**
- Consumes: Task 1 variant, Task 4 `CommunitySend`/`SendOutcome`, existing `route`/`TwitchSender`.
- Produces:

```rust
pub trait OutboundMetrics: Send + Sync { fn record(&self, outcome: SendOutcome, elapsed: Duration); fn record_refused_static(&self); }  // Prometheus impl in Task 6
// TwitchSender<'a, S: IrcOutbound> gains an optional community backend:
pub struct TwitchSender<'a, S: IrcOutbound, C: CommunitySend> { irc: &'a S, community: Option<&'a C> }  // None == flag OFF; absence handled explicitly below, never defaulted
// drain_loop gains a generic `C: CommunitySend` + `Option<&C>` (None == flag OFF)
```
Rules: `ChatSendCommunity` with backend `Some` ⇒ `community.send` → map `SendOutcome` to `SenderError` (`Sent`→Ok, `Timeout|IrcFailed|ResolveFailed|DirectoryUnavailable`→`Failed`, `NoBinding|Ambiguous|BadSecret`→`Rejected`, each logged WARN with community_id + outcome label only). `ChatSendCommunity` with backend `None` (flag OFF) ⇒ `Rejected` + `record_refused_static()` + ERROR log "community-scoped outbound entry on a static-identity drain — flag mismatch" — **never** sent via the static identity. A legacy `ChatSend{channel,..}` with backend `Some` (stale producer, flag OFF upstream) ⇒ `Rejected` loudly (a bundle-named channel is not honored in dynamic mode). Malformed/unknown handling unchanged.

- [ ] **Step 1: Write failing tests**

```rust
#[tokio::test] async fn community_entry_routes_to_community_sender_with_numeric_id() {}   // fake CommunitySend records (7,"hi"); static IrcOutbound fake never called
#[tokio::test] async fn community_entry_with_flag_off_is_rejected_not_sent_statically() {} // backend None -> static IrcOutbound fake call count == 0; refused_static metric == 1; ERROR logged
#[tokio::test] async fn stray_channel_on_community_entry_is_ignored() {}                  // payload has "channel":"#evil" -> fake CommunitySend sees only (id,text)
#[tokio::test] async fn legacy_channel_entry_in_dynamic_mode_is_rejected() {}
#[tokio::test] async fn every_send_outcome_is_recorded_exactly_once() {}                   // table over all 8 SendOutcome values
#[tokio::test] async fn one_failing_community_does_not_stop_the_drain() {}                 // A -> Ambiguous, B -> Sent; both processed
#[tokio::test] async fn malformed_entry_still_dropped_and_loop_continues() {}              // regression
#[tokio::test] async fn flag_off_legacy_path_is_unchanged() {}                             // existing static-identity tests pass untouched
```
- [ ] **Step 2: Run to verify fail** — `cargo test --manifest-path core/svc_ingest/Cargo.toml outbound::` → FAIL.
- [ ] **Step 3: Implement** — thread `Option<&C>` through `drain_loop`/`run`; `TwitchSender::send_chat_community`; `OutboundMetrics` trait + a no-op test impl.
- [ ] **Step 4: Run** all `outbound`, `outbound_ops`, `twitch_outbound` suites → PASS.
- [ ] **Step 5: Commit** — `feat(svc_ingest): drain routes community-scoped Twitch sends, refuses static fallback`

**Deliverable:** the drain sends per community in dynamic mode and fails loud on any mixed configuration.

---

### Task 6: Prometheus metrics + startup wiring (flag ON vs flag-OFF static)

**Files:**
- Modify: `core/svc_ingest/src/telemetry.rs` (new collectors), `core/svc_ingest/src/lib.rs` (`try_start_twitch_outbound :~583`), `core/svc_ingest/src/config.rs`, `core/svc_ingest/src/twitch_sessions/boot.rs` (expose B2's resolver builder as `pub(crate) fn build_connection_resolver(config) -> anyhow::Result<Arc<ConnectionResolver>>` if not already; **no logic fork**)
- Create: `core/svc_ingest/src/twitch_outbound/boot.rs` (`validate_outbound_mode`, `try_start_twitch_outbound_dynamic`)
- Test: `telemetry.rs`, `config.rs`, `core/svc_ingest/tests/twitch_outbound_boot.rs`

**Interfaces:**
- Produces: metrics `svc_ingest_twitch_outbound_total{outcome}` (counter; values = the 8 `SendOutcome::label()`s + `refused_static`), `svc_ingest_twitch_outbound_duration_seconds` (histogram, **no labels**), `svc_ingest_twitch_binding_directory_entries` (gauge); env `TWITCH_SEND_TIMEOUT_SECS` (default 15, min 1); `enum OutboundMode { Dynamic, StaticDev, Off }`; `fn validate_outbound_mode(cfg:&Config) -> Result<OutboundMode, ConfigError>`.

Mode table (ruling, mirrors B2's): flag ON + `TWITCH_IRC_OAUTH_TOKEN` **or** `TWITCH_IRC_NICK` set ⇒ **fatal** (names both settings, never a value); flag ON + neither ⇒ `Dynamic` (requires scope resolver installed + resolver builds, else fatal — same loud failures as B2 Task 7); flag OFF + token+nick ⇒ `StaticDev` (today's `try_start_twitch_outbound`, WARN "single-tenant static identity — dev fallback"); flag OFF + neither ⇒ `Off`.

- [ ] **Step 1: Write failing tests**

```rust
#[test] fn flag_on_with_static_token_is_fatal() {}                       // also nick-only; error text has no secret value
#[test] fn flag_on_without_static_env_is_dynamic() {}
#[test] fn flag_off_with_static_env_is_static_dev_and_warns() {}
#[test] fn flag_off_without_env_is_off() {}
#[test] fn dynamic_without_resolver_config_is_fatal() {}                 // fail loud, mirrors B2
#[test] fn send_timeout_below_minimum_is_rejected() {}
#[test] fn metric_label_values_are_a_closed_set() {}                     // iterate every outcome; label ∈ allowed set; no community/channel/login label exists
#[test] fn duration_histogram_has_no_labels() {}
#[tokio::test] async fn dynamic_mode_does_not_build_static_identity() {} // no TwitchOutboundIdentity constructed; drain started with Some(community sender)
#[tokio::test] async fn drain_task_ends_on_process_shutdown() {}
```
- [ ] **Step 2: Run to verify fail** → FAIL.
- [ ] **Step 3: Implement** collectors + `OutboundMetrics` impl; `validate_outbound_mode`; in `run_with_shutdown` replace the `try_start_twitch_outbound(config)` call with a mode `match` (Dynamic → build `BindingDirectory` + `CommunityTwitchSender` + `outbound::run` with `Some`, StaticDev → existing fn, Off → info log).
- [ ] **Step 4: Run** `cargo test --manifest-path core/svc_ingest/Cargo.toml` incl. `tests/run.rs`, `tests/main_binary.rs` → PASS.
- [ ] **Step 5: Commit** — `feat(svc_ingest): wire per-community Twitch outbound behind INGEST_DYNAMIC_CONNECTIONS`

**Deliverable:** flag ON runs the per-community sender and refuses the static identity; flag OFF is unchanged.

---

### Task 7: Real-path tests — Valkey list → drain → resolver → real IRC server

**Files:**
- Create: `core/svc_ingest/tests/twitch_outbound_realpath.rs` (reuses B2's in-process IRC server helper from `tests/support/`; extend it to record `PASS`/`NICK`/`JOIN`/`PRIVMSG` per connection)
- Test: same

**Interfaces:**
- Consumes: Tasks 1–6 with a fake `ConnectionResolve` (hub I/O boundary only) and a fake `RelaySource` queue; **real** `TwitchIrcSender` over a real TCP socket to the local server.

- [ ] **Step 1: Write failing tests**

```rust
#[tokio::test] async fn queued_community_reply_arrives_as_that_communitys_bot_in_its_channel() {}   // server saw NICK==bot_login, PASS oauth:<tok>, JOIN #chan, PRIVMSG #chan :text; counts printed
#[tokio::test] async fn two_communities_two_channels_two_tokens() {}                                // entries for 7 and 8 -> server saw two connections with disjoint nick/pass/channel; no cross-over
#[tokio::test] async fn forged_channel_never_reaches_irc_server() {}                                // entry {"community_id":7,"channel":"#evil",...} -> server never saw JOIN/PRIVMSG on #evil
#[tokio::test] async fn rotated_token_is_used_on_the_next_send() {}                                 // resolver returns tok1 then tok2 -> server saw tok1 then tok2 (nothing cached)
#[tokio::test] async fn removed_binding_stops_sends_within_ttl_and_never_sends_statically() {}      // binding removed -> NoBinding; server sees zero further connections; static identity never used
#[tokio::test] async fn hub_down_fails_sends_loudly_and_drain_keeps_running() {}                    // resolver Transient -> failed outcome counted; later healthy entry still delivered
#[tokio::test] async fn producer_to_consumer_roundtrip() {}                                         // svc_action build via capabilities test seam -> JSON -> svc_ingest parse -> delivered (drift guard end to end)
#[tokio::test] async fn telemetry_counts_reported_per_run() {}                                      // logs ≥1, metric points ≥1, histogram ≥1; counts printed; zero = FAIL
```
- [ ] **Step 2: Run to verify fail** — `cargo test --manifest-path core/svc_ingest/Cargo.toml --test twitch_outbound_realpath` → FAIL.
- [ ] **Step 3: Implement** only test support (server recorder) — production code is already in place; fix any gap the tests expose in Tasks 1–6.
- [ ] **Step 4: Run** → PASS; deliberately break pinning (re-add channel passthrough) once and confirm `forged_channel…` FAILS, then revert (gate-can-fail proof, per Verification Integrity).
- [ ] **Step 5: Commit** — `test(svc_ingest): real-path Twitch per-community outbound against live IRC server`

**Deliverable:** end-to-end proof that the reply leaves as the right bot into the right channel and nothing else.

---

### Task 8: Chart grants, live hub e2e, docs, and the gate

**Files:**
- Modify: k8s/helm/waddles/templates (svc-ingest + svc-action env/JWT-scope), `values-alpha.yaml` (+ beta/prod untouched), `core/svc_ingest/README.md`, `core/svc_action/README.md`, `Makefile`
- Test: Helm render test; live test

**Interfaces:**
- Consumes: Phase A live harness (`scripts/ci/connection-resolver-live.sh`), B2's `make test-svc-ingest-twitch-live` seeding.
- Produces: runbook (pair a bot in webui → bundle reply sends as that bot in that channel; unpair → sends refused with `NoBinding`); `make test-svc-ingest-twitch-outbound-live`.

- [ ] **Step 1: Write failing tests**

```rust
#[tokio::test] #[ignore] async fn live_hub_binding_reply_sends_as_that_bot() {}      // real hub-api list_bindings/resolve; local IRC server saw PASS/NICK/PRIVMSG; counts printed
#[tokio::test] #[ignore] async fn live_hub_unbound_community_send_is_refused() {}
```
Helm test: alpha renders `INGEST_DYNAMIC_CONNECTIONS=true` + `SVC_ACTION_TWITCH_DYNAMIC_OUTBOUND=true` **together**, no `TWITCH_IRC_OAUTH_TOKEN`/`TWITCH_IRC_NICK` on svc-ingest, svc-ingest JWT scopes include `connections:list` + `connections:resolve`; beta/prod values unchanged (flags OFF).
- [ ] **Step 2: Run to verify fail** — live test FAIL (target missing); `make helm-test` render assertion FAIL.
- [ ] **Step 3: Implement** chart + values + READMEs (document: ruling, flag pairing, ambiguity limitation, head-of-line trade-off) + Makefile target.
- [ ] **Step 4: Run the full gate** — `make lint test-security`; `cargo test/clippy/deny` for `svc_ingest`, `svc_action`, `connection_resolver`; coverage ≥ 90 % on `twitch_outbound/*` and the touched `outbound*.rs`/`capabilities.rs` hunks (report numbers); `no-stubs` gate clean; `make test-svc-ingest-twitch-outbound-live`; telemetry smoke (log records ≥1, metric points ≥1, histograms ≥1 — counts printed).
- [ ] **Step 5: Commit** — `feat(chart,svc_ingest,svc_action): enable per-community Twitch outbound on alpha, docs`

**Deliverable:** deployable, documented, and verified end to end against a real hub-api.

---

## Open Items (for the orchestrator, not plan tasks)

- **Stack order:** Phase A → B2 → B3 (B3 imports B2's `discover`/`SessionSpec`/`map_resolve_error`/resolver boot). If B2 slips, B3 Task 3 can inline a copy of `discover` only with an explicit "dedupe on merge" note — not recommended.
- **Multiple Twitch connections per community = refused (`Ambiguous`).** A bundle has no way to say which connection; needs `connection_id` in `InvokeScope` (a Chunk-1/dispatcher change). Product question: is one Twitch bot per community the intended 1.0 model?
- **Head-of-line blocking:** the drain is sequential `BRPOP`; a slow send delays all communities up to `TWITCH_SEND_TIMEOUT_SECS`. If alpha shows it, bounded concurrency keyed by `community_id` is the fix (not in scope).
- **Twitch auth rate limit** (per-account IRC auth attempts) is not enforced client-side; per-send connect could trip it for a very chatty bot. Revisit with the penguin-libs live-session send follow-up.
- **Verify at execution:** `ResolvedConnection` may expose a tenant id — if so, assert it equals the token/scope tenant in Task 4 (not in the frozen contract as quoted, so not assumed).
- **`chat.delete`/`dm.send` for Twitch** remain Helix stubs (`outbound_ops.rs:~376`); per-community Helix auth is the same resolver contract, separate stream.
