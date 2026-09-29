//! Prometheus metrics (`:9090`, matches `values.yaml`
//! `egressProxy.metricsPort`) plus the `/health`/`/ready` probes the Helm
//! Deployment's liveness/readiness probes already target. OTel
//! traces/logs are emitted separately via `tracing` (see `src/lib.rs`);
//! this remains the secondary Prometheus scrape surface per
//! `critical-rules.md` Observability.

use std::sync::Arc;

use axum::extract::State;
use axum::response::IntoResponse;
use axum::routing::get;
use axum::Router;
use prometheus::{Encoder, HistogramVec, IntCounterVec, Registry, TextEncoder};

pub struct Metrics {
    pub registry: Registry,
    pub requests_total: IntCounterVec,
    pub bytes_transferred_total: IntCounterVec,
    pub connect_duration_seconds: HistogramVec,
}

impl Metrics {
    pub fn new() -> Arc<Self> {
        let registry = Registry::new();
        let requests_total = IntCounterVec::new(
            prometheus::Opts::new(
                "egress_proxy_requests_total",
                "Proxied requests by mode and decision",
            ),
            &["mode", "decision", "reason"],
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
        let connect_duration_seconds = HistogramVec::new(
            prometheus::HistogramOpts::new(
                "egress_proxy_connect_duration_seconds",
                "Time to validate + dial the upstream connection",
            ),
            &["mode"],
        )
        .expect("metric registration");

        registry
            .register(Box::new(requests_total.clone()))
            .expect("register");
        registry
            .register(Box::new(bytes_transferred_total.clone()))
            .expect("register");
        registry
            .register(Box::new(connect_duration_seconds.clone()))
            .expect("register");

        Arc::new(Self {
            registry,
            requests_total,
            bytes_transferred_total,
            connect_duration_seconds,
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
    let mut buffer = Vec::new();
    let encoder = TextEncoder::new();
    let families = metrics.registry.gather();
    encoder
        .encode(&families, &mut buffer)
        .expect("prometheus encode");
    (
        [(
            axum::http::header::CONTENT_TYPE,
            encoder.format_type().to_string(),
        )],
        buffer,
    )
}

pub fn router(metrics: Arc<Metrics>) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/ready", get(ready))
        .route("/metrics", get(metrics_handler))
        .with_state(metrics)
}
