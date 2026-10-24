//! Telemetry bootstrap: delegates `tracing` + sanitizing OTel logs/traces/
//! metrics entirely to `penguin-logging` (spec S4.9), keeping only this
//! service's own Prometheus request metrics (`RequestMetrics`) local, since
//! those are svc-ingest-specific counters, not part of the shared crate's
//! surface.
//!
//! `penguin-logging` reads the exact same standard OTLP environment
//! variables this module used to read by hand
//! (`OTEL_EXPORTER_OTLP_ENDPOINT`/`_PROTOCOL`/`_HEADERS`,
//! `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`, plus `LOG_LEVEL`) --
//! see `rules/critical-rules.md` Observability (OTel) -- and preserves the
//! same graceful-degradation contract: an unset `OTEL_EXPORTER_OTLP_ENDPOINT`
//! skips OTLP export entirely (stdout JSON + Prometheus `/metrics` still
//! work), and a failed exporter build downgrades rather than panicking or
//! propagating. Closes the "no Rust penguin logging crate exists yet" gap
//! recorded in `rules/backend-rust.md`.
//!
//! Mirrors `core/svc_process/src/telemetry.rs` (established there, M4) --
//! once wired, a service should never hand-roll `tracing-subscriber`/OTel
//! plumbing again.

use std::time::Duration;

use crate::twitch_sessions::manager::{ReconcileReport, SessionEnd};
use crate::twitch_sessions::metrics::{
    DiscoveryOutcome, LeaseOutcome, SessionMetrics, RESOLVE_CLASSES, SKIP_REASONS,
};

/// Allow-list for the `event` label of Twitch session-event metrics.
const SESSION_EVENTS: [&str; 5] = [
    "started",
    "stopped",
    "restarted",
    "lease_denied",
    "lease_lost",
];

/// Re-exported so callers (`crate::lib::run_with_shutdown`) hold the guard
/// type without needing to depend on `penguin_logging` directly for it.
pub use penguin_logging::TelemetryGuard;

/// Initializes structured logging + OTel logs/traces/metrics via
/// `penguin_logging::init`, and returns the guard plus a fresh Prometheus
/// [`prometheus::Registry`] for this service's own `/metrics` HTTP surface.
/// Must be called exactly once, before any other `tracing` macro use.
///
/// `penguin_logging::init` also returns a `LevelHandle` for runtime
/// log-level changes; this service doesn't yet expose a control-plane
/// endpoint to use it, so it is intentionally dropped here rather than
/// plumbed through unused.
pub fn init(default_service_name: &str) -> (TelemetryGuard, prometheus::Registry) {
    let cfg = penguin_logging::ServiceConfig::from_env(default_service_name);
    let (guard, _level_handle, registry) = penguin_logging::init(cfg);
    (guard, registry)
}

/// Renders the Prometheus text-format exposition body for `/metrics`.
pub fn render_metrics(registry: &prometheus::Registry) -> anyhow::Result<String> {
    use prometheus::Encoder;
    let metric_families = registry.gather();
    let mut buf = Vec::new();
    prometheus::TextEncoder::new().encode(&metric_families, &mut buf)?;
    Ok(String::from_utf8(buf)?)
}

/// Base HTTP request metrics registered once against the Prometheus
/// registry and shared via [`crate::http::AppState`] so the request-path
/// middleware can record into them without re-registering (a
/// `prometheus::Registry` panics on duplicate registration). Histograms
/// for load/latency come first per `rules/critical-rules.md`
/// Observability -- a lone request counter is not instrumentation.
#[derive(Clone)]
pub struct RequestMetrics {
    pub http_requests_total: prometheus::IntCounterVec,
    pub http_request_duration_seconds: prometheus::HistogramVec,
}

