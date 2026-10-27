//! Telemetry bootstrap: reuses `penguin-logging` (spec SS4.9) exactly like
//! `core/svc_process`/`core/svc_action`/`core/svc_ingest` -- `tracing` +
//! sanitizing OTel logs/traces + OTel/Prometheus metrics, all driven by
//! the standard `OTEL_EXPORTER_OTLP_*` env vars (`critical-rules.md`
//! Observability). An unset `OTEL_EXPORTER_OTLP_ENDPOINT` or a failed
//! exporter build skips OTLP export entirely -- stdout JSON logs and the
//! Prometheus `/metrics` surface keep working either way, and proxying
//! itself never depends on the exporter being reachable (a dead collector
//! only means buffered spans/logs/metrics are eventually dropped by the
//! batch exporter, never a blocked or failed request).
//!
//! This module additionally sets the global W3C `traceparent` propagator
//! (not part of `penguin-logging` itself, which has no HTTP inbound path
//! of its own) so [`extract_parent_context`] can pull the calling
//! service's trace context out of an inbound CONNECT/forward-HTTP
//! request and attach it as the parent of this proxy's own span
//! (`crate::proxy::handle`), keeping one trace across the service
//! boundary instead of starting a disconnected one here.

pub use penguin_logging::TelemetryGuard;

use opentelemetry::propagation::{Extractor, TextMapPropagator};
use opentelemetry_sdk::propagation::TraceContextPropagator;

/// Initializes structured logging + OTel logs/traces/metrics via
/// `penguin_logging::init`, installs the W3C trace-context propagator,
/// and returns the guard plus a fresh Prometheus [`prometheus::Registry`]
/// for this service's own `/metrics` HTTP surface. Must be called exactly
/// once, before any other `tracing` macro use.
pub fn init(default_service_name: &str) -> (TelemetryGuard, prometheus::Registry) {
    let cfg = penguin_logging::ServiceConfig::from_env(default_service_name);
    let (guard, _level_handle, registry) = penguin_logging::init(cfg);
    opentelemetry::global::set_text_map_propagator(TraceContextPropagator::new());
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

/// Adapts an `http::HeaderMap` to OpenTelemetry's [`Extractor`] trait so
/// [`extract_parent_context`] can hand it straight to a
/// [`TextMapPropagator`].
struct HeaderExtractor<'a>(&'a http::HeaderMap);

impl Extractor for HeaderExtractor<'_> {
    fn get(&self, key: &str) -> Option<&str> {
        self.0.get(key).and_then(|v| v.to_str().ok())
    }

    fn keys(&self) -> Vec<&str> {
        self.0.keys().map(|k| k.as_str()).collect()
    }
}

/// Extracts a W3C `traceparent`/`tracestate` pair from `headers` (sent by
/// the calling data-plane service, if it's instrumented) into an
/// [`opentelemetry::Context`], for [`tracing_opentelemetry::
/// OpenTelemetrySpanExt::set_parent`] to attach to this proxy's own
/// request span. Absent/malformed headers simply produce a fresh root
/// context -- this never fails or blocks a request.
pub fn extract_parent_context(headers: &http::HeaderMap) -> opentelemetry::Context {
    TraceContextPropagator::new().extract(&HeaderExtractor(headers))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extract_parent_context_on_headers_with_no_traceparent_is_a_root_context() {
        let headers = http::HeaderMap::new();
        let cx = extract_parent_context(&headers);
        // No remote span recorded -- this is a fresh (non-remote) context,
        // never a panic/error on absent headers.
        assert!(!opentelemetry::trace::TraceContextExt::span(&cx)
            .span_context()
            .is_remote());
    }

    #[test]
    fn extract_parent_context_reads_a_valid_traceparent_header() {
        let mut headers = http::HeaderMap::new();
        headers.insert(
            "traceparent",
            "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
                .parse()
                .unwrap(),
        );
        let cx = extract_parent_context(&headers);
        let span = opentelemetry::trace::TraceContextExt::span(&cx);
        let span_cx = span.span_context();
        assert!(span_cx.is_valid());
        assert!(span_cx.is_remote());
    }

    #[test]
    fn render_metrics_on_empty_registry_is_empty_string() {
        let registry = prometheus::Registry::new();
        let rendered = render_metrics(&registry).expect("empty registry still encodes");
        assert!(rendered.is_empty());
    }
}
