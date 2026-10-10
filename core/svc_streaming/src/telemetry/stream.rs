//! Stream data-plane OpenTelemetry instruments: the latency/duration
//! histograms, event counters, and current-state gauges for the
//! ingest -> pipeline -> egress path.
//!
//! Emission is OTLP-only (the primary signal transport, see
//! `rules/critical-rules.md` Observability): every instrument is created
//! from the process-global meter provider that [`crate::telemetry::init`]
//! installs from the standard `OTEL_EXPORTER_OTLP_*` env vars -- nothing here
//! names a destination. When no OTLP endpoint is configured the global
//! provider is the API's no-op, so recording is a cheap no-op and never an
//! error: **no method on [`StreamMetrics`] returns a `Result` or can panic**,
//! because a telemetry failure must never become a pipeline failure.
//!
//! Label discipline: every attribute is a bounded enum value
//! (`protocol`, `egress`, `stage`, `peer`, ...). Stream keys, WHIP tokens,
//! user ids, peer addresses, and per-stream ids never become metric
//! attributes -- they would be both a cardinality explosion and a
//! secret/PII leak.
//!
//! | Instrument | Kind | Meaning |
//! |---|---|---|
//! | `stream_ingest_handoff_seconds` | histogram | one ingest chunk read -> written to ffmpeg stdin (backpressure signal) |
//! | `stream_time_to_first_egress_seconds` | histogram | ingest accepted -> first egress unit playable (HLS segment on disk) |
//! | `stream_fanout_latency_seconds` | histogram | RTP packet published into the SFU fanout -> picked up by a WHEP viewer |
//! | `stream_segment_duration_seconds` | histogram | media duration of each HLS segment (`#EXTINF`) |
//! | `stream_segment_size_bytes` | histogram | size of each completed HLS segment |
//! | `stream_hls_playlist_age_seconds` | histogram | age of the media playlist, sampled every poll |
//! | `stream_stage_duration_seconds` | histogram | per pipeline-setup/teardown stage (see [`Stage`]) |
//! | `stream_external_call_duration_seconds` | histogram | outbound call to a dependency (see [`ExternalPeer`]) |
//! | `stream_relay_session_seconds` | histogram | lifetime of one RTMP/SRT relay target |
//! | `stream_session_duration_seconds` | histogram | lifetime of one pumped ingest session |
//! | `stream_sessions_total` / `stream_session_failures_total` / `stream_ingest_bytes_total` / `stream_segments_total` | counters | events |
//! | `stream_active_sessions` | up-down counter | pumped ingest sessions right now |

use std::future::Future;
use std::sync::{Arc, OnceLock};
use std::time::{Duration, Instant};

use opentelemetry::metrics::{Counter, Histogram, Meter, UpDownCounter};
use opentelemetry::{global, KeyValue};
use tracing::Instrument as _;

use crate::ingest::IngestKind;

/// Instrumentation scope name for every instrument in this module.
pub const METER_NAME: &str = "svc_streaming";

/// Bucket boundaries (seconds) for latency-shaped histograms. The OTel SDK's
/// default boundaries are millisecond-oriented (`0, 5, 10, 25, ...`), which
/// would collapse every sub-5-second observation into two buckets.
const LATENCY_BOUNDARIES_S: &[f64] = &[
    0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0,
];
/// Bucket boundaries (seconds) for HLS segment media durations -- clustered
/// around the 2-6 s targets real packagers use.
const SEGMENT_DURATION_BOUNDARIES_S: &[f64] = &[
    0.25, 0.5, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 15.0, 30.0,
];
/// Bucket boundaries (seconds) for session/relay lifetimes, 1 s .. 1 day.
const LIFETIME_BOUNDARIES_S: &[f64] = &[
    1.0, 5.0, 15.0, 60.0, 300.0, 900.0, 1800.0, 3600.0, 7200.0, 14400.0, 43200.0, 86400.0,
];
/// Bucket boundaries (bytes) for HLS segment sizes, 1 KiB .. 32 MiB.
const SIZE_BOUNDARIES_BYTES: &[f64] = &[
    1024.0,
    16384.0,
    65536.0,
    262_144.0,
    1_048_576.0,
    2_097_152.0,
    4_194_304.0,
    8_388_608.0,
    16_777_216.0,
    33_554_432.0,
];

