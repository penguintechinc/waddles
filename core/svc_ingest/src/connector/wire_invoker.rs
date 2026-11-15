//! The production [`ConnectorInvoker`]: connector `receiver` exports carried
//! over the executor host-API wire (`penguin-bundle-host` 0.3.0,
//! `ExportKind::Connector*`), replacing the old fail-loud placeholder.
//!
//! ```text
//!   ConnectorDriver --> WireConnectorInvoker --invoke frame--> bundle_executor
//!                              |                                (ConnectorHost)
//!                          ExecutorLink  <==== executor dials, speaks first ====
//! ```
//!
//! # Roles
//!
//! The executor *dials* the stage and opens with `hello` (spec S6.6), so
//! svc_ingest is the listening side. [`ExecutorLink`] owns that side of one
//! accepted connection: [`ExecutorLink::attach`] runs the `hello`/`hello-ok`
//! handshake over any accepted duplex stream (the TLS/mTLS accept loop is the
//! caller's -- it arrives with the first real transport) and then multiplexes
//! `invoke` request/reply pairs by correlation id. [`WireConnectorInvoker`]
//! resolves a platform to its pinned `(app_id, digest)` in the
//! [`ConnectorRegistry`] and speaks the typed payloads of the wire contract
//! (`bundle_executor::connector_wire` documents the exact JSON).
//!
//! # Failure classes (spec S2.6), never a silent fallback
//!
//! | Wire outcome | [`ConnectorInvokeError`] |
//! |---|---|
//! | `result` with `{"frame_error"}` | `Guest` (connection stays up) |
//! | `error` `EXECUTOR_DEADLINE` (+`fuel_exhausted`) / `MEMORY_LIMIT` / `WASM_TRAP` | `SandboxTrip` (`deadline`/`fuel_exhausted`/`memory_limit`/`trap`) |
//! | `error` `EXECUTOR_DEADLINE` + `pool_exhausted`, any other code | `Unavailable` |
//! | no executor attached, connection lost, reply timeout, malformed reply | `Unavailable` |
//!
//! A connector that cannot be reached is therefore *always* a typed, counted
//! `Unavailable` that tears the connection down under backoff -- the same
//! fail-loud contract the retired placeholder had, now backed by a live path.
//!
//! # Logging
//!
//! Only the export label, correlation id and error class are ever logged --
//! never frame bytes, actors, payloads, or executor/guest-supplied text.

use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine as _;
use penguin_bundle_host::wire::message::PROTOCOL_VERSION;
use penguin_bundle_host::wire::{
    read_frame, write_frame, CapabilityKind, ErrorBody, ErrorCode, ExportKind, Frame, HelloLimits,
    HelloOkBody, HostResultBody, HostResultError, InvokeBody, Message, ResultBody,
};
use serde::Deserialize;
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::sync::{mpsc, oneshot};
use tracing::{debug, info, warn};

use super::host_calls::{CallOrigin, HostCallHandler};
use super::invoker::{ConnectorInvokeError, ConnectorInvoker};
use super::registry::ConnectorRegistry;
use super::types::{ConnectionCtx, ConnectorEvent, ConnectorFrameError, Handshake, RawFrame};

/// Per-call wall-clock budget sent as `invoke.deadline_ms` and awaited.
pub const DEFAULT_CALL_TIMEOUT: Duration = Duration::from_secs(5);
/// Extra wait beyond `deadline_ms` for the reply to cross the wire.
const REPLY_GRACE: Duration = Duration::from_secs(2);
/// How long the executor has to open with `hello` once attached.
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(10);
/// Bound on a write reaching the writer task (back-pressure, never unbounded).
const WRITE_TIMEOUT: Duration = Duration::from_secs(5);
/// Outbound frames buffered per session before a caller waits.
const WRITER_CHANNEL_CAPACITY: usize = 256;
/// Default cap on invocations awaiting a reply on one session.
pub const DEFAULT_MAX_IN_FLIGHT: usize = 256;
/// `hello-ok` stage name.
const STAGE_NAME: &str = "svc-ingest";
/// `hello-ok` memory figure (informational; connectors are bounded by the
/// executor's own `ConnectorLimits`).
const HELLO_MEMORY_MB: u32 = 64;
/// `HostResultError::code` for every host-call a connector makes: svc_ingest
/// services no connector host capability over this link yet.
const HOST_CALL_DENIED: &str = "HOST_CALL_DENIED";

/// Why attaching an executor connection failed. Typed and loud.
#[derive(Debug, thiserror::Error)]
pub enum AttachError {
    /// Reading or writing the handshake frames failed.
    #[error("host-api handshake I/O failed: {0}")]
    Io(String),
    /// The executor did not open with `hello` in time.
    #[error("host-api handshake timed out")]
    Timeout,
    /// The first frame was not a `hello`.
    #[error("host-api handshake expected `hello`, got another frame")]
    UnexpectedFrame,
    /// The executor speaks a different protocol version.
    #[error("host-api protocol version {0} is not supported (want {PROTOCOL_VERSION})")]
    ProtocolVersion(u32),
    /// The link already holds [`MAX_SESSIONS`] live executor sessions.
    #[error("host-api link already holds the maximum number of executor sessions")]
    TooManySessions,
}

/// An executor's answer to one `invoke`: its `result` or typed `error`.
type Reply = Result<ResultBody, ErrorBody>;

/// One awaiting invocation: where its reply goes and what it is, as the HOST
/// recorded it when sending. The origin is how a host-call arriving during this
/// invocation (its `call_id` is this entry's id) is attributed -- never guest data.
struct Pending {
    tx: oneshot::Sender<Reply>,
    origin: CallOrigin,
}

/// One accepted executor connection's request/reply bookkeeping.
struct Session {
    writer: mpsc::Sender<Frame>,
    pending: Mutex<HashMap<u64, Pending>>,
    next_id: AtomicU64,
    closed: AtomicBool,
    max_in_flight: usize,
    /// Scope-checked host-call servicing; `None` keeps the link deny-all.
    host_calls: Option<Arc<dyn HostCallHandler>>,
    /// Flag keys the host answers `flags.enabled` with `true` (see
    /// [`ExecutorLink::with_enabled_flags`]).
    enabled_flags: Arc<HashSet<String>>,
}

/// Releases one reply slot on drop so a cancelled call cannot leak it.
struct SlotGuard<'a> {
    session: &'a Session,
    id: u64,
}

impl Drop for SlotGuard<'_> {
    fn drop(&mut self) {
        self.session.forget(self.id);
    }
}

impl Session {
    /// Marks the session closed and fails every awaiting call (their senders
    /// drop, which callers see as a lost connection). Idempotent.
    fn close(&self) {
        self.closed.store(true, Ordering::SeqCst);
        match self.pending.lock() {
            Ok(mut pending) => pending.clear(),
            // A poisoned table can no longer be trusted; the session is
            // closed so nothing new registers against it.
            Err(poisoned) => poisoned.into_inner().clear(),
        }
    }

    fn is_closed(&self) -> bool {
        self.closed.load(Ordering::SeqCst)
    }

    /// Registers a reply slot for a fresh correlation id, refusing when the
    /// session is closed or already at its in-flight cap.
    fn register(
        &self,
        origin: CallOrigin,
    ) -> Result<(u64, oneshot::Receiver<Reply>), ConnectorInvokeError> {
        if self.is_closed() {
            return Err(unavailable("executor connection is closed"));
        }
        let mut pending = self
            .pending
            .lock()
            .map_err(|_| unavailable("executor session state is unusable"))?;
        if pending.len() >= self.max_in_flight {
            return Err(unavailable("too many in-flight connector invocations"));
        }
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let (tx, rx) = oneshot::channel();
        pending.insert(id, Pending { tx, origin });
        Ok((id, rx))
    }

