//! Shared in-memory OpenTelemetry sink for the telemetry-emission
//! integration tests (`rules/testing.md` Telemetry Validation: a smoke run
//! must prove logs/metrics/traces were *received*, with counts, never "no
//! errors").
//!
//! [`OtelSink::install`] points the process-global meter provider, text-map
//! propagator, and `tracing` subscriber at in-memory exporters, so code under
//! test that reaches for `opentelemetry::global::meter` /
//! `StreamMetrics::shared()` / `tracing::info_span!` emits into a sink the
//! test can read back. It must run **before** anything constructs a
//! `StreamMetrics` (instruments bound to the no-op default provider never
//! come alive). Each `tests/*.rs` file is its own process, so the globals
//! are isolated per file.

#![allow(dead_code)]

use std::sync::OnceLock;

use opentelemetry::global;
use opentelemetry::metrics::MeterProvider as _;
use opentelemetry::trace::TracerProvider as _;
use opentelemetry::KeyValue;
use opentelemetry_sdk::metrics::data::{AggregatedMetrics, MetricData};
use opentelemetry_sdk::metrics::{InMemoryMetricExporter, PeriodicReader, SdkMeterProvider};
use opentelemetry_sdk::propagation::TraceContextPropagator;
use opentelemetry_sdk::trace::{InMemorySpanExporter, SdkTracerProvider, SpanData};
use tracing_subscriber::layer::SubscriberExt as _;
use tracing_subscriber::util::SubscriberInitExt as _;

/// One flattened exported metric data point: histograms fill `count`/`sum`;
/// sums (counters, up-down counters) and gauges put their value in `sum`.
#[derive(Debug, Clone)]
pub struct Point {
    pub count: u64,
    pub sum: f64,
    pub attrs: Vec<(String, String)>,
}

fn attrs_of<'a>(kvs: impl Iterator<Item = &'a KeyValue>) -> Vec<(String, String)> {
    kvs.map(|kv| (kv.key.to_string(), kv.value.to_string()))
        .collect()
}

/// In-memory metric + span exporters wired into the process globals.
pub struct OtelSink {
    meter_provider: SdkMeterProvider,
    metric_exporter: InMemoryMetricExporter,
    tracer_provider: SdkTracerProvider,
    span_exporter: InMemorySpanExporter,
    collect_lock: std::sync::Mutex<()>,
}

static SINK: OnceLock<OtelSink> = OnceLock::new();

