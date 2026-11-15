//! Telemetry-validation assertions for the command-latency instrumentation (the repo's smoke-test
//! rule): one over-budget command must emit logs >= 1, metric data points >= 1 (histograms >= 1)
//! and spans >= 1, counts printed, ZERO of any signal is a FAILURE -- never a skip.
//!
//! The sinks are in-process (in-memory OTel exporters + a capturing log layer) so this runs in every
//! `cargo test`; the services' own smoke tests additionally run against the OTLP test sink.

use std::sync::{Arc, Mutex};

use command_latency::stage::{report_confirmed, Kind};
use command_latency::stamp::{Door, Stamps};
use command_latency::Outcome;
use opentelemetry::trace::TracerProvider as _;
use opentelemetry_sdk::metrics::data::{AggregatedMetrics, MetricData};
use opentelemetry_sdk::metrics::{InMemoryMetricExporter, PeriodicReader, SdkMeterProvider};
use opentelemetry_sdk::trace::{InMemorySpanExporter, SdkTracerProvider};
use tracing_subscriber::layer::{Context, SubscriberExt as _};
use tracing_subscriber::Layer;

#[derive(Clone, Default)]
struct Logs(Arc<Mutex<Vec<tracing::Level>>>);

impl<S: tracing::Subscriber> Layer<S> for Logs {
    fn on_event(&self, e: &tracing::Event<'_>, _: Context<'_, S>) {
        self.0.lock().unwrap().push(*e.metadata().level());
    }
}

#[test]
fn an_over_budget_command_emits_logs_metrics_histograms_and_spans() {
    command_latency::switch::set_disabled(false);
    let metric_exporter = InMemoryMetricExporter::default();
    let meter_provider = SdkMeterProvider::builder()
        .with_reader(PeriodicReader::builder(metric_exporter.clone()).build())
        .build();
    opentelemetry::global::set_meter_provider(meter_provider.clone());
    let span_exporter = InMemorySpanExporter::default();
    let tracer_provider = SdkTracerProvider::builder()
        .with_simple_exporter(span_exporter.clone())
        .build();
    let logs = Logs::default();
    let subscriber = tracing_subscriber::registry()
        .with(
            tracing_opentelemetry::layer()
                .with_tracer(tracer_provider.tracer("telemetry-validation")),
        )
        .with(logs.clone());

    let now = command_latency::stamp::now_ms();
    let ts = Stamps {
        t0_ms: Some(now - 1_300),
        door: Some(Door::Gateway),
        command: Some("ping".into()),
        ..Stamps::default()
    }
    .merge_into_tracestate(None);
    tracing::subscriber::with_default(subscriber, || {
        let span = tracing::info_span!("outbound.send", app_id = "waddles.core.example.ping");
        let _g = span.enter();
        let r = report_confirmed(
            Kind::User,
            Some(&ts),
            "waddles.core.example.ping",
            7,
            9,
            Outcome::Ok,
            now - 100,
            now,
        )
        .expect("stamped command is reported");
        assert!(r.over_budget);
    });
    meter_provider.force_flush().unwrap();
    tracer_provider.force_flush().unwrap();

    let log_records = logs.0.lock().unwrap().len();
    let mut data_points = 0usize;
    let mut histograms = 0usize;
    for rm in metric_exporter.get_finished_metrics().unwrap() {
        for sm in rm.scope_metrics() {
            for m in sm.metrics() {
                match m.data() {
                    AggregatedMetrics::F64(MetricData::Histogram(h)) => {
                        let n = h.data_points().count();
                        histograms += n;
                        data_points += n;
                    }
                    AggregatedMetrics::U64(MetricData::Sum(s)) => {
                        data_points += s.data_points().count()
                    }
                    _ => {}
                }
            }
        }
    }
    let spans = span_exporter.get_finished_spans().unwrap().len();
    eprintln!(
        "telemetry validation: logs={log_records} metric_data_points={data_points} histograms={histograms} spans={spans}"
    );
    assert!(log_records >= 1, "FAIL: no log record emitted");
    assert!(data_points >= 1, "FAIL: no metric data point emitted");
    assert!(histograms >= 1, "FAIL: no histogram emitted");
    assert!(spans >= 1, "FAIL: no span emitted");
}
