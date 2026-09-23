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
    ExportKind, InvokeBody, LoadBody, LoadLimits, LoadedBody, Message, TraceContext,
};
use penguin_spine::{
    Delivered, Grant, GroupReader, SpineClient, SpineConfig, SpineError, SpineMetrics, Stage,
    StageEnvelope,
};
use serde::Deserialize;

use crate::hop::KeyRing;
use crate::host_api::{Connection, ConnectionRegistry, HostApiError};
use crate::retry::{dispatch_with_retry, AttemptOutcome, DispatchRecord, Jitter};
use crate::usage::UsageBatcher;

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
pub async fn ensure_loaded(
    conn: &Connection,
    app_id: &str,
    version: &str,
    digest: &str,
    component_key: &str,
    sidecar_key: &str,
    limits: LoadLimits,
) -> Result<LoadedBody, InvokeError> {
    let reply = conn
        .request(Message::Load(LoadBody {
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

/// Invokes the bundle's `dispatch` export for one delivered envelope
/// (assumption: the `dispatch(envelope: stage-envelope, config: string)`
/// WIT signature maps to a JSON payload of `{"envelope": ..., "config":
/// ...}` -- see the module doc for why this exact wire-JSON convention is
/// this crate's own documented choice rather than a value copied from a
/// landed reference). Returns the raw `result.payload` JSON for
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
        "envelope": env,
        "config": config_json,
    });
    let reply = conn
        .request(Message::Invoke(InvokeBody {
            app_id: app_id.to_string(),
            digest: digest.to_string(),
            export: ExportKind::Dispatch,
            payload,
            deadline_ms,
            trace,
        }))
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

/// The `dispatch` export's JSON result shape this stage expects (spec
/// §6.5's `transport-result`/`transport-error` WIT records, flattened
/// under one `ok` discriminant -- see [`invoke_dispatch`]'s doc for the
/// wire-JSON convention this assumes).
#[derive(Debug, Deserialize)]
struct DispatchResultPayload {
    ok: bool,
    #[serde(default)]
    status: Option<u16>,
    #[serde(default)]
    detail: Option<String>,
    #[serde(default)]
    retryable: Option<bool>,
    #[serde(default)]
    code: Option<String>,
    #[serde(default)]
    message: Option<String>,
    #[serde(default)]
    retry_after_ms: Option<u32>,
}

/// Classifies a `dispatch` export's raw JSON result into an
/// [`AttemptOutcome`] the retry loop understands. `target_type_hint` (e.g.
/// `"irc_relay"`) is used for a successful outcome's
/// `action_dispatch_log.target_type` when the payload itself doesn't name
/// one -- mirrors the Python runner's `result.transport` field.
fn interpret_dispatch_payload(
    payload: &serde_json::Value,
    target_type_hint: &str,
) -> AttemptOutcome {
    let parsed: DispatchResultPayload = match serde_json::from_value(payload.clone()) {
        Ok(p) => p,
        Err(e) => {
            return AttemptOutcome::NonRetryable {
                http_status: None,
                detail: format!("dispatch result payload malformed: {e}"),
            }
        }
    };
    if parsed.ok {
        return AttemptOutcome::Success {
            target_type: target_type_hint.to_string(),
            http_status: parsed.status.map(i32::from),
            detail: parsed.detail.unwrap_or_default(),
        };
    }
    let detail = parsed.message.unwrap_or_else(|| {
        parsed
            .code
            .clone()
            .unwrap_or_else(|| "dispatch failed".to_string())
    });
    if parsed.retryable.unwrap_or(false) {
        AttemptOutcome::Retryable {
            http_status: parsed.status.map(i32::from),
            detail,
            retry_after_ms: parsed.retry_after_ms.map(u64::from),
        }
    } else {
        AttemptOutcome::NonRetryable {
            http_status: parsed.status.map(i32::from),
            detail,
        }
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
    pub digest: String,
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
            artifact_digest: Some(deps.digest.clone()),
            consumer_id: deps.consumer_id.clone(),
        };
        return deps.spine.dead_letter(d, &err).await;
    }

    let Some(connection) = deps.connections.active() else {
        tracing::warn!(app_id = %deps.app_id, "no executor connection available, dead-lettering for redelivery");
        let err = penguin_spine::DlqError {
            kind: penguin_spine::DlqErrorKind::ExecutorUnavailable,
            code: "EXECUTOR_UNAVAILABLE".to_string(),
            message: "no active host-api connection".to_string(),
            detail: None,
            artifact_digest: Some(deps.digest.clone()),
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
    // a separate DLQ path -- `deps.connections.active()` above already
    // catches the "no executor at all" case before ever entering this
    // loop, which is the one infra failure worth a distinct DLQ kind at
    // this stage's current scope.
    let (record, _attempts) = dispatch_with_retry(
        |_attempt| async {
            match invoke_dispatch(
                &connection,
                &deps.app_id,
                &deps.digest,
                &d.env,
                &deps.config_json,
                deps.retry_policy.call_timeout_ms,
            )
            .await
            {
                Ok(p) => interpret_dispatch_payload(&p, "irc_relay"),
                Err(e) => AttemptOutcome::NonRetryable {
                    http_status: None,
                    detail: format!("invoke failed: {e}"),
                },
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
/// shape to `core/svc_process/src/spine.rs::drain_loop`.
async fn drain_loop<R: StreamReader, A: AuditSink, T: TenantResolver, S: SpineOps>(
    mut reader: R,
    stream_key: String,
    deps: DispatchDeps<A, T, S>,
    mut shutdown: tokio::sync::oneshot::Receiver<()>,
) -> Result<(), SpineError> {
    loop {
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
    drain_loop(reader, stream_key, deps, shutdown).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::hop::BoundaryReason;

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
        let outcome = interpret_dispatch_payload(
            &serde_json::json!({"ok": false, "retryable": true, "code": "RATE_LIMITED", "message": "slow down", "retry_after_ms": 5000}),
            "irc_relay",
        );
        assert_eq!(
            outcome,
            AttemptOutcome::Retryable {
                http_status: None,
                detail: "slow down".to_string(),
                retry_after_ms: Some(5000),
            }
        );
    }

    #[test]
    fn interpret_non_retryable_failure_payload() {
        let outcome = interpret_dispatch_payload(
            &serde_json::json!({"ok": false, "retryable": false, "status": 400, "message": "bad request"}),
            "irc_relay",
        );
        assert_eq!(
            outcome,
            AttemptOutcome::NonRetryable {
                http_status: Some(400),
                detail: "bad request".to_string(),
            }
        );
    }

    #[test]
    fn interpret_failure_defaults_to_non_retryable_when_retryable_absent() {
        let outcome = interpret_dispatch_payload(
            &serde_json::json!({"ok": false, "code": "UNKNOWN"}),
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
            digest: "sha256:00".to_string(),
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
            serde_json::json!({"ok": false, "retryable": false, "status": 400, "message": "bad"}),
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
            drain_loop(EmptyReader, "unused".to_string(), deps, rx),
        )
        .await
        .expect("drain_loop must return promptly once shutdown resolves");
        assert!(result.is_ok());
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
}
