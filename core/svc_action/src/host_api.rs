//! The host-API mTLS server (spec §6.6/§7.1): the stage side of the
//! bundle-executor wire protocol. **The executor dials the stage** -- this
//! module accepts that connection, completes the `hello`/`hello-ok`
//! handshake (validating protocol version and sandbox posture, spec §12.2/
//! §14.6 tests 12a-12c), then lets `crate::dispatch` drive `load`/`invoke`
//! requests over it while a background read loop answers `host-call`
//! frames via a [`crate::capabilities::CapabilityHandler`] and delivers
//! every other reply through a [`CorrelationTable`].
//!
//! Mirrors `core/bundle_executor/src/wire.rs` (the executor/client side)
//! with the roles reversed: there, the executor sends `hello` and answers
//! `load`/`invoke`; here, the stage answers `hello` and sends `load`/
//! `invoke`. `core/bundle_executor/src/tls.rs` is the client-TLS mirror of
//! [`build_server_config`] below.

use std::collections::HashMap;
use std::net::SocketAddr;
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use penguin_bundle_host::wire::{
    read_frame, write_frame, CorrelationError, CorrelationTable, ErrorBody, ErrorCode, Frame,
    HelloOkBody, IdAllocator, InvokeBody, Message, ShutdownBody,
};
use rustls::server::WebPkiClientVerifier;
use rustls::{RootCertStore, ServerConfig};
use rustls_pki_types::pem::PemObject;
use rustls_pki_types::{CertificateDer, PrivateKeyDer};
use socket2::{SockRef, TcpKeepalive};
use thiserror::Error;
use tokio::io::{AsyncRead, AsyncWrite, AsyncWriteExt};
use tokio::sync::{mpsc, oneshot};
use tokio_rustls::TlsAcceptor;
use tracing::{debug, error, info, warn};

use crate::capabilities::{denied, CapabilityHandler, InvokeScope};
use crate::config::CliConfig;

/// Errors this module raises. Every variant is fatal to the connection or
/// listener it occurred on.
#[derive(Debug, Error)]
pub enum HostApiError {
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
    #[error("tls error: {0}")]
    Tls(#[from] rustls::Error),
    #[error("host-api config error: {0}")]
    Config(String),
    #[error("frame error: {0}")]
    Frame(#[from] penguin_bundle_host::wire::FrameError),
    #[error("correlation error: {0}")]
    Correlation(#[from] CorrelationError),
    #[error("connection unavailable")]
    ConnectionUnavailable,
    #[error("unexpected frame: {0}")]
    UnexpectedFrame(&'static str),
    #[error("peer reported error {code:?}: {message}")]
    PeerError { code: ErrorCode, message: String },
    #[error("heartbeat timed out waiting for pong")]
    HeartbeatTimeout,
}

fn load_certs(path: &Path) -> Result<Vec<CertificateDer<'static>>, HostApiError> {
    CertificateDer::pem_file_iter(path)
        .map_err(|e| HostApiError::Config(format!("failed to read certs from {path:?}: {e}")))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| HostApiError::Config(format!("invalid PEM certificate in {path:?}: {e}")))
}

fn load_private_key(path: &Path) -> Result<PrivateKeyDer<'static>, HostApiError> {
    PrivateKeyDer::from_pem_file(path)
        .map_err(|e| HostApiError::Config(format!("failed to read private key from {path:?}: {e}")))
}

/// Builds the rustls `ServerConfig` for the host-API mTLS listener: the
/// configured server cert/key, and mutual-TLS client-certificate
/// verification against the configured CA (spec §6.6: "Both peers present
/// certificates" -- a connection whose peer certificate does not verify is
/// "closed before a single frame is read", spec §14.6 test 12d).
pub fn build_server_config(cli: &CliConfig) -> Result<ServerConfig, HostApiError> {
    // Two rustls crypto backends are linked into this binary now
    // (`ring` + `penguin-licensing`'s transitive `aws-lc-rs`) -- see
    // `crate::crypto`'s doc for why `ServerConfig::builder()` below
    // panics without this.
    crate::crypto::ensure_crypto_provider_installed();
    let (cert_path, key_path) = match (
        &cli.host_api_server_cert_file,
        &cli.host_api_server_key_file,
    ) {
        (Some(c), Some(k)) => (c, k),
        _ => {
            return Err(HostApiError::Config(
                "HOST_API_SERVER_CERT_FILE and HOST_API_SERVER_KEY_FILE must both be set"
                    .to_string(),
            ))
        }
    };
    let certs = load_certs(cert_path)?;
    let key = load_private_key(key_path)?;

    let client_verifier = match &cli.host_api_client_ca_file {
        Some(ca_path) => {
            let mut roots = RootCertStore::empty();
            for cert in load_certs(ca_path)? {
                roots
                    .add(cert)
                    .map_err(|e| HostApiError::Config(format!("invalid CA certificate: {e}")))?;
            }
            WebPkiClientVerifier::builder(Arc::new(roots))
                .build()
                .map_err(|e| HostApiError::Config(format!("invalid client verifier: {e}")))?
        }
        None => return Err(HostApiError::Config(
            "HOST_API_CLIENT_CA_FILE is required -- mTLS client verification must not be optional"
                .to_string(),
        )),
    };

    let config = ServerConfig::builder()
        .with_client_cert_verifier(client_verifier)
        .with_single_cert(certs, key)?;
    Ok(config)
}

/// One accepted host-API connection's request/reply bookkeeping. Owns the
/// ids this side allocates (for `load`/`unload`/`invoke`/`ping`/`shutdown`
/// -- the message kinds the stage initiates, spec §6.6), the channel
/// frames are written through, and the per-in-flight-`invoke`
/// [`InvokeScope`] table `crate::capabilities`'s module doc describes: one
/// connection multiplexes many `invoke`s, so the scope a `host-call`
/// answers against must be looked up by that call's own `call_id`, never
/// assumed from the connection as a whole.
pub struct Connection {
    ids: IdAllocator,
    pending: CorrelationTable,
    writer_tx: mpsc::UnboundedSender<Frame>,
    closed: Arc<std::sync::atomic::AtomicBool>,
    scopes: std::sync::Mutex<std::collections::HashMap<u64, InvokeScope>>,
}

impl Connection {
    fn new(writer_tx: mpsc::UnboundedSender<Frame>) -> Arc<Self> {
        Arc::new(Self {
            ids: IdAllocator::new(),
            pending: CorrelationTable::new(),
            writer_tx,
            closed: Arc::new(std::sync::atomic::AtomicBool::new(false)),
            scopes: std::sync::Mutex::new(std::collections::HashMap::new()),
        })
    }

    /// Sends `message` under a freshly allocated id and awaits the peer's
    /// reply frame. Used for every stage-initiated exchange that carries no
    /// per-invoke scope: `load` -> `loaded`/`error`, `unload` ->
    /// `unloaded`/`error`, `ping` -> `pong`. `invoke` goes through
    /// [`Connection::invoke`] instead, which additionally registers the
    /// call's [`InvokeScope`] for the read loop's `host-call` handling.
    pub async fn request(&self, message: Message) -> Result<Frame, HostApiError> {
        if self.closed.load(Ordering::Acquire) {
            return Err(HostApiError::ConnectionUnavailable);
        }
        let id = self.ids.next_id();
        let rx = self.pending.register(id)?;
        self.send(Frame::new(id, message))?;
        rx.await.map_err(|_| HostApiError::ConnectionUnavailable)
    }

    /// Sends an `invoke` frame, registering `scope` under the frame's own
    /// id for the duration of the call so any `host-call` the executor
    /// issues mid-invoke (`call_id` == this frame's id, spec §6.6) resolves
    /// to the correct `(tenant, community, app_id)` -- never a different
    /// invoke's scope, and never a connection-wide default (spec §5.11,
    /// `crate::capabilities`'s module doc). The scope is removed once the
    /// reply arrives or the request fails, whichever comes first -- a
    /// `call_id` outliving its invoke must never resolve to a stale scope.
    pub async fn invoke(
        &self,
        body: InvokeBody,
        scope: InvokeScope,
    ) -> Result<Frame, HostApiError> {
        if self.closed.load(Ordering::Acquire) {
            return Err(HostApiError::ConnectionUnavailable);
        }
        let id = self.ids.next_id();
        let rx = self.pending.register(id)?;
        self.scopes
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(id, scope);
        let send_result = self.send(Frame::new(id, Message::Invoke(body)));
        if let Err(err) = send_result {
            self.scopes
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .remove(&id);
            return Err(err);
        }
        let result = rx.await.map_err(|_| HostApiError::ConnectionUnavailable);
        self.scopes
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .remove(&id);
        result
    }

    /// Looks up the [`InvokeScope`] registered for `call_id` by
    /// [`Connection::invoke`] -- `None` means `call_id` does not name any
    /// invoke currently in flight on this connection (already completed,
    /// never existed, or belongs to a different connection): the read
    /// loop's [`HostApiError`]-free path treats that as `unknown_invoke`,
    /// never as "use some other scope instead".
    fn scope_for(&self, call_id: u64) -> Option<InvokeScope> {
        self.scopes
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .get(&call_id)
            .cloned()
    }

    /// Sends `shutdown` without awaiting a reply (spec §6.6: "connection
    /// closed after in-flight calls drain or `grace_ms` elapses" -- no
    /// reply frame is defined for this message kind).
    pub fn shutdown(&self, grace_ms: u64) -> Result<(), HostApiError> {
        let id = self.ids.next_id();
        self.send(Frame::new(id, Message::Shutdown(ShutdownBody { grace_ms })))
    }

    fn send(&self, frame: Frame) -> Result<(), HostApiError> {
        self.writer_tx
            .send(frame)
            .map_err(|_| HostApiError::ConnectionUnavailable)
    }

    fn deliver(&self, frame: Frame) -> Result<(), CorrelationError> {
        self.pending.complete(frame)
    }

    /// True once the read loop has observed EOF/a fatal error and this
    /// connection can no longer serve a request.
    pub fn is_closed(&self) -> bool {
        self.closed.load(Ordering::Acquire)
    }

    /// Sends `ping` and awaits `pong` within `timeout` -- the heartbeat
    /// supervisor's ([`run_heartbeat`]) probe for the alpha 2026-10-02
    /// incident this whole mechanism exists to catch: a rolled svc pod left
    /// the executor bound to a terminated peer with no OS-level signal at
    /// all, so the only reliable "is the peer actually still there" check
    /// is a round trip on the wire protocol itself. On timeout the pending
    /// registration is cancelled so it never leaks in the correlation
    /// table.
    pub async fn ping(&self, timeout: Duration) -> Result<(), HostApiError> {
        if self.closed.load(Ordering::Acquire) {
            return Err(HostApiError::ConnectionUnavailable);
        }
        let id = self.ids.next_id();
        let rx = self.pending.register(id)?;
        self.send(Frame::new(id, Message::Ping))?;
        match tokio::time::timeout(timeout, rx).await {
            Ok(Ok(frame)) => match frame.message {
                Message::Pong => Ok(()),
                _ => Err(HostApiError::UnexpectedFrame("expected pong")),
            },
            Ok(Err(_)) => Err(HostApiError::ConnectionUnavailable),
            Err(_) => {
                self.pending.cancel(id);
                Err(HostApiError::HeartbeatTimeout)
            }
        }
    }