    /// Drops a reply slot (timeout / failed send). Returns nothing: a missing
    /// slot just means the reply already won the race.
    fn forget(&self, id: u64) {
        if let Ok(mut pending) = self.pending.lock() {
            pending.remove(&id);
        }
    }

    /// The host-recorded origin of the invocation `call_id` names. An unknown
    /// or already-finished id is [`CallOrigin::Other`]: it unlocks nothing.
    fn origin_of(&self, call_id: u64) -> CallOrigin {
        match self.pending.lock() {
            Ok(pending) => pending
                .get(&call_id)
                .map_or(CallOrigin::Other, |p| p.origin.clone()),
            Err(_) => CallOrigin::Other,
        }
    }

    /// Routes a `result`/`error` reply to the call awaiting `id`. An unknown
    /// id (a reply after its call timed out) is logged and dropped.
    fn deliver(&self, id: u64, reply: Reply) {
        let slot = match self.pending.lock() {
            Ok(mut pending) => pending.remove(&id),
            Err(_) => None,
        };
        match slot {
            Some(p) => {
                // The caller may have just timed out; nothing to do then.
                let _ = p.tx.send(reply);
            }
            None => warn!(
                id,
                "host-api reply for an unknown or expired invocation dropped"
            ),
        }
    }
}

/// Most executor sessions one link holds at once. One session per executor
/// replica (plus a transient overlap while a replica reconnects); the cap
/// bounds memory if a misbehaving peer keeps dialing.
const MAX_SESSIONS: usize = 64;

/// svc_ingest's side of the executor host-API connection: one live
/// [`Session`] per attached executor replica. Each `invoke` goes to exactly ONE
/// session, chosen round-robin, so N replicas share the connector work without
/// any item reaching two executors. A replica that reconnects simply adds a
/// fresh session; the old one is dropped when its connection ends.
pub struct ExecutorLink {
    sessions: Mutex<Vec<Arc<Session>>>,
    rotation: AtomicU64,
    call_timeout: Duration,
    max_in_flight: usize,
    enabled_flags: Arc<HashSet<String>>,
    host_calls: Option<Arc<dyn HostCallHandler>>,
}

impl ExecutorLink {
    /// A link with no executor attached; every call is a typed
    /// `Unavailable` until [`ExecutorLink::attach`] succeeds.
    #[must_use]
    pub fn new() -> Self {
        Self {
            sessions: Mutex::new(Vec::new()),
            rotation: AtomicU64::new(0),
            call_timeout: DEFAULT_CALL_TIMEOUT,
            max_in_flight: DEFAULT_MAX_IN_FLIGHT,
            enabled_flags: Arc::new(HashSet::new()),
            host_calls: None,
        }
    }

    /// Plugs in the scope-checked host-call handler. Without it every
    /// connector host-call stays refused (deny all). With it, EVERY host-call
    /// (including `flags`) is decided by the handler: unscoped calls are denied.
    #[must_use]
    pub fn with_host_calls(mut self, handler: Arc<dyn HostCallHandler>) -> Self {
        self.host_calls = Some(handler);
        self
    }

    /// The flag keys a connector's `flags.enabled` host-call is answered
    /// `true` for (every other key, and every other host capability, is
    /// refused / answered with the guest's own default). Boot passes the
    /// platform connector cutover flags here when the generic outbound executor
    /// is ON: the connectors gate `sender.build-request` on them, and the
    /// executor path is the host's decision to use the connector.
    #[must_use]
    pub fn with_enabled_flags<I, S>(mut self, keys: I) -> Self
    where
        I: IntoIterator<Item = S>,
        S: Into<String>,
    {
        self.enabled_flags = Arc::new(keys.into_iter().map(Into::into).collect());
        self
    }

    /// Overrides the per-call wall-clock budget (sent as `deadline_ms`).
    #[must_use]
    pub fn with_call_timeout(mut self, call_timeout: Duration) -> Self {
        self.call_timeout = call_timeout;
        self
    }

    /// Overrides the cap on invocations awaiting a reply.
    #[must_use]
    pub fn with_max_in_flight(mut self, max_in_flight: usize) -> Self {
        self.max_in_flight = max_in_flight.max(1);
        self
    }

    /// True when at least one live (not closed) executor session is attached.
    #[must_use]
    pub fn is_attached(&self) -> bool {
        self.attached_count() > 0
    }

    /// How many live (not closed) executor sessions are attached -- one per
    /// connected executor replica.
    #[must_use]
    pub fn attached_count(&self) -> usize {
        match self.sessions.lock() {
            Ok(guard) => guard.iter().filter(|s| !s.is_closed()).count(),
            Err(poisoned) => poisoned
                .into_inner()
                .iter()
                .filter(|s| !s.is_closed())
                .count(),
        }
    }

    /// The live sessions in the order the next `invoke` should try them:
    /// rotated by a per-call round-robin turn so successive calls start at
    /// successive replicas. A caller tries the first and only moves to the next
    /// when the first REFUSED the hand-off (cap hit / writer gone), so one work
    /// item is still executed by at most one executor.
    fn ordered_sessions(&self) -> Vec<Arc<Session>> {
        let guard = match self.sessions.lock() {
            Ok(guard) => guard,
            Err(poisoned) => poisoned.into_inner(),
        };
        let mut live: Vec<Arc<Session>> =
            guard.iter().filter(|s| !s.is_closed()).cloned().collect();
        if live.is_empty() {
            return live;
        }
        let turn = self.rotation.fetch_add(1, Ordering::Relaxed);
        let start = usize::try_from(turn % live.len() as u64).unwrap_or(0);
        live.rotate_left(start);
        live
    }

    /// Runs the `hello`/`hello-ok` handshake on `io` (an accepted executor
    /// connection), then serves it in the background: replies are routed to
    /// their callers, `ping` gets `pong`, and any `host-call` is refused
    /// typed. The session is ADDED alongside the other replicas' sessions (it
    /// never replaces or closes them); at most [`MAX_SESSIONS`] are held and a
    /// connection past that is refused. Returns once the handshake completed.
    pub async fn attach<IO>(self: &Arc<Self>, io: IO) -> Result<(), AttachError>
    where
        IO: AsyncRead + AsyncWrite + Unpin + Send + 'static,
    {
        let (mut reader, mut writer) = tokio::io::split(io);
        let hello = tokio::time::timeout(HANDSHAKE_TIMEOUT, read_frame(&mut reader))
            .await
            .map_err(|_| AttachError::Timeout)?
            .map_err(|e| AttachError::Io(e.to_string()))?;
        let hello_id = hello.id;
        match hello.message {
            Message::Hello(body) if body.protocol_version == u32::from(PROTOCOL_VERSION) => {}
            Message::Hello(body) => {
                let refusal = Frame::new(
                    hello_id,
                    Message::Error(ErrorBody {
                        code: ErrorCode::ProtocolVersion,
                        message: format!("unsupported protocol version {}", body.protocol_version),
                        detail: None,
                    }),
                );
                // Best effort: the connection is being refused either way.
                let _ = write_frame(&mut writer, &refusal).await;
                return Err(AttachError::ProtocolVersion(body.protocol_version));
            }
            _ => return Err(AttachError::UnexpectedFrame),
        }
        if self.attached_count() >= MAX_SESSIONS {
            let refusal = Frame::new(
                hello_id,
                Message::Error(ErrorBody {
                    code: ErrorCode::HostCallFailed,
                    message: "too many executor sessions".to_string(),
                    detail: None,
                }),
            );
            // Best effort: the connection is being refused either way.
            let _ = write_frame(&mut writer, &refusal).await;
            return Err(AttachError::TooManySessions);
        }
        let ok = Frame::new(
            hello_id,
            Message::HelloOk(HelloOkBody {
                stage: STAGE_NAME.to_string(),
                protocol_version: u32::from(PROTOCOL_VERSION),
                limits: HelloLimits {
                    call_timeout_ms: duration_ms(self.call_timeout),
                    memory_mb: HELLO_MEMORY_MB,
                    max_concurrent_calls: u32::try_from(self.max_in_flight).unwrap_or(u32::MAX),
                },
            }),
        );
        write_frame(&mut writer, &ok)
            .await
            .map_err(|e| AttachError::Io(e.to_string()))?;

        let (writer_tx, mut writer_rx) = mpsc::channel::<Frame>(WRITER_CHANNEL_CAPACITY);
        let session = Arc::new(Session {
            writer: writer_tx,
            pending: Mutex::new(HashMap::new()),
            // Executor-initiated ids (hello, host-call) are the executor's own
            // space; ours start at 1 and are only ever matched against ours.
            next_id: AtomicU64::new(1),
            closed: AtomicBool::new(false),
            max_in_flight: self.max_in_flight,
            enabled_flags: Arc::clone(&self.enabled_flags),
            host_calls: self.host_calls.clone(),
        });

        let writer_session = Arc::clone(&session);
        tokio::spawn(async move {
            while let Some(frame) = writer_rx.recv().await {
                if let Err(e) = write_frame(&mut writer, &frame).await {
                    warn!(error = %e, "host-api write failed, closing the executor session");
                    break;
                }
            }
            writer_session.close();
        });

        let reader_session = Arc::clone(&session);
        let link = Arc::clone(self);
        tokio::spawn(async move {
            read_loop(&mut reader, &reader_session).await;
            reader_session.close();
            link.clear_if_current(&reader_session);
            info!("host-api executor session ended");
        });

        let attached = {
            let mut guard = match self.sessions.lock() {
                Ok(guard) => guard,
                Err(poisoned) => poisoned.into_inner(),
            };
            guard.retain(|s| !s.is_closed());
            guard.push(session);
            guard.len()
        };
        info!(
            executor_sessions = attached,
            "host-api executor session attached"
        );
        Ok(())
    }

