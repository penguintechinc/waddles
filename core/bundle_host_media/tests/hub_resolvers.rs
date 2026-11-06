//! `HubTierResolver` / `HubCredentialResolver` against a fake hub-api.
#![allow(clippy::unwrap_used, clippy::expect_used)]

mod common;

use std::sync::atomic::{AtomicU16, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::extract::{Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::routing::{get, post};
use axum::{Json, Router};
use bundle_host_media::auth::StaticBearer;
use bundle_host_media::credentials::{CredentialError, CredentialResolver, HubCredentialResolver};
use bundle_host_media::tier::{
    effective, require_professional, HubTierResolver, TierError, TierResolver,
};
use bundle_host_media::{MediaError, Tier};
use common::scope;

#[derive(Default)]
struct Hub {
    status: AtomicU16,
    tenant: Mutex<String>,
    community: Mutex<String>,
    tier_calls: AtomicUsize,
    auth: Mutex<Vec<String>>,
    byok_body: Mutex<serde_json::Value>,
    byok_status: AtomicU16,
    byok_reply: Mutex<serde_json::Value>,
}

async fn tier(
    State(h): State<Arc<Hub>>,
    headers: HeaderMap,
    Query(q): Query<std::collections::HashMap<String, String>>,
) -> (StatusCode, Json<serde_json::Value>) {
    h.tier_calls.fetch_add(1, Ordering::SeqCst);
    h.auth.lock().unwrap().push(
        headers
            .get("authorization")
            .unwrap()
            .to_str()
            .unwrap()
            .to_string(),
    );
    assert_eq!(q["tenant_id"], "7");
    assert_eq!(q["community_id"], "42");
    let st = h.status.load(Ordering::SeqCst);
    (
        StatusCode::from_u16(st).unwrap(),
        Json(
            serde_json::json!({"tenant_tier": *h.tenant.lock().unwrap(), "community_tier": *h.community.lock().unwrap()}),
        ),
    )
}

async fn byok(
    State(h): State<Arc<Hub>>,
    headers: HeaderMap,
    Json(b): Json<serde_json::Value>,
) -> (StatusCode, Json<serde_json::Value>) {
    h.auth.lock().unwrap().push(
        headers
            .get("authorization")
            .unwrap()
            .to_str()
            .unwrap()
            .to_string(),
    );
    *h.byok_body.lock().unwrap() = b;
    (
        StatusCode::from_u16(h.byok_status.load(Ordering::SeqCst)).unwrap(),
        Json(h.byok_reply.lock().unwrap().clone()),
    )
}

async fn hub() -> (Arc<Hub>, String) {
    let h = Arc::new(Hub::default());
    h.status.store(200, Ordering::SeqCst);
    let app = Router::new()
        .route("/api/v1/internal/media/tier", get(tier))
        .route("/api/v1/internal/media/byok/resolve", post(byok))
        .with_state(h.clone());
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(l, app).await.unwrap() });
    (h, format!("http://{addr}"))
}

fn tier_resolver(base: String, fresh: Duration, grace: Duration) -> HubTierResolver {
    HubTierResolver::new(
        reqwest::Client::new(),
        base,
        Arc::new(StaticBearer("jwt-1".into())),
        fresh,
        grace,
    )
}

#[test]
fn effective_tier_is_the_max_of_tenant_and_community() {
    use Tier::*;
    for (t, c, want) in [
        (Free, Free, Free),
        (Free, Professional, Professional),
        (Professional, Free, Professional),
        (Enterprise, Free, Enterprise),
        (Free, Enterprise, Enterprise),
        (Professional, Enterprise, Enterprise),
    ] {
        assert_eq!(effective(t, c), want, "{t:?}/{c:?}");
    }
}

