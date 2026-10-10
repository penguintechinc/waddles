//! Telemetry-emission coverage for the egress/external-call instruments
//! (`stream_external_call_duration_seconds`, `stream_relay_session_seconds`)
//! plus W3C trace-context propagation on outbound HTTP.
//!
//! Real code paths against local stand-ins: an in-process axum server plays
//! hub-api / the loopback `ingest-auth` route, an `object_store::InMemory`
//! plays the recordings bucket. Every test reads back from the in-memory
//! OTel sink (`tests/otel_common`) and asserts a **non-zero data-point
//! count** -- a gate that cannot fail on a silent exporter is not a gate.
//! Each instrument family is touched by exactly one test in this process, so
//! exact counts are stable despite the shared sink.

mod otel_common;

use std::net::SocketAddr;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::http::HeaderMap;
use axum::routing::post;
use axum::{Json, Router};
use clap::Parser;
use object_store::memory::InMemory;
use opentelemetry::trace::TraceContextExt as _;
use otel_common::{wait_for, OtelSink};
use tracing::Instrument as _;
use tracing_opentelemetry::OpenTelemetrySpanExt as _;

use svc_streaming::billing::token_ledger::TokenLedgerClient;
use svc_streaming::config::{CliConfig, Config, Secret};
use svc_streaming::egress::record::{register_metrics, RecordSink};
use svc_streaming::egress::relay::RelaySink;
use svc_streaming::egress::OutputSink;
use svc_streaming::pipeline::{ObjectStoreRef, OutputSpec};
use svc_streaming::rtc::ingest_auth::{InternalIngestAuthClient, WhipTokenAuthorizer};
use svc_streaming::store::SecretRef;
use svc_streaming::telemetry::stream::StreamMetrics;
use uuid::Uuid;

