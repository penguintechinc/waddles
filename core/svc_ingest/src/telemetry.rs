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
            "Total platform receiver reconnect attempts, labeled by platform",
        ),
        &["platform"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(receiver_reconnects_total.clone()))
        .expect("register svc_ingest_receiver_reconnects_total");

    IngestMetrics {
        events_published_total,
        publish_errors_total,
        receiver_reconnects_total,
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
    fn ingest_metrics_insecure_transport_does_not_panic() {
        let registry = prometheus::Registry::new();
        let metrics = register_ingest_metrics(&registry);
        metrics.insecure_transport("valkey", "tls", true);
        metrics.insecure_transport("valkey", "tls", false);
    }
}
