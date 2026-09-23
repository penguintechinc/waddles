//! The host-API wire protocol connection (spec
//! `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md` SS6.6):
//! the executor dials, sends `hello`, then runs one read loop that both
//! delivers replies to whatever this side is awaiting (`hello-ok`/`error`
//! after `hello`, `host-result` after a `host-call`) and dispatches new
//! requests the stage sends (`load`/`unload`/`invoke`/`ping`/`shutdown`)
//! to a [`RequestHandler`] supplied by the caller.
//!
//! Deliberately generic over `AsyncRead + AsyncWrite` (matching
//! `penguin_bundle_host::wire::{read_frame, write_frame}`'s own framing
//! primitives) so the protocol logic in this file is unit-testable over
//! an in-memory `tokio::io::duplex` without a real TLS socket -- see
//! `tests` below. `crate::tls` supplies the real mTLS transport.

use std::sync::Arc;

use penguin_bundle_host::wire::{
    read_frame, write_frame, CorrelationError, CorrelationTable, ErrorBody, Frame, HelloBody,
    HelloOkBody, IdAllocator, InvokeBody, LoadBody, LoadedBody, Message, ResultBody, ShutdownBody,
    UnloadBody, UnloadedBody,
};
use tokio::io::{AsyncRead, AsyncWrite, AsyncWriteExt};
use tokio::sync::mpsc;
use tracing::{debug, warn};

use crate::error::ExecutorError;

/// What the caller (`crate::invoke`, or a test double) does for every kind
/// of request the stage may send on this connection. `on_ping`/
/// `on_shutdown` have default no-op/log bodies since most callers (and
/// every test in this file) don't need to customize them.
pub trait RequestHandler: Send + Sync + 'static {
    /// Handle `load`: fetch, verify and precompile the bundle, then report
    /// what it exports.
    fn on_load(
        &self,
        body: LoadBody,
    ) -> impl std::future::Future<Output = Result<LoadedBody, ErrorBody>> + Send;

    /// Handle `unload`: drain and evict the bundle's cached artifact.
    fn on_unload(
        &self,
        body: UnloadBody,
    ) -> impl std::future::Future<Output = Result<UnloadedBody, ErrorBody>> + Send;

    /// Handle `invoke`: run the bundle's export under the per-call
    /// deadline, servicing host calls against `connection` as they occur.
    /// `invoke_id` is this `invoke` frame's correlation id, threaded onto
    /// every `host-call` issued during it as `HostCallBody.call_id` (spec
    /// SS6.6: "the `invoke` frame id this call happened during, so the
    /// stage can charge the call against that invocation's remaining
    /// deadline").
    fn on_invoke(
        &self,
        body: InvokeBody,
        invoke_id: u64,
        connection: Arc<Connection>,
    ) -> impl std::future::Future<Output = Result<ResultBody, ErrorBody>> + Send;

    /// Handle `ping`. Default: no-op (the caller replies `pong`
    /// unconditionally).
    fn on_ping(&self) -> impl std::future::Future<Output = ()> + Send {
        async {}
    }

    /// Handle `shutdown`: the read loop exits right after this returns.
    fn on_shutdown(&self, body: ShutdownBody) -> impl std::future::Future<Output = ()> + Send;
}

/// One host-API connection's request/reply bookkeeping: correlation ids
/// this side allocates (for `hello` and `host-call`, the two message
/// kinds the executor initiates -- spec SS6.6) and the channel frames are
/// written through.
pub struct Connection {
    ids: IdAllocator,
    pending: CorrelationTable,
    writer_tx: mpsc::UnboundedSender<Frame>,
}

impl Connection {
    /// `pub(crate)` (not `pub`) so `crate::host::imports`'s tests can build
    /// a `Connection` directly against a lightweight in-memory writer
    /// task instead of driving the full `hello`/`hello-ok` handshake
    /// `run_connection` normally requires -- production code never calls
    /// this from outside `wire`/`invoke`.
    pub(crate) fn new(writer_tx: mpsc::UnboundedSender<Frame>) -> Arc<Self> {
        Arc::new(Self {
            ids: IdAllocator::new(),
            pending: CorrelationTable::new(),
            writer_tx,
        })
    }