/// Spawns a one-route POST server returning `body`, recording every
/// request's headers. Returns its address and the captured headers.
async fn capture_server(
    route: &'static str,
    body: serde_json::Value,
) -> (SocketAddr, Arc<Mutex<Vec<HeaderMap>>>) {
    let captured: Arc<Mutex<Vec<HeaderMap>>> = Arc::new(Mutex::new(Vec::new()));
    let sink = captured.clone();
    let app = Router::new().route(
        route,
        post(move |headers: HeaderMap| {
            let sink = sink.clone();
            let body = body.clone();
            async move {
                sink.lock().unwrap().push(headers);
                Json(body)
            }
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    (addr, captured)
}

fn traceparent_trace_id(headers: &HeaderMap) -> String {
    let value = headers
        .get("traceparent")
        .expect("outbound request must carry a traceparent")
        .to_str()
        .unwrap();
    // `00-<32 hex trace id>-<16 hex span id>-<flags>`
    value
        .split('-')
        .nth(1)
        .expect("traceparent shape")
        .to_string()
}

#[tokio::test]
async fn token_ledger_debit_is_timed_and_propagates_the_callers_trace() {
    let sink = OtelSink::install();
    let metrics = StreamMetrics::with_meter(&sink.meter());
    let (addr, captured) = capture_server(
        "/api/v1/marketplace/communities/{community_id}/tokens/debit",
        serde_json::json!({"balance_after": 5}),
    )
    .await;
    let client = TokenLedgerClient::new().with_stream_metrics(metrics);

    let span = tracing::info_span!("test_transcode_admission");
    let trace_id = span.context().span().span_context().trace_id().to_string();
    let result = client
        .debit_transcoding_tokens(
            &format!("http://{addr}"),
            "bearer-jwt",
            7,
            3,
            "transcode",
            "ref-1",
        )
        .instrument(span)
        .await;
    assert!(result.ok, "debit succeeds against the mock: {result:?}");

    let headers = captured.lock().unwrap().pop().expect("one request");
    assert_eq!(
        traceparent_trace_id(&headers),
        trace_id,
        "hub-api must see the admission check's trace"
    );
    let (count, _) = sink.histogram(
        "stream_external_call_duration_seconds",
        &[("peer", "token_ledger"), ("outcome", "ok")],
    );
    println!("telemetry: token_ledger ok external-call data points: {count}");
    assert_eq!(count, 1);

    // An unreachable ledger is a transport error: the call still resolves
    // (BLOCK-WITH-FALLBACK) but is timed under outcome=error.
    let down = client
        .debit_transcoding_tokens(
            "http://127.0.0.1:1",
            "bearer-jwt",
            7,
            3,
            "transcode",
            "ref-2",
        )
        .await;
    assert!(!down.ok);
    let (errors, _) = sink.histogram(
        "stream_external_call_duration_seconds",
        &[("peer", "token_ledger"), ("outcome", "error")],
    );
    assert_eq!(errors, 1);
}

#[tokio::test]
async fn ingest_auth_call_is_timed_and_propagates_the_callers_trace() {
    let sink = OtelSink::install();
    let metrics = StreamMetrics::with_meter(&sink.meter());
    let (addr, captured) = capture_server(
        "/api/v1/internal/streaming/ingest-auth",
        serde_json::json!({
            "status": "success",
            "data": {"allowed": true, "community_id": null, "config_id": null},
            "meta": {"version": 1, "timestamp": "2026-10-09T00:00:00Z"}
        }),
    )
    .await;
    let cli = CliConfig::try_parse_from(["svc-streaming", "--http-port", &addr.port().to_string()])
        .expect("cli parses");
    let config = Config {
        cli,
        db_password: Secret::new("x"),
        cache_password: None,
        service_api_key: Secret::new("svc-key"),
        jwt_hmac_secret: None,
    };
    let client = InternalIngestAuthClient::new(&config)
        .expect("client builds")
        .with_stream_metrics(metrics);

    let span = tracing::info_span!("test_whip_authorize");
    let trace_id = span.context().span().span_context().trace_id().to_string();
    let allowed = client
        .authorize("whip-token")
        .instrument(span)
        .await
        .expect("authorize succeeds");
    assert!(allowed);

    let headers = captured.lock().unwrap().pop().expect("one request");
    assert_eq!(traceparent_trace_id(&headers), trace_id);
    assert_eq!(
        headers.get("x-service-key").map(|v| v.to_str().unwrap()),
        Some("svc-key"),
        "trace propagation must not displace the service credential"
    );
    let (count, _) = sink.histogram(
        "stream_external_call_duration_seconds",
        &[("peer", "ingest_auth"), ("outcome", "ok")],
    );
    println!("telemetry: ingest_auth ok external-call data points: {count}");
    assert_eq!(count, 1);
}

#[tokio::test(flavor = "multi_thread")]
async fn recording_upload_is_timed_as_an_object_store_call() {
    let sink = OtelSink::install();
    let metrics = StreamMetrics::with_meter(&sink.meter());
    let root =
        std::env::temp_dir().join(format!("svc-streaming-telemetry-record-{}", Uuid::new_v4()));
    let store: Arc<InMemory> = Arc::new(InMemory::new());
    let record = RecordSink::new(
        root.clone(),
        store as Arc<dyn object_store::ObjectStore>,
        register_metrics(&prometheus::Registry::new()),
    )
    .with_poll_interval(Duration::from_millis(30))
    .with_stream_metrics(metrics);

    let pipeline_id = Uuid::new_v4();
    record
        .start(
            pipeline_id,
            OutputSpec::Record {
                profile: "1080p60".into(),
                target: ObjectStoreRef {
                    store: "s3-recordings".into(),
                    prefix: "tenant-1/community-1".into(),
                },
            },
        )
        .await
        .expect("start");
    let dir = root
        .join("tenant-1/community-1")
        .join(pipeline_id.to_string());
    tokio::fs::create_dir_all(&dir).await.unwrap();
    tokio::fs::write(dir.join("20261009120000.ts"), vec![0xAB; 1024])
        .await
        .unwrap();
    tokio::time::sleep(Duration::from_millis(100)).await;
    // A newer file closes the first one; the watcher uploads it.
    tokio::fs::write(dir.join("20261009120100.ts"), vec![0xCD; 1024])
        .await
        .unwrap();

    wait_for(
        "the object-store upload call histogram to emit",
        Duration::from_secs(10),
        || {
            sink.histogram(
                "stream_external_call_duration_seconds",
                &[("peer", "object_store"), ("outcome", "ok")],
            )
            .0 >= 1
        },
    )
    .await;
    let (count, sum) = sink.histogram(
        "stream_external_call_duration_seconds",
        &[("peer", "object_store"), ("outcome", "ok")],
    );
    println!("telemetry: object_store ok external-call data points: {count}, sum={sum}");

    record.stop(pipeline_id).await.expect("stop");
    tokio::fs::remove_dir_all(&root).await.ok();
}

#[tokio::test]
async fn relay_targets_emit_one_session_lifetime_each_when_the_pipeline_stops() {
    let sink = OtelSink::install();
    let metrics = StreamMetrics::with_meter(&sink.meter());
    let rtmp_var = "SVC_STREAMING_TELEMETRY_TEST_RELAY_RTMP";
    let srt_var = "SVC_STREAMING_TELEMETRY_TEST_RELAY_SRT";
    // SAFETY: unique variable names used only by this test, set before any
    // await point; no other test in this file reads or writes them.
    unsafe {
        std::env::set_var(rtmp_var, "rtmp://live.example.com/app/secret-stream-key");
        std::env::set_var(srt_var, "srt://relay.example.com:9000?streamid=secret-id");
    }
    let relay = RelaySink::new().with_stream_metrics(metrics);
    let pipeline_id = Uuid::new_v4();
    relay
        .start(
            pipeline_id,
            OutputSpec::RtmpPush {
                url_secret_ref: SecretRef::Env {
                    var: rtmp_var.into(),
                },
            },
        )
        .await
        .expect("rtmp target starts");
    relay
        .start(
            pipeline_id,
            OutputSpec::SrtPush {
                url_secret_ref: SecretRef::Env {
                    var: srt_var.into(),
                },
            },
        )
        .await
        .expect("srt target starts");

    // Nothing is recorded while the targets are live.
    assert_eq!(sink.histogram("stream_relay_session_seconds", &[]).0, 0);

    tokio::time::sleep(Duration::from_millis(30)).await;
    relay.stop(pipeline_id).await.expect("stop");
    // Idempotent stop must not double-record.
    relay.stop(pipeline_id).await.expect("second stop");

    for kind in ["rtmp", "srt"] {
        let (count, sum) = sink.histogram("stream_relay_session_seconds", &[("kind", kind)]);
        println!("telemetry: relay session {kind}: {count} data point(s), sum={sum}");
        assert_eq!(count, 1, "{kind}: one lifetime sample per target");
        assert!(sum >= 0.03, "{kind}: target lived >= 30ms, recorded {sum}s");
    }
    // The label set is protocol-only: never the (secret) destination URL.
    for point in sink.points("stream_relay_session_seconds") {
        assert!(
            point.attrs.iter().all(|(_, v)| !v.contains("secret")),
            "relay attributes leaked a credential: {:?}",
            point.attrs
        );
    }

    unsafe {
        std::env::remove_var(rtmp_var);
        std::env::remove_var(srt_var);
    }
}
