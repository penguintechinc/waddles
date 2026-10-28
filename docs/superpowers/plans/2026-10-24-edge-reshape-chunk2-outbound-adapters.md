# Edge-Reshape Chunk 2, Stream B1: Outbound Destination Adapters + Fan-Out — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let svc-action route a bundle's output op to EXTERNAL destinations (external HTTP, AWS Lambda, GCP Cloud Function) and FAN OUT one op to several destinations at once (e.g. gateway reply + Lambda + HTTP), each destination succeeding/failing independently, with every external adapter PII-scope NONE **by construction** (no detokenize code path exists in them, enforced by the Cargo dependency graph, not by review).

**Architecture:** A new leaf crate `core/outbound_adapters` holds one adapter per `ConnectionKind` (transport + envelope-wrap + per-destination auth + declared policy + declared `PiiScope`); it depends on `connection_resolver` (Phase A, frozen contract) and **never** on `egress_detokenizer`/`hub_client`/`svc_action`, so a detokenize call is un-compilable inside it. svc-action gains `outbound/` (policy executor: per-attempt `resolve()`, timeout, retry w/ backoff+jitter via the existing `retry.rs`, per-destination circuit breaker via `core/circuit_breaker`, deterministic idempotency key, OTel; plus the fan-out engine), extends `Destination` with `External`, and wires `handle_relay` behind two default-OFF flags. The platform/gateway leg is unchanged (Chunk 1a) and is just one fan-out leg that keeps the existing detokenize-at-drain path.

**Tech Stack:** Rust (Tokio, reqwest =0.12.28 rustls, `aws-sigv4`, `jsonwebtoken` ring backend, `sha2`), `core/connection_resolver` (Phase A), `core/circuit_breaker`, `penguin-logging`/`tracing`, Helm (`k8s/helm/waddlebot`).

**Spec:** `docs/superpowers/specs/2026-10-23-edge-reshape-gateway-dispatcher-design.md` (§6.2 dispatcher steps 1-5, §6.3 adapter unit, §6.4 PII scope table, §9 builder calls, anti-goal A1). Depends on Phase A plan `2026-10-24-edge-reshape-chunk2-connection-store.md` (Task 7 crate).

## Global Constraints

- **svc-action is in-line/data-plane + security-sensitive → Rust only. No Go. No new Python on this path.**
- **PII invariant (A1, spec §6.4): external HTTP / Lambda / GCP adapters are PII scope NONE by construction.** svc-action NEVER detokenizes for them; `core/outbound_adapters` has NO dependency (direct or transitive) on `egress_detokenizer`, `hub_client`, or `svc_action`, and no detokenize symbol. Tokens/UUIDs only on the wire. Detokenization stays solely on the platform/gateway egress path, unchanged from Chunk 1a (`capabilities.rs` `detokenize_text` callers are NOT touched).
- **Adapter inputs are a `TokenizedOp` newtype** — constructible only through a validator that rejects forbidden PII-bearing keys; adapters cannot receive a raw `serde_json::Value`.
- **Frozen contract consumed, not redefined:** `ConnectionResolver::resolve(ConnectionRef{community_id, connection_id}) -> Result<ResolvedConnection, ResolveError>`; `ResolvedConnection` is NOT `Clone`; `ConnectionKind{PlatformOauth=1,ExternalHttp=2,AwsLambda=3,GcpFunction=4}`; `ConnectionSecret` variants and `ResolveError::is_transient()` exactly as Phase A ships them. B1 imports only that crate's public interface.
- **No static secrets; creds only via `resolve()`, fetched per attempt, NEVER cached** (not in a map, not in an adapter field, not in a signed-URL cache). `ResolvedConnection` dropped at end of the attempt. Secret types redact in `Debug`.
- **Every destination call:** hard timeout, retry with backoff + full jitter (retry only on transient), per-destination circuit breaker, deterministic idempotency key `hex(sha256(op_id || 0x00 || connection_id))`, bounded response read (64 KiB). One destination failing never fails or delays its fan-out siblings beyond the shared deadline.
- **Destinations are referenced by numeric `connection_id` + numeric `community_id`** (from the trusted `InvokeScope`, never guest-supplied); never by name/slug/URL. Bundle-supplied URLs are impossible — the URL comes only from the resolved connection.
- **SSRF:** every external dial re-validates the resolved IP via `bundle_host_http::egress::is_forbidden_address(ip, false)` with DNS-pinned connect (defends DNS-rebinding even though Phase A validated the URL at write time). Loopback is permitted only through a `#[cfg(any(test, feature = "test-loopback"))]` constructor, never in a release build.
- **Flags (default OFF):** `waddles.core.outbound-destinations` (operational, env baseline like `EGRESS_VIA_DISPATCHER_FLAG`, requires that flag ON too) AND licensed `waddles.integrations.static_connections` (Professional; license server only — NEVER env-bypassable). Both must be ON or the call is refused `feature_disabled`. Unseen/unreachable → OFF (fail closed), cached last-known per `flags.rs`/`LicenseFlag`.
- **No PII/secrets in logs, spans, metric labels, error text:** labels = `kind` (closed enum), `outcome` (closed enum), `attempt`; never connection_id (unbounded), URL, host, region, function, or message text. Error `Display` carries kind + status class only.
- **Fail loud, no stubs:** unknown kind, no adapter registered for a kind, `PlatformOauth` routed to the external path, or fan-out width > 8 → loud typed error to the bundle, never a silent drop/fallback. No `.unwrap()`/`.expect()` outside tests; `cargo fmt`/`clippy -D warnings`/`deny`/`audit` clean; exact `=` pins; no PRC crates (`aws-sigv4`/`jsonwebtoken` are Western OSS, verify `cargo deny` licenses).
- **Rules:** every struct/fn gets a 2-3 line doc comment; no ASCII dividers; `{{user:<token>}}` placeholders pass through UNRESOLVED to external destinations (they are tokens, not PII).
- **Observability:** `tracing` spans per fan-out and per destination attempt; counters `svc_action_outbound_sends_total{kind,outcome}`; histogram `svc_action_outbound_latency_seconds{kind}`; `svc_action_outbound_fanout_width` histogram; OTLP from `OTEL_*` env; a dead exporter never fails a send.
- **90%+ coverage; real-path tests:** a real local HTTP sink (axum) for external HTTP; **real SigV4 signing asserted against the AWS published test vector AND recomputed independently in the sink**; **real RS256 GCP assertion verified by the local token endpoint against the public key**; no mocks of the unit under test.
- **Base branch for execution:** fresh worktree off `origin/release/v3.0.X` tip `cfa003fe` (orchestrator checkout is stale). Re-verify every path/line below with Read/Grep first. Phase A's `core/connection_resolver` must be merged (or stacked under) before Task 6; Tasks 1-5 compile against a path dep on it.
- **Out of scope:** gateway/overlay/streaming adapters (Chunk 3), GCP id-token caching (follow-up issue; v1 does one token exchange per send), Lambda synchronous `RequestResponse` (v1 = async `Event` invoke), workflow engine, svc-gateway changes.

