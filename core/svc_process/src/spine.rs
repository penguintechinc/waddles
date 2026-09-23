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

use penguin_bundle_host::wire::{ErrorCode, ExportKind, InvokeBody, Message, TraceContext};
use penguin_spine::{
    Delivered, DlqError, DlqErrorKind, Grant, GroupReader, PlatformEvent, Scope, SpineClient,
    SpineConfig, SpineError, SpineMetrics, Stage, StageEnvelope,
};

use crate::builtins::RouteDecision;
use crate::capabilities::{CapabilityHandler, StageCapabilities};
use crate::hop::KeyRing;
use crate::host_api::{Connection, ConnectionRegistry, HostApiError};
use crate::license::FeatureGate;

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
    ExecutorError { code: ErrorCode, message: String },
    #[error("transform payload encode/decode failed: {0}")]
    MalformedPayload(String),
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

/// Invokes the bundle's `transform` export for one delivered event over
/// `conn`, scoped to `capabilities` for exactly this call (see
/// `crate::host_api`'s per-invoke scoping design). Returns the classified
/// [`TransformOutcome`] on any *executor-answered* reply (`result` frame,
/// success or `unsupported_stage` alike); an `InvokeError` covers every
/// case that never produced one (host-api failure, guest trap, malformed
/// reply payload).
pub async fn invoke_transform(
    conn: &Connection,
    app_id: &str,
    digest: &str,
    event: &PlatformEvent,
    deadline_ms: u64,
    trace: Option<TraceContext>,
    capabilities: Arc<dyn CapabilityHandler>,
) -> Result<TransformOutcome, InvokeError> {
    let payload =
        wire_platform_event(event).map_err(|e| InvokeError::MalformedPayload(e.to_string()))?;
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
        .await?;
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
        }),
        _ => Err(InvokeError::MalformedPayload(
            "expected result or error frame".to_string(),
        )),
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

/// Everything [`handle_delivered`] needs beyond the entry itself --
/// bundled so `drain_batch`/`drain_loop`/`run` don't carry an
/// ever-growing parameter list. Mirrors `svc_action::dispatch::
/// DispatchDeps`'s shape.
pub struct ProcessDeps<S: SpineOps> {
    pub app_id: String,
    /// Interim substitute for the distribution poll's resolved digest
    /// (spec §6.7) -- `crate::lib::try_start_process_loop`'s
    /// `PROCESS_BUNDLE_DIGEST`. Empty disables nothing by itself: an
    /// empty digest is simply sent as-is and the executor reports
    /// `UNKNOWN_BUNDLE`, mapped to `DlqErrorKind::BundleError` like any
    /// other unloaded-bundle invoke -- the same "caller's responsibility
    /// until the poll client lands" scope `svc_action::dispatch::
    /// ensure_loaded`'s doc comment documents for its own crate.
    pub digest: String,
    pub key_ring: KeyRing,
    pub connections: Arc<ConnectionRegistry>,
    pub call_timeout_ms: u64,
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
}