/// Registers the service's base Prometheus metrics (an `up` gauge plus a
/// per-request counter and latency histogram, both labeled by
/// method/path/status where applicable) against `registry` and returns
/// handles for request-path code to record into. Must be called exactly
/// once per `registry` -- see [`crate::http::AppState::new`].
pub fn register_request_metrics(registry: &prometheus::Registry) -> RequestMetrics {
    let up = prometheus::IntGauge::new("svc_ingest_up", "1 if the process is running")
        .expect("valid metric definition");
    registry
        .register(Box::new(up.clone()))
        .expect("register svc_ingest_up");
    up.set(1);

    let http_requests_total = prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_ingest_http_requests_total",
            "Total HTTP requests handled, labeled by method/path/status",
        ),
        &["method", "path", "status"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(http_requests_total.clone()))
        .expect("register svc_ingest_http_requests_total");

    let http_request_duration_seconds = prometheus::HistogramVec::new(
        prometheus::HistogramOpts::new(
            "svc_ingest_http_request_duration_seconds",
            "HTTP request duration in seconds, labeled by method/path",
        ),
        &["method", "path"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(http_request_duration_seconds.clone()))
        .expect("register svc_ingest_http_request_duration_seconds");

    RequestMetrics {
        http_requests_total,
        http_request_duration_seconds,
    }
}

/// Ingest-specific Prometheus metrics: platform events published onto the
/// spine and platform receiver connection state, labeled by platform so an
/// operator can see per-source health at a glance -- see `src/ingest/`.
#[derive(Clone)]
pub struct IngestMetrics {
    pub events_published_total: prometheus::IntCounterVec,
    pub publish_errors_total: prometheus::IntCounterVec,
    pub receiver_reconnects_total: prometheus::IntCounterVec,
    /// Twitch EventSub webhook verification outcomes, labeled by result
    /// (`ok`/`bad_signature`/`replay_rejected`/`secret_not_found`/
    /// `missing_header`/`bad_content_type`/`oversized_body`) --
    /// `crate::ingest::twitch_eventsub::handle_webhook`.
    pub eventsub_verifications_total: prometheus::IntCounterVec,
    /// Twitch EventSub message-ids rejected as duplicates by the dedup
    /// guard.
    pub eventsub_dedup_hits_total: prometheus::IntCounter,
    /// Twitch EventSub webhook handler latency, labeled by outcome -- the
    /// fast-path histogram `rules/critical-rules.md` Observability requires
    /// (load/latency histograms first, not just a counter).
    pub eventsub_request_duration_seconds: prometheus::HistogramVec,
    pub receiver_connection_healthy: prometheus::IntGaugeVec,
    /// Total spine (Valkey) connect attempts made by `crate::lib::
    /// connect_spine_with_retry`, labeled by receiver -- every retry
    /// increments this, so a flat line here means a receiver is stuck
    /// retrying forever (or never started). Regression: one-shot valkey
    /// probe disabled discord receiver (alpha 2026-10-02).
    pub spine_connect_attempts_total: prometheus::IntCounterVec,
    /// 1 while a receiver's spine connection is up and its loop is
    /// running, 0 while it's down/retrying -- labeled by receiver, backs
    /// `crate::http::health::readyz`'s per-receiver readiness report.
    pub consumer_loop_running: prometheus::IntGaugeVec,
    /// Dataplane scope-resolver outcomes, labeled by platform/outcome
    /// (`hit`/`resolved`/`not_found`/`ambiguous`/`invalid`/`inconsistent`/
    /// `error`/`no_source`) -- every non-`hit`/`resolved` outcome means the
    /// event was DROPPED (never defaulted to a tenant), so a rising
    /// miss rate is the operator's signal that guilds/channels are unlinked.
    pub scope_resolutions_total: prometheus::IntCounterVec,
    /// Live per-community Twitch IRC sessions on this replica.
    pub twitch_sessions_live: prometheus::IntGauge,
    /// Twitch reconcile passes, labeled by outcome (`ok`/`discovery_failed`).
    pub twitch_reconcile_total: prometheus::IntCounterVec,
    /// Twitch reconcile pass duration in seconds.
    pub twitch_reconcile_duration_seconds: prometheus::Histogram,
    /// Twitch session lifecycle events, labeled by a fixed event set.
    pub twitch_session_events_total: prometheus::IntCounterVec,
    /// Twitch resolve failures, labeled by a fixed class set.
    pub twitch_resolve_errors_total: prometheus::IntCounterVec,
    /// Bindings skipped by the latest Twitch discovery pass, labeled by a
    /// fixed reason set (a gauge: per-pass snapshots must not accumulate).
    pub twitch_discovery_skipped: prometheus::IntGaugeVec,
}

