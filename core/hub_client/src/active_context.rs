//! Client for hub-api's `ActiveContextService` (`active_context.proto`) -- the invoking user's
//! active community for a platform source. Replaces the former REST hop
//! `GET|PUT /api/v1/internal/dataplane/active-context` (intra-cluster traffic is gRPC only).
//!
//! * **One long-lived channel**: [`ActiveContextClient::connect`] dials hub-api's internal gRPC
//!   endpoint once over TLS (CA-pinned via [`crate::build_tls_config`], hostname verification on);
//!   tonic multiplexes every call over that HTTP/2 connection and reconnects it transparently.
//!   Clones share channel, token cache and breaker.
//! * **Machine-JWT auth** with scope [`SCOPE`], per-call deadline and circuit breaker via
//!   [`crate::rpc::RpcGuard`]. No in-client retry: slash routing is user-facing and fails closed
//!   (the caller answers the user and publishes nothing).
//! * **OTel**: each call runs in a `hub_client.active_context.*` tracing span (exported through the
//!   service's OTLP bridge) carrying only the outcome code -- never an id, name or user value.

use std::sync::Arc;
use std::time::Duration;

use service_auth::MachineJwtClient;
use tonic::transport::{Channel, Endpoint};
use tracing::Instrument;

use crate::pb::active_context_service_client::ActiveContextServiceClient;
use crate::pb::{ActiveContext, GetActiveContextRequest, SetActiveContextRequest};
use crate::rpc::RpcGuard;
use crate::{build_tls_config, HubClientError};

/// Machine-JWT scope hub-api requires for both RPCs.
pub const SCOPE: &str = "dataplane:active-context";

/// gRPC client for the per-user active-community context.
#[derive(Clone)]
pub struct ActiveContextClient {
    client: ActiveContextServiceClient<Channel>,
    guard: RpcGuard,
}

impl ActiveContextClient {
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
            client: ActiveContextServiceClient::new(channel),
            guard: RpcGuard::new(token_client),
        }
    }

    /// Overrides the per-call deadline (tests).
    #[must_use]
    pub fn with_deadline(mut self, deadline: Duration) -> Self {
        self.guard = self.guard.with_deadline(deadline);
        self
    }

    /// The user's active community for `source_id` (`user` = tokenized user UUID).
    ///
    /// # Errors
    /// [`HubClientError::CircuitOpen`], [`HubClientError::Auth`], or [`HubClientError::Grpc`] for
    /// any server refusal/failure (`NOT_FOUND`, `FAILED_PRECONDITION`, `UNAVAILABLE`, ...). An
    /// answer without a context is `Grpc(INTERNAL)`, never a default.
    pub async fn get(
        &self,
        platform: &str,
        source_id: &str,
        user: &str,
    ) -> Result<ActiveContext, HubClientError> {
        let span = tracing::info_span!(
            "hub_client.active_context.get",
            rpc.system = "grpc",
            rpc.method = "GetActiveContext",
            outcome = tracing::field::Empty,
        );
        let recorder = span.clone();
        async move {
            self.guard.admit()?;
            let result = self.call_get(platform, source_id, user).await;
            recorder.record("outcome", self.guard.settle(&result));
            result
        }
        .instrument(span)
        .await
    }

    /// Switches the user's active community to `community` (a community `public_id`).
    ///
    /// # Errors
    /// As [`Self::get`]; additionally `NOT_FOUND` when `community` is not paired to the source.
    pub async fn set(
        &self,
        platform: &str,
        source_id: &str,
        user: &str,
        community: &str,
    ) -> Result<ActiveContext, HubClientError> {
        let span = tracing::info_span!(
            "hub_client.active_context.set",
            rpc.system = "grpc",
            rpc.method = "SetActiveContext",
            outcome = tracing::field::Empty,
        );
        let recorder = span.clone();
        async move {
            self.guard.admit()?;
            let result = self.call_set(platform, source_id, user, community).await;
            recorder.record("outcome", self.guard.settle(&result));
            result
        }
        .instrument(span)
        .await
    }

    async fn call_get(
        &self,
        platform: &str,
        source_id: &str,
        user: &str,
    ) -> Result<ActiveContext, HubClientError> {
        let request = self
            .guard
            .request(GetActiveContextRequest {
                platform: platform.to_owned(),
                source_id: source_id.to_owned(),
                user: user.to_owned(),
            })
            .await?;
        let mut client = self.client.clone();
        let answer = client.get_active_context(request).await?.into_inner();
        answer.context.ok_or_else(missing_context)
    }

    async fn call_set(
        &self,
        platform: &str,
        source_id: &str,
        user: &str,
        community: &str,
    ) -> Result<ActiveContext, HubClientError> {
        let request = self
            .guard
            .request(SetActiveContextRequest {
                platform: platform.to_owned(),
                source_id: source_id.to_owned(),
                user: user.to_owned(),
                community: community.to_owned(),
            })
            .await?;
        let mut client = self.client.clone();
        let answer = client.set_active_context(request).await?.into_inner();
        answer.context.ok_or_else(missing_context)
    }
}