    /// Forcibly marks this connection closed -- used by [`run_heartbeat`]
    /// when it declares a session dead so [`ConnectionRegistry::active`]
    /// stops handing it out even before the read loop itself notices,
    /// which for a true half-open socket (no FIN ever arrives) may never
    /// happen on its own. The actual socket teardown happens when the
    /// caller's `tokio::select!` racing this heartbeat against the read
    /// loop drops the losing (read loop) future -- see `crate::host_api::
    /// serve`.
    pub(crate) fn mark_closed(&self) {
        self.closed.store(true, Ordering::Release);
    }
}

/// Test-only bare `Connection` construction -- for `crate::http::health`'s
/// readiness-transition tests, which need a `Connection` to register as
/// `ConnectionRegistry::set_active` without driving a full handshake.
#[cfg(test)]
pub(crate) fn test_connection() -> Arc<Connection> {
    let (tx, _rx) = mpsc::unbounded_channel();
    Connection::new(tx)
}

/// Completes the `hello`/`hello-ok` handshake as the stage side, then runs
/// the read loop until the peer disconnects or a fatal protocol error
/// occurs. Returns the live [`Connection`] immediately after the handshake
/// succeeds (the read loop continues in the background task the caller
/// spawns) -- see [`accept_and_handshake`], the usual entry point.
///
/// `pub(crate)` (not `pub`): `accept_and_handshake` is the only production
/// caller (it adds the TLS layer this function doesn't need to know
/// about); `crate::dispatch`'s own tests call this directly over a plain
/// in-memory duplex to build a real, handshaken [`Connection`] without a
/// TLS handshake or a live executor process.
pub(crate) async fn run_connection<S>(
    io: S,
    hello_ok: HelloOkBody,
    expected_sandbox_gvisor: bool,
    capabilities: Arc<dyn CapabilityHandler>,
) -> Result<
    (
        Arc<Connection>,
        impl std::future::Future<Output = Result<(), HostApiError>>,
    ),
    HostApiError,
>
where
    S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
{
    let (mut reader, mut writer) = tokio::io::split(io);
    let (writer_tx, mut writer_rx) = mpsc::unbounded_channel::<Frame>();
    let connection = Connection::new(writer_tx);

    let writer_task = tokio::spawn(async move {
        while let Some(frame) = writer_rx.recv().await {
            if let Err(e) = write_frame(&mut writer, &frame).await {
                warn!(error = %e, "host-api write failed, closing connection");
                break;
            }
        }
        let _ = writer.shutdown().await;
    });

    // The executor dials and speaks first (spec §6.6): the very first
    // frame on a fresh connection must be `hello`.
    let hello_frame = read_frame(&mut reader).await?;
    let hello = match hello_frame.message {
        Message::Hello(h) => h,
        other => {
            writer_task.abort();
            warn!(?other, "first frame on host-api connection was not hello");
            return Err(HostApiError::UnexpectedFrame("expected hello"));
        }
    };
    if hello.protocol_version != 1 {
        let message = format!("unsupported protocol_version {}", hello.protocol_version);
        connection.send(Frame::new(
            hello_frame.id,
            Message::Error(ErrorBody {
                code: ErrorCode::ProtocolVersion,
                message: message.clone(),
                detail: None,
            }),
        ))?;
        drop(connection);
        let _ = writer_task.await;
        return Err(HostApiError::PeerError {
            code: ErrorCode::ProtocolVersion,
            message,
        });
    }
    let executor_gvisor = hello.sandbox.runtime == "gvisor";
    if expected_sandbox_gvisor && !executor_gvisor {
        connection.send(Frame::new(
            hello_frame.id,
            Message::Error(ErrorBody {
                code: ErrorCode::UnsandboxedExecutor,
                message: format!(
                    "stage expects WADDLES_SANDBOX_GVISOR=true but executor reported sandbox {:?}",
                    hello.sandbox.runtime
                ),
                detail: None,
            }),
        ))?;
        // Flush the queued error frame, then close -- spec §6.6 test 12c:
        // "Connection refused with UNSANDBOXED_EXECUTOR".
        drop(connection);
        let _ = writer_task.await;
        return Err(HostApiError::PeerError {
            code: ErrorCode::UnsandboxedExecutor,
            message: "executor sandbox posture mismatch".to_string(),
        });
    }
    debug!(?hello, "host-api hello accepted");
    connection.send(Frame::new(hello_frame.id, Message::HelloOk(hello_ok)))?;

    let read_loop = {
        let connection = Arc::clone(&connection);
        async move {
            let result = read_loop(&mut reader, &connection, capabilities).await;
            connection.closed.store(true, Ordering::Release);
            writer_task.abort();
            result
        }
    };

    Ok((connection, read_loop))
}

async fn read_loop<R>(
    reader: &mut R,
    connection: &Arc<Connection>,
    capabilities: Arc<dyn CapabilityHandler>,
) -> Result<(), HostApiError>
where
    R: AsyncRead + Unpin,
{
    loop {
        let frame = read_frame(reader).await?;
        match frame.message {
            // Replies to something this side initiated.
            Message::Loaded(_) | Message::Unloaded(_) | Message::Result(_) | Message::Pong => {
                if let Err(CorrelationError::Unknown(id)) = connection.deliver(frame) {
                    warn!(id, "host-api reply matched no pending request, dropping");
                }
            }
            Message::Error(ref e) => {
                let unsolicited = matches!(
                    connection.deliver(frame.clone()),
                    Err(CorrelationError::Unknown(_))
                );
                if unsolicited {
                    return Err(HostApiError::PeerError {
                        code: e.code,
                        message: e.message.clone(),
                    });
                }
            }
            // The executor issues host-call frames; answer via the
            // capability handler and reply on the same connection, reusing
            // its correlation id (spec §6.6). Spawned onto its own task so
            // a slow capability call never blocks delivery of other
            // in-flight replies. The scope answered against is looked up by
            // `body.call_id` (the originating `invoke` frame's own id) --
            // never a connection-wide default -- so a `call_id` that names
            // no in-flight invoke on this connection (forged, stale, or
            // from a different connection/tenant) is refused outright
            // rather than answered against a guessed scope (spec §5.11,
            // `crate::capabilities`'s module doc; this is the fix for the
            // post-M3 review's connection-fixed-scope finding).
            Message::HostCall(body) => {
                let connection = Arc::clone(connection);
                let capabilities = Arc::clone(&capabilities);
                tokio::spawn(async move {
                    let scope = connection.scope_for(body.call_id);
                    let result = match scope {
                        Some(scope) => capabilities.handle(&scope, body).await,
                        None => Err(denied(
                            "unknown_invoke",
                            "host-call call_id does not match any invoke in flight on this connection",
                        )),
                    };
                    let reply = match result {
                        Ok(value) => penguin_bundle_host::wire::HostResultBody {
                            result: Some(value),
                            error: None,
                        },
                        Err(e) => penguin_bundle_host::wire::HostResultBody {
                            result: None,
                            error: Some(e),
                        },
                    };
                    let _ = connection.send(Frame::new(frame.id, Message::HostResult(reply)));
                });
            }
            // Bidirectional heartbeat (fix/executor-link-heartbeat): this
            // stage drives its own `ping`/`pong` round trip via
            // `run_heartbeat`, but also answers an executor-initiated
            // `ping` rather than treating it as the fatal "unexpected
            // frame" every other unhandled kind is below -- the executor
            // side may run its own independent heartbeat (a separate
            // change, on its own branch) and this stage must never close a
            // perfectly healthy connection just because the executor
            // happened to speak first.
            Message::Ping => {
                debug!("host-api heartbeat: received ping, replying pong");
                if let Err(e) = connection.send(Frame::new(frame.id, Message::Pong)) {
                    warn!(error = %e, "host-api failed to reply to ping");
                }
            }
            other => {
                warn!(
                    ?other,
                    "host-api received a frame kind the stage never expects"
                );
                return Err(HostApiError::UnexpectedFrame(
                    "unexpected frame kind on stage side",
                ));
            }
        }
    }
}

/// Consecutive missed heartbeats before [`run_heartbeat`] declares a
/// session dead (fix/executor-link-heartbeat, alpha 2026-10-02 incident).
pub const HEARTBEAT_MISSED_LIMIT: u32 = 3;

/// Heartbeat supervisor for one accepted executor session: sends `ping`
/// every `interval` and requires `pong` back within that same interval.
/// After [`HEARTBEAT_MISSED_LIMIT`] consecutive misses the session is
/// declared dead -- loudly (`ERROR`, naming the peer and how long it's been
/// since the last successful heartbeat in the rendered message, not just
/// structured fields) -- and [`Connection::mark_closed`] is called so
/// [`ConnectionRegistry::active`] stops handing this connection out. The
/// caller (`crate::host_api::serve`) races this future against the read
/// loop via `tokio::select!`; whichever finishes first wins, and dropping
/// the other tears down the underlying socket.
pub async fn run_heartbeat(
    connection: Arc<Connection>,
    peer: SocketAddr,
    interval: Duration,
    metrics: crate::telemetry::HostApiMetrics,
) {
    let mut missed: u32 = 0;
    let mut last_success = Instant::now();
    loop {
        tokio::time::sleep(interval).await;
        if connection.is_closed() {
            debug!(%peer, "host-api heartbeat: connection already closed, stopping");
            return;
        }
        debug!(%peer, "host-api heartbeat: sending ping");
        match connection.ping(interval).await {
            Ok(()) => {
                if missed > 0 {
                    info!(%peer, missed, "host-api heartbeat: pong received, session recovered");
                }
                missed = 0;
                last_success = Instant::now();
                debug!(%peer, "host-api heartbeat: pong received");
            }
            Err(e) => {
                missed += 1;
                let age_s = last_success.elapsed().as_secs();
                if missed >= HEARTBEAT_MISSED_LIMIT {
                    error!(
                        %peer,
                        missed,
                        last_seen_age_s = age_s,
                        error = %e,
                        "host-api heartbeat timeout: dropping dead executor session {peer} \
                         after {missed} consecutive missed heartbeats (last seen {age_s}s ago)"
                    );
                    metrics.heartbeat_timeouts_total.inc();
                    connection.mark_closed();
                    return;
                }
                warn!(
                    %peer,
                    missed,
                    last_seen_age_s = age_s,
                    error = %e,
                    "host-api heartbeat missed ({missed}/{HEARTBEAT_MISSED_LIMIT}) for {peer}, \
                     last seen {age_s}s ago"
                );
            }
        }
    }
}

/// Accepts one TCP connection, completes the TLS handshake, then the
/// `hello`/`hello-ok` host-API handshake -- the production entry point
/// [`crate::dispatch`]'s connection-accept loop calls per incoming
/// connection.
pub async fn accept_and_handshake(
    tcp: tokio::net::TcpStream,
    acceptor: &TlsAcceptor,
    stage_name: &str,
    limits: penguin_bundle_host::wire::HelloLimits,
    expected_sandbox_gvisor: bool,
    capabilities: Arc<dyn CapabilityHandler>,
) -> Result<
    (
        Arc<Connection>,
        impl std::future::Future<Output = Result<(), HostApiError>>,
    ),
    HostApiError,
> {
    let tls_stream = acceptor.accept(tcp).await?;
    run_connection(
        tls_stream,
        HelloOkBody {
            stage: stage_name.to_string(),
            protocol_version: 1,
            limits,
        },
        expected_sandbox_gvisor,
        capabilities,
    )
    .await
}

/// A one-shot channel a caller uses to stop `serve` gracefully.
pub type ShutdownReceiver = oneshot::Receiver<()>;

/// Registry of currently-live executor connections this stage can dispatch
/// `invoke`/`load` requests over. `EXECUTOR_STAGE_CONNECTIONS` (spec §6.6)
/// describes spreading invocations across many connections from one
/// executor replica -- identical shape to
/// `core/svc_process::host_api::ConnectionRegistry`.
///
/// **Regression: svc reported zero executors while holding a live session
/// after reconnect (alpha 2026-10-03).** The previous design kept a single
/// `Mutex<Option<Arc<Connection>>>` "active" slot, overwritten wholesale by
/// every newly accepted connection (`set_active`). That is a race the
/// moment more than one connection is ever in flight for the same stage:
/// if connection B supersedes connection A in the slot and then B itself
/// closes quickly (a duplicate dial, a transient reconnect race, or simply
/// the older connection B replaced), `active()` reports "no executor" --
/// `duration_without_executor` climbs and the watchdog dead-letters
/// everything -- even though connection A is still fully alive, still
/// answering heartbeats, and simply no longer reachable through the slot.
/// Observed live on alpha: `svc-process` held a connection established at
/// 02:09:22 from 10.1.0.33 for the next ~10 minutes (confirmed by that
/// pod's own `connection closed` log only at the 02:20 restart), yet
/// logged "no executor connection" every 60s the entire time. `svc-action`
/// exhibited the identical pattern.
///
/// The fix: every live session is tracked in `sessions`, keyed by a unique
/// per-connection id allocated in [`Self::register`] -- never by peer
/// address (NAT/fast-reconnect can collide there) and never collapsed into
/// a single slot a dead connection can occupy while a live one goes
/// unregistered. Teardown ([`Self::remove`]) removes only its own id, so a
/// session's close can never clear another session's registration. The
/// connected-executor count is the live map size ([`Self::live_count`]).
/// [`Self::active`] picks a live session to dispatch over -- **newest wins**
/// (highest allocated id still reporting `!is_closed()`), preserving this
/// landing's prior single-executor-replica behavior; TODO(M3+) is
/// round-robin across all held connections once a replica legitimately
/// holds more than one. The zero-executor watchdog and `/health` readiness
/// both read through `active`/`live_count`, so they observe exactly the
/// same live set dispatch does.
#[derive(Default)]
pub struct ConnectionRegistry {
    /// Every session this stage currently holds a connection object for
    /// (live or not-yet-pruned-closed), keyed by the id `register` handed
    /// back to the accept task that owns it.
    sessions: Mutex<HashMap<u64, Arc<Connection>>>,
    next_id: AtomicU64,
    /// When [`Self::active`] last transitioned from "some" to "none" --
    /// `None` means either an executor is currently active, or none has
    /// ever connected since this registry was created. Backs both
    /// [`Self::duration_without_executor`] (the `/health` liveness-grace
    /// check and `crate::dispatch`'s loud dead-letter log) and readiness
    /// (`/healthz` is simply `active().is_some()`).
    zero_since: Mutex<Option<Instant>>,
    /// Set once, in production wiring (`crate::try_start_host_api`), to
    /// `telemetry::HostApiMetrics::dead_lettered_no_executor_total` --
    /// `OnceLock` rather than a constructor parameter so the many existing
    /// `ConnectionRegistry::new()` call sites across this crate's tests
    /// don't all need updating for a metric they don't exercise.
    dead_lettered_no_executor_total: std::sync::OnceLock<prometheus::IntCounter>,
}

impl ConnectionRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    /// Registers a newly-handshaken connection as a live session and
    /// returns the id its owning accept task must pass back to
    /// [`Self::remove`] on teardown -- the only correct way to drop a
    /// session, since removing by id can never disturb any other session.
    pub fn register(&self, conn: Arc<Connection>) -> u64 {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        self.sessions
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(id, conn);
        id
    }