/// A discrete step of pipeline setup/teardown whose duration is recorded in
/// `stream_stage_duration_seconds{stage}`. A closed enum so the label set
/// stays bounded.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Stage {
    /// Establishing (or fetching the pooled) database connection.
    DbConnect,
    /// Resolving the ingest key to its `streaming_configs` row.
    ConfigLookup,
    /// Resolving the community's owning tenant.
    TenantResolve,
    /// Building the [`crate::pipeline::PipelineSpec`] (targets query + spec).
    SpecBuild,
    /// Registering/starting every egress sink.
    EgressStart,
    /// The supervisor's whole `start` (secret resolution + argv build).
    EngineStart,
    /// The `ffmpeg` process `spawn` syscall.
    FfmpegSpawn,
    /// `ffmpeg` spawn -> its first progress line (encoder is live).
    FfmpegFirstProgress,
    /// Graceful `ffmpeg` stop: `q` -> SIGTERM -> SIGKILL escalation.
    FfmpegStop,
    /// The orchestrator's whole `stop_pipeline` teardown.
    Teardown,
}

impl Stage {
    /// The bounded `stage` attribute value.
    pub const fn as_str(self) -> &'static str {
        match self {
            Stage::DbConnect => "db_connect",
            Stage::ConfigLookup => "config_lookup",
            Stage::TenantResolve => "tenant_resolve",
            Stage::SpecBuild => "spec_build",
            Stage::EgressStart => "egress_start",
            Stage::EngineStart => "engine_start",
            Stage::FfmpegSpawn => "ffmpeg_spawn",
            Stage::FfmpegFirstProgress => "ffmpeg_first_progress",
            Stage::FfmpegStop => "ffmpeg_stop",
            Stage::Teardown => "teardown",
        }
    }

    /// The OTel span name for this stage (`pipeline.<stage>`).
    const fn span_name(self) -> &'static str {
        match self {
            Stage::DbConnect => "pipeline.db_connect",
            Stage::ConfigLookup => "pipeline.config_lookup",
            Stage::TenantResolve => "pipeline.tenant_resolve",
            Stage::SpecBuild => "pipeline.spec_build",
            Stage::EgressStart => "pipeline.egress_start",
            Stage::EngineStart => "pipeline.engine_start",
            Stage::FfmpegSpawn => "pipeline.ffmpeg_spawn",
            Stage::FfmpegFirstProgress => "pipeline.ffmpeg_first_progress",
            Stage::FfmpegStop => "pipeline.ffmpeg_stop",
            Stage::Teardown => "pipeline.teardown",
        }
    }
}

/// The egress path a latency observation belongs to.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Egress {
    /// File-based HLS served from `STREAM_DATA_DIR`.
    Hls,
}

impl Egress {
    /// The bounded `egress` attribute value.
    pub const fn as_str(self) -> &'static str {
        match self {
            Egress::Hls => "hls",
        }
    }
}

/// Media kind of a WebRTC fanout track.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MediaKind {
    Video,
    Audio,
}

impl MediaKind {
    /// The bounded `kind` attribute value.
    pub const fn as_str(self) -> &'static str {
        match self {
            MediaKind::Video => "video",
            MediaKind::Audio => "audio",
        }
    }
}

/// An outbound dependency whose call duration is recorded in
/// `stream_external_call_duration_seconds{peer, outcome}`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExternalPeer {
    /// hub-api's transcode-token ledger.
    TokenLedger,
    /// This service's own loopback `ingest-auth` route (WHIP token check).
    IngestAuth,
    /// The recording object store (S3-compatible).
    ObjectStore,
}

impl ExternalPeer {
    /// The bounded `peer` attribute value.
    pub const fn as_str(self) -> &'static str {
        match self {
            ExternalPeer::TokenLedger => "token_ledger",
            ExternalPeer::IngestAuth => "ingest_auth",
            ExternalPeer::ObjectStore => "object_store",
        }
    }
}

/// How an external call ended: `Ok` when the call completed and returned a
/// response, `Error` when it failed at the transport level (connect,
/// timeout, store error). An HTTP error *status* is still a completed
/// call -- it is timed as `Ok` here and judged by the caller.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CallOutcome {
    Ok,
    Error,
}

impl CallOutcome {
    /// The bounded `outcome` attribute value.
    pub const fn as_str(self) -> &'static str {
        match self {
            CallOutcome::Ok => "ok",
            CallOutcome::Error => "error",
        }
    }
}

impl<T, E> From<&Result<T, E>> for CallOutcome {
    fn from(result: &Result<T, E>) -> Self {
        if result.is_ok() {
            CallOutcome::Ok
        } else {
            CallOutcome::Error
        }
    }
}