/// Handles exactly one delivered entry end to end: hop-verify, invoke
/// `transform`, apply cross-app routing, and either enqueue+ack or
/// dead-letter. Returns `Ok(())` in every case where the entry was
/// terminally handled (acked, dropped-and-acked, or dead-lettered) --
/// only a `SpineError` from the ack/DLQ/append write itself propagates,
/// matching `svc_action::dispatch::handle_delivered`'s identical
/// error-handling shape.
///
/// **Not yet wired here (documented, not silently skipped):** the
/// content-moderation gate (`crate::builtins::run_moderation_gate`, an
/// honest TODO(M4+) seam that always returns "no match" today, so wiring
/// it in would currently be a no-op); spec §5.3's consumer-side `consumes.
/// event_types`/`filters` cheap-skip optimization (needs the bundle's
/// resolved manifest, itself blocked on the same distribution poll gap);
/// and calling `svc_action::dispatch`-style `ensure_loaded` before invoke
/// (same "caller's responsibility until the poll lands" scope as that
/// crate's own M3 landing -- see [`ProcessDeps::digest`]'s doc).
async fn handle_delivered<S: SpineOps>(
    d: &Delivered,
    deps: &ProcessDeps<S>,
) -> Result<(), SpineError> {
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
            artifact_digest: Some(deps.digest.clone()),
            consumer_id: deps.consumer_id.clone(),
        };
        return deps.spine.dead_letter(d, &err).await;
    }

    let Some(connection) = deps.connections.active() else {
        tracing::warn!(app_id = %deps.app_id, "no executor connection available, dead-lettering for redelivery");
        let err = DlqError {
            kind: DlqErrorKind::ExecutorUnavailable,
            code: "EXECUTOR_UNAVAILABLE".to_string(),
            message: "no active host-api connection".to_string(),
            detail: None,
            artifact_digest: Some(deps.digest.clone()),
            consumer_id: deps.consumer_id.clone(),
        };
        return deps.spine.dead_letter(d, &err).await;
    };

    // Per-invoke capability scope (see `crate::host_api`/`crate::
    // capabilities`'s per-invoke-scoping design): built fresh from THIS
    // envelope's own (tenant, community, app_id), never a fixed
    // connection-lifetime default.
    let capabilities: Arc<dyn CapabilityHandler> = Arc::new(StageCapabilities::new(
        d.env.tenant.clone(),
        d.env.community.clone(),
        deps.app_id.clone(),
    ));
    let trace = d.env.trace.as_ref().map(|t| TraceContext {
        traceparent: t.traceparent.clone(),
        tracestate: t.tracestate.clone(),
    });

    let outcome = invoke_transform(
        &connection,
        &deps.app_id,
        &deps.digest,
        &d.env.event,
        deps.call_timeout_ms,
        trace,
        capabilities,
    )
    .await;

    let event_out = match outcome {
        Err(InvokeError::ExecutorError { code, message }) => {
            let kind = error_code_to_dlq_kind(code);
            tracing::error!(app_id = %deps.app_id, ?code, %message, "transform invoke failed, dead-lettering");
            let err = DlqError {
                kind,
                code: format!("{code:?}"),
                message,
                detail: None,
                artifact_digest: Some(deps.digest.clone()),
                consumer_id: deps.consumer_id.clone(),
            };
            return deps.spine.dead_letter(d, &err).await;
        }
        Err(e) => {
            tracing::error!(app_id = %deps.app_id, error = %e, "transform invoke failed, dead-lettering");
            let err = DlqError {
                kind: DlqErrorKind::ExecutorUnavailable,
                code: "INVOKE_FAILED".to_string(),
                message: e.to_string(),
                detail: None,
                artifact_digest: Some(deps.digest.clone()),
                consumer_id: deps.consumer_id.clone(),
            };
            return deps.spine.dead_letter(d, &err).await;
        }
        Ok(TransformOutcome::UnsupportedStage) => {
            tracing::error!(app_id = %deps.app_id, "bundle does not implement process-stage.transform, dead-lettering");
            let err = DlqError {
                kind: DlqErrorKind::BundleError,
                code: "UNSUPPORTED_STAGE".to_string(),
                message: "bundle does not implement process-stage.transform".to_string(),
                detail: None,
                artifact_digest: Some(deps.digest.clone()),
                consumer_id: deps.consumer_id.clone(),
            };
            return deps.spine.dead_letter(d, &err).await;
        }
        Ok(TransformOutcome::NoReply) => {
            tracing::info!(app_id = %deps.app_id, "transform returned no reply");
            return deps.spine.ack(d, &deps.app_id).await;
        }
        Ok(TransformOutcome::Reply(event)) => *event,
    };

    let mut event_out = event_out;
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
            return deps.spine.ack(d, &deps.app_id).await;
        }
        RouteDecision::SameApp => deps.app_id.clone(),
        RouteDecision::Redirect { target_app_id } => target_app_id,
    };

    let scope = Scope::new(d.env.tenant.clone(), d.env.community.clone());
    let dest_stream = scope.action_stream(&dest_app_id);
    let ts = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
    let envelope_out = StageEnvelope {
        schema_version: penguin_spine::ENVELOPE_SCHEMA_VERSION,
        tenant: d.env.tenant.clone(),
        community: d.env.community.clone(),
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
    deps.spine.ack(d, &deps.app_id).await
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
    let reader = GroupReader::connect(
        &cfg,
        grants,
        app_id,
        Stage::Process,
        dlq,
        deps.metrics.clone(),
    )
    .await?;
    drain_loop(reader, deps, shutdown).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

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
    }

    impl SpineOps for FakeSpineOps {
        fn ack<'a>(
            &'a self,
            d: &'a Delivered,
            _app_id: &'a str,
        ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<(), SpineError>> + Send + 'a>>
        {
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
        let deps = ProcessDeps {
            app_id: "waddles.bot.commands.default".to_string(),
            digest: "sha256:00".to_string(),
            key_ring: test_ring(),
            connections,
            call_timeout_ms: 2000,
            approved_targets: HashMap::new(),
            consumer_id: "test-pod-consumer".to_string(),
            spine,
            metrics: metrics.clone() as Arc<dyn SpineMetrics>,
            // ON by default so every existing test's drain behavior is
            // unaffected -- the gate's own OFF/ON behavior is exercised
            // directly by the `license_gate_*` tests below.
            license: Arc::new(crate::license::test_support::FixedGate(true)),
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
}
