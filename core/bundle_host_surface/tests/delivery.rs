//! Delivery-path proof against a real bound HTTP server (the svc-presentation stand-in):
//! the exact URL, the community-bound bearer, and the mapping of every refusal class to the
//! stable error codes. Nothing is mocked below the socket.

use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use axum::extract::State;
use axum::http::{HeaderMap, StatusCode};
use axum::response::IntoResponse;
use axum::routing::post;
use axum::Router;
use bundle_host_surface::{PushTokenSource, SurfaceError, SurfaceHost, SurfacePolicy};

type Seen = Arc<Mutex<Vec<(String, String)>>>;

struct Tokens(Result<&'static str, &'static str>);

#[async_trait]
impl PushTokenSource for Tokens {
    async fn token_for(&self, community_id: i64) -> Result<Arc<str>, String> {
        match self.0 {
            Ok(t) => Ok(Arc::from(format!("{t}-{community_id}"))),
            Err(e) => Err(e.to_string()),
        }
    }
}

async fn server(status: StatusCode, retry_after: Option<&'static str>) -> (String, Seen) {
    let seen: Seen = Arc::default();
    let app = Router::new()
        .route(
            "/internal/surfaces/{c}/{s}/publish",
            post(
                move |State(seen): State<Seen>, headers: HeaderMap, uri: axum::http::Uri| async move {
                    let auth = headers
                        .get("authorization")
                        .and_then(|v| v.to_str().ok())
                        .unwrap_or_default()
                        .to_string();
                    seen.lock()
                        .expect("lock")
                        .push((uri.path().to_string(), auth));
                    let mut r = (
                        status,
                        r#"{"error":"x","message":"nope","violations":[{"path":"/a","rule":"required"}]}"#,
                    )
                        .into_response();
                    if let Some(v) = retry_after {
                        r.headers_mut()
                            .insert("retry-after", v.parse().expect("header value"));
                    }
                    r
                },
            ),
        )
        .with_state(seen.clone());
    let l = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind");
    let addr = l.local_addr().expect("addr");
    tokio::spawn(async move { axum::serve(l, app).await.expect("serve") });
    (format!("http://{addr}"), seen)
}

fn policy() -> SurfacePolicy {
    SurfacePolicy::from_permissions(["overlay.surface.music.queue"])
}

#[tokio::test]
async fn success_posts_to_the_community_scoped_route_with_the_bound_bearer() {
    let (base, seen) = server(StatusCode::OK, None).await;
    let host = SurfaceHost::new(format!("{base}/"), Arc::new(Tokens(Ok("tok")))).expect("host");
    host.publish(
        &policy(),
        42,
        "music.queue",
        r#"{"queue":[],"updated_ms":1}"#,
    )
    .await
    .expect("ok");
    let seen = seen.lock().expect("lock");
    assert_eq!(seen.len(), 1);
    assert_eq!(seen[0].0, "/internal/surfaces/42/music.queue/publish");
    assert_eq!(seen[0].1, "Bearer tok-42");
}

#[tokio::test]
async fn every_refusal_class_maps_to_its_stable_code() {
    for (status, retry, code) in [
        (StatusCode::NOT_FOUND, None, "invalid"),
        (StatusCode::UNPROCESSABLE_ENTITY, None, "invalid"),
        (StatusCode::PAYLOAD_TOO_LARGE, None, "too_large"),
        (StatusCode::TOO_MANY_REQUESTS, Some("3"), "rate_limited"),
        (StatusCode::FORBIDDEN, None, "denied"),
        (StatusCode::BAD_GATEWAY, None, "unavailable"),
    ] {
        let (base, _) = server(status, retry).await;
        let host = SurfaceHost::new(base, Arc::new(Tokens(Ok("t")))).expect("host");
        let err = host
            .publish(&policy(), 1, "music.queue", "{}")
            .await
            .expect_err("refused");
        assert_eq!(err.code(), code, "{status}");
        if status == StatusCode::UNPROCESSABLE_ENTITY {
            assert!(err.to_string().contains("[/a:required]"), "{err}");
        }
        if let SurfaceError::RateLimited { retry_after_ms } = err {
            assert_eq!(retry_after_ms, 3000);
        }
    }
}

#[tokio::test]
async fn local_checks_never_reach_the_network() {
    let (base, seen) = server(StatusCode::OK, None).await;
    let host = SurfaceHost::new(base, Arc::new(Tokens(Ok("t")))).expect("host");
    let big = format!("\"{}\"", "x".repeat(20_000));
    let cases: [(&str, i64, &str, &str); 5] = [
        ("music.queue", 1, "not json", "invalid"),
        ("music.queue", 1, &big, "too_large"),
        ("alert", 1, "{}", "not_granted"),
        ("../etc", 1, "{}", "invalid"),
        ("music.queue", 0, "{}", "invalid"),
    ];
    for (surface, community, payload, code) in cases {
        let err = host
            .publish(&policy(), community, surface, payload)
            .await
            .expect_err("refused locally");
        assert_eq!(err.code(), code, "{surface} {payload:.10}");
    }
    assert!(
        seen.lock().expect("lock").is_empty(),
        "a refused call hit the network"
    );
}

#[tokio::test]
async fn token_and_transport_failures_are_unavailable_not_silent() {
    let (base, _) = server(StatusCode::OK, None).await;
    let host = SurfaceHost::new(base, Arc::new(Tokens(Err("hub-api down")))).expect("host");
    let err = host
        .publish(&policy(), 1, "music.queue", "{}")
        .await
        .expect_err("no token");
    assert_eq!(err.code(), "unavailable");

    let dead = SurfaceHost::new("http://127.0.0.1:1", Arc::new(Tokens(Ok("t")))).expect("host");
    let err = dead
        .publish(&policy(), 1, "music.queue", "{}")
        .await
        .expect_err("connection refused");
    assert_eq!(err.code(), "unavailable");
}
