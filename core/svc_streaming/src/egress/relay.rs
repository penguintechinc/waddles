//! RTMP/SRT relay (stream-forwarding) sink -- implements [`OutputSink`]
//! for [`crate::pipeline::model::OutputSpec::RtmpPush`]/`SrtPush` targets:
//! resolves each target's `url_secret_ref` through the S1
//! [`crate::store::SecretResolver`] mechanism, validates it, tracks
//! per-target health from ffmpeg `-f tee` stderr, and exposes the ffmpeg
//! output-argument fragments a later chunk's `FfmpegRunner`/pipeline
//! supervisor (S3) assembles into the real command line.
//!
//! Naming note: never call this feature "restream"/"Restream" in code,
//! docs, or UI copy -- that is a competitor's trademarked product name.
//!
//! **Secret handling.** A relay target's resolved URL
//! (`rtmp://host/app/<streamkey>` or `srt://host:port?streamid=...`) is a
//! full credential and is never logged raw -- every accessor that returns
//! it says `_unredacted` in its name (see `target` submodule); tracing
//! output and `relay_bytes_total`'s `target` label use
//! [`target::ResolvedRelayTarget::url_redacted`] only.

pub mod error;
pub mod health;
pub mod metrics;
pub mod policy;
pub mod target;

use std::collections::HashMap;
use std::time::Instant;

use tokio::sync::RwLock;

use crate::egress::{OutputSink, SinkError};
use crate::pipeline::model::{OutputSpec, PipelineId};
use crate::store::{DefaultSecretResolver, SecretResolver};
use crate::telemetry::stream::StreamMetrics;

pub use error::RelayError;
pub use health::{FailureReasonKind, TargetHealth};
pub use metrics::{register_relay_metrics, RelayMetrics};
pub use policy::{RelayPolicy, RelayPolicyError};
pub use target::{RelayTargetKind, RelayTargetSpec, ResolvedRelayTarget, TeeSlave};

/// Per-pipeline relay state: the resolved targets (in start order -- the
/// order `Output #<N>` indices in ffmpeg stderr refer to) and a parallel
/// health vector.
#[derive(Default)]
struct PipelineRelayState {
    targets: Vec<ResolvedRelayTarget>,
    health: Vec<TargetHealth>,
    /// When each target (parallel to `targets`) started -- the zero point
    /// for `stream_relay_session_seconds`, recorded when it stops.
    started: Vec<Instant>,
}

/// RTMP/SRT relay sink. Generic over [`SecretResolver`] so tests can
/// inject a fake resolver; production code uses
/// [`RelaySink::new`]/[`RelaySink::with_policy`] (both backed by
/// [`DefaultSecretResolver`]).
pub struct RelaySink<R: SecretResolver = DefaultSecretResolver> {
    resolver: R,
    policy: RelayPolicy,
    metrics: Option<RelayMetrics>,
    stream_metrics: StreamMetrics,
    state: RwLock<HashMap<PipelineId, PipelineRelayState>>,
}

impl RelaySink<DefaultSecretResolver> {
    /// A relay sink using the default (env/file) secret resolver and the
    /// Free-tier [`RelayPolicy`].
    pub fn new() -> Self {
        Self::with_resolver_and_policy(DefaultSecretResolver, RelayPolicy::default())
    }

    /// A relay sink using the default secret resolver and an explicit
    /// [`RelayPolicy`] (e.g. [`RelayPolicy::PROFESSIONAL`]).
    pub fn with_policy(policy: RelayPolicy) -> Self {
        Self::with_resolver_and_policy(DefaultSecretResolver, policy)
    }
}

impl Default for RelaySink<DefaultSecretResolver> {
    fn default() -> Self {
        Self::new()
    }
}

impl<R: SecretResolver> RelaySink<R> {
    /// A relay sink using a caller-supplied [`SecretResolver`] and the
    /// Free-tier [`RelayPolicy`] -- primarily for tests.
    pub fn with_resolver(resolver: R) -> Self {
        Self::with_resolver_and_policy(resolver, RelayPolicy::default())
    }

    /// Full constructor: explicit resolver and policy.
    pub fn with_resolver_and_policy(resolver: R, policy: RelayPolicy) -> Self {
        Self {
            resolver,
            policy,
            metrics: None,
            stream_metrics: StreamMetrics::shared(),
            state: RwLock::new(HashMap::new()),
        }
    }

