//! Bridge from the `metrics` facade into the OTel meter provider.
//!
//! `bundle-capability-gate` records its audit counters
//! (`waddles_bundle_capability_denied_total{app_id,permission,reason}` and
//! `waddles_bundle_capability_authorized_total{app_id,permission}`) through
//! the `metrics` crate facade and, by design, installs no recorder of its own
//! -- "the binary that eventually embeds this crate installs whatever
//! recorder it already uses". Until this module existed no recorder was
//! installed anywhere, so the facade's default no-op swallowed every
//! capability grant and denial. [`install`] closes that gap by registering an
//! [`OtelRecorder`] that forwards each facade metric to an OTel instrument of
//! the same name, which then flows out through the same OTLP and Prometheus
//! paths as every other metric.
//!
//! Only what the facade's users actually need is forwarded faithfully
//! (counters, histograms, absolute gauge sets). Operations OTel's synchronous
//! instruments cannot express (gauge increment/decrement, counter
//! `absolute`) are reported once per metric through `tracing::warn!` rather
//! than dropped silently.

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex, OnceLock};

use metrics::{
    Counter, CounterFn, Gauge, GaugeFn, Histogram, HistogramFn, Key, KeyName, Metadata, Recorder,
    SharedString, Unit,
};
use opentelemetry::metrics::Meter;
use opentelemetry::metrics::{Counter as OtelCounter, Gauge as OtelGauge, Histogram as OtelHist};
use opentelemetry::KeyValue;

/// Longest label value forwarded to OTel; anything longer is truncated so a
/// pathological value cannot bloat a series key.
const MAX_LABEL_VALUE_LEN: usize = 64;

/// Folds one facade label value into a bounded, identity-free form.
/// `permission` values for the `net.http.*` families embed the destination
/// host or IP (`net.http.fqdn:evil-123.example`), which a bundle controls --
/// left as-is, one hostile bundle could mint unbounded series -- so only the
/// family before the first `:` is kept. Everything is length-capped.
pub fn sanitize_label_value(label: &str, value: &str) -> String {
    let folded = if label == "permission" && value.starts_with("net.http.") {
        value.split_once(':').map_or(value, |(family, _)| family)
    } else {
        value
    };
    folded.chars().take(MAX_LABEL_VALUE_LEN).collect()
}

/// Converts a facade [`Key`]'s labels into OTel attributes via
/// [`sanitize_label_value`].
fn attributes(key: &Key) -> Vec<KeyValue> {
    key.labels()
        .map(|label| {
            KeyValue::new(
                label.key().to_string(),
                sanitize_label_value(label.key(), label.value()),
            )
        })
        .collect()
}

/// Locks `mutex`, recovering the guard if a panicking thread poisoned it: the
/// protected maps/sets are only ever inserted into, so a poisoned guard is
/// still consistent, and telemetry must never be able to panic the caller
/// (the same recovery `core/bundle_executor`'s heartbeat tracker uses).
fn lock<T>(mutex: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    match mutex.lock() {
        Ok(guard) => guard,
        Err(poisoned) => poisoned.into_inner(),
    }
}

/// Logs, at most once per `(metric, operation)` pair, that the facade asked
/// for something the OTel bridge cannot represent.
fn warn_unsupported_once(metric: &str, operation: &'static str) {
    static SEEN: OnceLock<Mutex<HashSet<String>>> = OnceLock::new();
    let seen = SEEN.get_or_init(|| Mutex::new(HashSet::new()));
    let first = lock(seen).insert(format!("{metric}/{operation}"));
    if first {
        tracing::warn!(
            metric,
            operation,
            "metrics-facade operation has no OTel equivalent and is NOT being recorded"
        );
    }
}

/// A `metrics::Recorder` that forwards to OTel instruments. Instruments are
/// created lazily, once per metric name, and shared by every handle.
pub struct OtelRecorder {
    meter: Meter,
    counters: Mutex<HashMap<String, OtelCounter<u64>>>,
    histograms: Mutex<HashMap<String, OtelHist<f64>>>,
    gauges: Mutex<HashMap<String, OtelGauge<f64>>>,
}

impl OtelRecorder {
    /// Builds a recorder that creates its instruments on `meter`.
    pub fn new(meter: Meter) -> Self {
        Self {
            meter,
            counters: Mutex::new(HashMap::new()),
            histograms: Mutex::new(HashMap::new()),
            gauges: Mutex::new(HashMap::new()),
        }
    }

