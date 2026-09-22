//! Telemetry bootstrap: `tracing` (structured logs, stdout JSON) +
//! OpenTelemetry OTLP traces/metrics + a Prometheus registry for the
//! secondary `/metrics` scrape surface.
//!
//! The OTLP destination is always env-configured
//! (`OTEL_EXPORTER_OTLP_ENDPOINT`/`_PROTOCOL`/`_HEADERS`,
//! `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`), never hardcoded --
//! see `rules/critical-rules.md` Observability (OTel). When
//! `OTEL_EXPORTER_OTLP_ENDPOINT` is unset, OTLP export is skipped entirely
//! rather than defaulting to a local collector nobody configured; a dead or
//! unreachable exporter must never crash or block the app, so any exporter
//! build failure downgrades to tracing-only (stdout) and is logged, not
//! propagated.
//!
//! Ported from `core/svc_streaming/src/telemetry.rs` verbatim except for
//! the metric name prefix -- once `penguin-logging` (spec S4.9) lands, this
//! module is replaced by that crate rather than hand-maintained per
//! service.

use opentelemetry::global;
use opentelemetry::trace::TracerProvider as _;
use opentelemetry_otlp::{Protocol, WithExportConfig};
use opentelemetry_sdk::metrics::SdkMeterProvider;
use opentelemetry_sdk::trace::SdkTracerProvider;
use opentelemetry_sdk::Resource;
use tracing_subscriber::layer::SubscriberExt;
use tracing_subscriber::util::SubscriberInitExt;
use tracing_subscriber::EnvFilter;

/// Holds OTel provider handles that must be flushed/shut down at process
/// exit. Dropping this guard (or calling [`TelemetryGuard::shutdown`])
/// flushes any buffered spans/metrics before the process exits.
pub struct TelemetryGuard {
    tracer_provider: Option<SdkTracerProvider>,
    meter_provider: Option<SdkMeterProvider>,
}

impl TelemetryGuard {
    /// Flushes and shuts down any active OTLP pipelines. Errors are logged,
    /// never propagated -- shutdown must not be able to fail the caller.
    pub fn shutdown(&mut self) {
        if let Some(provider) = self.tracer_provider.take() {
            if let Err(err) = provider.shutdown() {
                eprintln!("otel tracer provider shutdown error: {err}");
            }
        }
        if let Some(provider) = self.meter_provider.take() {
            if let Err(err) = provider.shutdown() {
                eprintln!("otel meter provider shutdown error: {err}");
            }
        }
    }
}

impl Drop for TelemetryGuard {
    fn drop(&mut self) {
        self.shutdown();
    }
}

fn otlp_protocol() -> Protocol {
    match std::env::var("OTEL_EXPORTER_OTLP_PROTOCOL").as_deref() {
        Ok("http/protobuf") => Protocol::HttpBinary,
        _ => Protocol::Grpc,
    }
}

fn resource(default_service_name: &str) -> Resource {
    let service_name =
        std::env::var("OTEL_SERVICE_NAME").unwrap_or_else(|_| default_service_name.to_string());
    // `Resource::builder()` already layers in `EnvResourceDetector`, which
    // reads `OTEL_RESOURCE_ATTRIBUTES` -- we only need to set the name
    // explicitly so there is always a sane default.
    Resource::builder().with_service_name(service_name).build()
}

fn build_tracer_provider(endpoint: &str, res: Resource) -> anyhow::Result<SdkTracerProvider> {
    let exporter = match otlp_protocol() {
        Protocol::Grpc => opentelemetry_otlp::SpanExporter::builder()
            .with_tonic()
            .with_endpoint(endpoint)
            .build()?,
        _ => opentelemetry_otlp::SpanExporter::builder()
            .with_http()
            .with_endpoint(endpoint)
            .build()?,
    };
    Ok(SdkTracerProvider::builder()
        .with_batch_exporter(exporter)
        .with_resource(res)
        .build())
}