/// Why an accepted ingest session never became a running pipeline. A
/// closed enum -- the `reason` attribute stays bounded and carries no
/// error text (which could embed a key or URL).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionFailure {
    NoDatabase,
    ConfigNotFound,
    TenantUnresolved,
    SpecBuildFailed,
    EngineStartFailed,
    WhipSdpMissing,
    StdinUnavailable,
}

impl SessionFailure {
    /// The bounded `reason` attribute value.
    pub const fn as_str(self) -> &'static str {
        match self {
            SessionFailure::NoDatabase => "no_database",
            SessionFailure::ConfigNotFound => "config_not_found",
            SessionFailure::TenantUnresolved => "tenant_unresolved",
            SessionFailure::SpecBuildFailed => "spec_build_failed",
            SessionFailure::EngineStartFailed => "engine_start_failed",
            SessionFailure::WhipSdpMissing => "whip_sdp_missing",
            SessionFailure::StdinUnavailable => "stdin_unavailable",
        }
    }
}

/// The bounded `protocol` attribute value for an [`IngestKind`].
pub const fn protocol_label(kind: IngestKind) -> &'static str {
    match kind {
        IngestKind::Rtmp => "rtmp",
        IngestKind::Srt => "srt",
        IngestKind::Whip => "whip",
    }
}

fn secs(elapsed: Duration) -> f64 {
    elapsed.as_secs_f64()
}

struct Instruments {
    ingest_handoff: Histogram<f64>,
    ingest_bytes: Counter<u64>,
    time_to_first_egress: Histogram<f64>,
    fanout_latency: Histogram<f64>,
    segment_duration: Histogram<f64>,
    segment_size: Histogram<u64>,
    segments: Counter<u64>,
    playlist_age: Histogram<f64>,
    stage_duration: Histogram<f64>,
    external_call: Histogram<f64>,
    relay_session: Histogram<f64>,
    session_duration: Histogram<f64>,
    sessions: Counter<u64>,
    session_failures: Counter<u64>,
    active_sessions: UpDownCounter<i64>,
}

impl Instruments {
    fn build(meter: &Meter) -> Self {
        let seconds_histogram = |name: &'static str, description: &'static str, bounds: &[f64]| {
            meter
                .f64_histogram(name)
                .with_description(description)
                .with_unit("s")
                .with_boundaries(bounds.to_vec())
                .build()
        };
        Self {
            ingest_handoff: seconds_histogram(
                "stream_ingest_handoff_seconds",
                "Time to hand one ingest chunk to the ffmpeg stdin pipe (blocking-pool queueing + write); rises under ffmpeg backpressure",
                LATENCY_BOUNDARIES_S,
            ),
            ingest_bytes: meter
                .u64_counter("stream_ingest_bytes_total")
                .with_description("Ingest bytes pumped into ffmpeg stdin")
                .with_unit("By")
                .build(),
            time_to_first_egress: seconds_histogram(
                "stream_time_to_first_egress_seconds",
                "Time from ingest accepted to the first egress unit (HLS segment) being available",
                LATENCY_BOUNDARIES_S,
            ),
            fanout_latency: seconds_histogram(
                "stream_fanout_latency_seconds",
                "Time an RTP packet spends between being published into the SFU fanout and a WHEP viewer receiving it (sampled)",
                LATENCY_BOUNDARIES_S,
            ),
            segment_duration: seconds_histogram(
                "stream_segment_duration_seconds",
                "Media duration of each completed HLS segment, from its #EXTINF entry",
                SEGMENT_DURATION_BOUNDARIES_S,
            ),
            segment_size: meter
                .u64_histogram("stream_segment_size_bytes")
                .with_description("Size of each completed HLS segment")
                .with_unit("By")
                .with_boundaries(SIZE_BOUNDARIES_BYTES.to_vec())
                .build(),
            segments: meter
                .u64_counter("stream_segments_total")
                .with_description("Completed HLS segments observed")
                .build(),
            playlist_age: seconds_histogram(
                "stream_hls_playlist_age_seconds",
                "Age of the HLS media playlist, sampled every poll interval",
                LATENCY_BOUNDARIES_S,
            ),
            stage_duration: seconds_histogram(
                "stream_stage_duration_seconds",
                "Duration of each pipeline setup/teardown stage",
                LATENCY_BOUNDARIES_S,
            ),
            external_call: seconds_histogram(
                "stream_external_call_duration_seconds",
                "Duration of outbound calls to dependencies, by peer and outcome",
                LATENCY_BOUNDARIES_S,
            ),
            relay_session: seconds_histogram(
                "stream_relay_session_seconds",
                "Lifetime of one RTMP/SRT relay target, recorded when it stops",
                LIFETIME_BOUNDARIES_S,
            ),
            session_duration: seconds_histogram(
                "stream_session_duration_seconds",
                "Lifetime of one pumped (RTMP/SRT) ingest session",
                LIFETIME_BOUNDARIES_S,
            ),
            sessions: meter
                .u64_counter("stream_sessions_total")
                .with_description("Pumped ingest sessions, by lifecycle outcome (started/ended)")
                .build(),
            session_failures: meter
                .u64_counter("stream_session_failures_total")
                .with_description("Accepted ingest sessions that never became a running pipeline, by reason")
                .build(),
            active_sessions: meter
                .i64_up_down_counter("stream_active_sessions")
                .with_description("Pumped (RTMP/SRT) ingest sessions currently live")
                .build(),
        }
    }
}