    /// Returns the counter named `name`, creating it on first use.
    fn counter(&self, name: &str) -> OtelCounter<u64> {
        let mut map = lock(&self.counters);
        // The Prometheus exporter re-appends `_total` to every counter, so the
        // instrument is registered WITHOUT it (see `stage_metrics::
        // instrument_name`): `x_total` scrapes as `x_total`, never `x_total_total`.
        let instrument = crate::stage_metrics::instrument_name(name);
        map.entry(name.to_string())
            .or_insert_with(|| self.meter.u64_counter(instrument.to_string()).build())
            .clone()
    }

    /// Returns the histogram named `name`, creating it on first use.
    fn histogram(&self, name: &str) -> OtelHist<f64> {
        let mut map = lock(&self.histograms);
        map.entry(name.to_string())
            .or_insert_with(|| self.meter.f64_histogram(name.to_string()).build())
            .clone()
    }

    /// Returns the gauge named `name`, creating it on first use.
    fn gauge(&self, name: &str) -> OtelGauge<f64> {
        let mut map = lock(&self.gauges);
        map.entry(name.to_string())
            .or_insert_with(|| self.meter.f64_gauge(name.to_string()).build())
            .clone()
    }
}

/// Counter handle bound to one `(name, labels)` registration.
struct CounterHandle {
    name: String,
    counter: OtelCounter<u64>,
    attrs: Vec<KeyValue>,
}

impl CounterFn for CounterHandle {
    fn increment(&self, value: u64) {
        self.counter.add(value, &self.attrs);
    }

    fn absolute(&self, _value: u64) {
        warn_unsupported_once(&self.name, "counter.absolute");
    }
}

/// Gauge handle bound to one `(name, labels)` registration.
struct GaugeHandle {
    name: String,
    gauge: OtelGauge<f64>,
    attrs: Vec<KeyValue>,
}

impl GaugeFn for GaugeHandle {
    fn increment(&self, _value: f64) {
        warn_unsupported_once(&self.name, "gauge.increment");
    }

    fn decrement(&self, _value: f64) {
        warn_unsupported_once(&self.name, "gauge.decrement");
    }

    fn set(&self, value: f64) {
        self.gauge.record(value, &self.attrs);
    }
}

/// Histogram handle bound to one `(name, labels)` registration.
struct HistogramHandle {
    histogram: OtelHist<f64>,
    attrs: Vec<KeyValue>,
}

impl HistogramFn for HistogramHandle {
    fn record(&self, value: f64) {
        self.histogram.record(value, &self.attrs);
    }
}

impl Recorder for OtelRecorder {
    fn describe_counter(&self, _key: KeyName, _unit: Option<Unit>, _description: SharedString) {}

    fn describe_gauge(&self, _key: KeyName, _unit: Option<Unit>, _description: SharedString) {}

    fn describe_histogram(&self, _key: KeyName, _unit: Option<Unit>, _description: SharedString) {}

    fn register_counter(&self, key: &Key, _metadata: &Metadata<'_>) -> Counter {
        Counter::from_arc(Arc::new(CounterHandle {
            name: key.name().to_string(),
            counter: self.counter(key.name()),
            attrs: attributes(key),
        }))
    }

    fn register_gauge(&self, key: &Key, _metadata: &Metadata<'_>) -> Gauge {
        Gauge::from_arc(Arc::new(GaugeHandle {
            name: key.name().to_string(),
            gauge: self.gauge(key.name()),
            attrs: attributes(key),
        }))
    }

    fn register_histogram(&self, key: &Key, _metadata: &Metadata<'_>) -> Histogram {
        Histogram::from_arc(Arc::new(HistogramHandle {
            histogram: self.histogram(key.name()),
            attrs: attributes(key),
        }))
    }
}

