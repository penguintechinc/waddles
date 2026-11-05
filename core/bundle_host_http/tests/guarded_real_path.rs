//! Real-path tests for `guarded`: real local axum servers (loopback,
//! exact-ip allowlisted) that redirect at forbidden targets.

use std::net::{IpAddr, SocketAddr};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::extract::State;
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Redirect};
use axum::routing::get;
use axum::Router;
use bundle_host_http::guarded::{
    forbidden_reason, resolve_pinned, GuardError, GuardedClient, GuardedLimits,
};

type Seen = Arc<Mutex<Vec<Option<String>>>>;

async fn serve(app: Router) -> SocketAddr {
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let a = l.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(l, app).await.unwrap() });
    a
}

fn lo() -> Vec<IpAddr> {
    vec!["127.0.0.1".parse().unwrap()]
}

fn client(max_redirects: usize, max_bytes: usize) -> GuardedClient {
    GuardedClient::new(
        GuardedLimits {
            max_redirects,
            timeout: Duration::from_secs(5),
            max_response_bytes: max_bytes,
        },
        lo(),
    )
}

async fn get_to(c: &GuardedClient, url: &str) -> Result<u16, GuardError> {
    c.fetch(reqwest::Method::GET, url, vec![], None)
        .await
        .map(|r| r.status)
}

async fn echo(State(s): State<Seen>, h: HeaderMap) -> impl IntoResponse {
    s.lock().unwrap().push(
        h.get("authorization")
            .map(|v| v.to_str().unwrap().to_string()),
    );
    StatusCode::OK
}

#[tokio::test]
async fn redirect_to_metadata_and_loopback_is_refused() {
    let app = Router::new()
        .route(
            "/meta",
            get(|| async { Redirect::temporary("http://169.254.169.254/latest/meta-data") }),
        )
        .route(
            "/lo",
            get(|| async { Redirect::temporary("http://127.0.0.2:9/x") }),
        )
        .route(
            "/v6",
            get(|| async { Redirect::temporary("http://[::ffff:169.254.169.254]/x") }),
        )
        .route(
            "/dec",
            get(|| async { Redirect::temporary("http://2852039166/x") }),
        )
        .route(
            "/private",
            get(|| async { Redirect::temporary("http://10.0.0.5/x") }),
        )
        .route(
            "/down",
            get(|| async { Redirect::temporary("ftp://example.com/x") }),
        );
    let a = serve(app).await;
    let c = client(3, 1024);
    for p in ["meta", "lo", "v6", "dec", "private"] {
        let r = get_to(&c, &format!("http://{a}/{p}")).await;
        assert!(
            matches!(r, Err(GuardError::ForbiddenAddress(_))),
            "{p}: {r:?}"
        );
    }
    assert_eq!(
        get_to(&c, &format!("http://{a}/down")).await,
        Err(GuardError::SchemeNotAllowed)
    );
}

#[tokio::test]
async fn redirect_without_following_returns_3xx() {
    let app = Router::new().route(
        "/r",
        get(|| async { Redirect::temporary("http://169.254.169.254/") }),
    );
    let a = serve(app).await;
    assert_eq!(
        get_to(&client(0, 1024), &format!("http://{a}/r")).await,
        Ok(307)
    );
}

#[tokio::test]
async fn cross_origin_redirect_drops_authorization_same_origin_keeps_it() {
    let seen: Seen = Arc::default();
    let app = Router::new()
        .route("/echo", get(echo))
        .route("/same", get(|| async { Redirect::temporary("/echo") }))
        .with_state(seen.clone());
    let a = serve(app).await;
    let seen2: Seen = Arc::default();
    let b = serve(
        Router::new()
            .route("/echo", get(echo))
            .with_state(seen2.clone()),
    )
    .await;
    let x = serve(Router::new().route(
        "/x",
        get(move || async move { Redirect::temporary(&format!("http://{b}/echo")) }),
    ))
    .await;
    let c = client(3, 1024);
    let h = vec![("Authorization".to_string(), "Bearer sekrit".to_string())];
    let r = c
        .fetch(
            reqwest::Method::GET,
            &format!("http://{a}/same"),
            h.clone(),
            None,
        )
        .await
        .unwrap();
    assert_eq!(r.status, 200);
    assert_eq!(
        seen.lock().unwrap().as_slice(),
        [Some("Bearer sekrit".to_string())]
    );
    let r = c
        .fetch(reqwest::Method::GET, &format!("http://{x}/x"), h, None)
        .await
        .unwrap();
    assert_eq!(r.status, 200);
    assert_eq!(seen2.lock().unwrap().as_slice(), [None]);
}

