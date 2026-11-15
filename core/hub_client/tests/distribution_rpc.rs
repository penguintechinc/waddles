//! Real-path tests for `HubClient::list_distribution_bundles` and the shared channel helpers:
//! a real tonic server on loopback (the generated `DistributionService` server trait), a real
//! `MachineJwtClient` bootstrapping from a fake mint, a real lazy `Channel`. Nothing
//! between the client and the wire is mocked.

use std::net::SocketAddr;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use hub_client::pb::distribution_service_server::{DistributionService, DistributionServiceServer};
use hub_client::pb::{
    DistributionBundle, DistributionStage, ListBundlesRequest, ListBundlesResponse,
};
use hub_client::{lazy_channel, HubClient, HubClientError};
use service_auth::test_support::{MintReply, MockHub};
use service_auth::MachineJwtClient;
use tonic::{Request, Response, Status};

const SCOPE: &str = "distribution:read";
const TOKEN: &str = "machine-jwt-for-distribution";

/// What the fake hub saw on each call: (authorization header, stage code, community id).
type Seen = Arc<Mutex<Vec<(String, i32, Option<i64>)>>>;

/// Fake `DistributionService`: enforces the bearer token like the real `AuthInterceptor`
/// (UNAUTHENTICATED otherwise), fails the first `fail_first` calls with `UNAVAILABLE`, and
/// answers a fixed bundle list.
struct FakeHub {
    seen: Seen,
    fail_first: u32,
    calls: Arc<Mutex<u32>>,
    reply: Result<Vec<DistributionBundle>, tonic::Code>,
}

#[tonic::async_trait]
impl DistributionService for FakeHub {
    async fn list_bundles(
        &self,
        request: Request<ListBundlesRequest>,
    ) -> Result<Response<ListBundlesResponse>, Status> {
        let auth = request
            .metadata()
            .get("authorization")
            .and_then(|v| v.to_str().ok())
            .unwrap_or_default()
            .to_string();
        if auth != format!("Bearer {TOKEN}") {
            return Err(Status::unauthenticated("unauthorized"));
        }
        let call = {
            let mut n = self.calls.lock().unwrap();
            *n += 1;
            *n
        };
        let message = request.into_inner();
        self.seen
            .lock()
            .unwrap()
            .push((auth, message.stage, message.community_id));
        if call <= self.fail_first {
            return Err(Status::unavailable("restarting"));
        }
        match &self.reply {
            Ok(bundles) => Ok(Response::new(ListBundlesResponse {
                bundles: bundles.clone(),
            })),
            Err(code) => Err(Status::new(*code, "refused")),
        }
    }
}

fn bundle() -> DistributionBundle {
    DistributionBundle {
        app_id: "waddles.core.demo.echo".into(),
        community_id: Some(7),
        entrypoint: Some("builtin_handlers.echo_ingest:normalize".into()),
        spec_json: b"{}".to_vec(),
        config_json: br#"{"greeting":"yo"}"#.to_vec(),
    }
}

/// Starts the fake hub on a loopback port; returns its `http://` endpoint.
async fn serve(hub: FakeHub) -> String {
    let probe = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr: SocketAddr = probe.local_addr().unwrap();
    drop(probe);
    tokio::spawn(async move {
        tonic::transport::Server::builder()
            .add_service(DistributionServiceServer::new(hub))
            .serve(addr)
            .await
            .unwrap();
    });
    // The lazy channel re-dials until the listener is up; give the spawn a moment.
    tokio::time::sleep(Duration::from_millis(100)).await;
    format!("http://{addr}")
}

/// A `HubClient` over a lazy channel to `endpoint`, with a real bootstrap against an in-process
/// gRPC `ServiceAuthService` (kept alive by the returned hub); it only mints for `SCOPE`.
async fn client(endpoint: &str) -> (HubClient, MockHub) {
    let mint = MockHub::builder()
        .mint(vec![MintReply::when(|c| c.scope == SCOPE, TOKEN, 3600)])
        .start()
        .await;
    let sa = std::env::temp_dir().join(format!("hub-client-dist-sa-{}", std::process::id()));
    std::fs::write(&sa, "projected-sa-token").unwrap();
    let token_client = Arc::new(MachineJwtClient::new(
        mint.endpoint.clone(),
        sa.to_string_lossy().to_string(),
        SCOPE,
    ));
    let channel = lazy_channel(endpoint, None).unwrap();
    (HubClient::with_channel(channel, token_client), mint)
}

