//! HLS egress sink (issue #287 S7): the [`OutputSink`] implementation that
//! supervises `ffmpeg`-written HLS output on disk (directory lifecycle,
//! segment/byte/playlist-age metrics, delayed cleanup on stop), plus the
//! public serving surface for the files it supervises.
//!
//! - [`output`] -- disk layout + the `ffmpeg` argv fragment (spec §4),
//!   usable synchronously by `pipeline::ffmpeg`'s (S3) argv builder before
//!   this sink's async lifecycle even starts.
//! - [`serve`] -- the public `GET /live/...` axum routes that read back
//!   what ffmpeg wrote; see [`serve::hls_router`] for the mount point.

pub mod output;
pub mod serve;

pub use output::HlsOutputTarget;
pub use serve::{
    hls_router, EmptyRunningPipelines, HlsRouterState, RunningPipeline, RunningPipelines,
};

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime};

use prometheus::{GaugeVec, IntCounterVec, Opts};
use tracing::Instrument as _;

use crate::egress::{OutputSink, SinkError};
use crate::ingest::IngestKind;
use crate::pipeline::model::{HlsVariant, OutputSpec, PipelineId};
use crate::telemetry::stream::{Egress, StreamMetrics};

/// When (and over which protocol) a pipeline's ingest was accepted -- the
/// zero point for the `stream_time_to_first_egress_seconds` histogram.
/// Carries both clocks: the wall clock to compare against a segment file's
/// mtime (exact, independent of the poll interval) and a monotonic
/// [`Instant`] as the fallback when a filesystem reports no mtime.
#[derive(Debug, Clone, Copy)]
pub struct IngestOrigin {
    pub protocol: IngestKind,
    pub accepted_at: SystemTime,
    pub accepted_instant: Instant,
}

impl IngestOrigin {
    /// Stamps "ingest accepted" as of right now.
    pub fn now(protocol: IngestKind) -> Self {
        Self {
            protocol,
            accepted_at: SystemTime::now(),
            accepted_instant: Instant::now(),
        }
    }
}

/// Default background poll interval for the segment/byte/playlist-age
/// tracker -- see [`HlsSink::with_intervals`] to override (tests use a much
/// shorter interval to stay fast and deterministic).
const DEFAULT_POLL_INTERVAL: Duration = Duration::from_secs(2);
/// How far *before* the ingest-acceptance stamp a segment's mtime may fall
/// and still count as "published right after acceptance" -- covers the
/// kernel's coarse file-timestamp clock lagging `SystemTime::now()`. See
/// `Poller::record_first_egress`.
const MTIME_SKEW_TOLERANCE: Duration = Duration::from_millis(100);
/// Spec default: how long a stopped pipeline's directory is kept on disk
/// for late viewers before cleanup removes it (spec §1).
pub const DEFAULT_CLEANUP_DELAY: Duration = Duration::from_secs(30);

/// Prometheus metrics owned by [`HlsSink`], registered once against
/// whichever [`prometheus::Registry`] the caller passes to [`HlsSink::new`]
/// (this service's shared registry, see `http::AppState`). Labeled by
/// `variant`/`profile` only -- never `pipeline_id`, an unbounded
/// per-stream cardinality axis -- matching this crate's `mode` label
/// convention (`rules/critical-rules.md` Observability): operators see the
/// active delivery path, not a per-stream label explosion.
#[derive(Clone)]
struct HlsMetrics {
    segments_written_total: IntCounterVec,
    bytes_total: IntCounterVec,
    playlist_age_seconds: GaugeVec,
}

impl HlsMetrics {
    fn register(registry: &prometheus::Registry) -> Self {
        let segments_written_total = IntCounterVec::new(
            Opts::new(
                "hls_segments_written_total",
                "Total HLS media segments observed written to disk, labeled by variant/profile",
            ),
            &["variant", "profile"],
        )
        .expect("valid metric definition");
        registry
            .register(Box::new(segments_written_total.clone()))
            .expect("register hls_segments_written_total");

        let bytes_total = IntCounterVec::new(
            Opts::new(
                "hls_bytes_total",
                "Total bytes of HLS segment data observed written to disk",
            ),
            &["variant", "profile"],
        )
        .expect("valid metric definition");
        registry
            .register(Box::new(bytes_total.clone()))
            .expect("register hls_bytes_total");

        let playlist_age_seconds = GaugeVec::new(
            Opts::new(
                "hls_playlist_age_s",
                "Seconds since the media playlist file was last modified",
            ),
            &["variant", "profile"],
        )
        .expect("valid metric definition");
        registry
            .register(Box::new(playlist_age_seconds.clone()))
            .expect("register hls_playlist_age_s");

        Self {
            segments_written_total,
            bytes_total,
            playlist_age_seconds,
        }
    }
}

/// One active output target's supervision handle.
struct TrackedTarget {
    poll_handle: tokio::task::JoinHandle<()>,
}

/// One pipeline's HLS state: which community it belongs to (set via
/// [`HlsSink::register_pipeline`]) plus every active output target started
/// for it (one per profile/variant pair).
#[derive(Default)]
struct PipelineState {
    community_id: Option<String>,
    origin: Option<IngestOrigin>,
    targets: Vec<TrackedTarget>,
}