## Review Focus

- **Detokenize reachable from an external adapter** (A1) via a future edit, a transitive dep, or an `Arc<dyn …>` smuggled into `AdapterCall`. (Task 1 `outbound_adapters_cargo_graph_has_no_pii_crates` using `cargo metadata`, + `adapter_call_has_no_resolver_field` source scan; Task 9 `external_fanout_leg_never_calls_detokenizer` with a counting resolver asserting 0 calls while the gateway leg calls it.)
- **SSRF / DNS-rebinding on a user-configured HTTP endpoint** (hostname resolving to 169.254.169.254, `::ffff:127.0.0.1`, redirect to internal, userinfo). (Task 3 `ssrf_rejects_*` table incl. metadata IP, IPv4-mapped IPv6, redirect-302-to-loopback not followed.)
- **Partial fan-out failure semantics:** one leg times out / opens its breaker while siblings succeed; platform reply must not be delayed or lost; a retried fan-out must not double-post to legs that already succeeded. (Task 7 `slow_leg_does_not_delay_siblings`, `one_leg_fails_others_succeed_report`, `rerun_with_same_op_id_reuses_idempotency_keys`; Task 6 `breaker_open_short_circuits_without_resolve`.)
- **Credential caching / leakage across retries or communities:** a retry reusing a stale expired cred; connection of community A resolvable under scope of B; secret in `Debug`/error/log. (Task 6 `every_attempt_calls_resolve_again`, `scope_community_is_used_not_op_supplied_community`; Task 3 `debug_and_error_output_redact_secret`.)
- **Signature correctness drift:** SigV4 canonical-request details (region/service, payload hash, header casing, session token header) and GCP assertion `aud`/`target_audience`/`exp` — a "signs something" mock would pass while AWS/GCP reject. (Task 4 `sigv4_matches_aws_published_vector` + sink-side recompute, `session_token_adds_security_token_header`; Task 5 `assertion_verifies_with_public_key_and_has_target_audience_equal_function_url`.)

---

### Task 1: `core/outbound_adapters` crate skeleton — trait, `TokenizedOp`, errors, PII-by-construction guard

**Files:**
- Create: `core/outbound_adapters/Cargo.toml`, `core/outbound_adapters/src/lib.rs`, `src/adapter.rs`, `src/tokenized_op.rs`, `src/error.rs`, `src/registry.rs`, `tests/pii_graph.rs`, `core/outbound_adapters/deny.toml`
- Modify: workspace/lock bookkeeping for the new crate (follow how `core/circuit_breaker` is registered — Grep for its `Cargo.lock`/CI matrix entries and mirror them, incl. the `ci-gate` cargo-build list per memory `ci-gate-missing-rust-build`)

**Interfaces:**
- Consumes: `connection_resolver::{ResolvedConnection, ConnectionKind}` (path dep), `bundle_host_http` NOT used here.
- Produces:
```rust
/// What sensitive data an adapter may ever carry. Only `None` exists in this crate.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PiiScope { None }

/// A bundle output op validated as tokens/UUIDs-only. Only constructor is `parse`.
pub struct TokenizedOp { /* op_id: String, op: String, provider: String, body: serde_json::Value */ }
impl TokenizedOp {
    pub fn parse(op_id: &str, op: &str, provider: &str, body: serde_json::Value) -> Result<Self, OpRejected>;
    pub fn op_id(&self) -> &str; pub fn op(&self) -> &str; pub fn provider(&self) -> &str;
    pub fn body(&self) -> &serde_json::Value;
}
pub enum OpRejected { ForbiddenField { key: &'static str }, TooLarge { bytes: usize }, NotObject }

/// Everything an adapter sees for one attempt. No resolver, no detokenizer, no http client handle.
pub struct AdapterCall<'a> {
    pub conn: &'a ResolvedConnection,
    pub op: &'a TokenizedOp,
    pub idempotency_key: &'a str,
    pub attempt_timeout: std::time::Duration,
}

/// Per-adapter policy the shared executor enforces.
#[derive(Clone, Copy, Debug)]
pub struct AdapterPolicy { pub attempt_timeout_ms: u64, pub max_retries: u32, pub base_backoff_ms: u64, pub max_backoff_ms: u64 }

pub struct AdapterOk { pub status: u16, pub detail: &'static str }
pub enum AdapterError {
    Transient { status: Option<u16>, retry_after_ms: Option<u64> },
    Permanent { status: Option<u16> },
    Timeout,
    Ssrf,
    Config,           // wrong secret variant for kind, malformed endpoint
}
impl AdapterError { pub fn is_transient(&self) -> bool; pub fn outcome_label(&self) -> &'static str; }

pub type AdapterFuture<'a> = std::pin::Pin<Box<dyn std::future::Future<Output = Result<AdapterOk, AdapterError>> + Send + 'a>>;

/// One adapter per ConnectionKind: transport + envelope + auth + declared policy and PII scope.
pub trait DestinationAdapter: Send + Sync {
    fn kind(&self) -> ConnectionKind;
    fn pii_scope(&self) -> PiiScope;
    fn policy(&self) -> AdapterPolicy;
    fn send<'a>(&'a self, call: AdapterCall<'a>) -> AdapterFuture<'a>;
}

pub struct AdapterRegistry { /* HashMap<ConnectionKind, Arc<dyn DestinationAdapter>> */ }
impl AdapterRegistry {
    pub fn new() -> Self;
    pub fn register(&mut self, a: Arc<dyn DestinationAdapter>) -> Result<(), RegistryError>; // dup kind or PlatformOauth => Err
    pub fn get(&self, kind: ConnectionKind) -> Result<&Arc<dyn DestinationAdapter>, RegistryError>; // missing => Err NoAdapter (loud)
}
```

