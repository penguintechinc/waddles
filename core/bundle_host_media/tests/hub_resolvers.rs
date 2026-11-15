//! `HubTierResolver` / `HubCredentialResolver` against a REAL in-process tonic `MediaService`
//! on loopback (the generated server trait, a real lazy `Channel`): request shape, bearer
//! metadata, tier cache + grace, and fail-closed handling of every error status.
#![allow(clippy::unwrap_used, clippy::expect_used)]

mod common;

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use bundle_host_media::auth::StaticBearer;
use bundle_host_media::credentials::{CredentialError, CredentialResolver, HubCredentialResolver};
use bundle_host_media::tier::{require_professional, HubTierResolver, TierError, TierResolver};
use bundle_host_media::{MediaError, Tier};
use common::scope;
use hub_client::lazy_channel;
use hub_client::pb::media_service_server::{MediaService, MediaServiceServer};
use hub_client::pb::{
    ByokStatus, GetEffectiveTierRequest, GetEffectiveTierResponse, MediaTier, ResolveByokRequest,
    ResolveByokResponse,
};
use tonic::{Code, Request, Response, Status};

const DEADLINE: Duration = Duration::from_secs(2);

/// Scriptable fake hub: each RPC answers `Err(code)` when set, else the scripted reply.
#[derive(Default)]
struct Hub {
    tier: Mutex<i32>,
    tier_error: Mutex<Option<Code>>,
    tier_calls: AtomicUsize,
    tier_request: Mutex<Option<GetEffectiveTierRequest>>,
    auth: Mutex<Vec<String>>,
    byok_request: Mutex<Option<ResolveByokRequest>>,
    byok_error: Mutex<Option<Code>>,
    byok_reply: Mutex<ResolveByokResponse>,
}

fn bearer<T>(r: &Request<T>) -> String {
    r.metadata()
        .get("authorization")
        .unwrap()
        .to_str()
        .unwrap()
        .to_string()
}

struct Svc(Arc<Hub>);

#[tonic::async_trait]
impl MediaService for Svc {
    async fn resolve_byok(
        &self,
        request: Request<ResolveByokRequest>,
    ) -> Result<Response<ResolveByokResponse>, Status> {
        let h = &self.0;
        h.auth.lock().unwrap().push(bearer(&request));
        *h.byok_request.lock().unwrap() = Some(request.into_inner());
        if let Some(code) = *h.byok_error.lock().unwrap() {
            return Err(Status::new(code, "SECRET-LOOKING-server-text"));
        }
        Ok(Response::new(h.byok_reply.lock().unwrap().clone()))
    }

    async fn get_effective_tier(
        &self,
        request: Request<GetEffectiveTierRequest>,
    ) -> Result<Response<GetEffectiveTierResponse>, Status> {
        let h = &self.0;
        h.tier_calls.fetch_add(1, Ordering::SeqCst);
        h.auth.lock().unwrap().push(bearer(&request));
        *h.tier_request.lock().unwrap() = Some(request.into_inner());
        if let Some(code) = *h.tier_error.lock().unwrap() {
            return Err(Status::new(code, "SECRET-LOOKING-server-text"));
        }
        Ok(Response::new(GetEffectiveTierResponse {
            tier: *h.tier.lock().unwrap(),
        }))
    }
}

async fn hub() -> (Arc<Hub>, String) {
    let h = Arc::new(Hub::default());
    let probe = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = probe.local_addr().unwrap();
    drop(probe);
    let svc = Svc(h.clone());
    tokio::spawn(async move {
        tonic::transport::Server::builder()
            .add_service(MediaServiceServer::new(svc))
            .serve(addr)
            .await
            .unwrap();
    });
    tokio::time::sleep(Duration::from_millis(150)).await;
    (h, format!("http://{addr}"))
}

fn tier_resolver(endpoint: &str, fresh: Duration, grace: Duration) -> HubTierResolver {
    HubTierResolver::new(
        lazy_channel(endpoint, None).unwrap(),
        Arc::new(StaticBearer("jwt-1".into())),
        DEADLINE,
        fresh,
        grace,
    )
}