    /// Back-compat convenience for tests that only need *a* live session
    /// present and don't care about its id -- production teardown always
    /// goes through [`Self::register`] paired with [`Self::remove`].
    pub fn set_active(&self, conn: Arc<Connection>) {
        self.register(conn);
    }

    /// Removes exactly session `id` -- a no-op if it was already removed or
    /// never existed. Never touches any other session, including one that
    /// has since superseded it as [`Self::active`]'s pick (the alpha
    /// 2026-10-03 bug this type exists to prevent).
    pub fn remove(&self, id: u64) {
        self.sessions
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .remove(&id);
    }

    /// Prunes closed sessions from the map and returns the remaining live
    /// ones. Shared by [`Self::active`] and [`Self::live_count`] so both
    /// observe exactly the same live set on every call.
    fn prune_and_snapshot(&self) -> Vec<(u64, Arc<Connection>)> {
        let mut guard = self.sessions.lock().unwrap_or_else(|e| e.into_inner());
        guard.retain(|_, conn| !conn.is_closed());
        guard
            .iter()
            .map(|(id, conn)| (*id, Arc::clone(conn)))
            .collect()
    }

    /// Number of sessions currently reporting a live (non-closed)
    /// connection -- the correct `connected_executors` gauge value,
    /// replacing the old `registry.active().is_some() as i64` which could
    /// read 0 while other live sessions existed outside the single slot.
    pub fn live_count(&self) -> usize {
        self.prune_and_snapshot().len()
    }

    /// Returns a live session to dispatch over, or `None` if none remain.
    /// Selection is "newest live session wins" (see the type doc) -- a
    /// closed connection is never returned, so callers fail over to "no
    /// executor available" (`error.kind = "executor_unavailable"`, spec
    /// §6.3) rather than sending into a dead socket.
    pub fn active(&self) -> Option<Arc<Connection>> {
        let live = self.prune_and_snapshot();
        let active = live
            .into_iter()
            .max_by_key(|(id, _)| *id)
            .map(|(_, conn)| conn);
        let mut zero_since = self.zero_since.lock().unwrap_or_else(|e| e.into_inner());
        if active.is_some() {
            *zero_since = None;
        } else {
            zero_since.get_or_insert_with(Instant::now);
        }
        active
    }

    /// How long this registry has held zero live executor sessions --
    /// `Duration::ZERO` while one is active. Fix/executor-link-heartbeat:
    /// backs both `crate::dispatch::handle_delivered`'s loud dead-letter
    /// log (names the outage duration in the rendered message, not just a
    /// field) and `/health`'s liveness-grace check (fail liveness only
    /// after `EXECUTOR_GRACE_SECONDS`, not on every transient reconnect).
    pub fn duration_without_executor(&self) -> Duration {
        if self.active().is_some() {
            return Duration::ZERO;
        }
        let zero_since = self.zero_since.lock().unwrap_or_else(|e| e.into_inner());
        zero_since.map(|t| t.elapsed()).unwrap_or(Duration::ZERO)
    }

    /// Wires the `dispatch_dead_lettered_no_executor_total` counter in --
    /// called exactly once, from `crate::try_start_host_api`.
    pub fn set_dead_letter_metric(&self, counter: prometheus::IntCounter) {
        let _ = self.dead_lettered_no_executor_total.set(counter);
    }

    /// Increments `dispatch_dead_lettered_no_executor_total` if the metric
    /// has been wired (see [`Self::set_dead_letter_metric`]) -- a no-op
    /// (never a panic) in the many tests that build a bare
    /// `ConnectionRegistry::new()` without wiring telemetry.
    pub fn record_dead_letter_no_executor(&self) {
        if let Some(counter) = self.dead_lettered_no_executor_total.get() {
            counter.inc();
        }
    }
}

/// Fixed cadence for [`run_zero_executor_watchdog`]'s periodic ERROR log --
/// independent of `EXECUTOR_GRACE_SECONDS` (the outage threshold), which is
/// configurable.
const ZERO_EXECUTOR_WATCHDOG_PERIOD: Duration = Duration::from_secs(60);

/// Logs loudly, on a fixed cadence (`period`), while the executor link has
/// been down longer than `grace` -- regression: readiness gated on
/// executor connection deadlocked rollouts (alpha 2026-10-02). Distinct
/// from `crate::dispatch::handle_delivered`'s per-message dead-letter ERROR
/// log: that one only fires when work is actually waiting to dispatch, so
/// an idle stage with zero executors and nothing queued would otherwise go
/// unlogged indefinitely. Runs for the lifetime of the process -- like
/// `run_heartbeat`, it is not wired to `shutdown` and simply stops when the
/// runtime does. `period` is a parameter (production wiring always passes
/// [`ZERO_EXECUTOR_WATCHDOG_PERIOD`]) purely so tests can use a short tick
/// instead of waiting out the real 60s cadence.
pub async fn run_zero_executor_watchdog(
    registry: Arc<ConnectionRegistry>,
    grace: Duration,
    period: Duration,
) {
    let mut interval = tokio::time::interval(period);
    interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    loop {
        interval.tick().await;
        let outage = registry.duration_without_executor();
        if outage >= grace {
            let outage_s = outage.as_secs();
            let grace_s = grace.as_secs();
            tracing::error!(
                no_executor_for_s = outage_s,
                grace_s,
                "no executor connection for {outage_s}s, past the {grace_s}s grace period -- \
                 dispatch is dead-lettering all work for redelivery"
            );
        }
    }
}

