//! The action-stage dispatch loop (spec §4.3/§16 M3 row): `XREADGROUP`s
//! one bundle's own `:action` stream through its single consumer group
//! `{app_id}`, verifies the hop (`crate::hop`, spec §5.11) before any other
//! processing, invokes the bundle's `dispatch` export over the host-API
//! connection (`crate::host_api`), applies retry-with-backoff
//! (`crate::retry`) to the bundle's own classified outcome, records the
//! final result to `action_dispatch_log`, batches a usage delta
//! (`crate::usage`, spec §5.12), and `XACK`s or dead-letters the entry.
//!
//! Structured exactly like `core/svc_process/src/spine.rs` (the M4
//! reference for this same drain-loop shape): a private `StreamReader`
//! trait wraps `penguin_spine::GroupReader::read` so `drain_batch`/
//! `drain_loop` are unit-testable against a fake reader, and the real
//! `run()` entry point wires the live Valkey/host-API connections.
//!
//! **Scope note (spec §7.6, bucket poller/hot-swap):** deciding *which*
//! digest a bundle should run -- comparing the distribution API's
//! advertised digest set against what an executor already has loaded -- is
//! not wired in this landing (it depends on the same `GET /api/v1/
//! distribution/bundles?stage=action` poll client that svc-process's own
//! M4 skeleton also left as `TODO(M4)`, spec §6.7). [`ensure_loaded`] sends
//! a real `load` frame and is fully wired/tested; *when* to call it with
//! *which* digest is the caller's responsibility until that poll client
//! lands -- `run()` accepts the digest/keys as parameters rather than
//! discovering them.

use std::sync::{Arc, Mutex};

use penguin_bundle_host::wire::{
    ExportKind, InvokeBody, LoadBody, LoadLimits, LoadedBody, Message, TraceContext, UnloadBody,
    UnloadedBody,
};
use penguin_spine::{
    Delivered, Grant, GroupReader, SpineClient, SpineConfig, SpineError, SpineMetrics, Stage,
    StageEnvelope,
};
use serde::Deserialize;

use crate::active_digests::{ActiveDigests, LoadedSessions};
use crate::capabilities::InvokeScope;
use crate::hop::KeyRing;
use crate::host_api::{Connection, ConnectionRegistry, HostApiError};
use crate::retry::{dispatch_with_retry, AttemptOutcome, DispatchRecord, Jitter};
use crate::usage::UsageBatcher;

/// Where [`DispatchDeps`] gets the digest to `load`/`invoke` with on every
/// single delivered entry -- direct port of `core/svc_process/src/
/// spine.rs::DigestSource` under this stage's own module. See that type's
/// doc for the full rationale; reproduced narrowly here since the two
/// crates don't share a dependency this seam could live in.
#[derive(Clone)]
pub enum DigestSource {
    /// The legacy, single-bundle-per-pod, env-configured path
    /// (`crate::lib::try_start_dispatch`'s `ACTION_BUNDLE_DIGEST`) --
    /// unchanged behavior from before this change. Empty disables nothing
    /// by itself: an empty digest is simply sent as-is and the executor
    /// reports `UNKNOWN_BUNDLE`, mapped to a non-retryable attempt like any
    /// other unloaded-bundle invoke. This variant must never be selected by
    /// the multi-tenant path (`crate::dispatch_supervisor`'s own doc).
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
    ///
    /// regression: svc-action had no multi-tenant dispatch consumers;
    /// replies never sent after legacy env removal (alpha 2026-10-03)
    ///
    /// `sessions` is the per-session counterpart `crate::changelog_consumer`
    /// writes to in lock-step with `digests` -- [`DigestSource::session_for`]
    /// uses it to pick a live executor session that actually has the
    /// resolved digest loaded, never just whichever connection
    /// `ConnectionRegistry::active()` calls "newest" (regression: bundles
    /// loaded only onto a terminating executor during rollout; live
    /// executor got none, alpha 2026-10-03).
    Active {
        scope: bundle_active_set::AppScope,
        digests: Arc<ActiveDigests>,
        sessions: Arc<LoadedSessions>,
    },
}

impl DigestSource {
    /// Resolves the digest to `invoke` with right now. See each variant's
    /// own doc for what `None`/empty means.
    fn current(&self) -> Option<String> {
        match self {
            DigestSource::Static(d) => Some(d.clone()),
            DigestSource::Active { scope, digests, .. } => digests.get(scope),
        }
    }

    /// The live executor session (if any) that should serve this `digest` --
    /// `DigestSource::Static` has no per-session tracking at all (the
    /// legacy, single-bundle-per-pod path predates multi-session executors)
    /// so it is not resolvable here; `handle_delivered` falls back to
    /// `ConnectionRegistry::active()` for that variant only, unchanged from
    /// before this fix. `DigestSource::Active` MUST use this instead of
    /// `ConnectionRegistry::active()` -- picking a live session that merely
    /// happens to be newest, without checking it actually has `digest`
    /// loaded, is exactly the alpha 2026-10-03 failure mode.
    fn session_for_digest(&self, digest: &str) -> Option<bundle_active_set::SessionId> {
        match self {
            DigestSource::Static(_) => None,
            DigestSource::Active {
                scope, sessions, ..
            } => sessions.pick_session_with_digest(scope, digest),
        }
    }
}

/// Tunables `run()` needs beyond what `penguin_spine::SpineConfig` already
/// covers (spec §4.3).
#[derive(Debug, Clone)]
pub struct RetryPolicy {
    pub max_retries: u32,
    pub base_backoff_ms: u64,
    pub max_backoff_ms: u64,
    pub call_timeout_ms: u64,
}

/// Errors invoking the bundle's `dispatch` export over the host-API
/// connection -- distinct from [`crate::retry::AttemptOutcome`], which
/// classifies the bundle's own *business-level* result. An `InvokeError`
/// means the invocation itself never produced a bundle-classified outcome
/// at all (infrastructure failure) and is DLQ'd directly, never run through
/// the in-process retry loop (spec §6.3's `call_timeout`/`bundle_trap`/
/// `host_call_denied`/`executor_unavailable` DLQ kinds are all
/// infrastructure-level, distinct from the bundle's own
/// `transport-error.retryable`, spec §4.3).
#[derive(Debug, thiserror::Error)]
pub enum InvokeError {
    #[error("no executor connection available")]
    NoExecutor,
    #[error("host-api error: {0}")]
    HostApi(#[from] HostApiError),
    #[error("executor reported error {code:?}: {message}")]
    ExecutorError { code: String, message: String },
    #[error("invoke result payload was not valid JSON: {0}")]
    MalformedPayload(String),
}

/// Sends `load` for one bundle over `conn` and returns the executor's
/// `loaded` reply (spec §6.6). A real, fully-wired wrapper around
/// [`Connection::request`] -- see the module doc for what remains a seam
/// (deciding *when*/*which* digest to load).
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
            code: format!("{:?}", e.code),
            message: e.message,
        }),
        _ => Err(InvokeError::ExecutorError {
            code: "UNEXPECTED_FRAME".to_string(),
            message: "expected loaded or error".to_string(),
        }),
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
            code: format!("{:?}", e.code),
            message: e.message,
        }),
        _ => Err(InvokeError::ExecutorError {
            code: "UNEXPECTED_FRAME".to_string(),
            message: "expected unloaded or error".to_string(),
        }),
    }
}

/// Converts the internal `penguin_spine::StageEnvelope` into the exact wire
/// shape `wit/waddle-bundle/stage.wit`'s `stage-envelope`/`platform-event`
/// records require. `core/bundle_executor/src/engine.rs`'s `bindgen!`
/// generates those types with `additional_derives: [serde::Deserialize]`
/// and no `rename_all`, so the JSON keys are exactly the WIT fields'
/// snake_case Rust identifiers (`payload_json`, `event_type`, `app_id`,
/// `target_app_id`, `trace_context`) -- confirmed against
/// `core/bundle_executor/src/invoke.rs`'s `EnvelopeAndConfig` struct.
///
/// Deliberately narrower than `StageEnvelope`'s own `Serialize` impl: this
/// crate's `env` carries D30 identity/audit fields (`schema_version`,
/// `workstream_id`, `event_id`, `session_id`, `binding`) a bundle must
/// never see (spec §5.11 "Bundles cannot move a workstream"), and nests
/// `event.payload` as a JSON *object* rather than the WIT record's
/// `payload_json` canonical-JSON *string*. Serializing `env` directly (this
/// function's predecessor) round-trips through `serde_json::from_value`
/// into `bundle_executor`'s WIT-generated `StageEnvelope` type and fails
/// every call with `MalformedFrame: missing field \`payload_json\`` --
/// caught by the hermetic relay e2e proof (`fix/svc-action-bundle-executor-
/// alpha-wiring`), never previously exercised end-to-end because no bundle-
/// executor was wired to this stage until that same change.
fn envelope_to_wire_json(env: &StageEnvelope) -> Result<serde_json::Value, InvokeError> {
    let payload_json = serde_json::to_string(&env.event.payload)
        .map_err(|e| InvokeError::MalformedPayload(e.to_string()))?;
    Ok(serde_json::json!({
        "tenant": env.tenant,
        "community": env.community,
        "app_id": env.app_id,
        "stage": env.stage,
        "event": {
            "platform": env.event.platform,
            "event_type": env.event.event_type,
            "actor": env.event.actor,
            "payload_json": payload_json,
            "occurred_at": env.event.occurred_at,
        },
        "ts": env.ts,
        "target_app_id": env.target_app_id,
        "trace_context": env.trace.as_ref().map(|t| t.traceparent.clone()),
    }))
}

