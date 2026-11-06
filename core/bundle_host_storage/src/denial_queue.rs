//! Bounded async queue for DENIAL audit records. Denials are attacker-triggerable
//! (a bundle can spam cross-prefix calls it is not granted), so they must never
//! compete with the fail-closed ALLOWED-path audits for the sink's pool: one worker
//! drains the queue sequentially (at most ONE sink call in flight for denials);
//! when the queue is full a denial record is dropped and counted
//! (`waddles_bundle_storage_audit_dropped_total`) -- the denial itself is unaffected.

use std::sync::Arc;

use tokio::sync::{mpsc, oneshot};

use crate::audit::{AuditEvent, AuditSink};
use crate::metrics;

enum Msg {
    Event(AuditEvent),
    Flush(oneshot::Sender<()>),
}

/// Handle to the denial-audit worker.
pub struct DenialQueue {
    tx: mpsc::Sender<Msg>,
}

impl DenialQueue {
    /// Spawns the worker on the current Tokio runtime.
    pub fn new(sink: Arc<dyn AuditSink>, capacity: usize) -> Self {
        let (tx, mut rx) = mpsc::channel::<Msg>(capacity.max(1));
        tokio::spawn(async move {
            while let Some(msg) = rx.recv().await {
                match msg {
                    Msg::Event(e) => {
                        if let Err(err) = sink.record(e).await {
                            tracing::error!(error = %err, "bundle storage denial audit write failed");
                        }
                    }
                    Msg::Flush(done) => {
                        // Receiver gone == flusher gave up; nothing to do.
                        let _ = done.send(());
                    }
                }
            }
        });
        Self { tx }
    }

    /// Enqueues without ever awaiting; a full queue drops + counts the record.
    pub fn enqueue(&self, event: AuditEvent) {
        if self.tx.try_send(Msg::Event(event)).is_err() {
            metrics::audit_dropped();
            tracing::warn!("bundle storage denial audit queue full; record dropped");
        }
    }

    /// Resolves once every record enqueued before this call has been written (tests/shutdown).
    pub async fn flush(&self) {
        let (done, wait) = oneshot::channel();
        if self.tx.send(Msg::Flush(done)).await.is_ok() {
            // Worker dropped the reply only if it exited; either way we are done waiting.
            let _ = wait.await;
        }
    }
}