#[tokio::test]
async fn redirect_loop_and_oversize_are_bounded() {
    let app = Router::new()
        .route("/loop", get(|| async { Redirect::temporary("/loop") }))
        .route("/big", get(|| async { "x".repeat(4096) }));
    let a = serve(app).await;
    let c = client(2, 1024);
    assert_eq!(
        get_to(&c, &format!("http://{a}/loop")).await,
        Err(GuardError::TooManyRedirects)
    );
    assert_eq!(
        get_to(&c, &format!("http://{a}/big")).await,
        Err(GuardError::ResponseTooLarge)
    );
}

#[tokio::test]
async fn slow_server_hits_deadline() {
    let app = Router::new().route(
        "/slow",
        get(|| async {
            tokio::time::sleep(Duration::from_secs(5)).await;
            "late"
        }),
    );
    let a = serve(app).await;
    let c = GuardedClient::new(
        GuardedLimits {
            max_redirects: 0,
            timeout: Duration::from_millis(200),
            max_response_bytes: 1024,
        },
        lo(),
    );
    let r = get_to(&c, &format!("http://{a}/slow")).await;
    assert!(
        matches!(r, Err(GuardError::Timeout | GuardError::Transport(_))),
        "{r:?}"
    );
}

#[tokio::test]
async fn default_policy_refuses_local_targets_and_bad_shapes() {
    let c = GuardedClient::new(GuardedLimits::default(), vec![]);
    for u in [
        "https://127.0.0.1/",
        "https://localhost/",
        "https://[::1]/",
        "https://[::ffff:127.0.0.1]/",
        "https://[64:ff9b::a9fe:a9fe]/",
        "https://0x7f000001/",
        "https://017700000001/",
        "https://2130706433/",
        "https://169.254.169.254:443/",
    ] {
        let r = get_to(&c, u).await;
        assert!(
            matches!(r, Err(GuardError::ForbiddenAddress(_))),
            "{u}: {r:?}"
        );
    }
    assert_eq!(
        get_to(&c, "http://8.8.8.8/").await,
        Err(GuardError::SchemeNotAllowed)
    );
    assert_eq!(
        get_to(&c, "ws://8.8.8.8/").await,
        Err(GuardError::SchemeNotAllowed)
    );
    assert_eq!(
        get_to(&c, "https://user:pw@8.8.8.8/").await,
        Err(GuardError::UserInfoForbidden)
    );
    assert_eq!(
        get_to(&c, "https://8.8.8.8@127.0.0.1/").await,
        Err(GuardError::UserInfoForbidden)
    );
    assert_eq!(get_to(&c, "not a url").await, Err(GuardError::MalformedUrl));
}

#[test]
fn error_display_never_carries_address_or_host() {
    let s = GuardError::ForbiddenAddress("cloud_metadata").to_string();
    assert!(!s.contains("169") && !s.contains("metadata"));
    for e in [
        GuardError::MalformedUrl,
        GuardError::SchemeNotAllowed,
        GuardError::UserInfoForbidden,
        GuardError::DnsTimeout,
        GuardError::DnsFailed,
        GuardError::NoAddress,
        GuardError::BadRedirect,
        GuardError::BodyCrossOrigin,
        GuardError::TooManyRedirects,
        GuardError::Timeout,
        GuardError::ResponseTooLarge,
        GuardError::Transport("k"),
    ] {
        assert!(!e.to_string().is_empty() && !e.class().is_empty());
    }
}

#[tokio::test]
async fn strict_ranges_and_pinning() {
    for ip in [
        "169.254.169.254",
        "10.1.2.3",
        "100.64.0.1",
        "192.0.2.1",
        "198.18.0.1",
        "240.0.0.1",
        "0.1.2.3",
        "::1",
        "fe80::1",
        "fc00::1",
        "fec0::1",
        "::ffff:10.0.0.1",
        "64:ff9b::a00:1",
        "2002:a9fe:a9fe::1",
        "2001:db8::1",
        "100::1",
        "64:ff9b:1::1",
        "3fff::1",
        "5f00::1",
        "::7f00:1",
    ] {
        assert!(forbidden_reason(ip.parse().unwrap()).is_some(), "{ip}");
    }
    for ip in ["8.8.8.8", "1.1.1.1", "2606:4700::1111"] {
        assert!(forbidden_reason(ip.parse().unwrap()).is_none(), "{ip}");
    }
    let u = |s: &str| reqwest::Url::parse(s).unwrap();
    assert_eq!(
        resolve_pinned(&u("https://8.8.8.8/"), &[])
            .await
            .unwrap()
            .port(),
        443
    );
    assert_eq!(
        resolve_pinned(&u("http://127.0.0.1:9/"), &lo())
            .await
            .unwrap()
            .port(),
        9
    );
}