fn build_meter_provider(endpoint: &str, res: Resource) -> anyhow::Result<SdkMeterProvider> {
    let exporter = match otlp_protocol() {
        Protocol::Grpc => opentelemetry_otlp::MetricExporter::builder()
            .with_tonic()
            .with_endpoint(endpoint)
            .build()?,
        _ => opentelemetry_otlp::MetricExporter::builder()
            .with_http()
            .with_endpoint(endpoint)
            .build()?,
    };
    Ok(SdkMeterProvider::builder()
        .with_periodic_exporter(exporter)
        .with_resource(res)
        .build())
}

/// Initializes `tracing` (env-filtered, JSON to stdout) plus best-effort
/// OTLP trace/metric export, and returns a fresh Prometheus [`Registry`]
/// for the `/metrics` HTTP surface. Must be called exactly once, before any
/// other `tracing` macro use.
pub fn init(default_service_name: &str) -> (TelemetryGuard, prometheus::Registry) {
    let env_filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));

    let endpoint = std::env::var("OTEL_EXPORTER_OTLP_ENDPOINT").ok();

    let (tracer_provider, meter_provider) = match &endpoint {
        Some(endpoint) => {
            let res = resource(default_service_name);
            let tracer = build_tracer_provider(endpoint, res.clone())
                .inspect_err(|err| {
                    eprintln!("otel trace exporter init failed, continuing without traces: {err}");
                })
                .ok();
            let meter = build_meter_provider(endpoint, res)
                .inspect_err(|err| {
                    eprintln!(
                        "otel metric exporter init failed, continuing without OTLP metrics: {err}"
                    );
                })
                .ok();
            (tracer, meter)
        }
        None => (None, None),
    };

    if let Some(provider) = &meter_provider {
        global::set_meter_provider(provider.clone());
    }

    let fmt_layer = tracing_subscriber::fmt::layer().json();
    let registry = tracing_subscriber::registry()
        .with(env_filter)
        .with(fmt_layer);

    match &tracer_provider {
        Some(provider) => {
            let tracer = provider.tracer(default_service_name.to_string());
            registry
                .with(tracing_opentelemetry::layer().with_tracer(tracer))
                .init();
        }
        None => registry.init(),
    }

    (
        TelemetryGuard {
            tracer_provider,
            meter_provider,
        },
        prometheus::Registry::new(),
    )
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    // std::env is process-global; serialize env-mutating tests so parallel
    // `cargo test` threads don't race on the same variables.
    static ENV_LOCK: Mutex<()> = Mutex::new(());

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
    fn otlp_protocol_defaults_to_grpc() {
        let _guard = ENV_LOCK.lock().unwrap();
        // SAFETY: serialized by ENV_LOCK.
        unsafe { std::env::remove_var("OTEL_EXPORTER_OTLP_PROTOCOL") };
        assert!(matches!(otlp_protocol(), Protocol::Grpc));
    }

    #[test]
    fn otlp_protocol_recognizes_http_protobuf() {
        let _guard = ENV_LOCK.lock().unwrap();
        // SAFETY: serialized by ENV_LOCK.
        unsafe { std::env::set_var("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf") };
        assert!(matches!(otlp_protocol(), Protocol::HttpBinary));
        unsafe { std::env::remove_var("OTEL_EXPORTER_OTLP_PROTOCOL") };
    }

    #[test]
    fn render_metrics_on_empty_registry_is_empty_string() {
        let registry = prometheus::Registry::new();
        let rendered = render_metrics(&registry).expect("empty registry still encodes");
        assert!(rendered.is_empty());
    }

    #[test]
    fn resource_defaults_to_provided_service_name() {
        let _guard = ENV_LOCK.lock().unwrap();
        // SAFETY: serialized by ENV_LOCK.
        unsafe { std::env::remove_var("OTEL_SERVICE_NAME") };
        let res = resource("svc-ingest-test");
        let value = res.get(&opentelemetry::Key::from_static_str("service.name"));
        assert_eq!(
            value.map(|v| v.to_string()),
            Some("svc-ingest-test".to_string())
        );
    }
}
