//! Telemetry bootstrap: delegates `tracing` + sanitizing OTel logs/traces/
//! metrics entirely to `penguin-logging` (spec SS4.9), keeping only this
//! service's own Prometheus request metrics (`RequestMetrics`) local, since
//! those are svc-process-specific counters, not part of the shared crate's
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
//! This is the second half of the M4 penguin-libs dependency pattern (see
//! `Cargo.toml`'s header comment for the first half, the git-dependency
//! mechanism itself) -- once wired, a service should never hand-roll
//! `tracing-subscriber`/OTel plumbing again.

/// Re-exported so callers (`crate::lib::run_with_shutdown`) hold the guard
/// type without needing to depend on `penguin_logging` directly for it.
pub use penguin_logging::TelemetryGuard;

/// Initializes structured logging + OTel logs/traces/metrics via
/// `penguin_logging::init`, and returns the guard plus a fresh Prometheus
/// [`prometheus::Registry`] for this service's own `/metrics` HTTP surface.
/// Must be called exactly once, before any other `tracing` macro use.
///
/// `penguin_logging::init` also returns a `LevelHandle` for runtime
/// log-level changes; this skeleton doesn't yet expose a control-plane
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
    let up = prometheus::IntGauge::new("svc_process_up", "1 if the process is running")
        .expect("valid metric definition");
    registry
        .register(Box::new(up.clone()))
        .expect("register svc_process_up");
    up.set(1);

    let http_requests_total = prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_process_http_requests_total",
            "Total HTTP requests handled, labeled by method/path/status",
        ),
        &["method", "path", "status"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(http_requests_total.clone()))
        .expect("register svc_process_http_requests_total");

    let http_request_duration_seconds = prometheus::HistogramVec::new(
        prometheus::HistogramOpts::new(
            "svc_process_http_request_duration_seconds",
            "HTTP request duration in seconds, labeled by method/path",
        ),
        &["method", "path"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(http_request_duration_seconds.clone()))
        .expect("register svc_process_http_request_duration_seconds");

    RequestMetrics {
        http_requests_total,
        http_request_duration_seconds,
    }
}

/// Ops-visibility fix (security review): the DB-driven active-bundle
/// loader (`crate::bundle_loader`) previously only logged when
/// `bundle_active_set::read_active_set` excluded an active row (no current
/// approval / missing digest / referential-integrity gap) -- a feature
/// silently going dark (e.g. an approval expiring with nothing
/// re-approving it) is easy to miss in a log stream alone. Registered
/// alongside [`register_request_metrics`] (same "before `AppState` exists"
/// timing constraint doesn't apply here, but kept in this module for the
/// same "this service's own Prometheus metrics live here" reason) and
/// incremented once per excluded row by `bundle_loader::run_tick`, labeled
/// by `app_id`/`reason` (`bundle_active_set::ExclusionReason::as_str`).
pub fn register_bundle_loader_excluded_metrics(
    registry: &prometheus::Registry,
) -> prometheus::IntCounterVec {
    let excluded_total = prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_process_bundle_active_set_excluded_total",
            "Active app_active_versions rows excluded from the DB-driven bundle loader's \
             active set, by app_id/reason",
        ),
        &["app_id", "reason"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(excluded_total.clone()))
        .expect("register svc_process_bundle_active_set_excluded_total");
    excluded_total
}

/// Prometheus handles for `crate::source_supervisor`: a gauge tracking how
/// many per-`(app_id, platform, source_id)` binding consumer tasks are
/// currently running, and a counter (labeled `action` = `"spawn"`/`"stop"`)
/// tracking every spawn/stop transition -- so a flapping binding (spawned
/// and stopped repeatedly, e.g. a NOGROUP retry loop against a
/// not-yet-provisioned stream) is visible on a dashboard, not just the
/// point-in-time gauge value.
#[derive(Clone)]
pub struct SourceBindingSupervisorMetrics {
    pub active_consumers: prometheus::IntGauge,
    pub consumer_transitions_total: prometheus::IntCounterVec,
}

/// Registers [`SourceBindingSupervisorMetrics`] against `registry`. Must be
/// called exactly once per `registry` -- see
/// [`register_bundle_loader_excluded_metrics`]'s identical constraint.
pub fn register_source_binding_supervisor_metrics(
    registry: &prometheus::Registry,
) -> SourceBindingSupervisorMetrics {
    let active_consumers = prometheus::IntGauge::new(
        "svc_process_source_binding_consumers_active",
        "Number of per-(app_id, platform, source_id) source-binding consumer tasks currently running",
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(active_consumers.clone()))
        .expect("register svc_process_source_binding_consumers_active");

    let consumer_transitions_total = prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_process_source_binding_consumer_transitions_total",
            "Source-binding consumer spawn/stop transitions, labeled by action",
        ),
        &["action"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(consumer_transitions_total.clone()))
        .expect("register svc_process_source_binding_consumer_transitions_total");

    SourceBindingSupervisorMetrics {
        active_consumers,
        consumer_transitions_total,
    }
}

