//! `HubTierResolver` / `HubCredentialResolver` against a fake hub-api.
#![allow(clippy::unwrap_used, clippy::expect_used)]

mod common;

use std::sync::atomic::{AtomicU16, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::extract::State;
use axum::http::{HeaderMap, StatusCode};
use axum::routing::post;
use axum::{Json, Router};
use bundle_host_media::auth::StaticBearer;
use bundle_host_media::credentials::{CredentialError, CredentialResolver, HubCredentialResolver};
use bundle_host_media::tier::{require_professional, HubTierResolver, TierError, TierResolver};
use bundle_host_media::{MediaError, Tier};
use common::scope;

#[derive(Default)]
struct Hub {
    status: AtomicU16,
    tier: Mutex<String>,
    tier_calls: AtomicUsize,
    tier_body: Mutex<serde_json::Value>,
    auth: Mutex<Vec<String>>,
    byok_body: Mutex<serde_json::Value>,
    byok_status: AtomicU16,
    byok_reply: Mutex<serde_json::Value>,
}

async fn tier(
    State(h): State<Arc<Hub>>,
    headers: HeaderMap,
    Json(b): Json<serde_json::Value>,
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
    *h.tier_body.lock().unwrap() = b;
    let st = h.status.load(Ordering::SeqCst);
    (
        StatusCode::from_u16(st).unwrap(),
        Json(serde_json::json!({"tier": *h.tier.lock().unwrap()})),
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
        .route("/api/v1/internal/media/effective-tier", post(tier))
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

#[tokio::test]
async fn hub_tier_request_shape_jwt_and_cache() {
    let (h, base) = hub().await;
    let r = tier_resolver(base, Duration::from_secs(60), Duration::from_secs(600));
    *h.tier.lock().unwrap() = "professional".into();
    assert_eq!(
        r.effective_tier(&scope("a")).await.unwrap(),
        Tier::Professional
    );
    assert_eq!(
        *h.tier_body.lock().unwrap(),
        serde_json::json!({"tenant_id": 7, "community_id": 42})
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
    *h.tier.lock().unwrap() = "free".into();
    assert_eq!(
        require_professional(&r, &scope("a")).await.unwrap_err(),
        MediaError::TierRequired("professional".into())
    );
}

#[tokio::test]
async fn unknown_tier_slug_fails_closed() {
    let (h, base) = hub().await;
    let r = tier_resolver(base, Duration::ZERO, Duration::from_secs(600));
    *h.tier.lock().unwrap() = "platinum".into();
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
    *h.tier.lock().unwrap() = "enterprise".into();
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
    *h.byok_reply.lock().unwrap() =
        serde_json::json!({"status": "ok", "tier": "professional", "api_key": "AIza-secret"});
    let key = c.resolve(&scope("a"), "gemini").await.unwrap();
    assert_eq!(key.expose(), "AIza-secret");
    assert_eq!(format!("{key:?}"), "ApiKey(<redacted>)");
    assert_eq!(
        *h.byok_body.lock().unwrap(),
        serde_json::json!({"tenant_id": 7, "community_id": 42, "provider": "gemini"})
    );
    assert_eq!(h.auth.lock().unwrap().last().unwrap(), "Bearer jwt-2");
    *h.byok_reply.lock().unwrap() =
        serde_json::json!({"status": "not_configured", "tier": "professional", "api_key": null});
    assert_eq!(
        c.resolve(&scope("a"), "gemini").await.unwrap_err(),
        CredentialError::NotConfigured
    );
    *h.byok_reply.lock().unwrap() =
        serde_json::json!({"status": "tier_required", "tier": "free", "api_key": null});
    assert_eq!(
        c.resolve(&scope("a"), "gemini").await.unwrap_err(),
        CredentialError::TierRequired
    );
    // ok without a key, unknown status, malformed body: fail closed.
    for bad in [
        serde_json::json!({"status": "ok", "tier": "professional", "api_key": null}),
        serde_json::json!({"status": "ok", "tier": "professional", "api_key": ""}),
        serde_json::json!({"status": "weird", "tier": "x", "api_key": null}),
        serde_json::json!({"nope": 1}),
    ] {
        *h.byok_reply.lock().unwrap() = bad;
        assert!(matches!(
            c.resolve(&scope("a"), "gemini").await,
            Err(CredentialError::Unavailable(_))
        ));
    }
    // Every documented error status is Unavailable, never NotConfigured.
    for st in [400u16, 401, 403, 404, 429, 500, 503] {
        h.byok_status.store(st, Ordering::SeqCst);
        *h.byok_reply.lock().unwrap() = serde_json::json!({"status": "not_configured"});
        assert!(
            matches!(
                c.resolve(&scope("a"), "gemini").await,
                Err(CredentialError::Unavailable(_))
            ),
            "{st}"
        );
    }
}