/// Registers this service's ingest-path metrics against `registry`. Must be
/// called exactly once per `registry` -- see [`crate::http::AppState::new`].
pub fn register_ingest_metrics(registry: &prometheus::Registry) -> IngestMetrics {
    let events_published_total = prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_ingest_events_published_total",
            "Total normalized platform events XADDed onto the spine, labeled by platform/source",
        ),
        &["platform", "source_id"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(events_published_total.clone()))
        .expect("register svc_ingest_events_published_total");

    let publish_errors_total = prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_ingest_publish_errors_total",
            "Total publish_event failures, labeled by platform/reason",
        ),
        &["platform", "reason"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(publish_errors_total.clone()))
        .expect("register svc_ingest_publish_errors_total");

    let receiver_reconnects_total = prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_ingest_receiver_reconnects_total",
            "Total platform receiver reconnect attempts, labeled by platform and triggering reason \
             (e.g. resumable_close/reconnect_fresh_close/session_invalidated/other)",
        ),
        &["platform", "reason"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(receiver_reconnects_total.clone()))
        .expect("register svc_ingest_receiver_reconnects_total");

    let eventsub_verifications_total = prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_ingest_eventsub_verifications_total",
            "Total Twitch EventSub webhook verification attempts, labeled by outcome",
        ),
        &["outcome"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(eventsub_verifications_total.clone()))
        .expect("register svc_ingest_eventsub_verifications_total");

    let eventsub_dedup_hits_total = prometheus::IntCounter::new(
        "svc_ingest_eventsub_dedup_hits_total",
        "Total Twitch EventSub deliveries rejected as duplicate message-ids",
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(eventsub_dedup_hits_total.clone()))
        .expect("register svc_ingest_eventsub_dedup_hits_total");

    let eventsub_request_duration_seconds = prometheus::HistogramVec::new(
        prometheus::HistogramOpts::new(
            "svc_ingest_eventsub_request_duration_seconds",
            "Twitch EventSub webhook handler latency in seconds, labeled by outcome",
        ),
        &["outcome"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(eventsub_request_duration_seconds.clone()))
        .expect("register svc_ingest_eventsub_request_duration_seconds");

    // Starts at 1 (healthy) for every platform the first time it's touched
    // via `with_label_values` -- there is no fixed set of platform labels
    // to pre-populate at registration time (unlike `up`), so this gauge
    // only exists in the exposition once a receiver loop has run at least
    // once. See `ReceiverHealthMetrics::receiver_marked_unhealthy`.
    let receiver_connection_healthy = prometheus::IntGaugeVec::new(
        prometheus::Opts::new(
            "svc_ingest_receiver_connection_healthy",
            "1 if the platform receiver's connection is healthy, 0 once a fatal \
             (non-retryable) close has stopped that connection's reconnect loop -- \
             the service process itself keeps running either way",
        ),
        &["platform"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(receiver_connection_healthy.clone()))
        .expect("register svc_ingest_receiver_connection_healthy");

    let spine_connect_attempts_total = prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_ingest_spine_connect_attempts_total",
            "Total spine (Valkey) connect attempts made by a receiver's connect-retry loop, \
             labeled by receiver",
        ),
        &["receiver"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(spine_connect_attempts_total.clone()))
        .expect("register svc_ingest_spine_connect_attempts_total");

    let consumer_loop_running = prometheus::IntGaugeVec::new(
        prometheus::Opts::new(
            "svc_ingest_consumer_loop_running",
            "1 while a receiver's spine connection is up and its loop is running, 0 while \
             down/retrying, labeled by receiver",
        ),
        &["receiver"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(consumer_loop_running.clone()))
        .expect("register svc_ingest_consumer_loop_running");

    let scope_resolutions_total = prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_ingest_scope_resolutions_total",
            "Dataplane scope-resolver outcomes, labeled by platform/outcome \
             (non-hit/resolved outcomes: the event was dropped, never defaulted)",
        ),
        &["platform", "outcome"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(scope_resolutions_total.clone()))
        .expect("register svc_ingest_scope_resolutions_total");

    let twitch_sessions_live = prometheus::IntGauge::new(
        "svc_ingest_twitch_sessions_live",
        "Live per-community Twitch IRC sessions on this replica",
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(twitch_sessions_live.clone()))
        .expect("register svc_ingest_twitch_sessions_live");

    let twitch_reconcile_total = prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_ingest_twitch_reconcile_total",
            "Twitch session reconcile passes, labeled by outcome (ok/discovery_failed)",
        ),
        &["outcome"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(twitch_reconcile_total.clone()))
        .expect("register svc_ingest_twitch_reconcile_total");

    let twitch_reconcile_duration_seconds =
        prometheus::Histogram::with_opts(prometheus::HistogramOpts::new(
            "svc_ingest_twitch_reconcile_duration_seconds",
            "Twitch session reconcile pass duration in seconds",
        ))
        .expect("valid metric definition");
    registry
        .register(Box::new(twitch_reconcile_duration_seconds.clone()))
        .expect("register svc_ingest_twitch_reconcile_duration_seconds");

    let twitch_session_events_total = prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_ingest_twitch_session_events_total",
            "Twitch session lifecycle events (started/stopped/restarted/lease_denied/lease_lost)",
        ),
        &["event"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(twitch_session_events_total.clone()))
        .expect("register svc_ingest_twitch_session_events_total");

    let twitch_resolve_errors_total = prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_ingest_twitch_resolve_errors_total",
            "Twitch resolve failures, labeled by a fixed class set",
        ),
        &["class"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(twitch_resolve_errors_total.clone()))
        .expect("register svc_ingest_twitch_resolve_errors_total");

    let twitch_discovery_skipped = prometheus::IntGaugeVec::new(
        prometheus::Opts::new(
            "svc_ingest_twitch_discovery_skipped",
            "Bindings skipped by the latest Twitch discovery pass (malformed/duplicate)",
        ),
        &["why"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(twitch_discovery_skipped.clone()))
        .expect("register svc_ingest_twitch_discovery_skipped");

    IngestMetrics {
        twitch_sessions_live,
        twitch_reconcile_total,
        twitch_reconcile_duration_seconds,
        twitch_session_events_total,
        twitch_resolve_errors_total,
        twitch_discovery_skipped,
        scope_resolutions_total,
        events_published_total,
        publish_errors_total,
        receiver_reconnects_total,
        eventsub_verifications_total,
        eventsub_dedup_hits_total,
        eventsub_request_duration_seconds,
        receiver_connection_healthy,
        spine_connect_attempts_total,
        consumer_loop_running,
    }
}