- [ ] **Step 1: Write failing tests**

```rust
// tests/pii_graph.rs
#[test] fn outbound_adapters_cargo_graph_has_no_pii_crates() {
    // runs `cargo metadata --format-version 1` for this package; walks the full resolve graph;
    // asserts examined_nodes > 0 AND none of {egress_detokenizer, hub_client, svc_action,
    // bundle_host_db} appear. Prints examined count.
}
#[test] fn adapter_call_has_no_resolver_field() {
    // include_str!("../src/adapter.rs"): assert no "detokeniz", "Resolver" (except ConnectionKind/ResolvedConnection import), "hub_client".
}
// src/tokenized_op.rs tests
#[test] fn parse_rejects_forbidden_keys_at_any_depth_table() {} // email, phone, address, username, display_name, ip, real_name; nested + array
#[test] fn parse_accepts_user_token_placeholder_untouched() {} // text "hi {user:7f3a..}" round-trips byte-equal
#[test] fn parse_rejects_non_object_and_oversize() {} // > 256 KiB => TooLarge
// src/registry.rs tests
#[test] fn register_platform_oauth_is_error() {}
#[test] fn get_missing_kind_is_loud_no_adapter() {}
#[test] fn all_external_adapters_declare_pii_scope_none() {} // filled in Tasks 3-5 via a shared fixture list
```

- [ ] **Step 2:** Run `cargo test -p outbound-adapters` in the Rust container — expect compile failure.
- [ ] **Step 3:** Implement Cargo.toml (exact pins; `connection-resolver` path dep with `version = "=…"`), types above, `TokenizedOp::parse` forbidden-key walker (case-insensitive key match, recursive, depth cap 16, closed key list in one `const FORBIDDEN_KEYS`), registry.
- [ ] **Step 4:** Run tests; confirm graph test prints `examined_nodes=N` with N>0 and passes; temporarily add `egress_detokenizer` as a dep and confirm the test FAILS, then revert.
- [ ] **Step 5:** `cargo fmt && cargo clippy -p outbound-adapters -- -D warnings && cargo deny check`.
- [ ] **Step 6:** Commit `feat(outbound): adapter trait + TokenizedOp + PII-free crate graph`.

---

### Task 2: Secret-aware HTTP client + SSRF-guarded pinned dialer (shared by all three adapters)

**Files:**
- Create: `core/outbound_adapters/src/net.rs` (shared `GuardedClient`), `core/outbound_adapters/tests/net_ssrf.rs`
- Modify: `core/outbound_adapters/Cargo.toml` (`reqwest`, `bundle-host-http` path dep for `is_forbidden_address` only — Verify first that `bundle_host_http` has no PII/detokenize dependency: re-run Task 1 graph test afterwards)

**Interfaces:**
- Consumes: `bundle_host_http::egress::is_forbidden_address(ip: IpAddr, allow_private: bool) -> Option<&'static str>` (`core/bundle_host_http/src/egress.rs:1515`).
- Produces:
```rust
/// Builds a one-shot reqwest client whose connect is pinned to a validated IP; no redirects.
pub struct GuardedClient { /* allow_loopback: bool (cfg-gated ctor) */ }
impl GuardedClient {
    pub fn production() -> Self;
    #[cfg(any(test, feature = "test-loopback"))] pub fn allow_loopback_for_tests() -> Self;
    /// Resolves host, rejects any forbidden IP (all A/AAAA), pins first allowed, https-only in production.
    pub async fn post(&self, url: &str, headers: http::HeaderMap, body: Vec<u8>, timeout: Duration)
        -> Result<GuardedResponse, AdapterError>;
}
pub struct GuardedResponse { pub status: u16, pub retry_after_ms: Option<u64>, pub body_prefix: Vec<u8> } // body capped 64 KiB
pub fn classify_status(status: u16, retry_after_ms: Option<u64>) -> Result<AdapterOk, AdapterError>; // 2xx ok; 408/425/429/5xx transient; other 4xx permanent
```

- [ ] **Step 1: Write failing tests**

