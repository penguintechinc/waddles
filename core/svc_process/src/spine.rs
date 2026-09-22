//! Wires `penguin-spine` (spec SS4.7) as the process-stage consumer:
//! `XREADGROUP`s a bundle's granted ingest-source Valkey streams, up to the
//! exact point a delivered entry would be handed to the bundle's
//! `transform` call over the `bundle-executor` wire protocol -- and stops
//! there. `bundle-executor` does not exist yet (Wave 2, blocked on M2), so
//! [`handle_delivered`] is the seam and nothing past it is implemented or
//! faked.
//!
//! This is the concrete consumer half of the M4 penguin-libs dependency
//! pattern established in `Cargo.toml` (the git-dependency mechanism) and
//! `crate::telemetry` (the sanitizing OTel wiring); this module is the
//! third and final M1 crate wired into this skeleton
//! (`penguin-bundle-host`/`penguin-connectors` remain `// TODO(M4)`,
//! blocked on M2's executor/compiler -- see `crate::lib`).
//!
//! The drain loop's control flow (batch dispatch, shutdown handling) is
//! split from `penguin_spine::GroupReader`'s actual `XREADGROUP` I/O behind
//! the private [`StreamReader`] trait so it can be unit-tested against a
//! fake reader -- `cargo test` has no live Valkey to connect to, and the
//! real `GroupReader::connect` path is only exercised by a running service
//! (or a future `testcontainers`-backed integration test, see
//! `implementing-database-patterns` skill).

use std::sync::Arc;

use penguin_spine::{
    Delivered, Grant, GroupReader, SpineClient, SpineConfig, SpineError, SpineMetrics, Stage,
};

/// Abstraction over [`penguin_spine::GroupReader::read`]'s exact signature,
/// implemented for `GroupReader` itself as a pure delegation. Exists solely
/// so [`drain_loop`] can be driven by a fake reader in tests.
trait StreamReader {
    async fn read(&mut self) -> Result<Vec<Delivered>, SpineError>;
}

impl StreamReader for GroupReader {
    async fn read(&mut self) -> Result<Vec<Delivered>, SpineError> {
        GroupReader::read(self).await
    }
}

/// The executor-invocation seam (spec SS4.2, SS16's M4 row). Every entry
/// `penguin-spine` successfully reads and deserializes reaches exactly this
/// point and no further in the M4 skeleton -- it is logged for
/// observability and then intentionally left un-acked: an unacknowledged
/// entry stays in the consumer group's pending-entries list (at-least-once
/// delivery, spec SS5.3), which is the correct outcome for work that was
/// never actually attempted, not a bug to fix later.
///
/// TODO(M4): executor integration -- blocked on bundle-executor (Wave 2).
/// Once the executor binary exists, this function additionally invokes the
/// bundle's `transform` over its mTLS wire protocol, runs the stage
/// built-ins (moderation gate, enforcement routing, cross-app
/// `_target_app_id` routing) around that call, `SpineClient::append`s the
/// result onto the bundle's `:action` stream, and only then
/// `SpineClient::ack`s the source entry -- see SS4.2.
fn handle_delivered(d: &Delivered, metrics: &dyn SpineMetrics) {
    tracing::info!(
        stream = %d.stream,
        entry_id = %d.entry_id,
        app_id = %d.env.app_id,
        tenant = %d.env.tenant,
        event_type = %d.env.event.event_type,
        deliveries = d.deliveries,
        "delivered entry received; executor integration is TODO(M4), blocked on bundle-executor (Wave 2)"
    );
    metrics.consumer_skipped(&d.env.app_id, "executor_not_implemented");
}

/// Reads and dispatches exactly one batch, returning how many entries were
/// handled. Split out from [`drain_loop`] so both the dispatch logic and
/// the loop/shutdown control flow are independently unit-tested.
async fn drain_batch<R: StreamReader>(
    reader: &mut R,
    metrics: &dyn SpineMetrics,
) -> Result<usize, SpineError> {
    let batch = reader.read().await?;
    for d in &batch {
        handle_delivered(d, metrics);
    }
    Ok(batch.len())
}

/// Runs [`drain_batch`] in a loop until `shutdown` resolves. A
/// `tokio::sync::oneshot::Receiver` is used (rather than a generic
/// `Future`) specifically because it is safe to poll repeatedly after
/// resolving -- `tokio::select!` re-polls every still-enabled branch on
/// every loop iteration regardless of which branch it picks, and a
/// one-shot-style future like `std::future::ready` would panic if it
/// "loses" a race once and is polled again on the next iteration; a
/// `oneshot::Receiver` instead keeps returning `Ready` once its sender is
/// gone, so reusing `&mut shutdown` across iterations is sound.
async fn drain_loop<R: StreamReader>(
    mut reader: R,
    metrics: Arc<dyn SpineMetrics>,
    mut shutdown: tokio::sync::oneshot::Receiver<()>,
) -> Result<(), SpineError> {
    loop {
        tokio::select! {
            _ = &mut shutdown => return Ok(()),
            result = drain_batch(&mut reader, metrics.as_ref()) => {
                result?;
            }
        }
    }
}