impl IngestMetrics {
    /// Records one Twitch EventSub webhook verification outcome --
    /// `crate::ingest::twitch_eventsub::handle_webhook`.
    pub fn record_eventsub_verification(&self, outcome: &str) {
        self.eventsub_verifications_total
            .with_label_values(&[outcome])
            .inc();
    }

    /// Records one Twitch EventSub duplicate-message-id rejection.
    pub fn record_eventsub_dedup_hit(&self) {
        self.eventsub_dedup_hits_total.inc();
    }

    /// Observes one Twitch EventSub webhook handler's end-to-end latency,
    /// labeled by `outcome` (mirrors [`Self::record_eventsub_verification`]'s
    /// label set, plus the terminal response outcomes: `ack`/
    /// `duplicate_ignored`/`acknowledged`/`ignored`/`unknown_type`/
    /// `challenge`/an error variant name).
    pub fn observe_eventsub_duration(&self, outcome: &str, seconds: f64) {
        self.eventsub_request_duration_seconds
            .with_label_values(&[outcome])
            .observe(seconds);
    }

    /// Records one spine-connect attempt for `receiver` --
    /// `crate::lib::connect_spine_with_retry`.
    pub fn record_spine_connect_attempt(&self, receiver: &str) {
        self.spine_connect_attempts_total
            .with_label_values(&[receiver])
            .inc();
    }

    /// Sets `receiver`'s consumer-loop-running gauge to 1 (connected) or 0
    /// (down/retrying) -- `crate::lib::connect_spine_with_retry`.
    pub fn set_consumer_loop_running(&self, receiver: &str, running: bool) {
        self.consumer_loop_running
            .with_label_values(&[receiver])
            .set(i64::from(running));
    }
}

