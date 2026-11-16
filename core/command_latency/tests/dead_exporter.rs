//! A dead OTLP exporter must never fail or slow a command.
//!
//! Metrics AND traces are exported over OTLP/gRPC to a CLOSED port (nothing listens on 127.0.0.1:1),
//! the exact failure mode of a collector outage. The command path -- every record helper, every span
//! -- is exercised in a hot loop: no panic, every call returns, and the MEDIAN call stays in the
//! microsecond range. The median (not the max) is asserted because the host this runs on is often
//! heavily loaded by other jobs: absolute worst-case timings measured under load prove nothing, a
//! median of thousands of calls does. The measured numbers are printed.

use std::time::{Duration, Instant};

use command_latency::stage::{self, Kind};
use command_latency::stamp::{now_ms, Door, Stamps};
use command_latency::{Hop, Outcome};
use opentelemetry::trace::TracerProvider as _;
use opentelemetry_otlp::WithExportConfig as _;
use opentelemetry_sdk::metrics::{PeriodicReader, SdkMeterProvider};
use opentelemetry_sdk::trace::SdkTracerProvider;
use tracing_subscriber::layer::SubscriberExt as _;

const DEAD: &str = "http://127.0.0.1:1";
const N: usize = 3_000;

#[tokio::test(flavor = "multi_thread")]
async fn commands_are_unaffected_by_a_dead_otlp_collector() {
    command_latency::switch::set_disabled(false);

    let metric_exporter = opentelemetry_otlp::MetricExporter::builder()
        .with_tonic()
        .with_endpoint(DEAD)
        .build()
        .expect("exporter construction never needs the collector");
    let reader = PeriodicReader::builder(metric_exporter)
        .with_interval(Duration::from_millis(50))
        .build();
    let meter_provider = SdkMeterProvider::builder().with_reader(reader).build();
    opentelemetry::global::set_meter_provider(meter_provider.clone());

    let span_exporter = opentelemetry_otlp::SpanExporter::builder()
        .with_tonic()
        .with_endpoint(DEAD)
        .build()
        .expect("exporter construction never needs the collector");
    let tracer_provider = SdkTracerProvider::builder()
        .with_batch_exporter(span_exporter)
        .build();
    let subscriber = tracing_subscriber::registry().with(
        tracing_opentelemetry::layer().with_tracer(tracer_provider.tracer("dead-exporter-test")),
    );

    let ts = Stamps {
        t0_ms: Some(now_ms()),
        door: Some(Door::Gateway),
        command: Some("ping".into()),
        ..Stamps::default()
    }
    .merge_into_tracestate(None);

    let mut per_call_us: Vec<u128> = Vec::with_capacity(N);
    let wall = Instant::now();
    tracing::subscriber::with_default(subscriber, || {
        for i in 0..N {
            let t = Instant::now();
            let span = tracing::info_span!("action.dispatch", app_id = "a", tenant_id = 1_i64);
            let _g = span.enter();
            command_latency::metrics::metrics()
                .record_hop(Hop::QueueWait, Duration::from_millis(5));
            let now = now_ms();
            let _ = stage::report_confirmed(
                Kind::User,
                Some(&ts),
                "waddles.core.example.ping",
                1,
                1,
                if i % 2 == 0 {
                    Outcome::Ok
                } else {
                    Outcome::Timeout
                },
                now,
                now + 3,
            );
            per_call_us.push(t.elapsed().as_micros());
        }
    });
    let total = wall.elapsed();

    per_call_us.sort_unstable();
    let median = per_call_us[N / 2];
    let p99 = per_call_us[N * 99 / 100];
    eprintln!(
        "dead exporter: {N} commands in {total:?}; per-call median {median} us, p99 {p99} us \
         (timings measured on a loaded host are indicative only)"
    );
    assert!(
        median < 5_000,
        "median per-call {median} us: a dead exporter must not sit on the command path"
    );

    // Shutting the providers down against a dead collector must not panic either (it may report an
    // export error, which is the contract: telemetry failure is never a request failure).
    let _ = meter_provider.shutdown();
    let _ = tracer_provider.shutdown();
}
