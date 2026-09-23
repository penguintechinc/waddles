//! The host-API mTLS server (spec §6.6/§7.1): the stage side of the
//! bundle-executor wire protocol. **The executor dials the stage** -- this
//! module accepts that connection, completes the `hello`/`hello-ok`
//! handshake (validating protocol version and sandbox posture, spec §12.2/
//! §14.6 tests 12a-12c), then lets `crate::execute` drive `load`/`invoke`
//! requests over it while a background read loop answers `host-call`
//! frames through a per-invoke [`CapabilityHandler`] scope and delivers
//! every other reply through a [`CorrelationTable`].
//!
//! Mirrors `core/svc_action/src/host_api.rs` and `core/bundle_executor/
//! src/wire.rs` (the executor/client side) with the roles reversed: there,
//! the executor sends `hello` and answers `load`/`invoke`; here, the stage
//! answers `hello` and sends `load`/`invoke`. `core/bundle_executor/src/
//! tls.rs` is the client-TLS mirror of [`build_server_config`] below.
//!
//! **Per-invoke capability scoping -- the corrected design (task
//! instruction, from the security review of `core/svc_action`'s landed
//! M3):** `svc_action::host_api::Connection` binds one
//! `Arc<dyn CapabilityHandler>` for the connection's *entire lifetime*,
//! fixed at `run_connection`/`accept_and_handshake` construction time --
//! correct only while a connection ever serves a single, unchanging
//! `(tenant, community, app_id)`, and a known latent bug the moment it
//! serves more than one (which every non-trivial deployment eventually
//! does, once the distribution poll assigns real per-tenant activations).
//! This module instead derives the capability scope **per invoke**, keyed
//! by the `invoke` frame's own id -- the same id `HostCallBody.call_id`
//! echoes back (spec §6.6: "The `invoke` frame id this call happened
//! during"):
//!
//! - [`Connection::invoke`] (not the generic [`Connection::request`])
//!   registers the caller-supplied, already-envelope-scoped
//!   `Arc<dyn CapabilityHandler>` into [`Connection`]'s own
//!   `invoke_scopes` map under the id it allocates for the `invoke`
//!   frame, sends the frame, awaits the reply, and removes the entry
//!   again on every exit path (success, executor-reported error, or a
//!   dropped/timed-out request) -- never left behind for a later,
//!   differently-scoped invoke to accidentally answer under.
//! - The read loop's `host-call` handler looks the incoming
//!   `call.call_id` up in that map and answers with whatever handler is
//!   registered there; a `call_id` with no live entry (already completed,
//!   never existed, or a malformed/replayed id) falls back to
//!   [`Connection`]'s connection-level `fallback_capabilities` --
//!   `crate::capabilities::DenyAllCapabilities` in production, never a
//!   fixed "real" identity. This is fail-closed by construction: an
//!   unscoped host-call is always denied, never silently answered under
//!   the wrong tenant/community/app_id.

use std::collections::HashMap;
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use penguin_bundle_host::wire::{
    read_frame, write_frame, CorrelationError, CorrelationTable, ErrorBody, ErrorCode, Frame,
    HelloOkBody, IdAllocator, InvokeBody, Message, ShutdownBody,
};
use rustls::server::WebPkiClientVerifier;
use rustls::{RootCertStore, ServerConfig};
use rustls_pki_types::pem::PemObject;
use rustls_pki_types::{CertificateDer, PrivateKeyDer};
use thiserror::Error;
use tokio::io::{AsyncRead, AsyncWrite, AsyncWriteExt};
use tokio::sync::{mpsc, oneshot};
use tokio_rustls::TlsAcceptor;
use tracing::{debug, warn};

use crate::capabilities::CapabilityHandler;
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
/// frames are written through, and the per-invoke capability scope map
/// (see the module doc).
pub struct Connection {
    ids: IdAllocator,
    pending: CorrelationTable,
    writer_tx: mpsc::UnboundedSender<Frame>,
    closed: Arc<std::sync::atomic::AtomicBool>,
    /// Keyed by the `invoke` frame's own id -- populated by [`Self::invoke`]
    /// for the duration of exactly that call, removed on every exit path.
    invoke_scopes: Mutex<HashMap<u64, Arc<dyn CapabilityHandler>>>,
    /// Answers a `host-call` whose `call_id` has no live entry in
    /// `invoke_scopes` -- `DenyAllCapabilities` in production.
    fallback_capabilities: Arc<dyn CapabilityHandler>,
}

