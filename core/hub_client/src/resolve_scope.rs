//! Client for hub-api's `DataplaneScopeService` (`dataplane_scope.proto`) -- a platform source
//! (`discord` guild id, `twitch` channel login, or the reserved `community` pseudo-platform) to its
//! authoritative numeric `(tenant_id, community_id)`. Replaces the former REST hop
//! `GET /api/v1/internal/dataplane/resolve-scope` (intra-cluster traffic is gRPC only).
//!
//! * **One long-lived channel**: [`ResolveScopeClient::connect`] dials hub-api's internal gRPC
//!   endpoint once over TLS (CA-pinned via [`crate::build_tls_config`], hostname verification on);
//!   tonic multiplexes every lookup over that HTTP/2 connection and reconnects it transparently,
//!   so a cold-miss lookup never pays a handshake. Clones share channel, token cache and breaker.
//! * **Machine-JWT auth** with scope [`SCOPE`], per-call deadline and circuit breaker via
//!   [`crate::rpc::RpcGuard`]. No in-client retry: the caller (`svc_ingest::scope_resolver`) owns
//!   the bounded retry/negative-cache policy and distinguishes definitive refusals
//!   (`NOT_FOUND`/`FAILED_PRECONDITION`/`INVALID_ARGUMENT`, breaker-neutral) from transient ones.
//! * **OTel**: each call runs in a `hub_client.resolve_scope.*` tracing span (exported through the
//!   service's OTLP bridge) carrying only the outcome code -- never a source id.

use std::sync::Arc;
use std::time::Duration;

use service_auth::MachineJwtClient;
use tonic::transport::{Channel, Endpoint};
use tracing::Instrument;

use crate::pb::dataplane_scope_service_client::DataplaneScopeServiceClient;
use crate::pb::{ResolveScopeBatchRequest, ResolveScopeRequest, ResolveScopeResponse, ScopeResult};
use crate::rpc::RpcGuard;
use crate::{build_tls_config, HubClientError};

/// Machine-JWT scope hub-api requires for both RPCs.
pub const SCOPE: &str = "dataplane:resolve-scope";

/// gRPC client for source -> numeric scope resolution.
#[derive(Clone)]
pub struct ResolveScopeClient {
    client: DataplaneScopeServiceClient<Channel>,
    guard: RpcGuard,
}

impl ResolveScopeClient {
    /// Dials hub-api's internal gRPC endpoint (e.g. `https://waddlebot-hub-api-v3:50204`) over TLS
    /// and builds the machine-JWT bootstrap against `token_endpoint`/`sa_token_path` with
    /// [`SCOPE`]. `ca_cert_path` pins the CA that signed hub-api's cert (see
    /// [`crate::HubClient::connect`]); `None` falls back to the system roots.
    ///
    /// # Errors
    /// [`HubClientError`] if the CA file is unreadable, the endpoint is invalid, or hub-api is
    /// unreachable/untrusted -- fatal at boot, never a silent downgrade.
    pub async fn connect(
        endpoint: impl Into<String>,
        token_endpoint: impl Into<String>,
        sa_token_path: impl Into<String>,
        ca_cert_path: Option<&str>,
    ) -> Result<Self, HubClientError> {
        let tls = build_tls_config(ca_cert_path)?;
        let channel = Endpoint::from_shared(endpoint.into())?
            .tls_config(tls)?
            .connect()
            .await?;
        Ok(Self::with_channel(
            channel,
            Arc::new(MachineJwtClient::new(token_endpoint, sa_token_path, SCOPE)),
        ))
    }

    /// Builds a client over an already-established channel (in-process loopback servers in
    /// tests). The token client must carry [`SCOPE`].
    #[must_use]
    pub fn with_channel(channel: Channel, token_client: Arc<MachineJwtClient>) -> Self {
        Self {
            client: DataplaneScopeServiceClient::new(channel),
            guard: RpcGuard::new(token_client),
        }
    }

    /// Overrides the per-call deadline (tests).
    #[must_use]
    pub fn with_deadline(mut self, deadline: Duration) -> Self {
        self.guard = self.guard.with_deadline(deadline);
        self
    }

    /// Resolves one source.
    ///
    /// # Errors
    /// [`HubClientError::CircuitOpen`], [`HubClientError::Auth`], or [`HubClientError::Grpc`]:
    /// `INVALID_ARGUMENT` (malformed), `NOT_FOUND` (unlinked), `FAILED_PRECONDITION` (claimed by
    /// several communities), `UNAUTHENTICATED`/`PERMISSION_DENIED`, `UNAVAILABLE`, ...
    pub async fn resolve(
        &self,
        platform: &str,
        source_id: &str,
    ) -> Result<ResolveScopeResponse, HubClientError> {
        let span = tracing::info_span!(
            "hub_client.resolve_scope.resolve",
            rpc.system = "grpc",
            rpc.method = "ResolveScope",
            outcome = tracing::field::Empty,
        );
        let recorder = span.clone();
        async move {
            self.guard.admit()?;
            let result = self.call_resolve(platform, source_id).await;
            recorder.record("outcome", self.guard.settle(&result));
            result
        }
        .instrument(span)
        .await
    }

