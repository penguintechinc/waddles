//! JWT Phase-0 hardening through the real router and the REAL global meter
//! provider: HTTP status mapping (401 vs 403) plus the
//! `waddles_jwt_verifications_total{verifier,alg,outcome}` /
//! `waddles_jwt_verification_seconds{verifier,alg}` stream the Python
//! verifiers share (`docs/JWT_VERIFICATION.md`).
//!
//! One test function on purpose: the global provider is process-wide and the
//! instruments bind on first use.

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::body::Body;
use axum::http::header::AUTHORIZATION;
use axum::http::{Request, StatusCode};
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine as _;
use clap::Parser;
use jsonwebtoken::{Algorithm, EncodingKey};
use opentelemetry_sdk::error::OTelSdkResult;
use opentelemetry_sdk::metrics::data::{AggregatedMetrics, MetricData, ResourceMetrics};
use opentelemetry_sdk::metrics::exporter::PushMetricExporter;
use opentelemetry_sdk::metrics::{PeriodicReader, SdkMeterProvider, Temporality};
use serde_json::{json, Value};
use tower::ServiceExt;

use svc_streaming::config::{CliConfig, Config, Secret};
use svc_streaming::http::{router, AppState};

const SECRET: &str = "jwt-metrics-test-secret";

/// `(metric name, label name -> value, value)` for every exported point.
type Points = Arc<Mutex<Vec<(String, BTreeMap<String, String>, u64)>>>;

#[derive(Clone, Debug, Default)]
struct Snapshot(Points);