fn creds(endpoint: &str, token: &str) -> HubCredentialResolver {
    HubCredentialResolver::new(
        lazy_channel(endpoint, None).unwrap(),
        Arc::new(StaticBearer(token.into())),
        DEADLINE,
    )
}

fn set_tier(h: &Hub, tier: MediaTier) {
    *h.tier.lock().unwrap() = tier as i32;
}

#[tokio::test]
async fn hub_tier_request_shape_jwt_and_cache() {
    let (h, endpoint) = hub().await;
    let r = tier_resolver(&endpoint, Duration::from_secs(60), Duration::from_secs(600));
    set_tier(&h, MediaTier::Professional);
    assert_eq!(
        r.effective_tier(&scope("a")).await.unwrap(),
        Tier::Professional
    );
    assert_eq!(
        *h.tier_request.lock().unwrap(),
        Some(GetEffectiveTierRequest {
            tenant_id: 7,
            community_id: 42
        })
    );
    assert_eq!(h.auth.lock().unwrap()[0], "Bearer jwt-1");
    assert!(require_professional(&r, &scope("a")).await.is_ok());
    // Cached within the fresh TTL: no second hub call.
    r.effective_tier(&scope("a")).await.unwrap();
    assert_eq!(h.tier_calls.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn free_tenant_and_free_community_is_refused() {
    let (h, endpoint) = hub().await;
    let r = tier_resolver(&endpoint, Duration::from_secs(60), Duration::from_secs(600));
    set_tier(&h, MediaTier::Free);
    assert_eq!(
        require_professional(&r, &scope("a")).await.unwrap_err(),
        MediaError::TierRequired("professional".into())
    );
}

#[tokio::test]
async fn unspecified_or_unknown_tier_fails_closed() {
    let (h, endpoint) = hub().await;
    let r = tier_resolver(&endpoint, Duration::ZERO, Duration::from_secs(600));
    for raw in [MediaTier::Unspecified as i32, 99] {
        *h.tier.lock().unwrap() = raw;
        assert_eq!(
            r.effective_tier(&scope("a")).await.unwrap_err(),
            TierError::Unknown,
            "{raw}"
        );
        assert_eq!(
            require_professional(&r, &scope("a")).await.unwrap_err(),
            MediaError::TierRequired("license_unknown".into())
        );
    }
}

#[tokio::test]
async fn hub_down_never_seen_refuses_but_last_known_serves_within_grace() {
    let (h, endpoint) = hub().await;
    // Never seen + hub error => refuse.
    *h.tier_error.lock().unwrap() = Some(Code::Unavailable);
    let r = tier_resolver(&endpoint, Duration::ZERO, Duration::from_secs(600));
    assert!(matches!(
        r.effective_tier(&scope("a")).await,
        Err(TierError::Unavailable(_))
    ));
    assert_eq!(
        require_professional(&r, &scope("a")).await.unwrap_err(),
        MediaError::TierRequired("license_unknown".into())
    );
    // Seen, then hub down => last-known within grace.
    *h.tier_error.lock().unwrap() = None;
    set_tier(&h, MediaTier::Enterprise);
    assert_eq!(
        r.effective_tier(&scope("a")).await.unwrap(),
        Tier::Enterprise
    );
    *h.tier_error.lock().unwrap() = Some(Code::Internal);
    assert_eq!(
        r.effective_tier(&scope("a")).await.unwrap(),
        Tier::Enterprise
    );
    // Grace expired => refuse again.
    let strict = tier_resolver(&endpoint, Duration::ZERO, Duration::ZERO);
    *h.tier_error.lock().unwrap() = None;
    strict.effective_tier(&scope("a")).await.unwrap();
    tokio::time::sleep(Duration::from_millis(5)).await;
    *h.tier_error.lock().unwrap() = Some(Code::Internal);
    assert!(strict.effective_tier(&scope("a")).await.is_err());
}

#[tokio::test]
async fn tier_refusals_are_unavailable_and_never_echo_server_text() {
    let (h, endpoint) = hub().await;
    let r = tier_resolver(&endpoint, Duration::ZERO, Duration::ZERO);
    for code in [
        Code::NotFound,
        Code::PermissionDenied,
        Code::Unauthenticated,
        Code::InvalidArgument,
        Code::ResourceExhausted,
    ] {
        *h.tier_error.lock().unwrap() = Some(code);
        match r.effective_tier(&scope("a")).await {
            Err(TierError::Unavailable(m)) => assert!(!m.contains("SECRET"), "{m}"),
            other => panic!("{code:?}: {other:?}"),
        }
    }
}

#[tokio::test]
async fn unreachable_hub_is_unavailable() {
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = l.local_addr().unwrap();
    drop(l);
    let endpoint = format!("http://{addr}");
    let r = tier_resolver(&endpoint, Duration::ZERO, Duration::ZERO);
    assert!(matches!(
        r.effective_tier(&scope("a")).await,
        Err(TierError::Unavailable(_))
    ));
    let c = creds(&endpoint, "j");
    assert!(matches!(
        c.resolve(&scope("a"), "gemini").await,
        Err(CredentialError::Unavailable(_))
    ));
}

fn reply(status: ByokStatus, key: Option<&str>) -> ResolveByokResponse {
    ResolveByokResponse {
        status: status as i32,
        tier: MediaTier::Professional as i32,
        api_key: key.map(str::to_owned),
    }
}

#[tokio::test]
async fn byok_resolver_contract() {
    let (h, endpoint) = hub().await;
    let c = creds(&endpoint, "jwt-2");
    *h.byok_reply.lock().unwrap() = reply(ByokStatus::Ok, Some("AIza-secret"));
    let key = c.resolve(&scope("a"), "gemini").await.unwrap();
    assert_eq!(key.expose(), "AIza-secret");
    assert_eq!(format!("{key:?}"), "ApiKey(<redacted>)");
    assert_eq!(
        *h.byok_request.lock().unwrap(),
        Some(ResolveByokRequest {
            tenant_id: 7,
            community_id: 42,
            provider: "gemini".into()
        })
    );
    assert_eq!(h.auth.lock().unwrap().last().unwrap(), "Bearer jwt-2");

    *h.byok_reply.lock().unwrap() = reply(ByokStatus::NotConfigured, None);
    assert_eq!(
        c.resolve(&scope("a"), "gemini").await.unwrap_err(),
        CredentialError::NotConfigured
    );
    *h.byok_reply.lock().unwrap() = reply(ByokStatus::TierRequired, None);
    assert_eq!(
        c.resolve(&scope("a"), "gemini").await.unwrap_err(),
        CredentialError::TierRequired
    );
    // ok without a key / with an empty key, unspecified or unknown status: fail closed.
    for bad in [
        reply(ByokStatus::Ok, None),
        reply(ByokStatus::Ok, Some("")),
        reply(ByokStatus::Unspecified, None),
        ResolveByokResponse {
            status: 99,
            ..Default::default()
        },
    ] {
        *h.byok_reply.lock().unwrap() = bad;
        assert!(matches!(
            c.resolve(&scope("a"), "gemini").await,
            Err(CredentialError::Unavailable(_))
        ));
    }
    // Every documented error status is Unavailable (never NotConfigured) and never echoes
    // server text.
    for code in [
        Code::InvalidArgument,
        Code::Unauthenticated,
        Code::PermissionDenied,
        Code::NotFound,
        Code::ResourceExhausted,
        Code::Internal,
        Code::Unavailable,
    ] {
        *h.byok_error.lock().unwrap() = Some(code);
        *h.byok_reply.lock().unwrap() = reply(ByokStatus::NotConfigured, None);
        match c.resolve(&scope("a"), "gemini").await {
            Err(CredentialError::Unavailable(m)) => assert!(!m.contains("SECRET"), "{m}"),
            other => panic!("{code:?}: {other:?}"),
        }
    }
}