/// Installs an [`OtelRecorder`] on the global OTel meter as the process-wide
/// `metrics` recorder. Must run after [`crate::telemetry::init`] (so the meter
/// is backed by the real provider). Returns whether this call installed it; a
/// recorder already being present is logged at ERROR, because the gate's
/// audit counters would then go wherever that other recorder sends them.
pub fn install() -> bool {
    let recorder = OtelRecorder::new(opentelemetry::global::meter(crate::stage_metrics::SCOPE));
    match metrics::set_global_recorder(recorder) {
        Ok(()) => {
            tracing::info!(
                "metrics-facade recorder installed: capability gate audit counters now export via OTel"
            );
            true
        }
        Err(err) => {
            tracing::error!(
                error = %err,
                "a metrics-facade recorder is already installed; capability gate audit counters \
                 are NOT routed to OTel by this service"
            );
            false
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::stage_metrics::test_support::Harness;

    fn recorder() -> (Harness, OtelRecorder) {
        let harness = Harness::new();
        let recorder = OtelRecorder::new(harness.meter());
        (harness, recorder)
    }

    #[test]
    fn counter_increments_forward_to_an_otel_counter_with_its_labels() {
        let (h, recorder) = recorder();
        metrics::with_local_recorder(&recorder, || {
            metrics::counter!(
                "waddles_bundle_capability_denied_total",
                "app_id" => "waddles.test.facade",
                "permission" => "storage.kv",
                "reason" => "not_granted",
            )
            .increment(1);
            metrics::counter!(
                "waddles_bundle_capability_denied_total",
                "app_id" => "waddles.test.facade",
                "permission" => "storage.kv",
                "reason" => "not_granted",
            )
            .increment(2);
        });
        assert_eq!(
            h.count(
                "waddles_bundle_capability_denied_total",
                &[
                    ("app_id", "waddles.test.facade"),
                    ("permission", "storage.kv"),
                    ("reason", "not_granted"),
                ]
            ),
            3.0
        );
    }

    #[test]
    fn instruments_are_created_once_per_name_and_shared_across_label_sets() {
        let (h, recorder) = recorder();
        metrics::with_local_recorder(&recorder, || {
            metrics::counter!("waddles_test_shared_total", "k" => "a").increment(1);
            metrics::counter!("waddles_test_shared_total", "k" => "b").increment(1);
        });
        assert_eq!(h.count("waddles_test_shared_total", &[("k", "a")]), 1.0);
        assert_eq!(h.count("waddles_test_shared_total", &[("k", "b")]), 1.0);
        assert_eq!(recorder.counters.lock().unwrap().len(), 1);
    }

    #[test]
    fn histograms_and_absolute_gauges_forward() {
        let (h, recorder) = recorder();
        metrics::with_local_recorder(&recorder, || {
            metrics::histogram!("waddles_test_hist", "k" => "v").record(2.5);
            metrics::gauge!("waddles_test_gauge", "k" => "v").set(7.0);
        });
        let hist = h
            .last("waddles_test_hist", &[("k", "v")])
            .expect("histogram exported");
        assert_eq!((hist.value, hist.sum), (1.0, 2.5));
        let gauge = h
            .last("waddles_test_gauge", &[("k", "v")])
            .expect("gauge exported");
        assert_eq!(gauge.value, 7.0);
    }

    #[test]
    fn operations_otel_cannot_express_are_not_recorded() {
        let (h, recorder) = recorder();
        metrics::with_local_recorder(&recorder, || {
            metrics::gauge!("waddles_test_unsupported_gauge").increment(1.0);
            metrics::gauge!("waddles_test_unsupported_gauge").decrement(1.0);
            metrics::counter!("waddles_test_unsupported_counter").absolute(9);
        });
        assert_eq!(h.count("waddles_test_unsupported_gauge", &[]), 0.0);
        assert_eq!(h.count("waddles_test_unsupported_counter", &[]), 0.0);
    }

    #[test]
    fn net_http_permission_labels_collapse_to_their_family() {
        assert_eq!(
            sanitize_label_value("permission", "net.http.fqdn:evil-123.example"),
            "net.http.fqdn"
        );
        assert_eq!(
            sanitize_label_value("permission", "net.http.public-ip:203.0.113.9"),
            "net.http.public-ip"
        );
        assert_eq!(
            sanitize_label_value("permission", "net.http.private-ip:10.0.0.0/8"),
            "net.http.private-ip"
        );
        // Bounded vocabularies pass through unchanged.
        assert_eq!(
            sanitize_label_value("permission", "chat.send:discord"),
            "chat.send:discord"
        );
        assert_eq!(
            sanitize_label_value("reason", "net.http.x:y"),
            "net.http.x:y"
        );
        assert_eq!(
            sanitize_label_value("app_id", &"x".repeat(500)).len(),
            MAX_LABEL_VALUE_LEN
        );
    }

    #[test]
    fn hostile_http_destinations_cannot_mint_new_series() {
        let (h, recorder) = recorder();
        metrics::with_local_recorder(&recorder, || {
            for i in 0..50 {
                metrics::counter!(
                    "waddles_bundle_capability_denied_total",
                    "app_id" => "waddles.test.hostile",
                    "permission" => format!("net.http.fqdn:host-{i}.example"),
                    "reason" => "not_granted",
                )
                .increment(1);
            }
        });
        let series = h
            .points()
            .into_iter()
            .filter(|p| p.name == "waddles_bundle_capability_denied")
            .count();
        assert_eq!(series, 1, "50 distinct hosts must fold into one series");
        assert_eq!(
            h.count(
                "waddles_bundle_capability_denied_total",
                &[("permission", "net.http.fqdn")]
            ),
            50.0
        );
    }

    /// The reason this module exists: the gate's own `authorize()` audit
    /// counters (recorded through the `metrics` facade) must reach OTel.
    #[test]
    fn the_real_gate_audit_counters_reach_otel_through_the_bridge() {
        use bundle_capability_gate::{
            CapabilityGate, GrantScopeKey, GrantSet, GrantedPermission, InMemoryGrantSnapshot,
            InMemoryInstancePolicySnapshot, InMemoryMembership, InMemoryQuotaLedger,
        };
        use penguin_bundle_host::wire::{CapabilityKind, HostCallBody};

        use crate::capabilities::{CapabilityHandler, StageCapabilities};

        let (h, recorder) = recorder();
        // `platform.clock` granted, nothing else: the clock call is
        // authorized and any `http` call fails closed `not_granted`.
        let snapshot = InMemoryGrantSnapshot::new();
        snapshot.set(
            GrantScopeKey {
                tenant_id: 1,
                community_id: 1,
                app_id: "waddles.test.gatebridge".to_string(),
                app_version: 1,
            },
            GrantSet {
                permission_snapshot_hash: "test".to_string(),
                grants: std::collections::HashMap::from([(
                    "platform.clock".to_string(),
                    GrantedPermission {
                        permission_id: "platform.clock".to_string(),
                        params: serde_json::json!({}),
                    },
                )]),
            },
        );
        let gate = Arc::new(CapabilityGate::new(
            Arc::new(snapshot),
            Arc::new(InMemoryMembership::new()),
            Arc::new(InMemoryQuotaLedger::new()),
            Arc::new(InMemoryInstancePolicySnapshot::new()),
        ));
        let egress = Arc::new(bundle_host_http::egress::EgressGuard::new(
            Arc::new(bundle_host_http::egress::ReqwestTransport::new()),
            bundle_host_http::egress::EgressLimits {
                allow_private_hosts: false,
                rate_limit_rps: 10,
                rate_limit_burst: 20,
                timeout: std::time::Duration::from_secs(5),
                max_redirects: 3,
                max_response_bytes: 1_048_576,
                allowed_ports: vec![443],
                proxy_url: None,
            },
            crate::capabilities::HttpEgressCatalog::new(),
            prometheus::IntCounterVec::new(
                prometheus::Opts::new("test_facade_egress_denied_total", "test"),
                &["app_id", "reason"],
            )
            .expect("valid metric definition"),
            bundle_host_http::egress::boxed(bundle_host_http::egress::StaticFlag(true)),
        ));
        let caps = StageCapabilities::<redis::aio::MultiplexedConnection>::new(
            "acme".to_string(),
            Some("main".to_string()),
            "waddles.test.gatebridge".to_string(),
            1,
            1,
            1,
            egress,
            gate,
        );
        let call = |capability: CapabilityKind, op: &str, args: serde_json::Value| HostCallBody {
            app_id: "waddles.test.gatebridge".to_string(),
            capability,
            op: op.to_string(),
            args,
            call_id: 1,
        };

        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("current-thread runtime");
        metrics::with_local_recorder(&recorder, || {
            runtime.block_on(async {
                // `clock` is an always-granted platform permission -> authorized.
                caps.handle(call(
                    CapabilityKind::Clock,
                    "now-rfc3339",
                    serde_json::json!({}),
                ))
                .await
                .expect("clock call is always granted");
                // `http` to an ungranted host -> denied by the gate.
                let denied = caps
                    .handle(call(
                        CapabilityKind::Http,
                        "send",
                        serde_json::json!({"url": "https://hostile-1.example/x"}),
                    ))
                    .await;
                assert!(denied.is_err(), "ungranted http must be denied");
            });
        });

        assert!(
            h.count(
                "waddles_bundle_capability_authorized_total",
                &[("app_id", "waddles.test.gatebridge")]
            ) >= 1.0,
            "the gate's authorize() audit counter must reach OTel"
        );
        assert_eq!(
            h.count(
                "waddles_bundle_capability_denied_total",
                &[
                    ("app_id", "waddles.test.gatebridge"),
                    ("permission", "net.http.fqdn"),
                    ("reason", "not_granted"),
                ]
            ),
            1.0,
            "the gate's denial must reach OTel with the host folded away"
        );
    }
}