/// Cheap-to-clone handle to the stream data-plane instruments. See the
/// module docs for the instrument list and the never-fails contract.
#[derive(Clone)]
pub struct StreamMetrics {
    inner: Arc<Instruments>,
}

impl std::fmt::Debug for StreamMetrics {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("StreamMetrics").finish_non_exhaustive()
    }
}

static SHARED: OnceLock<StreamMetrics> = OnceLock::new();

impl StreamMetrics {
    /// Builds instruments from the current process-global meter provider.
    /// Instruments created before [`crate::telemetry::init`] installs the
    /// OTLP provider are permanently no-ops -- construct after init.
    pub fn new() -> Self {
        Self::with_meter(&global::meter(METER_NAME))
    }

    /// Builds instruments from an explicit `meter` -- used by tests that
    /// install their own in-memory provider.
    pub fn with_meter(meter: &Meter) -> Self {
        Self {
            inner: Arc::new(Instruments::build(meter)),
        }
    }

    /// The process-wide handle components default to when no explicit
    /// handle is injected. Initialized on first use from the global meter
    /// provider, so the first call must come after [`crate::telemetry::init`]
    /// (it does: `init` is the first thing `run_with_shutdown` does, and
    /// [`crate::telemetry::init`] itself warms this handle).
    pub fn shared() -> Self {
        SHARED.get_or_init(Self::new).clone()
    }

    /// Records one ingest chunk's handoff to ffmpeg stdin and its size.
    pub fn record_ingest_chunk(&self, kind: IngestKind, handoff: Duration, bytes: u64) {
        let attrs = [KeyValue::new("protocol", protocol_label(kind))];
        self.inner.ingest_handoff.record(secs(handoff), &attrs);
        self.inner.ingest_bytes.add(bytes, &attrs);
    }

    /// Records the time from ingest-accepted to the first egress unit being
    /// available.
    pub fn record_time_to_first_egress(&self, kind: IngestKind, egress: Egress, elapsed: Duration) {
        self.inner.time_to_first_egress.record(
            secs(elapsed),
            &[
                KeyValue::new("protocol", protocol_label(kind)),
                KeyValue::new("egress", egress.as_str()),
            ],
        );
    }

    /// Records one sampled RTP packet's dwell time in the SFU fanout.
    pub fn record_fanout_latency(&self, kind: Option<MediaKind>, elapsed: Duration) {
        self.inner.fanout_latency.record(
            secs(elapsed),
            &[KeyValue::new(
                "kind",
                kind.map(MediaKind::as_str).unwrap_or("unknown"),
            )],
        );
    }

    /// Records one completed HLS segment: its media duration, size, and the
    /// running segment count.
    pub fn record_segment(&self, variant: &'static str, duration_s: f64, size_bytes: u64) {
        let attrs = [KeyValue::new("variant", variant)];
        if duration_s.is_finite() && duration_s >= 0.0 {
            self.inner.segment_duration.record(duration_s, &attrs);
        }
        self.inner.segment_size.record(size_bytes, &attrs);
        self.inner.segments.add(1, &attrs);
    }

    /// Samples the HLS media playlist's age.
    pub fn record_playlist_age(&self, variant: &'static str, age: Duration) {
        self.inner
            .playlist_age
            .record(secs(age), &[KeyValue::new("variant", variant)]);
    }