/// Invokes the bundle's `dispatch` export for one delivered envelope. The
/// wire payload is `{"envelope": ..., "config": ...}` (this executor's own
/// convention for `EnvelopeAndConfig`, spec SS6.6 only specifies "the
/// export's arguments as JSON") with `envelope` built by
/// [`envelope_to_wire_json`] -- see that function's doc for the wire-shape
/// bug this replaced. Returns the raw `result.payload` JSON for
/// [`interpret_dispatch_payload`] to classify.
pub async fn invoke_dispatch(
    conn: &Connection,
    app_id: &str,
    digest: &str,
    env: &StageEnvelope,
    config_json: &str,
    deadline_ms: u64,
) -> Result<serde_json::Value, InvokeError> {
    let trace = env.trace.as_ref().map(|t| TraceContext {
        traceparent: t.traceparent.clone(),
        tracestate: t.tracestate.clone(),
    });
    let payload = serde_json::json!({
        "envelope": envelope_to_wire_json(env)?,
        "config": config_json,
    });
    // Spec §5.11: tenant/community come from the verified envelope, never
    // from payload -- this is the scope every `host-call` the executor
    // issues during this invoke will be answered against (see
    // `crate::capabilities`'s module doc and `crate::host_api::Connection::
    // invoke`).
    let scope = InvokeScope {
        tenant: env.tenant.clone(),
        community: env.community.clone(),
        app_id: app_id.to_string(),
        // The inbound event's own origin channel (spec: relay providers,
        // discord) -- `None` when the platform/event has no channel
        // concept. Never re-derived from anything a bundle returns.
        origin_channel_id: env.event.source.as_ref().and_then(|s| s.channel_id.clone()),
    };
    let reply = conn
        .invoke(
            InvokeBody {
                app_id: app_id.to_string(),
                digest: digest.to_string(),
                export: ExportKind::Dispatch,
                payload,
                deadline_ms,
                trace,
            },
            scope,
        )
        .await?;
    match reply.message {
        Message::Result(body) => Ok(body.payload),
        Message::Error(e) => Err(InvokeError::ExecutorError {
            code: format!("{:?}", e.code),
            message: e.message,
        }),
        _ => Err(InvokeError::ExecutorError {
            code: "UNEXPECTED_FRAME".to_string(),
            message: "expected result or error".to_string(),
        }),
    }
}

/// `bundle_executor`'s actual `dispatch` result wire shape (spec §6.5's
/// `result<transport-result, transport-error>`, `core/bundle_executor/src/
/// invoke.rs`'s `ExportKind::Dispatch` arm) -- **not** flattened under one
/// `ok` discriminant, contrary to this struct's predecessor
/// (`DispatchResultPayload`): a WIT `Ok` return serializes `transport-
/// result` FLAT at the top level (its own `ok`/`status`/`detail`/
/// `provider_message_id` fields, `ok` always `true` here), while an `Err`
/// return wraps `transport-error` under a `transport_error` key with NO
/// top-level `ok` field at all (`transport-error` has no `ok`/`status`
/// fields either -- only `retryable`/`code`/`message`/`retry_after_ms`,
/// spec `wit/waddle-bundle/stage.wit`). The single-struct assumption meant
/// every real dispatch failure hit "missing field `ok`" instead of being
/// classified retryable/non-retryable -- caught by the hermetic relay e2e
/// proof (`fix/svc-action-bundle-executor-alpha-wiring`), never previously
/// exercised end-to-end because no bundle-executor was wired to this stage
/// until that same change.
#[derive(Debug, Deserialize)]
struct TransportResultPayload {
    ok: bool,
    #[serde(default)]
    status: Option<u16>,
    #[serde(default)]
    detail: Option<String>,
}

/// See [`TransportResultPayload`]'s doc -- the `Err` branch's wire shape.
#[derive(Debug, Deserialize)]
struct TransportErrorPayload {
    retryable: bool,
    code: String,
    message: String,
    #[serde(default)]
    retry_after_ms: Option<u32>,
}

/// The `Err` branch's outer wrapper key (`bundle_executor::invoke`'s
/// `serde_json::json!({"transport_error": v})`).
#[derive(Debug, Deserialize)]
struct TransportErrorEnvelope {
    transport_error: TransportErrorPayload,
}

/// Classifies a `dispatch` export's raw JSON result into an
/// [`AttemptOutcome`] the retry loop understands. `target_type_hint` (e.g.
/// `"irc_relay"`) is used for a successful outcome's
/// `action_dispatch_log.target_type` when the payload itself doesn't name
/// one -- mirrors the Python runner's `result.transport` field. Tries the
/// `Err` (`transport_error`-wrapped) shape first since it's structurally
/// distinguishable (the wrapper key), falling back to the flat `Ok`
/// (`transport-result`) shape -- see [`TransportResultPayload`]'s doc for
/// why these are two shapes, not one.
fn interpret_dispatch_payload(
    payload: &serde_json::Value,
    target_type_hint: &str,
) -> AttemptOutcome {
    if let Ok(err_env) = serde_json::from_value::<TransportErrorEnvelope>(payload.clone()) {
        let e = err_env.transport_error;
        let detail = format!("{} ({})", e.message, e.code);
        return if e.retryable {
            AttemptOutcome::Retryable {
                http_status: None,
                detail,
                retry_after_ms: e.retry_after_ms.map(u64::from),
            }
        } else {
            AttemptOutcome::NonRetryable {
                http_status: None,
                detail,
            }
        };
    }
    match serde_json::from_value::<TransportResultPayload>(payload.clone()) {
        Ok(p) if p.ok => AttemptOutcome::Success {
            target_type: target_type_hint.to_string(),
            http_status: p.status.map(i32::from),
            detail: p.detail.unwrap_or_default(),
        },
        Ok(_) | Err(_) => AttemptOutcome::NonRetryable {
            http_status: None,
            detail: format!(
                "dispatch result payload matched neither transport-result nor \
                 transport-error: {payload}"
            ),
        },
    }
}

/// Writes the final [`DispatchRecord`] to `action_dispatch_log` -- narrow
/// trait so `handle_delivered` is testable without a live Postgres
/// connection; `crate::db::entities::action_dispatch_log::insert` backs
/// the real implementation used by [`run`].
pub trait AuditSink: Send + Sync {
    fn record<'a>(
        &'a self,
        tenant_id: i32,
        community_id: Option<i32>,
        app_id: &'a str,
        record: &'a DispatchRecord,
        envelope_ts: Option<chrono::DateTime<chrono::FixedOffset>>,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send + 'a>>;
}

/// `(tenants.id, communities.id)` -- the pair [`TenantResolver::resolve`]
/// produces, `communities.id` absent for a tenant-wide activation or an
/// unresolvable community slug.
pub type ResolvedTenant = (i32, Option<i32>);

/// Resolves an envelope's `tenant`/`community` slugs to the integer FKs
/// `action_dispatch_log` requires -- narrow trait so `handle_delivered`
/// doesn't need a live `tenants` table to be unit-tested. The real
/// implementation queries the same way the Python runner's
/// `_resolve_tenant_id` does (memoized per slug).
pub trait TenantResolver: Send + Sync {
    fn resolve<'a>(
        &'a self,
        tenant_slug: &'a str,
        community_slug: Option<&'a str>,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Option<ResolvedTenant>> + Send + 'a>>;
}

/// Abstraction over [`penguin_spine::GroupReader::read`]'s exact signature
/// (mirrors `core/svc_process/src/spine.rs`'s identical seam) so
/// [`drain_loop`] can be driven by a fake reader in tests.
trait StreamReader {
    async fn read(&mut self) -> Result<Vec<Delivered>, SpineError>;
}

