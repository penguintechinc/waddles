//! Recording sink.
//!
//! Segments live pipeline output to local disk via ffmpeg's `segment`
//! muxer ([`RecordSink::ffmpeg_output_args`]), then a background watcher
//! task ([`egress::record::watcher`]) uploads each closed segment to
//! S3-compatible object storage (SeaweedFS in this cluster -- see
//! `k8s/helm/waddlebot/templates/svc-streaming.yaml`) via `object_store`,
//! deleting the local copy once the upload succeeds. See
//! `docs/plans/2026-09-11-svc-streaming-pipeline-matrix.md` §4 Record + §8
//! Ports/infra for the argv/ports/infra this implements against.
//!
//! ## `ObjectStoreRef.prefix` doubles as the tenant/community path
//!
//! [`OutputSpec::Record`] (owned by S1/S3's `pipeline::model`, not this
//! chunk) carries `profile` and `target: ObjectStoreRef { store, prefix }`
//! only, and [`OutputSink::start`]'s signature (`egress::mod`, also not
//! owned by this chunk) is `(pipeline_id, spec)` -- no full `PipelineSpec`,
//! so no separate `tenant`/`community_id` field reaches this sink directly.
//! `ObjectStoreRef.prefix` already encodes `"{tenant}/{community_id}"` (see
//! the sample fixture in `pipeline::model`'s own tests:
//! `prefix: "tenant-1/community-1"`), so both the local segment directory
//! and the remote S3 key reuse that prefix rather than requiring a
//! signature change outside this chunk's ownership. `target.store` is kept
//! as free-form metadata (a future multi-store MVP could route on it); this
//! MVP wires exactly one S3-compatible store from `S3_ENDPOINT`/
//! `RECORDINGS_BUCKET`/`S3_ACCESS_KEY`/`S3_SECRET_KEY`.

mod index;
mod metrics;
mod spool;
mod watcher;

pub use index::{RecordingIndex, RecordingSegment};
pub use metrics::{register_metrics, RecordMetrics};
pub use spool::{DfFreeSpaceProbe, FreeSpaceProbe};

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use anyhow::Context as _;
use object_store::aws::AmazonS3Builder;
use object_store::ObjectStore;
use tokio::sync::Mutex as AsyncMutex;
use tokio::task::JoinHandle;

use crate::egress::{OutputSink, SinkError};
use crate::pipeline::model::{ObjectStoreRef, OutputSpec, PipelineId};
use crate::store::{SecretRef, SecretResolver};
use crate::telemetry::stream::StreamMetrics;

const DEFAULT_SEGMENT_SECONDS: u64 = 60;
const DEFAULT_POLL_INTERVAL: Duration = Duration::from_secs(2);
const DEFAULT_MIN_FREE_BYTES: u64 = 1024 * 1024 * 1024; // 1 GiB
const DEFAULT_RETRY_BASE_DELAY: Duration = Duration::from_millis(500);
const DEFAULT_RETRY_MAX_DELAY: Duration = Duration::from_secs(30);
const DEFAULT_MAX_ATTEMPTS_PER_CYCLE: u32 = 3;

struct RunningWatcher {
    stop_flag: Arc<AtomicBool>,
    handle: JoinHandle<()>,
}

/// Recording sink. One instance is shared across every pipeline that
/// records to object storage -- `start`/`stop` key per-pipeline watcher
/// tasks by [`PipelineId`], each with its own directory under
/// `local_root`.
#[derive(Clone)]
pub struct RecordSink {
    local_root: PathBuf,
    store: Arc<dyn ObjectStore>,
    index: RecordingIndex,
    metrics: RecordMetrics,
    stream_metrics: StreamMetrics,
    free_space_probe: Arc<dyn FreeSpaceProbe>,
    segment_seconds: u64,
    poll_interval: Duration,
    min_free_bytes: u64,
    retry_base_delay: Duration,
    retry_max_delay: Duration,
    max_attempts_per_cycle: u32,
    running: Arc<AsyncMutex<HashMap<PipelineId, RunningWatcher>>>,
}

