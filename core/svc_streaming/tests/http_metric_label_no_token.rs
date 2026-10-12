//! Regression: the WHIP bearer token lives in the request path
//! (`/whip/<token>`, `/whip/<token>/<session>`). The HTTP metrics middleware
//! once used the raw `uri().path()` as the `path` label, writing every live
//! token into `/metrics` (secret disclosure to telemetry) and minting one
//! series per token (unbounded cardinality). The `TraceLayer` default span
//! carried the full `uri` the same way.
//!
//! Drives the **real** assembled router with WHIP requests carrying unique
//! token paths, then asserts:
//!
//! 1. the metric labels are the matched route *template*, never the token,
//! 2. unmatched paths collapse to a single fixed `unmatched` label,
//! 3. the token appears in neither the Prometheus exposition nor any
//!    rendered tracing span/event,
//!
//! with positive controls (templated series present, log lines captured) so
//! "no secret found" can never mean "nothing was examined".

mod log_capture;

use axum::body::Body;
use axum::http::header::CONTENT_TYPE;
use axum::http::{Request, StatusCode};
use clap::Parser;
use http_body_util::BodyExt;
use tower::ServiceExt;
use uuid::Uuid;

use log_capture::LogCapture;
use svc_streaming::config::{CliConfig, Config, Secret};
use svc_streaming::http::{metrics_router, router, AppState};

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

async fn scrape(state: AppState) -> String {
    let response = metrics_router(state)
        .oneshot(
            Request::builder()
                .uri("/metrics")
                .body(Body::empty())
                .expect("metrics request builds"),
        )
        .await
        .expect("metrics router responds");
    let body = response
        .into_body()
        .collect()
        .await
        .expect("metrics body collects")
        .to_bytes();
    String::from_utf8(body.to_vec()).expect("metrics exposition is utf-8")
}

// regression: whip-token-metric-label-leak
#[tokio::test]
async fn whip_token_never_reaches_metric_labels_or_spans() {
    let capture = LogCapture::install();
    let state = test_state();
    let app = router(state.clone());

    let token = "whip-bearer-regression-fixture";
    let session = Uuid::new_v4().to_string();
    let scanner_path = "/probe-scanner-7a1c9e3b-regression/secret-segment";

    // POST /whip/{token}: wrong content type -> 400 before any media work.
    let create = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("/whip/{token}"))
                .header(CONTENT_TYPE, "application/json")
                .body(Body::from("{}"))
                .expect("create request builds"),
        )
        .await
        .expect("create responds");
    assert_eq!(create.status(), StatusCode::BAD_REQUEST);

    // DELETE /whip/{token}/{session_id}: unknown session -> 404 (matched route).
    let delete = app
        .clone()
        .oneshot(
            Request::builder()
                .method("DELETE")
                .uri(format!("/whip/{token}/{session}"))
                .body(Body::empty())
                .expect("delete request builds"),
        )
        .await
        .expect("delete responds");
    assert_eq!(delete.status(), StatusCode::NOT_FOUND);

    // A path that matches no route at all.
    let missing = app
        .oneshot(
            Request::builder()
                .uri(scanner_path)
                .body(Body::empty())
                .expect("unmatched request builds"),
        )
        .await
        .expect("unmatched responds");
    assert_eq!(missing.status(), StatusCode::NOT_FOUND);

    let rendered = scrape(state).await;
    let series = rendered
        .lines()
        .filter(|l| l.starts_with("svc_streaming_http_request"))
        .count();
    println!("metric-label: examined {series} http request series line(s)");
    assert!(
        series > 0,
        "no http request series -- the check proved nothing"
    );

    // Positive controls: the templated labels are what is exposed.
    assert!(
        rendered.contains("route=\"/whip/{token}\""),
        "expected templated WHIP create route label:\n{rendered}"
    );
    assert!(
        rendered.contains("method=\"POST\",route=\"/whip/{token}\""),
        "expected POST create series:\n{rendered}"
    );
    assert!(
        rendered.contains("method=\"DELETE\",route=\"/whip/{token}/{session_id}\""),
        "expected DELETE session series:\n{rendered}"
    );
    assert!(
        rendered.contains("route=\"unmatched\""),
        "unmatched paths must collapse to one fixed label:\n{rendered}"
    );

    // The regression itself: no live path fragment in any label.
    assert!(!rendered.contains(token), "WHIP token leaked into /metrics");
    assert!(
        !rendered.contains(&session),
        "session id leaked into /metrics"
    );
    assert!(
        !rendered.contains("probe-scanner-7a1c9e3b-regression"),
        "raw unmatched path leaked into /metrics (unbounded cardinality)"
    );
    assert!(
        !rendered.contains("path=\""),
        "a raw `path` label is back on the HTTP metrics:\n{rendered}"
    );

    // Same guarantee for spans/logs: TraceLayer must not render the URI.
    capture.assert_no_secret_leak(
        &[token, "probe-scanner-7a1c9e3b-regression"],
        &["route=/whip/{token}"],
    );
}