impl StreamReader for GroupReader {
    async fn read(&mut self) -> Result<Vec<Delivered>, SpineError> {
        GroupReader::read(self).await
    }
}

/// Abstraction over the two [`penguin_spine::SpineClient`] operations
/// [`handle_delivered`] needs (`ack`/`dead_letter`) -- narrow trait, same
/// rationale as [`AuditSink`]/[`TenantResolver`]: a live Valkey connection
/// is `SpineClient::connect`'s own concern (already covered by
/// `penguin-spine`'s own test suite), not something every caller of this
/// module's control flow should need just to exercise it.
pub trait SpineOps: Send + Sync {
    fn ack<'a>(
        &'a self,
        d: &'a Delivered,
        app_id: &'a str,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<(), SpineError>> + Send + 'a>>;

    fn dead_letter<'a>(
        &'a self,
        d: &'a Delivered,
        err: &'a penguin_spine::DlqError,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<(), SpineError>> + Send + 'a>>;
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
        err: &'a penguin_spine::DlqError,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<(), SpineError>> + Send + 'a>>
    {
        Box::pin(SpineClient::dead_letter(self, d, err))
    }
}

/// Everything [`handle_delivered`] needs beyond the entry itself --
/// bundled so `drain_batch`/`drain_loop`/`run` don't carry an
/// ever-growing parameter list.
pub struct DispatchDeps<A: AuditSink, T: TenantResolver, S: SpineOps> {
    pub app_id: String,
    /// See [`DigestSource`]'s own doc -- resolved fresh on every single
    /// delivered entry, never captured once at spawn time (regression:
    /// svc-action had no multi-tenant dispatch consumers; replies never
    /// sent after legacy env removal, alpha 2026-10-03).
    pub digest_source: DigestSource,
    pub config_json: String,
    pub key_ring: KeyRing,
    pub connections: Arc<ConnectionRegistry>,
    pub retry_policy: RetryPolicy,
    pub jitter: Jitter,
    pub audit: A,
    pub tenants: T,
    pub usage: Arc<Mutex<UsageBatcher>>,
    /// This pod's own identity (`penguin_spine::SpineConfig::consumer_id`,
    /// `SPINE_CONSUMER_ID`) -- the value a `DlqError.consumer_id` must
    /// carry, matching `penguin_spine::client::claim_stale`'s convention
    /// (the *consumer* that owned the entry when it was dead-lettered, not
    /// the entry's own stream id -- `Delivered::entry_id` is a different
    /// concept entirely and must never be substituted here).
    pub consumer_id: String,
    pub spine: S,
    pub metrics: Arc<dyn SpineMetrics>,
}

/// Handles exactly one delivered entry end to end. Returns `Ok(())` in
/// every case where the entry was terminally handled (acked or
/// dead-lettered) -- only a `SpineError` from the ack/DLQ write itself
/// propagates, matching `core/svc_process/src/spine.rs`'s error-handling
/// shape.
async fn handle_delivered<A: AuditSink, T: TenantResolver, S: SpineOps>(
    d: &Delivered,
    stream_key: &str,
    deps: &DispatchDeps<A, T, S>,
) -> Result<(), SpineError> {
    // Spec §5.11: hop verification runs before any other processing.
    if let Err(reason) = crate::hop::verify_hop(&deps.key_ring, &d.env, stream_key, &deps.app_id) {
        deps.metrics
            .tenant_boundary_violation("action", reason.as_metric_reason());
        tracing::error!(
            app_id = %d.env.app_id,
            tenant = %d.env.tenant,
            reason = reason.as_metric_reason(),
            "hop verification failed, dead-lettering (never retried)"
        );
        let err = penguin_spine::DlqError {
            kind: penguin_spine::DlqErrorKind::TenantBoundary,
            code: "TENANT_BOUNDARY".to_string(),
            message: reason.to_string(),
            detail: None,
            artifact_digest: deps.digest_source.current(),
            consumer_id: deps.consumer_id.clone(),
        };
        return deps.spine.dead_letter(d, &err).await;
    }

    // Resolve the digest to invoke with BEFORE ever checking for an
    // executor connection -- "if no active digest is known for an app when
    // a message arrives, log ERROR and dead-letter for redelivery; never
    // invoke with an empty digest" (regression: svc-action had no
    // multi-tenant dispatch consumers; replies never sent after legacy env
    // removal, alpha 2026-10-03). `DigestSource::Static` always resolves
    // (possibly to an intentionally empty string, unchanged legacy
    // behavior); only `DigestSource::Active` with no entry for this scope
    // yields `None` here.
    let Some(digest) = deps.digest_source.current() else {
        tracing::error!(
            app_id = %deps.app_id,
            tenant = %d.env.tenant,
            community = ?d.env.community,
            "no active bundle digest known for this app's scope; dead-lettering for redelivery"
        );
        let err = penguin_spine::DlqError {
            kind: penguin_spine::DlqErrorKind::BundleError,
            code: "NO_ACTIVE_DIGEST".to_string(),
            message: "no active bundle digest known for this (tenant, community, app) scope"
                .to_string(),
            detail: None,
            artifact_digest: None,
            consumer_id: deps.consumer_id.clone(),
        };
        return deps.spine.dead_letter(d, &err).await;
    };

    // Pick the executor session to invoke on. `DigestSource::Active` MUST
    // pick a live session that actually has `digest` loaded
    // (`bundle_active_set::pick_session_with_digest`, via `DigestSource::
    // session_for_digest`), never just whichever connection
    // `ConnectionRegistry::active()` calls "newest" -- that is exactly the
    // alpha 2026-10-03 failure mode (a rolling pod's about-to-terminate
    // executor session briefly "newest", so every invoke routed there and
    // got `UnknownBundle`/silence while the real live executor held
    // everything). `DigestSource::Static` has no per-session tracking at all
    // (the legacy, single-bundle-per-pod path predates multi-session
    // executors) and keeps the unchanged `ConnectionRegistry::active()`
    // fallback.
    let no_loaded_session = matches!(deps.digest_source, DigestSource::Active { .. })
        && deps.digest_source.session_for_digest(&digest).is_none();
    let connection = if no_loaded_session {
        None
    } else {
        match deps.digest_source.session_for_digest(&digest) {
            Some(session_id) => deps.connections.get(session_id),
            None => deps.connections.active(),
        }
    };

    let Some(connection) = connection else {
        if no_loaded_session {
            // Fail-closed, distinct from "no executor at all" below: at
            // least one executor is connected, but none of them has this
            // exact digest loaded right now -- never invoke a session that
            // lacks the bundle (regression: bundles loaded only onto a
            // terminating executor during rollout; live executor got none,
            // alpha 2026-10-03).
            tracing::error!(
                app_id = %deps.app_id,
                digest_prefix = %bundle_active_set::digest_prefix(&digest),
                "no live executor session has this bundle's digest loaded; dead-lettering for redelivery"
            );
            let err = penguin_spine::DlqError {
                kind: penguin_spine::DlqErrorKind::ExecutorUnavailable,
                code: "NO_LOADED_EXECUTOR".to_string(),
                message: "no live executor session has the target digest loaded".to_string(),
                detail: None,
                artifact_digest: Some(digest.clone()),
                consumer_id: deps.consumer_id.clone(),
            };
            return deps.spine.dead_letter(d, &err).await;
        }
        // Escalated WARN -> ERROR (fix/executor-link-heartbeat, alpha
        // 2026-10-02 incident: svc-process/svc-action were rolled and each
        // bundle-executor stayed bound to its old, terminated pod; the new
        // svc had zero executors and silently dead-lettered every entry at
        // WARN -- nobody noticed until a user reported it). The outage
        // duration is named in the rendered message itself, not only a
        // structured field, per the "over-log, never swallow errors" rule.
        let app_id = &deps.app_id;
        let no_executor_for_s = deps.connections.duration_without_executor().as_secs();
        deps.connections.record_dead_letter_no_executor();
        tracing::error!(
            app_id = %app_id,
            no_executor_for_s,
            "no executor connection available for {no_executor_for_s}s (app_id {app_id}), \
             dead-lettering for redelivery"
        );
        let err = penguin_spine::DlqError {
            kind: penguin_spine::DlqErrorKind::ExecutorUnavailable,
            code: "EXECUTOR_UNAVAILABLE".to_string(),
            message: "no active host-api connection".to_string(),
            detail: None,
            artifact_digest: Some(digest.clone()),
            consumer_id: deps.consumer_id.clone(),
        };
        return deps.spine.dead_letter(d, &err).await;
    };

    // Every attempt -- including the first -- goes through the same
    // in-process retry-with-backoff loop (spec §4.3: "the runner owns all
    // backoff timing; a bundle never sleeps"), matching the Python
    // runner's `_attempt` closure calling the entrypoint fresh on every
    // attempt via `retry_with_backoff`. An `InvokeError` (infrastructure
    // failure: no reply, malformed frame, executor-reported protocol
    // error) is distinct from the bundle's own classified
    // retryable/non-retryable outcome; this landing records it as a
    // non-retryable attempt (detail names the invoke failure) rather than
    // a separate DLQ path -- the connection resolution above already
    // catches the "no executor at all"/"no session has this digest" cases
    // before ever entering this loop, which is the one infra failure worth
    // a distinct DLQ kind at this stage's current scope.
    let (record, _attempts) = dispatch_with_retry(
        |_attempt| async {
            match invoke_dispatch(
                &connection,
                &deps.app_id,
                &digest,
                &d.env,
                &deps.config_json,
                deps.retry_policy.call_timeout_ms,
            )
            .await
            {
                Ok(p) => interpret_dispatch_payload(&p, "irc_relay"),
                Err(e) => {
                    // Diagnosability fix (regression: multi_tenant path
                    // sent bare-hex digest to Invoke, UnknownBundle despite
                    // loaded bundle (alpha 2026-10-03)): `UnknownBundle`'s
                    // `message` IS the digest the executor echoed back
                    // (`bundle_executor::invoke::on_invoke`'s
                    // `error_body`), so an empty/unresolved digest renders
                    // here with nothing to grep on -- log the resolved
                    // digest's own prefix explicitly, matching
                    // `core/svc_process/src/spine.rs`'s identical fix.
                    tracing::error!(
                        app_id = %deps.app_id,
                        digest_prefix = bundle_active_set::digest_prefix(&digest),
                        error = %e,
                        "action invoke failed, recording non-retryable attempt"
                    );
                    AttemptOutcome::NonRetryable {
                        http_status: None,
                        detail: format!("invoke failed: {e}"),
                    }
                }
            }
        },
        deps.retry_policy.max_retries,
        deps.retry_policy.base_backoff_ms,
        deps.retry_policy.max_backoff_ms,
        &deps.jitter,
        |dur| tokio::time::sleep(dur),
    )
    .await;

    let (tenant_fk, community_fk) = deps
        .tenants
        .resolve(&d.env.tenant, d.env.community.as_deref())
        .await
        .unwrap_or((0, None));
    let envelope_ts = chrono::DateTime::parse_from_rfc3339(&d.env.ts).ok();
    deps.audit
        .record(tenant_fk, community_fk, &deps.app_id, &record, envelope_ts)
        .await;

    deps.usage
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .record_action_delivered(
            &d.env.tenant,
            d.env.community.as_deref(),
            &d.env.workstream_id,
            &d.env.app_id,
        );

    deps.spine.ack(d, &deps.app_id).await
}