/// HLS egress sink -- see module docs. Holds no per-request state beyond
/// its own bookkeeping `Mutex`; safe to share as `Arc<HlsSink>` across
/// however many pipelines the supervisor (S3) runs concurrently.
pub struct HlsSink {
    data_dir: PathBuf,
    poll_interval: Duration,
    cleanup_delay: Duration,
    metrics: HlsMetrics,
    stream_metrics: StreamMetrics,
    state: Mutex<HashMap<PipelineId, PipelineState>>,
}

impl HlsSink {
    /// Builds a sink rooted at `data_dir` (`STREAM_DATA_DIR`), registering
    /// its metrics against `registry`, using the spec-default poll interval
    /// and cleanup delay.
    pub fn new(data_dir: PathBuf, registry: &prometheus::Registry) -> Self {
        Self::with_intervals(
            data_dir,
            registry,
            DEFAULT_POLL_INTERVAL,
            DEFAULT_CLEANUP_DELAY,
        )
    }

    /// Like [`Self::new`], with an overridden cleanup delay (spec default:
    /// 30s after `stop()` before the directory is removed).
    pub fn with_cleanup_delay(
        data_dir: PathBuf,
        registry: &prometheus::Registry,
        cleanup_delay: Duration,
    ) -> Self {
        Self::with_intervals(data_dir, registry, DEFAULT_POLL_INTERVAL, cleanup_delay)
    }

    /// Full constructor: overrides both the segment-tracking poll interval
    /// and the post-stop cleanup delay. Tests use short intervals here to
    /// stay fast and deterministic instead of waiting on real 2s/30s
    /// timers.
    pub fn with_intervals(
        data_dir: PathBuf,
        registry: &prometheus::Registry,
        poll_interval: Duration,
        cleanup_delay: Duration,
    ) -> Self {
        Self {
            data_dir,
            poll_interval,
            cleanup_delay,
            metrics: HlsMetrics::register(registry),
            stream_metrics: StreamMetrics::shared(),
            state: Mutex::new(HashMap::new()),
        }
    }

    /// Replaces the process-wide stream instruments with an explicit handle
    /// -- for tests that install their own meter provider.
    pub fn with_stream_metrics(mut self, stream_metrics: StreamMetrics) -> Self {
        self.stream_metrics = stream_metrics;
        self
    }

    /// Records when `pipeline_id`'s ingest was accepted, so the poller can
    /// emit time-to-first-egress when the first segment lands. Must be called
    /// before [`OutputSink::start`] for that pipeline (the poller captures
    /// the origin when it spawns); without it the HLS sink simply skips that
    /// one histogram. Like [`Self::register_pipeline`], not part of the
    /// frozen [`OutputSink`] trait.
    pub fn mark_ingest_origin(&self, pipeline_id: PipelineId, origin: IngestOrigin) {
        let mut state = lock_state(&self.state);
        state.entry(pipeline_id).or_default().origin = Some(origin);
    }

    /// Associates `pipeline_id` with `community_id` before [`OutputSink::
    /// start`] is called for it.
    ///
    /// Not part of the [`OutputSink`] trait -- `start(pipeline_id, spec)`'s
    /// signature is frozen (`egress::mod`, owned by S1) and never carries
    /// `community_id`, even though the enclosing `PipelineSpec` does
    /// (`pipeline::model`). The pipeline supervisor (S3) is expected to
    /// call this alongside building/dispatching a `PipelineSpec` (which
    /// does carry `community_id`) before invoking `start` for each of its
    /// `OutputSpec::Hls` entries. `start` returns `Err` if this was never
    /// called for a given `pipeline_id` -- a missing association is a
    /// caller bug, surfaced as a `Result`, never a panic.
    pub fn register_pipeline(&self, pipeline_id: PipelineId, community_id: impl Into<String>) {
        let mut state = lock_state(&self.state);
        state.entry(pipeline_id).or_default().community_id = Some(community_id.into());
    }

    fn community_id_for(&self, pipeline_id: PipelineId) -> Option<String> {
        lock_state(&self.state)
            .get(&pipeline_id)
            .and_then(|s| s.community_id.clone())
    }
}