impl RecordSink {
    /// Builds a sink against explicit dependencies. Production defaults
    /// (60s segments, 2s poll, 1 GiB spool floor, `df`-based free space
    /// probe) apply until overridden via the `with_*` builders below --
    /// tests use those builders to shrink the poll interval / free-space
    /// floor / retry backoff to keep test runtime tight.
    pub fn new(
        local_root: impl Into<PathBuf>,
        store: Arc<dyn ObjectStore>,
        metrics: RecordMetrics,
    ) -> Self {
        Self {
            local_root: local_root.into(),
            store,
            index: RecordingIndex::new(),
            metrics,
            stream_metrics: StreamMetrics::shared(),
            free_space_probe: Arc::new(DfFreeSpaceProbe),
            segment_seconds: DEFAULT_SEGMENT_SECONDS,
            poll_interval: DEFAULT_POLL_INTERVAL,
            min_free_bytes: DEFAULT_MIN_FREE_BYTES,
            retry_base_delay: DEFAULT_RETRY_BASE_DELAY,
            retry_max_delay: DEFAULT_RETRY_MAX_DELAY,
            max_attempts_per_cycle: DEFAULT_MAX_ATTEMPTS_PER_CYCLE,
            running: Arc::new(AsyncMutex::new(HashMap::new())),
        }
    }

    /// Builds a sink wired to the S3-compatible store this cluster's Helm
    /// chart provisions (`S3_ENDPOINT`/`RECORDINGS_BUCKET`/`S3_ACCESS_KEY`/
    /// `S3_SECRET_KEY`, see `k8s/helm/waddlebot/templates/svc-streaming.yaml`),
    /// resolving credentials through the S1 [`SecretResolver`]/[`SecretRef`]
    /// pattern rather than reading `std::env` for them inline -- the
    /// resolved values are never logged (`resolver.resolve` returns a
    /// [`crate::config::Secret`], whose `Debug` impl redacts). `metrics`
    /// should come from [`register_metrics`] called against the service's
    /// shared Prometheus registry (see `telemetry::register_request_metrics`
    /// for the equivalent pattern) so `/metrics` actually exposes these
    /// series -- this chunk doesn't own that wiring.
    pub fn from_env(
        resolver: &dyn SecretResolver,
        stream_data_dir: &Path,
        metrics: RecordMetrics,
    ) -> anyhow::Result<Self> {
        let endpoint = std::env::var("S3_ENDPOINT").context("S3_ENDPOINT is not set")?;
        let bucket = std::env::var("RECORDINGS_BUCKET").context("RECORDINGS_BUCKET is not set")?;
        let access_key = resolver
            .resolve(&SecretRef::Env {
                var: "S3_ACCESS_KEY".to_string(),
            })
            .context("resolving S3_ACCESS_KEY")?;
        let secret_key = resolver
            .resolve(&SecretRef::Env {
                var: "S3_SECRET_KEY".to_string(),
            })
            .context("resolving S3_SECRET_KEY")?;
        let allow_http = endpoint.starts_with("http://");

        let s3 = AmazonS3Builder::new()
            .with_endpoint(&endpoint)
            .with_bucket_name(&bucket)
            .with_access_key_id(access_key.expose())
            .with_secret_access_key(secret_key.expose())
            .with_allow_http(allow_http)
            .build()
            .context("building S3 client for recordings")?;

        Ok(Self::new(
            stream_data_dir.join("rec"),
            Arc::new(s3),
            metrics,
        ))
    }

    /// Replaces the process-wide stream instruments with an explicit handle
    /// -- for tests that install their own meter provider.
    #[must_use]
    pub fn with_stream_metrics(mut self, stream_metrics: StreamMetrics) -> Self {
        self.stream_metrics = stream_metrics;
        self
    }

    /// Overrides the free-space probe (defaults to [`DfFreeSpaceProbe`]).
    #[must_use]
    pub fn with_free_space_probe(mut self, probe: Arc<dyn FreeSpaceProbe>) -> Self {
        self.free_space_probe = probe;
        self
    }

