//! In-memory metrics capture for tests: an SDK meter provider whose exporter
//! snapshots every data point, so a test can assert the exact instrument
//! names, units and label sets without any network or global state.
//!
//! Compiled for this crate's own tests and, behind the non-default
//! `test-support` feature, for dependents (`egress_proxy`) that assert the
//! `waddles_jwt_verifications_total{verifier,alg,outcome}` stream their
//! verifier emits. Never enabled in a service binary.

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use opentelemetry_sdk::error::OTelSdkResult;
use opentelemetry_sdk::metrics::data::{AggregatedMetrics, MetricData, ResourceMetrics};
use opentelemetry_sdk::metrics::exporter::PushMetricExporter;
use opentelemetry_sdk::metrics::{PeriodicReader, SdkMeterProvider, Temporality};

use crate::jwt_hardening::{JwtMetrics, METRIC_VERIFICATIONS};

/// One exported data point: counter value, or histogram observation count.
#[derive(Debug, Clone, PartialEq)]
pub struct Point {
    /// Instrument name (`waddles_jwt_verifications_total` / `..._seconds`).
    pub name: String,
    /// Instrument unit.
    pub unit: String,
    /// Exact label set, name -> value.
    pub attrs: BTreeMap<String, String>,
    /// Counter value, or histogram observation count.
    pub value: u64,
    /// Explicit histogram bucket boundaries (empty for the counter).
    pub bounds: Vec<f64>,
}

#[derive(Clone, Debug, Default)]
struct Snapshot {
    points: Arc<Mutex<Vec<Point>>>,
}

impl PushMetricExporter for Snapshot {
    async fn export(&self, metrics: &ResourceMetrics) -> OTelSdkResult {
        let mut out = Vec::new();
        for scope in metrics.scope_metrics() {
            for metric in scope.metrics() {
                let attrs_of = |iter: &mut dyn Iterator<Item = &opentelemetry::KeyValue>| {
                    iter.map(|kv| (kv.key.to_string(), kv.value.as_str().to_string()))
                        .collect::<BTreeMap<_, _>>()
                };
                match metric.data() {
                    AggregatedMetrics::U64(MetricData::Sum(sum)) => {
                        for dp in sum.data_points() {
                            out.push(Point {
                                name: metric.name().to_string(),
                                unit: metric.unit().to_string(),
                                attrs: attrs_of(&mut dp.attributes()),
                                value: dp.value(),
                                bounds: Vec::new(),
                            });
                        }
                    }
                    AggregatedMetrics::F64(MetricData::Histogram(hist)) => {
                        for dp in hist.data_points() {
                            out.push(Point {
                                name: metric.name().to_string(),
                                unit: metric.unit().to_string(),
                                attrs: attrs_of(&mut dp.attributes()),
                                value: dp.count(),
                                bounds: dp.bounds().collect(),
                            });
                        }
                    }
                    _ => {}
                }
            }
        }
        *self.points.lock().expect("snapshot lock") = out;
        Ok(())
    }

    fn force_flush(&self) -> OTelSdkResult {
        Ok(())
    }

    fn shutdown_with_timeout(&self, _timeout: Duration) -> OTelSdkResult {
        Ok(())
    }

    fn temporality(&self) -> Temporality {
        Temporality::Cumulative
    }
}

/// A capturing provider plus the [`JwtMetrics`] bound to it.
pub struct Capture {
    snapshot: Snapshot,
    provider: SdkMeterProvider,
    pub metrics: JwtMetrics,
}

impl Default for Capture {
    fn default() -> Self {
        Self::new()
    }
}

impl Capture {
    /// A fresh provider with an hour-long export interval (flushed by hand).
    pub fn new() -> Self {
        let snapshot = Snapshot::default();
        let reader = PeriodicReader::builder(snapshot.clone())
            .with_interval(Duration::from_secs(3600))
            .build();
        let provider = SdkMeterProvider::builder().with_reader(reader).build();
        let metrics = JwtMetrics::new(&provider);
        Self {
            snapshot,
            provider,
            metrics,
        }
    }

    /// Install this capture's provider as the process-wide OTel meter
    /// provider, so the public (global-metrics) verifier entry points report
    /// into it. Call once, before the first verification, from a test binary
    /// that owns its own process (an integration test under `tests/`).
    pub fn install_global(&self) {
        opentelemetry::global::set_meter_provider(self.provider.clone());
    }

    /// Every exported data point named `name`, after a forced flush.
    pub fn points(&self, name: &str) -> Vec<Point> {
        self.provider.force_flush().expect("flush meter provider");
        self.snapshot
            .points
            .lock()
            .expect("snapshot lock")
            .iter()
            .filter(|point| point.name == name)
            .cloned()
            .collect()
    }

    /// The counter value for the exact `(verifier, alg, outcome)` label set.
    pub fn count(&self, verifier: &str, alg: &str, outcome: &str) -> u64 {
        self.points(METRIC_VERIFICATIONS)
            .into_iter()
            .filter(|p| {
                p.attrs.get("verifier").map(String::as_str) == Some(verifier)
                    && p.attrs.get("alg").map(String::as_str) == Some(alg)
                    && p.attrs.get("outcome").map(String::as_str) == Some(outcome)
            })
            .map(|p| p.value)
            .sum()
    }

    /// The sum of every counter data point (total verifications recorded).
    pub fn total(&self) -> u64 {
        self.points(METRIC_VERIFICATIONS)
            .iter()
            .map(|p| p.value)
            .sum()
    }
}