```rust
#[tokio::test] async fn ssrf_rejects_metadata_and_private_and_mapped_v6_table() {} // 169.254.169.254, 10.0.0.1, ::1, ::ffff:127.0.0.1, fd00::1 -> AdapterError::Ssrf
#[tokio::test] async fn ssrf_checks_every_resolved_address_not_just_first() {} // resolver stub returns [public, 127.0.0.1] -> rejected
#[tokio::test] async fn production_client_rejects_http_scheme() {}
#[tokio::test] async fn redirect_302_to_loopback_is_not_followed() {} // sink returns 302 -> result is Permanent{302}, second hit count == 0
#[tokio::test] async fn response_body_is_capped_at_64k() {}
#[tokio::test] async fn timeout_returns_adapter_timeout() {}
#[test] fn classify_status_table() {} // 200/202 ok; 408,425,429,500,502,503,504 transient; 400,401,403,404,410 permanent; Retry-After parsed (seconds + clamp)
```

- [ ] **Step 2:** Run — expect failures.
- [ ] **Step 3:** Implement `GuardedClient` using reqwest `resolve_to_addrs`/custom resolver pinning; `redirect(Policy::none())`; `Retry-After` seconds parse clamped to `max_backoff`.
- [ ] **Step 4:** Run tests (real axum sink on 127.0.0.1 via `allow_loopback_for_tests`); re-run Task 1 graph test.
- [ ] **Step 5:** clippy/fmt/deny.
- [ ] **Step 6:** Commit `feat(outbound): guarded SSRF-pinned http dialer`.

---

### Task 3: External HTTP adapter (pass-through POST, per-connection auth)

**Files:**
- Create: `core/outbound_adapters/src/external_http.rs`, `core/outbound_adapters/tests/external_http_sink.rs`
- Modify: `core/outbound_adapters/src/lib.rs` (re-export, `default_registry` helper)

**Interfaces:**
- Consumes: `GuardedClient` (Task 2); `ConnectionSecret::{Bearer, Basic, ApiKeyHeader}`; `Endpoint.url`.
- Produces:
```rust
pub struct ExternalHttpAdapter { /* client: GuardedClient */ }
impl ExternalHttpAdapter { pub fn new(client: GuardedClient) -> Self; }
// DestinationAdapter: kind()=ExternalHttp, pii_scope()=None,
// policy()={attempt_timeout_ms:10_000,max_retries:3,base_backoff_ms:200,max_backoff_ms:5_000}
// Envelope: pass-through — body = TokenizedOp.body JSON as-is, Content-Type application/json,
// headers: `Idempotency-Key`, `X-Waddles-Op` (op string), `X-Waddles-Provider`; auth per secret variant.
// Any other secret variant (AwsAccessKey/Gcp*/OAuth*) => AdapterError::Config.
```

- [ ] **Step 1: Write failing tests** (real axum sink recording method/headers/body)

```rust
#[tokio::test] async fn posts_op_body_verbatim_with_idempotency_header() {}
#[tokio::test] async fn bearer_basic_apikey_header_each_sent_correctly_table() {}
#[tokio::test] async fn wrong_secret_variant_for_kind_is_config_error_and_sends_nothing() {} // sink hits == 0
#[tokio::test] async fn sink_503_is_transient_400_is_permanent() {}
#[tokio::test] async fn user_token_placeholder_reaches_sink_unresolved() {} // body contains "{user:<uuid>}" literally
#[test] fn debug_and_error_output_redact_secret() {} // format!("{:?}", call), AdapterError Display: no token substring
#[test] fn declares_pii_scope_none_and_registers_under_external_http() {}
```

- [ ] **Step 2:** Run — expect failures. **Step 3:** Implement. **Step 4:** Run + coverage ≥90% for the file. **Step 5:** clippy/fmt/deny + Task 1 graph test. **Step 6:** Commit `feat(outbound): external http adapter`.

---

### Task 4: AWS Lambda adapter (SigV4-signed async Invoke)

**Files:**
- Create: `core/outbound_adapters/src/lambda.rs`, `core/outbound_adapters/tests/lambda_sigv4.rs`, `core/outbound_adapters/tests/vectors/aws_sigv4_get_vanilla.json` (AWS published sigv4 test-suite vector)
- Modify: `core/outbound_adapters/Cargo.toml` (`aws-sigv4`, exact pin, `default-features = false` + the minimal `sign-http` feature; Verify via `cargo tree` that no PRC/odd crate enters, `cargo deny` clean)

**Interfaces:**
- Consumes: `ConnectionSecret::AwsAccessKey{access_key_id, secret_access_key, session_token}`; `Endpoint{region, function, url}`.
- Produces:
```rust
pub struct LambdaAdapter { /* client: GuardedClient */ }
impl LambdaAdapter { pub fn new(client: GuardedClient) -> Self; }
/// Signs (region,"lambda") POST; exposed for the vector test.
pub(crate) fn sign_invoke(req: &UnsignedInvoke, key: &AwsKey, now: SystemTime) -> Result<SignedHeaders, AdapterError>;
// kind()=AwsLambda, pii_scope()=None, policy()={attempt_timeout_ms:15_000,max_retries:3,base_backoff_ms:250,max_backoff_ms:8_000}
// Target: Endpoint.url if set (test/VPC endpoint) else https://lambda.{region}.amazonaws.com
//   path /2015-03-31/functions/{function}/invocations, header X-Amz-Invocation-Type: Event (success = 202)
// Event envelope: {"v":1,"source":"waddles","op":..,"provider":..,"idempotency_key":..,"payload":<TokenizedOp.body>}
// region/function validated against closed charsets (re.fullmatch-equivalent, compiled once) before use.
```

- [ ] **Step 1: Write failing tests**