    /// Replaces the process-wide stream instruments with an explicit handle
    /// -- for tests that install their own meter provider.
    pub fn with_stream_metrics(mut self, stream_metrics: StreamMetrics) -> Self {
        self.stream_metrics = stream_metrics;
        self
    }

    /// Attaches Prometheus metric handles (from [`register_relay_metrics`])
    /// to this sink. Without this, the sink still functions -- metrics
    /// recording is simply skipped.
    pub fn with_metrics(mut self, metrics: RelayMetrics) -> Self {
        self.metrics = Some(metrics);
        self
    }

    /// Returns the ffmpeg `-f tee` slave list for every currently-active
    /// target of `pipeline_id`, in start order. Empty if the pipeline has
    /// no active relay targets. Use this whenever a pipeline has more than
    /// one relay destination sharing a profile; for exactly one target,
    /// [`Self::ffmpeg_output_args`] returns the simpler direct-output form.
    pub async fn tee_slaves(&self, pipeline_id: PipelineId) -> Vec<TeeSlave> {
        let state = self.state.read().await;
        state
            .get(&pipeline_id)
            .map(|entry| target::tee_slaves(&entry.targets))
            .unwrap_or_default()
    }

    /// Returns the ffmpeg `-f <mux> <url>` output-argument fragments for
    /// `pipeline_id`'s single active relay target. Errors with
    /// [`RelayError::NoActiveTargets`] if there are none, or
    /// [`RelayError::MultipleTargets`] if there is more than one -- callers
    /// with multiple targets must use [`Self::tee_slaves`] and build a
    /// `-f tee` output instead.
    pub async fn ffmpeg_output_args(
        &self,
        pipeline_id: PipelineId,
    ) -> Result<Vec<String>, RelayError> {
        let state = self.state.read().await;
        let entry = state
            .get(&pipeline_id)
            .ok_or(RelayError::NoActiveTargets(pipeline_id))?;
        match entry.targets.as_slice() {
            [] => Err(RelayError::NoActiveTargets(pipeline_id)),
            [only] => Ok(only.ffmpeg_output_args_unredacted()),
            multiple => Err(RelayError::MultipleTargets(pipeline_id, multiple.len())),
        }
    }

    /// Current [`TargetHealth`] for every target of `pipeline_id`, in
    /// start order. Empty if the pipeline isn't tracked.
    pub async fn target_health(&self, pipeline_id: PipelineId) -> Vec<TargetHealth> {
        let state = self.state.read().await;
        state
            .get(&pipeline_id)
            .map(|e| e.health.clone())
            .unwrap_or_default()
    }

    /// Classifies one line of ffmpeg stderr for `pipeline_id` and, if it
    /// matches a known failure pattern, marks the corresponding target
    /// [`TargetHealth::Failing`] and records
    /// `relay_target_failures_total{reason}`. ffmpeg's own diagnostic
    /// output can embed the resolved secret URL (e.g. `Output #1 ... to
    /// 'srt://host?streamid=...'`); this scrubs any known raw target URL
    /// out of the line before it reaches `tracing`, so `line` itself is
    /// never forwarded unredacted into logs.
    pub async fn observe_stderr_line(&self, pipeline_id: PipelineId, line: &str) {
        let mut state = self.state.write().await;
        let Some(entry) = state.get_mut(&pipeline_id) else {
            return;
        };

        let Some(reason) = health::classify_stderr_line(line) else {
            return;
        };

        let safe_line = redact_line(line, &entry.targets);
        let index = health::extract_output_index(line).filter(|&i| i < entry.health.len());

        if let Some(index) = index {
            entry.health[index] = TargetHealth::Failing(reason);
            tracing::warn!(
                %pipeline_id,
                target = %entry.targets[index].url_redacted,
                reason = %reason,
                line = %safe_line,
                "relay target failing"
            );
        } else {
            tracing::warn!(%pipeline_id, reason = %reason, line = %safe_line, "relay target failing (target index unknown)");
        }

        let active = entry
            .health
            .iter()
            .filter(|h| matches!(h, TargetHealth::Active))
            .count();

        if let Some(metrics) = &self.metrics {
            metrics
                .relay_target_failures_total
                .with_label_values(&[reason.metric_label()])
                .inc();
            metrics
                .relay_targets_active
                .with_label_values(&[&pipeline_id.to_string()])
                .set(active as i64);
        }
    }