    /// Drops exactly `session` (and any other closed one) from the held set --
    /// a finishing reader never evicts a different replica's live session.
    fn clear_if_current(&self, session: &Arc<Session>) {
        let mut guard = match self.sessions.lock() {
            Ok(guard) => guard,
            Err(poisoned) => poisoned.into_inner(),
        };
        guard.retain(|s| !Arc::ptr_eq(s, session) && !s.is_closed());
    }

    /// Sends one `invoke` and awaits its `result`/`error`, bounded by the
    /// call timeout plus a transport grace.
    pub(crate) async fn call(
        &self,
        app_id: &str,
        digest: &str,
        export: ExportKind,
        payload: serde_json::Value,
    ) -> Result<ResultBody, ConnectorInvokeError> {
        let sessions = self.ordered_sessions();
        if sessions.is_empty() {
            return Err(unavailable("no executor is connected to this stage"));
        }
        // `build-request` has no `ConnectionCtx`: the host records the
        // invocation so the cutover-flag read it triggers can be attributed
        // (see `CallOrigin::BuildRequest`); everything else is `Other`.
        let origin = match export {
            ExportKind::ConnectorBuildRequest => CallOrigin::BuildRequest {
                app_id: app_id.to_string(),
            },
            _ => CallOrigin::Other,
        };
        let frame_for = |id: u64| {
            Frame::new(
                id,
                Message::Invoke(InvokeBody {
                    app_id: app_id.to_string(),
                    digest: digest.to_string(),
                    export,
                    payload: payload.clone(),
                    deadline_ms: duration_ms(self.call_timeout),
                    trace: None,
                }),
            )
        };
        // Hand the invocation to the first replica that accepts it. A session
        // that refuses the hand-off (in-flight cap, dead writer) never saw the
        // call, so moving on to the next cannot double-execute it.
        let mut handed_off = None;
        let mut last_err = unavailable("no executor is connected to this stage");
        for session in &sessions {
            let (id, rx) = match session.register(origin.clone()) {
                Ok(slot) => slot,
                Err(e) => {
                    last_err = e;
                    continue;
                }
            };
            if session
                .writer
                .send_timeout(frame_for(id), WRITE_TIMEOUT)
                .await
                .is_err()
            {
                session.forget(id);
                last_err = unavailable("could not hand the invocation to the executor");
                continue;
            }
            handed_off = Some((Arc::clone(session), id, rx));
            break;
        }
        let Some((session, id, rx)) = handed_off else {
            return Err(last_err);
        };
        // Frees the slot however this future ends, including being dropped
        // mid-await (cancellation); `forget` is idempotent.
        let _slot = SlotGuard {
            session: &session,
            id,
        };
        debug!(id, export = export.as_str(), "connector invoke sent");
        match tokio::time::timeout(self.call_timeout + REPLY_GRACE, rx).await {
            Err(_elapsed) => {
                session.forget(id);
                Err(unavailable("executor did not reply in time"))
            }
            Ok(Err(_dropped)) => Err(unavailable("executor connection lost mid-call")),
            Ok(Ok(Ok(body))) => Ok(body),
            Ok(Ok(Err(body))) => Err(map_executor_error(&body)),
        }
    }
}

impl Default for ExecutorLink {
    fn default() -> Self {
        Self::new()
    }
}

/// Reads executor frames until EOF or a fatal protocol error, routing each.
async fn read_loop<R: AsyncRead + Unpin>(reader: &mut R, session: &Arc<Session>) {
    loop {
        let frame = match read_frame(reader).await {
            Ok(frame) => frame,
            Err(e) => {
                debug!(error = %e, "host-api read ended");
                return;
            }
        };
        match frame.message {
            Message::Result(body) => session.deliver(frame.id, Ok(body)),
            Message::Error(body) => session.deliver(frame.id, Err(body)),
            Message::Ping => {
                if session
                    .writer
                    .send_timeout(Frame::new(frame.id, Message::Pong), WRITE_TIMEOUT)
                    .await
                    .is_err()
                {
                    return;
                }
            }
            Message::HostCall(call) if session.host_calls.is_some() => {
                // Serviced off the read loop: a slow identity mint must not
                // stall replies. The handler bounds concurrency itself.
                if let Some(handler) = session.host_calls.clone() {
                    let session = Arc::clone(session);
                    let id = frame.id;
                    // Attributed on the read loop, before the spawn, so the
                    // invocation cannot finish and drop its entry first.
                    let origin = session.origin_of(call.call_id);
                    tokio::spawn(async move {
                        let outcome = handler.handle_from(&call, &origin).await;
                        let reply = Message::HostResult(match outcome {
                            Ok(value) => HostResultBody {
                                result: Some(value),
                                error: None,
                            },
                            Err(e) => HostResultBody {
                                result: None,
                                error: Some(HostResultError {
                                    code: e.code().to_string(),
                                    message: e.wire_message(),
                                }),
                            },
                        });
                        if session
                            .writer
                            .send_timeout(Frame::new(id, reply), WRITE_TIMEOUT)
                            .await
                            .is_err()
                        {
                            session.close();
                        }
                    });
                }
            }
            Message::HostCall(call)
                if call.capability == CapabilityKind::Flags && call.op == "enabled" =>
            {
                let key = call.args.get("key").and_then(serde_json::Value::as_str);
                // A missing/non-bool default is "not asking for ON": false.
                let default = call
                    .args
                    .get("default_value")
                    .is_some_and(|v| v.as_bool() == Some(true));
                let enabled = key.is_some_and(|k| session.enabled_flags.contains(k)) || default;
                let reply = Message::HostResult(HostResultBody {
                    result: Some(serde_json::json!({ "enabled": enabled })),
                    error: None,
                });
                if session
                    .writer
                    .send_timeout(Frame::new(frame.id, reply), WRITE_TIMEOUT)
                    .await
                    .is_err()
                {
                    return;
                }
            }
            Message::HostCall(call) => {
                // A connector asked for a host capability this link does not
                // service. Typed refusal so the guest call fails fast instead
                // of hanging until the executor's host-call timeout.
                warn!(
                    capability = ?call.capability,
                    "connector host-call refused: no connector host capability on this link"
                );
                let refusal = Message::HostResult(HostResultBody {
                    result: None,
                    error: Some(HostResultError {
                        code: HOST_CALL_DENIED.to_string(),
                        message: "svc-ingest services no connector host capability".to_string(),
                    }),
                });
                if session
                    .writer
                    .send_timeout(Frame::new(frame.id, refusal), WRITE_TIMEOUT)
                    .await
                    .is_err()
                {
                    return;
                }
            }
            other => {
                warn!(
                    kind = message_label(&other),
                    "unexpected host-api frame from the executor; closing the session"
                );
                return;
            }
        }
    }
}