    /// Overrides the watcher's directory poll interval (default 2s).
    #[must_use]
    pub fn with_poll_interval(mut self, interval: Duration) -> Self {
        self.poll_interval = interval;
        self
    }

    /// Overrides the minimum free space (bytes) below which a pipeline's
    /// watcher stops recording (default 1 GiB).
    #[must_use]
    pub fn with_min_free_bytes(mut self, bytes: u64) -> Self {
        self.min_free_bytes = bytes;
        self
    }

    /// Overrides the per-attempt upload retry backoff (default 500ms base,
    /// 30s cap, exponential).
    #[must_use]
    pub fn with_retry_backoff(mut self, base: Duration, max: Duration) -> Self {
        self.retry_base_delay = base;
        self.retry_max_delay = max;
        self
    }

    /// Overrides how many upload attempts a single poll cycle makes for one
    /// segment before deferring the retry to the next cycle (default 3;
    /// clamped to at least 1).
    #[must_use]
    pub fn with_max_attempts_per_cycle(mut self, attempts: u32) -> Self {
        self.max_attempts_per_cycle = attempts.max(1);
        self
    }

    /// The shared [`RecordingIndex`] this sink's watcher tasks populate.
    /// `Clone`-cheap; a later chunk (S2) can hold its own clone to serve a
    /// `GET /api/v1/.../recordings` route without depending on
    /// `RecordSink` itself.
    pub fn index(&self) -> RecordingIndex {
        self.index.clone()
    }

    fn segment_dir(&self, pipeline_id: PipelineId, target: &ObjectStoreRef) -> PathBuf {
        self.local_root
            .join(&target.prefix)
            .join(pipeline_id.to_string())
    }

    /// Builds the `ffmpeg` argv fragment for this pipeline's `Record`
    /// output -- exposed so the pipeline supervisor (S3) can splice it into
    /// the full command line, the same way S7's HLS sink exposes its own
    /// argv fragment. Per
    /// `docs/plans/2026-09-11-svc-streaming-pipeline-matrix.md` §4:
    /// `-f segment -segment_time 60 -reset_timestamps 1 -strftime 1 <path>`.
    pub fn ffmpeg_output_args(
        &self,
        pipeline_id: PipelineId,
        target: &ObjectStoreRef,
    ) -> Vec<String> {
        let pattern = self
            .segment_dir(pipeline_id, target)
            .join("%Y%m%d%H%M%S.ts");
        vec![
            "-f".to_string(),
            "segment".to_string(),
            "-segment_time".to_string(),
            self.segment_seconds.to_string(),
            "-reset_timestamps".to_string(),
            "1".to_string(),
            "-strftime".to_string(),
            "1".to_string(),
            pattern.to_string_lossy().into_owned(),
        ]
    }
}

/// Splits an `ObjectStoreRef.prefix` of the form `"{tenant}/{community_id}"`
/// into its two segments for indexing/logging. A prefix without a `/` is
/// treated as community-only (empty tenant) rather than erroring -- this
/// sink doesn't validate `PipelineSpec` shape, that's `pipeline::model`'s
/// and the API layer's job.
fn split_tenant_community(prefix: &str) -> (&str, &str) {
    match prefix.rsplit_once('/') {
        Some((tenant, community_id)) => (tenant, community_id),
        None => ("", prefix),
    }
}