impl OutputSink for HlsSink {
    async fn start(&self, pipeline_id: PipelineId, spec: OutputSpec) -> Result<(), SinkError> {
        let (variant, profile) = match spec {
            OutputSpec::Hls { variant, profile } => (variant, profile),
            other => {
                return Err(SinkError::Other(anyhow::anyhow!(
                    "HlsSink received a non-HLS OutputSpec: {other:?}"
                )));
            }
        };

        if !output::is_safe_path_segment(&profile) {
            return Err(SinkError::Other(anyhow::anyhow!(
                "unsafe HLS profile name: {profile:?}"
            )));
        }

        let community_id = self.community_id_for(pipeline_id).ok_or_else(|| {
            SinkError::Other(anyhow::anyhow!(
                "pipeline {pipeline_id} has no registered community_id -- \
                 call HlsSink::register_pipeline before start"
            ))
        })?;

        let target = HlsOutputTarget::new(&self.data_dir, pipeline_id, &profile, variant);
        create_output_dir(&target.output_dir)
            .await
            .map_err(|err| SinkError::Other(err.into()))?;

        tracing::info!(
            %pipeline_id,
            %community_id,
            profile = %profile,
            ?variant,
            dir = %target.output_dir.display(),
            "HlsSink starting output"
        );

        let origin = lock_state(&self.state)
            .get(&pipeline_id)
            .and_then(|state| state.origin);
        let poller = Poller {
            target,
            profile,
            variant,
            metrics: self.metrics.clone(),
            stream_metrics: self.stream_metrics.clone(),
            origin,
            seen_segments: HashSet::new(),
            announced: HashSet::new(),
            first_egress_recorded: false,
        };
        let poll_handle = spawn_poller(poller, self.poll_interval, pipeline_id);

        let mut state = lock_state(&self.state);
        state
            .entry(pipeline_id)
            .or_default()
            .targets
            .push(TrackedTarget { poll_handle });
        Ok(())
    }

    async fn stop(&self, pipeline_id: PipelineId) -> Result<(), SinkError> {
        let removed = lock_state(&self.state).remove(&pipeline_id);
        let Some(pipeline_state) = removed else {
            // Idempotent per the trait's contract: stopping an unknown or
            // already-stopped pipeline is not an error.
            return Ok(());
        };
        for tracked in &pipeline_state.targets {
            tracked.poll_handle.abort();
        }

        let dir = output::hls_root(&self.data_dir).join(pipeline_id.to_string());
        let delay = self.cleanup_delay;
        tracing::info!(%pipeline_id, dir = %dir.display(), cleanup_delay_s = delay.as_secs(), "HlsSink stopping output, cleanup scheduled");
        tokio::spawn(async move {
            tokio::time::sleep(delay).await;
            match tokio::fs::remove_dir_all(&dir).await {
                Ok(()) => tracing::debug!(dir = %dir.display(), "HLS directory cleaned up"),
                Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
                Err(err) => {
                    tracing::warn!(dir = %dir.display(), error = %err, "HLS cleanup failed to remove directory");
                }
            }
        });

        Ok(())
    }
}

/// Locks `state`, recovering from poisoning instead of panicking -- a panic
/// while handling one pipeline's start/stop must never permanently wedge
/// the sink for every other pipeline it supervises.
fn lock_state(
    state: &Mutex<HashMap<PipelineId, PipelineState>>,
) -> std::sync::MutexGuard<'_, HashMap<PipelineId, PipelineState>> {
    state
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

/// Creates `dir` (and its parents) with mode `0750` on Unix -- the
/// ffmpeg-written segment tree is unreadable to other unprivileged users in
/// the container while still readable/writable by this service's own
/// `appuser`, see `rules/client.md` Rootless Containers.
async fn create_output_dir(dir: &std::path::Path) -> std::io::Result<()> {
    tokio::fs::create_dir_all(dir).await?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        tokio::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o750)).await?;
    }
    Ok(())
}

fn variant_label(variant: HlsVariant) -> &'static str {
    match variant {
        HlsVariant::Ll => "ll",
        HlsVariant::Std => "std",
    }
}

/// Extracts `segment file name -> media duration (seconds)` from an HLS media
/// playlist's `#EXTINF:<duration>,` entries. Only segments the playlist
/// actually lists appear in the result, which is exactly the "this segment
/// is complete and published" signal [`Poller`] needs (ffmpeg writes the
/// segment file first and only then rewrites the playlist). Other tags
/// between an `#EXTINF` and its URI line are skipped; a malformed or
/// negative duration drops that one entry rather than failing the parse.
pub(crate) fn parse_extinf_durations(playlist: &str) -> HashMap<String, f64> {
    let mut durations = HashMap::new();
    let mut pending: Option<f64> = None;
    for line in playlist.lines().map(str::trim) {
        if let Some(rest) = line.strip_prefix("#EXTINF:") {
            pending = rest
                .split(',')
                .next()
                .and_then(|value| value.trim().parse::<f64>().ok())
                .filter(|value| value.is_finite() && *value >= 0.0);
        } else if line.is_empty() || line.starts_with('#') {
            continue;
        } else if let Some(duration) = pending.take() {
            let uri = line.split('?').next().unwrap_or(line);
            let name = uri.rsplit('/').next().unwrap_or(uri);
            durations.insert(name.to_string(), duration);
        }
    }
    durations
}

/// One `*.m4s` file seen in an output directory during a poll.
#[derive(Clone)]
struct SegmentEntry {
    name: String,
    modified: Option<SystemTime>,
    len: u64,
}

/// State of one output target's background poll loop -- see
/// [`spawn_poller`]. Split from the loop so [`Poller::tick`] is directly
/// testable without timers.
struct Poller {
    target: HlsOutputTarget,
    profile: String,
    variant: HlsVariant,
    metrics: HlsMetrics,
    stream_metrics: StreamMetrics,
    origin: Option<IngestOrigin>,
    /// Paths already counted into the Prometheus counters.
    seen_segments: HashSet<PathBuf>,
    /// Segment file names whose OTel segment stats were already recorded;
    /// pruned to what is still on disk so a long stream cannot grow it
    /// without bound as `delete_segments` rolls the window.
    announced: HashSet<String>,
    first_egress_recorded: bool,
}