    /// Sends `message` under a freshly allocated id and awaits the peer's
    /// reply frame. Used for the two executor-initiated exchanges:
    /// `hello` -> `hello-ok`/`error`, and `host-call` -> `host-result`
    /// (spec SS6.6).
    pub async fn request(&self, message: Message) -> Result<Frame, ExecutorError> {
        let id = self.ids.next_id();
        let rx = self.pending.register(id)?;
        self.send(Frame::new(id, message))?;
        rx.await.map_err(|_| ExecutorError::ConnectionUnavailable)
    }

    /// Sends `message` as the reply to an incoming request, reusing its
    /// `id` (spec SS6.6: "every reply reuses it").
    pub fn reply_to(&self, id: u64, message: Message) -> Result<(), ExecutorError> {
        self.send(Frame::new(id, message))
    }

    fn send(&self, frame: Frame) -> Result<(), ExecutorError> {
        self.writer_tx
            .send(frame)
            .map_err(|_| ExecutorError::ConnectionUnavailable)
    }

    /// `pub(crate)` for the same reason as [`Connection::new`]: test-only
    /// direct construction from `crate::host::imports`.
    pub(crate) fn deliver(&self, frame: Frame) -> Result<(), CorrelationError> {
        self.pending.complete(frame)
    }
}

/// Dials nothing itself (the caller supplies an already-connected `io`,
/// real TLS or an in-memory test duplex): completes the `hello`/
/// `hello-ok` handshake, then runs the read loop until `shutdown`, EOF, or
/// a fatal protocol error. Returns once the connection is done; the
/// caller reconnects (spec SS4.5: "the executor reconnects with
/// exponential backoff").
pub async fn run_connection<S, H>(io: S, hello: HelloBody, handler: H) -> Result<(), ExecutorError>
where
    S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
    H: RequestHandler,
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
    let handler = Arc::new(handler);

    // Register + send `hello` before the read loop starts, then run the
    // read loop and the wait for its reply CONCURRENTLY via `select!`
    // rather than sequentially: the reply can only ever be delivered by
    // the read loop itself (via `Connection::deliver`, the same path
    // every later `host-call` reply takes), so awaiting it before the
    // read loop exists would deadlock forever -- there would be nothing
    // reading frames off the wire to satisfy it.
    let hello_id = connection.ids.next_id();
    let hello_rx = connection.pending.register(hello_id)?;
    connection.send(Frame::new(hello_id, Message::Hello(hello)))?;

    let mut read_loop_fut = Box::pin(read_loop(&mut reader, &connection, &handler));
    let mut hello_rx = hello_rx;
    let hello_reply;
    let mut loop_already_finished = None;
    tokio::select! {
        biased;
        reply = &mut hello_rx => {
            hello_reply = reply.map_err(|_| ExecutorError::ConnectionUnavailable)?;
        }
        loop_result = &mut read_loop_fut => {
            // The read loop can deliver the `hello-ok`/`error` reply and
            // keep going -- even to completion -- within a single poll,
            // if every frame the peer sent was already fully buffered (a
            // burst of `hello-ok` immediately followed by more frames).
            // `hello_rx` may therefore already hold a value even though
            // ITS poll never got a chance to run first; check for that
            // before concluding the handshake never completed, otherwise
            // a perfectly good connection that happened to finish very
            // quickly is misreported as `ConnectionUnavailable`.
            match hello_rx.try_recv() {
                Ok(reply) => {
                    hello_reply = reply;
                    loop_already_finished = Some(loop_result);
                }
                Err(_) => {
                    writer_task.abort();
                    return match loop_result {
                        Ok(()) => Err(ExecutorError::ConnectionUnavailable),
                        Err(e) => Err(e),
                    };
                }
            }
        }
    }
    let limits = match hello_reply.message {
        Message::HelloOk(HelloOkBody { limits, .. }) => limits,
        Message::Error(e) => {
            writer_task.abort();
            return Err(ExecutorError::HostCallDenied {
                capability: "connection",
                op: "hello",
                code: format!("{:?}", e.code),
                message: e.message,
            });
        }
        _ => {
            writer_task.abort();
            return Err(ExecutorError::UnexpectedFrame("expected hello-ok or error"));
        }
    };
    debug!(?limits, "host-api handshake complete");

    // If the read loop already ran to completion while delivering the
    // `hello-ok` reply (see the `loop_already_finished` comment above),
    // its outcome IS the connection's final result -- polling the same
    // future again would panic ("future polled after completion").
    let result = match loop_already_finished {
        Some(result) => result,
        None => read_loop_fut.await,
    };
    writer_task.abort();
    result
}