impl OutputSink for RecordSink {
    async fn start(&self, pipeline_id: PipelineId, spec: OutputSpec) -> Result<(), SinkError> {
        let (profile, target) = match spec {
            OutputSpec::Record { profile, target } => (profile, target),
            other => {
                return Err(SinkError::Other(anyhow::anyhow!(
                    "RecordSink::start received a non-Record OutputSpec: {other:?}"
                )))
            }
        };

        let (tenant, community_id) = split_tenant_community(&target.prefix);
        let dir = self.segment_dir(pipeline_id, &target);
        tokio::fs::create_dir_all(&dir)
            .await
            .context("creating recording segment directory")?;

        let stop_flag = Arc::new(AtomicBool::new(false));
        let ctx = watcher::WatcherContext {
            dir,
            pipeline_id,
            tenant: tenant.to_string(),
            community_id: community_id.to_string(),
            profile: profile.clone(),
            key_prefix: format!("{}/{pipeline_id}", target.prefix),
            store: self.store.clone(),
            index: self.index.clone(),
            metrics: self.metrics.clone(),
            stream_metrics: self.stream_metrics.clone(),
            free_space_probe: self.free_space_probe.clone(),
            local_root: self.local_root.clone(),
            min_free_bytes: self.min_free_bytes,
            poll_interval: self.poll_interval,
            retry_base_delay: self.retry_base_delay,
            retry_max_delay: self.retry_max_delay,
            max_attempts_per_cycle: self.max_attempts_per_cycle,
        };

        let handle = tokio::spawn(watcher::run(ctx, stop_flag.clone()));

        let mut running = self.running.lock().await;
        if let Some(previous) = running.insert(pipeline_id, RunningWatcher { stop_flag, handle }) {
            // A watcher was already running for this pipeline_id -- stop it
            // (this makes a repeated `start` idempotent-ish -- restart, not
            // duplicate) so we don't leak the old task.
            previous.stop_flag.store(true, Ordering::SeqCst);
            previous.handle.abort();
            tracing::warn!(%pipeline_id, "RecordSink::start replaced an already-running watcher for this pipeline");
        }
        tracing::info!(%pipeline_id, profile = %profile, "recording started");
        Ok(())
    }