impl Poller {
    /// One poll: refresh the Prometheus counters, emit OTel stats for newly
    /// completed segments, and sample the playlist age.
    async fn tick(&mut self) {
        let label = variant_label(self.variant);
        let on_disk = self.scan_segments(label).await;
        self.publish_completed_segments(&on_disk, label).await;
        self.sample_playlist_age(label).await;
    }

    /// Scans `output_dir` for segment files and increments the Prometheus
    /// counters by the *delta* since the last poll (counters are monotonic;
    /// a directory that shrinks as `delete_segments` rolls the playlist
    /// window must never decrement them). Returns every segment file found.
    async fn scan_segments(&mut self, label: &'static str) -> Vec<SegmentEntry> {
        let mut on_disk = Vec::new();
        let mut new_segment_count: u64 = 0;
        let mut new_bytes: u64 = 0;
        if let Ok(mut entries) = tokio::fs::read_dir(&self.target.output_dir).await {
            while let Ok(Some(entry)) = entries.next_entry().await {
                let path = entry.path();
                if path.extension().and_then(|e| e.to_str()) != Some("m4s") {
                    continue;
                }
                let is_new = self.seen_segments.insert(path.clone());
                let Ok(meta) = entry.metadata().await else {
                    continue;
                };
                if is_new {
                    new_segment_count += 1;
                    new_bytes += meta.len();
                }
                if let Some(name) = path.file_name().and_then(|n| n.to_str()) {
                    on_disk.push(SegmentEntry {
                        name: name.to_string(),
                        modified: meta.modified().ok(),
                        len: meta.len(),
                    });
                }
            }
        }
        if new_segment_count > 0 {
            self.metrics
                .segments_written_total
                .with_label_values(&[label, &self.profile])
                .inc_by(new_segment_count);
            self.metrics
                .bytes_total
                .with_label_values(&[label, &self.profile])
                .inc_by(new_bytes);
        }
        on_disk
    }

    /// Emits segment duration/size and (once) time-to-first-egress for every
    /// segment that is on disk *and* listed in the media playlist -- i.e.
    /// complete and published. A segment file that is not listed yet (ffmpeg
    /// mid-write) is simply retried next tick, so its size is never sampled
    /// while partial.
    async fn publish_completed_segments(&mut self, on_disk: &[SegmentEntry], label: &'static str) {
        let names: HashSet<&str> = on_disk.iter().map(|entry| entry.name.as_str()).collect();
        self.announced.retain(|name| names.contains(name.as_str()));

        let mut pending: Vec<SegmentEntry> = on_disk
            .iter()
            .filter(|entry| !self.announced.contains(&entry.name))
            .cloned()
            .collect();
        if pending.is_empty() {
            return;
        }
        let Ok(playlist) = tokio::fs::read_to_string(self.target.media_playlist_path()).await
        else {
            return;
        };
        let durations = parse_extinf_durations(&playlist);
        pending.retain(|entry| durations.contains_key(&entry.name));
        // Oldest first, so time-to-first-egress is taken from the earliest
        // published segment even if several appeared between two ticks.
        pending.sort_by_key(|entry| entry.modified);

        for entry in pending {
            let duration_s = durations.get(&entry.name).copied().unwrap_or_default();
            self.stream_metrics
                .record_segment(label, duration_s, entry.len);
            tracing::debug!(
                segment_duration_s = duration_s,
                size_bytes = entry.len,
                "hls segment published"
            );
            if !self.first_egress_recorded {
                self.record_first_egress(&entry);
            }
            self.announced.insert(entry.name);
        }
    }

    /// Records ingest-accepted -> `entry` published, using the segment's
    /// mtime (exact; independent of the poll interval). With no
    /// [`IngestOrigin`] there is nothing to measure and the attempt stops.
    ///
    /// File mtimes come from the kernel's coarse clock, which lags
    /// `SystemTime::now()` by up to a few milliseconds, so a segment
    /// written right after ingest acceptance can carry an mtime a hair
    /// *before* the acceptance stamp: within [`MTIME_SKEW_TOLERANCE`] that
    /// is clamped to zero latency. A segment older than that (a leftover
    /// file from a previous run of the same deterministic pipeline id, or a
    /// real clock step) is skipped and the next segment tried -- never
    /// recorded as a bogus value.
    fn record_first_egress(&mut self, entry: &SegmentEntry) {
        let Some(origin) = self.origin else {
            self.first_egress_recorded = true;
            return;
        };
        let elapsed = match entry.modified {
            Some(modified) => match modified.duration_since(origin.accepted_at) {
                Ok(elapsed) => Some(elapsed),
                Err(early) if early.duration() <= MTIME_SKEW_TOLERANCE => Some(Duration::ZERO),
                Err(_) => None,
            },
            None => Some(origin.accepted_instant.elapsed()),
        };
        match elapsed {
            Some(elapsed) => {
                self.stream_metrics.record_time_to_first_egress(
                    origin.protocol,
                    Egress::Hls,
                    elapsed,
                );
                tracing::debug!(
                    time_to_first_egress_s = elapsed.as_secs_f64(),
                    "first hls segment published"
                );
                self.first_egress_recorded = true;
            }
            None => tracing::debug!(
                "segment predates ingest acceptance (stale file or clock step), not recording time-to-first-egress"
            ),
        }
    }

