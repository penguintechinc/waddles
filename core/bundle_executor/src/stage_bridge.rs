//! Test-support seam: a REAL [`HostBridge`] whose host-calls travel over a
//! caller-supplied stream to a REAL stage (e.g. svc_ingest's `ExecutorLink`),
//! so an integration test can prove a connector's `flags`/`identity` imports
//! are answered by the stage's own code instead of a scripted double.
//!
//! [`crate::wire::Connection`] is crate-private, so [`bridge_to_stage`] does
//! the executor half of the host-API handshake itself (`hello` -> `hello-ok`)
//! and then runs the same writer/reader tasks [`crate::parity_support`] uses.
//! Nothing here is reachable from the production binary's request path.

use std::sync::Arc;

use penguin_bundle_host::wire::message::PROTOCOL_VERSION;
use penguin_bundle_host::wire::{read_frame, write_frame, Frame, HelloBody, Message, SandboxInfo};
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::sync::mpsc;

use crate::error::ExecutorError;
use crate::host::HostBridge;
use crate::wire::{Connection, WRITER_QUEUE_CAPACITY};

/// Dials `io` as an executor: sends `hello`, requires `hello-ok`, then returns
/// a [`HostBridge`] whose host-calls are written to `io` and answered by the
/// stage on the other end.
///
/// # Errors
/// [`ExecutorError::Config`] when the stage does not answer `hello` with
/// `hello-ok`.
pub async fn bridge_to_stage<IO>(io: IO) -> Result<Arc<HostBridge>, ExecutorError>
where
    IO: AsyncRead + AsyncWrite + Unpin + Send + 'static,
{
    let (mut reader, mut writer) = tokio::io::split(io);
    let hello = Frame::new(
        1,
        Message::Hello(HelloBody {
            protocol_version: u32::from(PROTOCOL_VERSION),
            executor_version: "stage-bridge".to_string(),
            wasmtime_version: "test".to_string(),
            wasmtime_abi: "test".to_string(),
            collector: "drc".to_string(),
            sandbox: SandboxInfo {
                runtime: "runc".to_string(),
                verified: false,
            },
            executor_id: None,
        }),
    );
    write_frame(&mut writer, &hello)
        .await
        .map_err(|e| ExecutorError::Config(format!("stage-bridge hello failed: {e}")))?;
    let ok = read_frame(&mut reader)
        .await
        .map_err(|e| ExecutorError::Config(format!("stage-bridge hello-ok failed: {e}")))?;
    if !matches!(ok.message, Message::HelloOk(_)) {
        return Err(ExecutorError::Config(
            "stage-bridge: stage did not answer hello with hello-ok".to_string(),
        ));
    }

    let (writer_tx, mut writer_rx) = mpsc::channel::<Frame>(WRITER_QUEUE_CAPACITY);
    let connection = Connection::new(writer_tx);
    tokio::spawn(async move {
        while let Some(frame) = writer_rx.recv().await {
            if write_frame(&mut writer, &frame).await.is_err() {
                break;
            }
        }
    });
    let reader_conn = Arc::clone(&connection);
    tokio::spawn(async move {
        while let Ok(frame) = read_frame(&mut reader).await {
            let _ = reader_conn.deliver(frame);
        }
    });
    Ok(HostBridge::new(connection))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use penguin_bundle_host::wire::{CapabilityKind, HelloLimits, HelloOkBody, HostResultBody};

    fn hello_ok() -> Message {
        Message::HelloOk(HelloOkBody {
            stage: "svc-test".to_string(),
            protocol_version: u32::from(PROTOCOL_VERSION),
            limits: HelloLimits {
                call_timeout_ms: 1000,
                memory_mb: 64,
                max_concurrent_calls: 4,
            },
        })
    }

    /// A stage that answers `hello` with `reply`, then (if it was `hello-ok`) serves host calls
    /// by echoing the op name back as the result.
    fn spawn_stage(io: tokio::io::DuplexStream, reply: Message) {
        tokio::spawn(async move {
            let (mut r, mut w) = tokio::io::split(io);
            let hello = read_frame(&mut r).await.unwrap();
            assert!(matches!(hello.message, Message::Hello(_)));
            let ok = matches!(reply, Message::HelloOk(_));
            write_frame(&mut w, &Frame::new(hello.id, reply))
                .await
                .unwrap();
            if !ok {
                return;
            }
            while let Ok(frame) = read_frame(&mut r).await {
                if let Message::HostCall(body) = frame.message {
                    let result = HostResultBody {
                        result: Some(serde_json::json!({ "echo": body.op })),
                        error: None,
                    };
                    if write_frame(&mut w, &Frame::new(frame.id, Message::HostResult(result)))
                        .await
                        .is_err()
                    {
                        break;
                    }
                }
            }
        });
    }

    #[tokio::test]
    async fn a_hello_ok_stage_yields_a_bridge_that_round_trips_host_calls() {
        let (exec_io, stage_io) = tokio::io::duplex(16 * 1024);
        spawn_stage(stage_io, hello_ok());
        let bridge = bridge_to_stage(exec_io).await.expect("handshake ok");
        let out = bridge
            .call(
                "app",
                7,
                CapabilityKind::Context,
                "get-context",
                serde_json::json!({}),
            )
            .await
            .expect("host call answered by the stage");
        assert_eq!(out["echo"], "get-context");
    }

    #[tokio::test]
    async fn a_stage_that_does_not_answer_hello_ok_is_refused() {
        let (exec_io, stage_io) = tokio::io::duplex(16 * 1024);
        let wrong = Message::HostResult(HostResultBody {
            result: Some(serde_json::json!({})),
            error: None,
        });
        spawn_stage(stage_io, wrong);
        let err = bridge_to_stage(exec_io).await.err().expect("refused");
        assert!(
            err.to_string()
                .contains("did not answer hello with hello-ok"),
            "{err}"
        );
    }

    #[tokio::test]
    async fn a_stage_that_hangs_up_fails_the_handshake() {
        let (exec_io, stage_io) = tokio::io::duplex(16 * 1024);
        drop(stage_io);
        let err = bridge_to_stage(exec_io)
            .await
            .err()
            .expect("handshake fails");
        assert!(err.to_string().contains("stage-bridge hello"), "{err}");
    }
}