    /// Records one pipeline setup/teardown stage's duration.
    pub fn record_stage(&self, stage: Stage, elapsed: Duration) {
        self.inner
            .stage_duration
            .record(secs(elapsed), &[KeyValue::new("stage", stage.as_str())]);
    }

    /// Awaits `fut` inside a `pipeline.<stage>` span (child of the current
    /// span, so the stage shows up in the session's trace) and records its
    /// duration in `stream_stage_duration_seconds{stage}`.
    pub async fn time_stage<F: Future>(&self, stage: Stage, fut: F) -> F::Output {
        let span = tracing::info_span!(
            "pipeline_stage",
            stage = stage.as_str(),
            otel.name = stage.span_name()
        );
        let start = Instant::now();
        let out = fut.instrument(span).await;
        self.record_stage(stage, start.elapsed());
        out
    }

    /// Records one outbound call's duration and outcome.
    pub fn record_external_call(
        &self,
        peer: ExternalPeer,
        outcome: CallOutcome,
        elapsed: Duration,
    ) {
        self.inner.external_call.record(
            secs(elapsed),
            &[
                KeyValue::new("peer", peer.as_str()),
                KeyValue::new("outcome", outcome.as_str()),
            ],
        );
    }

    /// Awaits a fallible external call, recording its duration with the
    /// outcome taken from the `Result`.
    pub async fn time_external<F, T, E>(&self, peer: ExternalPeer, fut: F) -> Result<T, E>
    where
        F: Future<Output = Result<T, E>>,
    {
        let start = Instant::now();
        let result = fut.await;
        self.record_external_call(peer, CallOutcome::from(&result), start.elapsed());
        result
    }

    /// Records a relay target's lifetime (`kind` is `"rtmp"` or `"srt"`).
    pub fn record_relay_session(&self, kind: &'static str, elapsed: Duration) {
        self.inner
            .relay_session
            .record(secs(elapsed), &[KeyValue::new("kind", kind)]);
    }

    /// Counts an accepted session that never became a running pipeline.
    pub fn record_session_failure(&self, kind: IngestKind, reason: SessionFailure) {
        self.inner.session_failures.add(
            1,
            &[
                KeyValue::new("protocol", protocol_label(kind)),
                KeyValue::new("reason", reason.as_str()),
            ],
        );
    }

    /// Marks a pumped ingest session live. The returned guard decrements the
    /// active gauge and records the session's lifetime when dropped, so an
    /// early return or panic can never leak a "live" session.
    pub fn session_started(&self, kind: IngestKind) -> SessionGuard {
        let protocol = protocol_label(kind);
        let attrs = [KeyValue::new("protocol", protocol)];
        self.inner.active_sessions.add(1, &attrs);
        self.inner.sessions.add(
            1,
            &[
                KeyValue::new("protocol", protocol),
                KeyValue::new("outcome", "started"),
            ],
        );
        SessionGuard {
            metrics: self.clone(),
            protocol,
            started: Instant::now(),
        }
    }
}

impl Default for StreamMetrics {
    fn default() -> Self {
        Self::new()
    }
}

/// RAII marker for one live pumped ingest session -- see
/// [`StreamMetrics::session_started`].
pub struct SessionGuard {
    metrics: StreamMetrics,
    protocol: &'static str,
    started: Instant,
}

impl Drop for SessionGuard {
    fn drop(&mut self) {
        let inner = &self.metrics.inner;
        inner
            .active_sessions
            .add(-1, &[KeyValue::new("protocol", self.protocol)]);
        inner.session_duration.record(
            secs(self.started.elapsed()),
            &[KeyValue::new("protocol", self.protocol)],
        );
        inner.sessions.add(
            1,
            &[
                KeyValue::new("protocol", self.protocol),
                KeyValue::new("outcome", "ended"),
            ],
        );
    }
}

/// In-memory OTel harness shared by this crate's unit tests (also used by
/// the HLS sink's tests): a real SDK meter provider feeding an
/// `InMemoryMetricExporter`, plus helpers that read data points back.
#[cfg(test)]
pub(crate) mod test_support {
    use super::*;
    use opentelemetry::metrics::MeterProvider as _;
    use opentelemetry_sdk::metrics::data::{AggregatedMetrics, MetricData};
    use opentelemetry_sdk::metrics::{InMemoryMetricExporter, PeriodicReader, SdkMeterProvider};