#[tokio::test]
async fn hub_tier_lifts_via_community_and_sends_machine_jwt() {
    let (h, base) = hub().await;
    let r = tier_resolver(base, Duration::from_secs(60), Duration::from_secs(600));
    *h.tenant.lock().unwrap() = "free".into();
    *h.community.lock().unwrap() = "professional".into();
    assert_eq!(
        r.effective_tier(&scope("a")).await.unwrap(),
        Tier::Professional
    );
    assert_eq!(h.auth.lock().unwrap()[0], "Bearer jwt-1");
    assert!(require_professional(&r, &scope("a")).await.is_ok());
    // Cached within the fresh TTL: no second hub call.
    r.effective_tier(&scope("a")).await.unwrap();
    assert_eq!(h.tier_calls.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn free_tenant_and_free_community_is_refused() {
    let (h, base) = hub().await;
    let r = tier_resolver(base, Duration::from_secs(60), Duration::from_secs(600));
    *h.tenant.lock().unwrap() = "free".into();
    *h.community.lock().unwrap() = "free".into();
    assert_eq!(
        require_professional(&r, &scope("a")).await.unwrap_err(),
        MediaError::TierRequired("professional".into())
    );
}

#[tokio::test]
async fn unknown_tier_slug_fails_closed() {
    let (h, base) = hub().await;
    let r = tier_resolver(base, Duration::ZERO, Duration::from_secs(600));
    *h.tenant.lock().unwrap() = "platinum".into();
    *h.community.lock().unwrap() = "professional".into();
    assert_eq!(
        r.effective_tier(&scope("a")).await.unwrap_err(),
        TierError::Unknown
    );
    assert_eq!(
        require_professional(&r, &scope("a")).await.unwrap_err(),
        MediaError::TierRequired("license_unknown".into())
    );
}

#[tokio::test]
async fn hub_down_never_seen_refuses_but_last_known_serves_within_grace() {
    let (h, base) = hub().await;
    // Never seen + hub error => refuse.
    h.status.store(503, Ordering::SeqCst);
    let r = tier_resolver(base.clone(), Duration::ZERO, Duration::from_secs(600));
    assert!(matches!(
        r.effective_tier(&scope("a")).await,
        Err(TierError::Unavailable(_))
    ));
    assert_eq!(
        require_professional(&r, &scope("a")).await.unwrap_err(),
        MediaError::TierRequired("license_unknown".into())
    );
    // Seen, then hub down => last-known within grace.
    h.status.store(200, Ordering::SeqCst);
    *h.tenant.lock().unwrap() = "enterprise".into();
    *h.community.lock().unwrap() = "free".into();
    assert_eq!(
        r.effective_tier(&scope("a")).await.unwrap(),
        Tier::Enterprise
    );
    h.status.store(500, Ordering::SeqCst);
    assert_eq!(
        r.effective_tier(&scope("a")).await.unwrap(),
        Tier::Enterprise
    );
    // Grace expired => refuse again.
    let strict = tier_resolver(base, Duration::ZERO, Duration::ZERO);
    h.status.store(200, Ordering::SeqCst);
    strict.effective_tier(&scope("a")).await.unwrap();
    tokio::time::sleep(Duration::from_millis(5)).await;
    h.status.store(500, Ordering::SeqCst);
    assert!(strict.effective_tier(&scope("a")).await.is_err());
}

#[tokio::test]
async fn unreachable_hub_is_unavailable() {
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    drop(l);
    let r = tier_resolver(format!("http://{addr}"), Duration::ZERO, Duration::ZERO);
    assert!(matches!(
        r.effective_tier(&scope("a")).await,
        Err(TierError::Unavailable(_))
    ));
    let c = HubCredentialResolver::new(
        reqwest::Client::new(),
        format!("http://{addr}"),
        Arc::new(StaticBearer("j".into())),
    );
    assert!(matches!(
        c.resolve(&scope("a"), "gemini").await,
        Err(CredentialError::Unavailable(_))
    ));
}

#[tokio::test]
async fn byok_resolver_contract() {
    let (h, base) = hub().await;
    let c = HubCredentialResolver::new(
        reqwest::Client::new(),
        base,
        Arc::new(StaticBearer("jwt-2".into())),
    );
    h.byok_status.store(200, Ordering::SeqCst);
    *h.byok_reply.lock().unwrap() = serde_json::json!({"api_key": "AIza-secret"});
    let key = c.resolve(&scope("a"), "gemini").await.unwrap();
    assert_eq!(key.expose(), "AIza-secret");
    assert_eq!(format!("{key:?}"), "ApiKey(<redacted>)");
    assert_eq!(
        *h.byok_body.lock().unwrap(),
        serde_json::json!({"tenant_id": 7, "community_id": 42, "provider": "gemini"})
    );
    assert_eq!(h.auth.lock().unwrap().last().unwrap(), "Bearer jwt-2");
    // Typed not-configured shapes.
    *h.byok_reply.lock().unwrap() = serde_json::json!({"api_key": ""});
    assert_eq!(
        c.resolve(&scope("a"), "gemini").await.unwrap_err(),
        CredentialError::NotConfigured
    );
    h.byok_status.store(404, Ordering::SeqCst);
    assert_eq!(
        c.resolve(&scope("a"), "gemini").await.unwrap_err(),
        CredentialError::NotConfigured
    );
    h.byok_status.store(409, Ordering::SeqCst);
    *h.byok_reply.lock().unwrap() = serde_json::json!({"code": "not_configured"});
    assert_eq!(
        c.resolve(&scope("a"), "gemini").await.unwrap_err(),
        CredentialError::NotConfigured
    );
    *h.byok_reply.lock().unwrap() = serde_json::json!({"error": "byok_not_configured"});
    assert_eq!(
        c.resolve(&scope("a"), "gemini").await.unwrap_err(),
        CredentialError::NotConfigured
    );
    // Anything else is unavailable, never "not configured".
    h.byok_status.store(500, Ordering::SeqCst);
    *h.byok_reply.lock().unwrap() = serde_json::json!({"x": 1});
    assert!(matches!(
        c.resolve(&scope("a"), "gemini").await,
        Err(CredentialError::Unavailable(_))
    ));
    h.byok_status.store(200, Ordering::SeqCst);
    *h.byok_reply.lock().unwrap() = serde_json::json!({"nope": 1});
    assert!(matches!(
        c.resolve(&scope("a"), "gemini").await,
        Err(CredentialError::Unavailable(_))
    ));
}