fn missing_context() -> HubClientError {
    HubClientError::Grpc(tonic::Status::internal(
        "hub-api answered without a context",
    ))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use crate::pb::active_context_service_server::{
        ActiveContextService, ActiveContextServiceServer,
    };
    use crate::pb::{GetActiveContextResponse, SetActiveContextResponse, SwitchableCommunity};
    use std::sync::Mutex;
    use tokio::net::TcpListener;
    use tonic::{Request, Response, Status};

    /// What the fake hub saw: `(rpc, bearer, request fields)`.
    type Seen = Arc<Mutex<Vec<(String, String, Vec<String>)>>>;

    struct FakeHub {
        seen: Seen,
        reply: Result<Option<ActiveContext>, Status>,
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
    impl ActiveContextService for FakeHub {
        async fn get_active_context(
            &self,
            request: Request<GetActiveContextRequest>,
        ) -> Result<Response<GetActiveContextResponse>, Status> {
            let b = bearer(&request);
            let r = request.into_inner();
            self.seen.lock().unwrap().push((
                "get".into(),
                b,
                vec![r.platform, r.source_id, r.user],
            ));
            self.reply
                .clone()
                .map(|context| Response::new(GetActiveContextResponse { context }))
        }

        async fn set_active_context(
            &self,
            request: Request<SetActiveContextRequest>,
        ) -> Result<Response<SetActiveContextResponse>, Status> {
            let b = bearer(&request);
            let r = request.into_inner();
            self.seen.lock().unwrap().push((
                "set".into(),
                b,
                vec![r.platform, r.source_id, r.user, r.community],
            ));
            self.reply
                .clone()
                .map(|context| Response::new(SetActiveContextResponse { context }))
        }
    }

    async fn spawn_hub(reply: Result<Option<ActiveContext>, Status>) -> (Channel, Seen) {
        let seen: Seen = Arc::default();
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let svc = ActiveContextServiceServer::new(FakeHub {
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

    /// Real in-process gRPC `ServiceAuthService` answering every mint with a machine JWT. The mock
    /// is leaked on purpose (it must outlive the returned URL string) and ends with the runtime.
    async fn token_server() -> String {
        let mock = service_auth::test_support::MockHub::builder()
            .mint(vec![service_auth::test_support::MintReply::token("jwt-abc", 900)])
            .start()
            .await;
        let endpoint = mock.endpoint.clone();
        std::mem::forget(mock);
        endpoint
    }

    async fn client_over(channel: Channel) -> ActiveContextClient {
        let sa = std::env::temp_dir().join(format!("hub_client_ac_{}", std::process::id()));
        std::fs::write(&sa, "sa-token").unwrap();
        ActiveContextClient::with_channel(
            channel,
            Arc::new(MachineJwtClient::new(
                token_server().await,
                sa.to_string_lossy().into_owned(),
                SCOPE,
            )),
        )
    }

    fn context() -> ActiveContext {
        ActiveContext {
            community_public_id: "pub-1".into(),
            community_id: 12,
            community_name: "Alpha".into(),
            explicit: true,
            communities: vec![SwitchableCommunity {
                public_id: "pub-1".into(),
                display_name: "Alpha".into(),
                position: 0,
            }],
        }
    }

    #[test]
    fn scope_matches_the_server_contract() {
        assert_eq!(SCOPE, "dataplane:active-context");
    }

    #[tokio::test]
    async fn get_sends_bearer_and_fields_and_returns_the_numeric_context() {
        let (channel, seen) = spawn_hub(Ok(Some(context()))).await;
        let client = client_over(channel).await;
        let got = client.get("discord", "guild-1", "user-uuid").await.unwrap();
        assert_eq!(got, context());
        assert_eq!(got.community_id, 12);
        let seen = seen.lock().unwrap();
        assert_eq!(seen.len(), 1);
        assert_eq!(seen[0].0, "get");
        assert_eq!(seen[0].1, "Bearer jwt-abc");
        assert_eq!(seen[0].2, ["discord", "guild-1", "user-uuid"]);
    }

    #[tokio::test]
    async fn set_sends_the_community_public_id() {
        let (channel, seen) = spawn_hub(Ok(Some(context()))).await;
        let client = client_over(channel).await;
        client
            .set("discord", "guild-1", "user-uuid", "pub-1")
            .await
            .unwrap();
        let seen = seen.lock().unwrap();
        assert_eq!(seen[0].0, "set");
        assert_eq!(seen[0].2, ["discord", "guild-1", "user-uuid", "pub-1"]);
    }

    #[tokio::test]
    async fn an_answer_without_a_context_is_an_internal_error_never_a_default() {
        let (channel, _) = spawn_hub(Ok(None)).await;
        let client = client_over(channel).await;
        for result in [
            client.get("discord", "g", "u").await,
            client.set("discord", "g", "u", "c").await,
        ] {
            match result {
                Err(HubClientError::Grpc(s)) => assert_eq!(s.code(), tonic::Code::Internal),
                other => panic!("expected Internal, got {other:?}"),
            }
        }
    }

    #[tokio::test]
    async fn refusals_surface_as_grpc_errors() {
        for code in [
            tonic::Code::NotFound,
            tonic::Code::FailedPrecondition,
            tonic::Code::PermissionDenied,
        ] {
            let (channel, _) = spawn_hub(Err(Status::new(code, "no"))).await;
            let client = client_over(channel).await;
            match client.get("discord", "g", "u").await {
                Err(HubClientError::Grpc(s)) => assert_eq!(s.code(), code),
                other => panic!("expected {code:?}, got {other:?}"),
            }
        }
    }

    #[tokio::test]
    async fn server_errors_open_the_breaker_caller_errors_do_not() {
        let (channel, _) = spawn_hub(Err(Status::not_found("none"))).await;
        let client = client_over(channel).await;
        for _ in 0..10 {
            assert!(matches!(
                client.get("discord", "g", "u").await,
                Err(HubClientError::Grpc(_))
            ));
        }
        let (channel, _) = spawn_hub(Err(Status::unavailable("down"))).await;
        let client = client_over(channel).await;
        for _ in 0..5 {
            let _ = client.get("discord", "g", "u").await;
        }
        assert!(matches!(
            client.get("discord", "g", "u").await,
            Err(HubClientError::CircuitOpen)
        ));
    }

    #[tokio::test]
    async fn token_failure_is_an_auth_error_and_never_calls_hub() {
        let (channel, seen) = spawn_hub(Ok(Some(context()))).await;
        let client = ActiveContextClient::with_channel(
            channel,
            Arc::new(MachineJwtClient::new(
                "http://127.0.0.1:1/token",
                "/nonexistent/sa-token",
                SCOPE,
            )),
        );
        assert!(matches!(
            client.get("discord", "g", "u").await,
            Err(HubClientError::Auth(_))
        ));
        assert!(seen.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn connect_fails_closed_on_missing_ca_bad_endpoint_and_refused() {
        let missing = ActiveContextClient::connect(
            "https://127.0.0.1:1",
            "http://127.0.0.1:1/token",
            "/nonexistent",
            Some("/nonexistent/ca.pem"),
        )
        .await;
        assert!(matches!(missing, Err(HubClientError::TlsCaRead { .. })));
        assert!(ActiveContextClient::connect("not a uri", "t", "s", None)
            .await
            .is_err());
        let refused = ActiveContextClient::connect("https://127.0.0.1:1", "t", "s", None).await;
        assert!(matches!(refused, Err(HubClientError::Transport(_))));
    }
}
