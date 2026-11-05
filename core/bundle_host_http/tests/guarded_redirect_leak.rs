//! Real-path regression tests: a cross-origin redirect must never carry
//! custom credential headers or the request body to the new origin.

use std::net::{IpAddr, SocketAddr};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::extract::State;
use axum::http::{HeaderMap, StatusCode};
use axum::response::Redirect;
use axum::Router;
use bundle_host_http::guarded::{GuardedClient, GuardedLimits};

type Seen = Arc<Mutex<Vec<(Vec<String>, Vec<u8>)>>>;

async fn serve(app: Router) -> SocketAddr {
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let a = l.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(l, app).await.unwrap() });
    a
}

fn client() -> GuardedClient {
    let lo: IpAddr = "127.0.0.1".parse().unwrap();
    GuardedClient::new(
        GuardedLimits {
            max_redirects: 3,
            timeout: Duration::from_secs(5),
            max_response_bytes: 1024,
        },
        vec![lo],
    )
}

async fn capture(State(s): State<Seen>, h: HeaderMap, body: axum::body::Bytes) -> StatusCode {
    let names = h.keys().map(|k| k.to_string().to_lowercase()).collect();
    s.lock().unwrap().push((names, body.to_vec()));
    StatusCode::OK
}

// regression: cross-origin redirect leaked custom credential headers + body
#[tokio::test]
async fn cross_origin_redirect_forwards_only_safe_headers_and_no_body() {
    for code in [302u16, 303] {
        let seen: Seen = Arc::default();
        let b = serve(
            Router::new()
                .route("/t", axum::routing::any(capture))
                .with_state(seen.clone()),
        )
        .await;
        let x = serve(Router::new().route(
            "/x",
            axum::routing::any(move || async move {
                (
                    StatusCode::from_u16(code).unwrap(),
                    [("location", format!("http://{b}/t"))],
                )
            }),
        ))
        .await;
        let h = vec![
            ("X-API-Key".to_string(), "k-secret".to_string()),
            ("Client-Id".to_string(), "cid-secret".to_string()),
            ("Authorization".to_string(), "Bearer s".to_string()),
            ("Accept".to_string(), "application/json".to_string()),
        ];
        let r = client()
            .fetch(
                reqwest::Method::POST,
                &format!("http://{x}/x"),
                h,
                Some(b"body-secret".to_vec()),
            )
            .await
            .unwrap();
        assert_eq!(r.status, 200);
        let got = seen.lock().unwrap();
        assert_eq!(got.len(), 1, "{code}");
        for bad in ["x-api-key", "client-id", "authorization"] {
            assert!(!got[0].0.contains(&bad.to_string()), "{code} leaked {bad}");
        }
        assert!(got[0].0.contains(&"accept".to_string()), "{code}");
        assert!(got[0].1.is_empty(), "{code} body leaked");
    }
}

#[tokio::test]
async fn same_origin_307_keeps_body_and_credential_headers() {
    let seen: Seen = Arc::default();
    let a = serve(
        Router::new()
            .route("/t", axum::routing::any(capture))
            .route(
                "/r",
                axum::routing::any(|| async { Redirect::temporary("/t") }),
            )
            .with_state(seen.clone()),
    )
    .await;
    client()
        .fetch(
            reqwest::Method::POST,
            &format!("http://{a}/r"),
            vec![("X-API-Key".to_string(), "k".to_string())],
            Some(b"b".to_vec()),
        )
        .await
        .unwrap();
    let got = seen.lock().unwrap();
    assert_eq!(got[0].1, b"b");
    assert!(got[0].0.contains(&"x-api-key".to_string()));
}

// regression: gh-989 cross-origin 307/308 silently dropped the body -> now an
// explicit typed refusal, and the target origin sees no request at all.
#[tokio::test]
async fn cross_origin_307_308_with_body_is_refused_not_silently_stripped() {
    use bundle_host_http::guarded::GuardError;
    for code in [307u16, 308] {
        let seen: Seen = Arc::default();
        let b = serve(
            Router::new()
                .route("/t", axum::routing::any(capture))
                .with_state(seen.clone()),
        )
        .await;
        let x = serve(Router::new().route(
            "/x",
            axum::routing::any(move || async move {
                (
                    StatusCode::from_u16(code).unwrap(),
                    [("location", format!("http://{b}/t"))],
                )
            }),
        ))
        .await;
        let r = client()
            .fetch(
                reqwest::Method::POST,
                &format!("http://{x}/x"),
                vec![],
                Some(b"body-secret".to_vec()),
            )
            .await;
        assert_eq!(r.unwrap_err(), GuardError::BodyCrossOrigin, "{code}");
        assert!(
            seen.lock().unwrap().is_empty(),
            "{code}: request reached target"
        );
    }
}
