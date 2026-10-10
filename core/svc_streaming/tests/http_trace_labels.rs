//! Regression: the control-plane router must never publish a credential into
//! telemetry. WHIP/WHEP tokens ride in the URL path (`POST /whip/{token}`),
//! and two surfaces used the raw path -- the Prometheus `path` label and
//! tower-http's default span `uri` attribute -- which put the token on the
//! `/metrics` scrape surface and into every exported trace (and minted one
//! series per token / per scanner probe).
//!
//! Also proves an incoming W3C `traceparent` parents the request span (the
//! inbound half of cross-service trace propagation), reading both signals
//! back from the in-memory OTel sink (`tests/otel_common`).

mod otel_common;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use clap::Parser;
use http_body_util::BodyExt;
use opentelemetry::trace::{SpanId, TraceId};
use tower::ServiceExt;

use svc_streaming::config::{CliConfig, Config, Secret};
use svc_streaming::http::{metrics_router, router, AppState};

const SECRET_TOKEN: &str = "sk_live_SECRET_WHIP_TOKEN_9d41";
const TRACE_ID: &str = "0af7651916cd43dd8448eb211c80319c";
const PARENT_SPAN_ID: &str = "b7ad6b7169203331";

fn test_state() -> AppState {
    let cli = CliConfig::try_parse_from(["svc-streaming"]).expect("defaults parse");
    let config = Config {
        cli,
        db_password: Secret::new("db-pass"),
        cache_password: None,
        service_api_key: Secret::new("service-key"),
        jwt_hmac_secret: Some(Secret::new("hmac-secret")),
    };
    AppState::new(config, prometheus::Registry::new())
}

async fn get(app: axum::Router, uri: &str, traceparent: Option<&str>) -> StatusCode {
    let mut request = Request::builder().uri(uri);
    if let Some(value) = traceparent {
        request = request.header("traceparent", value);
    }
    app.oneshot(request.body(Body::empty()).unwrap())
        .await
        .unwrap()
        .status()
}

#[tokio::test]
async fn route_template_not_raw_path_labels_metrics_and_spans() {
    let otel = otel_common::OtelSink::install();
    let state = test_state();
    let app = router(state.clone());

    let parent = format!("00-{TRACE_ID}-{PARENT_SPAN_ID}-01");
    assert_eq!(
        get(app.clone(), "/health", Some(&parent)).await,
        StatusCode::OK
    );
    // GET on a POST-only WHIP route: matched path, 405 -- the token is in
    // the URL but must appear nowhere in telemetry.
    assert_eq!(
        get(app.clone(), &format!("/whip/{SECRET_TOKEN}"), None).await,
        StatusCode::METHOD_NOT_ALLOWED
    );
    // No route at all.
    assert_eq!(
        get(app.clone(), "/scanner/probe/.env", None).await,
        StatusCode::NOT_FOUND
    );

    // --- Prometheus `/metrics`: template labels, no secret, bounded set.
    let response = metrics_router(state)
        .oneshot(
            Request::builder()
                .uri("/metrics")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let body = response.into_body().collect().await.unwrap().to_bytes();
    let rendered = String::from_utf8(body.to_vec()).unwrap();
    println!(
        "telemetry: /metrics exposes {} http_requests_total series",
        rendered
            .lines()
            .filter(|l| l.starts_with("svc_streaming_http_requests_total{"))
            .count()
    );
    assert!(
        !rendered.contains(SECRET_TOKEN) && !rendered.contains("scanner/probe"),
        "raw request path leaked into the /metrics scrape surface:\n{rendered}"
    );
    assert!(rendered.contains(r#"path="/whip/{token}""#), "{rendered}");
    assert!(rendered.contains(r#"path="unmatched""#), "{rendered}");
    assert!(rendered.contains(r#"path="/health""#), "{rendered}");

    // --- Spans: route template, no secret, parented to the traceparent.
    let spans = otel.spans();
    println!(
        "telemetry: {} span(s) exported by the router run",
        spans.len()
    );
    assert!(!spans.is_empty(), "no spans exported at all");
    for span in &spans {
        let attrs = otel_common::span_attrs(span);
        assert!(
            !span.name.contains(SECRET_TOKEN)
                && !span.name.contains("scanner/probe")
                && attrs
                    .iter()
                    .all(|a| !a.contains(SECRET_TOKEN) && !a.contains("scanner/probe")),
            "raw request path leaked into span `{}`: {attrs:?}",
            span.name
        );
    }
    let health = spans
        .iter()
        .find(|s| s.name == "GET /health")
        .expect("a `GET /health` request span");
    assert_eq!(
        health.span_context.trace_id(),
        TraceId::from_hex(TRACE_ID).unwrap(),
        "the request joins the caller's trace"
    );
    assert_eq!(
        health.parent_span_id,
        SpanId::from_hex(PARENT_SPAN_ID).unwrap()
    );
    assert!(spans.iter().any(|s| s.name == "GET /whip/{token}"));
    assert!(spans.iter().any(|s| s.name == "GET unmatched"));
}
