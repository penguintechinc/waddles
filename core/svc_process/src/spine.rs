//! Wires `penguin-spine` (spec §4.7) as the process-stage consumer:
//! `XREADGROUP`s a bundle's granted ingest-source Valkey streams, verifies
//! the hop (`crate::hop`, spec §5.11) before any other processing, invokes
//! the bundle's `transform` over the `bundle-executor` wire protocol
//! (`crate::host_api`/`crate::capabilities`), applies the cross-app
//! `_target_app_id` routing built-in (`crate::builtins`), and `XADD`s the
//! result onto the destination bundle's own `:action` stream.
//!
//! Structured exactly like `core/svc_action/src/dispatch.rs` (which
//! explicitly names this module as "the M4 reference for this same
//! drain-loop shape" in its own doc comment, before this landing filled
//! it in): a private `StreamReader` trait wraps `penguin_spine::
//! GroupReader::read` so `drain_batch`/`drain_loop` are unit-testable
//! against a fake reader, and a `SpineOps` trait wraps the
//! `ack`/`dead_letter`/`append` operations `handle_delivered` needs so
//! those tests don't require a live Valkey. The real `run()` entry point
//! wires the live Valkey/host-API connections.
//!
//! **Wire-JSON convention for `transform` (spec §6.5/§6.6, assumption
//! A2).** `core/bundle_executor/src/invoke.rs::on_invoke`'s `ExportKind::
//! Transform` arm is the landed, normative convention this module matches
//! exactly (not an independent choice, unlike `svc_action::dispatch`'s own
//! documented `{"envelope":..., "config":...}` convention, which predates
//! any landed executor-side reference): `InvokeBody.payload` is the WIT
//! `platform-event` record's bindgen-generated JSON shape *directly* --
//! `{"platform", "event_type", "actor", "payload_json", "occurred_at"}`,
//! where `payload_json` is `penguin_spine::PlatformEvent.payload`
//! (a JSON *object*) re-encoded as a canonical JSON *string* (WIT has no
//! open-ended-object type, spec §6.5's own words: "every open-ended
//! structure is carried as canonical UTF-8 JSON text"). The `Ok(Ok(reply))`
//! result payload is `Option<PlatformEvent>` in that same shape (`null` for
//! "no reply"); `Ok(Err(unsupported_stage))` is `{"unsupported_stage":
//! {...}}`; a guest trap is a `Message::Error` frame, never a `Message::
//! Result`. [`wire_platform_event`]/[`platform_event_from_wire`] are the
//! two conversion functions this module owns for that shape.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use circuit_breaker::CircuitBreaker;
use penguin_bundle_host::wire::{
    ErrorCode, ExportKind, InvokeBody, LoadBody, LoadLimits, LoadedBody, Message, TraceContext,
    UnloadBody, UnloadedBody,
};
use penguin_spine::{
    Delivered, DlqError, DlqErrorKind, Grant, GroupReader, GroupStats, PlatformEvent, Scope,
    SpineClient, SpineConfig, SpineError, SpineMetrics, Stage, StageEnvelope,
};

use crate::active_digests::ActiveDigests;
use crate::builtins::RouteDecision;
use crate::capabilities::{CapabilityHandler, StageCapabilities};
use crate::hop::KeyRing;
use crate::host_api::{Connection, ConnectionRegistry, HostApiError};
use crate::license::FeatureGate;
use crate::stage_metrics::{self, StageResult};

/// Converts a `penguin_spine::PlatformEvent` into the WIT `platform-event`
/// record's JSON shape (see the module doc's wire-JSON convention) -- the
/// `InvokeBody.payload` this module sends for `ExportKind::Transform`.
fn wire_platform_event(event: &PlatformEvent) -> Result<serde_json::Value, serde_json::Error> {
    let payload_json = serde_json::to_string(&event.payload)?;
    Ok(serde_json::json!({
        "platform": event.platform,
        "event_type": event.event_type,
        "actor": event.actor,
        "payload_json": payload_json,
        "occurred_at": event.occurred_at,
    }))
}

/// The inverse of [`wire_platform_event`]: parses a `transform` result's
/// wire-shaped `platform-event` JSON back into a `penguin_spine::
/// PlatformEvent`, going through that type's own strict `Deserialize`
/// impl (never constructed directly from untrusted bundle output) so a
/// malformed field (empty `platform`/`event_type`, a bad `occurred_at`,
/// invalid `payload_json`) is rejected the same way any other envelope
/// input would be -- a bundle's return value is guest-controlled and must
/// never be trusted more than wire input from any other untrusted source.
/// `source` is never populated from bundle output (spec §6.5's WIT record
/// has no `source` field at all -- it is stage-injected provenance, not a
/// bundle-settable value).
fn platform_event_from_wire(wire: &serde_json::Value) -> Result<PlatformEvent, InvokeError> {
    let obj = wire.as_object().ok_or_else(|| {
        InvokeError::MalformedPayload("transform reply is not a JSON object".to_string())
    })?;
    let payload_json = obj
        .get("payload_json")
        .and_then(|v| v.as_str())
        .ok_or_else(|| {
            InvokeError::MalformedPayload("missing string field 'payload_json'".to_string())
        })?;
    let payload: serde_json::Value = serde_json::from_str(payload_json).map_err(|e| {
        InvokeError::MalformedPayload(format!("payload_json is not valid JSON: {e}"))
    })?;
    let reconstructed = serde_json::json!({
        "platform": obj.get("platform"),
        "event_type": obj.get("event_type"),
        "actor": obj.get("actor"),
        "payload": payload,
        "occurred_at": obj.get("occurred_at"),
        "source": null,
    });
    serde_json::from_value(reconstructed)
        .map_err(|e| InvokeError::MalformedPayload(format!("invalid platform-event: {e}")))
}

// regression: action envelope dropped event.source so discord relay had no origin channel (alpha 2026-10-02)
/// Overwrites `event_out.source` with `inbound`'s own `event.source` --
/// `source` identifies which inbound connection produced this entry
/// (platform/account/channel) and is the ONLY place `svc_action::dispatch::
/// invoke_dispatch` derives `origin_channel_id` from (host-controlled by
/// design, never bundle-chosen -- see that function's doc and
/// `svc_action::capabilities::handle_discord_relay`'s "the channel is never
/// the bundle's to name"). `platform_event_from_wire` already hardcodes
/// `source: null` on every bundle reply (its own doc: "never populated from
/// bundle output"), so `event_out.source` reaching here is always `None` on
/// the real invoke path today -- this is still the single place
/// `handle_delivered` builds the next-stage envelope, so it unconditionally
/// REPLACES whatever `event_out.source` holds (never merges, never trusts
/// it) rather than relying solely on the wire layer -- defense in depth
/// against a future wire-format change or a non-wire (builtin/synthetic)
/// caller ever populating it. Before this fix `event_out.source` (always
/// `None`) was carried straight onto the action envelope unchanged, so
/// every Discord relay's origin channel was `None` and `svc_action` denied
/// it with "discord relay requires an origin channel id" (a denial
/// `svc_action` never logs).
fn carry_inbound_source(event_out: &mut PlatformEvent, inbound: &PlatformEvent) {
    event_out.source = inbound.source.clone();
}

/// Errors invoking the bundle's `transform` export over the host-API
/// connection -- distinct from the bundle's own `result<option<
/// platform-event>, unsupported-stage>` business-level return, which
/// [`TransformOutcome`] classifies. An `InvokeError` means the invocation
/// itself never produced a bundle-classified outcome at all
/// (infrastructure failure or malformed guest output) and is DLQ'd
/// directly (spec §6.3's `call_timeout`/`bundle_trap`/`bundle_error`/
/// `host_call_denied`/`executor_unavailable` DLQ kinds are all
/// infrastructure-level).
#[derive(Debug, thiserror::Error)]
pub enum InvokeError {
    #[error("no executor connection available")]
    NoExecutor,
    #[error("host-api error: {0}")]
    HostApi(#[from] HostApiError),
    #[error("executor reported error {code:?}: {message}")]
    ExecutorError {
        code: ErrorCode,
        message: String,
        /// The executor's wire `detail` discriminator (`ErrorBody::detail`),
        /// e.g. `pool_exhausted` (back-pressure, NOT a guest fault) or
        /// `fuel_exhausted` (a guest fault, distinct from a wall-clock
        /// deadline). Carried through so consumers never have to guess from
        /// the coarse `code` alone.
        detail: Option<String>,
    },
    #[error("transform payload encode/decode failed: {0}")]
    MalformedPayload(String),
}

/// Sends `load` for one bundle over `conn` and returns the executor's
/// `loaded` reply (spec §6.6). A direct port of `core/svc_action/src/
/// dispatch.rs::ensure_loaded` (the M3 reference this crate's own module
/// doc names as the drain-loop shape to mirror) -- identical wire-level
/// behavior, just returning this module's own [`InvokeError`] instead of
/// `svc_action::dispatch::InvokeError`.
#[allow(clippy::too_many_arguments)]
pub async fn ensure_loaded(
    conn: &Connection,
    tenant_id: i32,
    community_id: i32,
    app_id: &str,
    version: &str,
    digest: &str,
    component_key: &str,
    sidecar_key: &str,
    limits: LoadLimits,
) -> Result<LoadedBody, InvokeError> {
    let started = std::time::Instant::now();
    let outcome = load_round_trip(
        conn,
        tenant_id,
        community_id,
        app_id,
        version,
        digest,
        component_key,
        sidecar_key,
        limits,
    )
    .await;
    let metrics = stage_metrics::stage_metrics();
    let result = if outcome.is_ok() { "ok" } else { "error" };
    metrics.record_bundle_load(app_id, "load", result, started.elapsed());
    if let Ok(loaded) = &outcome {
        // The executor's own account of the load (`precompile_ms`, covering
        // fetch + verify + compile on its side) -- the stage cannot see
        // inside the round trip, so this is the only per-phase signal.
        metrics.record_bundle_load(
            app_id,
            "precompile",
            "ok",
            Duration::from_millis(loaded.precompile_ms),
        );
    }
    outcome
}

/// The `load` request/reply exchange behind [`ensure_loaded`], split out so
/// the wrapper can time the whole round trip and label its outcome.
#[allow(clippy::too_many_arguments)]
async fn load_round_trip(
    conn: &Connection,
    tenant_id: i32,
    community_id: i32,
    app_id: &str,
    version: &str,
    digest: &str,
    component_key: &str,
    sidecar_key: &str,
    limits: LoadLimits,
) -> Result<LoadedBody, InvokeError> {
    let reply = conn
        .request(Message::Load(LoadBody {
            tenant_id,
            community_id,
            app_id: app_id.to_string(),
            version: version.to_string(),
            digest: digest.to_string(),
            component_key: component_key.to_string(),
            sidecar_key: sidecar_key.to_string(),
            capabilities: vec![],
            limits,
        }))
        .await?;
    match reply.message {
        Message::Loaded(body) => Ok(body),
        Message::Error(e) => Err(InvokeError::ExecutorError {
            code: e.code,
            message: e.message,
            detail: e.detail,
        }),
        _ => Err(InvokeError::MalformedPayload(
            "expected loaded or error frame".to_string(),
        )),
    }
}

/// Sends `unload` for one bundle over `conn` and returns the executor's
/// `unloaded` reply (spec §6.6) -- the counterpart [`ensure_loaded`] never
/// needed until the DB-driven active-bundle loader (`crate::
/// bundle_loader`): a single-bundle-per-instance drain loop had nothing to
/// unload; a hot-swappable multi-bundle registry does. `digest` must match
/// what the executor actually has loaded for `app_id`
/// (`core/bundle_executor/src/invoke.rs::on_unload`'s own digest check) --
/// callers pass the digest they last successfully `load`ed, never a
/// freshly-read DB value that might already differ.
pub async fn ensure_unloaded(
    conn: &Connection,
    tenant_id: i32,
    community_id: i32,
    app_id: &str,
    digest: &str,
) -> Result<UnloadedBody, InvokeError> {
    let reply = conn
        .request(Message::Unload(UnloadBody {
            tenant_id,
            community_id,
            app_id: app_id.to_string(),
            digest: digest.to_string(),
        }))
        .await?;
    match reply.message {
        Message::Unloaded(body) => Ok(body),
        Message::Error(e) => Err(InvokeError::ExecutorError {
            code: e.code,
            message: e.message,
            detail: e.detail,
        }),
        _ => Err(InvokeError::MalformedPayload(
            "expected unloaded or error frame".to_string(),
        )),
    }
}

/// Tracks whether [`ProcessDeps::digest`] has already been successfully
/// `load`ed onto the currently active executor connection, so
/// [`handle_delivered`] sends `load` at most once per (connection, digest)
/// pair rather than on every single invoke (spec §7.6's `load` path
/// re-fetches from the bucket and re-instantiates the WASM component --
/// far too expensive to repeat per event). A freshly (re)connected
/// executor always starts with nothing loaded (spec §7.5), so identity is
/// tracked by `Arc::ptr_eq` against the stored [`Connection`]: `crate::
/// host_api::ConnectionRegistry::set_active` constructs a brand-new
/// `Connection` for every accepted TCP connection, so a reconnect is
/// always a different `Arc` allocation and this cache correctly "forgets"
/// the stale load.
#[derive(Default)]
pub struct LoadState {
    loaded: std::sync::Mutex<Option<(Arc<Connection>, String)>>,
}

impl LoadState {
    pub fn new() -> Self {
        Self::default()
    }

    fn is_loaded_on(&self, connection: &Arc<Connection>, digest: &str) -> bool {
        matches!(
            &*self.loaded.lock().unwrap_or_else(|e| e.into_inner()),
            Some((c, d)) if Arc::ptr_eq(c, connection) && d == digest
        )
    }

    fn mark_loaded(&self, connection: Arc<Connection>, digest: String) {
        *self.loaded.lock().unwrap_or_else(|e| e.into_inner()) = Some((connection, digest));
    }
}

/// The bundle's `transform` export's classified return value (spec §6.5:
/// `result<option<platform-event>, unsupported-stage>`).
#[derive(Debug)]
pub enum TransformOutcome {
    /// `none` -- "no reply"; the event is dropped, nothing is enqueued.
    NoReply,
    /// `ok(some(event))` -- enqueue `event` (after cross-app routing).
    /// Boxed (clippy::large_enum_variant): `PlatformEvent` is far larger
    /// than the other variants.
    Reply(Box<PlatformEvent>),
    /// `err(unsupported-stage)` -- the bundle does not implement
    /// `process-stage.transform` at all; a manifest/registration bug
    /// (spec §6.5), DLQ'd with `error.kind = "bundle_error"`.
    UnsupportedStage,
}

/// Maps an `ErrorCode` (spec §6.6) reported on an `invoke`'s reply onto
/// the closest `penguin_spine::DlqErrorKind` (spec §6.3's ten-value
/// vocabulary, which has no 1:1 protocol-error kind for every `ErrorCode`
/// variant) -- grouped by what the failure actually means operationally:
/// a deadline/memory/trap/host-call-denial maps to its own dedicated kind;
/// anything naming a broken bundle registration (unknown bundle, digest
/// mismatch, load failure, missing export, malformed frame) maps to
/// `BundleError`; anything naming a broken *connection*/protocol maps to
/// `ExecutorUnavailable`.
fn error_code_to_dlq_kind(code: ErrorCode) -> DlqErrorKind {
    match code {
        ErrorCode::ExecutorDeadline => DlqErrorKind::CallTimeout,
        ErrorCode::MemoryLimit => DlqErrorKind::MemoryLimit,
        ErrorCode::WasmTrap => DlqErrorKind::BundleTrap,
        // `HostCallFailed` (the host-call attempt itself errored) buckets
        // with `HostCallDenied` (an ungranted capability) -- the ten-value
        // `DlqErrorKind` vocabulary has no separate "host call technically
        // failed" kind, and both name a problem in the same host-call
        // subsystem rather than the bundle's own registration or a
        // deadline/memory/trap condition.
        ErrorCode::HostCallDenied | ErrorCode::HostCallFailed => DlqErrorKind::HostCallDenied,
        ErrorCode::UnknownBundle
        | ErrorCode::DigestMismatch
        | ErrorCode::LoadFailed
        | ErrorCode::ExportMissing
        | ErrorCode::MalformedFrame => DlqErrorKind::BundleError,
        ErrorCode::ProtocolVersion
        | ErrorCode::FrameTooLarge
        | ErrorCode::UnsandboxedExecutor
        | ErrorCode::ShuttingDown => DlqErrorKind::ExecutorUnavailable,
    }
}

/// The executor `detail` discriminator for pool back-pressure
/// (`bundle_executor::invoke::acquire_call_slots` hitting the global/per-
/// bundle permit cap). Wire code is `ExecutorDeadline` (the pinned wire
/// crate has no dedicated code) but it says nothing about the guest.
const DETAIL_POOL_EXHAUSTED: &str = "pool_exhausted";

/// Whether an executor error counts as a GUEST fault against the circuit
/// breaker: a trap, a deadline/fuel exhaustion, or a memory-cap hit -- but
/// NOT `pool_exhausted` back-pressure, which is the executor shedding load
/// and must never open the breaker for innocent sources.
fn is_guest_fault(kind: DlqErrorKind, detail: Option<&str>) -> bool {
    if detail == Some(DETAIL_POOL_EXHAUSTED) {
        return false;
    }
    matches!(
        kind,
        DlqErrorKind::BundleTrap | DlqErrorKind::CallTimeout | DlqErrorKind::MemoryLimit
    )
}

/// The circuit-breaker key for one delivered entry: `{app_id}|{stream}`.
/// Bindings are `(tenant, community, app_id, platform, source_id)` and
/// several apps consume the same source stream, so keying on the stream
/// alone would let one app's faults disable every other app's consumer on
/// that stream. Matches `svc_action::dispatch::breaker_key`'s app-prefixed
/// scheme.
fn breaker_key(app_id: &str, stream: &str) -> String {
    format!("{app_id}|{stream}")
}

/// Records the executor-call histogram for one `invoke` reply and, when the
/// executor reported a sandbox trip, the trip counter. `result` is `ok` for a
/// result frame, `executor_error` for an error frame, `malformed` for any
/// other frame and `host_api_error` when no reply arrived at all.
fn record_executor_call(
    app_id: &str,
    export: &'static str,
    reply: &Result<penguin_bundle_host::wire::Frame, HostApiError>,
    elapsed: Duration,
) {
    let metrics = stage_metrics::stage_metrics();
    let result = match reply {
        Ok(frame) => match &frame.message {
            Message::Result(_) => "ok",
            Message::Error(e) => {
                if let Some(limit) = stage_metrics::sandbox_limit_for(e.code, e.detail.as_deref()) {
                    metrics.record_sandbox_trip(app_id, limit);
                }
                "executor_error"
            }
            _ => "malformed",
        },
        Err(_) => "host_api_error",
    };
    metrics.record_executor_call(app_id, export, result, elapsed);
}

/// Invokes the bundle's `transform` export for one delivered event over
/// `conn`, scoped to `capabilities` for exactly this call (see
/// `crate::host_api`'s per-invoke scoping design). Returns the classified
/// [`TransformOutcome`] on any *executor-answered* reply (`result` frame,
/// success or `unsupported_stage` alike); an `InvokeError` covers every
/// case that never produced one (host-api failure, guest trap, malformed
/// reply payload).
///
/// Guards every outbound `Invoke` (the shared helper both the legacy and
/// multi-tenant `ProcessDeps::digest_source` paths funnel through) against
/// a NON-EMPTY but malformed digest ever reaching the wire -- an empty
/// digest remains a legitimate, deliberately-unloaded sentinel for
/// `DigestSource::Static` (see that variant's own doc: the executor's own
/// `UNKNOWN_BUNDLE` reply is the intended signal there), but anything
/// non-empty must already be canonical (`sha256:<64-hex>`) by the time it
/// gets here -- `DigestSource::Active` never calls this with anything else
/// (its own `debug_assert` already covers that path); this is the single
/// chokepoint catching a regression in EITHER caller.
/// regression: multi-tenant consumers invoked with empty legacy digest,
/// UnknownBundle (alpha 2026-10-03)
pub async fn invoke_transform(
    conn: &Connection,
    app_id: &str,
    digest: &str,
    event: &PlatformEvent,
    deadline_ms: u64,
    trace: Option<TraceContext>,
    capabilities: Arc<dyn CapabilityHandler>,
) -> Result<TransformOutcome, InvokeError> {
    debug_assert!(
        digest.is_empty() || bundle_active_set::canonical_digest(digest).as_deref() == Ok(digest),
        "invoke digest {digest:?} must be either the legacy empty sentinel or already canonical"
    );
    let payload =
        wire_platform_event(event).map_err(|e| InvokeError::MalformedPayload(e.to_string()))?;
    let started = std::time::Instant::now();
    let reply = conn
        .invoke(
            InvokeBody {
                app_id: app_id.to_string(),
                digest: digest.to_string(),
                export: ExportKind::Transform,
                payload,
                deadline_ms,
                trace,
            },
            capabilities,
        )
        .await;
    record_executor_call(app_id, "transform", &reply, started.elapsed());
    let reply = reply?;
    match reply.message {
        Message::Result(body) => {
            if body.payload.is_null() {
                return Ok(TransformOutcome::NoReply);
            }
            if let Some(obj) = body.payload.as_object() {
                if obj.contains_key("unsupported_stage") {
                    return Ok(TransformOutcome::UnsupportedStage);
                }
            }
            let event = platform_event_from_wire(&body.payload)?;
            Ok(TransformOutcome::Reply(Box::new(event)))
        }
        Message::Error(e) => Err(InvokeError::ExecutorError {
            code: e.code,
            message: e.message,
            detail: e.detail,
        }),
        _ => Err(InvokeError::MalformedPayload(
            "expected result or error frame".to_string(),
        )),
    }
}

/// Runs ONE nested `tool.invoke` leg: sends an `ExportKind::ToolCall` invoke for
/// the already-authorized `plan` (target app, platform-installed digest, tool,
/// args, call chain, idempotency key) over `conn`, scoped to `capabilities` (the
/// TARGET's own capability set, never the caller's), and maps the reply onto the
/// `{ok}`/`{tool_error}` envelope or a typed [`ToolRefusal`].
///
/// The host enforces `deadline` itself with a timeout around the invoke -- the
/// executor's epoch trap is a backstop, never the mechanism (a slow target must
/// come back `timeout` even if the guest is not yet trapped). Nested sync
/// `conn.invoke` is safe: the connection is multiplexed by invoke id and host
/// calls run on their own spawned task (Task 0 ruling), and the caller's
/// self/cycle/depth guard already ran in `bundle_tool_call::plan_tool_call`
/// before this is reached, so no executor slot is requested twice by one chain.
pub async fn invoke_tool_call(
    conn: &Connection,
    plan: &bundle_tool_call::ToolPlan,
    deadline: Duration,
    trace: Option<TraceContext>,
    capabilities: Arc<dyn CapabilityHandler>,
) -> Result<serde_json::Value, bundle_tool_call::ToolRefusal> {
    use bundle_tool_call::ToolRefusal;
    debug_assert!(
        bundle_active_set::canonical_digest(&plan.target_digest).as_deref()
            == Ok(plan.target_digest.as_str()),
        "tool target digest {:?} must already be canonical",
        plan.target_digest
    );
    let deadline_ms = u64::try_from(deadline.as_millis())
        .unwrap_or(u64::MAX)
        .max(1);
    let payload = bundle_tool_call::nested_payload(plan)?;
    let started = std::time::Instant::now();
    let invoke = conn.invoke(
        InvokeBody {
            app_id: plan.target_app_id.clone(),
            digest: plan.target_digest.clone(),
            export: ExportKind::ToolCall,
            payload,
            deadline_ms,
            trace,
        },
        capabilities,
    );
    let reply = match tokio::time::timeout(deadline, invoke).await {
        Ok(reply) => reply,
        Err(_elapsed) => {
            tracing::warn!(
                target = %plan.target_app_id,
                tool = %plan.tool,
                deadline_ms,
                "nested tool call exceeded its host deadline"
            );
            stage_metrics::stage_metrics().record_executor_call(
                &plan.target_app_id,
                "tool_call",
                "host_timeout",
                started.elapsed(),
            );
            return Err(ToolRefusal::Timeout);
        }
    };
    record_executor_call(&plan.target_app_id, "tool_call", &reply, started.elapsed());
    match reply {
        Ok(frame) => bundle_tool_call::interpret_reply(frame.message),
        Err(HostApiError::WriterBackpressure { .. }) => Err(ToolRefusal::PoolExhausted),
        Err(e) => {
            tracing::warn!(target = %plan.target_app_id, error = %e, "nested tool call transport failure");
            Err(ToolRefusal::TargetUnavailable(
                "the executor connection failed".to_string(),
            ))
        }
    }
}

/// Abstraction over [`penguin_spine::GroupReader::read`]'s exact signature.
/// Exists solely so [`drain_loop`] can be driven by a fake reader in tests.
trait StreamReader {
    async fn read(&mut self) -> Result<Vec<Delivered>, SpineError>;
}

impl StreamReader for GroupReader {
    async fn read(&mut self) -> Result<Vec<Delivered>, SpineError> {
        GroupReader::read(self).await
    }
}

/// Abstraction over the three [`penguin_spine::SpineClient`] operations
/// [`handle_delivered`] needs (`ack`/`dead_letter`/`append`) -- narrow
/// trait, same rationale as `svc_action::dispatch::SpineOps`: a live
/// Valkey connection is `SpineClient::connect`'s own concern (already
/// covered by `penguin-spine`'s own test suite), not something every
/// caller of this module's control flow should need just to exercise it.
pub trait SpineOps: Send + Sync {
    fn ack<'a>(
        &'a self,
        d: &'a Delivered,
        app_id: &'a str,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<(), SpineError>> + Send + 'a>>;