    /// Records `bytes` forwarded to `pipeline_id`'s target at
    /// `target_index` (its position in start/`tee_slaves` order) against
    /// `relay_bytes_total`. A no-op if metrics aren't attached, the
    /// pipeline isn't tracked, or `target_index` is out of range --
    /// callers (the ffmpeg progress-line parser, owned by a later chunk)
    /// derive this best-effort from ffmpeg's `-progress` output.
    pub async fn record_bytes(&self, pipeline_id: PipelineId, target_index: usize, bytes: u64) {
        let Some(metrics) = &self.metrics else {
            return;
        };
        let state = self.state.read().await;
        let Some(entry) = state.get(&pipeline_id) else {
            return;
        };
        let Some(target) = entry.targets.get(target_index) else {
            return;
        };
        metrics
            .relay_bytes_total
            .with_label_values(&[&target.url_redacted])
            .inc_by(bytes);
    }

    /// Resolves, validates, and appends a single relay destination for
    /// `pipeline_id`, enforcing [`RelayPolicy::check_destination_count`]
    /// against the resulting total. Shared by [`OutputSink::start`] and
    /// tests so both exercise the same path.
    async fn add_target(
        &self,
        pipeline_id: PipelineId,
        spec: &OutputSpec,
    ) -> Result<(), RelayError> {
        let target_spec =
            RelayTargetSpec::from_output_spec(spec).ok_or(RelayError::UnsupportedOutputSpec)?;
        let raw = self.resolver.resolve(&target_spec.url_secret_ref)?;
        let resolved = ResolvedRelayTarget::from_raw(target_spec.kind, raw)?;

        let mut state = self.state.write().await;
        let entry = state.entry(pipeline_id).or_default();
        self.policy
            .check_destination_count(entry.targets.len() + 1)?;

        tracing::info!(
            %pipeline_id,
            target = %resolved.url_redacted,
            kind = ?resolved.kind,
            "relay target starting"
        );

        entry.targets.push(resolved);
        entry.health.push(TargetHealth::Active);
        entry.started.push(Instant::now());
        let active = entry.targets.len();
        drop(state);

        if let Some(metrics) = &self.metrics {
            metrics
                .relay_targets_active
                .with_label_values(&[&pipeline_id.to_string()])
                .set(active as i64);
        }
        Ok(())
    }
}

/// Replaces any target's raw resolved URL that appears verbatim in `line`
/// with its redacted form, so a defensive copy of ffmpeg's own stderr
/// output is safe to pass to `tracing`.
fn redact_line(line: &str, targets: &[ResolvedRelayTarget]) -> String {
    let mut safe = line.to_string();
    for target in targets {
        let raw = target.raw_url_unredacted();
        if !raw.is_empty() && safe.contains(raw) {
            safe = safe.replace(raw, &target.url_redacted);
        }
    }
    safe
}

impl<R: SecretResolver> OutputSink for RelaySink<R> {
    async fn start(&self, pipeline_id: PipelineId, spec: OutputSpec) -> Result<(), SinkError> {
        self.add_target(pipeline_id, &spec)
            .await
            .map_err(SinkError::from)
    }