/// Extends [`penguin_spine::SpineMetrics`] with the reconnect/health
/// counters this crate's own platform receiver loops need
/// (`ingest::discord`, and any future `ingest::twitch`-style caller) --
/// kept as a separate trait rather than folding into the shared spine
/// crate's `SpineMetrics` because these are svc-ingest's own receiver-
/// observability surface, not part of `penguin-spine`'s public contract.
/// Every method has a no-op default so a test double (e.g.
/// `ingest::discord::tests::NoopTestMetrics`) needs no implementation at
/// all unless a specific test wants to assert on the recorded values.
pub trait ReceiverHealthMetrics {
    /// Records one reconnect attempt for `platform`, labeled by the
    /// triggering `reason` -- e.g. `"resumable_close"`,
    /// `"reconnect_fresh_close"`, `"session_invalidated"`, `"other"`.
    fn receiver_reconnect(&self, _platform: &str, _reason: &str) {}

    /// Marks `platform`'s receiver connection as unhealthy: a fatal,
    /// non-retryable close (e.g. Discord `4004` auth failed, `4010`-`4014`
    /// sharding/intents) has stopped that connection's reconnect loop.
    /// `code` is the raw close code, when the peer sent one -- never a
    /// token, this is a small documented integer, not caller-provided
    /// secret material. The service process keeps running; only this one
    /// platform's ingest loop has stopped.
    fn receiver_marked_unhealthy(&self, _platform: &str, _code: Option<u16>) {}
}

impl ReceiverHealthMetrics for IngestMetrics {
    fn receiver_reconnect(&self, platform: &str, reason: &str) {
        self.receiver_reconnects_total
            .with_label_values(&[platform, reason])
            .inc();
    }

    fn receiver_marked_unhealthy(&self, platform: &str, code: Option<u16>) {
        self.receiver_connection_healthy
            .with_label_values(&[platform])
            .set(0);
        tracing::error!(
            platform,
            code = ?code,
            "receiver connection marked unhealthy (fatal, non-retryable close)"
        );
    }
}

/// Platform label of the legacy health gauge for Twitch IRC receivers.
const TWITCH_HEALTH_PLATFORM: &str = "twitch_irc";

impl IngestMetrics {
    /// Increments a closed-set counter; any value outside `allowed` is folded
    /// into `other` so no caller can mint a new label value (PII/cardinality).
    fn closed_inc(vec: &prometheus::IntCounterVec, allowed: &[&str], value: &str, n: u64) {
        let label = if allowed.contains(&value) {
            value
        } else {
            tracing::warn!("metric label outside its closed set; recorded as other");
            "other"
        };
        vec.with_label_values(&[label]).inc_by(n);
    }

    /// Sets the live-session gauge and mirrors it into
    /// `receiver_connection_healthy{platform="twitch_irc"}` (1 iff >=1 live).
    fn publish_live(&self, n: usize) {
        let n = i64::try_from(n).unwrap_or(i64::MAX);
        self.twitch_sessions_live.set(n);
        self.receiver_connection_healthy
            .with_label_values(&[TWITCH_HEALTH_PLATFORM])
            .set(i64::from(n > 0));
    }
}

impl SessionMetrics for IngestMetrics {
    fn session_ended(&self, reason: SessionEnd) {
        let ev = match reason {
            SessionEnd::LeaseLost => "lease_lost",
            SessionEnd::Shutdown | SessionEnd::NonRetryable => "stopped",
        };
        Self::closed_inc(&self.twitch_session_events_total, &SESSION_EVENTS, ev, 1);
    }

    fn lease_event(&self, outcome: LeaseOutcome) {
        let ev = match outcome {
            LeaseOutcome::Denied => "lease_denied",
            // A loss ends the session, so `session_ended(LeaseLost)` counts it.
            LeaseOutcome::Acquired
            | LeaseOutcome::Error
            | LeaseOutcome::Renewed
            | LeaseOutcome::Lost => return,
        };
        Self::closed_inc(&self.twitch_session_events_total, &SESSION_EVENTS, ev, 1);
    }

    fn discovery(&self, _outcome: DiscoveryOutcome) {}