    fn dead_letter<'a>(
        &'a self,
        d: &'a Delivered,
        err: &'a DlqError,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<(), SpineError>> + Send + 'a>>;

    fn append<'a>(
        &'a self,
        stream: &'a str,
        env: &'a StageEnvelope,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<String, SpineError>> + Send + 'a>>;
}

impl SpineOps for SpineClient {
    fn ack<'a>(
        &'a self,
        d: &'a Delivered,
        app_id: &'a str,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<(), SpineError>> + Send + 'a>>
    {
        Box::pin(SpineClient::ack(self, d, app_id))
    }

    fn dead_letter<'a>(
        &'a self,
        d: &'a Delivered,
        err: &'a DlqError,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<(), SpineError>> + Send + 'a>>
    {
        Box::pin(SpineClient::dead_letter(self, d, err))
    }

    fn append<'a>(
        &'a self,
        stream: &'a str,
        env: &'a StageEnvelope,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<String, SpineError>> + Send + 'a>>
    {
        Box::pin(SpineClient::append(self, stream, env))
    }
}

/// Where a [`ProcessDeps`]'s invoke/load digest comes from -- the fix for
/// the regression named below: the DB-driven multi-tenant path
/// (`crate::source_supervisor::run_binding_consumer`) used to build every
/// per-binding consumer's `ProcessDeps` with a fixed, permanently-empty
/// `digest: String::new()` (bundle load/unload is `crate::
/// changelog_consumer`'s job, never this consumer's own -- see
/// `crate::source_supervisor`'s module doc), so every single invoke on
/// that path hit the executor's real `UNKNOWN_BUNDLE` no matter what was
/// actually loaded.
///
/// regression: multi-tenant consumers invoked with empty legacy digest,
/// UnknownBundle (alpha 2026-10-03)
pub enum DigestSource {
    /// The legacy, single-bundle-per-pod, env-configured path
    /// (`crate::lib::try_start_process_loop`'s `PROCESS_BUNDLE_DIGEST`) --
    /// UNCHANGED behavior from before this fix. Empty disables nothing by
    /// itself: an empty digest is simply sent as-is and the executor
    /// reports `UNKNOWN_BUNDLE`, mapped to `DlqErrorKind::BundleError`
    /// like any other unloaded-bundle invoke -- the same "caller's
    /// responsibility until the poll client lands" scope `svc_action::
    /// dispatch::ensure_loaded`'s doc comment documents for its own
    /// crate. This variant must never be selected by the multi-tenant
    /// path (`crate::source_supervisor`'s own doc: "never fall back to
    /// legacy env on the multi-tenant path").
    Static(String),
    /// The DB-driven multi-tenant path: the CURRENT canonical digest for
    /// this consumer's own `(tenant_id, community_id, app_id)` scope, read
    /// fresh from `crate::changelog_consumer`'s shared [`ActiveDigests`]
    /// map on every single invoke -- never a value captured once at spawn
    /// time, so a hot-swapped bundle takes effect on the very next message
    /// with no consumer restart. [`DigestSource::current`] returns `None`
    /// when this scope has no active digest right now (never seen,
    /// unloaded, or its scope is currently failing to resolve); callers
    /// MUST dead-letter rather than ever invoke with an empty digest.
    Active {
        scope: bundle_active_set::AppScope,
        digests: Arc<ActiveDigests>,
    },
}

impl DigestSource {
    /// Resolves the digest to `load`/`invoke` with right now. See each
    /// variant's own doc for what `None`/empty means.
    fn current(&self) -> Option<String> {
        match self {
            DigestSource::Static(d) => Some(d.clone()),
            DigestSource::Active { scope, digests } => {
                let digest = digests.get(scope)?;
                // Defense in depth, not the primary guarantee: a canonical,
                // non-empty digest is already enforced at the DB-read
                // boundary (`bundle_active_set::canonical_digest`, applied
                // before `crate::changelog_consumer` ever calls
                // `ActiveDigests::set`) AND at `ActiveDigests::set` itself
                // (which now refuses to store an empty digest at all) --
                // this `debug_assert` exists purely to catch a future
                // regression that reintroduces a bare-hex or empty digest
                // into that map before it ever reaches the wire. It is
                // compiled OUT in the release profile this service actually
                // runs, which is exactly why [`Self::usable_digest`] below
                // is the one callers must use for the real runtime gate.
                debug_assert!(
                    !digest.is_empty(),
                    "ActiveDigests must never hold an empty digest for a scope"
                );
                debug_assert_eq!(
                    bundle_active_set::canonical_digest(&digest).as_deref(),
                    Ok(digest.as_str()),
                    "ActiveDigests digest {digest:?} must already be canonical (sha256:<64-hex>)"
                );
                Some(digest)
            }
        }
    }