```rust
#[test] fn sigv4_matches_aws_published_vector() {}               // fixed key/time/request => exact Authorization string from the vector file
#[tokio::test] async fn sink_recomputes_signature_independently_and_matches() {} // sink recomputes canonical request+signature from received request using the known secret (own HMAC impl in test) == Authorization header
#[tokio::test] async fn session_token_adds_x_amz_security_token_and_signs_it() {}
#[tokio::test] async fn posts_event_envelope_with_invocation_type_event() {}
#[tokio::test] async fn status_202_ok_429_and_5xx_transient_403_permanent() {} // AWS 403 SignatureDoesNotMatch/AccessDenied => Permanent (no retry storm)
#[test] fn rejects_hostile_region_and_function_table() {}        // "us-east-1\n", "../x", "a b", >64 chars, unicode, userinfo chars
#[tokio::test] async fn wrong_secret_variant_is_config_error() {}
#[test] fn declares_pii_scope_none() {}
```

- [ ] **Step 2:** Run — expect failures. **Step 3:** Implement (clock injected via `now` param; adapter uses `SystemTime::now()` only at the `send` edge). **Step 4:** Run + coverage. **Step 5:** clippy/fmt/deny + Task 1 graph test. **Step 6:** Commit `feat(outbound): aws lambda adapter with sigv4`.

---

### Task 5: GCP Cloud Function adapter (service-account identity token)

**Files:**
- Create: `core/outbound_adapters/src/gcp.rs`, `core/outbound_adapters/tests/gcp_idtoken.rs`
- Modify: `core/outbound_adapters/Cargo.toml` (`jsonwebtoken`, exact pin, ring backend only)

**Interfaces:**
- Consumes: `ConnectionSecret::GcpServiceAccount{client_email, private_key_pem, token_uri}`; `Endpoint{url, project}`.
- Produces:
```rust
pub struct GcpFunctionAdapter { /* client: GuardedClient */ }
impl GcpFunctionAdapter { pub fn new(client: GuardedClient) -> Self; }
/// Builds + RS256-signs the SA assertion JWT: iss=sub=client_email, aud=token_uri, iat/exp(<=300s), target_audience=function_url.
pub(crate) fn build_assertion(sa: &GcpSa, function_url: &str, now: u64) -> Result<String, AdapterError>;
// Flow per send: (1) POST token_uri grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer&assertion=<jwt> -> {"id_token":..}
//   (2) POST Endpoint.url with `Authorization: Bearer <id_token>`, envelope below. id_token NOT cached (v1; follow-up issue).
// token_uri must be https + host allow-suffix {oauth2.googleapis.com} in production (loopback via test ctor); both hops go through GuardedClient.
// kind()=GcpFunction, pii_scope()=None, policy()={attempt_timeout_ms:15_000,max_retries:3,base_backoff_ms:250,max_backoff_ms:8_000}
// Envelope: {"v":1,"source":"waddles","op":..,"provider":..,"idempotency_key":..,"payload":<body>}
```

- [ ] **Step 1: Write failing tests** (test-generated RSA key; local token endpoint + function sink)

```rust
#[test] fn assertion_verifies_with_public_key_and_has_target_audience_equal_function_url() {} // decode+verify RS256, assert iss/sub/aud/exp-iat<=300/target_audience
#[tokio::test] async fn token_endpoint_receives_jwt_bearer_grant_and_function_gets_bearer_id_token() {}
#[tokio::test] async fn token_endpoint_401_is_permanent_5xx_transient_and_function_never_called_on_token_failure() {}
#[tokio::test] async fn token_uri_not_googleapis_is_config_error_in_production_client() {} // blocks credential exfil to attacker token_uri
#[tokio::test] async fn malformed_pem_is_config_error_without_echoing_key_material() {}
#[tokio::test] async fn posts_event_envelope_with_idempotency_key() {}
#[test] fn declares_pii_scope_none() {}
```

- [ ] **Step 2:** Run — expect failures. **Step 3:** Implement. **Step 4:** Run + coverage. **Step 5:** clippy/fmt/deny + Task 1 graph test. **Step 6:** Commit `feat(outbound): gcp cloud function adapter`.

---

### Task 6: Per-destination policy executor in svc-action (resolve-per-attempt, timeout, retry, breaker, idempotency, OTel)

**Files:**
- Create: `core/svc_action/src/outbound/mod.rs`, `core/svc_action/src/outbound/executor.rs`, `core/svc_action/src/outbound/metrics.rs`
- Modify: `core/svc_action/src/lib.rs` (`pub mod outbound;`), `core/svc_action/Cargo.toml` (path deps `outbound-adapters`, `connection-resolver`)
- Test: `core/svc_action/src/outbound/executor.rs` (`#[cfg(test)]`), `core/svc_action/tests/outbound_executor_real_sink.rs`

**Interfaces:**
- Consumes: `AdapterRegistry`, `DestinationAdapter`, `TokenizedOp` (Task 1); `ConnectionResolver`/`ConnectionRef`/`ResolveError` (Phase A); `crate::retry::{Jitter, backoff_delay_ms}` (`retry.rs:62,119`); `circuit_breaker::CircuitBreaker::{allow, record_success, record_failure}` (`core/circuit_breaker/src/lib.rs:143,170,206`).
- Produces:
```rust
/// Outcome of one destination's full retry cycle.
pub enum DestOutcome { Delivered { attempts: u32, status: u16 }, Failed { attempts: u32, reason: FailReason } }
pub enum FailReason { NotUsable, NotFound, Auth, ResolveTransient, CircuitOpen, Timeout, Transient, Permanent, Ssrf, Config, NoAdapter }
impl FailReason { pub fn label(&self) -> &'static str; }   // closed, PII-free

pub struct OutboundExecutor { /* registry, resolver: Arc<dyn ConnectionResolver>, breaker, jitter, metrics */ }
impl OutboundExecutor {
    pub fn new(registry: Arc<AdapterRegistry>, resolver: Arc<dyn ConnectionResolver>, breaker: Arc<CircuitBreaker>, jitter: Jitter, metrics: Arc<OutboundMetrics>) -> Self;
    /// community_id comes from the trusted InvokeScope only.
    pub async fn send_one(&self, community_id: i64, connection_id: i64, op: &TokenizedOp) -> DestOutcome;
}
// per attempt: breaker.allow(key) -> resolve() -> registry.get(conn.kind) -> adapter.send(timeout) -> classify;
// breaker key = "outbound:{kind}:{connection_id}" (key only in the breaker map, never a label);
// NotFound/NotUsable/Auth/Malformed/Permanent/Ssrf/Config never retried; Transient/Timeout/ResolveTransient retried up to policy.max_retries;
// kind==PlatformOauth => FailReason::NoAdapter (routed to gateway path, never here).
```