impl OtelSink {
    /// Installs (once per process) and returns the sink.
    pub fn install() -> &'static OtelSink {
        SINK.get_or_init(|| {
            let metric_exporter = InMemoryMetricExporter::default();
            let meter_provider = SdkMeterProvider::builder()
                .with_reader(PeriodicReader::builder(metric_exporter.clone()).build())
                .build();
            global::set_meter_provider(meter_provider.clone());
            global::set_text_map_propagator(TraceContextPropagator::new());

            let span_exporter = InMemorySpanExporter::default();
            let tracer_provider = SdkTracerProvider::builder()
                .with_simple_exporter(span_exporter.clone())
                .build();
            let tracer = tracer_provider.tracer("svc-streaming-test");
            // INFO and above only, mirroring `telemetry::init`'s default
            // `EnvFilter("info")`: what this sink sees is what a default
            // production deployment would export. `try_init`: a second
            // install in one process (a test bug) must not panic the
            // harness; the first subscriber stays in force.
            let _ = tracing_subscriber::registry()
                .with(tracing_subscriber::filter::LevelFilter::INFO)
                .with(tracing_opentelemetry::layer().with_tracer(tracer))
                .try_init();

            OtelSink {
                meter_provider,
                metric_exporter,
                tracer_provider,
                span_exporter,
                collect_lock: std::sync::Mutex::new(()),
            }
        })
    }

    /// A fresh [`opentelemetry::metrics::Meter`] on the sink's provider, for
    /// building an explicit `StreamMetrics::with_meter`.
    pub fn meter(&self) -> opentelemetry::metrics::Meter {
        self.meter_provider.meter("svc-streaming-test")
    }

    /// Flushes the metric pipeline and returns every data point of
    /// instrument `name`. The exporter is reset first so only this flush's
    /// cumulative snapshot is read (summing across flushes would double
    /// count).
    pub fn points(&self, name: &str) -> Vec<Point> {
        // reset -> flush -> read must be atomic: tests in one process run in
        // parallel, and a concurrent reset between another test's flush and
        // read would hand it an empty snapshot.
        let _collecting = self
            .collect_lock
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        self.metric_exporter.reset();
        self.meter_provider.force_flush().expect("flush metrics");
        let mut out = Vec::new();
        for rm in self
            .metric_exporter
            .get_finished_metrics()
            .expect("read metrics")
        {
            for sm in rm.scope_metrics() {
                for metric in sm.metrics().filter(|m| m.name() == name) {
                    match metric.data() {
                        AggregatedMetrics::F64(MetricData::Histogram(h)) => {
                            out.extend(h.data_points().map(|p| Point {
                                count: p.count(),
                                sum: p.sum(),
                                attrs: attrs_of(p.attributes()),
                            }));
                        }
                        AggregatedMetrics::U64(MetricData::Histogram(h)) => {
                            out.extend(h.data_points().map(|p| Point {
                                count: p.count(),
                                sum: p.sum() as f64,
                                attrs: attrs_of(p.attributes()),
                            }));
                        }
                        AggregatedMetrics::U64(MetricData::Sum(s)) => {
                            out.extend(s.data_points().map(|p| Point {
                                count: 0,
                                sum: p.value() as f64,
                                attrs: attrs_of(p.attributes()),
                            }));
                        }
                        AggregatedMetrics::I64(MetricData::Sum(s)) => {
                            out.extend(s.data_points().map(|p| Point {
                                count: 0,
                                sum: p.value() as f64,
                                attrs: attrs_of(p.attributes()),
                            }));
                        }
                        AggregatedMetrics::I64(MetricData::Gauge(g)) => {
                            out.extend(g.data_points().map(|p| Point {
                                count: 0,
                                sum: p.value() as f64,
                                attrs: attrs_of(p.attributes()),
                            }));
                        }
                        AggregatedMetrics::F64(MetricData::Gauge(g)) => {
                            out.extend(g.data_points().map(|p| Point {
                                count: 0,
                                sum: p.value(),
                                attrs: attrs_of(p.attributes()),
                            }));
                        }
                        _ => {}
                    }
                }
            }
        }
        out
    }

    /// (count, sum) of histogram `name` points whose attributes include every
    /// `want` pair (an empty `want` matches all points).
    pub fn histogram(&self, name: &str, want: &[(&str, &str)]) -> (u64, f64) {
        matching(&self.points(name), want).fold((0, 0.0), |(c, s), p| (c + p.count, s + p.sum))
    }

    /// Total value of counter / up-down counter / gauge `name` points whose
    /// attributes include every `want` pair.
    pub fn counter(&self, name: &str, want: &[(&str, &str)]) -> i64 {
        matching(&self.points(name), want)
            .map(|p| p.sum as i64)
            .sum()
    }

    /// Every span closed so far (the simple processor exports on close).
    pub fn spans(&self) -> Vec<SpanData> {
        let _ = self.tracer_provider.force_flush();
        self.span_exporter.get_finished_spans().expect("read spans")
    }

    /// Asserts every `(name, want)` histogram has at least one data point and
    /// prints each count -- the telemetry gate never accepts a bare "no
    /// errors"; a zero denominator is a failure.
    pub fn assert_histograms_emitted(&self, expected: &[(&str, &[(&str, &str)])]) {
        for (name, want) in expected {
            let (count, sum) = self.histogram(name, want);
            println!("telemetry: histogram {name} {want:?}: {count} data point(s), sum={sum}");
            assert!(
                count >= 1,
                "histogram {name} {want:?} emitted {count} data points; expected >= 1"
            );
        }
    }
}

fn matching<'a>(all: &'a [Point], want: &'a [(&str, &str)]) -> impl Iterator<Item = &'a Point> {
    all.iter().filter(move |p| {
        want.iter()
            .all(|(k, v)| p.attrs.iter().any(|(ak, av)| ak == k && av == v))
    })
}

/// Renders a span's attributes as `key=value` strings.
pub fn span_attrs(span: &SpanData) -> Vec<String> {
    span.attributes
        .iter()
        .map(|kv| format!("{}={}", kv.key, kv.value))
        .collect()
}

/// Polls `check` every 20 ms until it returns `true` or `timeout` elapses
/// (panicking with `what` on timeout).
pub async fn wait_for(what: &str, timeout: std::time::Duration, mut check: impl FnMut() -> bool) {
    let deadline = std::time::Instant::now() + timeout;
    loop {
        if check() {
            return;
        }
        assert!(
            std::time::Instant::now() < deadline,
            "timed out after {timeout:?} waiting for: {what}"
        );
        tokio::time::sleep(std::time::Duration::from_millis(20)).await;
    }
}