impl Connection {
    fn new(
        writer_tx: mpsc::UnboundedSender<Frame>,
        fallback_capabilities: Arc<dyn CapabilityHandler>,
    ) -> Arc<Self> {
        Arc::new(Self {
            ids: IdAllocator::new(),
            pending: CorrelationTable::new(),
            writer_tx,
            closed: Arc::new(std::sync::atomic::AtomicBool::new(false)),
            invoke_scopes: Mutex::new(HashMap::new()),
            fallback_capabilities,
        })
    }

    /// Sends `message` under a freshly allocated id and awaits the peer's
    /// reply frame. Used for every stage-initiated exchange that carries
    /// no per-invoke capability scope: `load` -> `loaded`/`error`,
    /// `unload` -> `unloaded`/`error`, `ping` -> `pong`. **Not** used for
    /// `invoke` -- see [`Self::invoke`].
    pub async fn request(&self, message: Message) -> Result<Frame, HostApiError> {
        if self.closed.load(Ordering::Acquire) {
            return Err(HostApiError::ConnectionUnavailable);
        }
        let id = self.ids.next_id();
        let rx = self.pending.register(id)?;
        self.send(Frame::new(id, message))?;
        rx.await.map_err(|_| HostApiError::ConnectionUnavailable)
    }

    /// Sends an `invoke` frame scoped to `capabilities` for exactly this
    /// call's lifetime (see the module doc's "Per-invoke capability
    /// scoping" section) -- the sole way `crate::execute` should ever
    /// invoke a bundle export. `capabilities` is registered under the
    /// freshly allocated invoke id *before* the frame is sent (so a
    /// host-call racing in immediately after the executor receives it can
    /// never see an unregistered scope) and removed again once this
    /// method returns, on every exit path, via the `scopes` cleanup below.
    pub async fn invoke(
        &self,
        body: InvokeBody,
        capabilities: Arc<dyn CapabilityHandler>,
    ) -> Result<Frame, HostApiError> {
        if self.closed.load(Ordering::Acquire) {
            return Err(HostApiError::ConnectionUnavailable);
        }
        let id = self.ids.next_id();
        self.invoke_scopes
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(id, capabilities);

        let result = match self.pending.register(id) {
            Ok(rx) => match self.send(Frame::new(id, Message::Invoke(body))) {
                Ok(()) => rx.await.map_err(|_| HostApiError::ConnectionUnavailable),
                Err(e) => Err(e),
            },
            Err(e) => Err(HostApiError::from(e)),
        };

        self.invoke_scopes
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .remove(&id);
        result
    }

    /// Resolves the capability handler that should answer a `host-call`
    /// carrying `call_id` -- the registered per-invoke scope if one is
    /// still live, otherwise [`Self::fallback_capabilities`] (deny-by-
    /// default; see the module doc).
    fn capabilities_for(&self, call_id: u64) -> Arc<dyn CapabilityHandler> {
        self.invoke_scopes
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .get(&call_id)
            .cloned()
            .unwrap_or_else(|| Arc::clone(&self.fallback_capabilities))
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
}

/// Completes the `hello`/`hello-ok` handshake as the stage side, then runs
/// the read loop until the peer disconnects or a fatal protocol error
/// occurs. Returns the live [`Connection`] immediately after the handshake
/// succeeds (the read loop continues in the background task the caller
/// spawns) -- see [`accept_and_handshake`], the usual entry point.
///
/// `pub(crate)` (not `pub`): `accept_and_handshake` is the only production
/// caller (it adds the TLS layer this function doesn't need to know
/// about); `crate::execute`'s own tests call this directly over a plain
/// in-memory duplex to build a real, handshaken [`Connection`] without a
/// TLS handshake or a live executor process.
pub(crate) async fn run_connection<S>(
    io: S,
    hello_ok: HelloOkBody,
    expected_sandbox_gvisor: bool,
    fallback_capabilities: Arc<dyn CapabilityHandler>,
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
    let connection = Connection::new(writer_tx, fallback_capabilities);

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
            let result = read_loop(&mut reader, &connection).await;
            connection.closed.store(true, Ordering::Release);
            writer_task.abort();
            result
        }
    };

    Ok((connection, read_loop))
}