/// Connects the spine DLQ client and a grant-scoped [`GroupReader`] for
/// `app_id`'s process-stage streams, then runs the drain loop until
/// `shutdown` resolves. This is the function `crate::run_with_shutdown`
/// spawns as its own background task -- see the `// TODO(M4)` seam there.
///
/// `grants` is empty in every call site this skeleton has today: the
/// `GET /api/v1/distribution/bundles?stage=process` poll that would
/// resolve a bundle's granted ingest-source streams is itself blocked on
/// M2 (spec SS4.2). `GroupReader::read` on an empty grant list returns
/// `Ok(vec![])` immediately without blocking (see `penguin_spine`'s own
/// `read` doc comment), so this still connects to Valkey for real and
/// exercises the drain loop's shutdown path safely with nothing to read.
pub async fn run(
    cfg: SpineConfig,
    app_id: String,
    grants: Vec<Grant>,
    metrics: Arc<dyn SpineMetrics>,
    shutdown: tokio::sync::oneshot::Receiver<()>,
) -> Result<(), SpineError> {
    let dlq = SpineClient::connect(cfg.clone(), metrics.clone()).await?;
    let reader =
        GroupReader::connect(&cfg, grants, app_id, Stage::Process, dlq, metrics.clone()).await?;
    drain_loop(reader, metrics, shutdown).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    /// A fully-formed, schema-valid [`Delivered`] entry for tests -- every
    /// field on [`penguin_spine::StageEnvelope`] is `pub`, so constructing
    /// one directly (bypassing strict JSON deserialization, which is
    /// `penguin_spine`'s own concern, already covered by its own test
    /// suite) is the simplest fixture for exercising this module's dispatch
    /// logic.
    fn fixture_delivered(app_id: &str) -> Delivered {
        Delivered {
            stream: "waddles:t:acme:c:main:src:twitch:tw-channelA:events".to_string(),
            entry_id: "1234567890-0".to_string(),
            env: penguin_spine::StageEnvelope {
                schema_version: penguin_spine::ENVELOPE_SCHEMA_VERSION,
                tenant: "acme".to_string(),
                community: Some("main".to_string()),
                app_id: app_id.to_string(),
                stage: "process".to_string(),
                event: penguin_spine::PlatformEvent {
                    platform: "twitch".to_string(),
                    event_type: "chat.message".to_string(),
                    actor: Some("some_user".to_string()),
                    payload: serde_json::Map::new(),
                    occurred_at: "2026-09-22T00:00:00.000Z".to_string(),
                    source: None,
                },
                ts: "2026-09-22T00:00:00.000Z".to_string(),
                target_app_id: None,
                workstream_id: "00000000-0000-0000-0000-000000000001".to_string(),
                event_id: "00000000-0000-4000-8000-000000000002".to_string(),
                session_id: None,
                trace: None,
                binding: penguin_spine::Binding {
                    kid: "k1".to_string(),
                    mac: "a".repeat(64),
                },
            },
            deliveries: 1,
        }
    }

    /// Records every `SpineMetrics` call it receives, for asserting exactly
    /// which callback fired and with what arguments -- mirrors
    /// `penguin_spine`'s own `RecordingMetrics` test helper.
    #[derive(Default)]
    struct RecordingMetrics {
        calls: Mutex<Vec<(String, String)>>,
    }

    impl SpineMetrics for RecordingMetrics {
        fn consumer_skipped(&self, app_id: &str, reason: &str) {
            self.calls
                .lock()
                .unwrap()
                .push((app_id.to_string(), reason.to_string()));
        }
    }

    /// A [`StreamReader`] fed a fixed sequence of canned results, one per
    /// call to `read()`; the last result repeats once the sequence is
    /// exhausted so tests that race against a shutdown signal never panic
    /// on running out of fixtures.
    struct FakeReader {
        batches: Vec<Result<Vec<Delivered>, SpineErrorKind>>,
        calls: usize,
    }

    /// `SpineError` doesn't implement `Clone` (its `Redis`/`Json` variants
    /// wrap non-`Clone` external error types), so `FakeReader` stores this
    /// small `Clone`-able stand-in and builds the real `SpineError` lazily
    /// in `read()`.
    #[derive(Clone)]
    enum SpineErrorKind {
        Config(String),
    }

    impl FakeReader {
        fn new(batches: Vec<Result<Vec<Delivered>, SpineErrorKind>>) -> Self {
            Self { batches, calls: 0 }
        }
    }

    impl StreamReader for FakeReader {
        async fn read(&mut self) -> Result<Vec<Delivered>, SpineError> {
            let idx = self.calls.min(self.batches.len() - 1);
            self.calls += 1;
            match &self.batches[idx] {
                Ok(batch) => Ok(batch.clone()),
                Err(SpineErrorKind::Config(msg)) => Err(SpineError::Config(msg.clone())),
            }
        }
    }

    #[tokio::test]
    async fn handle_delivered_reports_executor_not_implemented() {
        let metrics = RecordingMetrics::default();
        let delivered = fixture_delivered("waddles.bot.commands.default");
        handle_delivered(&delivered, &metrics);
        let calls = metrics.calls.lock().unwrap();
        assert_eq!(
            *calls,
            vec![(
                "waddles.bot.commands.default".to_string(),
                "executor_not_implemented".to_string()
            )]
        );
    }

    #[tokio::test]
    async fn drain_batch_dispatches_every_entry_in_the_batch() {
        let metrics = RecordingMetrics::default();
        let mut reader = FakeReader::new(vec![Ok(vec![
            fixture_delivered("waddles.bot.commands.default"),
            fixture_delivered("waddles.bot.commands.default"),
        ])]);
        let count = drain_batch(&mut reader, &metrics).await.unwrap();
        assert_eq!(count, 2);
        assert_eq!(metrics.calls.lock().unwrap().len(), 2);
    }

    #[tokio::test]
    async fn drain_batch_on_empty_batch_returns_zero_without_dispatch() {
        let metrics = RecordingMetrics::default();
        let mut reader = FakeReader::new(vec![Ok(vec![])]);
        let count = drain_batch(&mut reader, &metrics).await.unwrap();
        assert_eq!(count, 0);
        assert!(metrics.calls.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn drain_batch_propagates_reader_error() {
        let metrics = RecordingMetrics::default();
        let mut reader = FakeReader::new(vec![Err(SpineErrorKind::Config("boom".to_string()))]);
        let err = drain_batch(&mut reader, &metrics).await.unwrap_err();
        assert!(matches!(err, SpineError::Config(msg) if msg == "boom"));
    }

    #[tokio::test]
    async fn drain_loop_stops_once_shutdown_resolves() {
        let metrics: Arc<dyn SpineMetrics> = Arc::new(RecordingMetrics::default());
        // Always returns an empty batch -- the loop would otherwise spin
        // forever without ever yielding, since neither branch here ever
        // blocks.
        let reader = FakeReader::new(vec![Ok(vec![])]);
        let (tx, rx) = tokio::sync::oneshot::channel();
        tx.send(()).unwrap();
        let result = tokio::time::timeout(
            std::time::Duration::from_secs(5),
            drain_loop(reader, metrics, rx),
        )
        .await
        .expect("drain_loop must return promptly once shutdown resolves");
        assert!(result.is_ok());
    }

    #[tokio::test]
    async fn drain_loop_propagates_reader_error_before_shutdown() {
        let metrics: Arc<dyn SpineMetrics> = Arc::new(RecordingMetrics::default());
        let reader = FakeReader::new(vec![Err(SpineErrorKind::Config("boom".to_string()))]);
        // Never resolves: the loop must return on the reader error, not by
        // racing a shutdown signal that never fires.
        let (_tx, rx) = tokio::sync::oneshot::channel();
        let result = tokio::time::timeout(
            std::time::Duration::from_secs(5),
            drain_loop(reader, metrics, rx),
        )
        .await
        .expect("drain_loop must return promptly on a reader error");
        assert!(matches!(result, Err(SpineError::Config(msg)) if msg == "boom"));
    }

    /// A syntactically valid [`SpineConfig`] that never actually connects --
    /// `security_transport_tls`/`security_transport_auth` are both disabled
    /// so `SpineConfig::validate()` accepts a plain `redis://` URL with no
    /// credentials, and port `1` (a privileged port with no listener in any
    /// CI/dev sandbox) refuses the TCP connection immediately rather than
    /// timing out, so [`penguin_spine::SpineClient::connect`]'s
    /// retry-with-backoff probe fails fast.
    fn unreachable_spine_config() -> SpineConfig {
        SpineConfig {
            valkey_url: "redis://127.0.0.1:1/".to_string(),
            valkey_username: None,
            valkey_password: None,
            valkey_ca_file: std::path::PathBuf::from("/nonexistent-ca.crt"),
            security_transport_tls: false,
            security_transport_auth: false,
            consumer_id: "test-consumer".to_string(),
            stream_maxlen: 100,
            read_count: 1,
            block_ms: 1_000,
            claim_idle_ms: 30_000,
            claim_interval_ms: 15_000,
            stats_interval_ms: 10_000,
            pel_alert: 5_000,
            dlq_maxlen: 100,
            max_deliveries: 5,
            drain_socket_timeout_s: 65,
            relay_block_timeout_s: 30,
        }
    }

    #[tokio::test]
    async fn run_propagates_a_connect_error_without_ever_reaching_drain_loop() {
        let metrics: Arc<dyn SpineMetrics> = Arc::new(RecordingMetrics::default());
        let (_tx, rx) = tokio::sync::oneshot::channel();
        let result = tokio::time::timeout(
            std::time::Duration::from_secs(30),
            run(
                unreachable_spine_config(),
                "waddles.bot.commands.default".to_string(),
                Vec::new(),
                metrics,
                rx,
            ),
        )
        .await
        .expect("SpineClient::connect must fail fast against a refused connection, not hang");
        assert!(result.is_err(), "connecting to a refused port must error");
    }
}
