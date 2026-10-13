//! Prometheus metrics for the connector driver. Every label is a bounded
//! enum-like `&'static str` or a registered platform slug -- never a
//! `source_id`/connection id (unbounded cardinality) and never anything
//! frame- or user-derived (PII-free by construction).

use prometheus::{HistogramOpts, HistogramVec, IntCounterVec, IntGaugeVec, Opts, Registry};

/// The connector driver's instruments.
#[derive(Clone)]
pub struct ConnectorMetrics {
    /// `svc_ingest_connector_frames_total{platform,outcome}` -- one per raw
    /// frame; outcome `ok`, `guest_error`, `oversize`, or a trip/unavailable
    /// kind.
    pub frames_total: IntCounterVec,
    /// `svc_ingest_connector_events_total{platform,outcome}` -- one per event
    /// a connector returned; outcome `published`, `invalid`,
    /// `publish_error`, `platform_mismatch`, `truncated`.
    pub events_total: IntCounterVec,
    /// `svc_ingest_connector_trap_total{platform,kind}` -- connection
    /// teardowns caused by a sandbox trip or an unavailable invoker
    /// (spec S2.6's `waddles_connector_trap_total`, minus the unbounded
    /// `source_id` label).
    pub trap_total: IntCounterVec,
    /// `svc_ingest_connector_on_frame_duration_seconds{platform}` -- the
    /// `on-frame` invocation latency (histogram, spec S4's budget).
    pub on_frame_duration_seconds: HistogramVec,
    /// `svc_ingest_connector_connection_up{platform}` -- live connections
    /// currently past connect+handshake.
    pub connection_up: IntGaugeVec,
    /// `svc_ingest_connector_connects_total{platform,outcome}` -- connect
    /// attempts, outcome `ok`/`failed`.
    pub connects_total: IntCounterVec,
}

/// Registers the connector metrics against `registry`. Call exactly once per
/// registry; a duplicate registration is a typed error, not a panic.
pub fn register_connector_metrics(
    registry: &Registry,
) -> Result<ConnectorMetrics, prometheus::Error> {
    let frames_total = IntCounterVec::new(
        Opts::new(
            "svc_ingest_connector_frames_total",
            "Raw frames handled by the connector driver, by platform and outcome",
        ),
        &["platform", "outcome"],
    )?;
    let events_total = IntCounterVec::new(
        Opts::new(
            "svc_ingest_connector_events_total",
            "Normalized events returned by connectors, by platform and outcome",
        ),
        &["platform", "outcome"],
    )?;
    let trap_total = IntCounterVec::new(
        Opts::new(
            "svc_ingest_connector_trap_total",
            "Connections torn down by a connector sandbox trip or unavailable invoker",
        ),
        &["platform", "kind"],
    )?;
    let on_frame_duration_seconds = HistogramVec::new(
        HistogramOpts::new(
            "svc_ingest_connector_on_frame_duration_seconds",
            "Connector on-frame invocation latency in seconds",
        )
        .buckets(vec![
            0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5,
        ]),
        &["platform"],
    )?;
    let connection_up = IntGaugeVec::new(
        Opts::new(
            "svc_ingest_connector_connection_up",
            "Connections currently past connect and handshake, by platform",
        ),
        &["platform"],
    )?;
    let connects_total = IntCounterVec::new(
        Opts::new(
            "svc_ingest_connector_connects_total",
            "Connector-driven connect attempts, by platform and outcome",
        ),
        &["platform", "outcome"],
    )?;
    registry.register(Box::new(frames_total.clone()))?;
    registry.register(Box::new(events_total.clone()))?;
    registry.register(Box::new(trap_total.clone()))?;
    registry.register(Box::new(on_frame_duration_seconds.clone()))?;
    registry.register(Box::new(connection_up.clone()))?;
    registry.register(Box::new(connects_total.clone()))?;
    Ok(ConnectorMetrics {
        frames_total,
        events_total,
        trap_total,
        on_frame_duration_seconds,
        connection_up,
        connects_total,
    })
}

impl ConnectorMetrics {
    /// Counts one frame outcome.
    pub fn frame(&self, platform: &str, outcome: &str) {
        self.frames_total
            .with_label_values(&[platform, outcome])
            .inc();
    }

    /// Counts one event outcome.
    pub fn event(&self, platform: &str, outcome: &str) {
        self.events_total
            .with_label_values(&[platform, outcome])
            .inc();
    }

    /// Counts one teardown cause.
    pub fn trap(&self, platform: &str, kind: &str) {
        self.trap_total.with_label_values(&[platform, kind]).inc();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn registers_once_and_a_second_registration_is_a_typed_error() {
        let registry = Registry::new();
        let m = register_connector_metrics(&registry).unwrap();
        m.frame("echo", "ok");
        m.event("echo", "published");
        m.trap("echo", "trap");
        m.on_frame_duration_seconds
            .with_label_values(&["echo"])
            .observe(0.001);
        m.connection_up.with_label_values(&["echo"]).set(1);
        m.connects_total.with_label_values(&["echo", "ok"]).inc();
        assert!(register_connector_metrics(&registry).is_err());
        let families = registry.gather();
        assert_eq!(
            families.len(),
            6,
            "all six connector series must be exported"
        );
    }
}