async fn read_loop<R>(reader: &mut R, connection: &Arc<Connection>) -> Result<(), HostApiError>
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
            // The executor issues host-call frames; answer via whichever
            // capability scope is live for `body.call_id` (see the module
            // doc's "Per-invoke capability scoping" section) and reply on
            // the same connection, reusing its correlation id (spec
            // §6.6). Spawned onto its own task so a slow capability call
            // never blocks delivery of other in-flight replies.
            Message::HostCall(body) => {
                let connection = Arc::clone(connection);
                let capabilities = connection.capabilities_for(body.call_id);
                tokio::spawn(async move {
                    let result = capabilities.handle(body).await;
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

/// Accepts one TCP connection, completes the TLS handshake, then the
/// `hello`/`hello-ok` host-API handshake -- the production entry point
/// [`crate::lib`]'s connection-accept loop calls per incoming connection.
pub async fn accept_and_handshake(
    tcp: tokio::net::TcpStream,
    acceptor: &TlsAcceptor,
    stage_name: &str,
    limits: penguin_bundle_host::wire::HelloLimits,
    expected_sandbox_gvisor: bool,
    fallback_capabilities: Arc<dyn CapabilityHandler>,
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
        fallback_capabilities,
    )
    .await
}

/// A one-shot channel a caller uses to stop `serve` gracefully.
pub type ShutdownReceiver = oneshot::Receiver<()>;

/// Registry of currently-live executor connections this stage can dispatch
/// `invoke`/`load` requests over. `EXECUTOR_STAGE_CONNECTIONS` (spec §6.6)
/// describes spreading invocations across many connections from one
/// executor replica; this M4 landing keeps the single most-recently
/// connected one active (correct for the common one-executor-replica
/// deployment; TODO(M4+) is round-robin across all held connections) --
/// identical shape to `core/svc_action::host_api::ConnectionRegistry`.
#[derive(Default)]
pub struct ConnectionRegistry {
    active: std::sync::Mutex<Option<Arc<Connection>>>,
    generation: AtomicU64,
}

impl ConnectionRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    /// Registers a newly-handshaken connection as the active one.
    pub fn set_active(&self, conn: Arc<Connection>) {
        *self.active.lock().unwrap_or_else(|e| e.into_inner()) = Some(conn);
        self.generation.fetch_add(1, Ordering::Relaxed);
    }

    /// Returns the current active connection, if it is still open. A
    /// closed connection is treated as absent so callers fail over to
    /// "no executor available" (`error.kind = "executor_unavailable"`,
    /// spec §6.3) rather than sending into a dead socket.
    pub fn active(&self) -> Option<Arc<Connection>> {
        let guard = self.active.lock().unwrap_or_else(|e| e.into_inner());
        guard.as_ref().filter(|c| !c.is_closed()).map(Arc::clone)
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
    fallback_capabilities: Arc<dyn CapabilityHandler>,
    mut shutdown: tokio::sync::oneshot::Receiver<()>,
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
                let acceptor = acceptor.clone();
                let registry = Arc::clone(&registry);
                let fallback_capabilities = Arc::clone(&fallback_capabilities);
                let stage_name = "svc-process".to_string();
                let expected_gvisor = cli.sandbox_gvisor;
                let limits = limits.clone();
                tokio::spawn(async move {
                    match accept_and_handshake(tcp, &acceptor, &stage_name, limits, expected_gvisor, fallback_capabilities).await {
                        Ok((connection, read_loop)) => {
                            debug!(%peer, "host-api connection established");
                            registry.set_active(connection);
                            if let Err(e) = read_loop.await {
                                warn!(%peer, error = %e, "host-api connection closed");
                            }
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
        HelloBody, HelloLimits, LoadBody, LoadLimits, LoadedBody, SandboxInfo,
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
                stage: "svc-process".to_string(),
                protocol_version: 1,
                limits: test_limits(),
            },
            false,
            Arc::new(DenyAllCapabilities),
        )
        .await
        .expect("handshake succeeds");
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
                stage: "svc-process".to_string(),
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
                stage: "svc-process".to_string(),
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
                stage: "svc-process".to_string(),
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
                stage: "svc-process".to_string(),
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

    /// Full round trip proving the per-invoke scoping design: after
    /// handshake, the stage sends `load`, the fake executor replies
    /// `loaded`; the stage then `invoke`s scoped to a recording
    /// capability handler, the executor issues a `host-call`, and the
    /// call is answered by that SPECIFIC scope -- not the connection-level
    /// fallback.
    #[tokio::test]
    async fn invoke_scopes_host_calls_to_the_capabilities_handed_to_that_invoke() {
        use crate::capabilities::CapabilityHandler;
        use penguin_bundle_host::wire::HostCallBody;
        use std::future::Future;
        use std::pin::Pin;

        struct RecordingCapabilities {
            tenant: &'static str,
        }
        impl CapabilityHandler for RecordingCapabilities {
            fn handle<'a>(
                &'a self,
                _call: HostCallBody,
            ) -> Pin<
                Box<
                    dyn Future<
                            Output = Result<
                                serde_json::Value,
                                penguin_bundle_host::wire::HostResultError,
                            >,
                        > + Send
                        + 'a,
                >,
            > {
                let tenant = self.tenant;
                Box::pin(async move { Ok(serde_json::json!({"tenant": tenant})) })
            }
        }

        let (stage_io, executor_io) = tokio::io::duplex(64 * 1024);

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
                        exports: vec!["transform".to_string()],
                    }),
                ),
            )
            .await
            .unwrap();

            let invoke = read_frame(&mut io).await.unwrap();
            let invoke_id = invoke.id;
            assert!(matches!(invoke.message, Message::Invoke(_)));

            write_frame(
                &mut io,
                &Frame::new(
                    2,
                    Message::HostCall(HostCallBody {
                        app_id: "waddles.bot.commands.default".to_string(),
                        capability: penguin_bundle_host::wire::CapabilityKind::Context,
                        op: "get".to_string(),
                        args: serde_json::json!({}),
                        call_id: invoke_id,
                    }),
                ),
            )
            .await
            .unwrap();
            let host_result = read_frame(&mut io).await.unwrap();
            let Message::HostResult(body) = host_result.message else {
                panic!("expected host-result");
            };
            assert_eq!(body.result.unwrap()["tenant"], "acme-scope");

            write_frame(
                &mut io,
                &Frame::new(
                    invoke_id,
                    Message::Result(penguin_bundle_host::wire::ResultBody {
                        payload: serde_json::json!(null),
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
                stage: "svc-process".to_string(),
                protocol_version: 1,
                limits: test_limits(),
            },
            false,
            Arc::new(DenyAllCapabilities),
        )
        .await
        .expect("handshake succeeds");
        let read_loop_handle = tokio::spawn(read_loop);

        connection
            .request(Message::Load(LoadBody {
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
                    export: penguin_bundle_host::wire::ExportKind::Transform,
                    payload: serde_json::json!({}),
                    deadline_ms: 2000,
                    trace: None,
                },
                Arc::new(RecordingCapabilities {
                    tenant: "acme-scope",
                }),
            )
            .await
            .expect("invoke succeeds");
        assert!(matches!(result.message, Message::Result(_)));

        executor.await.expect("executor task");
        let _ = tokio::time::timeout(std::time::Duration::from_secs(2), read_loop_handle).await;
    }

    /// A `host-call` arriving with a `call_id` that names no live invoke
    /// (already completed, or never existed) is denied by the fallback
    /// handler -- never silently answered under a stale or default scope.
    /// This is the regression test for the fixed design: `svc_action`'s
    /// M3 landing has no equivalent test because its capability handler
    /// was fixed for the whole connection and could not distinguish this
    /// case at all.
    #[tokio::test]
    async fn host_call_with_unknown_call_id_falls_back_to_deny_all() {
        use penguin_bundle_host::wire::HostCallBody;

        let (stage_io, executor_io) = tokio::io::duplex(64 * 1024);
        let executor = tokio::spawn(async move {
            let mut io = executor_io;
            write_frame(&mut io, &Frame::new(1, Message::Hello(hello_body("runc"))))
                .await
                .unwrap();
            read_frame(&mut io).await.unwrap();

            // No `invoke` was ever sent -- this call_id has no registered
            // scope.
            write_frame(
                &mut io,
                &Frame::new(
                    2,
                    Message::HostCall(HostCallBody {
                        app_id: "waddles.bot.commands.default".to_string(),
                        capability: penguin_bundle_host::wire::CapabilityKind::Context,
                        op: "get".to_string(),
                        args: serde_json::json!({}),
                        call_id: 999,
                    }),
                ),
            )
            .await
            .unwrap();
            let host_result = read_frame(&mut io).await.unwrap();
            let Message::HostResult(body) = host_result.message else {
                panic!("expected host-result");
            };
            assert!(body.error.is_some());
            assert_eq!(body.error.unwrap().code, "not_implemented");
        });

        let (_connection, read_loop) = run_connection(
            stage_io,
            HelloOkBody {
                stage: "svc-process".to_string(),
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

    #[tokio::test]
    async fn request_on_a_closed_connection_returns_connection_unavailable() {
        let (tx, _rx) = mpsc::unbounded_channel();
        let conn = Connection::new(tx, Arc::new(DenyAllCapabilities));
        conn.closed.store(true, Ordering::Release);
        let result = conn.request(Message::Ping).await;
        assert!(matches!(result, Err(HostApiError::ConnectionUnavailable)));
    }

    #[tokio::test]
    async fn invoke_on_a_closed_connection_returns_connection_unavailable() {
        let (tx, _rx) = mpsc::unbounded_channel();
        let conn = Connection::new(tx, Arc::new(DenyAllCapabilities));
        conn.closed.store(true, Ordering::Release);
        let result = conn
            .invoke(
                InvokeBody {
                    app_id: "waddles.bot.commands.default".to_string(),
                    digest: "sha256:00".to_string(),
                    export: penguin_bundle_host::wire::ExportKind::Transform,
                    payload: serde_json::json!({}),
                    deadline_ms: 2000,
                    trace: None,
                },
                Arc::new(DenyAllCapabilities),
            )
            .await;
        assert!(matches!(result, Err(HostApiError::ConnectionUnavailable)));
        // The scope must never be left registered when `invoke` bails out
        // before ever sending a frame.
        assert!(conn.invoke_scopes.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn shutdown_sends_a_shutdown_frame() {
        let (tx, mut rx) = mpsc::unbounded_channel();
        let conn = Connection::new(tx, Arc::new(DenyAllCapabilities));
        conn.shutdown(5_000).expect("shutdown queues a frame");
        let frame = rx.recv().await.expect("a frame was sent");
        assert!(matches!(
            frame.message,
            Message::Shutdown(ShutdownBody { grace_ms: 5_000 })
        ));
    }

    #[test]
    fn connection_registry_starts_empty() {
        let registry = ConnectionRegistry::new();
        assert!(registry.active().is_none());
    }

    #[tokio::test]
    async fn connection_registry_returns_the_active_connection() {
        let (tx, _rx) = mpsc::unbounded_channel();
        let conn = Connection::new(tx, Arc::new(DenyAllCapabilities));
        let registry = ConnectionRegistry::new();
        registry.set_active(Arc::clone(&conn));
        assert!(registry.active().is_some());
    }

    #[tokio::test]
    async fn connection_registry_hides_a_closed_connection() {
        let (tx, _rx) = mpsc::unbounded_channel();
        let conn = Connection::new(tx, Arc::new(DenyAllCapabilities));
        conn.closed.store(true, Ordering::Release);
        let registry = ConnectionRegistry::new();
        registry.set_active(conn);
        assert!(registry.active().is_none());
    }

    fn write_temp_pem(contents: &str, label: &str) -> std::path::PathBuf {
        use std::io::Write;
        let path = std::env::temp_dir().join(format!(
            "svc-process-test-{}-{}-{label}.pem",
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
        crate::config::CliConfig::parse_from(["svc-process"])
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
            rcgen::CertificateParams::new(vec!["svc-process-test-ca".to_string()]).unwrap();
        let ca_cert = ca_params.self_signed(&ca_key).unwrap();
        let issuer = rcgen::Issuer::from_params(&ca_params, ca_key);

        let server_key = rcgen::KeyPair::generate().unwrap();
        let server_params = rcgen::CertificateParams::new(vec!["svc-process".to_string()]).unwrap();
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
            rcgen::CertificateParams::new(vec!["svc-process-test-ca".to_string()]).unwrap();
        let ca_cert = ca_params.self_signed(&ca_key).unwrap();
        let issuer = rcgen::Issuer::from_params(&ca_params, ca_key);

        let server_key = rcgen::KeyPair::generate().unwrap();
        let server_params = rcgen::CertificateParams::new(vec!["svc-process".to_string()]).unwrap();
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
            rcgen::CertificateParams::new(vec!["svc-process-test-ca".to_string()]).unwrap();
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
                "svc-process",
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

    /// End-to-end through the production entry point [`serve`] itself
    /// (not just [`accept_and_handshake`]): binds a real ephemeral port,
    /// dials it with a real mTLS client, completes the `hello`/`hello-ok`
    /// handshake, confirms the connection lands in the registry, then
    /// resolves `shutdown` and confirms `serve` returns `Ok(())`.
    #[tokio::test]
    async fn serve_accepts_a_connection_and_registers_it_then_stops_on_shutdown() {
        let ca_key = rcgen::KeyPair::generate().unwrap();
        let ca_params =
            rcgen::CertificateParams::new(vec!["svc-process-test-ca".to_string()]).unwrap();
        let ca_cert = ca_params.self_signed(&ca_key).unwrap();
        let issuer = rcgen::Issuer::from_params(&ca_params, ca_key);

        let server_key = rcgen::KeyPair::generate().unwrap();
        let server_params = rcgen::CertificateParams::new(vec!["127.0.0.1".to_string()]).unwrap();
        let server_cert = server_params.signed_by(&server_key, &issuer).unwrap();

        let client_key = rcgen::KeyPair::generate().unwrap();
        let client_params =
            rcgen::CertificateParams::new(vec!["bundle-executor-test-client".to_string()]).unwrap();
        let client_cert = client_params.signed_by(&client_key, &issuer).unwrap();

        let ca_path = write_temp_pem(&ca_cert.pem(), "serve-ca");
        let server_cert_path = write_temp_pem(&server_cert.pem(), "serve-servercert");
        let server_key_path = write_temp_pem(&server_key.serialize_pem(), "serve-serverkey");

        // Grab a free ephemeral port, then release it immediately so
        // `serve` itself can bind it (small unavoidable TOCTOU window,
        // same pattern `svc_action`'s own healthcheck tests already use).
        let probe = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = probe.local_addr().unwrap().port();
        drop(probe);

        let mut cli = base_cli();
        cli.bind_addr = "127.0.0.1".parse().unwrap();
        cli.host_api_port = port;
        cli.host_api_server_cert_file = Some(server_cert_path.clone());
        cli.host_api_server_key_file = Some(server_key_path.clone());
        cli.host_api_client_ca_file = Some(ca_path.clone());

        let registry = Arc::new(ConnectionRegistry::new());
        let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel();
        let serve_handle = tokio::spawn(serve(
            cli,
            Arc::clone(&registry),
            Arc::new(DenyAllCapabilities),
            shutdown_rx,
        ));

        // Give the listener a moment to bind before dialing it.
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;

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
            CertificateDer::pem_file_iter(write_temp_pem(&client_cert.pem(), "serve-clientcert"))
                .unwrap()
                .collect::<Result<_, _>>()
                .unwrap();
        let client_key_der = PrivateKeyDer::from_pem_file(write_temp_pem(
            &client_key.serialize_pem(),
            "serve-clientkey",
        ))
        .unwrap();
        let client_config = rustls::ClientConfig::builder()
            .with_root_certificates(client_roots)
            .with_client_auth_cert(client_certs, client_key_der)
            .unwrap();
        let connector = tokio_rustls::TlsConnector::from(Arc::new(client_config));
        let tcp = tokio::net::TcpStream::connect(("127.0.0.1", port))
            .await
            .unwrap();
        let server_name = rustls_pki_types::ServerName::try_from("127.0.0.1").unwrap();
        let mut tls = connector.connect(server_name, tcp).await.unwrap();

        write_frame(&mut tls, &Frame::new(1, Message::Hello(hello_body("runc"))))
            .await
            .unwrap();
        let reply = read_frame(&mut tls).await.unwrap();
        assert!(matches!(reply.message, Message::HelloOk(_)));

        // Give the spawned accept-handler a moment to register the
        // connection before asserting on the registry.
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
        assert!(registry.active().is_some());

        shutdown_tx.send(()).expect("shutdown receiver still live");
        let result = tokio::time::timeout(std::time::Duration::from_secs(5), serve_handle)
            .await
            .expect("serve must return promptly once shutdown resolves")
            .expect("serve task must not panic");
        assert!(result.is_ok());

        let _ = std::fs::remove_file(&ca_path);
        let _ = std::fs::remove_file(&server_cert_path);
        let _ = std::fs::remove_file(&server_key_path);
    }
}
