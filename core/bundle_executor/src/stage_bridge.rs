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
