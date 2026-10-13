//! Prometheus metrics for the render path -- same direct-`prometheus`-crate
//! pattern `crate::telemetry::register_request_metrics` already
//! establishes for this service (not the `metrics`/`metrics-exporter-
//! prometheus` facade, which this crate doesn't depend on), registered
//! separately so adopting it doesn't touch `crate::http::AppState` (owned
//! by P3/P4's route wiring, not this slice). Histogram first, per
//! `rules/critical-rules.md` Observability -- a lone counter is not
//! instrumentation.

use prometheus::{HistogramOpts, HistogramVec, IntCounterVec, Opts};

/// Render-path metric handles -- labeled by `surface`
/// ([`overlay_schema::Surface::as_str`]) and, for the counter, `outcome`
/// (`"ok"`/`"error"`).
#[derive(Clone)]
pub struct RenderMetrics {
    pub renders_total: IntCounterVec,
    pub render_duration_seconds: HistogramVec,
}

/// Registers the render-path metrics against `registry`. Must be called
/// exactly once per registry (a `prometheus::Registry` panics on duplicate
/// registration) -- P3/P4 call this once during `AppState` construction,
/// alongside the existing `crate::telemetry::register_request_metrics`
/// call.
pub fn register_render_metrics(registry: &prometheus::Registry) -> RenderMetrics {
    let renders_total = IntCounterVec::new(
        Opts::new(
            "svc_presentation_overlay_renders_total",
            "Total overlay surface renders, labeled by surface and outcome (ok/error)",
        ),
        &["surface", "outcome"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(renders_total.clone()))
        .expect("register svc_presentation_overlay_renders_total");

    let render_duration_seconds = HistogramVec::new(
        HistogramOpts::new(
            "svc_presentation_overlay_render_duration_seconds",
            "Overlay surface render duration in seconds, labeled by surface",
        ),
        &["surface"],
    )
    .expect("valid metric definition");
    registry
        .register(Box::new(render_duration_seconds.clone()))
        .expect("register svc_presentation_overlay_render_duration_seconds");

    RenderMetrics {
        renders_total,
        render_duration_seconds,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use prometheus::Encoder;

    fn render(registry: &prometheus::Registry) -> String {
        let mut buf = Vec::new();
        prometheus::TextEncoder::new()
            .encode(&registry.gather(), &mut buf)
            .unwrap();
        String::from_utf8(buf).unwrap()
    }

    #[test]
    fn register_render_metrics_exposes_both_series_after_one_observation() {
        let registry = prometheus::Registry::new();
        let metrics = register_render_metrics(&registry);
        metrics
            .renders_total
            .with_label_values(&["chat", "ok"])
            .inc();
        metrics
            .render_duration_seconds
            .with_label_values(&["chat"])
            .observe(0.002);

        let rendered = render(&registry);
        assert!(rendered.contains("svc_presentation_overlay_renders_total"));
        assert!(rendered.contains("svc_presentation_overlay_render_duration_seconds"));
    }

    #[test]
    fn two_distinct_registries_each_register_cleanly() {
        // Guards against accidentally registering into a shared/global
        // registry -- a second, independent registry must not panic on
        // its own first registration.
        let registry_a = prometheus::Registry::new();
        let registry_b = prometheus::Registry::new();
        register_render_metrics(&registry_a);
        register_render_metrics(&registry_b);
    }
}