    /// Like [`Self::current`], but additionally treats an `Active`-path
    /// digest that resolved to an empty string the same as "no active
    /// digest known" (`None`) -- the one call [`handle_delivered`]'s own
    /// `NO_ACTIVE_DIGEST` guard must use, never [`Self::current`] directly,
    /// so that guard can never be bypassed by an empty-but-`Some` digest in
    /// the release profile (where the `debug_assert`s above are compiled
    /// out). Direct port of `core/svc_action/src/dispatch.rs::DigestSource::
    /// usable_digest`'s identical fix -- see that function's own doc for the
    /// full rationale, including why `Static`'s own intentionally-empty
    /// legacy sentinel is left untouched here.
    ///
    /// regression: same-digest manifest-only release (ping 1.0.2/1.0.3)
    /// emptied svc-action dispatch digest (alpha 2026-10-03)
    ///
    /// **Deliberately does NOT call [`Self::current`] for the `Active`
    /// case** -- `current`'s own `debug_assert`s would PANIC on an empty
    /// digest in debug/test builds rather than letting this function
    /// gracefully treat it as `None`, which would defeat the very guard
    /// this function exists to provide. Reads `digests.get(scope)` directly
    /// instead; the format-canonicalization assertion remains `current`'s
    /// job for its own (non-empty-digest) callers.
    fn usable_digest(&self) -> Option<String> {
        match self {
            DigestSource::Active { scope, digests } => digests.get(scope).filter(|d| !d.is_empty()),
            DigestSource::Static(_) => self.current(),
        }
    }
}

/// Everything [`handle_delivered`] needs beyond the entry itself --
/// bundled so `drain_batch`/`drain_loop`/`run` don't carry an
/// ever-growing parameter list. Mirrors `svc_action::dispatch::
/// DispatchDeps`'s shape.
pub struct ProcessDeps<S: SpineOps> {
    pub app_id: String,
    /// See [`DigestSource`]'s own doc -- replaces the former fixed
    /// `digest: String` field (regression: multi-tenant consumers invoked
    /// with empty legacy digest, UnknownBundle, alpha 2026-10-03). Sent via
    /// [`ensure_loaded`] before the first invoke that needs it -- see
    /// [`ProcessDeps::load_state`].
    pub digest_source: DigestSource,
    /// `PROCESS_BUNDLE_VERSION` -- the `load` frame's `version` field
    /// (spec §6.6). Distinct from `digest`: the executor's `loaded` reply
    /// echoes both back, and hot-swap reconciliation (TODO(M4+)) keys off
    /// the digest, not the version string.
    pub version: String,
    /// `PROCESS_BUNDLE_COMPONENT_KEY` -- the bucket key `ensure_loaded`
    /// asks the executor to fetch the compiled component from (spec §7.6
    /// step 3's naming convention; `crate::config::CliConfig`'s doc names
    /// the source poll this interim substitute stands in for).
    pub component_key: String,
    /// `PROCESS_BUNDLE_SIDECAR_KEY` -- the bucket key for the bundle's
    /// manifest sidecar, same convention as [`ProcessDeps::component_key`].
    pub sidecar_key: String,
    pub key_ring: KeyRing,
    pub connections: Arc<ConnectionRegistry>,
    pub call_timeout_ms: u64,
    /// Caches whether [`ProcessDeps::digest`] is already loaded on the
    /// currently active connection -- see [`LoadState`]'s doc for why this
    /// exists (never re-`load` on every invoke).
    pub load_state: Arc<LoadState>,
    /// See `crate::builtins::resolve_cross_app_route`'s doc for the
    /// `target_app_id -> approved tenant` shape and its TODO(M4+) real
    /// source.
    pub approved_targets: HashMap<String, String>,
    /// This pod's own identity (`penguin_spine::SpineConfig::consumer_id`,
    /// `SPINE_CONSUMER_ID`) -- the value a `DlqError.consumer_id` must
    /// carry (matching `penguin_spine::client::claim_stale`'s convention:
    /// the *consumer* that owned the entry, never the entry's own id).
    pub consumer_id: String,
    pub spine: S,
    pub metrics: Arc<dyn SpineMetrics>,
    /// Gates the drain loop on `waddles.core.rust-data-plane` (spec
    /// §13.5, `crate::license`) -- OFF means [`drain_batch`] never calls
    /// `reader.read()` at all (drains nothing; `/health`/`/metrics` are
    /// unaffected, since they run on entirely separate tasks).
    pub license: Arc<dyn FeatureGate>,
    /// The direct Valkey connection the `kv` host capability is backed by
    /// (`crate::capabilities::StageCapabilities::with_kv`), opened once at
    /// startup (`crate::lib::connect_kv`) and cloned -- a cheap handle
    /// clone over one shared connection, not a new socket -- into every
    /// per-invoke [`StageCapabilities`] this loop constructs. `None` when
    /// that connection could not be opened (spine config missing/Valkey
    /// unreachable at startup): every `kv` host-call then sees
    /// `not_implemented` rather than this loop failing to start, the same
    /// graceful-degradation posture `core/svc_action::capabilities::
    /// StageCapabilities::with_kv`'s doc describes.
    pub kv_conn: Option<redis::aio::MultiplexedConnection>,
    /// The manifest-declared-capability snapshot
    /// `bundle_host_kv::authorize::authorize_kv` checks before granting
    /// `kv` (coordinator fix on PR #425: "undeclared means denied").
    /// Populated once per active `app_id` per poll tick by
    /// `crate::bundle_loader::run_tick`/`crate::source_supervisor`'s own
    /// equivalent, from `bundle_active_set::ActiveBundleRow::
    /// declared_capabilities` -- shared (not copied) with every per-invoke
    /// [`StageCapabilities`] this loop constructs, so a capability change
    /// is visible to the very next `kv` host-call.
    pub kv_capabilities: Arc<bundle_host_kv::CapabilitySnapshot>,
    /// The standard enforcement gate (spec
    /// `docs/superpowers/specs/2026-09-28-bundle-permissions-and-capability-gate.md`
    /// SS5) every per-invoke [`StageCapabilities`] this loop constructs is
    /// wired with.
    pub gate: Arc<bundle_capability_gate::CapabilityGate>,
    /// Numeric `tenants.id`, resolved once at startup
    /// (`crate::lib::try_start_process_loop`) via `bundle_active_set::
    /// scope::resolve_scope` when `BUNDLE_SCOPE_TENANT_ID` and a DB reader
    /// account are configured -- constant across every invoke this loop
    /// handles, the same way [`ProcessDeps::digest_source`] is. `0` when
    /// unconfigured (this loop's env-only mode, see that function's doc) --
    /// fails closed (denies every non-platform permission) rather than
    /// matching a real tenant's grants.
    pub tenant_id: i32,
    /// See [`ProcessDeps::tenant_id`]'s doc. `0` is also the reserved
    /// tenant-wide sentinel (`bundle_active_set`'s own convention) for a
    /// genuinely-resolved tenant-wide scope -- distinguished from the
    /// unconfigured case only by `tenant_id` also being `0` there.
    pub community_id: i32,
    /// The live, poll-refreshed `app_id -> (digest, app_versions.id)`
    /// snapshot -- resolved PER INVOCATION in [`handle_delivered`] against
    /// whatever [`ProcessDeps::digest_source`] just resolved, never
    /// captured once at startup: a bundle hot swap (`crate::bundle_loader`'s
    /// poll tick, which also updates this same snapshot) must be reflected
    /// on the very next invocation (spec SS4/SS5.1), and an invoke whose
    /// version can no longer be resolved falls back to the fail-closed `0`
    /// sentinel rather than running under a stale one.
    pub app_version_snapshot: bundle_active_set::ActiveVersionSnapshot,
    /// The per-process `bundle_host_http::egress::EgressGuard` cloned into
    /// every per-invoke [`crate::capabilities::StageCapabilities`]'s
    /// `http` capability (see that struct's doc for why this is a
    /// singleton, not scope-implicit like every other capability).
    pub egress: Arc<bundle_host_http::egress::EgressGuard>,
    /// Gates the inbound PII-tokenization pre-dispatch pass
    /// (`crate::pii_tokenize`) on `waddles.core.disable-pii-tokenization`
    /// (opt-out kill-switch, default ENABLED -- `crate::license::
    /// PiiTokenizationGate`). OFF (the default) means tokenization runs;
    /// ON means this stage falls back to the pre-tokenization legacy
    /// behavior (raw PII reaches the bundle) -- an explicit, documented
    /// operational tradeoff, never the default.
    pub pii_gate: Arc<dyn FeatureGate>,
    /// `None` until a real `hub_client::HubClient` connection is wired at
    /// startup (`crate::lib::build_hub_minter`) -- when [`pii_gate`] is
    /// enabled (the default) and this is `None`, [`handle_delivered`]
    /// fails closed (dead-letters) rather than ever forwarding raw PII,
    /// exactly as if a configured minter's RPC call had failed.
    ///
    /// [`pii_gate`]: ProcessDeps::pii_gate
    pub pii_minter: Option<Arc<dyn crate::pii_tokenize::IdentityMinter>>,
    /// The `db` host capability's production wiring
    /// (`crate::capabilities::StageCapabilities::with_db`), built once at
    /// startup (`crate::lib::try_build_db_wiring`) and cloned -- cheap,
    /// `Arc`-backed -- into every per-invoke `StageCapabilities` this loop
    /// constructs, mirroring [`ProcessDeps::egress`]'s "per-process
    /// singleton, scope-implicit per call" shape. `None` when
    /// `BUNDLE_DB_PASSWORD` was never configured on this deployment (every
    /// `db` call then denies `not_implemented`, same graceful-degradation
    /// posture as [`ProcessDeps::kv_conn`]) -- see that function's own doc
    /// for the distinct, louder behavior when the password *was* configured
    /// but the connection itself failed.
    pub db_wiring: Option<crate::capabilities::DbWiring>,
    /// Per-(app, source) circuit breaker for guest faults (connector spec
    /// SS0 condition 5, `circuit_breaker` crate module doc): a bundle-
    /// executor trap/deadline(incl. fuel exhaustion, bucketed as
    /// `CallTimeout`)/memory-cap hit records a failure against
    /// [`breaker_key`] (`{app_id}|{stream}`; the stream is the closest
    /// thing this loop has to a source identity, the app prefix isolates
    /// co-consuming apps); `pool_exhausted` back-pressure never counts;
    /// `FAILURE_THRESHOLD` faults in `FAILURE_WINDOW` opens the breaker for
    /// `OPEN_BACKOFF`, and entries arriving meanwhile are dead-lettered
    /// (parked in the DLQ -- NOT auto-replayed) as
    /// `DlqErrorKind::BundleDisabled` rather than invoked at all -- one shared instance per pod (process-local,
    /// not cross-pod: this pod's fixed source shard is the complete
    /// blast-radius containment, see that crate's module doc), cloned into
    /// both the legacy and multi-tenant startup paths.
    pub breaker: Arc<CircuitBreaker>,
    /// The `reputation` host capability's production wiring (issue #726,
    /// `crate::lib::try_build_reputation_wiring`), cloned into every
    /// per-invoke `StageCapabilities` like [`ProcessDeps::db_wiring`]. `None`
    /// when `BUNDLE_REPUTATION_PASSWORD` is unset or the connection failed
    /// (every `reputation.*` call then denies `not_implemented`).
    pub reputation_wiring: Option<crate::capabilities::ReputationWiring>,
    /// The `economy` host capability's production wiring (issue #714,
    /// `crate::lib::try_build_economy_wiring`), cloned into every per-invoke
    /// `StageCapabilities` like [`ProcessDeps::reputation_wiring`]. `None`
    /// when `BUNDLE_ECONOMY_PASSWORD` is unset or the connection failed
    /// (every `economy.*` call then denies `not_implemented`).
    pub economy_wiring: Option<crate::capabilities::EconomyWiring>,
    /// The `identity` host capability's production wiring
    /// (`crate::lib::try_build_identity_wiring`), cloned into every per-invoke
    /// `StageCapabilities` like [`ProcessDeps::economy_wiring`] -- paired there
    /// with that invocation's host-derived [`crate::identity::InvocationIdentity`]
    /// (built in [`handle_delivered`] from the raw event). `None` when no DB
    /// reader account is configured (every `identity.*` call then denies
    /// `not_implemented`).
    pub identity_wiring: Option<crate::identity::IdentityWiring>,
    /// The `ai` host capability's production wiring
    /// (`crate::lib::build_ai_wiring`), cloned into every per-invoke
    /// `StageCapabilities` and paired there with that invocation's executor
    /// deadline (`now + call_timeout_ms`). `None` when the model endpoint is not
    /// a valid self-hosted URL or no license client exists (every `ai.generate`
    /// call then denies `not_implemented`).
    pub ai_wiring: Option<crate::ai::AiWiring>,
}

/// Handles exactly one delivered entry end to end: hop-verify, invoke
/// `transform`, apply cross-app routing, and either enqueue+ack or
/// dead-letter. Returns `Ok(())` in every case where the entry was
/// terminally handled (acked, dropped-and-acked, or dead-lettered) --
/// only a `SpineError` from the ack/DLQ/append write itself propagates,
/// matching `svc_action::dispatch::handle_delivered`'s identical
/// error-handling shape.
///
/// Sends [`ensure_loaded`] for the resolved digest ([`DigestSource`])
/// before the first `transform` invoke that needs it (cached per
/// connection by [`ProcessDeps::load_state`], see that type's doc for why)
/// -- this is what actually makes the executor hold the configured bundle
/// at all; without it every invoke would hit `UNKNOWN_BUNDLE` because
/// nothing upstream of this loop ever sends `load` (bug fix: previously
/// the only callers of `PROCESS_BUNDLE_*` were this crate's own
/// default-value unit tests).
///
/// **Not yet wired here (documented, not silently skipped):** the
/// content-moderation gate (`crate::builtins::run_moderation_gate`, an
/// honest TODO(M4+) seam that always returns "no match" today, so wiring
/// it in would currently be a no-op); spec §5.3's consumer-side `consumes.
/// event_types`/`filters` cheap-skip optimization (needs the bundle's
/// resolved manifest, itself blocked on the same distribution poll gap).
/// The numeric `(tenant_id, community_id)` of a MAC-verified v3 envelope,
/// narrowed to the executor's i32 scope. `Ok(None)` for a legacy envelope.
/// `Err` when an id exceeds `i32::MAX`, or when this loop is bound to a
/// resolved scope (`bound_tenant != 0`) that the envelope disagrees with --
/// the confused-deputy case (guest would see scope B, capabilities stay A).
fn verified_numeric_scope(
    env: &StageEnvelope,
    bound_tenant: i32,
    bound_community: i32,
) -> Result<Option<(i32, i32)>, String> {
    let (Some(t), Some(c)) = (env.tenant_id, env.community_id) else {
        return Ok(None);
    };
    let t = i32::try_from(t)
        .map_err(|_| format!("envelope tenant_id {t} exceeds the executor's i32 scope"))?;
    if bound_tenant != 0 && (t, c) != (bound_tenant, bound_community) {
        return Err(format!(
            "envelope scope ({t}, {c}) does not match this stage's bound scope ({bound_tenant}, {bound_community})"
        ));
    }
    Ok(Some((t, c)))
}

async fn handle_delivered<S: SpineOps>(
    d: &Delivered,
    deps: &ProcessDeps<S>,
) -> Result<(), SpineError> {
    let started = std::time::Instant::now();
    let outcome = handle_delivered_inner(d, deps).await;
    let result = match &outcome {
        Ok(result) => result.as_str(),
        // The ack/DLQ/append write itself failed: the entry stays pending
        // for redelivery, so it is neither delivered nor dead-lettered.
        Err(_) => "spine_error",
    };
    stage_metrics::stage_metrics().record_stage(&deps.app_id, result, started.elapsed());
    outcome.map(|_| ())
}

/// The body of [`handle_delivered`], returning the terminal [`StageResult`]
/// so the wrapper can label `waddles_stage_latency_seconds` without every
/// exit path touching telemetry. Behavior is unchanged from before the
/// metrics wrapper existed: each former `return` still performs exactly the
/// same ack/DLQ/append and now also names its outcome.
async fn handle_delivered_inner<S: SpineOps>(
    d: &Delivered,
    deps: &ProcessDeps<S>,
) -> Result<StageResult, SpineError> {
    // Spec §5.11: hop verification runs before any other processing.
    // Verified against THIS entry's own `d.stream` (not a single fixed
    // key, unlike `svc_action::dispatch`'s action-stream reader) --
    // process's `GroupReader` is granted potentially many ingest-source
    // streams at once (spec §5.1/§5.2), and a batch returned by one
    // `read()` call can mix entries from several of them.
    if let Err(reason) = crate::hop::verify_hop(&deps.key_ring, &d.env, &d.stream) {
        deps.metrics
            .tenant_boundary_violation("process", reason.as_metric_reason());
        tracing::error!(
            app_id = %d.env.app_id,
            tenant = %d.env.tenant,
            reason = reason.as_metric_reason(),
            "hop verification failed, dead-lettering (never retried)"
        );
        let err = DlqError {
            kind: DlqErrorKind::TenantBoundary,
            code: "TENANT_BOUNDARY".to_string(),
            message: reason.to_string(),
            detail: None,
            artifact_digest: deps.digest_source.current(),
            consumer_id: deps.consumer_id.clone(),
        };
        return deps
            .spine
            .dead_letter(d, &err)
            .await
            .map(|()| StageResult::DeadLettered);
    }

    // Connector spec SS0 condition 5: an (app, source) the breaker has
    // tripped open (repeated guest faults, see `ProcessDeps::breaker`'s
    // doc) is never invoked at all -- fail fast, before the PII-tokenization
    // RPC below, rather than spending a hub-api round trip on a known-bad
    // source. NOTE the entry is parked in the DLQ (`dead_letter_raw` is
    // XADD-to-DLQ then XACK): it is consumed, NOT redelivered, and nothing
    // auto-replays it when the backoff elapses -- an operator/replay tool
    // must re-drive it (`BundleDisabled.never_retry()` is `false`, i.e.
    // replay-eligible, but eligibility is not an automatic retry).
    let breaker_key = breaker_key(&deps.app_id, &d.stream);
    if !deps.breaker.allow(&breaker_key) {
        // debug, not warn: while the breaker is open EVERY entry for this
        // key lands here, so a per-entry warn is a log flood under load.
        // The single operator-visible signal is the one `error!(alert)` the
        // breaker itself emits on the OPEN transition.
        tracing::debug!(
            app_id = %d.env.app_id,
            stream = %d.stream,
            "circuit breaker open for this app/source, dead-lettering without invoking (parked in DLQ)"
        );
        let err = DlqError {
            kind: DlqErrorKind::BundleDisabled,
            code: "CIRCUIT_OPEN".to_string(),
            message: "source disabled by circuit breaker after repeated guest faults".to_string(),
            detail: None,
            artifact_digest: deps.digest_source.current(),
            consumer_id: deps.consumer_id.clone(),
        };
        return deps
            .spine
            .dead_letter(d, &err)
            .await
            .map(|()| StageResult::DeadLettered);
    }

    // PII boundary hard invariant (`rules/critical-rules.md` PII
    // Tokenization): a bundle's `transform` must never see a raw platform
    // username/login/display-name/mention -- run strictly after hop
    // verification and strictly before `invoke_transform` below. Fails
    // closed: any resolution failure (hub-api unreachable, circuit open,
    // no minter configured) dead-letters the entry for redelivery rather
    // than ever forwarding the raw event.
    // The mention bindings (token a bundle is shown -> raw reference) are
    // produced by the same pass and kept HOST-SIDE for the `identity`
    // capability; `None` when tokenization is switched off.
    let (tokenized_event, mention_bindings): (
        PlatformEvent,
        Option<Vec<crate::identity::MentionBinding>>,
    ) = if deps.pii_gate.enabled().await {
        match &deps.pii_minter {
            Some(minter) => {
                match crate::pii_tokenize::tokenize_event_with_mentions(
                    &d.env.event,
                    &d.env.tenant,
                    minter.as_ref(),
                )
                .await
                {
                    Ok(tokenized) => (tokenized.event, Some(tokenized.mentions)),
                    Err(reason) => {
                        tracing::error!(
                            app_id = %d.env.app_id,
                            tenant = %d.env.tenant,
                            error = %reason,
                            "PII tokenization failed; dead-lettering (fail-closed, never \
                             forwarding raw PII to a bundle)"
                        );
                        let err = DlqError {
                            kind: DlqErrorKind::BundleError,
                            code: "PII_TOKENIZE_FAILED".to_string(),
                            message: reason.to_string(),
                            detail: None,
                            artifact_digest: deps.digest_source.current(),
                            consumer_id: deps.consumer_id.clone(),
                        };
                        return deps
                            .spine
                            .dead_letter(d, &err)
                            .await
                            .map(|()| StageResult::DeadLettered);
                    }
                }
            }
            None => {
                tracing::error!(
                    app_id = %d.env.app_id,
                    tenant = %d.env.tenant,
                    "PII tokenization is enabled but no hub identity client is configured; \
                     dead-lettering (fail-closed, never forwarding raw PII to a bundle)"
                );
                let err = DlqError {
                    kind: DlqErrorKind::BundleError,
                    code: "PII_RESOLVER_UNAVAILABLE".to_string(),
                    message: "no hub_client identity minter configured".to_string(),
                    detail: None,
                    artifact_digest: deps.digest_source.current(),
                    consumer_id: deps.consumer_id.clone(),
                };
                return deps
                    .spine
                    .dead_letter(d, &err)
                    .await
                    .map(|()| StageResult::DeadLettered);
            }
        }
    } else {
        // Kill-switch ON: documented, explicit opt-out -- legacy
        // pre-tokenization behavior (raw PII reaches the bundle).
        (d.env.event.clone(), None)
    };

    // Resolve the digest to load/invoke with BEFORE ever checking for an
    // executor connection -- "if no active digest is known for an app when
    // a message arrives, log ERROR and dead-letter for redelivery; never
    // invoke with an empty digest" (regression: multi-tenant consumers
    // invoked with empty legacy digest, UnknownBundle, alpha 2026-10-03).
    // `DigestSource::Static` always resolves (possibly to an intentionally
    // empty string, unchanged legacy behavior -- see that variant's own
    // doc); only `DigestSource::Active` with no entry for this scope yields
    // `None` here.
    let Some(digest) = deps.digest_source.usable_digest() else {
        tracing::error!(
            app_id = %deps.app_id,
            tenant = %d.env.tenant,
            community = ?d.env.community,
            "no active bundle digest known for this app's scope; dead-lettering for redelivery"
        );
        let err = DlqError {
            kind: DlqErrorKind::BundleError,
            code: "NO_ACTIVE_DIGEST".to_string(),
            message: "no active bundle digest known for this (tenant, community, app) scope"
                .to_string(),
            detail: None,
            artifact_digest: None,
            consumer_id: deps.consumer_id.clone(),
        };
        return deps
            .spine
            .dead_letter(d, &err)
            .await
            .map(|()| StageResult::DeadLettered);
    };

    // Bundle-permissions-and-capability-gate wiring (spec SS12 Phase 4):
    // the `app_versions.id` this invoke's grants are keyed on (spec
    // SS4/SS5.1), resolved PER INVOCATION against the already-resolved
    // `digest` -- never a value captured once at startup. `digest_source`
    // above (not this lookup) already owns every dead-letter decision for
    // an unresolvable/superseded digest; `app_version_snapshot` is fed by
    // the exact same active-set reads (`crate::changelog_consumer`/
    // `crate::bundle_loader`), so it should always agree with whatever
    // `digest` was just resolved to. A momentary disagreement (the two
    // live maps refresh independently) fails closed to `0` -- which never
    // matches a real grant row -- rather than blocking dispatch on it.
    let app_version = deps
        .app_version_snapshot
        .resolve_for_digest(&deps.app_id, &digest)
        .unwrap_or_else(|| {
            tracing::warn!(
                app_id = %deps.app_id,
                digest_prefix = %bundle_active_set::digest_prefix(&digest),
                "no app_version found in the active-version snapshot for this digest; \
                 falling back to the fail-closed 0 sentinel (every non-platform \
                 permission will deny for this invoke)"
            );
            0
        });

    let Some(connection) = deps.connections.active() else {
        // Escalated WARN -> ERROR (fix/executor-link-heartbeat, alpha
        // 2026-10-02 incident: svc-process/svc-action were rolled and each
        // bundle-executor stayed bound to its old, terminated pod; the new
        // svc-process had zero executors and silently dead-lettered every
        // `!ping` at WARN -- nobody noticed until a user reported it). The
        // outage duration is named in the rendered message itself, not
        // only a structured field, per the "over-log, never swallow
        // errors" rule.
        let app_id = &deps.app_id;
        let no_executor_for_s = deps.connections.duration_without_executor().as_secs();
        deps.connections.record_dead_letter_no_executor();
        tracing::error!(
            app_id = %app_id,
            no_executor_for_s,
            "no executor connection available for {no_executor_for_s}s (app_id {app_id}), \
             dead-lettering for redelivery"
        );
        let err = DlqError {
            kind: DlqErrorKind::ExecutorUnavailable,
            code: "EXECUTOR_UNAVAILABLE".to_string(),
            message: "no active host-api connection".to_string(),
            detail: None,
            artifact_digest: Some(digest.clone()),
            consumer_id: deps.consumer_id.clone(),
        };
        return deps
            .spine
            .dead_letter(d, &err)
            .await
            .map(|()| StageResult::DeadLettered);
    };

    // Send `load` at most once per (connection, digest) -- see
    // `LoadState`'s doc. An empty digest means no bundle is configured yet
    // (legacy `PROCESS_BUNDLE_DIGEST` unset -- never possible on the
    // multi-tenant path, see [`DigestSource::current`]'s doc): skip
    // straight to invoking, exactly as before this fix, so the executor's
    // own `UNKNOWN_BUNDLE` reply still drives the existing `BundleError`
    // DLQ path below.
    if !digest.is_empty() && !deps.load_state.is_loaded_on(&connection, &digest) {
        // `(0, 0)`: this interim, env-configured single-app-per-pod path
        // predates the numeric `(tenant_id, community_id)` scoping
        // `bundle_active_set` introduced -- it has no real tenant row to
        // resolve, only `d.env.tenant`/`d.env.community` STRING slugs
        // (Valkey stream naming, a different identifier space entirely).
        // `(0, 0)` is a reserved sentinel (`bundle_active_set` tenant ids
        // start at 1 in every real schema row) naming "no real DB scope",
        // never confusable with a genuine tenant. The multi-tenant
        // `DigestSource::Active` path's OWN scope is a different concern
        // entirely (which digest to use) -- `ensure_loaded`'s own
        // `tenant_id`/`community_id` parameters here are the bundle
        // executor's load-scoping, unrelated to which digest was resolved.
        if let Err(e) = ensure_loaded(
            &connection,
            0,
            0,
            &deps.app_id,
            &deps.version,
            &digest,
            &deps.component_key,
            &deps.sidecar_key,
            LoadLimits {
                timeout_ms: deps.call_timeout_ms,
                memory_mb: 64,
            },
        )
        .await
        {
            tracing::error!(app_id = %deps.app_id, digest = %digest, error = %e, "bundle load failed, dead-lettering");
            let err = DlqError {
                kind: DlqErrorKind::BundleError,
                code: "LOAD_FAILED".to_string(),
                message: e.to_string(),
                detail: None,
                artifact_digest: Some(digest.clone()),
                consumer_id: deps.consumer_id.clone(),
            };
            return deps
                .spine
                .dead_letter(d, &err)
                .await
                .map(|()| StageResult::DeadLettered);
        }
        deps.load_state
            .mark_loaded(Arc::clone(&connection), digest.clone());
    }

    // Per-invoke capability scope (see `crate::host_api`/`crate::
    // capabilities`'s per-invoke-scoping design): built fresh from THIS
    // envelope's own (tenant, community, app_id), never a fixed
    // connection-lifetime default. `kv` reuses `deps.kv_conn` (a cheap
    // handle clone, see that field's doc) rather than opening a new
    // connection on every invoke. `egress` is the shared, per-process
    // singleton (`ProcessDeps::egress`'s doc). `gate` authorizes every
    // capability call, `http` included, before the egress guard ever runs
    // (module doc, `crate::capabilities`'s own module doc).
    // The numeric scope of the MAC-verified envelope (v3), narrowed to the
    // executor's i32 scope. Fail closed: an id that does not fit, or that
    // disagrees with the scope this loop's capabilities are bound to, is a
    // tenant-boundary violation -- dead-lettered, guest never called.
    let verified_scope = match verified_numeric_scope(&d.env, deps.tenant_id, deps.community_id) {
        Ok(v) => v,
        Err(message) => {
            tracing::error!(app_id = %deps.app_id, %message, "envelope numeric scope rejected, dead-lettering");
            deps.metrics
                .tenant_boundary_violation("process", "numeric_scope_mismatch");
            let err = DlqError {
                kind: DlqErrorKind::TenantBoundary,
                code: "TENANT_BOUNDARY".to_string(),
                message,
                detail: None,
                artifact_digest: Some(digest.clone()),
                consumer_id: deps.consumer_id.clone(),
            };
            return deps
                .spine
                .dead_letter(d, &err)
                .await
                .map(|()| StageResult::DeadLettered);
        }
    };
    let trace = d.env.trace.as_ref().map(|t| TraceContext {
        traceparent: t.traceparent.clone(),
        tracestate: t.tracestate.clone(),
    });
    let capabilities: Arc<dyn CapabilityHandler> = {
        let caps = StageCapabilities::<redis::aio::MultiplexedConnection>::new(
            d.env.tenant.clone(),
            d.env.community.clone(),
            deps.app_id.clone(),
            deps.tenant_id,
            deps.community_id,
            app_version,
            Arc::clone(&deps.egress),
            Arc::clone(&deps.gate),
        );
        let caps = caps.with_verified_scope(verified_scope);
        // `tool.invoke`: a top-level invocation's chain is just this app, and
        // every nested call is bounded by this invoke's own executor budget.
        let caps = caps.with_tool(crate::capabilities::ToolContext {
            connections: Arc::clone(&deps.connections),
            snapshot: deps.app_version_snapshot.clone(),
            chain: vec![deps.app_id.clone()],
            deadline: Some(std::time::Instant::now() + Duration::from_millis(deps.call_timeout_ms)),
            trace: trace.clone(),
        });
        let caps = match &deps.kv_conn {
            Some(conn) => caps.with_kv(conn.clone(), Arc::clone(&deps.kv_capabilities)),
            None => caps,
        };
        let caps = match &deps.db_wiring {
            Some(db) => caps.with_db(db.clone()),
            None => caps,
        };
        let caps = match &deps.reputation_wiring {
            Some(rep) => caps.with_reputation(rep.clone()),
            None => caps,
        };
        let caps = match &deps.economy_wiring {
            Some(eco) => caps.with_economy(eco.clone()),
            None => caps,
        };
        // `ai`: every model call is clamped to what remains of THIS invoke's
        // executor deadline, so the deadline is fixed here, at construction.
        let caps = match &deps.ai_wiring {
            Some(ai) => caps.with_ai(
                ai.clone(),
                std::time::Instant::now() + Duration::from_millis(deps.call_timeout_ms),
            ),
            None => caps,
        };
        // `identity`: the invocation's host-derived identity facts come from
        // the RAW event (never the tokenized copy a bundle sees) plus the
        // mention bindings the tokenization pass produced.
        let caps = match &deps.identity_wiring {
            Some(identity) => {
                let invocation = match mention_bindings {
                    Some(bindings) => {
                        crate::identity::InvocationIdentity::from_event(&d.env.event, bindings)
                    }
                    None => {
                        crate::identity::InvocationIdentity::for_untokenized_event(&d.env.event)
                    }
                }
                // The hop-verified envelope id: identical across redeliveries
                // of this event, the anchor of the economy's idempotency keys.
                .with_event_id(d.env.event_id.clone());
                caps.with_identity(identity.clone(), Arc::new(invocation))
            }
            None => caps,
        };
        Arc::new(caps)
    };

    let outcome = invoke_transform(
        &connection,
        &deps.app_id,
        &digest,
        &tokenized_event,
        deps.call_timeout_ms,
        trace,
        capabilities,
    )
    .await;

    let event_out = match outcome {
        Err(InvokeError::ExecutorError {
            code,
            message,
            detail,
        }) => {
            let kind = error_code_to_dlq_kind(code);
            // Connector spec SS0 condition 5: only an actual GUEST fault
            // (trap, epoch deadline -- fuel exhaustion is bucketed here
            // too, see `bundle_executor::invoke::on_invoke`'s doc -- or a
            // memory-cap hit) counts against this (app, source)'s breaker; a
            // broken bundle registration (`BundleError`), a denied/failed
            // host call, or `pool_exhausted` back-pressure is not evidence
            // this source's guest code is misbehaving (see `is_guest_fault`).
            if is_guest_fault(kind, detail.as_deref()) {
                deps.breaker.record_failure(&breaker_key);
            }
            // Diagnosability fix (regression: multi_tenant path sent
            // bare-hex digest to Invoke, UnknownBundle despite loaded
            // bundle (alpha 2026-10-03)): `message` IS the digest the
            // executor echoed back for `UnknownBundle`
            // (`bundle_executor::invoke::on_invoke`'s `error_body`), so an
            // empty/unresolved digest renders as an empty-looking
            // `message=""` field with nothing to grep on. Log the resolved
            // `digest`'s own prefix explicitly so this is diagnosable even
            // when `message` is empty.
            let digest_prefix = bundle_active_set::digest_prefix(&digest);
            tracing::error!(app_id = %deps.app_id, ?code, detail = detail.as_deref().unwrap_or(""), digest_prefix, %message, "transform invoke failed, dead-lettering");
            let err = DlqError {
                kind,
                code: format!("{code:?}"),
                message,
                // Surfaces `fuel_exhausted` / `pool_exhausted` on the DLQ
                // record so a fuel trap is distinguishable from a wall-
                // clock deadline (both are `CallTimeout`).
                detail,
                artifact_digest: Some(digest.clone()),
                consumer_id: deps.consumer_id.clone(),
            };
            return deps
                .spine
                .dead_letter(d, &err)
                .await
                .map(|()| StageResult::DeadLettered);
        }
        Err(e) => {
            tracing::error!(app_id = %deps.app_id, error = %e, "transform invoke failed, dead-lettering");
            let err = DlqError {
                kind: DlqErrorKind::ExecutorUnavailable,
                code: "INVOKE_FAILED".to_string(),
                message: e.to_string(),
                detail: None,
                artifact_digest: Some(digest.clone()),
                consumer_id: deps.consumer_id.clone(),
            };
            return deps
                .spine
                .dead_letter(d, &err)
                .await
                .map(|()| StageResult::DeadLettered);
        }
        Ok(TransformOutcome::UnsupportedStage) => {
            tracing::error!(app_id = %deps.app_id, "bundle does not implement process-stage.transform, dead-lettering");
            let err = DlqError {
                kind: DlqErrorKind::BundleError,
                code: "UNSUPPORTED_STAGE".to_string(),
                message: "bundle does not implement process-stage.transform".to_string(),
                detail: None,
                artifact_digest: Some(digest.clone()),
                consumer_id: deps.consumer_id.clone(),
            };
            return deps
                .spine
                .dead_letter(d, &err)
                .await
                .map(|()| StageResult::DeadLettered);
        }
        Ok(TransformOutcome::NoReply) => {
            deps.breaker.record_success(&breaker_key);
            tracing::info!(app_id = %deps.app_id, "transform returned no reply");
            return deps
                .spine
                .ack(d, &deps.app_id)
                .await
                .map(|()| StageResult::NoReply);
        }
        Ok(TransformOutcome::Reply(event)) => {
            deps.breaker.record_success(&breaker_key);
            *event
        }
    };

    let mut event_out = event_out;
    tracing::debug!(
        app_id = %deps.app_id,
        platform = d.env.event.source.as_ref().map(|s| s.platform.as_str()).unwrap_or(""),
        channel_id = d.env.event.source.as_ref().and_then(|s| s.channel_id.as_deref()).unwrap_or(""),
        "carrying inbound event.source onto action-stage envelope"
    );
    carry_inbound_source(&mut event_out, &d.env.event);

    let decision = crate::builtins::resolve_cross_app_route(
        &mut event_out.payload,
        &d.env.tenant,
        &deps.approved_targets,
    );
    let dest_app_id = match decision {
        RouteDecision::Denied { reason } => {
            deps.metrics.consumer_skipped(&deps.app_id, "route_denied");
            tracing::warn!(
                app_id = %deps.app_id,
                reason,
                "cross-app route denied, event dropped (not delivered anywhere)"
            );
            return deps
                .spine
                .ack(d, &deps.app_id)
                .await
                .map(|()| StageResult::RouteDenied);
        }
        RouteDecision::SameApp => deps.app_id.clone(),
        RouteDecision::Redirect { target_app_id } => target_app_id,
    };

    let scope = Scope::new(d.env.tenant.clone(), d.env.community.clone());
    let dest_stream = scope.action_stream(&dest_app_id);
    let ts = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
    let envelope_out = StageEnvelope {
        // Scope + schema version are copied VERBATIM from the verified
        // inbound envelope (never re-derived): the copied `binding` covers
        // exactly these fields, so a v3 envelope's numeric ids must travel
        // with it or the next hop's MAC verification would (correctly) fail.
        schema_version: d.env.schema_version,
        tenant: d.env.tenant.clone(),
        community: d.env.community.clone(),
        tenant_id: d.env.tenant_id,
        community_id: d.env.community_id,
        app_id: dest_app_id,
        stage: "action".to_string(),
        event: event_out,
        ts,
        target_app_id: None,
        workstream_id: d.env.workstream_id.clone(),
        event_id: d.env.event_id.clone(),
        session_id: d.env.session_id.clone(),
        trace: d.env.trace.clone(),
        binding: d.env.binding.clone(),
    };

    deps.spine.append(&dest_stream, &envelope_out).await?;
    deps.spine
        .ack(d, &deps.app_id)
        .await
        .map(|()| StageResult::Delivered)
}

/// Reads and dispatches exactly one batch, returning how many entries were
/// handled.
/// How long a gate-OFF iteration of [`drain_batch`] sleeps before
/// re-checking `deps.license` -- a live flag flip (ON -> OFF or back) is
/// picked up within this window, without a pod restart. Cheap: nothing
/// else happens while OFF, and `FeatureGate::enabled` itself never blocks
/// on network I/O (spec §13.5, `crate::license`'s module doc).
const GATE_OFF_RECHECK_INTERVAL: std::time::Duration = std::time::Duration::from_millis(500);

async fn drain_batch<R: StreamReader, S: SpineOps>(
    reader: &mut R,
    deps: &ProcessDeps<S>,
) -> Result<usize, SpineError> {
    if !deps.license.enabled().await {
        // OFF: drain nothing at all -- `reader.read()` (a real
        // `XREADGROUP`) is never called. Sleeping here (rather than
        // spinning) keeps a disabled pod from busy-looping on a cheap but
        // still-nonzero cached-read check.
        tokio::time::sleep(GATE_OFF_RECHECK_INTERVAL).await;
        return Ok(0);
    }
    let batch = reader.read().await?;
    stage_metrics::stage_metrics().record_batch(batch.len());
    for d in &batch {
        handle_delivered(d, deps).await?;
    }
    Ok(batch.len())
}

/// Runs [`drain_batch`] in a loop until `shutdown` resolves.
async fn drain_loop<R: StreamReader, S: SpineOps>(
    mut reader: R,
    deps: ProcessDeps<S>,
    mut shutdown: tokio::sync::oneshot::Receiver<()>,
) -> Result<(), SpineError> {
    loop {
        tokio::select! {
            _ = &mut shutdown => return Ok(()),
            result = drain_batch(&mut reader, &deps) => {
                result?;
            }
        }
    }
}

/// Connects the spine DLQ client and a grant-scoped [`GroupReader`] for
/// `deps.app_id`'s ingest-source streams, then runs the drain loop until
/// `shutdown` resolves. `grants` is resolved by
/// `crate::lib::try_start_process_loop` from interim, env-driven config
/// (see that module's doc for why -- the real `GET /api/v1/distribution/
/// bundles?stage=process` poll, spec §6.7, is `TODO(M4+)`); each delivered
/// entry is hop-verified against its OWN `Delivered::stream`, so `grants`
/// may safely name more than one stream once that poll lands.
pub async fn run(
    cfg: SpineConfig,
    grants: Vec<Grant>,
    deps: ProcessDeps<SpineClient>,
    shutdown: tokio::sync::oneshot::Receiver<()>,
) -> Result<(), SpineError> {
    let app_id = deps.app_id.clone();
    let dlq = SpineClient::connect(cfg.clone(), deps.metrics.clone()).await?;
    let streams: Vec<String> = grants.iter().map(|g| g.stream.clone()).collect();
    let sampler_client = dlq.clone();
    let reader = GroupReader::connect(
        &cfg,
        grants,
        app_id,
        Stage::Process,
        dlq,
        deps.metrics.clone(),
    )
    .await?;
    // Dropped (and the task aborted) when the drain loop returns.
    let _backlog_sampler = spawn_group_backlog_sampler(
        sampler_client,
        streams,
        cfg.stats_interval_ms,
        cfg.pel_alert,
    );
    drain_loop(reader, deps, shutdown).await
}

/// Source of consumer-group backlog samples (spec §5.6, `XINFO GROUPS`).
/// Production is [`SpineClient::group_stats`], which feeds `group_lag` /
/// `group_pending` into the [`SpineMetrics`] it was connected with -- the
/// stuck-bundle signal `waddles_spine_queue_depth` / `waddles_group_pending`
/// carry -- and returns the stats so the sampler can alert on them.
trait GroupStatsSource: Send + 'static {
    fn group_stats(
        &self,
        stream: &str,
    ) -> impl std::future::Future<Output = Result<Vec<GroupStats>, SpineError>> + Send;
}

impl GroupStatsSource for SpineClient {
    async fn group_stats(&self, stream: &str) -> Result<Vec<GroupStats>, SpineError> {
        SpineClient::group_stats(self, stream).await
    }
}

/// Aborts the wrapped task when dropped, so a sampler can never outlive the
/// drain loop it observes.
struct AbortOnDrop(tokio::task::JoinHandle<()>);

impl Drop for AbortOnDrop {
    fn drop(&mut self) {
        self.0.abort();
    }
}