/// Stable label for a message kind, for logs (never its contents).
fn message_label(message: &Message) -> &'static str {
    match message {
        Message::Hello(_) => "hello",
        Message::Loaded(_) => "loaded",
        Message::Unloaded(_) => "unloaded",
        Message::Result(_) => "result",
        Message::HostCall(_) => "host-call",
        Message::Error(_) => "error",
        Message::Pong => "pong",
        Message::HelloOk(_) => "hello-ok",
        Message::Load(_) => "load",
        Message::Unload(_) => "unload",
        Message::Invoke(_) => "invoke",
        Message::HostResult(_) => "host-result",
        Message::Ping => "ping",
        Message::Shutdown(_) => "shutdown",
    }
}

fn duration_ms(d: Duration) -> u64 {
    u64::try_from(d.as_millis()).unwrap_or(u64::MAX)
}

fn unavailable(reason: &str) -> ConnectorInvokeError {
    ConnectorInvokeError::Unavailable(reason.to_string())
}

/// Maps an executor `error` frame onto the host's failure classes by its typed
/// code (never by message text). The executor's message is deliberately not
/// carried: it can echo guest-supplied text.
fn map_executor_error(body: &ErrorBody) -> ConnectorInvokeError {
    let detail = body.detail.as_deref();
    match body.code {
        ErrorCode::ExecutorDeadline if detail == Some("pool_exhausted") => {
            unavailable("executor connector pool exhausted")
        }
        ErrorCode::ExecutorDeadline if detail == Some("fuel_exhausted") => {
            ConnectorInvokeError::sandbox_trip("fuel_exhausted", "fuel budget exhausted")
        }
        ErrorCode::ExecutorDeadline => {
            ConnectorInvokeError::sandbox_trip("deadline", "wall-clock deadline exceeded")
        }
        ErrorCode::MemoryLimit => {
            ConnectorInvokeError::sandbox_trip("memory_limit", "memory cap exceeded")
        }
        ErrorCode::WasmTrap => ConnectorInvokeError::sandbox_trip("trap", "guest trapped"),
        other => ConnectorInvokeError::Unavailable(format!(
            "executor refused the connector call ({other:?})"
        )),
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct HandshakeReply {
    bytes: String,
    secret_refs: Vec<(String, String)>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct WireEvent {
    platform: String,
    event_type: String,
    actor: Option<String>,
    payload_json: String,
    occurred_at: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct EventsReply {
    events: Vec<WireEvent>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct WireFrameError {
    kind: String,
    message: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct FrameErrorReply {
    frame_error: WireFrameError,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct HeartbeatReply {
    bytes: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct EmptyReply {}

/// Decodes a reply payload strictly; any shape drift is a typed
/// `Unavailable` (fail loud) rather than a guess.
fn decode_reply<T: for<'de> Deserialize<'de>>(
    payload: serde_json::Value,
) -> Result<T, ConnectorInvokeError> {
    serde_json::from_value(payload).map_err(|_| unavailable("executor sent a malformed reply"))
}

fn decode_bytes(b64: &str) -> Result<Vec<u8>, ConnectorInvokeError> {
    BASE64
        .decode(b64)
        .map_err(|_| unavailable("executor sent a malformed reply"))
}

fn to_frame_error(e: WireFrameError) -> Result<ConnectorFrameError, ConnectorInvokeError> {
    match e.kind.as_str() {
        "unsupported" => Ok(ConnectorFrameError::Unsupported(e.message)),
        "malformed" => Ok(ConnectorFrameError::Malformed(e.message)),
        "backend" => Ok(ConnectorFrameError::Backend(e.message)),
        _ => Err(unavailable("executor sent an unknown frame-error kind")),
    }
}

/// The production [`ConnectorInvoker`]: resolves a platform to its pinned
/// connector and runs the export on the executor over an [`ExecutorLink`].
/// Cheap to share; holds no per-connection state of its own.
pub struct WireConnectorInvoker {
    link: Arc<ExecutorLink>,
    registry: Arc<ConnectorRegistry>,
}

impl WireConnectorInvoker {
    /// An invoker for the connectors pinned in `registry`, reaching the
    /// executor over `link`.
    #[must_use]
    pub fn new(link: Arc<ExecutorLink>, registry: Arc<ConnectorRegistry>) -> Self {
        Self { link, registry }
    }

    /// Resolves `platform` to its pinned `(app_id, digest)` and runs `export`.
    /// An unpinned platform is a typed `Unavailable`, never a fallback.
    async fn call(
        &self,
        platform: &str,
        export: ExportKind,
        payload: serde_json::Value,
    ) -> Result<ResultBody, ConnectorInvokeError> {
        let pinned = self
            .registry
            .resolve(platform)
            .map_err(|e| ConnectorInvokeError::Unavailable(e.to_string()))?;
        self.link
            .call(&pinned.app_id, &pinned.digest, export, payload)
            .await
    }
}

/// The wire `ctx` object: `{platform, source_id, connection_id,
/// session_token}`.
fn ctx_json(ctx: &ConnectionCtx) -> serde_json::Value {
    serde_json::json!({
        "platform": ctx.platform,
        "source_id": ctx.source_id,
        "connection_id": ctx.connection_id,
        "session_token": ctx.session_token,
    })
}

impl ConnectorInvoker for WireConnectorInvoker {
    /// `receiver.on-connect` over the wire: returns the connector-built
    /// handshake (secret handles still opaque).
    async fn on_connect(
        &self,
        platform: &str,
        ctx: &ConnectionCtx,
    ) -> Result<Handshake, ConnectorInvokeError> {
        let payload = serde_json::json!({ "ctx": ctx_json(ctx) });
        let reply = self
            .call(platform, ExportKind::ConnectorOnConnect, payload)
            .await?;
        let reply: HandshakeReply = decode_reply(reply.payload)?;
        Ok(Handshake {
            bytes: decode_bytes(&reply.bytes)?,
            secret_refs: reply.secret_refs,
        })
    }

    /// `receiver.on-frame` over the wire (the hot path): a guest
    /// `frame-error` is `Guest`, a sandbox containment a `SandboxTrip`.
    async fn on_frame(
        &self,
        platform: &str,
        frame: RawFrame,
    ) -> Result<Vec<ConnectorEvent>, ConnectorInvokeError> {
        let payload = serde_json::json!({
            "ctx": ctx_json(&frame.ctx),
            "bytes": BASE64.encode(&frame.bytes),
            "received_at": frame.received_at,
        });
        let reply = self
            .call(platform, ExportKind::ConnectorOnFrame, payload)
            .await?;
        if reply.payload.get("frame_error").is_some() {
            let FrameErrorReply { frame_error } = decode_reply(reply.payload)?;
            return Err(ConnectorInvokeError::Guest(to_frame_error(frame_error)?));
        }
        let reply: EventsReply = decode_reply(reply.payload)?;
        Ok(reply
            .events
            .into_iter()
            .map(|e| ConnectorEvent {
                platform: e.platform,
                event_type: e.event_type,
                actor: e.actor,
                payload_json: e.payload_json,
                occurred_at: e.occurred_at,
            })
            .collect())
    }

    /// `receiver.on-heartbeat-due` over the wire: the optional bundle-supplied
    /// heartbeat body.
    async fn on_heartbeat_due(
        &self,
        platform: &str,
        ctx: &ConnectionCtx,
    ) -> Result<Option<Vec<u8>>, ConnectorInvokeError> {
        let payload = serde_json::json!({ "ctx": ctx_json(ctx) });
        let reply = self
            .call(platform, ExportKind::ConnectorOnHeartbeatDue, payload)
            .await?;
        let reply: HeartbeatReply = decode_reply(reply.payload)?;
        reply.bytes.as_deref().map(decode_bytes).transpose()
    }

    /// `receiver.on-disconnect` over the wire: best-effort notification that
    /// the connection ended.
    async fn on_disconnect(
        &self,
        platform: &str,
        ctx: &ConnectionCtx,
        reason: &str,
    ) -> Result<(), ConnectorInvokeError> {
        let payload = serde_json::json!({ "ctx": ctx_json(ctx), "reason": reason });
        let reply = self
            .call(platform, ExportKind::ConnectorOnDisconnect, payload)
            .await?;
        let EmptyReply {} = decode_reply(reply.payload)?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use crate::connector::registry::PinnedConnector;
    use penguin_bundle_host::wire::{HelloBody, HostCallBody, SandboxInfo};
    use tokio::io::{duplex, DuplexStream};

    const PLATFORM: &str = "echo";
    const APP_ID: &str = "waddles.core.example.echo";
    const DIGEST: &str = "sha256:abc";

    fn ctx() -> ConnectionCtx {
        ConnectionCtx {
            platform: PLATFORM.to_string(),
            source_id: "src-1".to_string(),
            connection_id: "conn-1".to_string(),
            session_token: Some("tok".to_string()),
        }
    }

    fn raw_frame(bytes: &[u8]) -> RawFrame {
        RawFrame {
            ctx: ctx(),
            bytes: bytes.to_vec(),
            received_at: "2026-10-13T00:00:00.000Z".to_string(),
        }
    }

    fn registry() -> Arc<ConnectorRegistry> {
        let mut r = ConnectorRegistry::new();
        r.register(PinnedConnector::new(PLATFORM, APP_ID, DIGEST, 4096))
            .unwrap();
        Arc::new(r)
    }

    fn hello(version: u32) -> Message {
        Message::Hello(HelloBody {
            protocol_version: version,
            executor_version: "t".to_string(),
            wasmtime_version: "t".to_string(),
            wasmtime_abi: "t".to_string(),
            collector: "drc".to_string(),
            sandbox: SandboxInfo {
                runtime: "runc".to_string(),
                verified: false,
            },
            executor_id: None,
        })
    }

    /// The scripted executor half: completes the handshake, then answers each
    /// `invoke` through `answer` (a `None` answer means "stay silent").
    struct FakeExecutor {
        io: DuplexStream,
    }

    impl FakeExecutor {
        /// Attaches a fresh fake executor to `link`, returning it post-handshake.
        async fn attach(link: &Arc<ExecutorLink>) -> Self {
            let (stage_io, mut exec_io) = duplex(1 << 20);
            let link2 = Arc::clone(link);
            let attach = tokio::spawn(async move { link2.attach(stage_io).await });
            write_frame(&mut exec_io, &Frame::new(1, hello(1)))
                .await
                .unwrap();
            let ok = read_frame(&mut exec_io).await.unwrap();
            assert!(matches!(ok.message, Message::HelloOk(_)));
            assert_eq!(ok.id, 1);
            attach.await.unwrap().unwrap();
            Self { io: exec_io }
        }

        async fn next_invoke(&mut self) -> (u64, InvokeBody) {
            let frame = read_frame(&mut self.io).await.unwrap();
            match frame.message {
                Message::Invoke(body) => (frame.id, body),
                other => panic!("expected invoke, got {other:?}"),
            }
        }

        async fn reply(&mut self, id: u64, message: Message) {
            write_frame(&mut self.io, &Frame::new(id, message))
                .await
                .unwrap();
        }

        async fn reply_ok(&mut self, id: u64, payload: serde_json::Value) {
            self.reply(
                id,
                Message::Result(ResultBody {
                    payload,
                    duration_ms: 1,
                    fuel_used: 1,
                }),
            )
            .await;
        }

        async fn reply_err(&mut self, id: u64, code: ErrorCode, detail: Option<&str>) {
            self.reply(
                id,
                Message::Error(ErrorBody {
                    code,
                    message: "guest text must not leak".to_string(),
                    detail: detail.map(str::to_string),
                }),
            )
            .await;
        }
    }

    async fn rig() -> (Arc<ExecutorLink>, Arc<WireConnectorInvoker>, FakeExecutor) {
        let link = Arc::new(ExecutorLink::new().with_call_timeout(Duration::from_millis(400)));
        let exec = FakeExecutor::attach(&link).await;
        let invoker = Arc::new(WireConnectorInvoker::new(Arc::clone(&link), registry()));
        (link, invoker, exec)
    }

    #[tokio::test]
    async fn on_connect_sends_the_pinned_identity_and_decodes_the_handshake() {
        let (_link, inv, mut exec) = rig().await;
        let call = tokio::spawn({
            let inv = Arc::clone(&inv);
            async move { inv.on_connect(PLATFORM, &ctx()).await }
        });
        let (id, body) = exec.next_invoke().await;
        assert_eq!(body.export, ExportKind::ConnectorOnConnect);
        assert_eq!(body.app_id, APP_ID);
        assert_eq!(body.digest, DIGEST);
        assert_eq!(body.payload["ctx"]["connection_id"], "conn-1");
        assert_eq!(body.payload["ctx"]["session_token"], "tok");
        exec.reply_ok(
            id,
            serde_json::json!({
                "bytes": BASE64.encode(b"hello"),
                "secret_refs": [["token", "handle-1"]],
            }),
        )
        .await;
        let hs = call.await.unwrap().unwrap();
        assert_eq!(hs.bytes, b"hello");
        assert_eq!(hs.secret_refs, vec![("token".into(), "handle-1".into())]);
    }

    #[tokio::test]
    async fn on_frame_sends_base64_bytes_and_decodes_events() {
        let (_link, inv, mut exec) = rig().await;
        let call = tokio::spawn({
            let inv = Arc::clone(&inv);
            async move { inv.on_frame(PLATFORM, raw_frame(&[0, 255, 10])).await }
        });
        let (id, body) = exec.next_invoke().await;
        assert_eq!(body.export, ExportKind::ConnectorOnFrame);
        assert_eq!(body.payload["bytes"], BASE64.encode([0u8, 255, 10]));
        assert_eq!(body.payload["received_at"], "2026-10-13T00:00:00.000Z");
        exec.reply_ok(
            id,
            serde_json::json!({"events":[{
                "platform":"echo","event_type":"message","actor":"alice",
                "payload_json":"{\"text\":\"hi\"}","occurred_at":"2026-10-13T00:00:00.000Z"
            }]}),
        )
        .await;
        let events = call.await.unwrap().unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].event_type, "message");
        assert_eq!(events[0].actor.as_deref(), Some("alice"));
    }

    #[tokio::test]
    async fn on_frame_maps_each_guest_frame_error_kind() {
        let (_link, inv, mut exec) = rig().await;
        for (kind, want) in [
            ("unsupported", ConnectorFrameError::Unsupported("m".into())),
            ("malformed", ConnectorFrameError::Malformed("m".into())),
            ("backend", ConnectorFrameError::Backend("m".into())),
        ] {
            let call = tokio::spawn({
                let inv = Arc::clone(&inv);
                async move { inv.on_frame(PLATFORM, raw_frame(b"x")).await }
            });
            let (id, _) = exec.next_invoke().await;
            exec.reply_ok(
                id,
                serde_json::json!({"frame_error":{"kind":kind,"message":"m"}}),
            )
            .await;
            let err = call.await.unwrap().unwrap_err();
            assert_eq!(err, ConnectorInvokeError::Guest(want));
            assert!(!err.tears_down_connection());
        }
    }

    #[tokio::test]
    async fn an_unknown_or_malformed_frame_error_is_unavailable_not_guessed() {
        let (_link, inv, mut exec) = rig().await;
        for payload in [
            serde_json::json!({"frame_error":{"kind":"weird","message":"m"}}),
            serde_json::json!({"frame_error":"not an object"}),
        ] {
            let call = tokio::spawn({
                let inv = Arc::clone(&inv);
                async move { inv.on_frame(PLATFORM, raw_frame(b"x")).await }
            });
            let (id, _) = exec.next_invoke().await;
            exec.reply_ok(id, payload).await;
            assert!(matches!(
                call.await.unwrap().unwrap_err(),
                ConnectorInvokeError::Unavailable(_)
            ));
        }
    }

    #[tokio::test]
    async fn heartbeat_decodes_a_body_and_an_absent_body() {
        let (_link, inv, mut exec) = rig().await;
        for (reply, want) in [
            (
                serde_json::json!({"bytes": BASE64.encode(b"ping")}),
                Some(b"ping".to_vec()),
            ),
            (serde_json::json!({"bytes": null}), None),
        ] {
            let call = tokio::spawn({
                let inv = Arc::clone(&inv);
                async move { inv.on_heartbeat_due(PLATFORM, &ctx()).await }
            });
            let (id, body) = exec.next_invoke().await;
            assert_eq!(body.export, ExportKind::ConnectorOnHeartbeatDue);
            exec.reply_ok(id, reply).await;
            assert_eq!(call.await.unwrap().unwrap(), want);
        }
    }

    #[tokio::test]
    async fn disconnect_sends_the_reason_and_expects_an_empty_result() {
        let (_link, inv, mut exec) = rig().await;
        let call = tokio::spawn({
            let inv = Arc::clone(&inv);
            async move { inv.on_disconnect(PLATFORM, &ctx(), "bye").await }
        });
        let (id, body) = exec.next_invoke().await;
        assert_eq!(body.export, ExportKind::ConnectorOnDisconnect);
        assert_eq!(body.payload["reason"], "bye");
        exec.reply_ok(id, serde_json::json!({})).await;
        call.await.unwrap().unwrap();
    }

    #[tokio::test]
    async fn executor_error_codes_map_to_the_three_failure_classes() {
        let (_link, inv, mut exec) = rig().await;
        let cases: [(ErrorCode, Option<&str>, &str); 7] = [
            (ErrorCode::WasmTrap, None, "trap"),
            (ErrorCode::ExecutorDeadline, None, "deadline"),
            (
                ErrorCode::ExecutorDeadline,
                Some("fuel_exhausted"),
                "fuel_exhausted",
            ),
            (ErrorCode::MemoryLimit, None, "memory_limit"),
            (
                ErrorCode::ExecutorDeadline,
                Some("pool_exhausted"),
                "unavailable",
            ),
            (ErrorCode::UnknownBundle, None, "unavailable"),
            (ErrorCode::ShuttingDown, None, "unavailable"),
        ];
        for (code, detail, kind) in cases {
            let call = tokio::spawn({
                let inv = Arc::clone(&inv);
                async move { inv.on_frame(PLATFORM, raw_frame(b"x")).await }
            });
            let (id, _) = exec.next_invoke().await;
            exec.reply_err(id, code, detail).await;
            let err = call.await.unwrap().unwrap_err();
            assert_eq!(err.kind(), kind, "{code:?}/{detail:?}");
            assert!(err.tears_down_connection());
            assert!(
                !err.to_string().contains("guest text must not leak"),
                "executor-supplied text must never reach the error value: {err}"
            );
        }
    }

    #[tokio::test]
    async fn a_malformed_reply_is_unavailable() {
        let (_link, inv, mut exec) = rig().await;
        for payload in [
            serde_json::json!({"nope": 1}),
            serde_json::json!({"bytes": "!!!", "secret_refs": []}),
            serde_json::json!({"bytes": "aGk=", "secret_refs": [], "extra": 1}),
        ] {
            let call = tokio::spawn({
                let inv = Arc::clone(&inv);
                async move { inv.on_connect(PLATFORM, &ctx()).await }
            });
            let (id, _) = exec.next_invoke().await;
            exec.reply_ok(id, payload).await;
            assert!(matches!(
                call.await.unwrap().unwrap_err(),
                ConnectorInvokeError::Unavailable(_)
            ));
        }
    }

    #[tokio::test]
    async fn an_unexpected_frame_kind_closes_the_session_and_fails_the_call() {
        let (link, inv, mut exec) = rig().await;
        let call = tokio::spawn({
            let inv = Arc::clone(&inv);
            async move { inv.on_connect(PLATFORM, &ctx()).await }
        });
        let (id, _) = exec.next_invoke().await;
        // A `pong` is a legal frame but never a reply to an invoke: the read
        // loop treats it as a protocol violation and closes the session.
        exec.reply(id, Message::Pong).await;
        assert!(matches!(
            call.await.unwrap().unwrap_err(),
            ConnectorInvokeError::Unavailable(_)
        ));
        tokio::time::sleep(Duration::from_millis(20)).await;
        assert!(!link.is_attached());
    }

    #[tokio::test]
    async fn no_executor_attached_is_a_typed_unavailable_for_every_export() {
        let link = Arc::new(ExecutorLink::new());
        assert!(!link.is_attached());
        let inv = WireConnectorInvoker::new(link, registry());
        let errs = [
            inv.on_connect(PLATFORM, &ctx()).await.err(),
            inv.on_frame(PLATFORM, raw_frame(b"x")).await.err(),
            inv.on_heartbeat_due(PLATFORM, &ctx()).await.err(),
            inv.on_disconnect(PLATFORM, &ctx(), "r").await.err(),
        ];
        assert_eq!(errs.len(), 4);
        for e in errs {
            let e = e.expect("every export must fail loudly with no executor");
            assert!(
                matches!(&e, ConnectorInvokeError::Unavailable(m) if m.contains("no executor"))
            );
            assert!(e.tears_down_connection());
        }
    }

    #[tokio::test]
    async fn an_unpinned_platform_is_unavailable_even_with_an_executor() {
        let (_link, inv, _exec) = rig().await;
        let err = inv.on_connect("slack", &ctx()).await.unwrap_err();
        assert!(matches!(err, ConnectorInvokeError::Unavailable(ref m) if m.contains("slack")));
    }

    #[tokio::test(start_paused = true)]
    async fn a_silent_executor_times_out_unavailable_and_the_slot_is_freed() {
        let link = Arc::new(ExecutorLink::new().with_call_timeout(Duration::from_millis(30)));
        let _exec = FakeExecutor::attach(&link).await;
        let inv = WireConnectorInvoker::new(Arc::clone(&link), registry());
        let err = inv.on_connect(PLATFORM, &ctx()).await.unwrap_err();
        assert!(matches!(err, ConnectorInvokeError::Unavailable(ref m) if m.contains("in time")));
        let session = link.ordered_sessions().remove(0);
        assert!(session.pending.lock().unwrap().is_empty());
    }

    /// regression: a call cancelled mid-flight (the driver drops an in-flight
    /// `on-heartbeat-due`/`on-frame` on teardown/shutdown) left its reply slot
    /// counted against `max_in_flight` until the session closed.
    #[tokio::test]
    async fn a_cancelled_call_frees_its_reply_slot() {
        let (link, inv, mut exec) = rig().await;
        let call = tokio::spawn({
            let inv = Arc::clone(&inv);
            async move { inv.on_heartbeat_due(PLATFORM, &ctx()).await }
        });
        let (_id, _) = exec.next_invoke().await;
        let session = link.ordered_sessions().remove(0);
        assert_eq!(session.pending.lock().unwrap().len(), 1);
        call.abort();
        let _ = call.await;
        assert!(
            session.pending.lock().unwrap().is_empty(),
            "a cancelled call must free its slot"
        );
    }

    #[tokio::test]
    async fn a_dropped_connection_fails_the_inflight_call_and_detaches() {
        let (link, inv, exec) = rig().await;
        let call = tokio::spawn({
            let inv = Arc::clone(&inv);
            async move { inv.on_connect(PLATFORM, &ctx()).await }
        });
        // Let the invoke reach the wire, then kill the executor end.
        tokio::time::sleep(Duration::from_millis(50)).await;
        drop(exec);
        let err = call.await.unwrap().unwrap_err();
        assert!(
            matches!(err, ConnectorInvokeError::Unavailable(_)),
            "{err:?}"
        );
        tokio::time::sleep(Duration::from_millis(50)).await;
        assert!(!link.is_attached(), "a dead session must not stay current");
    }

    #[tokio::test]
    async fn a_late_reply_for_an_expired_call_is_dropped_without_harm() {
        let (_link, inv, mut exec) = rig().await;
        // Reply with an id nobody is waiting on, then serve a real call.
        exec.reply_ok(9_999, serde_json::json!({})).await;
        let call = tokio::spawn({
            let inv = Arc::clone(&inv);
            async move { inv.on_disconnect(PLATFORM, &ctx(), "r").await }
        });
        let (id, _) = exec.next_invoke().await;
        exec.reply_ok(id, serde_json::json!({})).await;
        call.await.unwrap().unwrap();
    }

    #[tokio::test]
    async fn ping_gets_pong_and_a_host_call_is_refused_typed() {
        let (_link, _inv, mut exec) = rig().await;
        exec.reply(77, Message::Ping).await;
        let pong = read_frame(&mut exec.io).await.unwrap();
        assert_eq!((pong.id, pong.message), (77, Message::Pong));

        exec.reply(
            78,
            Message::HostCall(HostCallBody {
                app_id: APP_ID.to_string(),
                capability: CapabilityKind::Http,
                op: "send".to_string(),
                args: serde_json::json!({}),
                call_id: 1,
            }),
        )
        .await;
        let denied = read_frame(&mut exec.io).await.unwrap();
        assert_eq!(denied.id, 78);
        match denied.message {
            Message::HostResult(HostResultBody {
                result: None,
                error: Some(e),
            }) => assert_eq!(e.code, HOST_CALL_DENIED),
            other => panic!("{other:?}"),
        }
    }

    /// `flags.enabled` is answered `true` only for the keys the host enabled
    /// (or when the guest's own default is true); `flags.tier` and every
    /// other capability stay refused.
    #[tokio::test]
    async fn flags_enabled_is_answered_only_for_configured_keys() {
        let link = Arc::new(ExecutorLink::new().with_enabled_flags(["waddles.connector-twitch"]));
        let mut exec = FakeExecutor::attach(&link).await;
        let call = |op: &str, args: serde_json::Value| {
            Message::HostCall(HostCallBody {
                app_id: APP_ID.to_string(),
                capability: CapabilityKind::Flags,
                op: op.to_string(),
                args,
                call_id: 1,
            })
        };
        for (key, default, want) in [
            ("waddles.connector-twitch", false, true),
            ("waddles.connector-other", false, false),
            ("waddles.connector-other", true, true),
        ] {
            exec.reply(
                80,
                call(
                    "enabled",
                    serde_json::json!({"key": key, "default_value": default}),
                ),
            )
            .await;
            let r = read_frame(&mut exec.io).await.unwrap();
            match r.message {
                Message::HostResult(HostResultBody {
                    result: Some(v),
                    error: None,
                }) => assert_eq!(v["enabled"], want, "{key} default={default}"),
                other => panic!("{other:?}"),
            }
        }
        exec.reply(81, call("tier", serde_json::json!({}))).await;
        match read_frame(&mut exec.io).await.unwrap().message {
            Message::HostResult(HostResultBody {
                result: None,
                error: Some(e),
            }) => assert_eq!(e.code, HOST_CALL_DENIED),
            other => panic!("{other:?}"),
        }
    }

    #[tokio::test]
    async fn the_in_flight_cap_refuses_instead_of_queueing() {
        let link = Arc::new(
            ExecutorLink::new()
                .with_call_timeout(Duration::from_millis(400))
                .with_max_in_flight(1),
        );
        let mut exec = FakeExecutor::attach(&link).await;
        let inv = Arc::new(WireConnectorInvoker::new(Arc::clone(&link), registry()));
        let first = tokio::spawn({
            let inv = Arc::clone(&inv);
            async move { inv.on_connect(PLATFORM, &ctx()).await }
        });
        let (id, _) = exec.next_invoke().await;
        let err = inv.on_connect(PLATFORM, &ctx()).await.unwrap_err();
        assert!(matches!(err, ConnectorInvokeError::Unavailable(ref m) if m.contains("in-flight")));
        exec.reply_ok(id, serde_json::json!({"bytes":"","secret_refs":[]}))
            .await;
        first.await.unwrap().unwrap();
    }

    /// Several executor replicas stay attached side by side: a second attach
    /// never replaces or fails the first replica's session or its in-flight
    /// calls, and both are counted.
    #[tokio::test]
    async fn a_second_executor_attaches_alongside_the_first_without_failing_its_calls() {
        let (link, inv, mut first) = rig().await;
        let pending = tokio::spawn({
            let inv = Arc::clone(&inv);
            async move { inv.on_connect(PLATFORM, &ctx()).await }
        });
        let (id, _) = first.next_invoke().await;
        let _second = FakeExecutor::attach(&link).await;
        assert_eq!(link.attached_count(), 2, "both replicas stay attached");
        first
            .reply_ok(id, serde_json::json!({"bytes":"","secret_refs":[]}))
            .await;
        pending
            .await
            .unwrap()
            .expect("the first replica's in-flight call must survive the second attach");
    }

    /// Real-path distribution: with two replicas attached, successive invokes
    /// alternate between them, and every invoke reaches EXACTLY ONE replica
    /// (never both, never neither).
    #[tokio::test]
    async fn invokes_are_spread_across_replicas_and_each_reaches_exactly_one() {
        let (link, inv, mut a) = rig().await;
        let mut b = FakeExecutor::attach(&link).await;
        let mut got_a = 0usize;
        let mut got_b = 0usize;
        for _ in 0..6 {
            let call = tokio::spawn({
                let inv = Arc::clone(&inv);
                async move { inv.on_disconnect(PLATFORM, &ctx(), "r").await }
            });
            let (which, id) = tokio::select! {
                (id, _) = a.next_invoke() => (0usize, id),
                (id, _) = b.next_invoke() => (1usize, id),
            };
            if which == 0 {
                got_a += 1;
                a.reply_ok(id, serde_json::json!({})).await;
            } else {
                got_b += 1;
                b.reply_ok(id, serde_json::json!({})).await;
            }
            call.await.unwrap().unwrap();
        }
        assert_eq!((got_a, got_b), (3, 3), "work spreads evenly over replicas");
    }

    /// A replica that disconnects drops out of the rotation; the survivor keeps
    /// serving, and only when the last one is gone is the link unavailable.
    #[tokio::test]
    async fn a_departed_replica_leaves_the_rotation_and_the_survivor_serves() {
        let (link, inv, a) = rig().await;
        let mut b = FakeExecutor::attach(&link).await;
        drop(a);
        for _ in 0..50 {
            if link.attached_count() == 1 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        assert_eq!(link.attached_count(), 1);
        for _ in 0..3 {
            let call = tokio::spawn({
                let inv = Arc::clone(&inv);
                async move { inv.on_disconnect(PLATFORM, &ctx(), "r").await }
            });
            let (id, _) = b.next_invoke().await;
            b.reply_ok(id, serde_json::json!({})).await;
            call.await.unwrap().unwrap();
        }
        drop(b);
        for _ in 0..50 {
            if !link.is_attached() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        assert!(matches!(
            inv.on_disconnect(PLATFORM, &ctx(), "r").await.unwrap_err(),
            ConnectorInvokeError::Unavailable(_)
        ));
    }

    #[tokio::test]
    async fn the_handshake_refuses_a_wrong_protocol_version_and_a_non_hello() {
        let link = Arc::new(ExecutorLink::new());

        let (stage_io, mut exec_io) = duplex(1 << 16);
        let l = Arc::clone(&link);
        let attach = tokio::spawn(async move { l.attach(stage_io).await });
        write_frame(&mut exec_io, &Frame::new(1, hello(2)))
            .await
            .unwrap();
        let refusal = read_frame(&mut exec_io).await.unwrap();
        assert!(matches!(
            refusal.message,
            Message::Error(ErrorBody {
                code: ErrorCode::ProtocolVersion,
                ..
            })
        ));
        assert!(matches!(
            attach.await.unwrap(),
            Err(AttachError::ProtocolVersion(2))
        ));

        let (stage_io, mut exec_io) = duplex(1 << 16);
        let l = Arc::clone(&link);
        let attach = tokio::spawn(async move { l.attach(stage_io).await });
        write_frame(&mut exec_io, &Frame::new(1, Message::Ping))
            .await
            .unwrap();
        assert!(matches!(
            attach.await.unwrap(),
            Err(AttachError::UnexpectedFrame)
        ));

        // EOF before any frame is an I/O failure, not a hang.
        let (stage_io, exec_io) = duplex(1 << 16);
        drop(exec_io);
        assert!(matches!(
            link.attach(stage_io).await,
            Err(AttachError::Io(_))
        ));
        assert!(!link.is_attached());
    }

    #[tokio::test(start_paused = true)]
    async fn a_silent_peer_fails_the_handshake_by_timeout() {
        let link = Arc::new(ExecutorLink::new());
        let (stage_io, _exec_io) = duplex(1 << 16);
        assert!(matches!(
            link.attach(stage_io).await,
            Err(AttachError::Timeout)
        ));
    }

    #[test]
    fn message_labels_are_distinct_and_content_free() {
        use std::collections::HashSet;
        let labels: HashSet<_> = [
            message_label(&hello(1)),
            message_label(&Message::Pong),
            message_label(&Message::Ping),
        ]
        .into_iter()
        .collect();
        assert_eq!(labels.len(), 3);
    }

    /// An identity tokenizer that must never be reached by a flag read.
    struct NoTokenizer;

    impl super::super::host_calls::IdentityTokenizer for NoTokenizer {
        fn tokenize<'a>(
            &'a self,
            _scope: &'a super::super::host_calls::CallScope,
            _platform_user_id: &'a str,
        ) -> super::super::webhook::BoxFut<
            'a,
            Result<String, super::super::host_calls::TokenizeError>,
        > {
            Box::pin(async { Err(super::super::host_calls::TokenizeError::Unavailable) })
        }
    }

    const TWITCH_APP: &str = "waddles.core.service.twitch";

    /// A link whose handler is the production allowlist (twitch connector ON).
    async fn handler_rig() -> (Arc<ExecutorLink>, FakeExecutor) {
        use super::super::host_calls::{ActiveConnections, ConnectorHostCalls, HostCallConfig};
        let handler = ConnectorHostCalls::new(
            Arc::new(ActiveConnections::new()),
            Arc::new(NoTokenizer),
            HostCallConfig::new(["twitch"]).unwrap(),
        );
        let link = Arc::new(
            ExecutorLink::new()
                .with_call_timeout(Duration::from_secs(5))
                .with_host_calls(Arc::new(handler)),
        );
        let exec = FakeExecutor::attach(&link).await;
        (link, exec)
    }

    fn flag_call(app: &str, call_id: u64) -> Message {
        Message::HostCall(HostCallBody {
            app_id: app.to_string(),
            capability: CapabilityKind::Flags,
            op: "enabled".to_string(),
            args: serde_json::json!({"key": "waddles.connector-twitch"}),
            call_id,
        })
    }

    async fn host_result(exec: &mut FakeExecutor) -> HostResultBody {
        match read_frame(&mut exec.io).await.unwrap().message {
            Message::HostResult(r) => r,
            other => panic!("{other:?}"),
        }
    }

    /// The A2 open issue, over the real link: with the handler installed a flag
    /// read made DURING a build-request invocation is answered, because the link
    /// itself recorded that invocation (the call's `call_id`).
    #[tokio::test]
    async fn a_flag_read_during_build_request_is_answered_with_the_handler_installed() {
        let (link, mut exec) = handler_rig().await;
        let call = tokio::spawn({
            let link = Arc::clone(&link);
            async move {
                link.call(
                    TWITCH_APP,
                    DIGEST,
                    ExportKind::ConnectorBuildRequest,
                    serde_json::json!({}),
                )
                .await
            }
        });
        let (id, body) = exec.next_invoke().await;
        assert_eq!(body.export, ExportKind::ConnectorBuildRequest);
        exec.reply(900, flag_call(TWITCH_APP, id)).await;
        let r = host_result(&mut exec).await;
        assert_eq!(r.error, None);
        assert_eq!(r.result.unwrap()["enabled"], true);
        exec.reply_ok(id, serde_json::json!({})).await;
        call.await.unwrap().unwrap();
    }

    /// Mutation target (unscoped denial): the SAME flag read is denied when the
    /// `call_id` is unknown / stale, or when it happened during a non-build-request
    /// invocation (a tool-call), or when the connector is not the invoked one.
    #[tokio::test]
    async fn the_flag_read_is_denied_without_a_build_request_invocation() {
        let (link, mut exec) = handler_rig().await;
        // No invocation in flight at all: a guessed / stale call_id.
        for stale in [0u64, 1, 424_242] {
            exec.reply(901, flag_call(TWITCH_APP, stale)).await;
            let r = host_result(&mut exec).await;
            let e = r.error.expect("denied");
            assert_eq!(
                (e.code.as_str(), e.message.as_str()),
                ("denied", "unscoped_call")
            );
        }
        // During a tool-call invocation (origin Other): still denied.
        let call = tokio::spawn({
            let link = Arc::clone(&link);
            async move {
                link.call(
                    TWITCH_APP,
                    DIGEST,
                    ExportKind::ToolCall,
                    serde_json::json!({}),
                )
                .await
            }
        });
        let (id, _) = exec.next_invoke().await;
        exec.reply(902, flag_call(TWITCH_APP, id)).await;
        let e = host_result(&mut exec).await.error.expect("denied");
        assert_eq!(e.message, "unscoped_call");
        exec.reply_ok(id, serde_json::json!({})).await;
        call.await.unwrap().unwrap();
        // During a build-request for twitch, a host-call naming ANOTHER connector.
        let call = tokio::spawn({
            let link = Arc::clone(&link);
            async move {
                link.call(
                    TWITCH_APP,
                    DIGEST,
                    ExportKind::ConnectorBuildRequest,
                    serde_json::json!({}),
                )
                .await
            }
        });
        let (id, _) = exec.next_invoke().await;
        exec.reply(903, flag_call("waddles.core.service.discord", id))
            .await;
        let e = host_result(&mut exec).await.error.expect("denied");
        assert_eq!(e.message, "unknown_connector");
        exec.reply_ok(id, serde_json::json!({})).await;
        call.await.unwrap().unwrap();
        // After the invocation finished its id unlocks nothing.
        exec.reply(904, flag_call(TWITCH_APP, id)).await;
        let e = host_result(&mut exec).await.error.expect("denied");
        assert_eq!(e.message, "unscoped_call");
    }
}
