//! Real-path delivery tests: the stage-side [`SurfaceHost`] talks to a real tonic
//! `SurfaceService` bound on loopback. No mock transport -- the bearer header, the request fields
//! and every refusal status travel over genuine HTTP/2 gRPC.

use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use bundle_host_surface::pb::surface_service_server::{SurfaceService, SurfaceServiceServer};
use bundle_host_surface::pb::{PublishRequest, PublishResponse};
use bundle_host_surface::{
    PushTokenSource, SurfaceError, SurfaceHost, SurfacePolicy, RETRY_AFTER_MS_KEY,
};
use tonic::{Request, Response, Status};

/// What the fake server saw on its last call.
#[derive(Default, Clone)]
struct Seen {
    bearer: Option<String>,
    request: Option<(String, i64, String, String)>,
}

/// A server whose answer is decided by the surface id the client sends.
struct FakeSurfaces {
    seen: Arc<Mutex<Seen>>,
}

#[tonic::async_trait]
impl SurfaceService for FakeSurfaces {
    async fn publish(
        &self,
        request: Request<PublishRequest>,
    ) -> Result<Response<PublishResponse>, Status> {
        let bearer = request
            .metadata()
            .get("authorization")
            .and_then(|v| v.to_str().ok())
            .map(str::to_string);
        let req = request.into_inner();
        *self.seen.lock().unwrap() = Seen {
            bearer,
            request: Some((
                req.api_version.clone(),
                req.community_id,
                req.surface_id.clone(),
                req.payload_json.clone(),
            )),
        };
        match req.surface_id.as_str() {
            "alert" => Ok(Response::new(PublishResponse { seq: 41 })),
            "waddles.core.example.music.queue" => {
                Err(Status::invalid_argument("bad [/queue:type]"))
            }
            "live.channels" => {
                let mut s = Status::resource_exhausted("rate limited");
                s.metadata_mut()
                    .insert(RETRY_AFTER_MS_KEY, "2500".parse().unwrap());
                Err(s)
            }
            "clip.alert" => Err(Status::permission_denied("forbidden")),
            "waddles.core.example.music.now_playing" => Err(Status::out_of_range("too big")),
            _ => Err(Status::unavailable("down")),
        }
    }
}

struct FixedToken;

#[async_trait]
impl PushTokenSource for FixedToken {
    async fn token_for(&self, community_id: i64) -> Result<Arc<str>, String> {
        Ok(Arc::from(format!("tok-for-{community_id}")))
    }
}

async fn start() -> (SurfaceHost, Arc<Mutex<Seen>>) {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let seen = Arc::new(Mutex::new(Seen::default()));
    let svc = SurfaceServiceServer::new(FakeSurfaces { seen: seen.clone() });
    tokio::spawn(async move {
        tonic::transport::Server::builder()
            .add_service(svc)
            .serve_with_incoming(tokio_stream::wrappers::TcpListenerStream::new(listener))
            .await
            .unwrap();
    });
    let host = SurfaceHost::new(&format!("http://{addr}"), None, Arc::new(FixedToken)).unwrap();
    (host, seen)
}

fn all_granted() -> SurfacePolicy {
    SurfacePolicy::from_permissions([
        "overlay.surface.alert",
        "overlay.surface.waddles.core.example.music.queue",
        "overlay.surface.live.channels",
        "overlay.surface.clip.alert",
        "overlay.surface.waddles.core.example.music.now_playing",
        "overlay.surface.unknown_surface",
    ])
}

#[tokio::test]
async fn a_publish_carries_the_community_bound_bearer_and_exact_fields() {
    let (host, seen) = start().await;
    host.publish(
        &all_granted(),
        7,
        "alert",
        r#"{"alert_type":"info","message":"m"}"#,
    )
    .await
    .unwrap();
    let s = seen.lock().unwrap().clone();
    assert_eq!(s.bearer.as_deref(), Some("Bearer tok-for-7"));
    assert_eq!(
        s.request,
        Some((
            "v1".to_string(),
            7,
            "alert".to_string(),
            r#"{"alert_type":"info","message":"m"}"#.to_string()
        ))
    );
}

#[tokio::test]
async fn every_server_refusal_maps_to_the_stable_guest_codes() {
    let (host, _seen) = start().await;
    let policy = all_granted();
    for (surface, want) in [
        ("waddles.core.example.music.queue", "invalid"),
        ("live.channels", "rate_limited"),
        ("clip.alert", "denied"),
        ("waddles.core.example.music.now_playing", "too_large"),
        ("unknown_surface", "unavailable"),
    ] {
        let err = host.publish(&policy, 1, surface, "{}").await.unwrap_err();
        assert_eq!(err.code(), want, "{surface}");
    }
    // The delay survives the round trip.
    assert_eq!(
        host.publish(&policy, 1, "live.channels", "{}")
            .await
            .unwrap_err(),
        SurfaceError::RateLimited {
            retry_after_ms: 2500
        }
    );
}

#[tokio::test]
async fn a_dead_endpoint_is_unavailable_never_a_hang() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    drop(listener); // nothing listening: prompt connection refused
    let host = SurfaceHost::new(&format!("http://{addr}"), None, Arc::new(FixedToken)).unwrap();
    let err = host
        .publish(&all_granted(), 1, "alert", "{}")
        .await
        .unwrap_err();
    assert_eq!(err.code(), "unavailable");
}

#[tokio::test]
async fn publish_for_app_owns_the_apps_namespace_and_parses_the_guests_two_words() {
    let (host, seen) = start().await;
    let own = "waddles.core.example.music.queue";
    let args = serde_json::json!({"surface_id": own, "payload_json": "{}"});
    // The fake server answers `invalid_argument` for this surface id; the point is the call DID
    // reach it, for the host-verified community.
    let e = bundle_host_surface::publish_for_app(&host, "waddles.core.example.music", 9, &args)
        .await
        .unwrap_err();
    assert_eq!(e.code(), "invalid");
    assert_eq!(seen.lock().unwrap().request.as_ref().unwrap().1, 9);
    // Another bundle's namespace never leaves the host.
    let foreign =
        serde_json::json!({"surface_id": "waddles.core.example.dj.queue", "payload_json": "{}"});
    let e = bundle_host_surface::publish_for_app(&host, "waddles.core.example.music", 9, &foreign)
        .await
        .unwrap_err();
    assert_eq!(e.code(), "not_granted");
    // Extra keys (a guest naming a community) are refused.
    let sneaky = serde_json::json!({"surface_id": own, "payload_json": "{}", "community_id": 1});
    let e = bundle_host_surface::publish_for_app(&host, "waddles.core.example.music", 9, &sneaky)
        .await
        .unwrap_err();
    assert_eq!(e.code(), "invalid");
}