/// Reads and dispatches exactly one batch, returning how many entries were
/// handled.
async fn drain_batch<R: StreamReader, A: AuditSink, T: TenantResolver, S: SpineOps>(
    reader: &mut R,
    stream_key: &str,
    deps: &DispatchDeps<A, T, S>,
) -> Result<usize, SpineError> {
    let batch = reader.read().await?;
    for d in &batch {
        handle_delivered(d, stream_key, deps).await?;
    }
    Ok(batch.len())
}

/// Runs [`drain_batch`] in a loop until `shutdown` resolves -- identical
/// shape to `core/svc_process/src/spine.rs::drain_loop`, plus the spec
/// §13.5 `waddles.core.rust-data-plane` gate: while `rust_data_plane` is
/// disabled, this loop never calls [`drain_batch`] at all (nothing is
/// read, dispatched, acked, or dead-lettered) -- "the stage serves health
/// and metrics and drains nothing, which is the safe state during
/// rollout." The gate is re-checked every iteration (via
/// `crate::flags::FeatureFlag::enabled`'s non-blocking cached read for the
/// real `penguin_licensing`-backed implementation, never inline network
/// I/O), so a live flag flip takes effect within one poll of this loop,
/// not just at startup.
async fn drain_loop<R: StreamReader, A: AuditSink, T: TenantResolver, S: SpineOps>(
    mut reader: R,
    stream_key: String,
    deps: DispatchDeps<A, T, S>,
    rust_data_plane: Arc<dyn crate::flags::FeatureFlag>,
    mut shutdown: tokio::sync::oneshot::Receiver<()>,
) -> Result<(), SpineError> {
    loop {
        if !rust_data_plane.enabled().await {
            tokio::select! {
                _ = &mut shutdown => return Ok(()),
                () = tokio::time::sleep(std::time::Duration::from_secs(5)) => continue,
            }
        }
        tokio::select! {
            _ = &mut shutdown => return Ok(()),
            result = drain_batch(&mut reader, &stream_key, &deps) => {
                result?;
            }
        }
    }
}

/// Connects the spine DLQ client and a grant-scoped [`GroupReader`] for
/// `deps.app_id`'s action stream, then runs the drain loop until
/// `shutdown` resolves. `stream_key` is the single action-stream key this
/// bundle's grant list is expected to contain exactly once (spec §6.2:
/// `{scope}:app:{app_id}:action`) -- passed separately because
/// `Delivered::stream` already carries it per-entry and hop verification
/// needs it before the entry is otherwise trusted.
pub async fn run<A: AuditSink, T: TenantResolver>(
    cfg: SpineConfig,
    grants: Vec<Grant>,
    stream_key: String,
    deps: DispatchDeps<A, T, SpineClient>,
    rust_data_plane: Arc<dyn crate::flags::FeatureFlag>,
    shutdown: tokio::sync::oneshot::Receiver<()>,
) -> Result<(), SpineError> {
    let app_id = deps.app_id.clone();
    let dlq = SpineClient::connect(cfg.clone(), deps.metrics.clone()).await?;
    let reader = GroupReader::connect(
        &cfg,
        grants,
        app_id,
        Stage::Action,
        dlq,
        deps.metrics.clone(),
    )
    .await?;
    drain_loop(reader, stream_key, deps, rust_data_plane, shutdown).await
}

/// Builds a raw `redis::Client` for `cfg`'s transport -- the same
/// connection-building logic as `core/svc_ingest/src/outbound.rs::
/// build_redis_client`/`core/svc_process/src/spine.rs::build_raw_client`
/// (duplicated rather than imported: `penguin_spine`'s own equivalent is
/// `pub(crate)` to that crate). Used only by [`ensure_consumer_group`].
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
        crate::crypto::ensure_crypto_provider_installed();
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
/// <stream> <group> $ MKSTREAM` -- `BUSYGROUP` (already exists) is treated
/// as success, never an error. Called once at startup and again as the
/// self-heal step whenever a [`is_nogroup_error`] error surfaces mid-drain
/// (`crate::lib::try_start_dispatch`'s retry wrapper) -- on a fresh Valkey
/// nothing else in this env-driven single-bundle path ever creates the
/// group. Returns `Ok(true)` if newly created, `Ok(false)` if it already
/// existed (`BUSYGROUP`).
///
/// regression: drain loop exited on NOGROUP (alpha 2026-10-02) -- same bug
/// class as `core/svc_process`'s legacy process loop, fixed identically.
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
/// [`redis::RedisError::code`], same rationale as `core/svc_process::
/// source_supervisor::is_nogroup_error`'s identical check.
pub(crate) fn is_nogroup_error(err: &SpineError) -> bool {
    matches!(err, SpineError::Redis(e) if e.code() == Some("NOGROUP"))
}

/// Test-only helpers mirroring `core/svc_process/src/spine.rs::
/// test_support` -- a real local Valkey/Redis on the default port when one
/// happens to be reachable, skipped honestly (never a failure) otherwise.
#[cfg(test)]
pub(crate) mod test_support {
    use super::SpineConfig;

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
    use crate::hop::BoundaryReason;