    pub(crate) fn harness() -> (StreamMetrics, SdkMeterProvider, InMemoryMetricExporter) {
        let exporter = InMemoryMetricExporter::default();
        let provider = SdkMeterProvider::builder()
            .with_reader(PeriodicReader::builder(exporter.clone()).build())
            .build();
        let metrics = StreamMetrics::with_meter(&provider.meter("test"));
        (metrics, provider, exporter)
    }

    /// One exported data point flattened for assertions: histograms fill
    /// `count`/`sum`; sums (counters / up-down counters) put their value in
    /// `sum`.
    struct Point {
        count: u64,
        sum: f64,
        attrs: Vec<(String, String)>,
    }

    fn attrs_of<'a>(kvs: impl Iterator<Item = &'a KeyValue>) -> Vec<(String, String)> {
        kvs.map(|kv| (kv.key.to_string(), kv.value.to_string()))
            .collect()
    }

    /// Flushes `provider` and returns every data point of instrument `name`.
    /// The exporter is reset first so only this flush's (cumulative)
    /// snapshot is read -- summing across flushes would double count.
    fn points(
        provider: &SdkMeterProvider,
        exporter: &InMemoryMetricExporter,
        name: &str,
    ) -> Vec<Point> {
        exporter.reset();
        provider.force_flush().expect("flush");
        let mut out = Vec::new();
        for rm in exporter.get_finished_metrics().expect("metrics") {
            for sm in rm.scope_metrics() {
                for metric in sm.metrics().filter(|m| m.name() == name) {
                    match metric.data() {
                        AggregatedMetrics::F64(MetricData::Histogram(h)) => {
                            out.extend(h.data_points().map(|p| Point {
                                count: p.count(),
                                sum: p.sum(),
                                attrs: attrs_of(p.attributes()),
                            }));
                        }
                        AggregatedMetrics::U64(MetricData::Histogram(h)) => {
                            out.extend(h.data_points().map(|p| Point {
                                count: p.count(),
                                sum: p.sum() as f64,
                                attrs: attrs_of(p.attributes()),
                            }));
                        }
                        AggregatedMetrics::U64(MetricData::Sum(s)) => {
                            out.extend(s.data_points().map(|p| Point {
                                count: 0,
                                sum: p.value() as f64,
                                attrs: attrs_of(p.attributes()),
                            }));
                        }
                        AggregatedMetrics::I64(MetricData::Sum(s)) => {
                            out.extend(s.data_points().map(|p| Point {
                                count: 0,
                                sum: p.value() as f64,
                                attrs: attrs_of(p.attributes()),
                            }));
                        }
                        _ => {}
                    }
                }
            }
        }
        out
    }

    fn matching<'a>(all: &'a [Point], want: &'a [(&str, &str)]) -> impl Iterator<Item = &'a Point> {
        all.iter().filter(move |p| {
            want.iter()
                .all(|(k, v)| p.attrs.iter().any(|(ak, av)| ak == k && av == v))
        })
    }

    /// (count, sum) of histogram `name` points matching every `want` pair.
    pub(crate) fn histogram(
        provider: &SdkMeterProvider,
        exporter: &InMemoryMetricExporter,
        name: &str,
        want: &[(&str, &str)],
    ) -> (u64, f64) {
        let all = points(provider, exporter, name);
        matching(&all, want).fold((0, 0.0), |(c, s), p| (c + p.count, s + p.sum))
    }

    /// Total value of counter `name` points matching every `want` pair.
    pub(crate) fn sum_counter(
        provider: &SdkMeterProvider,
        exporter: &InMemoryMetricExporter,
        name: &str,
        want: &[(&str, &str)],
    ) -> i64 {
        let all = points(provider, exporter, name);
        matching(&all, want).map(|p| p.sum as i64).sum()
    }
}

#[cfg(test)]
mod tests {
    use super::test_support::{harness, histogram, sum_counter};
    use super::*;