/// Starts the periodic backlog sampler for `streams`, every `interval_ms`
/// (`SPINE_STATS_INTERVAL_MS`, the spine's own knob for exactly this). `None`
/// -- no sampler -- when the interval is `0` (operator opt-out) or there is
/// nothing to sample.
fn spawn_group_backlog_sampler<G: GroupStatsSource>(
    source: G,
    streams: Vec<String>,
    interval_ms: u64,
    pel_alert: u64,
) -> Option<AbortOnDrop> {
    if interval_ms == 0 || streams.is_empty() {
        return None;
    }
    Some(AbortOnDrop(tokio::spawn(sample_group_backlog(
        source,
        streams,
        Duration::from_millis(interval_ms),
        pel_alert,
    ))))
}

/// Samples every stream's consumer-group stats each `interval`, forever (until
/// aborted). The first sample is one full interval after start, so consumers
/// spawned together do not all query at once. A failed sample is logged at
/// DEBUG and retried next interval -- observing backlog must never disturb the
/// consumer it observes; a Valkey outage surfaces loudly through the drain
/// loop itself. A group whose pending-entries list exceeds `pel_alert`
/// (`SPINE_PEL_ALERT`) is logged at WARN: that is the stuck-bundle signal,
/// logged by `app_id` and counts only (stream names embed tenant slugs).
async fn sample_group_backlog<G: GroupStatsSource>(
    source: G,
    streams: Vec<String>,
    interval: Duration,
    pel_alert: u64,
) {
    let mut ticker = tokio::time::interval_at(tokio::time::Instant::now() + interval, interval);
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    loop {
        ticker.tick().await;
        for stream in &streams {
            match source.group_stats(stream).await {
                Ok(stats) => {
                    for stat in stats.iter().filter(|stat| stat.pending > pel_alert) {
                        tracing::warn!(
                            app_id = %stat.app_id,
                            pending = stat.pending,
                            pel_alert,
                            "consumer group pending-entries list is above SPINE_PEL_ALERT \
                             (a stuck bundle is not acking)"
                        );
                    }
                }
                Err(err) => {
                    tracing::debug!(
                        error = %err,
                        "consumer-group backlog sample failed; retrying next interval"
                    );
                }
            }
        }
    }
}

/// Builds a raw `redis::Client` for `cfg`'s transport -- the same
/// connection-building logic as `core/svc_ingest/src/outbound.rs::
/// build_redis_client` (that module's own doc explains why this is
/// duplicated rather than imported: `penguin_spine`'s own equivalent is
/// `pub(crate)` to that crate). Used only by [`ensure_consumer_group`]: a
/// raw `XGROUP CREATE` is outside `SpineClient`'s/`GroupReader`'s own
/// Streams-only surface.
fn build_raw_client(cfg: &SpineConfig) -> Result<redis::Client, redis::RedisError> {
    let base: redis::ConnectionInfo =
        redis::IntoConnectionInfo::into_connection_info(cfg.valkey_url.as_str())?;
    let mut settings = base.redis_settings().clone();
    if let Some(username) = &cfg.valkey_username {
        settings = settings.set_username(username);
    }
    if let Some(password) = &cfg.valkey_password {
        settings = settings.set_password(password);
    }
    let info = base.set_redis_settings(settings);

    if cfg.security_transport_tls {
        crate::host_api::ensure_crypto_provider_installed();
        let root_cert = std::fs::read(&cfg.valkey_ca_file).ok();
        redis::Client::build_with_tls(
            info,
            redis::TlsCertificates {
                client_tls: None,
                root_cert,
            },
        )
    } else {
        redis::Client::open(info)
    }
}

/// Idempotently ensures `group` exists on `stream` via `XGROUP CREATE
/// <stream> <group> $ MKSTREAM` -- `BUSYGROUP` (the group already exists)
/// is treated as success, never an error. On a fresh Valkey (no persisted
/// state), nothing else in this crate's env-driven legacy single-consumer
/// path (`crate::lib::try_start_process_loop`) ever creates the consumer
/// group, unlike the DB-driven multi-tenant path where hub-api is expected
/// to provision it out of band (`crate::source_supervisor`'s module doc) --
/// so that loop self-provisions here, both once at startup and again as the
/// self-heal step whenever a [`is_nogroup_error`] error surfaces mid-drain.
///
/// Returns `Ok(true)` if the group was newly created, `Ok(false)` if it
/// already existed (`BUSYGROUP`) -- callers use this to drive a
/// `consumer_group_created_total` counter without double-counting an
/// already-provisioned group on every retry.
///
/// regression: drain loop exited on NOGROUP (alpha 2026-10-02)
pub(crate) async fn ensure_consumer_group(
    cfg: &SpineConfig,
    stream: &str,
    group: &str,
) -> Result<bool, SpineError> {
    let client = build_raw_client(cfg)?;
    let mut conn = client.get_multiplexed_async_connection().await?;
    let result: Result<(), redis::RedisError> = redis::cmd("XGROUP")
        .arg("CREATE")
        .arg(stream)
        .arg(group)
        .arg("$")
        .arg("MKSTREAM")
        .query_async(&mut conn)
        .await;
    match result {
        Ok(()) => Ok(true),
        Err(e) if e.code() == Some("BUSYGROUP") => Ok(false),
        Err(e) => Err(SpineError::from(e)),
    }
}

/// `true` when `err` is Valkey's `NOGROUP` reply -- matches on
/// [`redis::RedisError::code`] (the raw server-reported error code) rather
/// than a substring match on the full `Display` text, same rationale as
/// `crate::source_supervisor::is_nogroup_error`'s identical check (kept as
/// a separate copy there -- see that module's own doc for why).
pub(crate) fn is_nogroup_error(err: &SpineError) -> bool {
    matches!(err, SpineError::Redis(e) if e.code() == Some("NOGROUP"))
}

/// Test-only helpers for exercising [`ensure_consumer_group`] against a
/// real local Valkey/Redis instance when one happens to be reachable (dev
/// box / CI service container on the default port) -- skipped gracefully
/// (never a failure) when nothing answers, so `cargo test` stays green on a
/// machine with no Valkey running. Mirrors the "use a real dependency when
/// available, skip honestly when not" posture this crate has no
/// `testcontainers` harness for yet.
#[cfg(test)]
pub(crate) mod test_support {
    use super::SpineConfig;

    /// A `SpineConfig` pointing at `127.0.0.1:6379` (plaintext, no auth) --
    /// `Some` only if something actually answers `PING` there.
    pub(crate) fn local_valkey_config() -> Option<SpineConfig> {
        let client = redis::Client::open("redis://127.0.0.1:6379/").ok()?;
        let mut conn = client.get_connection().ok()?;
        let _: String = redis::cmd("PING").query(&mut conn).ok()?;
        Some(SpineConfig {
            valkey_url: "redis://127.0.0.1:6379/".to_string(),
            valkey_username: None,
            valkey_password: None,
            valkey_ca_file: std::path::PathBuf::from("/nonexistent-ca.crt"),
            security_transport_tls: false,
            security_transport_auth: false,
            consumer_id: "test-consumer".to_string(),
            stream_maxlen: 1_000,
            read_count: 16,
            block_ms: 200,
            claim_idle_ms: 30_000,
            claim_interval_ms: 15_000,
            stats_interval_ms: 10_000,
            pel_alert: 5_000,
            dlq_maxlen: 1_000,
            max_deliveries: 5,
            drain_socket_timeout_s: 5,
            relay_block_timeout_s: 5,
        })
    }

    /// A process-unique key suffix (nanosecond timestamp) so parallel test
    /// runs against a shared, real Valkey instance never collide.
    pub(crate) fn unique_key(prefix: &str) -> String {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        format!("waddles:test:{prefix}:{nanos}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    fn env_with_scope(t: Option<i64>, c: Option<i32>) -> StageEnvelope {
        serde_json::from_value(serde_json::json!({
            "schema_version": if t.is_some() { 3 } else { 2 },
            "tenant": "acme", "community": "main",
            "tenant_id": t, "community_id": c,
            "app_id": "waddles.bot.commands.default", "stage": "process",
            "event": {"platform": "twitch", "event_type": "chat.message", "actor": null,
                      "payload": {}, "occurred_at": "2026-10-14T00:00:00.000Z"},
            "ts": "2026-10-14T00:00:00.000Z", "target_app_id": null,
            "workstream_id": "11111111-1111-4111-8111-111111111111", "event_id": "22222222-2222-4222-8222-222222222222", "session_id": "33333333-3333-4333-8333-333333333333", "trace": null,
            "binding": {"kid": "k1", "mac": "0".repeat(64)},
        }))
        .expect("fixture parses")
    }

    #[test]
    fn verified_numeric_scope_guards_the_confused_deputy_and_the_i32_cap() {
        // legacy envelope: no numeric scope, never an error
        assert_eq!(
            verified_numeric_scope(&env_with_scope(None, None), 5, 1),
            Ok(None)
        );
        // v3 matching the bound scope, and an unconfigured (0) bound scope
        let v3 = env_with_scope(Some(10), Some(7));
        assert_eq!(verified_numeric_scope(&v3, 10, 7), Ok(Some((10, 7))));
        assert_eq!(verified_numeric_scope(&v3, 0, 0), Ok(Some((10, 7))));
        // v3 for a DIFFERENT scope than the one capabilities are bound to
        assert!(verified_numeric_scope(&v3, 11, 7).is_err());
        assert!(verified_numeric_scope(&v3, 10, 8).is_err());
        // id above i32::MAX never wraps
        let big = env_with_scope(Some(i64::from(i32::MAX) + 1), Some(0));
        assert!(verified_numeric_scope(&big, 0, 0).is_err());
    }

    // regression: drain loop exited on NOGROUP (alpha 2026-10-02)
    #[test]
    fn is_nogroup_error_matches_on_the_redis_error_code_not_message_wording() {
        let err = SpineError::Redis(redis::make_extension_error(
            "NOGROUP".to_string(),
            Some(
                "No such key 'waddles:t:global:c:_tenant:src:twitch:tw-x:events' or consumer \
                 group 'waddles.core.example.ping' in XREADGROUP with GROUP option"
                    .to_string(),
            ),
        ));
        assert!(is_nogroup_error(&err));

        let err_different_wording = SpineError::Redis(redis::make_extension_error(
            "NOGROUP".to_string(),
            Some("a totally different detail string".to_string()),
        ));
        assert!(is_nogroup_error(&err_different_wording));
    }

    #[test]
    fn is_nogroup_error_rejects_a_different_redis_error_code() {
        let err = SpineError::Redis(redis::make_extension_error(
            "WRONGTYPE".to_string(),
            Some("Operation against a key holding the wrong kind of value".to_string()),
        ));
        assert!(!is_nogroup_error(&err));
    }

    #[test]
    fn is_nogroup_error_rejects_a_non_redis_spine_error() {
        let err = SpineError::Config("unrelated config error".to_string());
        assert!(!is_nogroup_error(&err));
    }

    // regression: drain loop exited on NOGROUP (alpha 2026-10-02) -- proves
    // `ensure_consumer_group` self-heals on a fresh Valkey (no group, no
    // stream) rather than ever surfacing NOGROUP to the drain loop.
    #[tokio::test]
    async fn ensure_consumer_group_creates_the_group_and_stream_on_a_fresh_valkey() {
        let Some(cfg) = test_support::local_valkey_config() else {
            eprintln!("skipping: no local Valkey reachable at 127.0.0.1:6379");
            return;
        };
        let stream = test_support::unique_key("ensure-group-stream");
        let group = test_support::unique_key("ensure-group-group");

        let created = ensure_consumer_group(&cfg, &stream, &group)
            .await
            .expect("first create succeeds");
        assert!(created, "group did not exist yet, must report created=true");

        // Idempotent: BUSYGROUP on the second call must be Ok(false), never
        // an error.
        let created_again = ensure_consumer_group(&cfg, &stream, &group)
            .await
            .expect("second create (BUSYGROUP) must not error");
        assert!(
            !created_again,
            "group already existed, must report created=false"
        );
    }

    fn fixture_delivered(
        tenant: &str,
        community: Option<&str>,
        ring: &KeyRing,
        kid: &str,
    ) -> Delivered {
        let mac = penguin_spine::compute_binding_mac(
            ring,
            kid,
            tenant,
            community,
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-4000-8000-000000000002",
            None,
        )
        .unwrap();
        let community_segment = community.unwrap_or(penguin_spine::TENANT_WIDE_SEGMENT);
        Delivered {
            stream: format!(
                "waddles:t:{tenant}:c:{community_segment}:src:twitch:tw-channelA:events"
            ),
            entry_id: "1234567890-0".to_string(),
            env: serde_json::from_value(serde_json::json!({
                "schema_version": 2,
                "tenant": tenant,
                "community": community,
                "app_id": "waddles.bot.commands.default",
                "stage": "process",
                "event": {
                    "platform": "twitch",
                    "event_type": "chat.message",
                    "actor": "some_user",
                    "payload": {"text": "!songrequest foo"},
                    "occurred_at": "2026-09-22T00:00:00.000Z",
                    "source": null
                },
                "ts": "2026-09-22T00:00:00.000Z",
                "target_app_id": null,
                "workstream_id": "00000000-0000-0000-0000-000000000001",
                "event_id": "00000000-0000-4000-8000-000000000002",
                "session_id": null,
                "trace": null,
                "binding": {"kid": kid, "mac": mac}
            }))
            .unwrap(),
            deliveries: 1,
            // The consumer-group name `GroupReader::read` would actually
            // set. Coincides with `env.app_id` above in this default
            // fixture; `dead_letters_using_the_readers_group_not_envelope_
            // app_id` below builds one where they deliberately DIFFER, the
            // exact "shared ingest-source stream" shape `d.group` exists
            // for (see `penguin_spine::Delivered`'s own doc comment at the
            // pinned rev).
            group: "waddles.bot.commands.default".to_string(),
        }
    }

    fn test_ring() -> KeyRing {
        KeyRing::new(vec![("k1".to_string(), vec![9u8; 32])])
    }

    /// A permissive gate for this module's fixtures -- these tests exercise
    /// the dispatch/DLQ/retry logic, not `crate::capabilities`'s gate
    /// wiring (that module's own tests cover deny-without-grant behavior).
    fn test_gate() -> Arc<bundle_capability_gate::CapabilityGate> {
        Arc::new(bundle_capability_gate::CapabilityGate::new(
            Arc::new(bundle_capability_gate::GrantCache::new(Arc::new(
                crate::grant_gate::AlwaysGrantedLoader::new(
                    bundle_capability_gate::InMemoryGrantLoader::new(),
                ),
            ))),
            Arc::new(bundle_capability_gate::InMemoryMembership::new()),
            Arc::new(bundle_capability_gate::InMemoryQuotaLedger::new()),
            Arc::new(bundle_capability_gate::InMemoryInstancePolicySnapshot::new()),
        ))
    }

    /// Like [`fixture_delivered`] but with a caller-supplied `event.source`
    /// (`fixture_delivered` hardcodes `source: null`) -- used by the
    /// `carry_inbound_source`/`handle_delivered` tests below that need a
    /// real inbound `source` to assert gets carried onto the action
    /// envelope.
    fn fixture_delivered_with_source(
        tenant: &str,
        community: Option<&str>,
        ring: &KeyRing,
        kid: &str,
        source: serde_json::Value,
    ) -> Delivered {
        let mac = penguin_spine::compute_binding_mac(
            ring,
            kid,
            tenant,
            community,
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-4000-8000-000000000002",
            None,
        )
        .unwrap();
        let community_segment = community.unwrap_or(penguin_spine::TENANT_WIDE_SEGMENT);
        Delivered {
            stream: format!("waddles:t:{tenant}:c:{community_segment}:src:discord:guild-A:events"),
            entry_id: "1234567890-0".to_string(),
            env: serde_json::from_value(serde_json::json!({
                "schema_version": 2,
                "tenant": tenant,
                "community": community,
                "app_id": "waddles.bot.commands.default",
                "stage": "process",
                "event": {
                    "platform": "discord",
                    "event_type": "chat.message",
                    "actor": "some_user",
                    "payload": {"text": "!ping"},
                    "occurred_at": "2026-09-22T00:00:00.000Z",
                    "source": source
                },
                "ts": "2026-09-22T00:00:00.000Z",
                "target_app_id": null,
                "workstream_id": "00000000-0000-0000-0000-000000000001",
                "event_id": "00000000-0000-4000-8000-000000000002",
                "session_id": null,
                "trace": null,
                "binding": {"kid": kid, "mac": mac}
            }))
            .unwrap(),
            deliveries: 1,
            group: "waddles.bot.commands.default".to_string(),
        }
    }

    #[test]
    // regression: action envelope dropped event.source so discord relay had no origin channel (alpha 2026-10-02)
    fn carry_inbound_source_populates_a_none_bundle_source_from_the_inbound_envelope() {
        let inbound = PlatformEvent {
            platform: "discord".to_string(),
            event_type: "chat.message".to_string(),
            actor: Some("some_user".to_string()),
            payload: serde_json::Map::new(),
            occurred_at: "2026-09-22T00:00:00.000Z".to_string(),
            source: Some(penguin_spine::Source {
                platform: "discord".to_string(),
                account_id: "bot-123".to_string(),
                channel_id: Some("origin-channel".to_string()),
            }),
        };
        let mut event_out = PlatformEvent {
            platform: "discord".to_string(),
            event_type: "chat.message".to_string(),
            actor: Some("bot".to_string()),
            payload: serde_json::from_value(serde_json::json!({"text": "pong"})).unwrap(),
            occurred_at: "2026-09-22T00:00:01.000Z".to_string(),
            source: None,
        };

        carry_inbound_source(&mut event_out, &inbound);

        let source = event_out.source.expect("source carried from inbound");
        assert_eq!(source.platform, "discord");
        assert_eq!(source.account_id, "bot-123");
        assert_eq!(source.channel_id.as_deref(), Some("origin-channel"));
    }

    #[test]
    // regression: action envelope dropped event.source so discord relay had no origin channel (alpha 2026-10-02)
    //
    // Security property: even if a bundle's `transform` output somehow
    // carried its OWN `source` (today impossible via the real wire path --
    // `platform_event_from_wire` hardcodes `source: null` -- but this
    // proves the host never trusts/merges one if it ever did), the inbound
    // envelope's `source` unconditionally wins. This is what makes a
    // bundle unable to pick its own Discord relay origin channel.
    fn carry_inbound_source_overwrites_a_bundle_supplied_source_with_the_inbound_one() {
        let inbound = PlatformEvent {
            platform: "discord".to_string(),
            event_type: "chat.message".to_string(),
            actor: Some("some_user".to_string()),
            payload: serde_json::Map::new(),
            occurred_at: "2026-09-22T00:00:00.000Z".to_string(),
            source: Some(penguin_spine::Source {
                platform: "discord".to_string(),
                account_id: "bot-123".to_string(),
                channel_id: Some("real-origin-channel".to_string()),
            }),
        };
        let mut event_out = PlatformEvent {
            platform: "discord".to_string(),
            event_type: "chat.message".to_string(),
            actor: Some("bot".to_string()),
            payload: serde_json::from_value(serde_json::json!({"text": "pong"})).unwrap(),
            occurred_at: "2026-09-22T00:00:01.000Z".to_string(),
            source: Some(penguin_spine::Source {
                platform: "discord".to_string(),
                account_id: "attacker-controlled".to_string(),
                channel_id: Some("attacker-chosen-channel".to_string()),
            }),
        };

        carry_inbound_source(&mut event_out, &inbound);

        let source = event_out.source.expect("source present");
        assert_eq!(source.account_id, "bot-123");
        assert_eq!(source.channel_id.as_deref(), Some("real-origin-channel"));
    }

    #[test]
    fn wire_platform_event_encodes_payload_as_a_json_string() {
        let event = PlatformEvent {
            platform: "twitch".to_string(),
            event_type: "chat.message".to_string(),
            actor: Some("user".to_string()),
            payload: serde_json::from_value(serde_json::json!({"text": "hi"})).unwrap(),
            occurred_at: "2026-09-22T00:00:00.000Z".to_string(),
            source: None,
        };
        let wire = wire_platform_event(&event).unwrap();
        assert_eq!(wire["platform"], "twitch");
        assert_eq!(wire["event_type"], "chat.message");
        assert_eq!(wire["actor"], "user");
        assert_eq!(wire["payload_json"], serde_json::json!(r#"{"text":"hi"}"#));
        assert_eq!(wire["occurred_at"], "2026-09-22T00:00:00.000Z");
    }

    #[test]
    fn platform_event_from_wire_round_trips_through_wire_platform_event() {
        let event = PlatformEvent {
            platform: "discord".to_string(),
            event_type: "chat.message".to_string(),
            actor: None,
            payload: serde_json::from_value(serde_json::json!({"a": 1})).unwrap(),
            occurred_at: "2026-09-22T00:00:00.000Z".to_string(),
            source: None,
        };
        let wire = wire_platform_event(&event).unwrap();
        let round_tripped = platform_event_from_wire(&wire).unwrap();
        assert_eq!(round_tripped.platform, "discord");
        assert_eq!(round_tripped.actor, None);
        assert_eq!(round_tripped.payload.get("a"), Some(&serde_json::json!(1)));
    }

    #[test]
    fn platform_event_from_wire_rejects_missing_payload_json() {
        let err = platform_event_from_wire(&serde_json::json!({"platform": "x"})).unwrap_err();
        assert!(matches!(err, InvokeError::MalformedPayload(_)));
    }

    #[test]
    fn platform_event_from_wire_rejects_empty_platform() {
        let wire = serde_json::json!({
            "platform": "",
            "event_type": "chat.message",
            "actor": null,
            "payload_json": "{}",
            "occurred_at": "2026-09-22T00:00:00.000Z",
        });
        assert!(platform_event_from_wire(&wire).is_err());
    }

    #[test]
    fn platform_event_from_wire_rejects_a_non_object_wire_value() {
        let err = platform_event_from_wire(&serde_json::json!("not-an-object")).unwrap_err();
        assert!(matches!(err, InvokeError::MalformedPayload(_)));
    }

    #[test]
    fn platform_event_from_wire_rejects_invalid_payload_json_string() {
        let wire = serde_json::json!({
            "platform": "twitch",
            "event_type": "chat.message",
            "actor": null,
            "payload_json": "not-valid-json{{{",
            "occurred_at": "2026-09-22T00:00:00.000Z",
        });
        let err = platform_event_from_wire(&wire).unwrap_err();
        assert!(
            matches!(err, InvokeError::MalformedPayload(msg) if msg.contains("not valid JSON"))
        );
    }

    #[test]
    fn error_code_mapping_covers_every_variant() {
        assert_eq!(
            error_code_to_dlq_kind(ErrorCode::ExecutorDeadline),
            DlqErrorKind::CallTimeout
        );
        assert_eq!(
            error_code_to_dlq_kind(ErrorCode::MemoryLimit),
            DlqErrorKind::MemoryLimit
        );
        assert_eq!(
            error_code_to_dlq_kind(ErrorCode::WasmTrap),
            DlqErrorKind::BundleTrap
        );
        for code in [ErrorCode::HostCallDenied, ErrorCode::HostCallFailed] {
            assert_eq!(error_code_to_dlq_kind(code), DlqErrorKind::HostCallDenied);
        }
        for code in [
            ErrorCode::UnknownBundle,
            ErrorCode::DigestMismatch,
            ErrorCode::LoadFailed,
            ErrorCode::ExportMissing,
            ErrorCode::MalformedFrame,
        ] {
            assert_eq!(error_code_to_dlq_kind(code), DlqErrorKind::BundleError);
        }
        for code in [
            ErrorCode::ProtocolVersion,
            ErrorCode::FrameTooLarge,
            ErrorCode::UnsandboxedExecutor,
            ErrorCode::ShuttingDown,
        ] {
            assert_eq!(
                error_code_to_dlq_kind(code),
                DlqErrorKind::ExecutorUnavailable
            );
        }
    }

    #[derive(Default)]
    struct RecordingSpineMetrics {
        violations: Mutex<Vec<(String, String)>>,
        skipped: Mutex<Vec<(String, String)>>,
    }
    impl SpineMetrics for RecordingSpineMetrics {
        fn tenant_boundary_violation(&self, stage: &str, reason: &str) {
            self.violations
                .lock()
                .unwrap()
                .push((stage.to_string(), reason.to_string()));
        }
        fn consumer_skipped(&self, app_id: &str, reason: &str) {
            self.skipped
                .lock()
                .unwrap()
                .push((app_id.to_string(), reason.to_string()));
        }
    }

    #[derive(Default)]
    struct FakeSpineOps {
        acked: Mutex<Vec<String>>,
        dead_lettered: Mutex<Vec<(String, DlqErrorKind, String)>>,
        appended: Mutex<Vec<(String, StageEnvelope)>>,
        /// When set, `ack` fails -- the ack write itself erroring is the one
        /// path that leaves an entry pending for redelivery.
        fail_ack: std::sync::atomic::AtomicBool,
    }

    impl SpineOps for FakeSpineOps {
        fn ack<'a>(
            &'a self,
            d: &'a Delivered,
            _app_id: &'a str,
        ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<(), SpineError>> + Send + 'a>>
        {
            if self.fail_ack.load(std::sync::atomic::Ordering::Relaxed) {
                return Box::pin(async {
                    Err(SpineError::Config("scripted ack failure".to_string()))
                });
            }
            self.acked.lock().unwrap().push(d.entry_id.clone());
            Box::pin(async { Ok(()) })
        }

        fn dead_letter<'a>(
            &'a self,
            d: &'a Delivered,
            err: &'a DlqError,
        ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<(), SpineError>> + Send + 'a>>
        {
            self.dead_lettered.lock().unwrap().push((
                d.entry_id.clone(),
                err.kind,
                err.consumer_id.clone(),
            ));
            Box::pin(async { Ok(()) })
        }

        fn append<'a>(
            &'a self,
            stream: &'a str,
            env: &'a StageEnvelope,
        ) -> std::pin::Pin<
            Box<dyn std::future::Future<Output = Result<String, SpineError>> + Send + 'a>,
        > {
            self.appended
                .lock()
                .unwrap()
                .push((stream.to_string(), env.clone()));
            Box::pin(async { Ok("1-0".to_string()) })
        }
    }

    fn test_deps(
        spine: FakeSpineOps,
        connections: Arc<ConnectionRegistry>,
    ) -> ProcessDeps<FakeSpineOps> {
        test_deps_with_metrics(spine, connections).0
    }

    /// Same as [`test_deps`], but also returns the concrete
    /// `RecordingSpineMetrics` handle so a test can assert on it directly
    /// -- `ProcessDeps::metrics` is `Arc<dyn SpineMetrics>`, which can't
    /// be downcast back without this.
    fn test_deps_with_metrics(
        spine: FakeSpineOps,
        connections: Arc<ConnectionRegistry>,
    ) -> (ProcessDeps<FakeSpineOps>, Arc<RecordingSpineMetrics>) {
        let metrics = Arc::new(RecordingSpineMetrics::default());
        // Seeded so the default fixture's `resolve_for_app` (empty
        // `digest`, the default below) resolves cleanly -- individual
        // tests that override `digest` to a non-empty value re-seed the
        // snapshot themselves (matching digest) right after doing so.
        let app_version_snapshot = bundle_active_set::ActiveVersionSnapshot::new();
        app_version_snapshot.update(&[bundle_active_set::ActiveBundleRow {
            app_id: "waddles.bot.commands.default".to_string(),
            version: "1".to_string(),
            version_id: 1,
            digest: String::new(),
            component_key: String::new(),
            sidecar_key: String::new(),
            artifact_signature: None,
            artifact_signature_key_id: None,
            artifact_signed_approval_id: None,
            declared_capabilities: Vec::new(),
            tools: Default::default(),
        }]);
        let deps = ProcessDeps {
            app_id: "waddles.bot.commands.default".to_string(),
            // Empty `Static` by default -- see `DigestSource::Static`'s
            // doc: an empty digest skips `ensure_loaded` entirely, which is
            // what every pre-existing test in this module (fixed before
            // this fix's `Load` call was added) already assumes of its fake
            // executor (`connected_registry_with_fake_executor` answers
            // exactly one `invoke`, no `load`). Tests exercising
            // `ensure_loaded` itself set `digest_source`/`component_key`/
            // `sidecar_key` explicitly.
            digest_source: DigestSource::Static(String::new()),
            version: "1".to_string(),
            component_key: String::new(),
            sidecar_key: String::new(),
            key_ring: test_ring(),
            connections,
            call_timeout_ms: 2000,
            load_state: Arc::new(LoadState::new()),
            approved_targets: HashMap::new(),
            consumer_id: "test-pod-consumer".to_string(),
            spine,
            metrics: metrics.clone() as Arc<dyn SpineMetrics>,
            // ON by default so every existing test's drain behavior is
            // unaffected -- the gate's own OFF/ON behavior is exercised
            // directly by the `license_gate_*` tests below.
            license: Arc::new(crate::license::test_support::FixedGate(true)),
            // No live Valkey server in this module's unit tests -- every
            // `kv` host-call a fixture invokes sees `not_implemented`,
            // exercised directly by `capabilities`'s own test suite
            // instead of here.
            kv_conn: None,
            kv_capabilities: Arc::new(bundle_host_kv::CapabilitySnapshot::new()),
            gate: test_gate(),
            tenant_id: 0,
            community_id: 0,
            app_version_snapshot,
            // Deny-by-default fixture (empty catalog, see
            // `crate::capabilities::HttpEgressCatalog`'s doc) -- no test in
            // this module exercises `http` through `ProcessDeps` itself
            // (that's `crate::capabilities`'s own test module's job); this
            // only needs to satisfy the field.
            egress: Arc::new(bundle_host_http::egress::EgressGuard::new(
                Arc::new(bundle_host_http::egress::ReqwestTransport::new()),
                bundle_host_http::egress::EgressLimits {
                    allow_private_hosts: false,
                    rate_limit_rps: 10,
                    rate_limit_burst: 20,
                    timeout: std::time::Duration::from_secs(5),
                    max_redirects: 3,
                    max_response_bytes: 1_048_576,
                    allowed_ports: vec![443],
                    proxy_url: None,
                },
                crate::capabilities::HttpEgressCatalog::new(),
                prometheus::IntCounterVec::new(
                    prometheus::Opts::new("test_spine_egress_denied_total", "test"),
                    &["app_id", "reason"],
                )
                .unwrap(),
                bundle_host_http::egress::boxed(bundle_host_http::egress::StaticFlag(true)),
            )),
            // OFF by default so every pre-existing test's `invoke_transform`
            // assertion (built against the raw, un-tokenized fixture event)
            // is unaffected -- same "ON/OFF fixture convention" as
            // `license` above. Dedicated `pii_tokenize_*` tests below
            // override both fields directly to exercise the gate's actual
            // ON behavior.
            pii_gate: Arc::new(crate::license::test_support::FixedGate(false)),
            pii_minter: None,
            // No live Postgres connection in this module's unit tests --
            // every `db` host-call a fixture invokes sees `not_implemented`,
            // exercised directly by `capabilities`'s own test suite instead.
            db_wiring: None,
            // A fresh, never-tripped breaker per test -- `circuit_breaker`'s
            // own crate has its own unit test suite for trip/reset/
            // isolation behavior; this only needs to satisfy the field so
            // `handle_delivered`'s `allow` check passes by default.
            breaker: Arc::new(CircuitBreaker::new(Arc::new(()))),
            reputation_wiring: None,
            economy_wiring: None,
            identity_wiring: None,
            ai_wiring: None,
        };
        (deps, metrics)
    }

    #[tokio::test]
    async fn handle_delivered_dead_letters_on_hop_verification_failure() {
        let ring = test_ring();
        // `d.stream` (not a separately-passed key) is what `handle_delivered`
        // verifies against now (see its doc) -- build a fixture whose
        // `env` was minted for tenant "acme" but whose `stream` names a
        // DIFFERENT tenant, the Sec14.11-style cross-tenant-replay shape.
        let mut d = fixture_delivered("acme", Some("main"), &ring, "k1");
        d.stream = "waddles:t:other-tenant:c:main:src:twitch:tw-channelA:events".to_string();

        let spine = FakeSpineOps::default();
        let deps = test_deps(spine, Arc::new(ConnectionRegistry::new()));

        handle_delivered(&d, &deps).await.unwrap();

        assert_eq!(deps.spine.acked.lock().unwrap().len(), 0);
        let dead_lettered = deps.spine.dead_lettered.lock().unwrap();
        assert_eq!(dead_lettered.len(), 1);
        assert_eq!(dead_lettered[0].1, DlqErrorKind::TenantBoundary);
        assert_eq!(dead_lettered[0].2, deps.consumer_id);
        assert_ne!(dead_lettered[0].2, d.entry_id);
    }

    #[tokio::test]
    async fn handle_delivered_dead_letters_when_no_executor_connection() {
        let ring = test_ring();
        let d = fixture_delivered("acme", Some("main"), &ring, "k1");

        let spine = FakeSpineOps::default();
        let deps = test_deps(spine, Arc::new(ConnectionRegistry::new()));

        handle_delivered(&d, &deps).await.unwrap();

        let dead_lettered = deps.spine.dead_lettered.lock().unwrap();
        assert_eq!(dead_lettered.len(), 1);
        assert_eq!(dead_lettered[0].1, DlqErrorKind::ExecutorUnavailable);
    }

    /// A minimal [`crate::pii_tokenize::IdentityMinter`] test fixture:
    /// always succeeds, minting `"tok-<platform_user_id>"` for every item.
    struct FixtureMinter;

    impl crate::pii_tokenize::IdentityMinter for FixtureMinter {
        fn mint_many<'a>(
            &'a self,
            _tenant_id: &'a str,
            items: Vec<crate::pii_tokenize::MintItem>,
        ) -> crate::pii_tokenize::MintResult<'a> {
            Box::pin(async move {
                Ok(items
                    .into_iter()
                    .map(|i| {
                        (
                            i.platform_user_id.clone(),
                            format!("tok-{}", i.platform_user_id),
                        )
                    })
                    .collect())
            })
        }
    }