    fn reconcile_done(&self, report: &ReconcileReport, elapsed: Duration, live: usize) {
        let outcome = if report.discovery_failed {
            "discovery_failed"
        } else {
            "ok"
        };
        self.twitch_reconcile_total
            .with_label_values(&[outcome])
            .inc();
        self.twitch_reconcile_duration_seconds
            .observe(elapsed.as_secs_f64());
        // Disjoint counts: a restart is `restarted` only, never also `started`.
        if report.started > 0 {
            Self::closed_inc(
                &self.twitch_session_events_total,
                &SESSION_EVENTS,
                "started",
                u64::from(report.started),
            );
        }
        if report.restarted > 0 {
            Self::closed_inc(
                &self.twitch_session_events_total,
                &SESSION_EVENTS,
                "restarted",
                u64::from(report.restarted),
            );
        }
        self.publish_live(live);
    }

    fn set_live_sessions(&self, n: usize) {
        self.publish_live(n);
    }

    fn resolve_failed(&self, class: &'static str) {
        Self::closed_inc(
            &self.twitch_resolve_errors_total,
            &RESOLVE_CLASSES,
            class,
            1,
        );
    }

    fn discovery_skipped(&self, why: &'static str, n: u32) {
        let label = if SKIP_REASONS.contains(&why) {
            why
        } else {
            tracing::warn!("metric label outside its closed set; recorded as other");
            "other"
        };
        self.twitch_discovery_skipped
            .with_label_values(&[label])
            .set(i64::from(n));
    }
}

impl crate::scope_resolver::ResolverMetrics for IngestMetrics {
    fn scope_resolution(&self, platform: &str, outcome: &'static str) {
        self.scope_resolutions_total
            .with_label_values(&[platform, outcome])
            .inc();
    }
}

/// Adapts [`IngestMetrics`] to [`penguin_spine::SpineMetrics`] so
/// [`crate::publish::publish_event`] can record `stream_event_written`
/// straight into this service's own Prometheus counters without
/// `publish.rs` depending on `prometheus` types directly.
impl penguin_spine::SpineMetrics for IngestMetrics {
    fn stream_event_written(&self, platform: &str, source_id: &str) {
        self.events_published_total
            .with_label_values(&[platform, source_id])
            .inc();
    }