/// Prometheus handle for `circuit_breaker::CircuitBreaker` (connector
/// spec SS0 condition 5): one counter, labeled by `source`/`action`
/// (`action` in `"failure"`/`"opened"`/`"closed"`), so a source flapping
/// open/closed or a fault storm across many sources is visible on a
/// dashboard rather than only in the `alert=true` log line
/// `CircuitBreaker::record_failure` emits on each trip.
#[derive(Clone)]
pub struct CircuitBreakerMetrics {
    pub transitions_total: prometheus::IntCounterVec,
}

/// Registers [`CircuitBreakerMetrics`] against `registry`. Must be called
/// exactly once per `registry` -- see
/// [`register_bundle_loader_excluded_metrics`]'s identical constraint.
pub fn register_circuit_breaker_metrics(registry: &prometheus::Registry) -> CircuitBreakerMetrics {
    let transitions_total = prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_process_circuit_breaker_transitions_total",
            "Per-source circuit breaker transitions (failure/opened/closed), labeled by \
             source/action",
        ),
        &["source", "action"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(transitions_total.clone()))
        .expect("register svc_process_circuit_breaker_transitions_total");

    CircuitBreakerMetrics { transitions_total }
}

impl circuit_breaker::CircuitBreakerMetrics for CircuitBreakerMetrics {
    fn transition(&self, source: &str, action: &str) {
        self.transitions_total
            .with_label_values(&[source, action])
            .inc();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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
        assert!(rendered.contains("svc_process_up 1"));
        assert!(rendered.contains("svc_process_http_requests_total"));
        assert!(rendered.contains("svc_process_http_request_duration_seconds"));
    }

    #[test]
    fn register_bundle_loader_excluded_metrics_produces_a_labeled_counter() {
        let registry = prometheus::Registry::new();
        let excluded_total = register_bundle_loader_excluded_metrics(&registry);
        excluded_total
            .with_label_values(&["waddles.a", "no_approval"])
            .inc();
        let rendered = render_metrics(&registry).expect("registry with metrics must encode");
        assert!(rendered.contains("svc_process_bundle_active_set_excluded_total"));
        assert!(rendered.contains(r#"app_id="waddles.a""#));
        assert!(rendered.contains(r#"reason="no_approval""#));
    }

    #[test]
    fn up_gauge_alone_is_a_non_empty_series_before_any_request() {
        let registry = prometheus::Registry::new();
        register_request_metrics(&registry);
        let rendered = render_metrics(&registry).expect("registry with metrics must encode");
        assert!(rendered.contains("svc_process_up 1"));
    }

    #[test]
    fn render_metrics_on_empty_registry_is_empty_string() {
        let registry = prometheus::Registry::new();
        let rendered = render_metrics(&registry).expect("empty registry still encodes");
        assert!(rendered.is_empty());
    }

    #[test]
    fn register_circuit_breaker_metrics_produces_a_labeled_counter() {
        use circuit_breaker::CircuitBreakerMetrics as _;
        let registry = prometheus::Registry::new();
        let metrics = register_circuit_breaker_metrics(&registry);
        metrics.transition("source-a", "opened");
        let rendered = render_metrics(&registry).expect("registry with metrics must encode");
        assert!(rendered.contains("svc_process_circuit_breaker_transitions_total"));
        assert!(rendered.contains(r#"source="source-a""#));
        assert!(rendered.contains(r#"action="opened""#));
    }

    #[test]
    fn register_source_binding_supervisor_metrics_produces_a_gauge_and_labeled_counter() {
        let registry = prometheus::Registry::new();
        let metrics = register_source_binding_supervisor_metrics(&registry);
        metrics.active_consumers.inc();
        metrics
            .consumer_transitions_total
            .with_label_values(&["spawn"])
            .inc();
        let rendered = render_metrics(&registry).expect("registry with metrics must encode");
        assert!(rendered.contains("svc_process_source_binding_consumers_active 1"));
        assert!(rendered.contains("svc_process_source_binding_consumer_transitions_total"));
        assert!(rendered.contains(r#"action="spawn""#));
    }
}