    #[test]
    fn label_values_are_a_closed_bounded_vocabulary() {
        let stages = [
            Stage::DbConnect,
            Stage::ConfigLookup,
            Stage::TenantResolve,
            Stage::SpecBuild,
            Stage::EgressStart,
            Stage::EngineStart,
            Stage::FfmpegSpawn,
            Stage::FfmpegFirstProgress,
            Stage::FfmpegStop,
            Stage::Teardown,
        ];
        let mut seen = std::collections::HashSet::new();
        for stage in stages {
            assert!(seen.insert(stage.as_str()), "duplicate {}", stage.as_str());
            assert_eq!(stage.span_name(), format!("pipeline.{}", stage.as_str()));
        }
        assert_eq!(protocol_label(IngestKind::Rtmp), "rtmp");
        assert_eq!(protocol_label(IngestKind::Srt), "srt");
        assert_eq!(protocol_label(IngestKind::Whip), "whip");
        assert_eq!(Egress::Hls.as_str(), "hls");
        assert_eq!(MediaKind::Video.as_str(), "video");
        assert_eq!(MediaKind::Audio.as_str(), "audio");
        assert_eq!(ExternalPeer::TokenLedger.as_str(), "token_ledger");
        assert_eq!(ExternalPeer::IngestAuth.as_str(), "ingest_auth");
        assert_eq!(ExternalPeer::ObjectStore.as_str(), "object_store");
        assert_eq!(CallOutcome::Ok.as_str(), "ok");
        assert_eq!(CallOutcome::Error.as_str(), "error");
        for reason in [
            SessionFailure::NoDatabase,
            SessionFailure::ConfigNotFound,
            SessionFailure::TenantUnresolved,
            SessionFailure::SpecBuildFailed,
            SessionFailure::EngineStartFailed,
            SessionFailure::WhipSdpMissing,
            SessionFailure::StdinUnavailable,
        ] {
            assert!(!reason.as_str().is_empty());
        }
    }

    #[test]
    fn call_outcome_follows_the_result() {
        assert_eq!(CallOutcome::from(&Ok::<(), ()>(())), CallOutcome::Ok);
        assert_eq!(CallOutcome::from(&Err::<(), ()>(())), CallOutcome::Error);
    }

    #[test]
    fn ingest_chunk_records_handoff_histogram_and_byte_counter() {
        let (metrics, provider, exporter) = harness();
        metrics.record_ingest_chunk(IngestKind::Rtmp, Duration::from_millis(5), 1500);
        metrics.record_ingest_chunk(IngestKind::Rtmp, Duration::from_millis(7), 500);
        metrics.record_ingest_chunk(IngestKind::Srt, Duration::from_millis(1), 10);

        let (count, sum) = histogram(
            &provider,
            &exporter,
            "stream_ingest_handoff_seconds",
            &[("protocol", "rtmp")],
        );
        assert_eq!(count, 2);
        assert!((sum - 0.012).abs() < 1e-9, "sum was {sum}");
        assert_eq!(
            sum_counter(
                &provider,
                &exporter,
                "stream_ingest_bytes_total",
                &[("protocol", "rtmp")]
            ),
            2000
        );
        assert_eq!(
            histogram(
                &provider,
                &exporter,
                "stream_ingest_handoff_seconds",
                &[("protocol", "srt")]
            )
            .0,
            1
        );
    }

    #[test]
    fn time_to_first_egress_is_labeled_by_protocol_and_egress() {
        let (metrics, provider, exporter) = harness();
        metrics.record_time_to_first_egress(IngestKind::Rtmp, Egress::Hls, Duration::from_secs(3));
        let (count, sum) = histogram(
            &provider,
            &exporter,
            "stream_time_to_first_egress_seconds",
            &[("protocol", "rtmp"), ("egress", "hls")],
        );
        assert_eq!((count, sum), (1, 3.0));
    }

    #[test]
    fn fanout_latency_labels_unknown_when_the_kind_is_not_set() {
        let (metrics, provider, exporter) = harness();
        metrics.record_fanout_latency(Some(MediaKind::Video), Duration::from_micros(250));
        metrics.record_fanout_latency(None, Duration::from_micros(250));
        for kind in ["video", "unknown"] {
            assert_eq!(
                histogram(
                    &provider,
                    &exporter,
                    "stream_fanout_latency_seconds",
                    &[("kind", kind)]
                )
                .0,
                1,
                "{kind}"
            );
        }
    }

    #[test]
    fn segment_records_duration_size_and_count_but_drops_a_nonsense_duration() {
        let (metrics, provider, exporter) = harness();
        metrics.record_segment("std", 4.0, 100_000);
        metrics.record_segment("std", f64::NAN, 50_000);
        metrics.record_segment("std", -1.0, 50_000);

        let (dur_count, dur_sum) = histogram(
            &provider,
            &exporter,
            "stream_segment_duration_seconds",
            &[("variant", "std")],
        );
        assert_eq!((dur_count, dur_sum), (1, 4.0), "only the valid duration");
        let (size_count, size_sum) = histogram(
            &provider,
            &exporter,
            "stream_segment_size_bytes",
            &[("variant", "std")],
        );
        assert_eq!((size_count, size_sum), (3, 200_000.0));
        assert_eq!(
            sum_counter(
                &provider,
                &exporter,
                "stream_segments_total",
                &[("variant", "std")]
            ),
            3
        );
    }

