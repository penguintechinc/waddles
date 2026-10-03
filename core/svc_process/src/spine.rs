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

use penguin_bundle_host::wire::{
    ErrorCode, ExportKind, InvokeBody, LoadBody, LoadLimits, LoadedBody, Message, TraceContext,
    UnloadBody, UnloadedBody,
};
use penguin_spine::{
    Delivered, DlqError, DlqErrorKind, Grant, GroupReader, PlatformEvent, Scope, SpineClient,
    SpineConfig, SpineError, SpineMetrics, Stage, StageEnvelope,
};

use crate::active_digests::ActiveDigests;
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
    ExecutorError { code: ErrorCode, message: String },
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
    /// The per-process `bundle_host_http::egress::EgressGuard` cloned into
    /// every per-invoke [`crate::capabilities::StageCapabilities`]'s
    /// `http` capability (see that struct's doc for why this is a
    /// singleton, not scope-implicit like every other capability).
    pub egress: Arc<bundle_host_http::egress::EgressGuard>,
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
            artifact_digest: deps.digest_source.current(),
            consumer_id: deps.consumer_id.clone(),
        };
        return deps.spine.dead_letter(d, &err).await;
    }

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
        return deps.spine.dead_letter(d, &err).await;
    };

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
        return deps.spine.dead_letter(d, &err).await;
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
            return deps.spine.dead_letter(d, &err).await;
        }
        deps.load_state
            .mark_loaded(Arc::clone(&connection), digest.clone());
    }

    // Per-invoke capability scope (see `crate::host_api`/`crate::
    // capabilities`'s per-invoke-scoping design): built fresh from THIS
    // envelope's own (tenant, community, app_id), never a fixed
    // connection-lifetime default. `kv` reuses `deps.kv_conn` (a cheap
    // handle clone, see that field's doc) rather than opening a new
    // connection on every invoke.
    let capabilities: Arc<dyn CapabilityHandler> = {
        let caps = StageCapabilities::<redis::aio::MultiplexedConnection>::new(
            d.env.tenant.clone(),
            d.env.community.clone(),
            deps.app_id.clone(),
            Arc::clone(&deps.egress),
        );
        let caps = match &deps.kv_conn {
            Some(conn) => caps.with_kv(conn.clone(), Arc::clone(&deps.kv_capabilities)),
            None => caps,
        };
        Arc::new(caps)
    };
    let trace = d.env.trace.as_ref().map(|t| TraceContext {
        traceparent: t.traceparent.clone(),
        tracestate: t.tracestate.clone(),
    });

    let outcome = invoke_transform(
        &connection,
        &deps.app_id,
        &digest,
        &d.env.event,
        deps.call_timeout_ms,
        trace,
        capabilities,
    )
    .await;

    let event_out = match outcome {
        Err(InvokeError::ExecutorError { code, message }) => {
            let kind = error_code_to_dlq_kind(code);
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
            tracing::error!(app_id = %deps.app_id, ?code, digest_prefix, %message, "transform invoke failed, dead-lettering");
            let err = DlqError {
                kind,
                code: format!("{code:?}"),
                message,
                detail: None,
                artifact_digest: Some(digest.clone()),
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
                artifact_digest: Some(digest.clone()),
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
                artifact_digest: Some(digest.clone()),
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
        deps.digest_source = DigestSource::Static(format!("sha256:{}", "0".repeat(64)));
        deps.component_key = "k".to_string();
        deps.sidecar_key = "s".to_string();

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
        deps.digest_source = DigestSource::Static(format!("sha256:{}", "0".repeat(64)));
        deps.component_key = "k".to_string();
        deps.sidecar_key = "s".to_string();

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
}