    async fn stop(&self, pipeline_id: PipelineId) -> Result<(), SinkError> {
        let running = {
            let mut guard = self.running.lock().await;
            guard.remove(&pipeline_id)
        };
        let Some(running) = running else {
            // Idempotent: stopping an unknown/already-stopped pipeline is
            // not an error, matching `PipelineEngine::stop`'s contract.
            return Ok(());
        };
        running.stop_flag.store(true, Ordering::SeqCst);
        running
            .handle
            .await
            .context("recording watcher task panicked")?;
        tracing::info!(%pipeline_id, "recording stopped");
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use object_store::memory::InMemory;
    use uuid::Uuid;

    fn test_sink(local_root: &Path) -> RecordSink {
        let metrics = register_metrics(&prometheus::Registry::new());
        RecordSink::new(local_root.to_path_buf(), Arc::new(InMemory::new()), metrics)
    }

    fn sample_target() -> ObjectStoreRef {
        ObjectStoreRef {
            store: "s3-recordings".into(),
            prefix: "tenant-1/community-1".into(),
        }
    }

    /// A fresh, uniquely-named scratch directory under the OS temp root --
    /// unique per call (not per process) so parallel `cargo test` threads
    /// never collide, unlike a `std::process::id()`-keyed path shared by
    /// every test in this module.
    fn scratch_dir() -> PathBuf {
        std::env::temp_dir().join(format!("svc-streaming-record-test-{}", Uuid::new_v4()))
    }

    // `from_env` reads process-global env vars; serialize the two tests
    // below against each other the same way `config.rs`/`store/secrets.rs`
    // serialize their own env-mutating tests. No other test in this crate
    // touches these four var names.
    static ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    fn clear_s3_env() {
        for var in [
            "S3_ENDPOINT",
            "RECORDINGS_BUCKET",
            "S3_ACCESS_KEY",
            "S3_SECRET_KEY",
        ] {
            // SAFETY: serialized by ENV_LOCK.
            unsafe { std::env::remove_var(var) };
        }
    }

    #[test]
    fn split_tenant_community_splits_on_last_slash() {
        assert_eq!(
            split_tenant_community("tenant-1/community-1"),
            ("tenant-1", "community-1")
        );
    }

    #[test]
    fn split_tenant_community_treats_missing_slash_as_community_only() {
        assert_eq!(
            split_tenant_community("community-only"),
            ("", "community-only")
        );
    }

    #[test]
    fn ffmpeg_output_args_matches_the_segment_muxer_recipe() {
        let dir = scratch_dir();
        let sink = test_sink(&dir);
        let pipeline_id = Uuid::nil();
        let target = sample_target();

        let args = sink.ffmpeg_output_args(pipeline_id, &target);

        let actual: Vec<&str> = args[..8].iter().map(String::as_str).collect();
        assert_eq!(
            actual,
            vec![
                "-f",
                "segment",
                "-segment_time",
                "60",
                "-reset_timestamps",
                "1",
                "-strftime",
                "1",
            ]
        );
        let pattern = &args[8];
        assert!(
            pattern.ends_with("%Y%m%d%H%M%S.ts"),
            "pattern was {pattern}"
        );
        assert!(
            pattern.contains(&format!("tenant-1/community-1/{pipeline_id}")),
            "pattern was {pattern}"
        );
    }

    #[tokio::test]
    async fn start_rejects_a_non_record_output_spec() {
        let dir = scratch_dir();
        let sink = test_sink(&dir);
        let err = sink
            .start(
                Uuid::nil(),
                OutputSpec::Whep {
                    profile: "1080p60".into(),
                },
            )
            .await
            .unwrap_err();
        assert!(matches!(err, SinkError::Other(_)));
    }

    #[tokio::test]
    async fn stop_on_a_pipeline_that_never_started_is_a_no_op() {
        let dir = scratch_dir();
        let sink = test_sink(&dir);
        sink.stop(Uuid::new_v4())
            .await
            .expect("idempotent stop must not error");
    }

    #[tokio::test]
    async fn start_then_stop_creates_and_tears_down_the_watcher() {
        let dir = scratch_dir();
        let sink = test_sink(&dir).with_poll_interval(Duration::from_millis(10));
        let pipeline_id = Uuid::new_v4();

        sink.start(
            pipeline_id,
            OutputSpec::Record {
                profile: "1080p60".into(),
                target: sample_target(),
            },
        )
        .await
        .expect("start must succeed");

        let expected_dir = dir
            .join("tenant-1/community-1")
            .join(pipeline_id.to_string());
        assert!(expected_dir.is_dir());

        sink.stop(pipeline_id).await.expect("stop must succeed");
        tokio::fs::remove_dir_all(&dir).await.ok();
    }

    #[test]
    fn from_env_builds_a_sink_from_the_documented_env_vars() {
        let _guard = ENV_LOCK
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        clear_s3_env();
        // SAFETY: serialized by ENV_LOCK above. `AmazonS3Builder::build()`
        // only constructs the client (credential provider + HTTP client) --
        // it never makes a network call, so a syntactically valid but
        // unreachable endpoint is sufficient here.
        unsafe {
            std::env::set_var("S3_ENDPOINT", "http://127.0.0.1:1");
            std::env::set_var("RECORDINGS_BUCKET", "test-recordings");
            std::env::set_var("S3_ACCESS_KEY", "test-access-key");
            std::env::set_var("S3_SECRET_KEY", "test-secret-key");
        }

        let dir = scratch_dir();
        let metrics = register_metrics(&prometheus::Registry::new());
        let result = RecordSink::from_env(&crate::store::DefaultSecretResolver, &dir, metrics);

        clear_s3_env();
        assert!(
            result.is_ok(),
            "from_env must succeed with all four vars set: {:?}",
            result.err()
        );
    }

    #[test]
    fn from_env_fails_with_a_clear_error_when_a_required_var_is_missing() {
        let _guard = ENV_LOCK
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        clear_s3_env();

        let dir = scratch_dir();
        let metrics = register_metrics(&prometheus::Registry::new());
        // `RecordSink` doesn't derive `Debug` (it holds `Arc<dyn
        // FreeSpaceProbe>`, which this crate's trait doesn't require to be
        // `Debug`), so `.expect_err`/`.unwrap_err` aren't usable here --
        // match instead.
        let err = match RecordSink::from_env(&crate::store::DefaultSecretResolver, &dir, metrics) {
            Ok(_) => panic!("missing S3_ENDPOINT must fail, not silently default"),
            Err(err) => err,
        };
        assert!(
            err.to_string().contains("S3_ENDPOINT"),
            "unexpected error: {err}"
        );
    }
}