    #[test]
    fn playlist_age_and_relay_session_record_a_point_each() {
        let (metrics, provider, exporter) = harness();
        metrics.record_playlist_age("ll", Duration::from_millis(1500));
        metrics.record_relay_session("rtmp", Duration::from_secs(90));
        assert_eq!(
            histogram(
                &provider,
                &exporter,
                "stream_hls_playlist_age_seconds",
                &[("variant", "ll")]
            )
            .0,
            1
        );
        assert_eq!(
            histogram(
                &provider,
                &exporter,
                "stream_relay_session_seconds",
                &[("kind", "rtmp")]
            ),
            (1, 90.0)
        );
    }

    #[tokio::test]
    async fn time_stage_returns_the_output_and_records_the_stage() {
        let (metrics, provider, exporter) = harness();
        let out = metrics
            .time_stage(Stage::SpecBuild, async {
                tokio::time::sleep(Duration::from_millis(20)).await;
                42
            })
            .await;
        assert_eq!(out, 42);
        let (count, sum) = histogram(
            &provider,
            &exporter,
            "stream_stage_duration_seconds",
            &[("stage", "spec_build")],
        );
        assert_eq!(count, 1);
        assert!(sum >= 0.02, "the stage ran >= 20ms, recorded {sum}");
    }

    #[tokio::test]
    async fn time_external_records_ok_and_error_outcomes_separately() {
        let (metrics, provider, exporter) = harness();
        let ok: Result<u8, &str> = metrics
            .time_external(ExternalPeer::TokenLedger, async { Ok(1) })
            .await;
        let err: Result<u8, &str> = metrics
            .time_external(ExternalPeer::TokenLedger, async { Err("boom") })
            .await;
        assert_eq!(ok, Ok(1));
        assert_eq!(err, Err("boom"));
        for outcome in ["ok", "error"] {
            assert_eq!(
                histogram(
                    &provider,
                    &exporter,
                    "stream_external_call_duration_seconds",
                    &[("peer", "token_ledger"), ("outcome", outcome)]
                )
                .0,
                1,
                "{outcome}"
            );
        }
    }

    #[test]
    fn session_guard_tracks_active_count_duration_and_lifecycle_counters() {
        let (metrics, provider, exporter) = harness();
        let guard = metrics.session_started(IngestKind::Srt);
        assert_eq!(
            sum_counter(
                &provider,
                &exporter,
                "stream_active_sessions",
                &[("protocol", "srt")]
            ),
            1
        );
        drop(guard);
        assert_eq!(
            sum_counter(
                &provider,
                &exporter,
                "stream_active_sessions",
                &[("protocol", "srt")]
            ),
            0,
            "drop must decrement the active gauge"
        );
        assert_eq!(
            histogram(
                &provider,
                &exporter,
                "stream_session_duration_seconds",
                &[("protocol", "srt")]
            )
            .0,
            1
        );
        for outcome in ["started", "ended"] {
            assert_eq!(
                sum_counter(
                    &provider,
                    &exporter,
                    "stream_sessions_total",
                    &[("protocol", "srt"), ("outcome", outcome)]
                ),
                1,
                "{outcome}"
            );
        }
    }

    #[test]
    fn session_failure_counter_carries_only_the_bounded_reason() {
        let (metrics, provider, exporter) = harness();
        metrics.record_session_failure(IngestKind::Rtmp, SessionFailure::ConfigNotFound);
        assert_eq!(
            sum_counter(
                &provider,
                &exporter,
                "stream_session_failures_total",
                &[("protocol", "rtmp"), ("reason", "config_not_found")]
            ),
            1
        );
    }

    #[test]
    fn recording_against_the_noop_global_provider_never_panics() {
        // No provider installed in this test: `new()` binds the API's no-op
        // meter, and every recording path must be a harmless no-op.
        let metrics = StreamMetrics::new();
        let _guard = metrics.session_started(IngestKind::Whip);
        metrics.record_stage(Stage::Teardown, Duration::from_millis(1));
        metrics.record_segment("std", 1.0, 1);
        assert!(format!("{metrics:?}").contains("StreamMetrics"));
        let _ = StreamMetrics::shared();
        let _ = StreamMetrics::default();
    }
}