    /// Resolves 1..=100 `(platform, source_id)` pairs; every result carries its own status and
    /// the order of `items` is preserved.
    ///
    /// # Errors
    /// As [`Self::resolve`]; `INVALID_ARGUMENT` when `items` is empty or over 100.
    pub async fn resolve_batch(
        &self,
        items: &[(String, String)],
    ) -> Result<Vec<ScopeResult>, HubClientError> {
        let span = tracing::info_span!(
            "hub_client.resolve_scope.batch",
            rpc.system = "grpc",
            rpc.method = "ResolveScopeBatch",
            outcome = tracing::field::Empty,
        );
        let recorder = span.clone();
        async move {
            self.guard.admit()?;
            let result = self.call_batch(items).await;
            recorder.record("outcome", self.guard.settle(&result));
            result
        }
        .instrument(span)
        .await
    }

    async fn call_resolve(
        &self,
        platform: &str,
        source_id: &str,
    ) -> Result<ResolveScopeResponse, HubClientError> {
        let request = self
            .guard
            .request(ResolveScopeRequest {
                platform: platform.to_owned(),
                source_id: source_id.to_owned(),
            })
            .await?;
        let mut client = self.client.clone();
        Ok(client.resolve_scope(request).await?.into_inner())
    }

    async fn call_batch(
        &self,
        items: &[(String, String)],
    ) -> Result<Vec<ScopeResult>, HubClientError> {
        let request = self
            .guard
            .request(ResolveScopeBatchRequest {
                items: items
                    .iter()
                    .map(|(platform, source_id)| ResolveScopeRequest {
                        platform: platform.clone(),
                        source_id: source_id.clone(),
                    })
                    .collect(),
            })
            .await?;
        let mut client = self.client.clone();
        Ok(client
            .resolve_scope_batch(request)
            .await?
            .into_inner()
            .results)
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use crate::pb::dataplane_scope_service_server::{
        DataplaneScopeService, DataplaneScopeServiceServer,
    };
    use crate::pb::{ResolveScopeBatchResponse, ScopeStatus};
    use std::sync::Mutex;
    use tokio::net::TcpListener;
    use tonic::{Request, Response, Status};

    /// What the fake hub saw: `(rpc, bearer, "platform:source" per item)`.
    type Seen = Arc<Mutex<Vec<(String, String, Vec<String>)>>>;

    struct FakeHub {
        seen: Seen,
        reply: Result<ResolveScopeResponse, Status>,
    }

    fn bearer<T>(request: &Request<T>) -> String {
        request
            .metadata()
            .get("authorization")
            .and_then(|v| v.to_str().ok())
            .unwrap_or_default()
            .to_owned()
    }

    #[tonic::async_trait]
    impl DataplaneScopeService for FakeHub {
        async fn resolve_scope(
            &self,
            request: Request<ResolveScopeRequest>,
        ) -> Result<Response<ResolveScopeResponse>, Status> {
            let b = bearer(&request);
            let r = request.into_inner();
            self.seen.lock().unwrap().push((
                "resolve".into(),
                b,
                vec![format!("{}:{}", r.platform, r.source_id)],
            ));
            self.reply.clone().map(Response::new)
        }

        async fn resolve_scope_batch(
            &self,
            request: Request<ResolveScopeBatchRequest>,
        ) -> Result<Response<ResolveScopeBatchResponse>, Status> {
            let b = bearer(&request);
            let items = request.into_inner().items;
            self.seen.lock().unwrap().push((
                "batch".into(),
                b,
                items
                    .iter()
                    .map(|r| format!("{}:{}", r.platform, r.source_id))
                    .collect(),
            ));
            let scope = self.reply.clone()?;
            Ok(Response::new(ResolveScopeBatchResponse {
                results: items
                    .into_iter()
                    .map(|r| ScopeResult {
                        platform: r.platform,
                        source_id: r.source_id,
                        status: ScopeStatus::Resolved as i32,
                        scope: Some(scope.clone()),
                    })
                    .collect(),
            }))
        }
    }

    async fn spawn_hub(reply: Result<ResolveScopeResponse, Status>) -> (Channel, Seen) {
        let seen: Seen = Arc::default();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let svc = DataplaneScopeServiceServer::new(FakeHub {
            seen: Arc::clone(&seen),
            reply,
        });
        tokio::spawn(async move {
            tonic::transport::Server::builder()
                .add_service(svc)
                .serve_with_incoming(tonic::transport::server::TcpIncoming::from(listener))
                .await
                .unwrap();
        });
        let channel = Endpoint::from_shared(format!("http://{addr}"))
            .unwrap()
            .connect()
            .await
            .unwrap();
        (channel, seen)
    }

    /// Real in-process gRPC `ServiceAuthService` answering every mint with `jwt-abc`.
    async fn token_server() -> String {
        service_auth::test_support::MockHub::builder()
            .mint(vec![service_auth::test_support::MintReply::token(
                "jwt-abc", 900,
            )])
            .start_detached()
            .await
    }

    async fn client_over(channel: Channel) -> ResolveScopeClient {
        let sa = std::env::temp_dir().join(format!("hub_client_rs_{}", std::process::id()));
        std::fs::write(&sa, "sa-token").unwrap();
        ResolveScopeClient::with_channel(
            channel,
            Arc::new(MachineJwtClient::new(
                token_server().await,
                sa.to_string_lossy().into_owned(),
                SCOPE,
            )),
        )
    }

    fn scope() -> ResolveScopeResponse {
        ResolveScopeResponse {
            tenant_id: 2,
            community_id: 9,
            tenant_slug: "acme".into(),
            community_name: "main".into(),
        }
    }

    #[test]
    fn scope_matches_the_server_contract() {
        assert_eq!(SCOPE, "dataplane:resolve-scope");
    }

    #[tokio::test]
    async fn resolve_sends_bearer_and_source_and_returns_the_numeric_scope() {
        let (channel, seen) = spawn_hub(Ok(scope())).await;
        let client = client_over(channel).await;
        let got = client.resolve("discord", "guild-1").await.unwrap();
        assert_eq!(got, scope());
        let seen = seen.lock().unwrap();
        assert_eq!(seen.len(), 1);
        assert_eq!(seen[0].0, "resolve");
        assert_eq!(seen[0].1, "Bearer jwt-abc");
        assert_eq!(seen[0].2, ["discord:guild-1"]);
    }

    #[tokio::test]
    async fn batch_preserves_order_and_carries_a_status_per_item() {
        let (channel, seen) = spawn_hub(Ok(scope())).await;
        let client = client_over(channel).await;
        let items = vec![
            ("discord".to_owned(), "1".to_owned()),
            ("twitch".to_owned(), "chan".to_owned()),
        ];
        let results = client.resolve_batch(&items).await.unwrap();
        assert_eq!(results.len(), 2);
        assert_eq!(
            [results[0].source_id.as_str(), results[1].source_id.as_str()],
            ["1", "chan"]
        );
        assert!(results
            .iter()
            .all(|r| r.status == ScopeStatus::Resolved as i32 && r.scope == Some(scope())));
        assert_eq!(seen.lock().unwrap()[0].2, ["discord:1", "twitch:chan"]);
    }

    #[tokio::test]
    async fn refusals_surface_as_grpc_errors_with_their_code() {
        for code in [
            tonic::Code::NotFound,
            tonic::Code::FailedPrecondition,
            tonic::Code::InvalidArgument,
            tonic::Code::PermissionDenied,
        ] {
            let (channel, _) = spawn_hub(Err(Status::new(code, "no"))).await;
            let client = client_over(channel).await;
            match client.resolve("discord", "1").await {
                Err(HubClientError::Grpc(s)) => assert_eq!(s.code(), code),
                other => panic!("expected {code:?}, got {other:?}"),
            }
        }
    }

    #[tokio::test]
    async fn definitive_refusals_never_open_the_breaker_but_server_errors_do() {
        let (channel, _) = spawn_hub(Err(Status::not_found("none"))).await;
        let client = client_over(channel).await;
        for _ in 0..10 {
            assert!(matches!(
                client.resolve("discord", "1").await,
                Err(HubClientError::Grpc(_))
            ));
        }
        let (channel, _) = spawn_hub(Err(Status::unavailable("down"))).await;
        let client = client_over(channel).await;
        for _ in 0..5 {
            let _ = client.resolve("discord", "1").await;
        }
        assert!(matches!(
            client.resolve("discord", "1").await,
            Err(HubClientError::CircuitOpen)
        ));
    }

    #[tokio::test]
    async fn token_failure_is_an_auth_error_and_never_calls_hub() {
        let (channel, seen) = spawn_hub(Ok(scope())).await;
        let client = ResolveScopeClient::with_channel(
            channel,
            Arc::new(MachineJwtClient::new(
                "http://127.0.0.1:1/token",
                "/nonexistent/sa-token",
                SCOPE,
            )),
        );
        assert!(matches!(
            client.resolve("discord", "1").await,
            Err(HubClientError::Auth(_))
        ));
        assert!(seen.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn connect_fails_closed_on_missing_ca_bad_endpoint_and_refused() {
        let missing = ResolveScopeClient::connect(
            "https://127.0.0.1:1",
            "http://127.0.0.1:1/token",
            "/nonexistent",
            Some("/nonexistent/ca.pem"),
        )
        .await;
        assert!(matches!(missing, Err(HubClientError::TlsCaRead { .. })));
        assert!(ResolveScopeClient::connect("not a uri", "t", "s", None)
            .await
            .is_err());
        let refused = ResolveScopeClient::connect("https://127.0.0.1:1", "t", "s", None).await;
        assert!(matches!(refused, Err(HubClientError::Transport(_))));
    }
}