- [ ] **Step 1: Write failing tests**

```rust
#[tokio::test] async fn every_attempt_calls_resolve_again() {}                // counting resolver: 3 attempts => resolve count == 3
#[tokio::test] async fn scope_community_is_used_not_op_supplied_community() {} // resolver asserts ConnectionRef.community_id == arg; op body containing "community_id": 999 ignored
#[tokio::test] async fn breaker_open_short_circuits_without_resolve() {}      // after N permanent-or-transient failures, resolve count stops growing
#[tokio::test] async fn transient_retries_then_delivers_with_same_idempotency_key() {} // sink: 503,503,200 => attempts==3, all three requests carry identical Idempotency-Key
#[tokio::test] async fn permanent_and_resolve_not_usable_are_not_retried() {}
#[tokio::test] async fn attempt_timeout_enforced_and_counts_as_transient() {} // tokio::time::pause
#[tokio::test] async fn backoff_uses_full_jitter_and_honors_retry_after_cap() {}
#[tokio::test] async fn platform_oauth_kind_is_no_adapter_loud() {}
#[tokio::test] async fn metrics_labels_are_closed_set_and_latency_histogram_recorded() {} // kind/outcome only; assert no connection_id/url label values; histogram count == attempts
#[tokio::test] async fn delivers_end_to_end_against_real_sink_via_real_resolver_fixture() {} // tests/outbound_executor_real_sink.rs
```

- [ ] **Step 2:** Run `cargo test -p svc-action outbound` — expect failures.
- [ ] **Step 3:** Implement executor + `OutboundMetrics` (OTel meter like `stage_metrics.rs:252`, `tracing` span `outbound.send` with attrs `kind`,`outcome`,`attempt` only).
- [ ] **Step 4:** Run tests; verify coverage ≥90% on `outbound/`.
- [ ] **Step 5:** clippy/fmt/deny/audit.
- [ ] **Step 6:** Commit `feat(svc-action): outbound policy executor`.

---

### Task 7: Fan-out engine (independent legs, bounded width, shared deadline)

**Files:**
- Create: `core/svc_action/src/outbound/fanout.rs`
- Modify: `core/svc_action/src/outbound/mod.rs`
- Test: in-file `#[cfg(test)]`

**Interfaces:**
- Consumes: `OutboundExecutor::send_one`, `DestOutcome`; the existing stateful leg is injected as a boxed async closure (so Task 9 plugs `dispatch_chat_send_routed` in without this module knowing about it).
- Produces:
```rust
pub const MAX_FANOUT_WIDTH: usize = 8;
pub enum LegTarget { External { connection_id: i64 }, Stateful /* gateway reply, handled by injected leg */ }
pub struct LegReport { pub target: LegLabel, pub outcome: DestOutcome }   // LegLabel: kind label + index only, never connection_id in labels
pub struct FanOutReport { pub legs: Vec<LegReport> }
impl FanOutReport { pub fn delivered(&self) -> usize; pub fn failed(&self) -> usize; pub fn all_failed(&self) -> bool; pub fn to_host_json(&self) -> serde_json::Value; }
pub enum FanOutError { Empty, TooWide { max: usize, got: usize }, DuplicateTarget }
pub async fn fan_out(
    exec: &OutboundExecutor, community_id: i64, op: &TokenizedOp,
    targets: Vec<LegTarget>, stateful_leg: Option<StatefulLeg>, overall_deadline: Duration,
) -> Result<FanOutReport, FanOutError>;
// legs run concurrently (join, semaphore = MAX_FANOUT_WIDTH); a leg exceeding overall_deadline is reported Failed{Timeout}, others unaffected.
```

- [ ] **Step 1: Write failing tests**

```rust
#[tokio::test] async fn one_leg_fails_others_succeed_report() {}              // [ok, 500-permanent, ok] => delivered 2 failed 1, order preserved
#[tokio::test] async fn slow_leg_does_not_delay_siblings() {}                  // time-paused: fast legs complete at t≈0 while slow leg runs to deadline
#[tokio::test] async fn stateful_leg_failure_does_not_cancel_external_legs_and_vice_versa() {}
#[tokio::test] async fn width_over_eight_is_loud_error_and_sends_nothing() {}
#[tokio::test] async fn duplicate_connection_ids_rejected() {}
#[tokio::test] async fn empty_targets_is_error() {}
#[tokio::test] async fn rerun_with_same_op_id_reuses_idempotency_keys() {}      // second fan_out of identical op => sink sees identical keys per leg
#[tokio::test] async fn all_failed_flag_and_host_json_shape() {}                // {"delivered":n,"failed":m,"legs":[{"kind":"aws_lambda","ok":false,"reason":"timeout","attempts":4}]} — no ids/urls
#[tokio::test] async fn fanout_width_histogram_and_span_recorded() {}
```

- [ ] **Step 2:** Run — expect failures. **Step 3:** Implement. **Step 4:** Run + coverage. **Step 5:** clippy/fmt. **Step 6:** Commit `feat(svc-action): destination fan-out engine`.

---

### Task 8: Routing — `Destination::External`, flags, and grant permission family

