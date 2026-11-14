//! One distributed trace per command: the first stage mints the envelope `traceparent` from its own
//! span, later stages parent on it, and errors carry type + message with an error status.
//!
//! Runs against an in-memory span exporter with the real `tracing-opentelemetry` bridge (the same
//! layer `penguin-logging` installs), so the span ids the stages exchange are real OTel ids.

use command_latency::span::{format_traceparent, record_error, set_remote_parent, span_ids};
use opentelemetry::trace::{Status, TracerProvider as _};
use opentelemetry_sdk::trace::{InMemorySpanExporter, SdkTracerProvider};
use tracing_subscriber::layer::SubscriberExt as _;

fn attr(span: &opentelemetry_sdk::trace::SpanData, key: &str) -> Option<String> {
    span.attributes
        .iter()
        .find(|kv| kv.key.as_str() == key)
        .map(|kv| kv.value.to_string())
}

#[test]
fn stages_join_one_trace_and_errors_are_recorded_on_the_span() {
    let exporter = InMemorySpanExporter::default();
    let provider = SdkTracerProvider::builder()
        .with_simple_exporter(exporter.clone())
        .build();
    let subscriber = tracing_subscriber::registry()
        .with(tracing_opentelemetry::layer().with_tracer(provider.tracer("trace-propagation-test")));

    tracing::subscriber::with_default(subscriber, || {
        // ---- ingest: the publish span's own ids become the envelope traceparent ------------
        let ingest = tracing::info_span!("ingest.publish", platform = "discord", tenant_id = 7_i64);
        let (trace_id, ingest_span_id, sampled) =
            span_ids(&ingest).expect("a tracer backs the span, so it has real ids");
        assert!(sampled);
        let traceparent = format_traceparent(&trace_id, &ingest_span_id, sampled);
        let tracestate = "waddles=t0:1;d:g";
        drop(ingest);

        // ---- process: remote parent from the envelope, children inherit it ------------------
        let process = tracing::info_span!("process.consume", app_id = "waddles.core.example.ping");
        assert!(set_remote_parent(&process, &traceparent, Some(tracestate)));
        {
            let _g = process.enter();
            let tokenize = tracing::info_span!("process.tokenize");
            drop(tokenize);
        }
        record_error(&process, "DeadLettered", "entry dead-lettered by the process stage");
        drop(process);

        // ---- action: same remote parent ----------------------------------------------------
        let action = tracing::info_span!("action.dispatch", app_id = "waddles.core.example.ping");
        assert!(set_remote_parent(&action, &traceparent, Some(tracestate)));
        drop(action);
    });
    provider.force_flush().unwrap();

    let spans = exporter.get_finished_spans().unwrap();
    let names: Vec<_> = spans.iter().map(|s| s.name.to_string()).collect();
    eprintln!("exported spans: {names:?}");
    for want in ["ingest.publish", "process.consume", "process.tokenize", "action.dispatch"] {
        assert!(names.iter().any(|n| n == want), "span {want} not exported: {names:?}");
    }
    let by = |n: &str| spans.iter().find(|s| s.name == n).unwrap();

    let ingest = by("ingest.publish");
    let trace_id = ingest.span_context.trace_id();
    for n in ["process.consume", "process.tokenize", "action.dispatch"] {
        assert_eq!(
            by(n).span_context.trace_id(),
            trace_id,
            "{n} must be in the ingest trace"
        );
    }
    assert_eq!(
        by("process.consume").parent_span_id,
        ingest.span_context.span_id(),
        "process.consume is a child of ingest.publish via the envelope traceparent"
    );
    assert_eq!(
        by("action.dispatch").parent_span_id,
        ingest.span_context.span_id()
    );
    assert_eq!(
        by("process.tokenize").parent_span_id,
        by("process.consume").span_context.span_id(),
        "an in-stage child span nests under the stage span"
    );

    // Errors: type + message attributes and an error status; healthy spans stay unset.
    let failed = by("process.consume");
    assert_eq!(attr(failed, "exception.type").as_deref(), Some("DeadLettered"));
    assert!(attr(failed, "exception.message")
        .is_some_and(|m| m.contains("dead-lettered")));
    assert!(matches!(failed.status, Status::Error { .. }));
    assert!(!matches!(by("action.dispatch").status, Status::Error { .. }));

    // Attributes stay bounded and non-PII: numeric ids and slugs only.
    assert_eq!(attr(ingest, "tenant_id").as_deref(), Some("7"));
}