async fn read_loop<R, H>(
    reader: &mut R,
    connection: &Arc<Connection>,
    handler: &Arc<H>,
) -> Result<(), ExecutorError>
where
    R: AsyncRead + Unpin,
    H: RequestHandler,
{
    loop {
        let frame = read_frame(reader).await?;
        match frame.message {
            // Replies to something this side initiated (hello, host-call).
            Message::HelloOk(_) | Message::HostResult(_) => {
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
                    // Spec SS6.6: an `error` frame is fatal for the
                    // connection it occurred on unless it was the awaited
                    // reply to something this side requested (handled by
                    // `deliver` above).
                    return Err(ExecutorError::HostCallDenied {
                        capability: "connection",
                        op: "unsolicited",
                        code: format!("{:?}", e.code),
                        message: e.message.clone(),
                    });
                }
            }
            // Requests the stage initiates; the executor answers by
            // reusing the incoming frame's id (spec SS6.6). Each is
            // spawned onto its own task rather than awaited inline: an
            // `invoke` handler issues `host-call` requests back through
            // this very `connection`, and their `host-result` replies can
            // only be delivered by THIS read loop continuing to read the
            // next frame -- awaiting `on_invoke` inline here would
            // deadlock the very first host call an invocation makes.
            // `load`/`unload`/`ping` are spawned too so a slow compile
            // (spec SS7.2 measured ~3-4s for a large component) never
            // blocks host-call delivery for calls already in flight.
            Message::Load(body) => {
                let (connection, handler) = (Arc::clone(connection), Arc::clone(handler));
                tokio::spawn(async move {
                    let reply = match handler.on_load(body).await {
                        Ok(loaded) => Message::Loaded(loaded),
                        Err(e) => Message::Error(e),
                    };
                    let _ = connection.reply_to(frame.id, reply);
                });
            }
            Message::Unload(body) => {
                let (connection, handler) = (Arc::clone(connection), Arc::clone(handler));
                tokio::spawn(async move {
                    let reply = match handler.on_unload(body).await {
                        Ok(unloaded) => Message::Unloaded(unloaded),
                        Err(e) => Message::Error(e),
                    };
                    let _ = connection.reply_to(frame.id, reply);
                });
            }
            Message::Invoke(body) => {
                let (connection, handler) = (Arc::clone(connection), Arc::clone(handler));
                tokio::spawn(async move {
                    let reply = match handler
                        .on_invoke(body, frame.id, Arc::clone(&connection))
                        .await
                    {
                        Ok(result) => Message::Result(result),
                        Err(e) => Message::Error(e),
                    };
                    let _ = connection.reply_to(frame.id, reply);
                });
            }
            Message::Ping => {
                let (connection, handler) = (Arc::clone(connection), Arc::clone(handler));
                tokio::spawn(async move {
                    handler.on_ping().await;
                    let _ = connection.reply_to(frame.id, Message::Pong);
                });
            }
            Message::Shutdown(body) => {
                handler.on_shutdown(body).await;
                return Ok(());
            }
            other => {
                warn!(
                    ?other,
                    "host-api received a frame kind this side never expects"
                );
                return Err(ExecutorError::UnexpectedFrame(
                    "unexpected frame kind on executor side",
                ));
            }
        }
    }
}