**Files:**
- Modify: `core/svc_action/src/dispatch_destination.rs` (`Destination`, `label`, `route_destination` callers), `core/svc_action/src/flags.rs`, `core/bundle_capability_gate/src/permission.rs` (`PermissionId` at :750, parse at ~:956, `as_wire`/family at ~:820/:862), docs for the new permission in the gate's catalog test
- Test: `dispatch_destination.rs` tests, `flags.rs` tests, `permission.rs` tests

**Interfaces:**
- Consumes: existing `Destination::StatefulPlatformReply`, `EGRESS_VIA_DISPATCHER_FLAG` pattern (`flags.rs`), `PermissionId` catalog.
- Produces:
```rust
// dispatch_destination.rs
pub enum Destination {
    StatefulPlatformReply { provider: String, op: RelayOp },
    External { connection_id: i64 },          // kind resolved at send time from the connection, never from the bundle
}
// label(): "stateful_platform_reply" | "external"   (kind label added by executor from the resolved connection)
pub fn route_destinations(provider: &str, op: RelayOp, external_ids: &[i64]) -> Result<Vec<Destination>, DispatchError>;
// DispatchError gains: ExternalIdInvalid { reason: &'static str }  (<=0 id), TooManyDestinations
// flags.rs
pub const OUTBOUND_DESTINATIONS_FLAG: &str = "waddles.core.outbound-destinations"; // operational, env baseline, unseen => OFF
pub const STATIC_CONNECTIONS_LICENSE_FLAG: &str = "waddles.integrations.static_connections"; // license-server only
pub struct OutboundEnabled { /* Arc<dyn FeatureFlag> ops, license, dispatcher */ }  // enabled() = ops && license && egress_via_dispatcher
// permission.rs: PermissionId::RelayDestination(i64) wire "relay.dest:<connection_id>", PermissionFamily::RelayDestination
```

- [ ] **Step 1: Write failing tests**

```rust
#[test] fn route_destinations_adds_external_per_id_and_keeps_stateful_first() {}
#[test] fn route_destinations_rejects_nonpositive_and_over_eight_ids() {}
#[test] fn existing_route_destination_behavior_unchanged_regression() {} // Chunk-1a parity: all prior tests still pass
#[tokio::test] async fn outbound_enabled_requires_all_three_flags() {} // StaticFlag matrix 2^3: only (T,T,T) => true
#[tokio::test] async fn license_flag_is_not_env_bypassable() {}          // set env WADDLES_CORE_OUTBOUND_DESTINATIONS + env for license key => license leg still false
#[test] fn relay_dest_permission_roundtrips_parse_and_wire_and_family() {}
#[test] fn relay_dest_permission_rejects_nonnumeric_and_negative_ids() {}
```

- [ ] **Step 2:** Run (`cargo test -p svc-action -p bundle-capability-gate`) — expect failures.
- [ ] **Step 3:** Implement. Note: a `PermissionId` addition may require a gate-catalog snapshot/golden update and the bundle-manifest permission schema; Grep `permission.rs` tests + `config/` for the catalog list and update in the same commit.
- [ ] **Step 4:** Run both crates' full suites (Chunk-1a tests must stay green).
- [ ] **Step 5:** clippy/fmt/deny.
- [ ] **Step 6:** Commit `feat(svc-action): external destination routing, flags, grant family`.

---

### Task 9: Wire into `handle_relay` — gate, fan-out, meter, real-path + PII e2e

**Files:**
- Modify: `core/svc_action/src/capabilities.rs` (`handle_relay` ~:1037; parse optional `destinations` arg; new `with_outbound(..)` builder next to `with_detokenize` ~:949; metering via `usage.record_relay_call` ~:1211 pattern), `core/svc_action/src/lib.rs` / `wiring.rs` (construct `AdapterRegistry`, `OutboundExecutor`, real `ConnectionResolver` client from env, only when the flag is ON at startup-or-per-call), `core/svc_action/src/config.rs` (resolver URL env, no secrets)
- Create: `core/svc_action/tests/outbound_fanout_e2e.rs`
- Test: `capabilities.rs` tests module (`:1825`)

**Interfaces:**
- Consumes: Tasks 6-8; existing `StageCapabilities::{gate.authorize, dispatch_chat_send_routed, usage}`; `InvokeScope{tenant_id, community_id}`.
- Produces: `StageCapabilities::with_outbound(self, outbound: OutboundWiring) -> Self`; `relay.push` args gain optional `"destinations": [<i64 connection_id>, ...]`. Order inside `handle_relay`, unchanged prefix then: (1) existing provider/op validation, (2) existing `<op>:<platform>` grant, (3) **if `destinations` present**: `OutboundEnabled` else `feature_disabled`; (4) per-id `gate.authorize(PermissionId::RelayDestination(id))` — any denial rejects the WHOLE call before any send (no partial authz); (5) `TokenizedOp::parse` (rejects forbidden fields, fail loud `invalid_args`); (6) fan-out (stateful leg = existing path incl. its own detokenize-at-drain; external legs = executor); (7) meter once per delivered leg; (8) reply `{"sent":true|false,"fanout":<report json>}`; all legs failed => Err `fanout_failed` (with report), partial => Ok with report. No `destinations` => byte-identical to today (parity).

- [ ] **Step 1: Write failing tests**

