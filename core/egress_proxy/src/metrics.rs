//! This service's own Prometheus metrics, registered against the shared
//! registry `crate::telemetry::init` returns (same `RequestMetrics`-style
//! pattern as `core/svc_process`/`core/svc_action`'s own `telemetry.rs`:
//! the OTel meter provider owns the process-wide `target_info` series,
//! application metrics register directly against the same registry).
//! Histograms come first per `rules/critical-rules.md` Observability -- a
//! lone request counter is not instrumentation -- plus the `/health`/
//! `/ready`/`/metrics` HTTP surface the Helm Deployment's probes target.

use std::sync::Arc;

use axum::extract::State;
use axum::response::IntoResponse;
use axum::routing::get;
use axum::Router;
use prometheus::{HistogramVec, IntCounterVec, Registry};

pub struct Metrics {
    pub registry: Registry,
    pub requests_total: IntCounterVec,
    /// Validation + DNS-resolve + dial latency, labeled by mode
    /// (`connect`/`forward`) -- the "latency" histogram.
    pub request_duration_seconds: HistogramVec,
    /// Total lifetime of a proxied connection (CONNECT tunnel open-to-
    /// close, or one forward-HTTP round trip), labeled by mode -- the
    /// "connection-duration" histogram.
    pub connection_duration_seconds: HistogramVec,
    /// Running byte counter, labeled by tenant/direction (unchanged from
    /// the original streaming-copy instrumentation).
    pub bytes_transferred_total: IntCounterVec,
    /// Total bytes moved per completed connection, labeled by tenant --
    /// the "bytes-transferred" histogram (distribution of connection
    /// sizes, complementing the running counter above).
    pub connection_bytes: HistogramVec,
}

impl Metrics {
    /// Registers every metric against `registry` (the same registry
    /// `crate::telemetry::init` hands back, shared with the OTel meter
    /// provider's Prometheus reader). Must be called exactly once per
    /// registry -- `prometheus::Registry` panics on duplicate
    /// registration, matching every other Rust data-plane service's
    /// `register_*_metrics` convention in this repo.
    pub fn new(registry: &Registry) -> Arc<Self> {
        let requests_total = IntCounterVec::new(
            prometheus::Opts::new(
                "egress_proxy_requests_total",
                "Proxied requests by mode and decision",
            ),
            &["mode", "decision", "reason"],
        )
        .expect("metric registration");
        let request_duration_seconds = HistogramVec::new(
            prometheus::HistogramOpts::new(
                "egress_proxy_request_duration_seconds",
                "Time to validate + resolve + dial the upstream connection",
            ),
            &["mode"],
        )
        .expect("metric registration");
        let connection_duration_seconds = HistogramVec::new(
            prometheus::HistogramOpts::new(
                "egress_proxy_connection_duration_seconds",
                "Total lifetime of a proxied connection, from validated to closed",
            )
            .buckets(vec![
                0.1, 0.5, 1.0, 5.0, 15.0, 30.0, 60.0, 300.0, 900.0, 3600.0,
            ]),
            &["mode"],
        )
        .expect("metric registration");
        let bytes_transferred_total = IntCounterVec::new(
            prometheus::Opts::new(
                "egress_proxy_bytes_transferred_total",
                "Bytes relayed by tenant and direction",
            ),
            &["tenant", "direction"],
        )
        .expect("metric registration");
        let connection_bytes = HistogramVec::new(
            prometheus::HistogramOpts::new(
                "egress_proxy_connection_bytes",
                "Total bytes moved (both directions) per completed connection",
            )
            .buckets(prometheus::exponential_buckets(1024.0, 4.0, 12).expect("valid buckets")),
            &["tenant"],
        )
        .expect("metric registration");

        registry
            .register(Box::new(requests_total.clone()))
            .expect("register");
        registry
            .register(Box::new(request_duration_seconds.clone()))
            .expect("register");
        registry
            .register(Box::new(connection_duration_seconds.clone()))
            .expect("register");
        registry
            .register(Box::new(bytes_transferred_total.clone()))
            .expect("register");
        registry
            .register(Box::new(connection_bytes.clone()))
            .expect("register");

        Arc::new(Self {
            registry: registry.clone(),
            requests_total,
            request_duration_seconds,
            connection_duration_seconds,
            bytes_transferred_total,
            connection_bytes,
        })
    }
}

async fn health() -> impl IntoResponse {
    "ok"
}

async fn ready() -> impl IntoResponse {
    "ok"
}

async fn metrics_handler(State(metrics): State<Arc<Metrics>>) -> impl IntoResponse {
    let rendered = crate::telemetry::render_metrics(&metrics.registry).unwrap_or_default();
    (
        [(
            axum::http::header::CONTENT_TYPE,
            "text/plain; version=0.0.4",
        )],
        rendered,
    )
}

pub fn router(metrics: Arc<Metrics>) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/ready", get(ready))
        .route("/metrics", get(metrics_handler))
        .with_state(metrics)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn registers_all_metrics_without_panicking_and_renders() {
        let registry = Registry::new();
        let metrics = Metrics::new(&registry);
        metrics
            .requests_total
            .with_label_values(&["connect", "allow", "-"])
            .inc();
        metrics
            .request_duration_seconds
            .with_label_values(&["connect"])
            .observe(0.01);
        metrics
            .connection_duration_seconds
            .with_label_values(&["connect"])
            .observe(1.5);
        metrics
            .bytes_transferred_total
            .with_label_values(&["tenant-a", "egress"])
            .inc_by(128);
        metrics
            .connection_bytes
            .with_label_values(&["tenant-a"])
            .observe(4096.0);

        let rendered = crate::telemetry::render_metrics(&registry).expect("registry must encode");
        assert!(rendered.contains("egress_proxy_requests_total"));
        assert!(rendered.contains("egress_proxy_request_duration_seconds"));
        assert!(rendered.contains("egress_proxy_connection_duration_seconds"));
        assert!(rendered.contains("egress_proxy_bytes_transferred_total"));
        assert!(rendered.contains("egress_proxy_connection_bytes"));
    }
}