    // regression: drain loop exited on NOGROUP (alpha 2026-10-02)
    #[test]
    fn is_nogroup_error_matches_on_the_redis_error_code_not_message_wording() {
        let err = SpineError::Redis(redis::make_extension_error(
            "NOGROUP".to_string(),
            Some(
                "No such key 'waddles:t:global:c:_tenant:app:waddles.a:action' or consumer \
                 group 'waddles.a' in XREADGROUP with GROUP option"
                    .to_string(),
            ),
        ));
        assert!(is_nogroup_error(&err));
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
    // stream) rather than ever surfacing NOGROUP to the dispatch loop.
    #[tokio::test]
    async fn ensure_consumer_group_creates_the_group_and_stream_on_a_fresh_valkey() {
        let Some(cfg) = test_support::local_valkey_config() else {
            eprintln!("skipping: no local Valkey reachable at 127.0.0.1:6379");
            return;
        };
        let stream = test_support::unique_key("action-ensure-group-stream");
        let group = test_support::unique_key("action-ensure-group-group");

        let created = ensure_consumer_group(&cfg, &stream, &group)
            .await
            .expect("first create succeeds");
        assert!(created);

        let created_again = ensure_consumer_group(&cfg, &stream, &group)
            .await
            .expect("second create (BUSYGROUP) must not error");
        assert!(!created_again);
    }

    #[test]
    fn envelope_to_wire_json_matches_the_wit_stage_envelope_shape() {
        // Regression for the hermetic relay e2e proof's "MalformedFrame:
        // missing field `payload_json`" -- `envelope_to_wire_json` must
        // produce exactly `wit/waddle-bundle/stage.wit`'s `stage-envelope`/
        // `platform-event` field set (snake_case, no D30 identity fields,
        // `payload_json` as a JSON *string*, `trace_context` as a flat
        // optional string), never `penguin_spine::StageEnvelope`'s own
        // richer `Serialize` shape.
        let ring = test_ring();
        let mac = mac_for(&ring, "acme");
        let d = fixture_delivered("acme", "waddles.bot.commands.default", mac, "k1");

        let wire = envelope_to_wire_json(&d.env).expect("payload always serializes");

        assert_eq!(wire["tenant"], "acme");
        assert_eq!(wire["community"], "main");
        assert_eq!(wire["app_id"], "waddles.bot.commands.default");
        assert_eq!(wire["stage"], "action");
        assert_eq!(wire["ts"], "2026-09-22T00:00:00.000Z");
        assert_eq!(wire["target_app_id"], serde_json::Value::Null);
        assert_eq!(wire["trace_context"], serde_json::Value::Null);
        assert_eq!(wire["event"]["platform"], "twitch");
        assert_eq!(wire["event"]["event_type"], "chat.message");
        assert_eq!(wire["event"]["actor"], "some_user");
        assert_eq!(wire["event"]["occurred_at"], "2026-09-22T00:00:00.000Z");
        // `payload_json` is a STRING (canonical JSON text), not a nested
        // object -- the exact field the executor reported missing.
        assert_eq!(wire["event"]["payload_json"], "{}");
        assert!(wire["event"].get("payload").is_none());
        // No D30 identity/audit fields ever cross to the bundle.
        assert!(wire.get("schema_version").is_none());
        assert!(wire.get("workstream_id").is_none());
        assert!(wire.get("event_id").is_none());
        assert!(wire.get("session_id").is_none());
        assert!(wire.get("binding").is_none());
    }

    #[test]
    fn interpret_success_payload() {
        let outcome = interpret_dispatch_payload(
            &serde_json::json!({"ok": true, "status": 200, "detail": "sent"}),
            "irc_relay",
        );
        assert_eq!(
            outcome,
            AttemptOutcome::Success {
                target_type: "irc_relay".to_string(),
                http_status: Some(200),
                detail: "sent".to_string(),
            }
        );
    }

    #[test]
    fn interpret_retryable_failure_payload() {
        // `bundle_executor::invoke`'s actual `Err` wire shape: `transport-
        // error` wrapped under a `transport_error` key, no top-level `ok`.
        let outcome = interpret_dispatch_payload(
            &serde_json::json!({"transport_error": {"retryable": true, "code": "RATE_LIMITED", "message": "slow down", "retry_after_ms": 5000}}),
            "irc_relay",
        );
        assert_eq!(
            outcome,
            AttemptOutcome::Retryable {
                http_status: None,
                detail: "slow down (RATE_LIMITED)".to_string(),
                retry_after_ms: Some(5000),
            }
        );
    }

    #[test]
    fn interpret_non_retryable_failure_payload() {
        let outcome = interpret_dispatch_payload(
            &serde_json::json!({"transport_error": {"retryable": false, "code": "BAD_REQUEST", "message": "bad request"}}),
            "irc_relay",
        );
        assert_eq!(
            outcome,
            AttemptOutcome::NonRetryable {
                http_status: None,
                detail: "bad request (BAD_REQUEST)".to_string(),
            }
        );
    }

    #[test]
    fn interpret_malformed_transport_error_defaults_to_non_retryable() {
        // Missing the WIT-required `retryable`/`message` fields -- neither
        // a valid `transport_error` wrapper nor a valid `ok: true`
        // transport-result; must fail safe, never be read as a success.
        let outcome = interpret_dispatch_payload(
            &serde_json::json!({"transport_error": {"code": "UNKNOWN"}}),
            "irc_relay",
        );
        assert!(matches!(outcome, AttemptOutcome::NonRetryable { .. }));
    }

    #[test]
    fn interpret_ok_false_transport_result_is_non_retryable() {
        // A flat transport-result with `ok: false` is not a shape
        // `bundle_executor` actually produces (failures are always
        // `transport_error`-wrapped) but must still fail safe rather than
        // being read as a success.
        let outcome = interpret_dispatch_payload(
            &serde_json::json!({"ok": false, "status": 200}),
            "irc_relay",
        );
        assert!(matches!(outcome, AttemptOutcome::NonRetryable { .. }));
    }

    #[test]
    fn interpret_malformed_payload_is_non_retryable() {
        let outcome = interpret_dispatch_payload(&serde_json::json!("not-an-object"), "irc_relay");
        assert!(matches!(outcome, AttemptOutcome::NonRetryable { .. }));
    }

    #[derive(Default)]
    struct FakeAudit {
        records: std::sync::Mutex<Vec<DispatchRecord>>,
    }

    impl AuditSink for FakeAudit {
        fn record<'a>(
            &'a self,
            _tenant_id: i32,
            _community_id: Option<i32>,
            _app_id: &'a str,
            record: &'a DispatchRecord,
            _envelope_ts: Option<chrono::DateTime<chrono::FixedOffset>>,
        ) -> std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send + 'a>> {
            let record = record.clone();
            Box::pin(async move {
                self.records.lock().unwrap().push(record);
            })
        }
    }

    struct FixedTenantResolver;
    impl TenantResolver for FixedTenantResolver {
        fn resolve<'a>(
            &'a self,
            _tenant_slug: &'a str,
            _community_slug: Option<&'a str>,
        ) -> std::pin::Pin<
            Box<dyn std::future::Future<Output = Option<(i32, Option<i32>)>> + Send + 'a>,
        > {
            Box::pin(async { Some((1, Some(2))) })
        }
    }

    fn fixture_delivered(tenant: &str, app_id: &str, mac: String, kid: &str) -> Delivered {
        Delivered {
            stream: format!("waddles:t:{tenant}:c:main:app:{app_id}:action"),
            entry_id: "1234567890-0".to_string(),
            env: serde_json::from_value(serde_json::json!({
                "schema_version": 2,
                "tenant": tenant,
                "community": "main",
                "app_id": app_id,
                "stage": "action",
                "event": {
                    "platform": "twitch",
                    "event_type": "chat.message",
                    "actor": "some_user",
                    "payload": {},
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
            // Action streams' consumer group is always `{app_id}` (spec
            // §5.9) -- `group` now carries the reader's own group
            // (penguin-spine `Delivered.group`, the dead_letter XACK
            // fix), coinciding with `env.app_id` here by construction.
            group: app_id.to_string(),
        }
    }

    fn test_ring() -> KeyRing {
        KeyRing::new(vec![("k1".to_string(), vec![9u8; 32])])
    }

    fn mac_for(ring: &KeyRing, tenant: &str) -> String {
        crate::hop::compute_mac(
            ring,
            "k1",
            tenant,
            Some("main"),
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-4000-8000-000000000002",
            None,
        )
        .unwrap()
    }

    #[test]
    fn boundary_reason_variants_used_by_dispatch_are_constructible() {
        // Exercises the `BoundaryReason` match arm this module's
        // `handle_delivered` relies on existing, without needing a live
        // host-api connection to reach that code path in an integration
        // test.
        let reason = BoundaryReason::TenantMismatch {
            envelope: "a".to_string(),
            key: "b".to_string(),
        };
        assert_eq!(reason.as_metric_reason(), "tenant_mismatch");
    }

    #[derive(Default)]
    struct RecordingSpineMetrics {
        violations: std::sync::Mutex<Vec<(String, String)>>,
    }
    impl SpineMetrics for RecordingSpineMetrics {
        fn tenant_boundary_violation(&self, stage: &str, reason: &str) {
            self.violations
                .lock()
                .unwrap()
                .push((stage.to_string(), reason.to_string()));
        }
    }

    // Full-fidelity test of the hop-verification-rejects-and-dead-letters
    // path requires a live Valkey (SpineClient::dead_letter issues real
    // XADD/XACK commands) -- exercised at the `crate::hop` unit level
    // instead (Sec14.11 tests 1/4 there), and end-to-end in `crate::hop`'s
    // own suite. This test proves `interpret_dispatch_payload` and the
    // metrics/label plumbing `handle_delivered` depends on are correct in
    // isolation, satisfying the same property without a Valkey dependency
    // in this module's own test run.
    #[test]
    fn fixture_envelope_with_correct_mac_passes_verify_hop() {
        let ring = test_ring();
        let mac = mac_for(&ring, "acme");
        let d = fixture_delivered("acme", "waddles.bot.commands.default", mac, "k1");
        assert!(
            crate::hop::verify_hop(&ring, &d.env, &d.stream, "waddles.bot.commands.default")
                .is_ok()
        );
    }

    #[test]
    fn fixture_envelope_with_wrong_app_id_reader_fails_verify_hop() {
        let ring = test_ring();
        let mac = mac_for(&ring, "acme");
        let d = fixture_delivered("acme", "waddles.bot.commands.default", mac, "k1");
        let err = crate::hop::verify_hop(&ring, &d.env, &d.stream, "waddles.other.app.default")
            .unwrap_err();
        assert_eq!(err.as_metric_reason(), "grant_scope_mismatch");
    }

    #[tokio::test]
    async fn fake_audit_and_tenant_resolver_are_exercised_directly() {
        let audit = FakeAudit::default();
        let resolver = FixedTenantResolver;
        let record = DispatchRecord {
            target_type: "irc_relay".to_string(),
            status: "success",
            attempt: 1,
            http_status: None,
            detail: "ok".to_string(),
        };
        audit
            .record(1, Some(2), "waddles.bot.commands.default", &record, None)
            .await;
        assert_eq!(audit.records.lock().unwrap().len(), 1);
        let resolved = resolver.resolve("acme", Some("main")).await;
        assert_eq!(resolved, Some((1, Some(2))));
    }

    #[test]
    fn recording_spine_metrics_captures_violations() {
        let metrics = RecordingSpineMetrics::default();
        metrics.tenant_boundary_violation("action", "mac_mismatch");
        assert_eq!(
            *metrics.violations.lock().unwrap(),
            vec![("action".to_string(), "mac_mismatch".to_string())]
        );
    }

    // -- Full `handle_delivered` control-flow tests: real `host_api`
    // handshake/invoke mechanics over an in-memory duplex (no TLS, no live
    // executor process), fake `SpineOps`/`AuditSink`/`TenantResolver` (no
    // live Valkey/Postgres) -- see `SpineOps`'s doc for why the ack/DLQ
    // write itself is faked rather than requiring a live Valkey server in
    // this module's own test run.

    #[derive(Default)]
    struct FakeSpineOps {
        acked: std::sync::Mutex<Vec<String>>,
        /// `(entry_id, kind, consumer_id)` -- `consumer_id` is captured
        /// separately from `entry_id` so tests can assert `DlqError.
        /// consumer_id` is the pod identity, never the entry's own id
        /// (gh review: both DLQ sites previously set `consumer_id:
        /// d.entry_id.clone()`).
        dead_lettered: std::sync::Mutex<Vec<(String, penguin_spine::DlqErrorKind, String)>>,
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
            err: &'a penguin_spine::DlqError,
        ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<(), SpineError>> + Send + 'a>>
        {
            self.dead_lettered.lock().unwrap().push((
                d.entry_id.clone(),
                err.kind,
                err.consumer_id.clone(),
            ));
            Box::pin(async { Ok(()) })
        }
    }

    fn test_deps(
        spine: FakeSpineOps,
        connections: Arc<ConnectionRegistry>,
    ) -> DispatchDeps<FakeAudit, FixedTenantResolver, FakeSpineOps> {
        DispatchDeps {
            app_id: "waddles.bot.commands.default".to_string(),
            digest_source: DigestSource::Static("sha256:00".to_string()),
            config_json: "{}".to_string(),
            key_ring: test_ring(),
            connections,
            retry_policy: RetryPolicy {
                max_retries: 2,
                base_backoff_ms: 1,
                max_backoff_ms: 2,
                call_timeout_ms: 2000,
            },
            jitter: Jitter::seeded(1),
            audit: FakeAudit::default(),
            tenants: FixedTenantResolver,
            usage: Arc::new(Mutex::new(UsageBatcher::new())),
            consumer_id: "test-pod-consumer".to_string(),
            spine,
            metrics: Arc::new(RecordingSpineMetrics::default()),
        }
    }

    #[tokio::test]
    async fn handle_delivered_dead_letters_on_hop_verification_failure() {
        let ring = test_ring();
        // MAC computed for a DIFFERENT tenant than the stream key names --
        // Sec14.11 test 1's exact shape.
        let mac = mac_for(&ring, "acme");
        let d = fixture_delivered("acme", "waddles.bot.commands.default", mac, "k1");
        let wrong_key = "waddles:t:other-tenant:c:main:app:waddles.bot.commands.default:action";

        let spine = FakeSpineOps::default();
        let deps = test_deps(spine, Arc::new(ConnectionRegistry::new()));

        handle_delivered(&d, wrong_key, &deps).await.unwrap();

        assert_eq!(deps.spine.acked.lock().unwrap().len(), 0);
        let dead_lettered = deps.spine.dead_lettered.lock().unwrap();
        assert_eq!(dead_lettered.len(), 1);
        assert_eq!(
            dead_lettered[0].1,
            penguin_spine::DlqErrorKind::TenantBoundary
        );
        // gh review: `DlqError.consumer_id` must be the pod identity
        // (`DispatchDeps::consumer_id`, matching
        // `penguin_spine::client::claim_stale`'s convention), never the
        // entry's own `entry_id`.
        assert_eq!(dead_lettered[0].2, deps.consumer_id);
        assert_ne!(dead_lettered[0].2, d.entry_id);
    }

    #[tokio::test]
    async fn handle_delivered_dead_letters_when_no_executor_connection() {
        let ring = test_ring();
        let mac = mac_for(&ring, "acme");
        let d = fixture_delivered("acme", "waddles.bot.commands.default", mac, "k1");

        let spine = FakeSpineOps::default();
        // Empty registry: `.active()` returns `None`.
        let deps = test_deps(spine, Arc::new(ConnectionRegistry::new()));

        handle_delivered(&d, &d.stream.clone(), &deps)
            .await
            .unwrap();

        let dead_lettered = deps.spine.dead_lettered.lock().unwrap();
        assert_eq!(dead_lettered.len(), 1);
        assert_eq!(
            dead_lettered[0].1,
            penguin_spine::DlqErrorKind::ExecutorUnavailable
        );
        assert_eq!(dead_lettered[0].2, deps.consumer_id);
        assert_ne!(dead_lettered[0].2, d.entry_id);
    }

    /// Drives a fake executor over an in-memory duplex: completes the
    /// `hello`/`hello-ok` handshake, then answers exactly one `invoke` with
    /// `result.payload = response`.
    async fn connected_registry_with_fake_executor(
        response: serde_json::Value,
    ) -> Arc<ConnectionRegistry> {
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
                stage: "svc-action".to_string(),
                protocol_version: 1,
                limits: penguin_bundle_host::wire::HelloLimits {
                    call_timeout_ms: 2000,
                    memory_mb: 64,
                    max_concurrent_calls: 32,
                },
            },
            false,
            Arc::new(crate::capabilities::DenyAllCapabilities),
        )
        .await
        .expect("handshake succeeds");
        tokio::spawn(read_loop);

        let registry = Arc::new(ConnectionRegistry::new());
        registry.set_active(connection);
        registry
    }

    #[tokio::test]
    async fn handle_delivered_success_records_audit_usage_and_acks() {
        let ring = test_ring();
        let mac = mac_for(&ring, "acme");
        let d = fixture_delivered("acme", "waddles.bot.commands.default", mac, "k1");
        let stream_key = d.stream.clone();

        let connections = connected_registry_with_fake_executor(
            serde_json::json!({"ok": true, "status": 200, "detail": "sent"}),
        )
        .await;
        let spine = FakeSpineOps::default();
        let deps = test_deps(spine, connections);

        handle_delivered(&d, &stream_key, &deps).await.unwrap();

        assert_eq!(deps.spine.dead_lettered.lock().unwrap().len(), 0);
        assert_eq!(*deps.spine.acked.lock().unwrap(), vec![d.entry_id.clone()]);
        let records = deps.audit.records.lock().unwrap();
        assert_eq!(records.len(), 1);
        assert_eq!(records[0].status, "success");
        assert_eq!(deps.usage.lock().unwrap().pending_len(), 1);
    }

    // regression: svc-action had no multi-tenant dispatch consumers; replies
    // never sent after legacy env removal (alpha 2026-10-03)
    #[tokio::test]
    async fn handle_delivered_dead_letters_with_no_active_digest_and_never_invokes() {
        let ring = test_ring();
        let mac = mac_for(&ring, "acme");
        let d = fixture_delivered("acme", "waddles.bot.commands.default", mac, "k1");
        let stream_key = d.stream.clone();

        // Empty `ActiveDigests`: this (tenant, community, app) scope has no
        // active digest known -- `connections` is deliberately a registry
        // with NO fake executor at all, proving the digest check happens
        // (and dead-letters) BEFORE any invoke is ever attempted.
        let spine = FakeSpineOps::default();
        let mut deps = test_deps(spine, Arc::new(ConnectionRegistry::new()));
        deps.digest_source = DigestSource::Active {
            scope: (1, 0, "waddles.bot.commands.default".to_string()),
            digests: Arc::new(ActiveDigests::new()),
            sessions: Arc::new(LoadedSessions::new()),
        };

        handle_delivered(&d, &stream_key, &deps).await.unwrap();

        assert_eq!(deps.spine.acked.lock().unwrap().len(), 0);
        let dead_lettered = deps.spine.dead_lettered.lock().unwrap();
        assert_eq!(dead_lettered.len(), 1);
        assert_eq!(dead_lettered[0].1, penguin_spine::DlqErrorKind::BundleError);
        assert_eq!(dead_lettered[0].2, deps.consumer_id);
    }

    /// Fail-closed requirement: at least one executor is connected, but no
    /// live session has this scope's active digest loaded -- must
    /// dead-letter `NO_LOADED_EXECUTOR`, never fall back to `ConnectionRegistry::
    /// active()` and invoke a session that lacks the bundle (regression:
    /// bundles loaded only onto a terminating executor during rollout; live
    /// executor got none, alpha 2026-10-03).
    #[tokio::test]
    async fn handle_delivered_dead_letters_no_loaded_executor_when_no_session_has_the_digest() {
        let ring = test_ring();
        let mac = mac_for(&ring, "acme");
        let d = fixture_delivered("acme", "waddles.bot.commands.default", mac, "k1");
        let stream_key = d.stream.clone();

        // A connection IS live (unlike the "no executor at all" test above),
        // but `sessions` has nothing loaded for this scope/digest at all.
        let connections = connected_registry_with_fake_executor(
            serde_json::json!({"ok": true, "status": 200, "detail": "sent"}),
        )
        .await;
        let digests = Arc::new(ActiveDigests::new());
        digests.set(
            (1, 0, "waddles.bot.commands.default".to_string()),
            "sha256:aa".to_string(),
        );
        let spine = FakeSpineOps::default();
        let mut deps = test_deps(spine, connections);
        deps.digest_source = DigestSource::Active {
            scope: (1, 0, "waddles.bot.commands.default".to_string()),
            digests,
            sessions: Arc::new(LoadedSessions::new()),
        };

        handle_delivered(&d, &stream_key, &deps).await.unwrap();

        assert_eq!(deps.spine.acked.lock().unwrap().len(), 0);
        let dead_lettered = deps.spine.dead_lettered.lock().unwrap();
        assert_eq!(dead_lettered.len(), 1);
        assert_eq!(
            dead_lettered[0].1,
            penguin_spine::DlqErrorKind::ExecutorUnavailable
        );
    }

    // regression: svc-action had no multi-tenant dispatch consumers; replies
    // never sent after legacy env removal (alpha 2026-10-03)
    #[tokio::test]
    async fn handle_delivered_active_digest_source_invokes_with_the_scopes_active_digest() {
        let ring = test_ring();
        let mac = mac_for(&ring, "acme");
        let d = fixture_delivered("acme", "waddles.bot.commands.default", mac, "k1");
        let stream_key = d.stream.clone();

        let connections = connected_registry_with_fake_executor(
            serde_json::json!({"ok": true, "status": 200, "detail": "sent"}),
        )
        .await;
        let scope = (1, 0, "waddles.bot.commands.default".to_string());
        let digests = Arc::new(ActiveDigests::new());
        digests.set(scope.clone(), "sha256:aa".to_string());
        // `pick_session_with_digest` must find the fake executor's session
        // holding exactly this digest -- never fall back to `ConnectionRegistry::
        // active()` alone (regression: bundles loaded only onto a
        // terminating executor during rollout; live executor got none,
        // alpha 2026-10-03).
        let sessions = Arc::new(LoadedSessions::new());
        let session_id = connections
            .live_session_ids()
            .into_iter()
            .next()
            .expect("the fake executor registered exactly one live session");
        sessions.mark_loaded(session_id, scope.clone(), "sha256:aa".to_string());
        let spine = FakeSpineOps::default();
        let mut deps = test_deps(spine, connections);
        deps.digest_source = DigestSource::Active {
            scope,
            digests,
            sessions,
        };

        handle_delivered(&d, &stream_key, &deps).await.unwrap();

        assert_eq!(deps.spine.dead_lettered.lock().unwrap().len(), 0);
        assert_eq!(*deps.spine.acked.lock().unwrap(), vec![d.entry_id.clone()]);
    }

    /// A [`crate::usage::UsageSink`] that records every delta it is asked
    /// to write -- the "mock sink asserting flush is called" fallback the
    /// review comment calls for (no live-Valkey test-container harness
    /// exists in this workspace yet), proving the *whole* chain end to
    /// end: `handle_delivered` -> `UsageBatcher::record_action_delivered`
    /// -> `UsageBatcher::flush` -> `UsageSink::write` (which
    /// `RedisUsageSink::write`, the real implementation, backs with a
    /// literal `XADD waddles:usage`, spec §5.12/D31). Regression coverage
    /// for the CRITICAL finding: usage deltas previously accumulated in
    /// `DispatchDeps::usage` forever because nothing ever called
    /// `UsageBatcher::flush` in production.
    #[derive(Default)]
    struct RecordingUsageSink {
        written: std::sync::Mutex<Vec<crate::usage::UsageDelta>>,
    }

    impl crate::usage::UsageSink for RecordingUsageSink {
        fn write(
            &self,
            delta: &crate::usage::UsageDelta,
        ) -> std::pin::Pin<
            Box<dyn std::future::Future<Output = Result<(), crate::usage::UsageError>> + Send + '_>,
        > {
            let delta = delta.clone();
            Box::pin(async move {
                self.written.lock().unwrap().push(delta);
                Ok(())
            })
        }
    }

    #[tokio::test]
    async fn a_delivered_action_flushes_to_the_usage_sink_as_an_xadd_ready_delta() {
        let ring = test_ring();
        let mac = mac_for(&ring, "acme");
        let d = fixture_delivered("acme", "waddles.bot.commands.default", mac, "k1");
        let stream_key = d.stream.clone();

        let connections = connected_registry_with_fake_executor(
            serde_json::json!({"ok": true, "status": 200, "detail": "sent"}),
        )
        .await;
        let spine = FakeSpineOps::default();
        let deps = test_deps(spine, connections);

        handle_delivered(&d, &stream_key, &deps).await.unwrap();
        assert_eq!(deps.usage.lock().unwrap().pending_len(), 1);

        let sink = RecordingUsageSink::default();
        // Don't hold the `MutexGuard` across `.await` (clippy::
        // await_holding_lock) -- take the batcher out synchronously first,
        // matching `crate::lib`'s own production flush-loop pattern.
        let mut batcher = std::mem::take(&mut *deps.usage.lock().unwrap());
        let flushed = batcher.flush(&sink).await;

        assert_eq!(flushed, 1, "the delivered action's delta must flush");
        assert_eq!(batcher.pending_len(), 0);
        let written = sink.written.lock().unwrap();
        assert_eq!(written.len(), 1);
        assert_eq!(written[0].tenant_id, "acme");
        assert_eq!(written[0].app_id, "waddles.bot.commands.default");
        assert_eq!(written[0].actions_delivered, 1);
    }

    #[tokio::test]
    async fn handle_delivered_non_retryable_failure_records_audit_and_acks() {
        let ring = test_ring();
        let mac = mac_for(&ring, "acme");
        let d = fixture_delivered("acme", "waddles.bot.commands.default", mac, "k1");
        let stream_key = d.stream.clone();

        let connections = connected_registry_with_fake_executor(
            serde_json::json!({"transport_error": {"retryable": false, "code": "BAD_REQUEST", "message": "bad"}}),
        )
        .await;
        let spine = FakeSpineOps::default();
        let deps = test_deps(spine, connections);

        handle_delivered(&d, &stream_key, &deps).await.unwrap();

        assert_eq!(deps.spine.dead_lettered.lock().unwrap().len(), 0);
        assert_eq!(deps.spine.acked.lock().unwrap().len(), 1);
        let records = deps.audit.records.lock().unwrap();
        assert_eq!(records[0].status, "non_retryable_failure");
        assert_eq!(records[0].attempt, 1);
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
        let mac = mac_for(&ring, "acme");
        let d = fixture_delivered("acme", "waddles.bot.commands.default", mac, "k1");
        let stream_key = d.stream.clone();
        let connections = connected_registry_with_fake_executor(
            serde_json::json!({"ok": true, "status": 200, "detail": "sent"}),
        )
        .await;
        let spine = FakeSpineOps::default();
        let deps = test_deps(spine, connections);
        let mut reader = OneShotReader {
            batch: Some(vec![d.clone()]),
        };

        let count = drain_batch(&mut reader, &stream_key, &deps).await.unwrap();
        assert_eq!(count, 1);
        assert_eq!(deps.spine.acked.lock().unwrap().len(), 1);
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
            drain_loop(
                EmptyReader,
                "unused".to_string(),
                deps,
                crate::flags::boxed(crate::flags::StaticFlag(true)),
                rx,
            ),
        )
        .await
        .expect("drain_loop must return promptly once shutdown resolves");
        assert!(result.is_ok());
    }

    /// Spec §13.5's `waddles.core.rust-data-plane` gate: OFF ⇒ "drains
    /// nothing". A reader that *would* hand back an entry on every call
    /// tracks how many times it was actually invoked; if the gate were not
    /// enforced, `drain_batch` (and therefore `reader.read()`) would run
    /// repeatedly. It is never called at all while the flag is off.
    #[tokio::test]
    async fn drain_loop_with_the_flag_off_never_calls_drain_batch() {
        let ring = test_ring();
        let mac = mac_for(&ring, "acme");
        let d = fixture_delivered("acme", "waddles.bot.commands.default", mac, "k1");

        struct CountingReader {
            calls: Arc<std::sync::atomic::AtomicUsize>,
            entry: Delivered,
        }
        impl StreamReader for CountingReader {
            async fn read(&mut self) -> Result<Vec<Delivered>, SpineError> {
                self.calls.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                Ok(vec![self.entry.clone()])
            }
        }

        let calls = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let reader = CountingReader {
            calls: Arc::clone(&calls),
            entry: d,
        };
        let spine = FakeSpineOps::default();
        let deps = test_deps(spine, Arc::new(ConnectionRegistry::new()));
        let (tx, rx) = tokio::sync::oneshot::channel();
        tokio::spawn(async move {
            tokio::time::sleep(std::time::Duration::from_millis(150)).await;
            let _ = tx.send(());
        });
        let result = tokio::time::timeout(
            std::time::Duration::from_secs(5),
            drain_loop(
                reader,
                "unused".to_string(),
                deps,
                crate::flags::boxed(crate::flags::StaticFlag(false)),
                rx,
            ),
        )
        .await
        .expect("drain_loop must still respond to shutdown while the flag is off");
        assert!(result.is_ok());
        assert_eq!(
            calls.load(std::sync::atomic::Ordering::SeqCst),
            0,
            "reader.read() must never be called while the flag is off"
        );
    }

    #[tokio::test]
    async fn ensure_loaded_sends_load_and_returns_the_loaded_reply() {
        use penguin_bundle_host::wire::{
            read_frame, write_frame, Frame, HelloBody, HelloOkBody, LoadedBody, SandboxInfo,
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
            let load_body = match load.message {
                Message::Load(b) => b,
                other => panic!("expected load, got {other:?}"),
            };
            write_frame(
                &mut executor_io,
                &Frame::new(
                    load.id,
                    Message::Loaded(LoadedBody {
                        app_id: load_body.app_id,
                        digest: load_body.digest,
                        precompile_ms: 5,
                        exports: vec!["dispatch".to_string()],
                    }),
                ),
            )
            .await
            .unwrap();
        });

        let (connection, read_loop) = crate::host_api::run_connection(
            stage_io,
            HelloOkBody {
                stage: "svc-action".to_string(),
                protocol_version: 1,
                limits: penguin_bundle_host::wire::HelloLimits {
                    call_timeout_ms: 2000,
                    memory_mb: 64,
                    max_concurrent_calls: 32,
                },
            },
            false,
            Arc::new(crate::capabilities::DenyAllCapabilities),
        )
        .await
        .expect("handshake succeeds");
        tokio::spawn(read_loop);

        let loaded = ensure_loaded(
            &connection,
            1,
            0,
            "waddles.bot.commands.default",
            "1",
            "sha256:00",
            "component-key",
            "sidecar-key",
            LoadLimits {
                timeout_ms: 2000,
                memory_mb: 64,
            },
        )
        .await
        .expect("load succeeds");
        assert_eq!(loaded.app_id, "waddles.bot.commands.default");
        assert_eq!(loaded.exports, vec!["dispatch".to_string()]);
    }

    /// `Message::Error` reply to a `load` request maps to
    /// `InvokeError::ExecutorError` -- distinct from the `Loaded` success
    /// path above, never previously exercised.
    #[tokio::test]
    async fn ensure_loaded_returns_executor_error_on_error_reply() {
        use penguin_bundle_host::wire::{
            read_frame, write_frame, ErrorBody, ErrorCode, Frame, HelloBody, HelloOkBody,
            SandboxInfo,
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
                        code: ErrorCode::DigestMismatch,
                        message: "digest mismatch".to_string(),
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
                stage: "svc-action".to_string(),
                protocol_version: 1,
                limits: penguin_bundle_host::wire::HelloLimits {
                    call_timeout_ms: 2000,
                    memory_mb: 64,
                    max_concurrent_calls: 32,
                },
            },
            false,
            Arc::new(crate::capabilities::DenyAllCapabilities),
        )
        .await
        .expect("handshake succeeds");
        tokio::spawn(read_loop);

        let err = ensure_loaded(
            &connection,
            1,
            0,
            "waddles.bot.commands.default",
            "1",
            "sha256:00",
            "component-key",
            "sidecar-key",
            LoadLimits {
                timeout_ms: 2000,
                memory_mb: 64,
            },
        )
        .await
        .expect_err("executor error reply must surface as an error");
        match err {
            InvokeError::ExecutorError { code, message } => {
                assert_eq!(code, "DigestMismatch");
                assert_eq!(message, "digest mismatch");
            }
            other => panic!("expected ExecutorError, got {other:?}"),
        }
    }

    /// `ensure_unloaded`'s own success path -- never previously exercised
    /// (mirrors `ensure_loaded_sends_load_and_returns_the_loaded_reply`
    /// above exactly, swapped for `unload`/`Unloaded`).
    #[tokio::test]
    async fn ensure_unloaded_sends_unload_and_returns_the_unloaded_reply() {
        use penguin_bundle_host::wire::{
            read_frame, write_frame, Frame, HelloBody, HelloOkBody, SandboxInfo, UnloadedBody,
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

            let unload = read_frame(&mut executor_io).await.unwrap();
            let unload_body = match unload.message {
                Message::Unload(b) => b,
                other => panic!("expected unload, got {other:?}"),
            };
            write_frame(
                &mut executor_io,
                &Frame::new(
                    unload.id,
                    Message::Unloaded(UnloadedBody {
                        app_id: unload_body.app_id,
                        digest: unload_body.digest,
                    }),
                ),
            )
            .await
            .unwrap();
        });

        let (connection, read_loop) = crate::host_api::run_connection(
            stage_io,
            HelloOkBody {
                stage: "svc-action".to_string(),
                protocol_version: 1,
                limits: penguin_bundle_host::wire::HelloLimits {
                    call_timeout_ms: 2000,
                    memory_mb: 64,
                    max_concurrent_calls: 32,
                },
            },
            false,
            Arc::new(crate::capabilities::DenyAllCapabilities),
        )
        .await
        .expect("handshake succeeds");
        tokio::spawn(read_loop);

        let unloaded = ensure_unloaded(
            &connection,
            1,
            0,
            "waddles.bot.commands.default",
            "sha256:00",
        )
        .await
        .expect("unload succeeds");
        assert_eq!(unloaded.app_id, "waddles.bot.commands.default");
        assert_eq!(unloaded.digest, "sha256:00");
    }
}