impl PushMetricExporter for Snapshot {
    async fn export(&self, metrics: &ResourceMetrics) -> OTelSdkResult {
        let mut out = Vec::new();
        for scope in metrics.scope_metrics() {
            for metric in scope.metrics() {
                let name = metric.name().to_string();
                match metric.data() {
                    AggregatedMetrics::U64(MetricData::Sum(sum)) => {
                        for dp in sum.data_points() {
                            let attrs = dp
                                .attributes()
                                .map(|kv| (kv.key.to_string(), kv.value.as_str().to_string()))
                                .collect();
                            out.push((name.clone(), attrs, dp.value()));
                        }
                    }
                    AggregatedMetrics::F64(MetricData::Histogram(hist)) => {
                        for dp in hist.data_points() {
                            let attrs = dp
                                .attributes()
                                .map(|kv| (kv.key.to_string(), kv.value.as_str().to_string()))
                                .collect();
                            out.push((name.clone(), attrs, dp.count()));
                        }
                    }
                    _ => {}
                }
            }
        }
        *self.0.lock().expect("snapshot lock") = out;
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

fn b64(value: &Value) -> String {
    URL_SAFE_NO_PAD.encode(value.to_string())
}

fn token(header: &Value, claims: &Value) -> String {
    let message = format!("{}.{}", b64(header), b64(claims));
    let signature = jsonwebtoken::crypto::sign(
        message.as_bytes(),
        &EncodingKey::from_secret(SECRET.as_bytes()),
        Algorithm::HS256,
    )
    .expect("sign");
    format!("{message}.{signature}")
}

fn state() -> AppState {
    let cli = CliConfig::try_parse_from(["svc-streaming"]).expect("defaults parse");
    AppState::new(
        Config {
            cli,
            db_password: Secret::new("db-pass"),
            cache_password: None,
            service_api_key: Secret::new("service-key"),
            jwt_hmac_secret: Some(Secret::new(SECRET)),
        },
        prometheus::Registry::new(),
    )
}

async fn status(app: &axum::Router, bearer: &str) -> StatusCode {
    app.clone()
        .oneshot(
            Request::builder()
                .uri("/api/v1/openapi.json")
                .header(AUTHORIZATION, format!("Bearer {bearer}"))
                .body(Body::empty())
                .expect("request"),
        )
        .await
        .expect("response")
        .status()
}

#[tokio::test]
async fn the_router_maps_rejections_and_reports_into_the_shared_metric_stream() {
    let snapshot = Snapshot::default();
    let provider = SdkMeterProvider::builder()
        .with_reader(
            PeriodicReader::builder(snapshot.clone())
                .with_interval(Duration::from_secs(3600))
                .build(),
        )
        .build();
    opentelemetry::global::set_meter_provider(provider.clone());

    let state = state();
    let (iss, aud) = (
        state.config.cli.jwt_issuer.clone(),
        state.config.cli.jwt_audience.clone(),
    );
    let app = router(state);
    let now = chrono::Utc::now().timestamp();
    let claims = json!({
        "sub": "user-1", "iss": iss, "aud": aud, "iat": now, "exp": now + 3600,
        "scope": "streaming:read", "tenant": "tenant-abc", "teams": [], "roles": [],
    });
    let header = json!({"alg": "HS256", "typ": "JWT", "kid": "hs256-v1"});

    // 200: valid.
    assert_eq!(status(&app, &token(&header, &claims)).await, StatusCode::OK);
    // 401: alg none (unsigned), alg confusion, key-material header, wrong key.
    let none = format!("{}.{}.", b64(&json!({"alg": "none"})), b64(&claims));
    assert_eq!(status(&app, &none).await, StatusCode::UNAUTHORIZED);
    let hs512 = jsonwebtoken::encode(
        &jsonwebtoken::Header::new(Algorithm::HS512),
        &claims,
        &EncodingKey::from_secret(SECRET.as_bytes()),
    )
    .expect("encode");
    assert_eq!(status(&app, &hs512).await, StatusCode::UNAUTHORIZED);
    let mut jku = header.clone();
    jku["jku"] = json!("https://attacker.example/keys");
    assert_eq!(
        status(&app, &token(&jku, &claims)).await,
        StatusCode::UNAUTHORIZED
    );
    // 403: validly signed but no tenant (never a default tenant).
    let mut no_tenant = claims.clone();
    no_tenant.as_object_mut().expect("object").remove("tenant");
    assert_eq!(
        status(&app, &token(&header, &no_tenant)).await,
        StatusCode::FORBIDDEN
    );
    // 401: expired by 5 s -- inside the library leeway, refused anyway (strict exp).
    let mut expired = claims.clone();
    expired["exp"] = json!(now - 5);
    assert_eq!(
        status(&app, &token(&header, &expired)).await,
        StatusCode::UNAUTHORIZED
    );

    provider.force_flush().expect("flush");
    let points = snapshot.0.lock().expect("snapshot lock").clone();
    let count = |alg: &str, outcome: &str| -> u64 {
        points
            .iter()
            .filter(|(name, attrs, _)| {
                name == "waddles_jwt_verifications_total"
                    && attrs.get("verifier").map(String::as_str) == Some("platform_hs256")
                    && attrs.get("alg").map(String::as_str) == Some(alg)
                    && attrs.get("outcome").map(String::as_str) == Some(outcome)
            })
            .map(|(_, _, value)| *value)
            .sum()
    };
    assert_eq!(count("hs256", "ok"), 1);
    assert_eq!(count("none", "alg_none"), 1);
    assert_eq!(count("hs512", "alg_mismatch"), 1);
    assert_eq!(count("hs256", "forbidden_header"), 1);
    assert_eq!(count("hs256", "missing_claim"), 1);
    assert_eq!(count("hs256", "expired"), 1);

    let total: u64 = points
        .iter()
        .filter(|(name, _, _)| name == "waddles_jwt_verifications_total")
        .map(|(_, _, value)| *value)
        .sum();
    assert_eq!(total, 6, "one sample per verification, none double-counted");
    for (name, attrs, _) in &points {
        let labels: Vec<&str> = attrs.keys().map(String::as_str).collect();
        match name.as_str() {
            "waddles_jwt_verifications_total" => {
                assert_eq!(
                    labels,
                    ["alg", "outcome", "verifier"],
                    "identical to Python"
                );
            }
            "waddles_jwt_verification_seconds" => {
                assert_eq!(labels, ["alg", "verifier"], "identical to Python");
            }
            _ => {}
        }
    }
    let observed: u64 = points
        .iter()
        .filter(|(name, _, _)| name == "waddles_jwt_verification_seconds")
        .map(|(_, _, value)| *value)
        .sum();
    assert_eq!(
        observed, 6,
        "every verification observes the latency histogram"
    );
}