    /// Refreshes the media playlist's on-disk age gauge (Prometheus) and
    /// histogram (OTel).
    async fn sample_playlist_age(&self, label: &'static str) {
        let age = tokio::fs::metadata(self.target.media_playlist_path())
            .await
            .ok()
            .and_then(|meta| meta.modified().ok())
            .and_then(|modified| SystemTime::now().duration_since(modified).ok());
        if let Some(age) = age {
            self.metrics
                .playlist_age_seconds
                .with_label_values(&[label, &self.profile])
                .set(age.as_secs_f64());
            self.stream_metrics.record_playlist_age(label, age);
        }
    }
}

/// Background task: every `poll_interval`, runs [`Poller::tick`] inside an
/// `egress.hls_poller` span (a child of whatever span called
/// [`HlsSink::start`], so it joins the session's trace). Aborted by
/// [`HlsSink::stop`].
fn spawn_poller(
    mut poller: Poller,
    poll_interval: Duration,
    pipeline_id: PipelineId,
) -> tokio::task::JoinHandle<()> {
    let span = tracing::info_span!(
        "egress.hls_poller",
        %pipeline_id,
        variant = variant_label(poller.variant),
    );
    tokio::spawn(
        async move {
            let mut interval = tokio::time::interval(poll_interval);
            loop {
                interval.tick().await;
                poller.tick().await;
            }
        }
        .instrument(span),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use uuid::Uuid;

    fn temp_data_dir() -> PathBuf {
        std::env::temp_dir().join(format!("svc-streaming-hls-sink-{}", Uuid::new_v4()))
    }

    fn fast_sink(data_dir: PathBuf, registry: &prometheus::Registry) -> HlsSink {
        HlsSink::with_intervals(
            data_dir,
            registry,
            Duration::from_millis(20),
            Duration::from_millis(50),
        )
    }

    #[tokio::test]
    async fn start_without_register_pipeline_errors() {
        let data_dir = temp_data_dir();
        let sink = fast_sink(data_dir.clone(), &prometheus::Registry::new());
        let err = sink
            .start(
                Uuid::new_v4(),
                OutputSpec::Hls {
                    variant: HlsVariant::Std,
                    profile: "1080p60".into(),
                },
            )
            .await
            .unwrap_err();
        assert!(matches!(err, SinkError::Other(_)));
    }

    #[tokio::test]
    async fn start_creates_pipeline_profile_directory() {
        let data_dir = temp_data_dir();
        let sink = fast_sink(data_dir.clone(), &prometheus::Registry::new());
        let pipeline_id = Uuid::new_v4();
        sink.register_pipeline(pipeline_id, "community-1");

        sink.start(
            pipeline_id,
            OutputSpec::Hls {
                variant: HlsVariant::Std,
                profile: "1080p60".into(),
            },
        )
        .await
        .expect("registered pipeline must start");

        let expected_dir = HlsOutputTarget::directory(&data_dir, pipeline_id, "1080p60");
        assert!(tokio::fs::metadata(&expected_dir).await.unwrap().is_dir());

        sink.stop(pipeline_id).await.unwrap();
        tokio::fs::remove_dir_all(&data_dir).await.ok();
    }

    #[tokio::test]
    async fn stop_is_idempotent_for_unknown_pipeline() {
        let sink = fast_sink(temp_data_dir(), &prometheus::Registry::new());
        assert!(sink.stop(Uuid::new_v4()).await.is_ok());
    }

    #[tokio::test]
    async fn stop_keeps_directory_briefly_then_cleans_it_up() {
        let data_dir = temp_data_dir();
        let sink = fast_sink(data_dir.clone(), &prometheus::Registry::new());
        let pipeline_id = Uuid::new_v4();
        sink.register_pipeline(pipeline_id, "community-1");
        sink.start(
            pipeline_id,
            OutputSpec::Hls {
                variant: HlsVariant::Std,
                profile: "1080p60".into(),
            },
        )
        .await
        .unwrap();

        let pipeline_dir = output::hls_root(&data_dir).join(pipeline_id.to_string());
        sink.stop(pipeline_id).await.unwrap();

        // Immediately after stop() the directory (last playlist) is still
        // present for late viewers -- cleanup_delay is 50ms in this test.
        assert!(tokio::fs::metadata(&pipeline_dir).await.is_ok());

        tokio::time::sleep(Duration::from_millis(200)).await;
        assert!(
            tokio::fs::metadata(&pipeline_dir).await.is_err(),
            "directory should be removed after the cleanup delay elapses"
        );
    }

    #[tokio::test]
    async fn metrics_track_new_segments_and_playlist_age() {
        let data_dir = temp_data_dir();
        let registry = prometheus::Registry::new();
        let sink = fast_sink(data_dir.clone(), &registry);
        let pipeline_id = Uuid::new_v4();
        sink.register_pipeline(pipeline_id, "community-1");
        sink.start(
            pipeline_id,
            OutputSpec::Hls {
                variant: HlsVariant::Std,
                profile: "1080p60".into(),
            },
        )
        .await
        .unwrap();

        let dir = HlsOutputTarget::directory(&data_dir, pipeline_id, "1080p60");
        tokio::fs::write(dir.join("index.m3u8"), b"#EXTM3U\n")
            .await
            .unwrap();
        tokio::fs::write(dir.join("segment_00001.m4s"), vec![0u8; 128])
            .await
            .unwrap();

        // Poll interval is 20ms -- give it several ticks to observe the
        // new segment and playlist file.
        tokio::time::sleep(Duration::from_millis(150)).await;

        let rendered = crate::telemetry::render_metrics(&registry).unwrap();
        assert!(rendered.contains("hls_segments_written_total"));
        assert!(rendered.contains("variant=\"std\""));
        assert!(rendered.contains("profile=\"1080p60\""));
        assert!(rendered.contains("hls_bytes_total"));
        assert!(rendered.contains("hls_playlist_age_s"));

        sink.stop(pipeline_id).await.unwrap();
        tokio::time::sleep(Duration::from_millis(100)).await;
        tokio::fs::remove_dir_all(&data_dir).await.ok();
    }

    #[tokio::test]
    async fn non_hls_output_spec_is_rejected() {
        let sink = fast_sink(temp_data_dir(), &prometheus::Registry::new());
        let pipeline_id = Uuid::new_v4();
        sink.register_pipeline(pipeline_id, "community-1");
        let err = sink
            .start(
                pipeline_id,
                OutputSpec::Whep {
                    profile: "1080p60".into(),
                },
            )
            .await
            .unwrap_err();
        assert!(matches!(err, SinkError::Other(_)));
    }

    // --- parse_extinf_durations ---

    #[test]
    fn extinf_parse_maps_segment_names_to_durations() {
        let playlist = "#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-MAP:URI=\"init.mp4\"\n\
            #EXTINF:4.000000,\nsegment_00001.m4s\n\
            #EXTINF:3.500,title\n#EXT-X-PROGRAM-DATE-TIME:2026-10-09T00:00:00Z\nsub/dir/segment_00002.m4s?token=abc\n";
        let durations = parse_extinf_durations(playlist);
        assert_eq!(durations.len(), 2);
        assert_eq!(durations["segment_00001.m4s"], 4.0);
        assert_eq!(
            durations["segment_00002.m4s"], 3.5,
            "path prefix + query stripped, interleaved tags skipped"
        );
    }

    #[test]
    fn extinf_parse_ignores_malformed_negative_and_orphaned_entries() {
        for playlist in [
            "",
            "#EXTM3U\n",
            "#EXTINF:abc,\nseg.m4s\n",
            "#EXTINF:-2.0,\nseg.m4s\n",
            "#EXTINF:NaN,\nseg.m4s\n",
            "seg.m4s\n",
            "#EXTINF:4.0,\n",
        ] {
            assert!(
                parse_extinf_durations(playlist).is_empty(),
                "expected no entries from {playlist:?}"
            );
        }
        // A bad entry drops only itself.
        let mixed = "#EXTINF:oops,\nbad.m4s\n#EXTINF:2.0,\ngood.m4s\n";
        let durations = parse_extinf_durations(mixed);
        assert_eq!(durations.len(), 1);
        assert_eq!(durations["good.m4s"], 2.0);
    }

    // --- Poller::tick (OTel emission) ---

    struct PollerFixture {
        data_dir: PathBuf,
        dir: PathBuf,
        poller: Poller,
        provider: opentelemetry_sdk::metrics::SdkMeterProvider,
        exporter: opentelemetry_sdk::metrics::InMemoryMetricExporter,
    }

    async fn poller_fixture(origin: Option<IngestOrigin>) -> PollerFixture {
        let (stream_metrics, provider, exporter) =
            crate::telemetry::stream::test_support::harness();
        let data_dir = temp_data_dir();
        let target = HlsOutputTarget::new(&data_dir, Uuid::new_v4(), "default", HlsVariant::Std);
        create_output_dir(&target.output_dir).await.unwrap();
        let dir = target.output_dir.clone();
        let poller = Poller {
            target,
            profile: "default".into(),
            variant: HlsVariant::Std,
            metrics: HlsMetrics::register(&prometheus::Registry::new()),
            stream_metrics,
            origin,
            seen_segments: HashSet::new(),
            announced: HashSet::new(),
            first_egress_recorded: false,
        };
        PollerFixture {
            data_dir,
            dir,
            poller,
            provider,
            exporter,
        }
    }

    fn hist(fx: &PollerFixture, name: &str) -> (u64, f64) {
        crate::telemetry::stream::test_support::histogram(&fx.provider, &fx.exporter, name, &[])
    }

    #[tokio::test]
    async fn tick_emits_segment_stats_and_first_egress_once_a_segment_is_listed() {
        let origin = IngestOrigin {
            protocol: IngestKind::Rtmp,
            accepted_at: SystemTime::now() - Duration::from_secs(5),
            accepted_instant: Instant::now(),
        };
        let mut fx = poller_fixture(Some(origin)).await;
        tokio::fs::write(fx.dir.join("segment_00001.m4s"), vec![0u8; 2048])
            .await
            .unwrap();

        // Segment on disk but not yet in the playlist: ffmpeg is mid-write,
        // so nothing may be sampled (its size could still be partial).
        fx.poller.tick().await;
        assert_eq!(hist(&fx, "stream_segment_size_bytes").0, 0);
        assert_eq!(hist(&fx, "stream_time_to_first_egress_seconds").0, 0);

        // Playlist lists it -> complete: duration/size/first-egress emitted.
        tokio::fs::write(
            fx.dir.join("index.m3u8"),
            "#EXTM3U\n#EXTINF:4.000000,\nsegment_00001.m4s\n",
        )
        .await
        .unwrap();
        fx.poller.tick().await;
        assert_eq!(hist(&fx, "stream_segment_duration_seconds"), (1, 4.0));
        assert_eq!(hist(&fx, "stream_segment_size_bytes"), (1, 2048.0));
        let (count, sum) = hist(&fx, "stream_time_to_first_egress_seconds");
        assert_eq!(count, 1);
        assert!(
            (4.5..30.0).contains(&sum),
            "origin was 5s before the segment's mtime, got {sum}"
        );
        assert_eq!(hist(&fx, "stream_hls_playlist_age_seconds").0, 1);

        // Further ticks neither re-announce the segment nor re-record
        // first-egress (the playlist-age histogram samples every tick).
        fx.poller.tick().await;
        assert_eq!(hist(&fx, "stream_segment_duration_seconds").0, 1);
        assert_eq!(hist(&fx, "stream_time_to_first_egress_seconds").0, 1);
        assert_eq!(hist(&fx, "stream_hls_playlist_age_seconds").0, 2);

        tokio::fs::remove_dir_all(&fx.data_dir).await.ok();
    }

    #[tokio::test]
    async fn tick_records_first_egress_from_the_earliest_of_several_new_segments() {
        let origin = IngestOrigin {
            protocol: IngestKind::Srt,
            accepted_at: SystemTime::now() - Duration::from_secs(20),
            accepted_instant: Instant::now(),
        };
        let mut fx = poller_fixture(Some(origin)).await;
        for n in 1..=3 {
            tokio::fs::write(fx.dir.join(format!("segment_{n:05}.m4s")), vec![0u8; 100])
                .await
                .unwrap();
        }
        tokio::fs::write(
            fx.dir.join("index.m3u8"),
            "#EXTM3U\n#EXTINF:2.0,\nsegment_00001.m4s\n#EXTINF:2.0,\nsegment_00002.m4s\n#EXTINF:2.0,\nsegment_00003.m4s\n",
        )
        .await
        .unwrap();
        fx.poller.tick().await;
        assert_eq!(hist(&fx, "stream_segment_duration_seconds"), (3, 6.0));
        assert_eq!(
            hist(&fx, "stream_time_to_first_egress_seconds").0,
            1,
            "first-egress is a once-per-pipeline observation"
        );
        tokio::fs::remove_dir_all(&fx.data_dir).await.ok();
    }

    #[tokio::test]
    async fn tick_skips_first_egress_for_a_segment_older_than_the_ingest() {
        // A leftover segment from a previous run of the same pipeline id.
        let origin = IngestOrigin {
            protocol: IngestKind::Rtmp,
            accepted_at: SystemTime::now() + Duration::from_secs(3600),
            accepted_instant: Instant::now(),
        };
        let mut fx = poller_fixture(Some(origin)).await;
        tokio::fs::write(fx.dir.join("segment_00001.m4s"), vec![0u8; 10])
            .await
            .unwrap();
        tokio::fs::write(
            fx.dir.join("index.m3u8"),
            "#EXTM3U\n#EXTINF:4.0,\nsegment_00001.m4s\n",
        )
        .await
        .unwrap();
        fx.poller.tick().await;
        assert_eq!(hist(&fx, "stream_segment_duration_seconds").0, 1);
        assert_eq!(
            hist(&fx, "stream_time_to_first_egress_seconds").0,
            0,
            "a stale segment must not produce a bogus latency"
        );
        assert!(
            !fx.poller.first_egress_recorded,
            "retry on the next segment"
        );
        tokio::fs::remove_dir_all(&fx.data_dir).await.ok();
    }

    #[tokio::test]
    async fn tick_clamps_a_slightly_early_mtime_to_zero_instead_of_dropping_the_sample() {
        // The kernel's coarse file-timestamp clock lags `SystemTime::now()`,
        // so a segment written right after acceptance can look a few ms
        // *older* than the acceptance stamp. Model it: accepted 20ms "after"
        // the file is written.
        let origin = IngestOrigin {
            protocol: IngestKind::Rtmp,
            accepted_at: SystemTime::now() + Duration::from_millis(20),
            accepted_instant: Instant::now(),
        };
        let mut fx = poller_fixture(Some(origin)).await;
        tokio::fs::write(fx.dir.join("segment_00001.m4s"), vec![0u8; 10])
            .await
            .unwrap();
        tokio::fs::write(
            fx.dir.join("index.m3u8"),
            "#EXTM3U\n#EXTINF:4.0,\nsegment_00001.m4s\n",
        )
        .await
        .unwrap();
        fx.poller.tick().await;
        assert_eq!(
            hist(&fx, "stream_time_to_first_egress_seconds"),
            (1, 0.0),
            "within the skew tolerance the latency is clamped to zero, not dropped"
        );
        assert!(fx.poller.first_egress_recorded);
        tokio::fs::remove_dir_all(&fx.data_dir).await.ok();
    }

    #[tokio::test]
    async fn first_egress_falls_back_to_the_monotonic_clock_when_the_fs_reports_no_mtime() {
        let origin = IngestOrigin {
            protocol: IngestKind::Srt,
            accepted_at: SystemTime::now(),
            accepted_instant: Instant::now()
                .checked_sub(Duration::from_secs(2))
                .expect("monotonic clock has run for at least 2s"),
        };
        let mut fx = poller_fixture(Some(origin)).await;
        fx.poller.record_first_egress(&SegmentEntry {
            name: "segment_00001.m4s".into(),
            modified: None,
            len: 1,
        });
        let (count, sum) = hist(&fx, "stream_time_to_first_egress_seconds");
        assert_eq!(count, 1);
        assert!(sum >= 2.0, "elapsed since the monotonic stamp, got {sum}");
        tokio::fs::remove_dir_all(&fx.data_dir).await.ok();
    }

    #[tokio::test]
    async fn tick_without_an_ingest_origin_still_records_segments_but_no_first_egress() {
        let mut fx = poller_fixture(None).await;
        tokio::fs::write(fx.dir.join("segment_00001.m4s"), vec![0u8; 10])
            .await
            .unwrap();
        tokio::fs::write(
            fx.dir.join("index.m3u8"),
            "#EXTM3U\n#EXTINF:4.0,\nsegment_00001.m4s\n",
        )
        .await
        .unwrap();
        fx.poller.tick().await;
        assert_eq!(hist(&fx, "stream_segment_duration_seconds").0, 1);
        assert_eq!(hist(&fx, "stream_time_to_first_egress_seconds").0, 0);
        assert!(
            fx.poller.first_egress_recorded,
            "nothing to measure, stop trying"
        );
        tokio::fs::remove_dir_all(&fx.data_dir).await.ok();
    }

    #[tokio::test]
    async fn announced_set_is_pruned_to_segments_still_on_disk() {
        let mut fx = poller_fixture(None).await;
        let segment = fx.dir.join("segment_00001.m4s");
        tokio::fs::write(&segment, vec![0u8; 10]).await.unwrap();
        tokio::fs::write(
            fx.dir.join("index.m3u8"),
            "#EXTM3U\n#EXTINF:4.0,\nsegment_00001.m4s\n",
        )
        .await
        .unwrap();
        fx.poller.tick().await;
        assert_eq!(fx.poller.announced.len(), 1);

        // `delete_segments` rolls the window: the file disappears.
        tokio::fs::remove_file(&segment).await.unwrap();
        fx.poller.tick().await;
        assert!(fx.poller.announced.is_empty(), "no unbounded growth");
        tokio::fs::remove_dir_all(&fx.data_dir).await.ok();
    }

    #[tokio::test]
    async fn spawned_poller_runs_ticks_until_the_sink_stops_it() {
        let (stream_metrics, provider, exporter) =
            crate::telemetry::stream::test_support::harness();
        let data_dir = temp_data_dir();
        let sink = fast_sink(data_dir.clone(), &prometheus::Registry::new())
            .with_stream_metrics(stream_metrics);
        let pipeline_id = Uuid::new_v4();
        sink.register_pipeline(pipeline_id, "community-1");
        sink.mark_ingest_origin(pipeline_id, IngestOrigin::now(IngestKind::Rtmp));
        sink.start(
            pipeline_id,
            OutputSpec::Hls {
                variant: HlsVariant::Std,
                profile: "default".into(),
            },
        )
        .await
        .unwrap();

        let dir = HlsOutputTarget::directory(&data_dir, pipeline_id, "default");
        tokio::fs::write(dir.join("segment_00001.m4s"), vec![0u8; 512])
            .await
            .unwrap();
        tokio::fs::write(
            dir.join("index.m3u8"),
            "#EXTM3U\n#EXTINF:4.0,\nsegment_00001.m4s\n",
        )
        .await
        .unwrap();

        // Poll interval is 20ms; give the real spawned task several ticks.
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            let (count, _) = crate::telemetry::stream::test_support::histogram(
                &provider,
                &exporter,
                "stream_time_to_first_egress_seconds",
                &[("protocol", "rtmp"), ("egress", "hls")],
            );
            if count == 1 {
                break;
            }
            assert!(
                Instant::now() < deadline,
                "spawned poller never emitted time-to-first-egress"
            );
            tokio::time::sleep(Duration::from_millis(25)).await;
        }

        sink.stop(pipeline_id).await.unwrap();
        tokio::time::sleep(Duration::from_millis(100)).await;
        tokio::fs::remove_dir_all(&data_dir).await.ok();
    }
}