fn hub(reply: Result<Vec<DistributionBundle>, tonic::Code>, fail_first: u32) -> (FakeHub, Seen) {
    let seen: Seen = Arc::default();
    (
        FakeHub {
            seen: seen.clone(),
            fail_first,
            calls: Arc::default(),
            reply,
        },
        seen,
    )
}

#[tokio::test]
async fn list_bundles_sends_typed_stage_bearer_token_and_decodes_the_reply() {
    let (fake, seen) = hub(Ok(vec![bundle()]), 0);
    let endpoint = serve(fake).await;
    let (client, _mint) = client(&endpoint).await;

    let response = client
        .list_distribution_bundles(DistributionStage::Process, Some(7))
        .await
        .unwrap();

    assert_eq!(response.bundles, vec![bundle()]);
    let seen = seen.lock().unwrap();
    assert_eq!(
        *seen,
        vec![(
            format!("Bearer {TOKEN}"),
            DistributionStage::Process as i32,
            Some(7)
        )],
        "exactly one call: bearer metadata, enum stage, optional community id"
    );
}

#[tokio::test]
async fn tenant_wide_poll_sends_no_community_id() {
    let (fake, seen) = hub(Ok(vec![]), 0);
    let endpoint = serve(fake).await;
    let (client, _t) = client(&endpoint).await;
    let response = client
        .list_distribution_bundles(DistributionStage::Ingest, None)
        .await
        .unwrap();
    assert!(response.bundles.is_empty());
    assert_eq!(seen.lock().unwrap()[0].2, None);
}

#[tokio::test]
async fn transient_unavailable_is_retried_on_the_same_long_lived_channel() {
    let (fake, seen) = hub(Ok(vec![bundle()]), 2);
    let endpoint = serve(fake).await;
    let (client, _t) = client(&endpoint).await;
    let response = client
        .list_distribution_bundles(DistributionStage::Action, None)
        .await
        .unwrap();
    assert_eq!(response.bundles.len(), 1);
    assert_eq!(
        seen.lock().unwrap().len(),
        3,
        "two UNAVAILABLE answers then success (default retry budget is 2)"
    );
}

#[tokio::test]
async fn a_refusal_surfaces_loudly_with_its_status_code_and_is_not_retried() {
    let (fake, seen) = hub(Err(tonic::Code::PermissionDenied), 0);
    let endpoint = serve(fake).await;
    let (client, _t) = client(&endpoint).await;
    let err = client
        .list_distribution_bundles(DistributionStage::Ingest, None)
        .await
        .unwrap_err();
    assert!(
        matches!(&err, HubClientError::Grpc(s) if s.code() == tonic::Code::PermissionDenied),
        "{err:?}"
    );
    assert_eq!(seen.lock().unwrap().len(), 1, "a refusal is never retried");
}

#[tokio::test]
async fn an_unreachable_hub_fails_instead_of_hanging() {
    // Nothing listens on this port; the lazy channel's connect error is a transport failure.
    let (client, _t) = client("http://127.0.0.1:9").await;
    let started = std::time::Instant::now();
    let err = client
        .list_distribution_bundles(DistributionStage::Ingest, None)
        .await
        .unwrap_err();
    assert!(matches!(err, HubClientError::Grpc(_)), "{err:?}");
    assert!(
        started.elapsed() < Duration::from_secs(20),
        "bounded failure"
    );
}

#[tokio::test]
async fn https_endpoint_with_an_unreadable_ca_file_is_refused_at_build_time() {
    let err = lazy_channel("https://hub-api:50204", Some("/nonexistent/ca.pem")).unwrap_err();
    assert!(matches!(err, HubClientError::TlsCaRead { .. }), "{err:?}");
}

#[tokio::test]
async fn plaintext_endpoint_builds_without_tls_material() {
    assert!(lazy_channel("http://127.0.0.1:50204", None).is_ok());
}