/// Issues one `host-call` request and interprets the `host-result` reply,
/// converting a stage-side error into [`ExecutorError::HostCallDenied`].
/// The single entry point every WIT `Host` trait impl in `crate::host`
/// funnels through (see `crate::host::bridge`).
pub async fn host_call(
    connection: &Connection,
    body: penguin_bundle_host::wire::HostCallBody,
    capability: &'static str,
    op: &'static str,
) -> Result<serde_json::Value, ExecutorError> {
    let reply = connection.request(Message::HostCall(body)).await?;
    match reply.message {
        Message::HostResult(r) => match (r.result, r.error) {
            (Some(v), None) => Ok(v),
            (None, Some(e)) => Err(ExecutorError::HostCallDenied {
                capability,
                op,
                code: e.code,
                message: e.message,
            }),
            _ => Err(ExecutorError::MalformedHostCall {
                capability,
                op,
                detail: "host-result carried both/neither of result and error".to_string(),
            }),
        },
        _ => Err(ExecutorError::UnexpectedFrame("expected host-result")),
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use penguin_bundle_host::wire::{
        ErrorCode, HelloLimits, HostCallBody, HostResultBody, SandboxInfo,
    };
    use std::sync::atomic::{AtomicBool, Ordering};

    struct EchoHandler {
        shutdown_seen: AtomicBool,
    }

    impl RequestHandler for EchoHandler {
        async fn on_load(&self, body: LoadBody) -> Result<LoadedBody, ErrorBody> {
            Ok(LoadedBody {
                app_id: body.app_id,
                digest: body.digest,
                precompile_ms: 1,
                exports: vec!["transform".to_string()],
            })
        }

        async fn on_unload(&self, body: UnloadBody) -> Result<UnloadedBody, ErrorBody> {
            Ok(UnloadedBody {
                app_id: body.app_id,
                digest: body.digest,
            })
        }

        async fn on_invoke(
            &self,
            body: InvokeBody,
            invoke_id: u64,
            connection: Arc<Connection>,
        ) -> Result<ResultBody, ErrorBody> {
            // Exercises the host-call round trip from inside an invoke
            // handler, same shape production `crate::invoke` uses.
            let host_reply = host_call(
                &connection,
                HostCallBody {
                    app_id: body.app_id,
                    capability: penguin_bundle_host::wire::CapabilityKind::Clock,
                    op: "now-millis".to_string(),
                    args: serde_json::json!({}),
                    call_id: invoke_id,
                },
                "clock",
                "now-millis",
            )
            .await;
            Ok(ResultBody {
                payload: serde_json::json!({ "host_reply_ok": host_reply.is_ok(), "echo": body.payload }),
                duration_ms: 1,
                fuel_used: 0,
            })
        }

        async fn on_shutdown(&self, _body: ShutdownBody) {
            self.shutdown_seen.store(true, Ordering::SeqCst);
        }
    }

    /// Drives the *stage* side of an in-memory duplex: answers the
    /// executor's `hello` with `hello-ok`, answers one `host-call` with a
    /// canned `host-result`, then sends `shutdown`.
    async fn run_fake_stage<S: AsyncRead + AsyncWrite + Unpin>(mut io: S) -> Frame {
        let hello = read_frame(&mut io).await.expect("hello");
        let hello_id = hello.id;
        write_frame(
            &mut io,
            &Frame::new(
                hello_id,
                Message::HelloOk(HelloOkBody {
                    stage: "svc-process".to_string(),
                    protocol_version: 1,
                    limits: HelloLimits {
                        call_timeout_ms: 2000,
                        memory_mb: 64,
                        max_concurrent_calls: 32,
                    },
                }),
            ),
        )
        .await
        .expect("write hello-ok");

        // Send `load`, expect `loaded` back.
        write_frame(
            &mut io,
            &Frame::new(
                100,
                Message::Load(LoadBody {
                    app_id: "waddles.test.app".to_string(),
                    version: "1".to_string(),
                    digest: "sha256:00".to_string(),
                    component_key: "k".to_string(),
                    sidecar_key: "s".to_string(),
                    capabilities: vec![],
                    limits: penguin_bundle_host::wire::LoadLimits {
                        timeout_ms: 2000,
                        memory_mb: 64,
                    },
                }),
            ),
        )
        .await
        .expect("write load");
        let loaded = read_frame(&mut io).await.expect("loaded reply");
        assert!(matches!(loaded.message, Message::Loaded(_)));

        // Send `invoke`; the handler will issue a `host-call` mid-flight.
        write_frame(
            &mut io,
            &Frame::new(
                101,
                Message::Invoke(InvokeBody {
                    app_id: "waddles.test.app".to_string(),
                    digest: "sha256:00".to_string(),
                    export: penguin_bundle_host::wire::ExportKind::Transform,
                    payload: serde_json::json!({"hello": "world"}),
                    deadline_ms: 2000,
                    trace: None,
                }),
            ),
        )
        .await
        .expect("write invoke");

        let host_call_frame = read_frame(&mut io).await.expect("host-call");
        let hc_id = host_call_frame.id;
        assert!(matches!(host_call_frame.message, Message::HostCall(_)));
        write_frame(
            &mut io,
            &Frame::new(
                hc_id,
                Message::HostResult(HostResultBody {
                    result: Some(serde_json::json!(1_700_000_000_000_u64)),
                    error: None,
                }),
            ),
        )
        .await
        .expect("write host-result");

        let result = read_frame(&mut io).await.expect("result reply");
        assert!(matches!(result.message, Message::Result(_)));

        write_frame(
            &mut io,
            &Frame::new(102, Message::Shutdown(ShutdownBody { grace_ms: 100 })),
        )
        .await
        .expect("write shutdown");

        result
    }

    #[tokio::test]
    async fn full_handshake_load_invoke_hostcall_shutdown_round_trip() {
        let (executor_io, stage_io) = tokio::io::duplex(64 * 1024);
        let handler = std::sync::Arc::new(EchoHandler {
            shutdown_seen: AtomicBool::new(false),
        });
        let handler_for_conn = handler.clone();

        let stage = tokio::spawn(run_fake_stage(stage_io));

        let executor = tokio::spawn(async move {
            run_connection(
                executor_io,
                HelloBody {
                    protocol_version: 1,
                    executor_version: "0.1.0".to_string(),
                    wasmtime_version: "43.0.2".to_string(),
                    wasmtime_abi: "test".to_string(),
                    collector: "drc".to_string(),
                    sandbox: SandboxInfo {
                        runtime: "runc".to_string(),
                        verified: false,
                    },
                },
                EchoHandlerRef(handler_for_conn),
            )
            .await
        });

        let result_frame = stage.await.expect("stage task");
        executor
            .await
            .expect("executor task")
            .expect("connection ran cleanly");
        assert!(handler.shutdown_seen.load(Ordering::SeqCst));

        if let Message::Result(r) = result_frame.message {
            assert_eq!(r.payload["host_reply_ok"], serde_json::json!(true));
        } else {
            panic!("expected a Result message");
        }
    }

    /// `RequestHandler` requires `Send + Sync + 'static`; wrapping the
    /// shared `Arc<EchoHandler>` behind a thin newtype keeps ownership
    /// clear at each call site in the test above.
    struct EchoHandlerRef(std::sync::Arc<EchoHandler>);
    impl RequestHandler for EchoHandlerRef {
        async fn on_load(&self, body: LoadBody) -> Result<LoadedBody, ErrorBody> {
            self.0.on_load(body).await
        }
        async fn on_unload(&self, body: UnloadBody) -> Result<UnloadedBody, ErrorBody> {
            self.0.on_unload(body).await
        }
        async fn on_invoke(
            &self,
            body: InvokeBody,
            invoke_id: u64,
            connection: Arc<Connection>,
        ) -> Result<ResultBody, ErrorBody> {
            self.0.on_invoke(body, invoke_id, connection).await
        }
        async fn on_shutdown(&self, body: ShutdownBody) {
            self.0.on_shutdown(body).await
        }
    }

    #[tokio::test]
    async fn hello_error_reply_surfaces_as_host_call_denied() {
        let (executor_io, mut stage_io) = tokio::io::duplex(4096);
        let executor = tokio::spawn(async move {
            run_connection(
                executor_io,
                HelloBody {
                    protocol_version: 1,
                    executor_version: "0.1.0".to_string(),
                    wasmtime_version: "43.0.2".to_string(),
                    wasmtime_abi: "test".to_string(),
                    collector: "drc".to_string(),
                    sandbox: SandboxInfo {
                        runtime: "runc".to_string(),
                        verified: false,
                    },
                },
                EchoHandlerRef(std::sync::Arc::new(EchoHandler {
                    shutdown_seen: AtomicBool::new(false),
                })),
            )
            .await
        });

        let hello = read_frame(&mut stage_io).await.expect("hello");
        write_frame(
            &mut stage_io,
            &Frame::new(
                hello.id,
                Message::Error(ErrorBody {
                    code: ErrorCode::UnsandboxedExecutor,
                    message: "gVisor required".to_string(),
                    detail: None,
                }),
            ),
        )
        .await
        .expect("write error");

        let result = executor.await.expect("executor task");
        assert!(matches!(result, Err(ExecutorError::HostCallDenied { .. })));
    }

    fn test_hello() -> HelloBody {
        HelloBody {
            protocol_version: 1,
            executor_version: "0.1.0".to_string(),
            wasmtime_version: "test".to_string(),
            wasmtime_abi: "test".to_string(),
            collector: "drc".to_string(),
            sandbox: SandboxInfo {
                runtime: "runc".to_string(),
                verified: false,
            },
        }
    }

    async fn complete_handshake<S: AsyncRead + AsyncWrite + Unpin>(io: &mut S) {
        let hello = read_frame(io).await.expect("hello");
        write_frame(
            io,
            &Frame::new(
                hello.id,
                Message::HelloOk(HelloOkBody {
                    stage: "svc-process".to_string(),
                    protocol_version: 1,
                    limits: penguin_bundle_host::wire::HelloLimits {
                        call_timeout_ms: 2000,
                        memory_mb: 64,
                        max_concurrent_calls: 32,
                    },
                }),
            ),
        )
        .await
        .expect("write hello-ok");
    }

    #[tokio::test]
    async fn ping_is_answered_with_pong() {
        crate::init_test_tracing();
        let (executor_io, mut stage_io) = tokio::io::duplex(4096);
        let executor = tokio::spawn(async move {
            run_connection(
                executor_io,
                test_hello(),
                EchoHandlerRef(std::sync::Arc::new(EchoHandler {
                    shutdown_seen: AtomicBool::new(false),
                })),
            )
            .await
        });

        complete_handshake(&mut stage_io).await;
        write_frame(&mut stage_io, &Frame::new(200, Message::Ping))
            .await
            .expect("write ping");
        let pong = read_frame(&mut stage_io).await.expect("pong reply");
        assert!(matches!(pong.message, Message::Pong));

        write_frame(
            &mut stage_io,
            &Frame::new(201, Message::Shutdown(ShutdownBody { grace_ms: 10 })),
        )
        .await
        .expect("write shutdown");
        executor
            .await
            .expect("executor task")
            .expect("clean shutdown");
    }

    #[tokio::test]
    async fn unload_success_round_trip() {
        let (executor_io, mut stage_io) = tokio::io::duplex(4096);
        let executor = tokio::spawn(async move {
            run_connection(
                executor_io,
                test_hello(),
                EchoHandlerRef(std::sync::Arc::new(EchoHandler {
                    shutdown_seen: AtomicBool::new(false),
                })),
            )
            .await
        });

        complete_handshake(&mut stage_io).await;
        write_frame(
            &mut stage_io,
            &Frame::new(
                300,
                Message::Unload(UnloadBody {
                    app_id: "waddles.test.app".to_string(),
                    digest: "sha256:00".to_string(),
                }),
            ),
        )
        .await
        .expect("write unload");
        let reply = read_frame(&mut stage_io).await.expect("unloaded reply");
        assert!(matches!(reply.message, Message::Unloaded(_)));

        write_frame(
            &mut stage_io,
            &Frame::new(301, Message::Shutdown(ShutdownBody { grace_ms: 10 })),
        )
        .await
        .expect("write shutdown");
        executor
            .await
            .expect("executor task")
            .expect("clean shutdown");
    }

    #[tokio::test]
    async fn an_unexpected_frame_kind_is_a_fatal_protocol_error() {
        crate::init_test_tracing();
        let (executor_io, mut stage_io) = tokio::io::duplex(4096);
        let executor = tokio::spawn(async move {
            run_connection(
                executor_io,
                test_hello(),
                EchoHandlerRef(std::sync::Arc::new(EchoHandler {
                    shutdown_seen: AtomicBool::new(false),
                })),
            )
            .await
        });

        complete_handshake(&mut stage_io).await;
        // `Result` is only ever sent executor -> stage; the stage sending
        // one to the executor is a frame kind this side never expects.
        write_frame(
            &mut stage_io,
            &Frame::new(
                400,
                Message::Result(penguin_bundle_host::wire::ResultBody {
                    payload: serde_json::json!(null),
                    duration_ms: 0,
                    fuel_used: 0,
                }),
            ),
        )
        .await
        .expect("write unexpected frame");

        let result = executor.await.expect("executor task");
        assert!(matches!(result, Err(ExecutorError::UnexpectedFrame(_))));
    }

    #[tokio::test]
    async fn an_unsolicited_error_frame_closes_the_connection() {
        crate::init_test_tracing();
        let (executor_io, mut stage_io) = tokio::io::duplex(4096);
        let executor = tokio::spawn(async move {
            run_connection(
                executor_io,
                test_hello(),
                EchoHandlerRef(std::sync::Arc::new(EchoHandler {
                    shutdown_seen: AtomicBool::new(false),
                })),
            )
            .await
        });

        complete_handshake(&mut stage_io).await;
        write_frame(
            &mut stage_io,
            &Frame::new(
                500,
                Message::Error(ErrorBody {
                    code: ErrorCode::ShuttingDown,
                    message: "unprompted".to_string(),
                    detail: None,
                }),
            ),
        )
        .await
        .expect("write unsolicited error");

        let result = executor.await.expect("executor task");
        assert!(matches!(result, Err(ExecutorError::HostCallDenied { .. })));
    }

    #[tokio::test]
    async fn a_reply_matching_no_pending_request_is_dropped_not_fatal() {
        crate::init_test_tracing();
        let (executor_io, mut stage_io) = tokio::io::duplex(4096);
        let executor = tokio::spawn(async move {
            run_connection(
                executor_io,
                test_hello(),
                EchoHandlerRef(std::sync::Arc::new(EchoHandler {
                    shutdown_seen: AtomicBool::new(false),
                })),
            )
            .await
        });

        complete_handshake(&mut stage_io).await;
        // No `host-call` with id 999 was ever issued; this reply matches
        // nothing pending and must be dropped, not treated as fatal.
        write_frame(
            &mut stage_io,
            &Frame::new(
                999,
                Message::HostResult(penguin_bundle_host::wire::HostResultBody {
                    result: Some(serde_json::json!(null)),
                    error: None,
                }),
            ),
        )
        .await
        .expect("write orphaned host-result");

        write_frame(
            &mut stage_io,
            &Frame::new(1000, Message::Shutdown(ShutdownBody { grace_ms: 10 })),
        )
        .await
        .expect("write shutdown");
        executor
            .await
            .expect("executor task")
            .expect("clean shutdown despite the dropped orphan reply");
    }
}