/// Binds `cli.host_api_port`, accepts connections in a loop, and registers
/// each successfully-handshaken one as [`ConnectionRegistry`]'s active
/// connection -- the production entry point `crate::lib::try_start_host_api`
/// spawns. Runs until `shutdown` resolves; a per-connection failure
/// (bad cert, wrong sandbox posture, protocol error) is logged and the
/// loop keeps accepting rather than exiting, matching spec §7.5's
/// per-connection (not whole-listener) failure model.
pub async fn serve(
    cli: crate::config::CliConfig,
    registry: Arc<ConnectionRegistry>,
    capabilities: Arc<dyn CapabilityHandler>,
    mut shutdown: tokio::sync::oneshot::Receiver<()>,
    metrics: crate::telemetry::HostApiMetrics,
) -> Result<(), HostApiError> {
    let server_config = build_server_config(&cli)?;
    let acceptor = TlsAcceptor::from(Arc::new(server_config));
    let addr = std::net::SocketAddr::new(cli.bind_addr, cli.host_api_port);
    let listener = tokio::net::TcpListener::bind(addr).await?;
    tracing::info!(%addr, "host-api mTLS listener bound");

    let limits = penguin_bundle_host::wire::HelloLimits {
        call_timeout_ms: cli.executor_call_timeout_ms,
        memory_mb: 64,
        max_concurrent_calls: 32,
    };
    let heartbeat_interval = cli.heartbeat_interval();
    tokio::spawn(run_zero_executor_watchdog(
        Arc::clone(&registry),
        cli.executor_grace(),
        ZERO_EXECUTOR_WATCHDOG_PERIOD,
    ));

    loop {
        tokio::select! {
            _ = &mut shutdown => return Ok(()),
            accepted = listener.accept() => {
                let (tcp, peer) = match accepted {
                    Ok(v) => v,
                    Err(e) => {
                        warn!(error = %e, "host-api accept failed");
                        continue;
                    }
                };
                // TCP keepalive (fix/executor-link-heartbeat, alpha
                // 2026-10-02 incident): the application-level `ping`/`pong`
                // heartbeat above is the primary detection mechanism, but
                // OS-level keepalive is cheap defense in depth for the
                // window before the first heartbeat round trip completes.
                // A failure to set it is logged, never fatal to accepting
                // the connection.
                if let Err(e) = SockRef::from(&tcp).set_tcp_keepalive(
                    &TcpKeepalive::new()
                        .with_time(heartbeat_interval)
                        .with_interval(heartbeat_interval),
                ) {
                    warn!(%peer, error = %e, "host-api: failed to set TCP keepalive on accepted socket");
                }
                let acceptor = acceptor.clone();
                let registry = Arc::clone(&registry);
                let capabilities = Arc::clone(&capabilities);
                let stage_name = "svc-action".to_string();
                let expected_gvisor = cli.sandbox_gvisor;
                let limits = limits.clone();
                let metrics = metrics.clone();
                tokio::spawn(async move {
                    match accept_and_handshake(tcp, &acceptor, &stage_name, limits, expected_gvisor, capabilities).await {
                        Ok((connection, read_loop)) => {
                            info!(%peer, "host-api connection established with executor {peer}");
                            // Own id, not a slot overwrite (regression:
                            // alpha 2026-10-03 -- see `ConnectionRegistry`'s
                            // doc): this session's own teardown below
                            // removes only `session_id`, so it can never
                            // clear a different, still-live session's
                            // registration out from under it.
                            let session_id = registry.register(Arc::clone(&connection));
                            metrics.connected_executors.set(registry.live_count() as i64);
                            let heartbeat = run_heartbeat(
                                Arc::clone(&connection),
                                peer,
                                heartbeat_interval,
                                metrics.clone(),
                            );
                            tokio::pin!(read_loop);
                            tokio::pin!(heartbeat);
                            tokio::select! {
                                res = &mut read_loop => {
                                    if let Err(e) = res {
                                        warn!(%peer, error = %e, "host-api connection closed");
                                    }
                                }
                                _ = &mut heartbeat => {
                                    warn!(%peer, "host-api connection dropped by heartbeat supervisor");
                                }
                            }
                            registry.remove(session_id);
                            metrics.connected_executors.set(registry.live_count() as i64);
                        }
                        Err(e) => warn!(%peer, error = %e, "host-api handshake failed"),
                    }
                });
            }
        }
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use crate::capabilities::DenyAllCapabilities;
    use penguin_bundle_host::wire::{
        HelloBody, HelloLimits, InvokeBody, LoadBody, LoadLimits, LoadedBody, SandboxInfo,
    };

    fn test_limits() -> HelloLimits {
        HelloLimits {
            call_timeout_ms: 2000,
            memory_mb: 64,
            max_concurrent_calls: 32,
        }
    }

    fn hello_body(sandbox_runtime: &str) -> HelloBody {
        HelloBody {
            protocol_version: 1,
            executor_version: "0.1.0".to_string(),
            wasmtime_version: "49.0.0".to_string(),
            wasmtime_abi: "test".to_string(),
            collector: "drc".to_string(),
            sandbox: SandboxInfo {
                runtime: sandbox_runtime.to_string(),
                verified: sandbox_runtime == "gvisor",
            },
        }
    }

    /// Drives the *executor* side of an in-memory duplex for the handshake
    /// tests: sends `hello`, expects `hello-ok`.
    async fn run_fake_executor_handshake<S: AsyncRead + AsyncWrite + Unpin>(
        mut io: S,
        sandbox_runtime: &str,
    ) -> Frame {
        write_frame(
            &mut io,
            &Frame::new(1, Message::Hello(hello_body(sandbox_runtime))),
        )
        .await
        .expect("write hello");
        read_frame(&mut io).await.expect("hello-ok or error reply")
    }

    #[tokio::test]
    async fn handshake_succeeds_with_matching_sandbox_posture() {
        let (stage_io, executor_io) = tokio::io::duplex(64 * 1024);
        let executor = tokio::spawn(run_fake_executor_handshake(executor_io, "runc"));
        let (connection, read_loop) = run_connection(
            stage_io,
            HelloOkBody {
                stage: "svc-action".to_string(),
                protocol_version: 1,
                limits: test_limits(),
            },
            false,
            Arc::new(DenyAllCapabilities),
        )
        .await
        .expect("handshake succeeds");
        // Checked immediately, before spawning `read_loop` -- once spawned,
        // it races the already-completed `executor` task's EOF (that task
        // drops its IO half on return), so this is the only
        // non-racy point at which "handshake succeeded, connection is
        // live" is deterministically true.
        assert!(!connection.is_closed());
        tokio::spawn(read_loop);

        let reply = executor.await.expect("executor task");
        assert!(matches!(reply.message, Message::HelloOk(_)));
    }

    #[tokio::test]
    async fn handshake_refuses_unsandboxed_executor_when_gvisor_expected() {
        let (stage_io, executor_io) = tokio::io::duplex(64 * 1024);
        let executor = tokio::spawn(run_fake_executor_handshake(executor_io, "runc"));
        let result = run_connection(
            stage_io,
            HelloOkBody {
                stage: "svc-action".to_string(),
                protocol_version: 1,
                limits: test_limits(),
            },
            true,
            Arc::new(DenyAllCapabilities),
        )
        .await;
        assert!(matches!(
            result,
            Err(HostApiError::PeerError {
                code: ErrorCode::UnsandboxedExecutor,
                ..
            })
        ));
        let reply = executor.await.expect("executor task");
        assert!(matches!(
            reply.message,
            Message::Error(ErrorBody {
                code: ErrorCode::UnsandboxedExecutor,
                ..
            })
        ));
    }

    #[tokio::test]
    async fn handshake_accepts_gvisor_when_gvisor_expected() {
        let (stage_io, executor_io) = tokio::io::duplex(64 * 1024);
        let executor = tokio::spawn(run_fake_executor_handshake(executor_io, "gvisor"));
        let (connection, read_loop) = run_connection(
            stage_io,
            HelloOkBody {
                stage: "svc-action".to_string(),
                protocol_version: 1,
                limits: test_limits(),
            },
            true,
            Arc::new(DenyAllCapabilities),
        )
        .await
        .expect("handshake succeeds");
        assert!(!connection.is_closed());
        tokio::spawn(read_loop);
        let reply = executor.await.expect("executor task");
        assert!(matches!(reply.message, Message::HelloOk(_)));
        let _ = connection;
    }

    #[tokio::test]
    async fn handshake_rejects_wrong_protocol_version() {
        let (stage_io, executor_io) = tokio::io::duplex(64 * 1024);
        let mut hello = hello_body("runc");
        hello.protocol_version = 99;
        tokio::spawn(async move {
            let mut io = executor_io;
            write_frame(&mut io, &Frame::new(1, Message::Hello(hello)))
                .await
                .expect("write hello");
        });
        let result = run_connection(
            stage_io,
            HelloOkBody {
                stage: "svc-action".to_string(),
                protocol_version: 1,
                limits: test_limits(),
            },
            false,
            Arc::new(DenyAllCapabilities),
        )
        .await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn first_frame_not_hello_is_rejected() {
        let (stage_io, executor_io) = tokio::io::duplex(64 * 1024);
        tokio::spawn(async move {
            let mut io = executor_io;
            write_frame(&mut io, &Frame::new(1, Message::Ping))
                .await
                .expect("write ping");
        });
        let result = run_connection(
            stage_io,
            HelloOkBody {
                stage: "svc-action".to_string(),
                protocol_version: 1,
                limits: test_limits(),
            },
            false,
            Arc::new(DenyAllCapabilities),
        )
        .await;
        assert!(matches!(
            result,
            Err(HostApiError::UnexpectedFrame("expected hello"))
        ));
    }

    /// Full round trip: after handshake, the stage sends `load`, the fake
    /// executor replies `loaded`; the stage then sends `invoke`, the
    /// executor issues a `host-call`, and the stage's capability handler
    /// answers it.
    #[tokio::test]
    async fn load_and_invoke_round_trip_with_host_call() {
        use crate::capabilities::{RelayQueue, StageCapabilities};
        use std::future::Future;
        use std::pin::Pin;

        struct NoopQueue;
        impl RelayQueue for NoopQueue {
            fn lpush<'a>(
                &'a self,
                _key: &'a str,
                _value: String,
            ) -> Pin<Box<dyn Future<Output = Result<(), String>> + Send + 'a>> {
                Box::pin(async { Ok(()) })
            }
        }

        let (stage_io, executor_io) = tokio::io::duplex(64 * 1024);
        let egress = Arc::new(crate::egress::EgressGuard::new(
            Arc::new(crate::egress::ReqwestTransport::new()),
            crate::egress::EgressLimits {
                allow_private_hosts: false,
                rate_limit_rps: 10,
                rate_limit_burst: 20,
                timeout: std::time::Duration::from_secs(5),
                max_redirects: 3,
                max_response_bytes: 1_048_576,
                allowed_ports: vec![443],
                proxy_url: None,
            },
            Arc::new(crate::distribution::BundleCatalog::new()),
            prometheus::IntCounterVec::new(
                prometheus::Opts::new("test_egress_denied_total_hostapi", "test"),
                &["app_id", "reason"],
            )
            .unwrap(),
            crate::flags::boxed(crate::flags::StaticFlag(true)),
        ));
        let capabilities: Arc<dyn CapabilityHandler> = Arc::new(StageCapabilities::<
            _,
            redis::aio::MultiplexedConnection,
        >::new(
            NoopQueue,
            egress,
            Arc::new(std::sync::Mutex::new(crate::usage::UsageBatcher::new())),
        ));

        let executor = tokio::spawn(async move {
            let mut io = executor_io;
            write_frame(&mut io, &Frame::new(1, Message::Hello(hello_body("runc"))))
                .await
                .unwrap();
            let hello_ok = read_frame(&mut io).await.unwrap();
            assert!(matches!(hello_ok.message, Message::HelloOk(_)));

            let load = read_frame(&mut io).await.unwrap();
            let (load_id, load_body) = match load.message {
                Message::Load(b) => (load.id, b),
                other => panic!("expected load, got {other:?}"),
            };
            write_frame(
                &mut io,
                &Frame::new(
                    load_id,
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

            let invoke = read_frame(&mut io).await.unwrap();
            let invoke_id = invoke.id;
            assert!(matches!(invoke.message, Message::Invoke(_)));

            // Issue a host-call mid-invoke (clock capability).
            write_frame(
                &mut io,
                &Frame::new(
                    2,
                    Message::HostCall(penguin_bundle_host::wire::HostCallBody {
                        app_id: "waddles.bot.commands.default".to_string(),
                        capability: penguin_bundle_host::wire::CapabilityKind::Clock,
                        op: "now-millis".to_string(),
                        args: serde_json::json!({}),
                        call_id: invoke_id,
                    }),
                ),
            )
            .await
            .unwrap();
            let host_result = read_frame(&mut io).await.unwrap();
            let host_result_body = match host_result.message {
                Message::HostResult(b) => b,
                other => panic!("expected host-result, got {other:?}"),
            };
            // Proves the per-invoke scope (registered by `Connection::
            // invoke` below) actually reached the capability handler: a
            // missing/mismatched scope would answer `unknown_invoke`
            // instead of a real clock value (spec §5.11, the fix this test
            // guards against regressing).
            assert!(
                host_result_body.error.is_none(),
                "expected the clock host-call to succeed, got {:?}",
                host_result_body.error
            );

            write_frame(
                &mut io,
                &Frame::new(
                    invoke_id,
                    Message::Result(penguin_bundle_host::wire::ResultBody {
                        payload: serde_json::json!({"ok": true}),
                        duration_ms: 3,
                        fuel_used: 0,
                    }),
                ),
            )
            .await
            .unwrap();
        });

        let (connection, read_loop) = run_connection(
            stage_io,
            HelloOkBody {
                stage: "svc-action".to_string(),
                protocol_version: 1,
                limits: test_limits(),
            },
            false,
            capabilities,
        )
        .await
        .expect("handshake succeeds");
        let read_loop_handle = tokio::spawn(read_loop);

        let loaded = connection
            .request(Message::Load(LoadBody {
                tenant_id: 1,
                community_id: 0,
                app_id: "waddles.bot.commands.default".to_string(),
                version: "1".to_string(),
                digest: "sha256:00".to_string(),
                component_key: "k".to_string(),
                sidecar_key: "s".to_string(),
                capabilities: vec![],
                limits: LoadLimits {
                    timeout_ms: 2000,
                    memory_mb: 64,
                },
            }))
            .await
            .expect("load succeeds");
        assert!(matches!(loaded.message, Message::Loaded(_)));

        let result = connection
            .invoke(
                InvokeBody {
                    app_id: "waddles.bot.commands.default".to_string(),
                    digest: "sha256:00".to_string(),
                    export: penguin_bundle_host::wire::ExportKind::Dispatch,
                    payload: serde_json::json!({}),
                    deadline_ms: 2000,
                    trace: None,
                },
                crate::capabilities::InvokeScope {
                    tenant: "acme".to_string(),
                    community: None,
                    app_id: "waddles.bot.commands.default".to_string(),
                    origin_channel_id: None,
                },
            )
            .await
            .expect("invoke succeeds");
        if let Message::Result(r) = result.message {
            assert_eq!(r.payload["ok"], serde_json::json!(true));
        } else {
            panic!("expected result");
        }

        executor.await.expect("executor task");
        // Give the read loop a moment to see EOF and mark the connection
        // closed, then stop it explicitly (its underlying io just ended).
        let _ = tokio::time::timeout(std::time::Duration::from_secs(2), read_loop_handle).await;
    }

    /// **The end-to-end proof this M3 landing exists to deliver**: a
    /// bundle's `relay`/`send` host-call, issued mid-`invoke` exactly as a
    /// real executor would relay it from a WASM guest, reaches a real
    /// `LPUSH` onto the Twitch outbound relay key -- through the exact
    /// connection/capability wiring `crate::lib::try_start_host_api`
    /// installs in production (mTLS handshake, `load`, per-invoke-scoped
    /// `invoke`, `host-call` dispatch), not a shortcut that calls
    /// `StageCapabilities::handle_relay` directly. Where a real deployment
    /// would `BRPOP` this key from a Valkey connection and write it to the
    /// Twitch IRC socket (`libs/waddle_transports/transports/irc_relay.py`,
    /// outside this crate's own process boundary), this test asserts the
    /// exact key and payload that reader consumes.
    #[tokio::test]
    async fn bundle_relay_host_call_reaches_a_twitch_lpush_end_to_end() {
        use crate::capabilities::{InvokeScope, RelayQueue, StageCapabilities};
        use std::future::Future;
        use std::pin::Pin;
        use std::sync::Mutex as StdMutex;

        #[derive(Default)]
        struct RecordingRelayQueue {
            pushed: StdMutex<Vec<(String, String)>>,
        }
        impl RelayQueue for RecordingRelayQueue {
            fn lpush<'a>(
                &'a self,
                key: &'a str,
                value: String,
            ) -> Pin<Box<dyn Future<Output = Result<(), String>> + Send + 'a>> {
                self.pushed
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .push((key.to_string(), value));
                Box::pin(async { Ok(()) })
            }
        }

        let (stage_io, executor_io) = tokio::io::duplex(64 * 1024);
        let relay_queue = Arc::new(RecordingRelayQueue::default());
        let egress = Arc::new(crate::egress::EgressGuard::new(
            Arc::new(crate::egress::ReqwestTransport::new()),
            crate::egress::EgressLimits {
                allow_private_hosts: false,
                rate_limit_rps: 10,
                rate_limit_burst: 20,
                timeout: std::time::Duration::from_secs(5),
                max_redirects: 3,
                max_response_bytes: 1_048_576,
                allowed_ports: vec![443],
                proxy_url: None,
            },
            Arc::new(crate::distribution::BundleCatalog::new()),
            prometheus::IntCounterVec::new(
                prometheus::Opts::new("test_egress_denied_total_relay_e2e", "test"),
                &["app_id", "reason"],
            )
            .unwrap(),
            crate::flags::boxed(crate::flags::StaticFlag(true)),
        ));
        // `Arc<RecordingRelayQueue>` doesn't itself impl `RelayQueue` (the
        // impl is on the concrete type); wrap so `StageCapabilities` can
        // still observe pushes through the shared `Arc` after being moved.
        struct SharedQueue(Arc<RecordingRelayQueue>);
        impl RelayQueue for SharedQueue {
            fn lpush<'a>(
                &'a self,
                key: &'a str,
                value: String,
            ) -> Pin<Box<dyn Future<Output = Result<(), String>> + Send + 'a>> {
                self.0.lpush(key, value)
            }
        }
        let capabilities: Arc<dyn CapabilityHandler> = Arc::new(StageCapabilities::<
            _,
            redis::aio::MultiplexedConnection,
        >::new(
            SharedQueue(Arc::clone(&relay_queue)),
            egress,
            Arc::new(std::sync::Mutex::new(crate::usage::UsageBatcher::new())),
        ));

        let executor = tokio::spawn(async move {
            let mut io = executor_io;
            write_frame(&mut io, &Frame::new(1, Message::Hello(hello_body("runc"))))
                .await
                .unwrap();
            read_frame(&mut io).await.unwrap();

            let load = read_frame(&mut io).await.unwrap();
            let (load_id, load_body) = match load.message {
                Message::Load(b) => (load.id, b),
                other => panic!("expected load, got {other:?}"),
            };
            write_frame(
                &mut io,
                &Frame::new(
                    load_id,
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

            let invoke = read_frame(&mut io).await.unwrap();
            let invoke_id = invoke.id;
            assert!(matches!(invoke.message, Message::Invoke(_)));

            // The bundle's `dispatch` export, mid-invoke, issues exactly
            // the `relay`/`send` host-call `crate::senders::
            // twitch_relay_args` documents as the wire shape a Twitch
            // send takes.
            write_frame(
                &mut io,
                &Frame::new(
                    2,
                    Message::HostCall(penguin_bundle_host::wire::HostCallBody {
                        app_id: "waddles.bot.commands.default".to_string(),
                        capability: penguin_bundle_host::wire::CapabilityKind::Relay,
                        op: "send".to_string(),
                        args: crate::senders::twitch_relay_args(
                            "#somechannel",
                            "hello from waddles",
                        ),
                        call_id: invoke_id,
                    }),
                ),
            )
            .await
            .unwrap();
            let host_result = read_frame(&mut io).await.unwrap();
            match host_result.message {
                Message::HostResult(b) => {
                    let result = b.result.expect("relay send succeeds end to end");
                    assert_eq!(result["queued"], serde_json::json!(true));
                }
                other => panic!("expected host-result, got {other:?}"),
            }

            write_frame(
                &mut io,
                &Frame::new(
                    invoke_id,
                    Message::Result(penguin_bundle_host::wire::ResultBody {
                        payload: serde_json::json!({"ok": true, "status": 200, "detail": "sent"}),
                        duration_ms: 3,
                        fuel_used: 0,
                    }),
                ),
            )
            .await
            .unwrap();
        });

        let (connection, read_loop) = run_connection(
            stage_io,
            HelloOkBody {
                stage: "svc-action".to_string(),
                protocol_version: 1,
                limits: test_limits(),
            },
            false,
            capabilities,
        )
        .await
        .expect("handshake succeeds");
        let read_loop_handle = tokio::spawn(read_loop);

        connection
            .request(Message::Load(LoadBody {
                tenant_id: 1,
                community_id: 0,
                app_id: "waddles.bot.commands.default".to_string(),
                version: "1".to_string(),
                digest: "sha256:00".to_string(),
                component_key: "k".to_string(),
                sidecar_key: "s".to_string(),
                capabilities: vec![],
                limits: LoadLimits {
                    timeout_ms: 2000,
                    memory_mb: 64,
                },
            }))
            .await
            .expect("load succeeds");

        let result = connection
            .invoke(
                InvokeBody {
                    app_id: "waddles.bot.commands.default".to_string(),
                    digest: "sha256:00".to_string(),
                    export: penguin_bundle_host::wire::ExportKind::Dispatch,
                    payload: serde_json::json!({}),
                    deadline_ms: 2000,
                    trace: None,
                },
                InvokeScope {
                    tenant: "acme".to_string(),
                    community: Some("main".to_string()),
                    app_id: "waddles.bot.commands.default".to_string(),
                    origin_channel_id: None,
                },
            )
            .await
            .expect("invoke succeeds");
        assert!(matches!(result.message, Message::Result(_)));

        executor.await.expect("executor task");
        let _ = tokio::time::timeout(std::time::Duration::from_secs(2), read_loop_handle).await;

        // The proof: exactly one LPUSH landed on the Twitch outbound relay
        // key, with the sanitized channel/text the bundle sent -- the
        // handoff point to the process that actually opens the Twitch IRC
        // socket.
        let pushed = relay_queue.pushed.lock().unwrap();
        assert_eq!(pushed.len(), 1);
        assert_eq!(pushed[0].0, "waddles:transport:irc:twitch:outbound");
        let parsed: serde_json::Value = serde_json::from_str(&pushed[0].1).unwrap();
        assert_eq!(parsed["channel"], "#somechannel");
        assert_eq!(parsed["text"], "hello from waddles");
    }

    /// Regression coverage for the mandatory negative test: "a cross-tenant
    /// call_id is refused". A `host-call` whose `call_id` does not match
    /// any `invoke` currently in flight on this connection (here: a
    /// `call_id` that was never registered at all, the same shape a
    /// forged/stale/cross-connection `call_id` would take) must be denied
    /// `unknown_invoke` -- never answered against some other invoke's
    /// scope, and never against a fabricated default.
    #[tokio::test]
    async fn host_call_with_an_unregistered_call_id_is_refused_as_unknown_invoke() {
        use crate::capabilities::{RelayQueue, StageCapabilities};
        use std::future::Future;
        use std::pin::Pin;

        struct NoopQueue;
        impl RelayQueue for NoopQueue {
            fn lpush<'a>(
                &'a self,
                _key: &'a str,
                _value: String,
            ) -> Pin<Box<dyn Future<Output = Result<(), String>> + Send + 'a>> {
                Box::pin(async { Ok(()) })
            }
        }

        let (stage_io, executor_io) = tokio::io::duplex(64 * 1024);
        let egress = Arc::new(crate::egress::EgressGuard::new(
            Arc::new(crate::egress::ReqwestTransport::new()),
            crate::egress::EgressLimits {
                allow_private_hosts: false,
                rate_limit_rps: 10,
                rate_limit_burst: 20,
                timeout: std::time::Duration::from_secs(5),
                max_redirects: 3,
                max_response_bytes: 1_048_576,
                allowed_ports: vec![443],
                proxy_url: None,
            },
            Arc::new(crate::distribution::BundleCatalog::new()),
            prometheus::IntCounterVec::new(
                prometheus::Opts::new("test_egress_denied_total_unknown_invoke", "test"),
                &["app_id", "reason"],
            )
            .unwrap(),
            crate::flags::boxed(crate::flags::StaticFlag(true)),
        ));
        let capabilities: Arc<dyn CapabilityHandler> = Arc::new(StageCapabilities::<
            _,
            redis::aio::MultiplexedConnection,
        >::new(
            NoopQueue,
            egress,
            Arc::new(std::sync::Mutex::new(crate::usage::UsageBatcher::new())),
        ));

        let executor = tokio::spawn(async move {
            let mut io = executor_io;
            write_frame(&mut io, &Frame::new(1, Message::Hello(hello_body("runc"))))
                .await
                .unwrap();
            read_frame(&mut io).await.unwrap();

            // No `invoke` was ever sent on this connection -- `call_id:
            // 9999` names nothing in flight.
            write_frame(
                &mut io,
                &Frame::new(
                    2,
                    Message::HostCall(penguin_bundle_host::wire::HostCallBody {
                        app_id: "waddles.bot.commands.default".to_string(),
                        capability: penguin_bundle_host::wire::CapabilityKind::Clock,
                        op: "now-millis".to_string(),
                        args: serde_json::json!({}),
                        call_id: 9999,
                    }),
                ),
            )
            .await
            .unwrap();
            let host_result = read_frame(&mut io).await.unwrap();
            match host_result.message {
                Message::HostResult(b) => {
                    let err = b
                        .error
                        .expect("expected an error, call_id was never registered");
                    assert_eq!(err.code, "unknown_invoke");
                }
                other => panic!("expected host-result, got {other:?}"),
            }
        });

        let (_connection, read_loop) = run_connection(
            stage_io,
            HelloOkBody {
                stage: "svc-action".to_string(),
                protocol_version: 1,
                limits: test_limits(),
            },
            false,
            capabilities,
        )
        .await
        .expect("handshake succeeds");
        let read_loop_handle = tokio::spawn(read_loop);

        executor.await.expect("executor task");
        let _ = tokio::time::timeout(std::time::Duration::from_secs(2), read_loop_handle).await;
    }

    /// The literal regression test for the post-M3 review finding this
    /// module fixes: two concurrent `invoke`s on the **same** connection,
    /// for two different tenants, each answered against its *own*
    /// `InvokeScope` -- never the other's. Before this fix,
    /// `StageCapabilities` was scoped once at connection-construction time,
    /// so every `host-call` on a connection resolved to whichever tenant
    /// happened to be first; this test would have failed under that design
    /// (both `context` calls would report the same tenant).
    #[tokio::test]
    async fn concurrent_invokes_for_different_tenants_never_cross_scope() {
        use crate::capabilities::{InvokeScope, RelayQueue, StageCapabilities};
        use std::future::Future;
        use std::pin::Pin;

        struct NoopQueue;
        impl RelayQueue for NoopQueue {
            fn lpush<'a>(
                &'a self,
                _key: &'a str,
                _value: String,
            ) -> Pin<Box<dyn Future<Output = Result<(), String>> + Send + 'a>> {
                Box::pin(async { Ok(()) })
            }
        }

        let (stage_io, executor_io) = tokio::io::duplex(64 * 1024);
        let egress = Arc::new(crate::egress::EgressGuard::new(
            Arc::new(crate::egress::ReqwestTransport::new()),
            crate::egress::EgressLimits {
                allow_private_hosts: false,
                rate_limit_rps: 10,
                rate_limit_burst: 20,
                timeout: std::time::Duration::from_secs(5),
                max_redirects: 3,
                max_response_bytes: 1_048_576,
                allowed_ports: vec![443],
                proxy_url: None,
            },
            Arc::new(crate::distribution::BundleCatalog::new()),
            prometheus::IntCounterVec::new(
                prometheus::Opts::new("test_egress_denied_total_cross_tenant", "test"),
                &["app_id", "reason"],
            )
            .unwrap(),
            crate::flags::boxed(crate::flags::StaticFlag(true)),
        ));
        let capabilities: Arc<dyn CapabilityHandler> = Arc::new(StageCapabilities::<
            _,
            redis::aio::MultiplexedConnection,
        >::new(
            NoopQueue,
            egress,
            Arc::new(std::sync::Mutex::new(crate::usage::UsageBatcher::new())),
        ));

        let executor = tokio::spawn(async move {
            let mut io = executor_io;
            write_frame(&mut io, &Frame::new(1, Message::Hello(hello_body("runc"))))
                .await
                .unwrap();
            read_frame(&mut io).await.unwrap();

            // Both `invoke`s land before either is answered -- the fake
            // executor reads both frames up front, mirroring a real
            // executor multiplexing two concurrent calls on one
            // connection.
            let invoke_a = read_frame(&mut io).await.unwrap();
            let invoke_b = read_frame(&mut io).await.unwrap();
            let (id_a, body_a) = match invoke_a.message {
                Message::Invoke(b) => (invoke_a.id, b),
                other => panic!("expected invoke, got {other:?}"),
            };
            let (id_b, _body_b) = match invoke_b.message {
                Message::Invoke(b) => (invoke_b.id, b),
                other => panic!("expected invoke, got {other:?}"),
            };

            // Issue a `context` host-call for each invoke's own id.
            write_frame(
                &mut io,
                &Frame::new(
                    100,
                    Message::HostCall(penguin_bundle_host::wire::HostCallBody {
                        app_id: body_a.app_id.clone(),
                        capability: penguin_bundle_host::wire::CapabilityKind::Context,
                        op: "get".to_string(),
                        args: serde_json::json!({}),
                        call_id: id_a,
                    }),
                ),
            )
            .await
            .unwrap();
            let result_a = read_frame(&mut io).await.unwrap();
            let result_b_body = {
                write_frame(
                    &mut io,
                    &Frame::new(
                        101,
                        Message::HostCall(penguin_bundle_host::wire::HostCallBody {
                            app_id: "waddles.bot.commands.default".to_string(),
                            capability: penguin_bundle_host::wire::CapabilityKind::Context,
                            op: "get".to_string(),
                            args: serde_json::json!({}),
                            call_id: id_b,
                        }),
                    ),
                )
                .await
                .unwrap();
                read_frame(&mut io).await.unwrap()
            };

            let tenant_of = |frame: Frame| -> String {
                match frame.message {
                    Message::HostResult(b) => b
                        .result
                        .expect("context call succeeds")
                        .get("tenant")
                        .and_then(|v| v.as_str())
                        .unwrap()
                        .to_string(),
                    other => panic!("expected host-result, got {other:?}"),
                }
            };
            assert_eq!(tenant_of(result_a), "tenant-a");
            assert_eq!(tenant_of(result_b_body), "tenant-b");

            for (id, digest) in [(id_a, "sha256:aa"), (id_b, "sha256:bb")] {
                write_frame(
                    &mut io,
                    &Frame::new(
                        id,
                        Message::Result(penguin_bundle_host::wire::ResultBody {
                            payload: serde_json::json!({"ok": true, "digest": digest}),
                            duration_ms: 1,
                            fuel_used: 0,
                        }),
                    ),
                )
                .await
                .unwrap();
            }
        });

        let (connection, read_loop) = run_connection(
            stage_io,
            HelloOkBody {
                stage: "svc-action".to_string(),
                protocol_version: 1,
                limits: test_limits(),
            },
            false,
            capabilities,
        )
        .await
        .expect("handshake succeeds");
        tokio::spawn(read_loop);

        let (result_a, result_b) = tokio::join!(
            connection.invoke(
                InvokeBody {
                    app_id: "waddles.bot.commands.default".to_string(),
                    digest: "sha256:aa".to_string(),
                    export: penguin_bundle_host::wire::ExportKind::Dispatch,
                    payload: serde_json::json!({}),
                    deadline_ms: 2000,
                    trace: None,
                },
                InvokeScope {
                    tenant: "tenant-a".to_string(),
                    community: None,
                    app_id: "waddles.bot.commands.default".to_string(),
                    origin_channel_id: None,
                },
            ),
            connection.invoke(
                InvokeBody {
                    app_id: "waddles.bot.commands.default".to_string(),
                    digest: "sha256:bb".to_string(),
                    export: penguin_bundle_host::wire::ExportKind::Dispatch,
                    payload: serde_json::json!({}),
                    deadline_ms: 2000,
                    trace: None,
                },
                InvokeScope {
                    tenant: "tenant-b".to_string(),
                    community: None,
                    app_id: "waddles.bot.commands.default".to_string(),
                    origin_channel_id: None,
                },
            ),
        );
        result_a.expect("invoke a succeeds");
        result_b.expect("invoke b succeeds");

        executor.await.expect("executor task");
    }

    #[test]
    fn connection_registry_starts_empty() {
        let registry = ConnectionRegistry::new();
        assert!(registry.active().is_none());
    }

    #[tokio::test]
    async fn connection_registry_returns_the_active_connection() {
        let (_a, _b) = tokio::io::duplex(1024);
        let (tx, _rx) = mpsc::unbounded_channel();
        let conn = Connection::new(tx);
        let registry = ConnectionRegistry::new();
        registry.set_active(Arc::clone(&conn));
        assert!(registry.active().is_some());
    }

    #[tokio::test]
    async fn connection_registry_hides_a_closed_connection() {
        let (tx, _rx) = mpsc::unbounded_channel();
        let conn = Connection::new(tx);
        conn.closed.store(true, Ordering::Release);
        let registry = ConnectionRegistry::new();
        registry.set_active(conn);
        assert!(registry.active().is_none());
    }

    #[test]
    fn connection_registry_duration_without_executor_grows_while_none_is_active() {
        let registry = ConnectionRegistry::new();
        let first = registry.duration_without_executor();
        assert!(first < Duration::from_millis(500));
        std::thread::sleep(Duration::from_millis(20));
        let second = registry.duration_without_executor();
        assert!(second > first);
    }

    #[tokio::test]
    async fn connection_registry_resets_duration_once_an_executor_connects() {
        let registry = ConnectionRegistry::new();
        let _ = registry.duration_without_executor();
        std::thread::sleep(Duration::from_millis(5));
        assert!(registry.duration_without_executor() > Duration::ZERO);
        let (tx, _rx) = mpsc::unbounded_channel();
        registry.set_active(Connection::new(tx));
        assert_eq!(registry.duration_without_executor(), Duration::ZERO);
    }

    // regression: svc reported zero executors while holding a live session
    // after reconnect (alpha 2026-10-03) -- the following four tests
    // exercise the `ConnectionRegistry` map-of-sessions fix directly: a
    // superseding/closed session must never clear a different, still-live
    // session's registration.

    /// (a) Session A connects, B connects, A closes: B must still be
    /// registered, `live_count` must read 1, and `active` (dispatch's
    /// session pick) must be B -- never `None`.
    #[tokio::test]
    async fn registry_keeps_a_live_session_when_a_different_one_is_removed() {
        let registry = ConnectionRegistry::new();
        let (tx_a, _rx_a) = mpsc::unbounded_channel();
        let conn_a = Connection::new(tx_a);
        let id_a = registry.register(Arc::clone(&conn_a));

        let (tx_b, _rx_b) = mpsc::unbounded_channel();
        let conn_b = Connection::new(tx_b);
        registry.register(Arc::clone(&conn_b));

        // A's own teardown removes only its own id.
        conn_a.mark_closed();
        registry.remove(id_a);

        assert_eq!(registry.live_count(), 1);
        let active = registry.active().expect("B is still live");
        assert!(Arc::ptr_eq(&active, &conn_b), "dispatch must use B");
    }

    /// (b) A connects, then drops abruptly (no FIN -- it simply never
    /// answers `ping`): the heartbeat supervisor must declare it dead
    /// (`mark_closed`) within [`HEARTBEAT_MISSED_LIMIT`] misses, pruning it
    /// to `live_count() == 0`, and the zero-executor watchdog must then
    /// fire `ERROR`.
    #[tokio::test]
    async fn heartbeat_drops_a_silent_session_and_watchdog_errors() {
        // Dropping the receiver end closes the channel, so every `ping`'s
        // `send` fails immediately -- simulating a half-open peer that
        // never replies, without waiting out real per-ping timeouts.
        let (tx, rx) = mpsc::unbounded_channel();
        drop(rx);
        let conn = Connection::new(tx);
        let registry = Arc::new(ConnectionRegistry::new());
        let id = registry.register(Arc::clone(&conn));
        assert_eq!(registry.live_count(), 1);

        let metrics = crate::telemetry::register_host_api_metrics(&prometheus::Registry::new());
        run_heartbeat(
            Arc::clone(&conn),
            "127.0.0.1:1".parse().unwrap(),
            Duration::from_millis(5),
            metrics,
        )
        .await;
        assert!(conn.is_closed(), "heartbeat must mark the session closed");

        // The owning accept task's teardown removes its own id once the
        // heartbeat future wins the `tokio::select!` race.
        registry.remove(id);
        assert_eq!(registry.live_count(), 0);

        let count = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let _guard = tracing::subscriber::set_default(ErrorCountingSubscriber(Arc::clone(&count)));
        let watchdog = tokio::spawn(run_zero_executor_watchdog(
            Arc::clone(&registry),
            Duration::from_millis(5),
            Duration::from_millis(5),
        ));
        tokio::time::sleep(Duration::from_millis(60)).await;
        watchdog.abort();
        assert!(count.load(Ordering::SeqCst) >= 1);
    }

    /// (c) Two concurrent live sessions: selection is stable -- the newest
    /// (highest-id) session is picked, and repeated calls to `active()`
    /// keep returning the same one while both remain live.
    #[tokio::test]
    async fn registry_selection_is_stable_across_two_concurrent_sessions() {
        let registry = ConnectionRegistry::new();
        let (tx_a, _rx_a) = mpsc::unbounded_channel();
        let conn_a = Connection::new(tx_a);
        registry.register(Arc::clone(&conn_a));

        let (tx_b, _rx_b) = mpsc::unbounded_channel();
        let conn_b = Connection::new(tx_b);
        registry.register(Arc::clone(&conn_b));

        assert_eq!(registry.live_count(), 2);
        let first = registry.active().expect("a live session exists");
        let second = registry.active().expect("a live session exists");
        assert!(Arc::ptr_eq(&first, &conn_b), "newest session (B) must win");
        assert!(
            Arc::ptr_eq(&second, &conn_b),
            "selection must be stable across repeated calls while both remain live"
        );
    }

    /// (d) The exact alpha 2026-10-03 sequence -- connect, close, reconnect
    /// -- must leave `live_count() == 1`, with the reconnected session as
    /// `active`. Before the fix, the single-slot registry could be left
    /// pointing at the closed first connection with nothing to replace it.
    #[tokio::test]
    async fn registry_connect_close_reconnect_leaves_count_one() {
        let registry = ConnectionRegistry::new();

        let (tx1, _rx1) = mpsc::unbounded_channel();
        let conn1 = Connection::new(tx1);
        let id1 = registry.register(Arc::clone(&conn1));
        conn1.mark_closed();
        registry.remove(id1);
        assert_eq!(registry.live_count(), 0);

        let (tx2, _rx2) = mpsc::unbounded_channel();
        let conn2 = Connection::new(tx2);
        registry.register(Arc::clone(&conn2));

        assert_eq!(registry.live_count(), 1);
        let active = registry.active().expect("the reconnected session is live");
        assert!(Arc::ptr_eq(&active, &conn2));
    }

    /// Minimal `tracing::Subscriber` that counts `ERROR`-level events --
    /// just enough to assert [`run_zero_executor_watchdog`] actually logs,
    /// without pulling in a test-only tracing-capture crate.
    struct ErrorCountingSubscriber(Arc<std::sync::atomic::AtomicUsize>);

    impl tracing::Subscriber for ErrorCountingSubscriber {
        fn enabled(&self, _metadata: &tracing::Metadata<'_>) -> bool {
            true
        }
        fn new_span(&self, _span: &tracing::span::Attributes<'_>) -> tracing::span::Id {
            tracing::span::Id::from_u64(1)
        }
        fn record(&self, _span: &tracing::span::Id, _values: &tracing::span::Record<'_>) {}
        fn record_follows_from(&self, _span: &tracing::span::Id, _follows: &tracing::span::Id) {}
        fn event(&self, event: &tracing::Event<'_>) {
            if *event.metadata().level() == tracing::Level::ERROR {
                self.0.fetch_add(1, Ordering::SeqCst);
            }
        }
        fn enter(&self, _span: &tracing::span::Id) {}
        fn exit(&self, _span: &tracing::span::Id) {}
    }

    // regression: readiness gated on executor connection deadlocked
    // rollouts (alpha 2026-10-02) -- the periodic watchdog is the loud
    // signal operators get for an idle stage sitting with zero executors
    // past the grace period (item 3 of the fix: readiness/liveness no
    // longer gate on executor presence, so this is the replacement
    // visibility mechanism).
    #[tokio::test]
    async fn zero_executor_watchdog_logs_error_once_past_the_grace_period() {
        let count = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let _guard = tracing::subscriber::set_default(ErrorCountingSubscriber(Arc::clone(&count)));

        let registry = Arc::new(ConnectionRegistry::new());
        assert!(registry.active().is_none()); // establishes `zero_since`

        let watchdog = tokio::spawn(run_zero_executor_watchdog(
            Arc::clone(&registry),
            Duration::from_millis(15),
            Duration::from_millis(10),
        ));
        tokio::time::sleep(Duration::from_millis(100)).await;
        watchdog.abort();

        assert!(count.load(Ordering::SeqCst) >= 1);
    }

    /// The watchdog must stay silent while an executor is connected -- it
    /// must never fire on a transient/healthy state.
    #[tokio::test]
    async fn zero_executor_watchdog_stays_silent_while_executor_connected() {
        let count = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let _guard = tracing::subscriber::set_default(ErrorCountingSubscriber(Arc::clone(&count)));

        let registry = Arc::new(ConnectionRegistry::new());
        registry.set_active(test_connection());

        let watchdog = tokio::spawn(run_zero_executor_watchdog(
            Arc::clone(&registry),
            Duration::from_millis(5),
            Duration::from_millis(10),
        ));
        tokio::time::sleep(Duration::from_millis(60)).await;
        watchdog.abort();

        assert_eq!(count.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn connection_registry_dead_letter_metric_is_a_noop_until_wired() {
        let registry = ConnectionRegistry::new();
        registry.record_dead_letter_no_executor();
    }

    #[test]
    fn connection_registry_dead_letter_metric_increments_once_wired() {
        let registry = ConnectionRegistry::new();
        let metrics = crate::telemetry::register_host_api_metrics(&prometheus::Registry::new());
        registry.set_dead_letter_metric(metrics.dead_lettered_no_executor_total.clone());
        registry.record_dead_letter_no_executor();
        registry.record_dead_letter_no_executor();
        assert_eq!(metrics.dead_lettered_no_executor_total.get(), 2);
    }

    /// `Message::Ping` sent by the executor must be answered with `Pong`,
    /// not treated as the fatal "unexpected frame" every other unhandled
    /// kind is.
    #[tokio::test]
    async fn stage_replies_pong_to_an_executor_initiated_ping() {
        let (stage_io, mut executor_io) = tokio::io::duplex(4096);
        let executor = tokio::spawn(async move {
            write_frame(
                &mut executor_io,
                &Frame::new(1, Message::Hello(hello_body("runc"))),
            )
            .await
            .unwrap();
            read_frame(&mut executor_io).await.unwrap(); // hello-ok
            write_frame(&mut executor_io, &Frame::new(42, Message::Ping))
                .await
                .unwrap();
            let pong = read_frame(&mut executor_io).await.unwrap();
            assert!(matches!(pong.message, Message::Pong));
            assert_eq!(pong.id, 42);
        });
        let (_connection, read_loop) = run_connection(
            stage_io,
            HelloOkBody {
                stage: "svc-action".to_string(),
                protocol_version: 1,
                limits: test_limits(),
            },
            false,
            Arc::new(DenyAllCapabilities),
        )
        .await
        .expect("handshake succeeds");
        let read_loop_handle = tokio::spawn(read_loop);
        executor.await.expect("executor task");
        let _ = tokio::time::timeout(std::time::Duration::from_secs(2), read_loop_handle).await;
    }

    /// `Connection::ping` succeeds end to end when the peer actually
    /// answers `pong`.
    #[tokio::test]
    async fn connection_ping_succeeds_when_peer_replies_pong() {
        let (stage_io, mut executor_io) = tokio::io::duplex(4096);
        let executor = tokio::spawn(async move {
            write_frame(
                &mut executor_io,
                &Frame::new(1, Message::Hello(hello_body("runc"))),
            )
            .await
            .unwrap();
            read_frame(&mut executor_io).await.unwrap(); // hello-ok
            let ping = read_frame(&mut executor_io).await.unwrap();
            assert!(matches!(ping.message, Message::Ping));
            write_frame(&mut executor_io, &Frame::new(ping.id, Message::Pong))
                .await
                .unwrap();
        });
        let (connection, read_loop) = run_connection(
            stage_io,
            HelloOkBody {
                stage: "svc-action".to_string(),
                protocol_version: 1,
                limits: test_limits(),
            },
            false,
            Arc::new(DenyAllCapabilities),
        )
        .await
        .expect("handshake succeeds");
        tokio::spawn(read_loop);
        connection
            .ping(Duration::from_secs(2))
            .await
            .expect("ping succeeds when the peer replies pong");
        executor.await.expect("executor task");
    }

    /// **regression: executor stuck on terminated svc pod after rollout
    /// (alpha 2026-10-02).** A session whose peer goes silent after the
    /// handshake (the same shape a half-open socket takes: frames queue,
    /// nobody ever replies) must be dropped by [`run_heartbeat`] after
    /// [`HEARTBEAT_MISSED_LIMIT`] consecutive missed heartbeats, marking
    /// the connection closed and incrementing `host_api_heartbeat_
    /// timeouts_total` -- loudly, not silently left "connected" forever.
    #[tokio::test]
    async fn run_heartbeat_drops_a_silent_session_after_missed_limit() {
        let (stage_io, mut executor_io) = tokio::io::duplex(4096);
        // Drives the handshake CONCURRENTLY with `run_connection` below
        // (not inline, sequentially) -- awaiting the executor's own
        // `hello`/`hello-ok` exchange before `run_connection` has even
        // been called would deadlock: nothing would be reading `stage_io`
        // yet to produce the `hello-ok` this task waits on.
        let executor = tokio::spawn(async move {
            write_frame(
                &mut executor_io,
                &Frame::new(1, Message::Hello(hello_body("runc"))),
            )
            .await
            .unwrap();
            let hello_ok = read_frame(&mut executor_io).await.unwrap();
            assert!(matches!(hello_ok.message, Message::HelloOk(_)));
            // Goes silent from here on -- it never answers any `ping` the
            // heartbeat supervisor sends, exactly like a half-open socket
            // pointed at a terminated pod. Keeps `executor_io` alive
            // (never dropped) for the rest of the test so the pipe stays
            // open rather than EOF-ing the read loop.
            std::future::pending::<()>().await;
        });

        let (connection, read_loop) = run_connection(
            stage_io,
            HelloOkBody {
                stage: "svc-action".to_string(),
                protocol_version: 1,
                limits: test_limits(),
            },
            false,
            Arc::new(DenyAllCapabilities),
        )
        .await
        .expect("handshake succeeds");
        tokio::spawn(read_loop);

        let metrics = crate::telemetry::register_host_api_metrics(&prometheus::Registry::new());
        let peer: SocketAddr = "127.0.0.1:0".parse().unwrap();
        run_heartbeat(
            Arc::clone(&connection),
            peer,
            Duration::from_millis(50),
            metrics.clone(),
        )
        .await;

        assert!(connection.is_closed());
        assert_eq!(metrics.heartbeat_timeouts_total.get(), 1);
        executor.abort();
    }

    fn write_temp_pem(contents: &str, label: &str) -> std::path::PathBuf {
        use std::io::Write;
        let path = std::env::temp_dir().join(format!(
            "svc-action-test-{}-{}-{label}.pem",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0)
        ));
        let mut f = std::fs::File::create(&path).expect("create temp pem file");
        f.write_all(contents.as_bytes())
            .expect("write temp pem file");
        path
    }

    fn base_cli() -> crate::config::CliConfig {
        use clap::Parser;
        crate::config::CliConfig::parse_from(["svc-action"])
    }

    #[test]
    fn build_server_config_rejects_missing_cert_and_key() {
        assert!(matches!(
            build_server_config(&base_cli()),
            Err(HostApiError::Config(_))
        ));
    }

    #[test]
    fn build_server_config_rejects_missing_client_ca() {
        let ca_key = rcgen::KeyPair::generate().unwrap();
        let ca_params =
            rcgen::CertificateParams::new(vec!["svc-action-test-ca".to_string()]).unwrap();
        let ca_cert = ca_params.self_signed(&ca_key).unwrap();
        let issuer = rcgen::Issuer::from_params(&ca_params, ca_key);

        let server_key = rcgen::KeyPair::generate().unwrap();
        let server_params = rcgen::CertificateParams::new(vec!["svc-action".to_string()]).unwrap();
        let server_cert = server_params.signed_by(&server_key, &issuer).unwrap();

        let _ca_path = write_temp_pem(&ca_cert.pem(), "ca");
        let cert_path = write_temp_pem(&server_cert.pem(), "servercert");
        let key_path = write_temp_pem(&server_key.serialize_pem(), "serverkey");

        let mut cli = base_cli();
        cli.host_api_server_cert_file = Some(cert_path.clone());
        cli.host_api_server_key_file = Some(key_path.clone());
        assert!(matches!(
            build_server_config(&cli),
            Err(HostApiError::Config(_))
        ));
        let _ = std::fs::remove_file(&cert_path);
        let _ = std::fs::remove_file(&key_path);
    }

    #[test]
    fn build_server_config_accepts_a_real_cert_key_and_ca() {
        let ca_key = rcgen::KeyPair::generate().unwrap();
        let ca_params =
            rcgen::CertificateParams::new(vec!["svc-action-test-ca".to_string()]).unwrap();
        let ca_cert = ca_params.self_signed(&ca_key).unwrap();
        let issuer = rcgen::Issuer::from_params(&ca_params, ca_key);

        let server_key = rcgen::KeyPair::generate().unwrap();
        let server_params = rcgen::CertificateParams::new(vec!["svc-action".to_string()]).unwrap();
        let server_cert = server_params.signed_by(&server_key, &issuer).unwrap();

        let ca_path = write_temp_pem(&ca_cert.pem(), "ca");
        let cert_path = write_temp_pem(&server_cert.pem(), "servercert");
        let key_path = write_temp_pem(&server_key.serialize_pem(), "serverkey");

        let mut cli = base_cli();
        cli.host_api_server_cert_file = Some(cert_path.clone());
        cli.host_api_server_key_file = Some(key_path.clone());
        cli.host_api_client_ca_file = Some(ca_path.clone());
        build_server_config(&cli).expect("valid cert/key/ca must build");

        let _ = std::fs::remove_file(&ca_path);
        let _ = std::fs::remove_file(&cert_path);
        let _ = std::fs::remove_file(&key_path);
    }

    /// End-to-end: a real rustls mTLS handshake (server config built by
    /// `build_server_config`, client presenting a cert signed by the same
    /// CA) followed by a real `hello`/`hello-ok` exchange over the
    /// resulting `TlsStream` -- proves `accept_and_handshake`'s full path,
    /// not just the config-building half.
    #[tokio::test]
    async fn accept_and_handshake_completes_a_real_mtls_connection() {
        let ca_key = rcgen::KeyPair::generate().unwrap();
        let ca_params =
            rcgen::CertificateParams::new(vec!["svc-action-test-ca".to_string()]).unwrap();
        let ca_cert = ca_params.self_signed(&ca_key).unwrap();
        let issuer = rcgen::Issuer::from_params(&ca_params, ca_key);

        let server_key = rcgen::KeyPair::generate().unwrap();
        let server_params = rcgen::CertificateParams::new(vec!["127.0.0.1".to_string()]).unwrap();
        let server_cert = server_params.signed_by(&server_key, &issuer).unwrap();

        let client_key = rcgen::KeyPair::generate().unwrap();
        let client_params =
            rcgen::CertificateParams::new(vec!["bundle-executor-test-client".to_string()]).unwrap();
        let client_cert = client_params.signed_by(&client_key, &issuer).unwrap();

        let ca_path = write_temp_pem(&ca_cert.pem(), "ca");
        let server_cert_path = write_temp_pem(&server_cert.pem(), "servercert");
        let server_key_path = write_temp_pem(&server_key.serialize_pem(), "serverkey");

        let mut cli = base_cli();
        cli.host_api_server_cert_file = Some(server_cert_path.clone());
        cli.host_api_server_key_file = Some(server_key_path.clone());
        cli.host_api_client_ca_file = Some(ca_path.clone());
        let server_config = build_server_config(&cli).expect("server config builds");
        let acceptor = TlsAcceptor::from(Arc::new(server_config));

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();

        let server_task = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.unwrap();
            let (connection, read_loop) = accept_and_handshake(
                tcp,
                &acceptor,
                "svc-action",
                test_limits(),
                false,
                Arc::new(DenyAllCapabilities),
            )
            .await
            .expect("handshake succeeds");
            tokio::spawn(read_loop);
            assert!(!connection.is_closed());
        });

        // Client side: dial with a cert signed by the same CA.
        let mut client_roots = rustls::RootCertStore::empty();
        client_roots
            .add(
                CertificateDer::pem_file_iter(&ca_path)
                    .unwrap()
                    .next()
                    .unwrap()
                    .unwrap(),
            )
            .unwrap();
        let client_certs: Vec<_> =
            CertificateDer::pem_file_iter(write_temp_pem(&client_cert.pem(), "clientcert"))
                .unwrap()
                .collect::<Result<_, _>>()
                .unwrap();
        let client_key_der =
            PrivateKeyDer::from_pem_file(write_temp_pem(&client_key.serialize_pem(), "clientkey"))
                .unwrap();
        let client_config = rustls::ClientConfig::builder()
            .with_root_certificates(client_roots)
            .with_client_auth_cert(client_certs, client_key_der)
            .unwrap();
        let connector = tokio_rustls::TlsConnector::from(Arc::new(client_config));
        let tcp = tokio::net::TcpStream::connect(addr).await.unwrap();
        let server_name = rustls_pki_types::ServerName::try_from("127.0.0.1").unwrap();
        let mut tls = connector.connect(server_name, tcp).await.unwrap();

        write_frame(&mut tls, &Frame::new(1, Message::Hello(hello_body("runc"))))
            .await
            .unwrap();
        let reply = read_frame(&mut tls).await.unwrap();
        assert!(matches!(reply.message, Message::HelloOk(_)));

        server_task.await.unwrap();
        let _ = std::fs::remove_file(&ca_path);
        let _ = std::fs::remove_file(&server_cert_path);
        let _ = std::fs::remove_file(&server_key_path);
    }
}