```rust
#[tokio::test] async fn no_destinations_arg_is_byte_identical_to_chunk1a_behavior() {}    // regression parity
#[tokio::test] async fn flag_off_with_destinations_is_feature_disabled_and_sends_nothing() {} // each of the 3 flags off in turn
#[tokio::test] async fn ungranted_destination_rejects_whole_call_before_any_send() {}       // [granted, ungranted] => sink hits == 0, stateful queue empty
#[tokio::test] async fn forbidden_pii_field_in_message_json_is_invalid_args_for_external_path() {}
#[tokio::test] async fn external_fanout_leg_never_calls_detokenizer() {}                    // counting DisplayNameResolver: external-only call => 0; gateway leg present => exactly its own calls
#[tokio::test] async fn fanout_gateway_plus_http_plus_lambda_partial_failure_reply_shape() {}
#[tokio::test] async fn meters_once_per_delivered_leg_not_per_attempt_or_failed_leg() {}
#[tokio::test] async fn external_destinations_disabled_when_resolver_unconfigured_is_loud() {} // no silent fallback
#[tokio::test] async fn telemetry_emits_logs_metrics_histogram_traces_with_counts() {}       // per testing.md: counts printed; zero => FAIL
```
E2E (`tests/outbound_fanout_e2e.rs`): real axum sinks (HTTP, Lambda-shaped w/ SigV4 recompute, GCP-shaped + token endpoint), real in-process `connection_resolver` HTTP fixture speaking Phase A's wire contract, flag-ON path end-to-end: one `relay.push` fans to gateway-queue + 3 external; assert each sink body is the same TOKENIZED payload and contains no resolved display name.

- [ ] **Step 2:** Run — expect failures.
- [ ] **Step 3:** Implement the wiring; keep the Chunk-1a inline/stateful code untouched except calling it as the stateful leg.
- [ ] **Step 4:** Run full `cargo test -p svc-action -p outbound-adapters` + coverage ≥90%; run `make smoke-test` incl. telemetry assertions with counts.
- [ ] **Step 5:** clippy/fmt/deny/audit; gitleaks; run enforcing-no-stubs gate (`grep` for `todo!`/`unimplemented!` in new files = 0, count printed).
- [ ] **Step 6:** Commit `feat(svc-action): fan-out relay to external destinations behind flags`.

---

### Task 10: Network policy, Helm env, docs, flag-on real-path CI

**Files:**
- Modify: `k8s/helm/waddlebot/` svc-action values + templates (CiliumNetworkPolicy egress, env `CONNECTION_RESOLVER_URL`, `OUTBOUND_DESTINATIONS` baseline default `false`), `core/svc_action/README.md` (or the crate's doc) , `docs/` standards page for destination adapters (PII-scope table), `.github/workflows/` rust job list (build + test `outbound-adapters`)
- Test: `helm template` + lint assertions; CI job runs the Task 9 e2e flag-ON

**Interfaces:**
- Consumes: Task 9 env names. Produces: Helm values `svcAction.outbound.enabled` (default `false`), `svcAction.outbound.egress.fqdns: []`, `svcAction.outbound.egress.cidrs: []` (empty by default = deny-by-default stays; operator allowlists destinations; CNP must still deny RFC1918/link-local/metadata regardless).

- [ ] **Step 1: Write failing checks**

```bash
# scripts/ci/check-outbound-egress-policy.sh (set -euo pipefail)
# renders the chart twice (enabled=false default, enabled=true with one fqdn) and asserts:
#   default: no egress rule beyond existing; enabled: exactly the listed fqdn + resolver svc; never 0.0.0.0/0, never 169.254.0.0/16 allowed.
#   prints "rendered_policies=N" ; N==0 => FAIL
```
- [ ] **Step 2:** Run — expect failure. **Step 3:** Implement chart changes + docs (PII-scope table, flag matrix, failure semantics, "tokens only on the wire", the GCP no-cache follow-up). **Step 4:** `helm lint`, `helm template` (helm v3.17.3 pinned), script passes and is proven to fail once by temporarily allowing `0.0.0.0/0`. **Step 5:** Add the CI step; confirm the `outbound-adapters` crate is in the ci-gate cargo-build list. **Step 6:** Commit `chore(outbound): helm egress policy, docs, ci wiring`.

---

## Self-Review

- **Spec coverage:** §6.2 steps 1-3,5 → Tasks 8-9 (grant-gate, meter, never-detokenize, fan-out); §6.3 adapter unit (transport/envelope/auth/retry/timeout/idempotency/PII scope) → Tasks 1-6; §6.4 scope NONE by construction → Task 1 graph test + Task 9 counting-resolver test; spec §9 builder calls (trait shape, compile-time property) → Task 1; Lambda/GCP envelopes + IAM signing → Tasks 4-5; Phase A contract used only via `resolve()` → Task 6. Chunk 3 adapters and workflow explicitly out of scope.
- **Placeholder scan:** no TBD/TODO steps; two "Verify first" instructions (Task 2 `bundle_host_http` deps, Task 8 gate-catalog golden update) are concrete Grep actions, not gaps.
- **Type consistency:** `TokenizedOp`, `AdapterCall`, `AdapterPolicy`, `AdapterError`, `DestOutcome`, `FailReason`, `LegTarget`, `FanOutReport`, `OutboundEnabled`, `PermissionId::RelayDestination` are named identically across Tasks 1/6/7/8/9. `ConnectionKind` values and `resolve()` signature match the frozen Phase A contract.
- **Rulings (flag for review):** (1) adapters live in a separate leaf crate so "no detokenize path" is a Cargo-graph compile-time property; (2) retry/breaker/timeout are enforced by one shared executor in svc-action using the adapter-declared `AdapterPolicy` (reuses `retry.rs` + `core/circuit_breaker`); (3) creds resolved per ATTEMPT, GCP id-token not cached in v1; (4) Lambda = async `Event` invoke; (5) destinations enter via `relay.push` `destinations: [connection_id]` + a new `relay.dest:<id>` permission family, all-or-nothing authz before any send; (6) partial fan-out failure = Ok with per-leg report, all-failed = Err; (7) two flags both required (ops env-baseline + license-server-only).