    async fn stop(&self, pipeline_id: PipelineId) -> Result<(), SinkError> {
        let mut state = self.state.write().await;
        let removed = state.remove(&pipeline_id);
        drop(state);

        // One lifetime sample per relay target (RTMP/SRT push), labeled by
        // protocol only -- never the redacted URL, which is per-destination.
        if let Some(removed) = removed {
            for (target, started) in removed.targets.iter().zip(&removed.started) {
                let kind = match target.kind {
                    RelayTargetKind::Rtmp => "rtmp",
                    RelayTargetKind::Srt => "srt",
                };
                self.stream_metrics
                    .record_relay_session(kind, started.elapsed());
            }
        }

        if let Some(metrics) = &self.metrics {
            let _ = metrics
                .relay_targets_active
                .remove_label_values(&[&pipeline_id.to_string()]);
        }
        tracing::debug!(%pipeline_id, "relay pipeline stopped");
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::{SecretError, SecretRef};
    use std::collections::HashMap as StdHashMap;
    use std::sync::Mutex;
    use uuid::Uuid;

    /// A fake [`SecretResolver`] backed by an in-memory map -- avoids
    /// mutating real process env vars across parallel tests.
    #[derive(Default)]
    struct FakeResolver(Mutex<StdHashMap<String, String>>);

    impl FakeResolver {
        fn with(pairs: &[(&str, &str)]) -> Self {
            let map = pairs
                .iter()
                .map(|(k, v)| (k.to_string(), v.to_string()))
                .collect();
            Self(Mutex::new(map))
        }
    }

    impl SecretResolver for FakeResolver {
        fn resolve(&self, secret_ref: &SecretRef) -> Result<crate::config::Secret, SecretError> {
            let key = match secret_ref {
                SecretRef::Env { var } => var.clone(),
                SecretRef::File { path } => path.clone(),
            };
            self.0
                .lock()
                .unwrap()
                .get(&key)
                .cloned()
                .map(crate::config::Secret::new)
                .ok_or(SecretError::MissingEnv(key))
        }
    }

    fn rtmp_spec(var: &str) -> OutputSpec {
        OutputSpec::RtmpPush {
            url_secret_ref: SecretRef::Env {
                var: var.to_string(),
            },
        }
    }

    fn srt_spec(var: &str) -> OutputSpec {
        OutputSpec::SrtPush {
            url_secret_ref: SecretRef::Env {
                var: var.to_string(),
            },
        }
    }

    #[tokio::test]
    async fn start_resolves_and_activates_a_single_rtmp_target() {
        let resolver = FakeResolver::with(&[("RELAY_URL", "rtmp://ingest.example.com/app/sk_abc")]);
        let sink = RelaySink::with_resolver(resolver);
        let pipeline_id = Uuid::new_v4();

        sink.start(pipeline_id, rtmp_spec("RELAY_URL"))
            .await
            .expect("starts");

        let args = sink
            .ffmpeg_output_args(pipeline_id)
            .await
            .expect("single target");
        assert_eq!(
            args,
            vec![
                "-f".to_string(),
                "flv".to_string(),
                "rtmp://ingest.example.com/app/sk_abc".to_string(),
            ]
        );
        assert_eq!(
            sink.target_health(pipeline_id).await,
            vec![TargetHealth::Active]
        );
    }

    #[tokio::test]
    async fn start_resolves_a_single_srt_target() {
        let resolver = FakeResolver::with(&[(
            "RELAY_SRT_URL",
            "srt://ingest.example.com:9000?streamid=sk_abc&latency=120",
        )]);
        let sink = RelaySink::with_resolver(resolver);
        let pipeline_id = Uuid::new_v4();

        sink.start(pipeline_id, srt_spec("RELAY_SRT_URL"))
            .await
            .expect("starts");

        let args = sink
            .ffmpeg_output_args(pipeline_id)
            .await
            .expect("single target");
        assert_eq!(args[0], "-f");
        assert_eq!(args[1], "mpegts");
        assert_eq!(
            args[2],
            "srt://ingest.example.com:9000?streamid=sk_abc&latency=120"
        );
    }

    #[tokio::test]
    async fn ffmpeg_output_args_errors_with_no_active_targets() {
        let sink: RelaySink = RelaySink::new();
        let err = sink.ffmpeg_output_args(Uuid::new_v4()).await.unwrap_err();
        assert!(matches!(err, RelayError::NoActiveTargets(_)));
    }

    #[tokio::test]
    async fn multiple_targets_group_into_tee_slaves_in_start_order() {
        let resolver = FakeResolver::with(&[
            ("A", "rtmp://a.example.com/app/k1"),
            ("B", "srt://b.example.com:9000?streamid=k2"),
        ]);
        let sink = RelaySink::with_resolver(resolver);
        let pipeline_id = Uuid::new_v4();

        sink.start(pipeline_id, rtmp_spec("A"))
            .await
            .expect("starts A");
        sink.start(pipeline_id, srt_spec("B"))
            .await
            .expect("starts B");

        let err = sink.ffmpeg_output_args(pipeline_id).await.unwrap_err();
        assert!(matches!(err, RelayError::MultipleTargets(_, 2)));

        let slaves = sink.tee_slaves(pipeline_id).await;
        assert_eq!(slaves.len(), 2);
        assert_eq!(
            slaves[0].tee_fragment_unredacted(),
            "[f=flv:onfail=ignore]rtmp://a.example.com/app/k1"
        );
        assert_eq!(
            slaves[1].tee_fragment_unredacted(),
            "[f=mpegts:onfail=ignore]srt://b.example.com:9000?streamid=k2"
        );
        // Never leak the raw secret through Display/Debug.
        assert!(!format!("{}", slaves[0]).contains("k1"));
        assert!(!format!("{:?}", slaves[1]).contains("k2"));
    }

    #[tokio::test]
    async fn a_fourth_destination_is_rejected_by_free_tier_policy() {
        let resolver = FakeResolver::with(&[
            ("A", "rtmp://a.example.com/app/k1"),
            ("B", "rtmp://b.example.com/app/k2"),
            ("C", "rtmp://c.example.com/app/k3"),
            ("D", "rtmp://d.example.com/app/k4"),
        ]);
        let sink = RelaySink::with_resolver(resolver);
        let pipeline_id = Uuid::new_v4();

        sink.start(pipeline_id, rtmp_spec("A")).await.unwrap();
        sink.start(pipeline_id, rtmp_spec("B")).await.unwrap();
        sink.start(pipeline_id, rtmp_spec("C")).await.unwrap();

        let err = sink.start(pipeline_id, rtmp_spec("D")).await.unwrap_err();
        match err {
            SinkError::Other(inner) => {
                assert_eq!(
                    inner.to_string(),
                    "relay limit: max 3 destinations on this tier"
                );
            }
            other => panic!("expected SinkError::Other, got {other:?}"),
        }
        // The rejected 4th target must not have been added.
        assert_eq!(sink.tee_slaves(pipeline_id).await.len(), 3);
    }

    #[tokio::test]
    async fn professional_policy_allows_a_fourth_destination() {
        let resolver = FakeResolver::with(&[
            ("A", "rtmp://a.example.com/app/k1"),
            ("B", "rtmp://b.example.com/app/k2"),
            ("C", "rtmp://c.example.com/app/k3"),
            ("D", "rtmp://d.example.com/app/k4"),
        ]);
        let sink = RelaySink::with_resolver_and_policy(resolver, RelayPolicy::PROFESSIONAL);
        let pipeline_id = Uuid::new_v4();

        for var in ["A", "B", "C", "D"] {
            sink.start(pipeline_id, rtmp_spec(var)).await.unwrap();
        }
        assert_eq!(sink.tee_slaves(pipeline_id).await.len(), 4);
    }

    #[tokio::test]
    async fn start_errors_when_the_secret_is_missing() {
        let sink: RelaySink<_> = RelaySink::with_resolver(FakeResolver::default());
        let err = sink
            .start(Uuid::new_v4(), rtmp_spec("MISSING"))
            .await
            .unwrap_err();
        assert!(matches!(err, SinkError::Other(_)));
    }

    #[tokio::test]
    async fn start_errors_on_scheme_mismatch() {
        let resolver = FakeResolver::with(&[("BAD", "http://ingest.example.com/app/sk_abc")]);
        let sink = RelaySink::with_resolver(resolver);
        let err = sink
            .start(Uuid::new_v4(), rtmp_spec("BAD"))
            .await
            .unwrap_err();
        assert!(matches!(err, SinkError::Other(_)));
    }

    #[tokio::test]
    async fn stop_removes_pipeline_state_and_is_idempotent() {
        let resolver = FakeResolver::with(&[("A", "rtmp://a.example.com/app/k1")]);
        let sink = RelaySink::with_resolver(resolver);
        let pipeline_id = Uuid::new_v4();

        sink.start(pipeline_id, rtmp_spec("A")).await.unwrap();
        sink.stop(pipeline_id).await.expect("stop succeeds");
        assert!(sink.tee_slaves(pipeline_id).await.is_empty());

        // Idempotent: stopping an already-stopped/unknown pipeline is Ok.
        sink.stop(pipeline_id).await.expect("stop is idempotent");
    }

    #[tokio::test]
    async fn observe_stderr_line_marks_the_indexed_target_failing_and_increments_metrics() {
        let registry = prometheus::Registry::new();
        let metrics = register_relay_metrics(&registry).unwrap();
        let resolver = FakeResolver::with(&[
            ("A", "rtmp://a.example.com/app/k1"),
            ("B", "srt://b.example.com:9000?streamid=k2"),
        ]);
        let sink = RelaySink::with_resolver(resolver).with_metrics(metrics);
        let pipeline_id = Uuid::new_v4();

        sink.start(pipeline_id, rtmp_spec("A")).await.unwrap();
        sink.start(pipeline_id, srt_spec("B")).await.unwrap();

        sink.observe_stderr_line(
            pipeline_id,
            "[tee @ 0x1] Output #1, mpegts, to 'srt://b.example.com:9000?streamid=k2': Connection refused",
        )
        .await;

        let health = sink.target_health(pipeline_id).await;
        assert_eq!(health[0], TargetHealth::Active);
        assert_eq!(
            health[1],
            TargetHealth::Failing(FailureReasonKind::ConnectionRefused)
        );
    }

    #[tokio::test]
    async fn observe_stderr_line_never_forwards_a_raw_secret_into_the_health_state() {
        // Regression guard: the classifier/redactor operate on the line
        // text, not the stored target -- assert the target's own
        // url_redacted stays redacted after processing a line that
        // embedded the raw secret.
        let resolver = FakeResolver::with(&[("A", "rtmp://a.example.com/app/k1")]);
        let sink = RelaySink::with_resolver(resolver);
        let pipeline_id = Uuid::new_v4();
        sink.start(pipeline_id, rtmp_spec("A")).await.unwrap();

        sink.observe_stderr_line(
            pipeline_id,
            "Output #0, flv, to 'rtmp://a.example.com/app/k1': Connection refused",
        )
        .await;

        let slaves = sink.tee_slaves(pipeline_id).await;
        assert!(!format!("{:?}", slaves[0]).contains("k1"));
    }

    #[tokio::test]
    async fn observe_stderr_line_ignores_unknown_pipelines() {
        let sink: RelaySink = RelaySink::new();
        // No panic, no state created for an untracked pipeline.
        sink.observe_stderr_line(Uuid::new_v4(), "Connection refused")
            .await;
    }

    #[tokio::test]
    async fn observe_stderr_line_ignores_non_failure_progress_lines() {
        let resolver = FakeResolver::with(&[("A", "rtmp://a.example.com/app/k1")]);
        let sink = RelaySink::with_resolver(resolver);
        let pipeline_id = Uuid::new_v4();
        sink.start(pipeline_id, rtmp_spec("A")).await.unwrap();

        sink.observe_stderr_line(
            pipeline_id,
            "frame=  120 fps= 30 q=-1.0 size=  512kB time=00:00:04.00",
        )
        .await;

        assert_eq!(
            sink.target_health(pipeline_id).await,
            vec![TargetHealth::Active]
        );
    }

    #[tokio::test]
    async fn record_bytes_increments_the_labeled_counter() {
        let registry = prometheus::Registry::new();
        let metrics = register_relay_metrics(&registry).unwrap();
        let resolver = FakeResolver::with(&[("A", "rtmp://a.example.com/app/k1")]);
        let sink = RelaySink::with_resolver(resolver).with_metrics(metrics.clone());
        let pipeline_id = Uuid::new_v4();
        sink.start(pipeline_id, rtmp_spec("A")).await.unwrap();

        sink.record_bytes(pipeline_id, 0, 2048).await;

        let value = metrics
            .relay_bytes_total
            .with_label_values(&["rtmp://a.example.com/app/****"])
            .get();
        assert_eq!(value, 2048);
    }

    #[tokio::test]
    async fn record_bytes_is_a_no_op_without_metrics_attached() {
        let resolver = FakeResolver::with(&[("A", "rtmp://a.example.com/app/k1")]);
        let sink = RelaySink::with_resolver(resolver);
        let pipeline_id = Uuid::new_v4();
        sink.start(pipeline_id, rtmp_spec("A")).await.unwrap();
        // No metrics attached -- must not panic.
        sink.record_bytes(pipeline_id, 0, 2048).await;
    }

    #[tokio::test]
    async fn start_rejects_non_relay_output_specs() {
        let sink: RelaySink = RelaySink::new();
        let spec = OutputSpec::Whep {
            profile: "1080p60".into(),
        };
        let err = sink.start(Uuid::new_v4(), spec).await.unwrap_err();
        assert!(matches!(err, SinkError::Other(_)));
    }
}