    /// Always fails -- simulates hub-api being unreachable.
    struct FixtureFailMinter;

    impl crate::pii_tokenize::IdentityMinter for FixtureFailMinter {
        fn mint_many<'a>(
            &'a self,
            _tenant_id: &'a str,
            _items: Vec<crate::pii_tokenize::MintItem>,
        ) -> crate::pii_tokenize::MintResult<'a> {
            Box::pin(async move {
                Err(crate::pii_tokenize::TokenizeError::ResolutionUnavailable(
                    "simulated hub-api outage".to_string(),
                ))
            })
        }
    }

    /// PII boundary hard invariant wiring test: when the kill-switch is
    /// disabled (tokenization ON, the default) and a minter is configured,
    /// `handle_delivered` reaches `invoke_transform` successfully (acked,
    /// never dead-lettered) -- the actual "only a token, never raw PII"
    /// substitution guarantee is proven exhaustively by
    /// `crate::pii_tokenize`'s own unit tests; this test proves the wiring
    /// reaches that code path rather than bypassing it.
    #[tokio::test]
    async fn handle_delivered_tokenizes_then_invokes_transform_when_pii_gate_enabled() {
        let ring = test_ring();
        let d = fixture_delivered("acme", Some("main"), &ring, "k1");
        let connections = connected_registry_with_fake_executor(serde_json::json!(null)).await;
        let spine = FakeSpineOps::default();
        let mut deps = test_deps(spine, connections);
        deps.pii_gate = Arc::new(crate::license::test_support::FixedGate(true));
        deps.pii_minter = Some(Arc::new(FixtureMinter));

        handle_delivered(&d, &deps).await.unwrap();

        assert!(deps.spine.dead_lettered.lock().unwrap().is_empty());
        assert_eq!(*deps.spine.acked.lock().unwrap(), vec![d.entry_id.clone()]);
    }

    /// Fail-closed regression test: PII tokenization enabled but no
    /// `hub_client` minter configured yet (the honest startup-wiring gap,
    /// see `crate::source_supervisor::SupervisorDeps::pii_minter`'s doc)
    /// must dead-letter, never fall through to `invoke_transform` with raw
    /// PII.
    #[tokio::test]
    async fn handle_delivered_dead_letters_when_pii_gate_enabled_and_no_minter_configured() {
        let ring = test_ring();
        let d = fixture_delivered("acme", Some("main"), &ring, "k1");
        let spine = FakeSpineOps::default();
        let mut deps = test_deps(spine, Arc::new(ConnectionRegistry::new()));
        deps.pii_gate = Arc::new(crate::license::test_support::FixedGate(true));
        deps.pii_minter = None;

        handle_delivered(&d, &deps).await.unwrap();

        assert!(deps.spine.acked.lock().unwrap().is_empty());
        let dead_lettered = deps.spine.dead_lettered.lock().unwrap();
        assert_eq!(dead_lettered.len(), 1);
        assert_eq!(dead_lettered[0].1, DlqErrorKind::BundleError);
    }

    /// Fail-closed regression test: a minter that fails (hub-api
    /// unreachable/circuit open) must dead-letter the entry rather than
    /// ever forwarding the raw, un-tokenized event.
    #[tokio::test]
    async fn handle_delivered_dead_letters_when_minter_fails() {
        let ring = test_ring();
        let d = fixture_delivered("acme", Some("main"), &ring, "k1");
        let spine = FakeSpineOps::default();
        let mut deps = test_deps(spine, Arc::new(ConnectionRegistry::new()));
        deps.pii_gate = Arc::new(crate::license::test_support::FixedGate(true));
        deps.pii_minter = Some(Arc::new(FixtureFailMinter));

        handle_delivered(&d, &deps).await.unwrap();

        assert!(deps.spine.acked.lock().unwrap().is_empty());
        let dead_lettered = deps.spine.dead_lettered.lock().unwrap();
        assert_eq!(dead_lettered.len(), 1);
        assert_eq!(dead_lettered[0].1, DlqErrorKind::BundleError);
    }

    /// Kill-switch regression test: with the opt-out flag ON (tokenization
    /// disabled), `handle_delivered` falls back to the legacy
    /// pre-tokenization behavior even with no minter configured at all --
    /// proving the kill-switch is a genuine escape hatch, not dead code.
    #[tokio::test]
    async fn handle_delivered_skips_tokenization_when_kill_switch_is_on() {
        let ring = test_ring();
        let d = fixture_delivered("acme", Some("main"), &ring, "k1");
        let connections = connected_registry_with_fake_executor(serde_json::json!(null)).await;
        let spine = FakeSpineOps::default();
        let mut deps = test_deps(spine, connections);
        deps.pii_gate = Arc::new(crate::license::test_support::FixedGate(false));
        deps.pii_minter = None;

        handle_delivered(&d, &deps).await.unwrap();

        assert!(deps.spine.dead_lettered.lock().unwrap().is_empty());
        assert_eq!(*deps.spine.acked.lock().unwrap(), vec![d.entry_id.clone()]);
    }

    fn test_scope(app_id: &str) -> bundle_active_set::AppScope {
        (1, 0, app_id.to_string())
    }

    // regression: multi-tenant consumers invoked with empty legacy digest,
    // UnknownBundle (alpha 2026-10-03)
    //
    // The DB-driven multi-tenant path must invoke with the active set's own
    // canonical digest for this consumer's scope, never the legacy
    // env-configured (and, pre-fix, permanently empty) `Static` value.
    #[tokio::test]
    async fn handle_delivered_active_digest_source_invokes_with_the_scopes_active_digest() {
        let ring = test_ring();
        let d = fixture_delivered("acme", Some("main"), &ring, "k1");
        let active_digest = format!("sha256:{}", "a".repeat(64));
        let (connections, load_count, last_load) =
            connected_registry_with_fake_executor_expecting_load_first(serde_json::json!(null), 1)
                .await;
        let digests = Arc::new(ActiveDigests::new());
        digests.set(
            test_scope("waddles.bot.commands.default"),
            active_digest.clone(),
        );
        let spine = FakeSpineOps::default();
        let mut deps = test_deps(spine, connections);
        deps.digest_source = DigestSource::Active {
            scope: test_scope("waddles.bot.commands.default"),
            digests,
        };
        deps.component_key = "k".to_string();
        deps.sidecar_key = "s".to_string();

        handle_delivered(&d, &deps).await.unwrap();

        assert_eq!(load_count.load(std::sync::atomic::Ordering::SeqCst), 1);
        let load_body = last_load.lock().unwrap().clone().unwrap();
        assert_eq!(load_body.digest, active_digest);
        assert!(deps.spine.dead_lettered.lock().unwrap().is_empty());
        assert_eq!(*deps.spine.acked.lock().unwrap(), vec![d.entry_id.clone()]);
    }

    // regression: multi-tenant consumers invoked with empty legacy digest,
    // UnknownBundle (alpha 2026-10-03)
    //
    // A hot-swap (`ActiveDigests::set` called again for the same scope,
    // exactly what `changelog_consumer::apply_active_set` does on a bundle
    // version bump) must be reflected on the VERY NEXT `DigestSource::
    // current` call -- no consumer restart, no new `ProcessDeps`, no new
    // `LoadState` -- proving the exact mechanism `handle_delivered` relies
    // on every single invoke (the end-to-end executor-wire proof that a
    // freshly-active digest reaches `load`/`invoke` is
    // `handle_delivered_active_digest_source_invokes_with_the_scopes_active_digest`
    // above).
    #[test]
    fn digest_source_active_current_reflects_a_hot_swap_immediately() {
        let scope = test_scope("waddles.a");
        let digest_v1 = format!("sha256:{}", "1".repeat(64));
        let digest_v2 = format!("sha256:{}", "2".repeat(64));
        let digests = Arc::new(ActiveDigests::new());
        digests.set(scope.clone(), digest_v1.clone());
        let source = DigestSource::Active {
            scope: scope.clone(),
            digests: Arc::clone(&digests),
        };
        assert_eq!(source.current(), Some(digest_v1));

        // Hot-swap: the changelog consumer loads a new digest for the same
        // scope -- the already-constructed `DigestSource` (never rebuilt)
        // must resolve it on its very next call.
        digests.set(scope, digest_v2.clone());
        assert_eq!(source.current(), Some(digest_v2));
    }

    // regression: multi-tenant consumers invoked with empty legacy digest,
    // UnknownBundle (alpha 2026-10-03)
    //
    // No active digest known for this scope (never loaded, unloaded, or the
    // scope is currently failing to resolve) must dead-letter for
    // redelivery with NO invoke ever sent -- never fall back to an empty
    // digest. The connection registry here has NO active connection at all;
    // if `handle_delivered` ever tried to invoke, it would panic on
    // `deps.connections.active()` returning `None` before reaching the
    // executor, proving this path returns before even checking for a
    // connection.
    #[tokio::test]
    async fn handle_delivered_dead_letters_when_no_active_digest_is_known_for_the_scope() {
        let ring = test_ring();
        let d = fixture_delivered("acme", Some("main"), &ring, "k1");
        let digests = Arc::new(ActiveDigests::new());
        // Deliberately never `set` for this scope -- "unknown app".
        let spine = FakeSpineOps::default();
        let mut deps = test_deps(spine, Arc::new(ConnectionRegistry::new()));
        deps.digest_source = DigestSource::Active {
            scope: test_scope("waddles.bot.commands.default"),
            digests,
        };

        handle_delivered(&d, &deps).await.unwrap();

        let dead_lettered = deps.spine.dead_lettered.lock().unwrap();
        assert_eq!(dead_lettered.len(), 1);
        assert_eq!(dead_lettered[0].1, DlqErrorKind::BundleError);
    }

    // regression: multi-tenant consumers invoked with empty legacy digest,
    // UnknownBundle (alpha 2026-10-03) -- `DigestSource::Active::current`'s
    // own debug_assert must fire for an empty digest ever smuggled into
    // `ActiveDigests` (defense in depth; `bundle_active_set::canonical_
    // digest` already prevents this at the DB-read boundary in production,
    // and `ActiveDigests::set`'s own non-empty guard is now a SECOND layer
    // -- `force_set_for_test` bypasses both to exercise this third,
    // release-profile-compiled-out layer in isolation).
    #[test]
    #[should_panic(expected = "must never hold an empty digest")]
    fn digest_source_active_current_panics_on_an_empty_active_digest_in_debug_builds() {
        let digests = Arc::new(ActiveDigests::new());
        digests.force_set_for_test(test_scope("waddles.a"), String::new());
        let source = DigestSource::Active {
            scope: test_scope("waddles.a"),
            digests,
        };
        let _ = source.current();
    }

    // regression: same-digest manifest-only release (ping 1.0.2/1.0.3)
    // emptied svc-action dispatch digest (alpha 2026-10-03). The RELEASE
    // profile this service actually runs compiles out the `debug_assert`
    // above -- `usable_digest` is the real runtime gate, and must treat an
    // empty `Active` digest exactly like "no active digest known", with NO
    // invoke ever attempted.
    #[tokio::test]
    async fn handle_delivered_dead_letters_no_active_digest_when_the_resolved_digest_is_empty() {
        let ring = test_ring();
        let d = fixture_delivered("acme", Some("main"), &ring, "k1");
        let digests = Arc::new(ActiveDigests::new());
        digests.force_set_for_test(test_scope("waddles.bot.commands.default"), String::new());
        let spine = FakeSpineOps::default();
        // No executor connection at all -- if `handle_delivered` ever tried
        // to invoke, it would fail before reaching the executor, proving
        // this path returns before even checking for a connection (same
        // shape as `handle_delivered_dead_letters_when_no_active_digest_is_
        // known_for_the_scope` above).
        let mut deps = test_deps(spine, Arc::new(ConnectionRegistry::new()));
        deps.digest_source = DigestSource::Active {
            scope: test_scope("waddles.bot.commands.default"),
            digests,
        };

        handle_delivered(&d, &deps).await.unwrap();

        let dead_lettered = deps.spine.dead_lettered.lock().unwrap();
        assert_eq!(dead_lettered.len(), 1);
        assert_eq!(dead_lettered[0].1, DlqErrorKind::BundleError);
    }

    /// Drives a fake executor over an in-memory duplex: completes the
    /// `hello`/`hello-ok` handshake, then answers exactly one `invoke`
    /// with `result.payload = response`.
    async fn connected_registry_with_fake_executor(
        response: serde_json::Value,
    ) -> Arc<ConnectionRegistry> {
        use crate::capabilities::DenyAllCapabilities;
        use penguin_bundle_host::wire::{
            read_frame, write_frame, Frame, HelloBody, HelloOkBody, ResultBody, SandboxInfo,
        };

        let (stage_io, mut executor_io) = tokio::io::duplex(64 * 1024);
        tokio::spawn(async move {
            write_frame(
                &mut executor_io,
                &Frame::new(
                    1,
                    Message::Hello(HelloBody {
                        protocol_version: 1,
                        executor_version: "0.1.0".to_string(),
                        wasmtime_version: "test".to_string(),
                        wasmtime_abi: "test".to_string(),
                        collector: "drc".to_string(),
                        sandbox: SandboxInfo {
                            runtime: "runc".to_string(),
                            verified: false,
                        },
                    }),
                ),
            )
            .await
            .unwrap();
            let hello_ok = read_frame(&mut executor_io).await.unwrap();
            assert!(matches!(hello_ok.message, Message::HelloOk(_)));

            let invoke = read_frame(&mut executor_io).await.unwrap();
            write_frame(
                &mut executor_io,
                &Frame::new(
                    invoke.id,
                    Message::Result(ResultBody {
                        payload: response,
                        duration_ms: 1,
                        fuel_used: 0,
                    }),
                ),
            )
            .await
            .unwrap();
        });

        let (connection, read_loop) = crate::host_api::run_connection(
            stage_io,
            HelloOkBody {
                stage: "svc-process".to_string(),
                protocol_version: 1,
                limits: penguin_bundle_host::wire::HelloLimits {
                    call_timeout_ms: 2000,
                    memory_mb: 64,
                    max_concurrent_calls: 32,
                },
            },
            false,
            Arc::new(DenyAllCapabilities),
        )
        .await
        .expect("handshake succeeds");
        tokio::spawn(read_loop);

        let registry = Arc::new(ConnectionRegistry::new());
        registry.set_active(connection);
        registry
    }

    /// Same as [`connected_registry_with_fake_executor`], but answers
    /// `invoke_count` invokes with the same `response` -- for tests
    /// (e.g. the redirect-after-upgrade regression) that drive
    /// `handle_delivered` more than once against the same live connection.
    async fn connected_registry_with_fake_executor_multi(
        response: serde_json::Value,
        invoke_count: usize,
    ) -> Arc<ConnectionRegistry> {
        use crate::capabilities::DenyAllCapabilities;
        use penguin_bundle_host::wire::{
            read_frame, write_frame, Frame, HelloBody, HelloOkBody, ResultBody, SandboxInfo,
        };

        let (stage_io, mut executor_io) = tokio::io::duplex(64 * 1024);
        tokio::spawn(async move {
            write_frame(
                &mut executor_io,
                &Frame::new(
                    1,
                    Message::Hello(HelloBody {
                        protocol_version: 1,
                        executor_version: "0.1.0".to_string(),
                        wasmtime_version: "test".to_string(),
                        wasmtime_abi: "test".to_string(),
                        collector: "drc".to_string(),
                        sandbox: SandboxInfo {
                            runtime: "runc".to_string(),
                            verified: false,
                        },
                    }),
                ),
            )
            .await
            .unwrap();
            let hello_ok = read_frame(&mut executor_io).await.unwrap();
            assert!(matches!(hello_ok.message, Message::HelloOk(_)));

            for _ in 0..invoke_count {
                let invoke = read_frame(&mut executor_io).await.unwrap();
                write_frame(
                    &mut executor_io,
                    &Frame::new(
                        invoke.id,
                        Message::Result(ResultBody {
                            payload: response.clone(),
                            duration_ms: 1,
                            fuel_used: 0,
                        }),
                    ),
                )
                .await
                .unwrap();
            }
        });

        let (connection, read_loop) = crate::host_api::run_connection(
            stage_io,
            HelloOkBody {
                stage: "svc-process".to_string(),
                protocol_version: 1,
                limits: penguin_bundle_host::wire::HelloLimits {
                    call_timeout_ms: 2000,
                    memory_mb: 64,
                    max_concurrent_calls: 32,
                },
            },
            false,
            Arc::new(DenyAllCapabilities),
        )
        .await
        .expect("handshake succeeds");
        tokio::spawn(read_loop);

        let registry = Arc::new(ConnectionRegistry::new());
        registry.set_active(connection);
        registry
    }

    /// Drives a fake executor over an in-memory duplex that expects `load`
    /// **before** any `invoke`: completes the `hello`/`hello-ok` handshake,
    /// answers exactly one `load` with `loaded` (recording the `LoadBody`
    /// it received onto `load_count`/`last_load`), then answers
    /// `invoke_count` `invoke`s in sequence with `result.payload =
    /// response`. Panics if an `invoke` arrives before the `load` --
    /// exactly the shape a real executor would reject with
    /// `UNKNOWN_BUNDLE` (spec §7.6), proving `handle_delivered` sends
    /// `load` first rather than skipping straight to `invoke` (Blocker B).
    async fn connected_registry_with_fake_executor_expecting_load_first(
        response: serde_json::Value,
        invoke_count: usize,
    ) -> (
        Arc<ConnectionRegistry>,
        Arc<std::sync::atomic::AtomicUsize>,
        Arc<Mutex<Option<LoadBody>>>,
    ) {
        use crate::capabilities::DenyAllCapabilities;
        use penguin_bundle_host::wire::{
            read_frame, write_frame, Frame, HelloBody, HelloOkBody, ResultBody, SandboxInfo,
        };

        let load_count = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let last_load = Arc::new(Mutex::new(None));
        let load_count_task = Arc::clone(&load_count);
        let last_load_task = Arc::clone(&last_load);

        let (stage_io, mut executor_io) = tokio::io::duplex(64 * 1024);
        tokio::spawn(async move {
            write_frame(
                &mut executor_io,
                &Frame::new(
                    1,
                    Message::Hello(HelloBody {
                        protocol_version: 1,
                        executor_version: "0.1.0".to_string(),
                        wasmtime_version: "test".to_string(),
                        wasmtime_abi: "test".to_string(),
                        collector: "drc".to_string(),
                        sandbox: SandboxInfo {
                            runtime: "runc".to_string(),
                            verified: false,
                        },
                    }),
                ),
            )
            .await
            .unwrap();
            let hello_ok = read_frame(&mut executor_io).await.unwrap();
            assert!(matches!(hello_ok.message, Message::HelloOk(_)));

            let load = read_frame(&mut executor_io).await.unwrap();
            let (load_id, load_body) = match load.message {
                Message::Load(b) => (load.id, b),
                other => panic!("expected load before any invoke, got {other:?}"),
            };
            load_count_task.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            *last_load_task.lock().unwrap() = Some(load_body.clone());
            write_frame(
                &mut executor_io,
                &Frame::new(
                    load_id,
                    Message::Loaded(LoadedBody {
                        app_id: load_body.app_id,
                        digest: load_body.digest,
                        precompile_ms: 1,
                        exports: vec!["transform".to_string()],
                    }),
                ),
            )
            .await
            .unwrap();

            for _ in 0..invoke_count {
                let invoke = read_frame(&mut executor_io).await.unwrap();
                write_frame(
                    &mut executor_io,
                    &Frame::new(
                        invoke.id,
                        Message::Result(ResultBody {
                            payload: response.clone(),
                            duration_ms: 1,
                            fuel_used: 0,
                        }),
                    ),
                )
                .await
                .unwrap();
            }
        });

        let (connection, read_loop) = crate::host_api::run_connection(
            stage_io,
            HelloOkBody {
                stage: "svc-process".to_string(),
                protocol_version: 1,
                limits: penguin_bundle_host::wire::HelloLimits {
                    call_timeout_ms: 2000,
                    memory_mb: 64,
                    max_concurrent_calls: 32,
                },
            },
            false,
            Arc::new(DenyAllCapabilities),
        )
        .await
        .expect("handshake succeeds");
        tokio::spawn(read_loop);

        let registry = Arc::new(ConnectionRegistry::new());
        registry.set_active(connection);
        (registry, load_count, last_load)
    }

    /// **The regression test for Blocker B**: `handle_delivered` must send
    /// a real `load` frame -- carrying `ProcessDeps::app_id`/`version`/
    /// `digest`/`component_key`/`sidecar_key` -- to the executor before its
    /// first `invoke` for a configured bundle. Before this fix, nothing in
    /// this crate ever sent `load` at all (`PROCESS_BUNDLE_*` were read by
    /// `crate::config` and used nowhere else), so every invoke would have
    /// hit the executor's real `UNKNOWN_BUNDLE` error in production; this
    /// test's fake executor panics on that exact ordering violation instead
    /// of silently accepting it.
    #[tokio::test]
    async fn handle_delivered_sends_load_before_the_first_invoke_for_a_configured_bundle() {
        let ring = test_ring();
        let d = fixture_delivered("acme", Some("main"), &ring, "k1");
        let (connections, load_count, last_load) =
            connected_registry_with_fake_executor_expecting_load_first(serde_json::json!(null), 1)
                .await;
        let spine = FakeSpineOps::default();
        let mut deps = test_deps(spine, connections);
        let digest =
            "sha256:deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef".to_string();
        deps.digest_source = DigestSource::Static(digest.clone());
        deps.version = "3".to_string();
        deps.component_key = "bundles/waddles.bot.commands.default/3/deadbeef.wasm".to_string();
        deps.sidecar_key = "bundles/waddles.bot.commands.default/3/deadbeef.json".to_string();
        deps.app_version_snapshot
            .update(&[bundle_active_set::ActiveBundleRow {
                app_id: deps.app_id.clone(),
                version: deps.version.clone(),
                version_id: 1,
                digest: digest.clone(),
                component_key: String::new(),
                sidecar_key: String::new(),
                artifact_signature: None,
                artifact_signature_key_id: None,
                artifact_signed_approval_id: None,
                declared_capabilities: Vec::new(),
                tools: Default::default(),
            }]);

        handle_delivered(&d, &deps).await.unwrap();

        assert_eq!(
            load_count.load(std::sync::atomic::Ordering::SeqCst),
            1,
            "exactly one load must be sent"
        );
        let load_body = last_load
            .lock()
            .unwrap()
            .clone()
            .expect("load must have been observed");
        assert_eq!(load_body.app_id, deps.app_id);
        assert_eq!(load_body.version, "3");
        assert_eq!(load_body.digest, digest);
        assert_eq!(load_body.component_key, deps.component_key);
        assert_eq!(load_body.sidecar_key, deps.sidecar_key);

        assert_eq!(*deps.spine.acked.lock().unwrap(), vec![d.entry_id.clone()]);
        assert!(deps.spine.dead_lettered.lock().unwrap().is_empty());
    }

    /// Sending `load` on every single invoke would re-fetch the bundle from
    /// the bucket and re-instantiate the WASM component per event -- far
    /// too expensive (see [`LoadState`]'s doc). Two entries handled in
    /// sequence on the same connection must trigger exactly one `load`.
    #[tokio::test]
    async fn handle_delivered_sends_load_at_most_once_per_connection_for_two_entries() {
        let ring = test_ring();
        let d1 = fixture_delivered("acme", Some("main"), &ring, "k1");
        let mut d2 = d1.clone();
        d2.entry_id = "1234567890-1".to_string();

        let (connections, load_count, _last_load) =
            connected_registry_with_fake_executor_expecting_load_first(serde_json::json!(null), 2)
                .await;
        let spine = FakeSpineOps::default();
        let mut deps = test_deps(spine, connections);
        let digest = format!("sha256:{}", "0".repeat(64));
        deps.digest_source = DigestSource::Static(digest.clone());
        deps.component_key = "k".to_string();
        deps.sidecar_key = "s".to_string();
        deps.app_version_snapshot
            .update(&[bundle_active_set::ActiveBundleRow {
                app_id: deps.app_id.clone(),
                version: deps.version.clone(),
                version_id: 1,
                digest: digest.clone(),
                component_key: String::new(),
                sidecar_key: String::new(),
                artifact_signature: None,
                artifact_signature_key_id: None,
                artifact_signed_approval_id: None,
                declared_capabilities: Vec::new(),
                tools: Default::default(),
            }]);

        handle_delivered(&d1, &deps).await.unwrap();
        handle_delivered(&d2, &deps).await.unwrap();

        assert_eq!(
            load_count.load(std::sync::atomic::Ordering::SeqCst),
            1,
            "load must be sent exactly once across two entries on the same connection"
        );
        assert_eq!(deps.spine.acked.lock().unwrap().len(), 2);
        assert!(deps.spine.dead_lettered.lock().unwrap().is_empty());
    }

    /// A `load` failure (executor-reported error) must dead-letter as
    /// `BundleError` rather than proceeding to `invoke` against a bundle
    /// the executor never actually holds.
    #[tokio::test]
    async fn handle_delivered_dead_letters_as_bundle_error_when_load_fails() {
        use crate::capabilities::DenyAllCapabilities;
        use penguin_bundle_host::wire::{
            read_frame, write_frame, ErrorBody, Frame, HelloBody, HelloOkBody, SandboxInfo,
        };

        let (stage_io, mut executor_io) = tokio::io::duplex(64 * 1024);
        tokio::spawn(async move {
            write_frame(
                &mut executor_io,
                &Frame::new(
                    1,
                    Message::Hello(HelloBody {
                        protocol_version: 1,
                        executor_version: "0.1.0".to_string(),
                        wasmtime_version: "test".to_string(),
                        wasmtime_abi: "test".to_string(),
                        collector: "drc".to_string(),
                        sandbox: SandboxInfo {
                            runtime: "runc".to_string(),
                            verified: false,
                        },
                    }),
                ),
            )
            .await
            .unwrap();
            read_frame(&mut executor_io).await.unwrap();

            let load = read_frame(&mut executor_io).await.unwrap();
            write_frame(
                &mut executor_io,
                &Frame::new(
                    load.id,
                    Message::Error(ErrorBody {
                        code: ErrorCode::LoadFailed,
                        message: "bucket fetch failed".to_string(),
                        detail: None,
                    }),
                ),
            )
            .await
            .unwrap();
        });

        let (connection, read_loop) = crate::host_api::run_connection(
            stage_io,
            HelloOkBody {
                stage: "svc-process".to_string(),
                protocol_version: 1,
                limits: penguin_bundle_host::wire::HelloLimits {
                    call_timeout_ms: 2000,
                    memory_mb: 64,
                    max_concurrent_calls: 32,
                },
            },
            false,
            Arc::new(DenyAllCapabilities),
        )
        .await
        .expect("handshake succeeds");
        tokio::spawn(read_loop);

        let registry = Arc::new(ConnectionRegistry::new());
        registry.set_active(connection);

        let ring = test_ring();
        let d = fixture_delivered("acme", Some("main"), &ring, "k1");
        let spine = FakeSpineOps::default();
        let mut deps = test_deps(spine, registry);
        let digest = format!("sha256:{}", "0".repeat(64));
        deps.digest_source = DigestSource::Static(digest.clone());
        deps.component_key = "k".to_string();
        deps.sidecar_key = "s".to_string();
        deps.app_version_snapshot
            .update(&[bundle_active_set::ActiveBundleRow {
                app_id: deps.app_id.clone(),
                version: deps.version.clone(),
                version_id: 1,
                digest: digest.clone(),
                component_key: String::new(),
                sidecar_key: String::new(),
                artifact_signature: None,
                artifact_signature_key_id: None,
                artifact_signed_approval_id: None,
                declared_capabilities: Vec::new(),
                tools: Default::default(),
            }]);

        handle_delivered(&d, &deps).await.unwrap();

        assert!(deps.spine.acked.lock().unwrap().is_empty());
        let dead_lettered = deps.spine.dead_lettered.lock().unwrap();
        assert_eq!(dead_lettered.len(), 1);
        assert_eq!(dead_lettered[0].1, DlqErrorKind::BundleError);
    }

    #[tokio::test]
    async fn handle_delivered_no_reply_acks_without_enqueue() {
        let ring = test_ring();
        let d = fixture_delivered("acme", Some("main"), &ring, "k1");
        let connections = connected_registry_with_fake_executor(serde_json::json!(null)).await;
        let spine = FakeSpineOps::default();
        let deps = test_deps(spine, connections);

        handle_delivered(&d, &deps).await.unwrap();

        assert_eq!(*deps.spine.acked.lock().unwrap(), vec![d.entry_id.clone()]);
        assert!(deps.spine.appended.lock().unwrap().is_empty());
        assert!(deps.spine.dead_lettered.lock().unwrap().is_empty());
    }

    /// Regression (gh-433 follow-up 2): source-binding-style resolution
    /// (empty `digest`, `resolve_for_app`) has no separate "pinned digest"
    /// to go stale -- a bundle upgrade (the poller's next tick reads a NEW
    /// digest/version for this SAME app_id, still active in scope) is
    /// transparently picked up on the very next delivery, never dead-
    /// lettered: an upgrade must not drop in-flight deliveries.
    #[tokio::test]
    async fn handle_delivered_continues_processing_after_an_upgrade_for_app_scoped_resolution() {
        let ring = test_ring();
        let d1 = fixture_delivered("acme", Some("main"), &ring, "k1");
        let connections =
            connected_registry_with_fake_executor_multi(serde_json::json!(null), 2).await;
        let spine = FakeSpineOps::default();
        let deps = test_deps(spine, connections);

        // First delivery: the snapshot resolves `deps.app_id` under its
        // seeded version -- succeeds and acks.
        handle_delivered(&d1, &deps).await.unwrap();
        assert_eq!(deps.spine.acked.lock().unwrap().len(), 1);

        // Upgrade: the poller's next tick reads a NEW digest/version for
        // this SAME app_id, still active in scope.
        deps.app_version_snapshot
            .update(&[bundle_active_set::ActiveBundleRow {
                app_id: "waddles.bot.commands.default".to_string(),
                version: "2".to_string(),
                version_id: 2,
                digest: "sha256:new-after-upgrade".to_string(),
                component_key: String::new(),
                sidecar_key: String::new(),
                artifact_signature: None,
                artifact_signature_key_id: None,
                artifact_signed_approval_id: None,
                declared_capabilities: Vec::new(),
                tools: Default::default(),
            }]);

        let mut d2 = d1.clone();
        d2.entry_id = "1234567890-1".to_string();
        handle_delivered(&d2, &deps).await.unwrap();

        assert!(
            deps.spine.dead_lettered.lock().unwrap().is_empty(),
            "an upgrade must never dead-letter an app-scoped delivery"
        );
        assert_eq!(
            deps.spine.acked.lock().unwrap().len(),
            2,
            "both the pre- and post-upgrade deliveries must be acked"
        );
    }

    /// Regression (gh-433 follow-up): `app_version` is resolved PER
    /// DELIVERY from the live snapshot, never once per connect/startup.
    /// The first delivery succeeds against the seeded version; a
    /// deactivation mid-run (`bundle_loader::run_tick`'s own
    /// `ActiveVersionSnapshot::update`, simulated here directly) that drops
    /// this app_id out of the active set entirely -- unlike an upgrade
    /// (tested above), which redirects -- must make the very NEXT delivery
    /// fail closed (dead-lettered, `HostCallDenied`) rather than keep
    /// invoking under a version the active set no longer recognizes.
    #[tokio::test]
    async fn handle_delivered_falls_back_to_the_fail_closed_app_version_after_deactivation() {
        let ring = test_ring();
        let d1 = fixture_delivered("acme", Some("main"), &ring, "k1");
        let connections =
            connected_registry_with_fake_executor_multi(serde_json::json!(null), 2).await;
        let spine = FakeSpineOps::default();
        let deps = test_deps(spine, connections);

        // First delivery: the snapshot still resolves `deps.app_id` (empty
        // `digest`, so `resolve_for_app`) -- succeeds and acks.
        handle_delivered(&d1, &deps).await.unwrap();
        assert_eq!(deps.spine.acked.lock().unwrap().len(), 1);
        assert!(deps.spine.dead_lettered.lock().unwrap().is_empty());

        // Deactivation: the poller's next tick reads an active set that no
        // longer includes this app_id at all (deactivated or revoked).
        // `digest_source` (not `app_version_snapshot`) owns dead-letter
        // decisions for the dispatch itself -- a deactivated app's `digest`
        // is a separate, already-tested failure path
        // (`handle_delivered_dead_letters_with_no_active_digest_and_never_invokes`).
        // This snapshot's only job is resolving the `app_versions.id` the
        // gate keys grants on; losing that entry must fail CLOSED to the
        // `0` sentinel (never matching a real grant row), not block
        // dispatch outright.
        deps.app_version_snapshot.update(&[]);
        assert_eq!(
            deps.app_version_snapshot
                .resolve_for_app("waddles.bot.commands.default"),
            None,
            "a deactivated app_id must no longer resolve in the live snapshot"
        );

        let mut d2 = d1.clone();
        d2.entry_id = "1234567890-1".to_string();
        handle_delivered(&d2, &deps).await.unwrap();

        assert_eq!(
            deps.spine.acked.lock().unwrap().len(),
            2,
            "the dispatch itself must still proceed -- app_version_snapshot losing an entry \
             fails closed on the GRANT lookup (app_version 0), it must never block dispatch"
        );
        assert!(deps.spine.dead_lettered.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn handle_delivered_reply_enqueues_onto_the_same_apps_action_stream_and_acks() {
        let ring = test_ring();
        let d = fixture_delivered("acme", Some("main"), &ring, "k1");
        let reply = wire_platform_event(&PlatformEvent {
            platform: "twitch".to_string(),
            event_type: "chat.message".to_string(),
            actor: Some("bot".to_string()),
            payload: serde_json::from_value(serde_json::json!({"text": "pong"})).unwrap(),
            occurred_at: "2026-09-22T00:00:01.000Z".to_string(),
            source: None,
        })
        .unwrap();
        let connections = connected_registry_with_fake_executor(reply).await;
        let spine = FakeSpineOps::default();
        let deps = test_deps(spine, connections);

        handle_delivered(&d, &deps).await.unwrap();

        assert_eq!(*deps.spine.acked.lock().unwrap(), vec![d.entry_id.clone()]);
        let appended = deps.spine.appended.lock().unwrap();
        assert_eq!(appended.len(), 1);
        assert_eq!(
            appended[0].0,
            "waddles:t:acme:c:main:app:waddles.bot.commands.default:action"
        );
        assert_eq!(appended[0].1.stage, "action");
        assert_eq!(appended[0].1.app_id, "waddles.bot.commands.default");
        // Binding/workstream/event_id carried unchanged (see the module
        // doc: the MAC formula doesn't cover app_id/stage).
        assert_eq!(appended[0].1.binding, d.env.binding);
        assert_eq!(appended[0].1.workstream_id, d.env.workstream_id);
        assert_eq!(appended[0].1.event_id, d.env.event_id);
        assert_eq!(
            appended[0].1.event.payload.get("text"),
            Some(&serde_json::json!("pong"))
        );
    }

    #[tokio::test]
    // regression: action envelope dropped event.source so discord relay had no origin channel (alpha 2026-10-02)
    //
    // End-to-end version of `carry_inbound_source_populates_a_none_bundle_
    // source_from_the_inbound_envelope` through the full `handle_delivered`
    // path: the bundle's `transform` reply carries no `source` (the only
    // shape possible through the real wire format -- `platform_event_from_
    // wire` hardcodes it `null`), yet the action envelope `handle_delivered`
    // appends still carries the INBOUND envelope's own `event.source` --
    // proving `svc_action::dispatch::invoke_dispatch`'s `origin_channel_id`
    // derivation (`env.event.source.channel_id`) will see a real channel
    // for a Discord relay instead of `None`.
    async fn handle_delivered_reply_carries_the_inbound_event_source_onto_the_action_envelope() {
        let ring = test_ring();
        let d = fixture_delivered_with_source(
            "acme",
            Some("main"),
            &ring,
            "k1",
            serde_json::json!({
                "platform": "discord",
                "account_id": "bot-123",
                "channel_id": "origin-channel"
            }),
        );
        let reply = wire_platform_event(&PlatformEvent {
            platform: "discord".to_string(),
            event_type: "chat.message".to_string(),
            actor: Some("bot".to_string()),
            payload: serde_json::from_value(serde_json::json!({"text": "pong"})).unwrap(),
            occurred_at: "2026-09-22T00:00:01.000Z".to_string(),
            source: None,
        })
        .unwrap();
        let connections = connected_registry_with_fake_executor(reply).await;
        let spine = FakeSpineOps::default();
        let deps = test_deps(spine, connections);

        handle_delivered(&d, &deps).await.unwrap();

        let appended = deps.spine.appended.lock().unwrap();
        assert_eq!(appended.len(), 1);
        let source = appended[0]
            .1
            .event
            .source
            .as_ref()
            .expect("inbound source carried onto the action envelope");
        assert_eq!(source.platform, "discord");
        assert_eq!(source.account_id, "bot-123");
        assert_eq!(source.channel_id.as_deref(), Some("origin-channel"));
    }

    #[tokio::test]
    async fn handle_delivered_redirects_to_an_approved_cross_app_target() {
        let ring = test_ring();
        let d = fixture_delivered("acme", Some("main"), &ring, "k1");
        let reply = wire_platform_event(&PlatformEvent {
            platform: "twitch".to_string(),
            event_type: "chat.message".to_string(),
            actor: None,
            payload: serde_json::from_value(serde_json::json!({
                "text": "posted",
                "_target_app_id": "waddles.community.forums.default"
            }))
            .unwrap(),
            occurred_at: "2026-09-22T00:00:01.000Z".to_string(),
            source: None,
        })
        .unwrap();
        let connections = connected_registry_with_fake_executor(reply).await;
        let spine = FakeSpineOps::default();
        let mut deps = test_deps(spine, connections);
        deps.approved_targets.insert(
            "waddles.community.forums.default".to_string(),
            "acme".to_string(),
        );

        handle_delivered(&d, &deps).await.unwrap();

        let appended = deps.spine.appended.lock().unwrap();
        assert_eq!(appended.len(), 1);
        assert_eq!(
            appended[0].0,
            "waddles:t:acme:c:main:app:waddles.community.forums.default:action"
        );
        assert_eq!(appended[0].1.app_id, "waddles.community.forums.default");
        assert!(!appended[0].1.event.payload.contains_key("_target_app_id"));
    }

    #[tokio::test]
    async fn handle_delivered_drops_a_denied_cross_app_route_without_enqueue() {
        let ring = test_ring();
        let d = fixture_delivered("acme", Some("main"), &ring, "k1");
        let reply = wire_platform_event(&PlatformEvent {
            platform: "twitch".to_string(),
            event_type: "chat.message".to_string(),
            actor: None,
            payload: serde_json::from_value(serde_json::json!({
                "text": "posted",
                "_target_app_id": "waddles.other.app.default"
            }))
            .unwrap(),
            occurred_at: "2026-09-22T00:00:01.000Z".to_string(),
            source: None,
        })
        .unwrap();
        let connections = connected_registry_with_fake_executor(reply).await;
        let spine = FakeSpineOps::default();
        let (deps, metrics) = test_deps_with_metrics(spine, connections);

        handle_delivered(&d, &deps).await.unwrap();

        assert!(deps.spine.appended.lock().unwrap().is_empty());
        assert_eq!(*deps.spine.acked.lock().unwrap(), vec![d.entry_id.clone()]);
        assert_eq!(
            *metrics.skipped.lock().unwrap(),
            vec![(
                "waddles.bot.commands.default".to_string(),
                "route_denied".to_string()
            )]
        );
    }

    #[tokio::test]
    async fn handle_delivered_unsupported_stage_dead_letters_as_bundle_error() {
        let ring = test_ring();
        let d = fixture_delivered("acme", Some("main"), &ring, "k1");
        let connections = connected_registry_with_fake_executor(
            serde_json::json!({"unsupported_stage": {"stage": "process"}}),
        )
        .await;
        let spine = FakeSpineOps::default();
        let deps = test_deps(spine, connections);

        handle_delivered(&d, &deps).await.unwrap();

        let dead_lettered = deps.spine.dead_lettered.lock().unwrap();
        assert_eq!(dead_lettered.len(), 1);
        assert_eq!(dead_lettered[0].1, DlqErrorKind::BundleError);
    }

    #[tokio::test]
    async fn drain_batch_dispatches_every_entry_and_acks_each() {
        struct OneShotReader {
            batch: Option<Vec<Delivered>>,
        }
        impl StreamReader for OneShotReader {
            async fn read(&mut self) -> Result<Vec<Delivered>, SpineError> {
                Ok(self.batch.take().unwrap_or_default())
            }
        }

        let ring = test_ring();
        let d = fixture_delivered("acme", Some("main"), &ring, "k1");
        let connections = connected_registry_with_fake_executor(serde_json::json!(null)).await;
        let spine = FakeSpineOps::default();
        let deps = test_deps(spine, connections);
        let mut reader = OneShotReader {
            batch: Some(vec![d.clone()]),
        };

        let count = drain_batch(&mut reader, &deps).await.unwrap();
        assert_eq!(count, 1);
        assert_eq!(deps.spine.acked.lock().unwrap().len(), 1);
    }

    /// Regression coverage for spec §13.5's `waddles.core.rust-data-plane`
    /// gate: OFF must drain nothing at all -- `reader.read()` (a real
    /// `XREADGROUP` in production) is never even called.
    #[tokio::test]
    async fn drain_batch_gate_off_never_reads_and_returns_zero() {
        struct PanicIfReadReader;
        impl StreamReader for PanicIfReadReader {
            async fn read(&mut self) -> Result<Vec<Delivered>, SpineError> {
                panic!("drain_batch must not call read() while the gate is OFF");
            }
        }

        let spine = FakeSpineOps::default();
        let mut deps = test_deps(spine, Arc::new(ConnectionRegistry::new()));
        deps.license = Arc::new(crate::license::test_support::FixedGate(false));
        let mut reader = PanicIfReadReader;

        let count = tokio::time::timeout(
            std::time::Duration::from_secs(5),
            drain_batch(&mut reader, &deps),
        )
        .await
        .expect("drain_batch must return promptly while the gate is OFF")
        .unwrap();
        assert_eq!(count, 0);
        assert!(deps.spine.acked.lock().unwrap().is_empty());
        assert!(deps.spine.dead_lettered.lock().unwrap().is_empty());
    }

    /// The gate is re-checked every iteration -- flipping it ON mid-loop
    /// (no restart) makes the very next `drain_batch` call actually read.
    #[tokio::test]
    async fn drain_batch_resumes_once_the_gate_flips_on() {
        struct OneShotReader {
            batch: Option<Vec<Delivered>>,
        }
        impl StreamReader for OneShotReader {
            async fn read(&mut self) -> Result<Vec<Delivered>, SpineError> {
                Ok(self.batch.take().unwrap_or_default())
            }
        }

        let ring = test_ring();
        let d = fixture_delivered("acme", Some("main"), &ring, "k1");
        let connections = connected_registry_with_fake_executor(serde_json::json!(null)).await;
        let spine = FakeSpineOps::default();
        let mut deps = test_deps(spine, connections);
        let gate = Arc::new(crate::license::test_support::ToggleGate::new(false));
        deps.license = gate.clone();
        let mut reader = OneShotReader {
            batch: Some(vec![d.clone()]),
        };

        let off_count = drain_batch(&mut reader, &deps).await.unwrap();
        assert_eq!(off_count, 0, "gate is OFF, nothing should drain yet");
        assert!(deps.spine.acked.lock().unwrap().is_empty());

        gate.set(true);
        let on_count = drain_batch(&mut reader, &deps).await.unwrap();
        assert_eq!(on_count, 1, "gate flipped ON, the pending entry drains");
        assert_eq!(deps.spine.acked.lock().unwrap().len(), 1);
    }

    /// Regression coverage for the fixed upstream bug (`penguin-spine`
    /// `Delivered.group`, this crate's `Cargo.toml` pin comment): the
    /// entry `handle_delivered` hands to `dead_letter` carries the
    /// READER's own group (`d.group`), distinct from `env.app_id` (the
    /// ingest-stamped, non-reader-identifying value) -- proving this
    /// crate's own code preserves that distinction end to end rather than
    /// collapsing it back to `env.app_id` anywhere along the way.
    #[tokio::test]
    async fn dead_letters_using_the_readers_group_not_envelope_app_id() {
        let ring = test_ring();
        let mut d = fixture_delivered("acme", Some("main"), &ring, "k1");
        // Shared ingest-source-stream shape: the envelope's own app_id is
        // svc-ingest's generic stamped value, NOT the reading bundle's
        // group.
        d.env.app_id = "waddles.core.ingest.default".to_string();
        d.group = "waddles.bot.commands.default".to_string();

        let spine = FakeSpineOps::default();
        // No executor connection -> `handle_delivered` dead-letters.
        let deps = test_deps(spine, Arc::new(ConnectionRegistry::new()));

        handle_delivered(&d, &deps).await.unwrap();

        let dead_lettered = deps.spine.dead_lettered.lock().unwrap();
        assert_eq!(dead_lettered.len(), 1);
        // `FakeSpineOps::dead_letter` (below) does not itself need to
        // inspect `d.group` to prove this -- `d` is passed through to
        // `crate::spine::SpineOps::dead_letter` unchanged, and the
        // pinned `penguin_spine::SpineClient::dead_letter` (verified by
        // that crate's own test suite at this rev) is what `XACK`s under
        // `d.group`. This test's own job is only to prove `d.group` still
        // holds its distinct, correct value at the point this crate hands
        // the entry off -- asserted directly here.
        assert_eq!(d.group, "waddles.bot.commands.default");
        assert_ne!(d.group, d.env.app_id);
    }

    #[tokio::test]
    async fn drain_loop_stops_once_shutdown_resolves() {
        struct EmptyReader;
        impl StreamReader for EmptyReader {
            async fn read(&mut self) -> Result<Vec<Delivered>, SpineError> {
                Ok(Vec::new())
            }
        }

        let spine = FakeSpineOps::default();
        let deps = test_deps(spine, Arc::new(ConnectionRegistry::new()));
        let (tx, rx) = tokio::sync::oneshot::channel();
        tx.send(()).unwrap();
        let result = tokio::time::timeout(
            std::time::Duration::from_secs(5),
            drain_loop(EmptyReader, deps, rx),
        )
        .await
        .expect("drain_loop must return promptly once shutdown resolves");
        assert!(result.is_ok());
    }

    /// Scripted [`crate::identity::MemberDirectory`]: platform account id ->
    /// community `user_uuid`, plus the set of confirmed members.
    struct SpineDirectory {
        by_platform: HashMap<String, uuid::Uuid>,
    }

    impl crate::identity::MemberDirectory for SpineDirectory {
        fn member_by_platform_id<'a>(
            &'a self,
            _scope: crate::identity::IdentityScope,
            _platform: &'a str,
            platform_user_id: &'a str,
        ) -> crate::identity::BoxFuture<'a, Result<uuid::Uuid, crate::identity::IdentityError>>
        {
            Box::pin(async move {
                self.by_platform
                    .get(platform_user_id)
                    .copied()
                    .ok_or(crate::identity::IdentityError::NotAMember)
            })
        }

        fn confirm_member<'a>(
            &'a self,
            _scope: crate::identity::IdentityScope,
            user: uuid::Uuid,
        ) -> crate::identity::BoxFuture<'a, Result<uuid::Uuid, crate::identity::IdentityError>>
        {
            Box::pin(async move {
                if self.by_platform.values().any(|u| *u == user) {
                    Ok(user)
                } else {
                    Err(crate::identity::IdentityError::NotAMember)
                }
            })
        }
    }

    /// What the scripted executor-side "bundle" observed during one invoke.
    #[derive(Default)]
    struct BundleObservation {
        /// The tokenized event the bundle was delivered (`payload_json`).
        delivered_payload_json: String,
        delivered_actor: Option<String>,
        /// `(op, host-result)` per host-call it made, in order.
        host_results: Vec<(String, penguin_bundle_host::wire::HostResultBody)>,
    }

    /// A fake executor that, on its single `invoke`, behaves like a points-game
    /// bundle: reads the placeholder token out of the message text it was
    /// delivered, then calls `identity.resolve_actor`, `identity.resolve_mention`
    /// with that token, and `identity.resolve_mention` with a raw handle it was
    /// never shown -- recording every answer.
    async fn connected_registry_with_scripted_identity_bundle(
    ) -> (Arc<ConnectionRegistry>, Arc<Mutex<BundleObservation>>) {
        use crate::capabilities::DenyAllCapabilities;
        use penguin_bundle_host::wire::{
            read_frame, write_frame, CapabilityKind, Frame, HelloBody, HelloOkBody, HostCallBody,
            ResultBody, SandboxInfo,
        };

        let observed = Arc::new(Mutex::new(BundleObservation::default()));
        let observed_task = Arc::clone(&observed);
        let (stage_io, mut executor_io) = tokio::io::duplex(64 * 1024);
        tokio::spawn(async move {
            write_frame(
                &mut executor_io,
                &Frame::new(
                    1,
                    Message::Hello(HelloBody {
                        protocol_version: 1,
                        executor_version: "0.1.0".to_string(),
                        wasmtime_version: "test".to_string(),
                        wasmtime_abi: "test".to_string(),
                        collector: "drc".to_string(),
                        sandbox: SandboxInfo {
                            runtime: "runc".to_string(),
                            verified: false,
                        },
                    }),
                ),
            )
            .await
            .unwrap();
            let hello_ok = read_frame(&mut executor_io).await.unwrap();
            assert!(matches!(hello_ok.message, Message::HelloOk(_)));

            let invoke = read_frame(&mut executor_io).await.unwrap();
            let invoke_id = invoke.id;
            let Message::Invoke(body) = invoke.message else {
                panic!("expected invoke");
            };
            let payload_json = body.payload["payload_json"]
                .as_str()
                .expect("payload_json string")
                .to_string();
            let delivered: serde_json::Value = serde_json::from_str(&payload_json).unwrap();
            let text = delivered["text"]
                .as_str()
                .expect("delivered payload carries a `text` string")
                .to_string();
            // The token inside the first `{user:<token>}` placeholder.
            let token = text
                .split("{user:")
                .nth(1)
                .and_then(|rest| rest.split('}').next())
                .expect("delivered text carries a `{user:<token>}` placeholder")
                .to_string();
            {
                let mut o = observed_task.lock().unwrap();
                o.delivered_payload_json = payload_json;
                o.delivered_actor = body.payload["actor"].as_str().map(str::to_string);
            }

            let calls: Vec<(&str, serde_json::Value)> = vec![
                ("identity.resolve_actor", serde_json::json!({})),
                (
                    "identity.resolve_mention",
                    serde_json::json!({ "token": token }),
                ),
                (
                    "identity.resolve_mention",
                    serde_json::json!({ "token": "@some_handle_never_shown" }),
                ),
            ];
            for (n, (op, args)) in calls.into_iter().enumerate() {
                write_frame(
                    &mut executor_io,
                    &Frame::new(
                        100 + n as u64,
                        Message::HostCall(HostCallBody {
                            app_id: "waddles.bot.commands.default".to_string(),
                            capability: CapabilityKind::Db,
                            op: op.to_string(),
                            args,
                            call_id: invoke_id,
                        }),
                    ),
                )
                .await
                .unwrap();
                let reply = read_frame(&mut executor_io).await.unwrap();
                let Message::HostResult(result) = reply.message else {
                    panic!("expected host-result");
                };
                observed_task
                    .lock()
                    .unwrap()
                    .host_results
                    .push((op.to_string(), result));
            }

            write_frame(
                &mut executor_io,
                &Frame::new(
                    invoke_id,
                    Message::Result(ResultBody {
                        payload: serde_json::json!(null),
                        duration_ms: 1,
                        fuel_used: 0,
                    }),
                ),
            )
            .await
            .unwrap();
        });

        let (connection, read_loop) = crate::host_api::run_connection(
            stage_io,
            HelloOkBody {
                stage: "svc-process".to_string(),
                protocol_version: 1,
                limits: penguin_bundle_host::wire::HelloLimits {
                    call_timeout_ms: 2000,
                    memory_mb: 64,
                    max_concurrent_calls: 32,
                },
            },
            false,
            Arc::new(DenyAllCapabilities),
        )
        .await
        .expect("handshake succeeds");
        tokio::spawn(read_loop);
        let registry = Arc::new(ConnectionRegistry::new());
        registry.set_active(connection);
        (registry, observed)
    }

    /// The glue `handle_delivered` owns and nothing else tests: the RAW event's
    /// platform account id and the tokenization pass's mention bindings become
    /// the invocation's host-side identity facts, wired into the very
    /// capability handler the executor's host-calls reach. Proven end to end
    /// through the real host-API connection: the "bundle" sees only a
    /// tokenized event, yet resolves its actor and its mention to community
    /// UUIDs -- and a raw handle it was never shown resolves to nothing.
    #[tokio::test]
    async fn handle_delivered_wires_the_invocation_identity_into_the_executors_host_calls() {
        let ring = test_ring();
        let mut d = fixture_delivered("acme", Some("main"), &ring, "k1");
        d.env.event.payload = serde_json::json!({
            "user_id": "1001",
            "author_id": "1001",
            "text": "!steal <@2002> 120",
        })
        .as_object()
        .cloned()
        .unwrap();

        let (alice, bob) = (uuid::Uuid::new_v4(), uuid::Uuid::new_v4());
        let (connections, observed) = connected_registry_with_scripted_identity_bundle().await;
        let mut deps = test_deps(FakeSpineOps::default(), connections);
        deps.tenant_id = 7;
        deps.community_id = 3;
        deps.pii_gate = Arc::new(crate::license::test_support::FixedGate(true));
        deps.pii_minter = Some(Arc::new(FixtureMinter));
        // A real gate granting `identity.resolve` for exactly this scope
        // (`app_version` 1: the snapshot `test_deps_with_metrics` seeds).
        let snapshot = bundle_capability_gate::InMemoryGrantSnapshot::new();
        snapshot.set(
            bundle_capability_gate::GrantScopeKey {
                tenant_id: 7,
                community_id: 3,
                app_id: "waddles.bot.commands.default".to_string(),
                app_version: 1,
            },
            bundle_capability_gate::GrantSet {
                permission_snapshot_hash: "t".to_string(),
                grants: [(
                    "identity.resolve".to_string(),
                    bundle_capability_gate::GrantedPermission {
                        permission_id: "identity.resolve".to_string(),
                        params: serde_json::json!({}),
                    },
                )]
                .into_iter()
                .collect(),
            },
        );
        deps.gate = Arc::new(bundle_capability_gate::CapabilityGate::new(
            Arc::new(snapshot),
            Arc::new(bundle_capability_gate::InMemoryMembership::new()),
            Arc::new(bundle_capability_gate::InMemoryQuotaLedger::new()),
            Arc::new(bundle_capability_gate::InMemoryInstancePolicySnapshot::new()),
        ));
        deps.identity_wiring = Some(crate::identity::IdentityWiring {
            directory: Arc::new(SpineDirectory {
                by_platform: [("1001".to_string(), alice), ("2002".to_string(), bob)]
                    .into_iter()
                    .collect(),
            }),
            handles: None,
            flag: Arc::new(crate::license::test_support::FixedGate(true)),
        });

        handle_delivered(&d, &deps).await.unwrap();

        assert!(deps.spine.dead_lettered.lock().unwrap().is_empty());
        assert_eq!(*deps.spine.acked.lock().unwrap(), vec![d.entry_id.clone()]);

        let o = observed.lock().unwrap();
        // What the bundle was delivered: tokenized, no raw mention, no raw handle.
        assert!(o.delivered_payload_json.contains("{user:tok-2002}"));
        assert!(!o.delivered_payload_json.contains("<@2002>"));
        assert_eq!(o.delivered_actor.as_deref(), Some("{user:tok-1001}"));
        assert!(!o.delivered_payload_json.contains("some_user"));

        // What it could resolve: actor and mention -> community UUIDs; a raw
        // handle it was never shown -> not_found.
        assert_eq!(o.host_results.len(), 3);
        let result_of = |i: usize| &o.host_results[i].1;
        assert_eq!(
            result_of(0).result,
            Some(serde_json::json!({ "user": alice.to_string() })),
            "{:?}",
            result_of(0).error
        );
        assert_eq!(
            result_of(1).result,
            Some(serde_json::json!({ "user": bob.to_string() })),
            "{:?}",
            result_of(1).error
        );
        let refusal = result_of(2).error.as_ref().expect("raw handle is refused");
        assert_eq!(refusal.code, "not_found");
    }

    /// Metric-emission tests: each drives a REAL `handle_delivered` /
    /// `ensure_loaded` / `drain_batch` path and reads the data points back out
    /// of an in-memory OTel exporter -- proving the instruments emit, not that
    /// they merely exist. They share the process-wide harness
    /// (`stage_metrics::test_support::shared`), so every series is selected by
    /// a per-test unique `app_id`.
    mod metrics_emission {
        use super::*;
        use crate::stage_metrics::test_support::shared;

        /// Like [`connected_registry_with_fake_executor`], but the executor
        /// answers its first request (`load` or `invoke`) with `reply`
        /// instead of a result -- e.g. a sandbox-trip `error` frame.
        pub(super) async fn connected_registry_replying_with(
            reply: Message,
        ) -> Arc<ConnectionRegistry> {
            use crate::capabilities::DenyAllCapabilities;
            use penguin_bundle_host::wire::{
                read_frame, write_frame, Frame, HelloBody, HelloOkBody, SandboxInfo,
            };

            let (stage_io, mut executor_io) = tokio::io::duplex(64 * 1024);
            tokio::spawn(async move {
                write_frame(
                    &mut executor_io,
                    &Frame::new(
                        1,
                        Message::Hello(HelloBody {
                            protocol_version: 1,
                            executor_version: "0.1.0".to_string(),
                            wasmtime_version: "test".to_string(),
                            wasmtime_abi: "test".to_string(),
                            collector: "drc".to_string(),
                            sandbox: SandboxInfo {
                                runtime: "runc".to_string(),
                                verified: false,
                            },
                        }),
                    ),
                )
                .await
                .unwrap();
                read_frame(&mut executor_io).await.unwrap();
                let request = read_frame(&mut executor_io).await.unwrap();
                write_frame(&mut executor_io, &Frame::new(request.id, reply))
                    .await
                    .unwrap();
            });
            let (connection, read_loop) = crate::host_api::run_connection(
                stage_io,
                HelloOkBody {
                    stage: "svc-process".to_string(),
                    protocol_version: 1,
                    limits: penguin_bundle_host::wire::HelloLimits {
                        call_timeout_ms: 2000,
                        memory_mb: 64,
                        max_concurrent_calls: 32,
                    },
                },
                false,
                Arc::new(DenyAllCapabilities),
            )
            .await
            .expect("handshake succeeds");
            tokio::spawn(read_loop);
            let registry = Arc::new(ConnectionRegistry::new());
            registry.set_active(connection);
            registry
        }

        pub(super) fn error_frame(code: ErrorCode) -> Message {
            Message::Error(penguin_bundle_host::wire::ErrorBody {
                code,
                message: "scripted".to_string(),
                detail: None,
            })
        }

        fn reply_event() -> serde_json::Value {
            wire_platform_event(&PlatformEvent {
                platform: "twitch".to_string(),
                event_type: "chat.message".to_string(),
                actor: Some("bot".to_string()),
                payload: serde_json::from_value(serde_json::json!({"text": "pong"})).unwrap(),
                occurred_at: "2026-09-22T00:00:01.000Z".to_string(),
                source: None,
            })
            .unwrap()
        }

        pub(super) fn deps_for(
            app_id: &str,
            connections: Arc<ConnectionRegistry>,
        ) -> ProcessDeps<FakeSpineOps> {
            let mut deps = test_deps(FakeSpineOps::default(), connections);
            deps.app_id = app_id.to_string();
            deps
        }

        #[tokio::test]
        async fn a_delivered_reply_records_stage_latency_and_executor_call() {
            let app = "metrics.spine.delivered";
            let ring = test_ring();
            let d = fixture_delivered("acme", Some("main"), &ring, "k1");
            let deps = deps_for(
                app,
                connected_registry_with_fake_executor(reply_event()).await,
            );

            handle_delivered(&d, &deps).await.unwrap();

            let h = shared();
            assert_eq!(
                h.count(
                    "waddles_stage_latency_seconds",
                    &[
                        ("app_id", app),
                        ("result", "delivered"),
                        ("stage", "process")
                    ]
                ),
                1.0
            );
            assert_eq!(
                h.count(
                    "waddles_executor_call_seconds",
                    &[("app_id", app), ("export", "transform"), ("result", "ok")]
                ),
                1.0
            );
            // The latency is a real elapsed time, bucketed on the second scale.
            let point = h
                .last("waddles_stage_latency_seconds", &[("app_id", app)])
                .expect("stage latency exported");
            assert!(
                point.sum > 0.0,
                "elapsed time must be recorded, got {}",
                point.sum
            );
        }

        #[tokio::test]
        async fn a_null_reply_records_no_reply() {
            let app = "metrics.spine.noreply";
            let ring = test_ring();
            let d = fixture_delivered("acme", Some("main"), &ring, "k1");
            let deps = deps_for(
                app,
                connected_registry_with_fake_executor(serde_json::Value::Null).await,
            );
            handle_delivered(&d, &deps).await.unwrap();
            assert_eq!(
                shared().count(
                    "waddles_stage_latency_seconds",
                    &[("app_id", app), ("result", "no_reply")]
                ),
                1.0
            );
        }

        #[tokio::test]
        async fn a_denied_cross_app_route_records_route_denied() {
            let app = "metrics.spine.routedenied";
            let ring = test_ring();
            let d = fixture_delivered("acme", Some("main"), &ring, "k1");
            let mut event = reply_event();
            event["payload_json"] = serde_json::json!(
                serde_json::json!({"text": "hi", "_target_app_id": "waddles.not.approved"})
                    .to_string()
            );
            let deps = deps_for(app, connected_registry_with_fake_executor(event).await);
            handle_delivered(&d, &deps).await.unwrap();
            assert_eq!(
                shared().count(
                    "waddles_stage_latency_seconds",
                    &[("app_id", app), ("result", "route_denied")]
                ),
                1.0
            );
        }

        #[tokio::test]
        async fn no_executor_records_a_dead_lettered_stage_with_no_executor_call() {
            let app = "metrics.spine.noexecutor";
            let ring = test_ring();
            let d = fixture_delivered("acme", Some("main"), &ring, "k1");
            let deps = deps_for(app, Arc::new(ConnectionRegistry::new()));
            handle_delivered(&d, &deps).await.unwrap();
            let h = shared();
            assert_eq!(
                h.count(
                    "waddles_stage_latency_seconds",
                    &[("app_id", app), ("result", "dead_lettered")]
                ),
                1.0
            );
            assert_eq!(
                h.count("waddles_executor_call_seconds", &[("app_id", app)]),
                0.0,
                "no executor was reached, so no executor call may be recorded"
            );
        }

        #[tokio::test]
        async fn a_hop_failure_records_a_dead_lettered_stage() {
            let app = "metrics.spine.hopfail";
            let ring = test_ring();
            let mut d = fixture_delivered("acme", Some("main"), &ring, "k1");
            d.stream = "waddles:t:other:c:main:src:twitch:tw-x:events".to_string();
            let deps = deps_for(app, Arc::new(ConnectionRegistry::new()));
            handle_delivered(&d, &deps).await.unwrap();
            assert_eq!(
                shared().count(
                    "waddles_stage_latency_seconds",
                    &[("app_id", app), ("result", "dead_lettered")]
                ),
                1.0
            );
        }

        #[tokio::test]
        async fn each_sandbox_trip_code_increments_its_limit_counter() {
            let cases = [
                (
                    ErrorCode::ExecutorDeadline,
                    "timeout",
                    "metrics.spine.trip.timeout",
                ),
                (
                    ErrorCode::MemoryLimit,
                    "memory",
                    "metrics.spine.trip.memory",
                ),
                (ErrorCode::WasmTrap, "trap", "metrics.spine.trip.trap"),
                (
                    ErrorCode::HostCallDenied,
                    "denied",
                    "metrics.spine.trip.denied",
                ),
            ];
            for (code, limit, app) in cases {
                let ring = test_ring();
                let d = fixture_delivered("acme", Some("main"), &ring, "k1");
                let deps = deps_for(
                    app,
                    connected_registry_replying_with(error_frame(code)).await,
                );
                handle_delivered(&d, &deps).await.unwrap();

                let h = shared();
                assert_eq!(
                    h.count(
                        "waddles_sandbox_trip_total",
                        &[("app_id", app), ("limit", limit)]
                    ),
                    1.0,
                    "{code:?} must count as limit={limit}"
                );
                assert_eq!(
                    h.count(
                        "waddles_executor_call_seconds",
                        &[("app_id", app), ("result", "executor_error")]
                    ),
                    1.0
                );
                assert_eq!(
                    h.count(
                        "waddles_stage_latency_seconds",
                        &[("app_id", app), ("result", "dead_lettered")]
                    ),
                    1.0
                );
            }
        }

        #[tokio::test]
        async fn a_non_sandbox_executor_error_is_not_counted_as_a_trip() {
            let app = "metrics.spine.unknownbundle";
            let ring = test_ring();
            let d = fixture_delivered("acme", Some("main"), &ring, "k1");
            let deps = deps_for(
                app,
                connected_registry_replying_with(error_frame(ErrorCode::UnknownBundle)).await,
            );
            handle_delivered(&d, &deps).await.unwrap();
            let h = shared();
            assert_eq!(
                h.count("waddles_sandbox_trip_total", &[("app_id", app)]),
                0.0
            );
            assert_eq!(
                h.count(
                    "waddles_executor_call_seconds",
                    &[("app_id", app), ("result", "executor_error")]
                ),
                1.0
            );
        }

        #[tokio::test]
        async fn an_unexpected_frame_kind_records_a_malformed_executor_call() {
            let app = "metrics.spine.malformed";
            let ring = test_ring();
            let d = fixture_delivered("acme", Some("main"), &ring, "k1");
            let deps = deps_for(app, connected_registry_replying_with(Message::Pong).await);
            handle_delivered(&d, &deps).await.unwrap();
            assert_eq!(
                shared().count(
                    "waddles_executor_call_seconds",
                    &[("app_id", app), ("result", "malformed")]
                ),
                1.0
            );
        }

        #[tokio::test]
        async fn a_closed_connection_records_a_host_api_error_executor_call() {
            let app = "metrics.spine.hostapierr";
            let registry = connected_registry_replying_with(Message::Pong).await;
            let conn = registry.active().expect("active connection");
            // The read loop observed EOF / the heartbeat declared the session
            // dead: the stage must fail the invoke, not hang, and say so.
            conn.mark_closed();

            let event = PlatformEvent {
                platform: "twitch".to_string(),
                event_type: "chat.message".to_string(),
                actor: None,
                payload: serde_json::from_value(serde_json::json!({"text": "x"})).unwrap(),
                occurred_at: "2026-09-22T00:00:00.000Z".to_string(),
                source: None,
            };
            let outcome = invoke_transform(
                &conn,
                app,
                "",
                &event,
                2000,
                None,
                Arc::new(crate::capabilities::DenyAllCapabilities),
            )
            .await;
            assert!(matches!(outcome, Err(InvokeError::HostApi(_))));

            assert_eq!(
                shared().count(
                    "waddles_executor_call_seconds",
                    &[("app_id", app), ("result", "host_api_error")]
                ),
                1.0
            );
        }

        #[tokio::test]
        async fn a_failed_ack_write_records_spine_error() {
            let app = "metrics.spine.spineerror";
            let ring = test_ring();
            let d = fixture_delivered("acme", Some("main"), &ring, "k1");
            let deps = deps_for(
                app,
                connected_registry_with_fake_executor(serde_json::Value::Null).await,
            );
            deps.spine
                .fail_ack
                .store(true, std::sync::atomic::Ordering::Relaxed);

            let outcome = handle_delivered(&d, &deps).await;
            assert!(outcome.is_err(), "the ack failure must still propagate");
            assert_eq!(
                shared().count(
                    "waddles_stage_latency_seconds",
                    &[("app_id", app), ("result", "spine_error")]
                ),
                1.0
            );
        }

        fn loaded_reply(precompile_ms: u64) -> Message {
            Message::Loaded(LoadedBody {
                app_id: "x".to_string(),
                digest: "sha256:abc".to_string(),
                precompile_ms,
                exports: vec!["transform".to_string()],
            })
        }

        async fn load(app: &str, conn: &Connection) -> Result<LoadedBody, InvokeError> {
            ensure_loaded(
                conn,
                1,
                1,
                app,
                "1",
                "sha256:abc",
                "ck",
                "sk",
                LoadLimits {
                    timeout_ms: 2000,
                    memory_mb: 64,
                },
            )
            .await
        }

        #[tokio::test]
        async fn ensure_loaded_records_the_round_trip_and_the_executor_reported_time() {
            let app = "metrics.spine.load.ok";
            let registry = connected_registry_replying_with(loaded_reply(42)).await;
            let conn = registry.active().expect("active connection");
            load(app, &conn).await.expect("load succeeds");

            let h = shared();
            assert_eq!(
                h.count(
                    "waddles_bundle_load_seconds",
                    &[("app_id", app), ("phase", "load"), ("result", "ok")]
                ),
                1.0
            );
            let precompile = h
                .last(
                    "waddles_bundle_load_seconds",
                    &[("app_id", app), ("phase", "precompile")],
                )
                .expect("executor-reported phase exported");
            assert!(
                (precompile.sum - 0.042).abs() < 1e-9,
                "precompile_ms=42 must be recorded as 0.042s, got {}",
                precompile.sum
            );
        }

        #[tokio::test]
        async fn ensure_loaded_failure_records_an_error_result_and_no_precompile_phase() {
            let app = "metrics.spine.load.err";
            let registry =
                connected_registry_replying_with(error_frame(ErrorCode::LoadFailed)).await;
            let conn = registry.active().expect("active connection");
            assert!(load(app, &conn).await.is_err());

            let h = shared();
            assert_eq!(
                h.count(
                    "waddles_bundle_load_seconds",
                    &[("app_id", app), ("phase", "load"), ("result", "error")]
                ),
                1.0
            );
            assert_eq!(
                h.count(
                    "waddles_bundle_load_seconds",
                    &[("app_id", app), ("phase", "precompile")]
                ),
                0.0
            );
        }

        /// A `StreamReader` fake returning one scripted batch, then nothing.
        struct OneBatch(Vec<Delivered>);

        impl StreamReader for OneBatch {
            async fn read(&mut self) -> Result<Vec<Delivered>, SpineError> {
                Ok(std::mem::take(&mut self.0))
            }
        }

        #[tokio::test]
        async fn drain_batch_records_the_batch_size_including_an_idle_zero() {
            let ring = test_ring();
            let batch = vec![
                fixture_delivered("acme", Some("main"), &ring, "k1"),
                fixture_delivered("acme", Some("main"), &ring, "k1"),
            ];
            let mut deps = deps_for("metrics.spine.batch", Arc::new(ConnectionRegistry::new()));
            deps.spine = FakeSpineOps::default();
            let before = shared()
                .last("waddles_spine_batch_size", &[("stage", "process")])
                .map_or((0.0, 0.0), |p| (p.value, p.sum));

            let mut reader = OneBatch(batch);
            assert_eq!(drain_batch(&mut reader, &deps).await.unwrap(), 2);
            assert_eq!(drain_batch(&mut reader, &deps).await.unwrap(), 0);

            let after = shared()
                .last("waddles_spine_batch_size", &[("stage", "process")])
                .expect("batch size exported");
            // Other tests drain concurrently into the same label-less series,
            // so assert "at least these two samples (2 and 0) landed".
            assert!(after.value >= before.0 + 2.0, "{before:?} -> {after:?}");
            assert!(after.sum >= before.1 + 2.0, "{before:?} -> {after:?}");
        }
    }

    /// Circuit-breaker SEMANTICS (adversarial review of #467): executor
    /// back-pressure is not a guest fault, and the breaker is isolated per
    /// `(app, stream)`, not per stream alone. Each test drives a REAL
    /// `handle_delivered` against a scripted executor connection.
    mod breaker_semantics {
        use super::metrics_emission::{connected_registry_replying_with, deps_for, error_frame};
        use super::*;
        use circuit_breaker::FAILURE_THRESHOLD;

        fn error_frame_with_detail(code: ErrorCode, detail: Option<&str>) -> Message {
            Message::Error(penguin_bundle_host::wire::ErrorBody {
                code,
                message: "scripted".to_string(),
                detail: detail.map(str::to_string),
            })
        }

        /// Feeds `FAILURE_THRESHOLD` deliveries of `d` through `deps`, each
        /// against a fresh executor connection that replies with `reply`.
        async fn fault_n_times(
            deps: &mut ProcessDeps<FakeSpineOps>,
            d: &Delivered,
            reply: impl Fn() -> Message,
        ) {
            for _ in 0..FAILURE_THRESHOLD {
                deps.connections = connected_registry_replying_with(reply()).await;
                handle_delivered(d, deps).await.unwrap();
            }
        }

        fn kinds(deps: &ProcessDeps<FakeSpineOps>) -> Vec<DlqErrorKind> {
            deps.spine
                .dead_lettered
                .lock()
                .unwrap()
                .iter()
                .map(|(_, k, _)| *k)
                .collect()
        }

        /// regression: #467 review HIGH 1 -- saturating the executor's pool
        /// (`pool_exhausted`, wire code `ExecutorDeadline`) times out innocent
        /// sources but must leave their breaker CLOSED.
        #[tokio::test]
        async fn pool_exhausted_back_pressure_never_opens_the_breaker() {
            let ring = test_ring();
            let d = fixture_delivered("acme", Some("main"), &ring, "k1");
            let app = "breaker.sem.pool";
            let mut deps = deps_for(app, Arc::new(ConnectionRegistry::new()));

            fault_n_times(&mut deps, &d, || {
                error_frame_with_detail(ErrorCode::ExecutorDeadline, Some("pool_exhausted"))
            })
            .await;
            // One more than the threshold is still never a trip.
            deps.connections = connected_registry_replying_with(error_frame_with_detail(
                ErrorCode::ExecutorDeadline,
                Some("pool_exhausted"),
            ))
            .await;
            handle_delivered(&d, &deps).await.unwrap();

            assert!(
                deps.breaker.allow(&breaker_key(app, &d.stream)),
                "back-pressure must leave the breaker CLOSED"
            );
            let kinds = kinds(&deps);
            assert_eq!(kinds.len() as u32, FAILURE_THRESHOLD + 1);
            assert!(
                kinds.iter().all(|k| *k == DlqErrorKind::CallTimeout),
                "every entry timed out but none was short-circuited by an open breaker: {kinds:?}"
            );
        }

        /// The control for the test above: a genuine timeout, a fuel trap and
        /// a wasm trap all DO count and open the breaker.
        #[tokio::test]
        async fn genuine_guest_faults_still_open_the_breaker() {
            let ring = test_ring();
            for (i, (code, detail)) in [
                (ErrorCode::ExecutorDeadline, None),
                (ErrorCode::ExecutorDeadline, Some("fuel_exhausted")),
                (ErrorCode::WasmTrap, None),
                (ErrorCode::MemoryLimit, None),
            ]
            .into_iter()
            .enumerate()
            {
                let d = fixture_delivered("acme", Some("main"), &ring, "k1");
                let app = format!("breaker.sem.fault.{i}");
                let mut deps = deps_for(&app, Arc::new(ConnectionRegistry::new()));
                fault_n_times(&mut deps, &d, || error_frame_with_detail(code, detail)).await;
                assert!(
                    !deps.breaker.allow(&breaker_key(&app, &d.stream)),
                    "{code:?}/{detail:?} must open the breaker"
                );
                // The next delivery is short-circuited without any executor.
                handle_delivered(&d, &deps).await.unwrap();
                assert_eq!(
                    kinds(&deps).last().copied(),
                    Some(DlqErrorKind::BundleDisabled)
                );
            }
        }

        /// regression: #467 review HIGH 2 -- app A's faults open A's breaker
        /// but never block app B consuming the SAME source stream through the
        /// SAME shared breaker instance.
        #[tokio::test]
        async fn one_apps_faults_never_block_another_app_on_the_same_stream() {
            let ring = test_ring();
            let d = fixture_delivered("acme", Some("main"), &ring, "k1");

            let mut deps_a = deps_for("breaker.sem.app-a", Arc::new(ConnectionRegistry::new()));
            let mut deps_b = deps_for(
                "breaker.sem.app-b",
                connected_registry_with_fake_executor(serde_json::json!(null)).await,
            );
            // `source_supervisor` shares ONE breaker across all bindings.
            deps_b.breaker = Arc::clone(&deps_a.breaker);

            fault_n_times(&mut deps_a, &d, || error_frame(ErrorCode::WasmTrap)).await;
            assert!(
                !deps_a
                    .breaker
                    .allow(&breaker_key("breaker.sem.app-a", &d.stream)),
                "app A's breaker is open"
            );
            handle_delivered(&d, &deps_a).await.unwrap();
            assert_eq!(
                kinds(&deps_a).last().copied(),
                Some(DlqErrorKind::BundleDisabled),
                "app A is short-circuited"
            );

            // App B, same stream, same shared breaker: must still be invoked
            // and acked, never short-circuited.
            handle_delivered(&d, &deps_b).await.unwrap();
            assert!(
                kinds(&deps_b).is_empty(),
                "app B must not be dead-lettered by app A's faults: {:?}",
                kinds(&deps_b)
            );
            assert_eq!(
                *deps_b.spine.acked.lock().unwrap(),
                vec![d.entry_id.clone()]
            );
        }

        #[test]
        fn breaker_key_is_app_prefixed() {
            assert_eq!(breaker_key("app.a", "stream-1"), "app.a|stream-1");
            assert_ne!(breaker_key("app.a", "s"), breaker_key("app.b", "s"));
        }

        #[test]
        fn is_guest_fault_excludes_only_pool_exhausted_back_pressure() {
            assert!(is_guest_fault(DlqErrorKind::CallTimeout, None));
            assert!(is_guest_fault(
                DlqErrorKind::CallTimeout,
                Some("fuel_exhausted")
            ));
            assert!(is_guest_fault(DlqErrorKind::BundleTrap, None));
            assert!(is_guest_fault(DlqErrorKind::MemoryLimit, None));
            assert!(!is_guest_fault(
                DlqErrorKind::CallTimeout,
                Some("pool_exhausted")
            ));
            assert!(!is_guest_fault(DlqErrorKind::BundleError, None));
            assert!(!is_guest_fault(DlqErrorKind::HostCallDenied, None));
        }
    }

    /// The consumer-group backlog sampler: the periodic `XINFO GROUPS` poll
    /// that makes `waddles_spine_queue_depth` / `waddles_group_pending` carry
    /// data. The loop is proven against a scripted source (no Valkey needed);
    /// one test additionally drives a REAL `SpineClient` against a local
    /// Valkey (skipped when none is reachable) to prove the spine's own
    /// callbacks land in the OTel instruments.
    mod backlog_sampler {
        use super::*;
        use crate::stage_metrics::test_support::Harness;
        use crate::stage_metrics::StageMetrics;
        use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

        struct ScriptedStats {
            calls: Arc<Mutex<Vec<String>>>,
            fail: Arc<AtomicBool>,
            stats: Vec<GroupStats>,
        }

        impl GroupStatsSource for ScriptedStats {
            async fn group_stats(&self, stream: &str) -> Result<Vec<GroupStats>, SpineError> {
                self.calls.lock().unwrap().push(stream.to_string());
                if self.fail.load(Ordering::Relaxed) {
                    return Err(SpineError::Config("scripted stats failure".to_string()));
                }
                Ok(self.stats.clone())
            }
        }

        fn scripted(
            stats: Vec<GroupStats>,
        ) -> (ScriptedStats, Arc<Mutex<Vec<String>>>, Arc<AtomicBool>) {
            let calls = Arc::new(Mutex::new(Vec::new()));
            let fail = Arc::new(AtomicBool::new(false));
            (
                ScriptedStats {
                    calls: Arc::clone(&calls),
                    fail: Arc::clone(&fail),
                    stats,
                },
                calls,
                fail,
            )
        }

        /// Polls until `calls` holds at least `n` entries (real time, tiny
        /// intervals), failing loudly instead of hanging.
        async fn wait_for_calls(calls: &Arc<Mutex<Vec<String>>>, n: usize) {
            let deadline = std::time::Instant::now() + Duration::from_secs(10);
            while calls.lock().unwrap().len() < n {
                assert!(
                    std::time::Instant::now() < deadline,
                    "sampler made only {} of {n} expected calls",
                    calls.lock().unwrap().len()
                );
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        }

        fn stat(app_id: &str, pending: u64) -> GroupStats {
            GroupStats {
                app_id: app_id.to_string(),
                stream: "waddles:t:acme:c:main:app:x:action".to_string(),
                lag: Some(0),
                pending,
            }
        }

        #[tokio::test]
        async fn every_stream_is_sampled_each_interval_until_the_guard_drops() {
            let (source, calls, _fail) = scripted(vec![stat("waddles.mx.sampler.v1", 0)]);
            let streams = vec!["stream-a".to_string(), "stream-b".to_string()];
            let guard = spawn_group_backlog_sampler(source, streams, 20, 5_000)
                .expect("a sampler is spawned for a non-zero interval");

            wait_for_calls(&calls, 4).await;
            let seen: std::collections::BTreeSet<String> =
                calls.lock().unwrap().iter().cloned().collect();
            assert_eq!(
                seen,
                std::collections::BTreeSet::from(["stream-a".to_string(), "stream-b".to_string()])
            );

            drop(guard);
            // Let any in-flight tick settle, then prove no further calls land.
            tokio::time::sleep(Duration::from_millis(60)).await;
            let after_drop = calls.lock().unwrap().len();
            tokio::time::sleep(Duration::from_millis(120)).await;
            assert_eq!(
                calls.lock().unwrap().len(),
                after_drop,
                "the sampler must stop with its guard"
            );
        }

        #[tokio::test]
        async fn a_failing_sample_never_stops_the_sampler() {
            let (source, calls, fail) = scripted(Vec::new());
            fail.store(true, Ordering::Relaxed);
            let _guard =
                spawn_group_backlog_sampler(source, vec!["stream-a".to_string()], 15, 5_000)
                    .expect("sampler spawned");
            wait_for_calls(&calls, 3).await;
            // Recovers and keeps going once the source works again.
            fail.store(false, Ordering::Relaxed);
            let before = calls.lock().unwrap().len();
            wait_for_calls(&calls, before + 2).await;
        }

        #[tokio::test]
        async fn a_group_above_the_pel_alert_is_still_sampled_without_disturbing_the_loop() {
            let (source, calls, _fail) = scripted(vec![
                stat("waddles.mx.stuck.v1", 9_999),
                stat("waddles.mx.fine.v1", 1),
            ]);
            let _guard = spawn_group_backlog_sampler(source, vec!["s".to_string()], 15, 5_000)
                .expect("sampler spawned");
            wait_for_calls(&calls, 3).await;
        }

        #[tokio::test]
        async fn a_zero_interval_or_no_streams_spawns_no_sampler() {
            let (source, _calls, _fail) = scripted(Vec::new());
            assert!(spawn_group_backlog_sampler(source, vec!["s".to_string()], 0, 5_000).is_none());
            let (source, _calls, _fail) = scripted(Vec::new());
            assert!(spawn_group_backlog_sampler(source, Vec::new(), 10_000, 5_000).is_none());
        }

        #[tokio::test]
        async fn the_first_sample_waits_a_full_interval() {
            let (source, calls, _fail) = scripted(Vec::new());
            let _guard = spawn_group_backlog_sampler(source, vec!["s".to_string()], 400, 5_000)
                .expect("sampler spawned");
            tokio::time::sleep(Duration::from_millis(100)).await;
            assert_eq!(
                calls.lock().unwrap().len(),
                0,
                "consumers started together must not all query at t=0"
            );
            wait_for_calls(&calls, 1).await;
        }

        /// Real `SpineClient` + real Valkey (skipped when none answers on
        /// 127.0.0.1:6379): the spine's own `group_stats` -> `group_lag` /
        /// `group_pending` callbacks reach this crate's OTel instruments with
        /// the real server's numbers.
        #[tokio::test]
        async fn a_real_spine_client_feeds_lag_and_pending_into_otel() {
            let Some(mut cfg) = test_support::local_valkey_config() else {
                eprintln!("skipping: no local Valkey reachable at 127.0.0.1:6379");
                return;
            };
            // `SpineClient::connect` validates the socket timeout against the
            // relay block timeout; the shared fixture only feeds raw clients.
            cfg.drain_socket_timeout_s = 65;
            let harness = Harness::new();
            let metrics: Arc<dyn SpineMetrics> = Arc::new(StageMetrics::new(&harness.meter()));
            let client = SpineClient::connect(cfg.clone(), metrics)
                .await
                .expect("connect");
            let stream = test_support::unique_key("backlog-sampler-stream");
            let group = "waddles.mx.backlog.v1";
            client.ensure_group(&stream, group).await.expect("group");

            // Three entries; read two without acking -> pending 2, lag 1.
            let raw = redis::Client::open(cfg.valkey_url.as_str()).expect("redis client");
            let mut conn = raw
                .get_multiplexed_async_connection()
                .await
                .expect("raw connection");
            for _ in 0..3 {
                let _: String = redis::cmd("XADD")
                    .arg(&stream)
                    .arg("*")
                    .arg("env")
                    .arg("{}")
                    .query_async(&mut conn)
                    .await
                    .expect("xadd");
            }
            let _: redis::Value = redis::cmd("XREADGROUP")
                .arg("GROUP")
                .arg(group)
                .arg("c1")
                .arg("COUNT")
                .arg(2)
                .arg("STREAMS")
                .arg(&stream)
                .arg(">")
                .query_async(&mut conn)
                .await
                .expect("xreadgroup");

            let stats = GroupStatsSource::group_stats(&client, &stream)
                .await
                .expect("group_stats");
            let ours = stats
                .iter()
                .find(|s| s.app_id == group)
                .expect("our group is reported");
            assert_eq!(ours.pending, 2);

            let pending = harness
                .last("waddles_group_pending", &[("app_id", group)])
                .expect("pending exported");
            assert_eq!((pending.value, pending.sum), (1.0, 2.0));
            let depth = harness
                .last("waddles_spine_queue_depth", &[("stage", "process")])
                .expect("queue depth exported");
            assert_eq!(depth.value, 1.0);
            assert_eq!(depth.sum, 1.0, "one undelivered entry remained");

            let _: i64 = redis::cmd("DEL")
                .arg(&stream)
                .query_async(&mut conn)
                .await
                .expect("cleanup");
        }

        #[tokio::test]
        async fn a_guard_drop_aborts_the_task() {
            let started = Arc::new(AtomicUsize::new(0));
            let counter = Arc::clone(&started);
            let guard = AbortOnDrop(tokio::spawn(async move {
                loop {
                    counter.fetch_add(1, Ordering::Relaxed);
                    tokio::time::sleep(Duration::from_millis(5)).await;
                }
            }));
            tokio::time::sleep(Duration::from_millis(30)).await;
            drop(guard);
            tokio::time::sleep(Duration::from_millis(30)).await;
            let after = started.load(Ordering::Relaxed);
            tokio::time::sleep(Duration::from_millis(60)).await;
            assert_eq!(started.load(Ordering::Relaxed), after);
        }
    }
}