    fn insecure_transport(&self, component: &str, aspect: &str, insecure: bool) {
        if insecure {
            tracing::warn!(component, aspect, "insecure transport in use");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use penguin_spine::SpineMetrics;

    #[test]
    fn register_request_metrics_produces_a_non_empty_exposition() {
        let registry = prometheus::Registry::new();
        let metrics = register_request_metrics(&registry);
        metrics
            .http_requests_total
            .with_label_values(&["GET", "/health", "200"])
            .inc();
        metrics
            .http_request_duration_seconds
            .with_label_values(&["GET", "/health"])
            .observe(0.001);

        let rendered = render_metrics(&registry).expect("registry with metrics must encode");
        assert!(rendered.contains("svc_ingest_up 1"));
        assert!(rendered.contains("svc_ingest_http_requests_total"));
        assert!(rendered.contains("svc_ingest_http_request_duration_seconds"));
    }

    #[test]
    fn up_gauge_alone_is_a_non_empty_series_before_any_request() {
        let registry = prometheus::Registry::new();
        register_request_metrics(&registry);
        let rendered = render_metrics(&registry).expect("registry with metrics must encode");
        assert!(rendered.contains("svc_ingest_up 1"));
    }

    #[test]
    fn render_metrics_on_empty_registry_is_empty_string() {
        let registry = prometheus::Registry::new();
        let rendered = render_metrics(&registry).expect("empty registry still encodes");
        assert!(rendered.is_empty());
    }

    #[test]
    fn ingest_metrics_stream_event_written_increments_the_labeled_counter() {
        let registry = prometheus::Registry::new();
        let metrics = register_ingest_metrics(&registry);
        metrics.stream_event_written("twitch", "tw-channelA");
        metrics.stream_event_written("twitch", "tw-channelA");
        let rendered = render_metrics(&registry).expect("registry with metrics must encode");
        assert!(rendered.contains("svc_ingest_events_published_total"));
        assert_eq!(
            metrics
                .events_published_total
                .with_label_values(&["twitch", "tw-channelA"])
                .get(),
            2
        );
    }

    #[test]
    fn eventsub_metrics_record_and_render() {
        let registry = prometheus::Registry::new();
        let metrics = register_ingest_metrics(&registry);
        metrics.record_eventsub_verification("ok");
        metrics.record_eventsub_verification("bad_signature");
        metrics.record_eventsub_dedup_hit();
        metrics.observe_eventsub_duration("ack", 0.002);

        let rendered = render_metrics(&registry).expect("registry with metrics must encode");
        assert!(rendered.contains("svc_ingest_eventsub_verifications_total"));
        assert!(rendered.contains("svc_ingest_eventsub_dedup_hits_total"));
        assert!(rendered.contains("svc_ingest_eventsub_request_duration_seconds"));
        assert_eq!(
            metrics
                .eventsub_verifications_total
                .with_label_values(&["ok"])
                .get(),
            1
        );
        assert_eq!(metrics.eventsub_dedup_hits_total.get(), 1);
    }

    #[test]
    fn ingest_metrics_insecure_transport_does_not_panic() {
        let registry = prometheus::Registry::new();
        let metrics = register_ingest_metrics(&registry);
        metrics.insecure_transport("valkey", "tls", true);
        metrics.insecure_transport("valkey", "tls", false);
    }

    fn rendered(registry: &prometheus::Registry) -> String {
        render_metrics(registry).expect("registry must encode")
    }

    #[test]
    fn gauge_tracks_live_sessions() {
        let registry = prometheus::Registry::new();
        let m = register_ingest_metrics(&registry);
        m.set_live_sessions(3);
        assert!(rendered(&registry).contains("svc_ingest_twitch_sessions_live 3"));
        assert_eq!(
            m.receiver_connection_healthy
                .with_label_values(&["twitch_irc"])
                .get(),
            1
        );
        m.set_live_sessions(0);
        assert!(rendered(&registry).contains("svc_ingest_twitch_sessions_live 0"));
        assert_eq!(
            m.receiver_connection_healthy
                .with_label_values(&["twitch_irc"])
                .get(),
            0
        );
    }

    #[test]
    fn reconcile_histogram_and_counters_render() {
        let registry = prometheus::Registry::new();
        let m = register_ingest_metrics(&registry);
        let ok = ReconcileReport {
            restarted: 2,
            ..ReconcileReport::default()
        };
        let failed = ReconcileReport {
            discovery_failed: true,
            ..ReconcileReport::default()
        };
        m.reconcile_done(&ok, Duration::from_millis(5), 4);
        m.reconcile_done(&failed, Duration::from_millis(7), 4);
        let text = rendered(&registry);
        assert!(text.contains("svc_ingest_twitch_reconcile_duration_seconds_count 2"));
        assert!(text.contains(r#"svc_ingest_twitch_reconcile_total{outcome="ok"} 1"#));
        assert!(text.contains(r#"svc_ingest_twitch_reconcile_total{outcome="discovery_failed"} 1"#));
        assert!(text.contains(r#"svc_ingest_twitch_session_events_total{event="restarted"} 2"#));
        assert!(text.contains("svc_ingest_twitch_sessions_live 4"));
    }

    #[test]
    fn session_and_lease_events_map_to_fixed_event_labels() {
        let registry = prometheus::Registry::new();
        let m = register_ingest_metrics(&registry);
        m.reconcile_done(
            &ReconcileReport {
                started: 1,
                restarted: 1,
                ..ReconcileReport::default()
            },
            Duration::ZERO,
            1,
        );
        m.session_ended(SessionEnd::Shutdown);
        m.session_ended(SessionEnd::NonRetryable);
        m.session_ended(SessionEnd::LeaseLost);
        m.lease_event(LeaseOutcome::Denied);
        m.lease_event(LeaseOutcome::Lost);
        for quiet in [
            LeaseOutcome::Acquired,
            LeaseOutcome::Error,
            LeaseOutcome::Renewed,
        ] {
            m.lease_event(quiet);
        }
        m.discovery(DiscoveryOutcome::Ok);
        let c = |e: &str| m.twitch_session_events_total.with_label_values(&[e]).get();
        assert_eq!(c("started"), 1, "restart is not also a start");
        assert_eq!(c("restarted"), 1);
        assert_eq!(c("stopped"), 2);
        assert_eq!(c("lease_lost"), 1, "one loss is counted once");
        assert_eq!(c("lease_denied"), 1);
    }

    #[test]
    fn metric_label_values_are_a_closed_set() {
        let registry = prometheus::Registry::new();
        let m = register_ingest_metrics(&registry);
        for class in RESOLVE_CLASSES {
            m.resolve_failed(class);
        }
        for why in SKIP_REASONS {
            m.discovery_skipped(why, 1);
        }
        for end in [
            SessionEnd::Shutdown,
            SessionEnd::NonRetryable,
            SessionEnd::LeaseLost,
        ] {
            m.session_ended(end);
        }
        for lo in [
            LeaseOutcome::Acquired,
            LeaseOutcome::Denied,
            LeaseOutcome::Error,
            LeaseOutcome::Renewed,
            LeaseOutcome::Lost,
        ] {
            m.lease_event(lo);
        }
        m.reconcile_done(
            &ReconcileReport {
                restarted: 1,
                ..ReconcileReport::default()
            },
            Duration::ZERO,
            1,
        );
        // Hostile values must be folded into `other`, never rendered.
        m.resolve_failed("oauth_SECRETTOKEN_xyz");
        m.discovery_skipped("chan_somestreamer", 1);

        let text = rendered(&registry);
        assert!(!text.contains("SECRETTOKEN") && !text.contains("somestreamer"));
        let allowed: std::collections::HashSet<&str> = RESOLVE_CLASSES
            .iter()
            .chain(SKIP_REASONS.iter())
            .chain(SESSION_EVENTS.iter())
            .chain(["ok", "discovery_failed", "other"].iter())
            .copied()
            .collect();
        let mut seen = 0;
        for line in text.lines().filter(|l| l.starts_with("svc_ingest_twitch_")) {
            for key in ["class", "why", "event", "outcome"] {
                let pat = format!("{key}=\"");
                if let Some(i) = line.find(&pat) {
                    let rest = &line[i + pat.len()..];
                    let val = &rest[..rest.find('"').expect("closing quote")];
                    assert!(
                        allowed.contains(val),
                        "label {key}={val} outside closed set"
                    );
                    seen += 1;
                }
            }
        }
        assert!(seen >= 15, "examined {seen} labels; scan looks broken");
        assert_eq!(
            m.twitch_resolve_errors_total
                .with_label_values(&["other"])
                .get(),
            1
        );
        // Every ResolveError variant's outcome() is inside the allow-list.
        use connection_resolver::{NotUsableReason, ResolveError};
        for e in [
            ResolveError::NotFound,
            ResolveError::NotUsable(NotUsableReason::Other),
            ResolveError::Auth,
            ResolveError::Transient("x"),
            ResolveError::CircuitOpen,
            ResolveError::Malformed("x"),
        ] {
            assert!(RESOLVE_CLASSES.contains(&e.outcome()));
        }
    }

    #[test]
    fn discovery_skipped_is_a_per_pass_snapshot_not_an_accumulator() {
        let registry = prometheus::Registry::new();
        let m = register_ingest_metrics(&registry);
        m.discovery_skipped("malformed", 0);
        m.discovery_skipped("duplicate", 3);
        m.discovery_skipped("duplicate", 3);
        assert_eq!(
            m.twitch_discovery_skipped
                .with_label_values(&["malformed"])
                .get(),
            0
        );
        assert_eq!(
            m.twitch_discovery_skipped
                .with_label_values(&["duplicate"])
                .get(),
            3
        );
    }

    #[test]
    fn register_ingest_metrics_registers_new_series_once() {
        let r = prometheus::Registry::new();
        let m = register_ingest_metrics(&r);
        m.set_live_sessions(2);
        m.resolve_failed("auth");
        m.reconcile_done(
            &ReconcileReport {
                started: 1,
                ..ReconcileReport::default()
            },
            Duration::ZERO,
            2,
        );
        m.discovery_skipped("duplicate", 1);
        let text = render_metrics(&r).expect("encode");
        for series in [
            "svc_ingest_twitch_sessions_live",
            "svc_ingest_twitch_reconcile_total",
            "svc_ingest_twitch_reconcile_duration_seconds",
            "svc_ingest_twitch_session_events_total",
            "svc_ingest_twitch_resolve_errors_total",
            "svc_ingest_twitch_discovery_skipped",
        ] {
            assert_eq!(
                text.matches(&format!("# TYPE {series} ")).count(),
                1,
                "{series} registered exactly once"
            );
        }
    }
}
