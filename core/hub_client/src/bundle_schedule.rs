//! Client for hub-api's `BundleScheduleService` (`bundle_schedule.proto`) -- the scheduled-bundle
//! listing svc-ingest's reconcile scheduler polls. Replaces the former REST hop
//! `GET /api/v1/internal/bundle-schedules` (intra-cluster traffic is gRPC only).
//!
//! * **One long-lived channel**: [`BundleScheduleClient::connect`] dials hub-api's internal gRPC
//!   endpoint once over TLS (CA-pinned via [`crate::build_tls_config`], hostname verification on);
//!   tonic multiplexes every poll over that HTTP/2 connection and reconnects it transparently, so
//!   the client is cheaply `Clone` and a poll never pays a handshake.
//! * **Machine-JWT auth**: every call carries `authorization: Bearer <jwt>` minted by
//!   [`service_auth::MachineJwtClient`] with scope [`SCOPE`]; hub-api additionally requires the
//!   operator-plane `system` tenant and an allow-listed caller.
//! * **Bounded**: a per-call deadline under hub-api's 5s ceiling, and a circuit breaker so a wedged
//!   hub-api fails fast. Only server-health failures count; a request the server answered and
//!   declined ([`crate::is_caller_error`]) is breaker-neutral. No in-client retry: the scheduler
//!   polls on its own cadence and treats a failed listing as "try again next tick".
//! * **OTel**: each call runs in a `hub_client.bundle_schedules.list` tracing span (exported via the
//!   service's OTLP bridge) carrying the outcome code and target count -- ids only, no PII.

use std::sync::Arc;
use std::time::Duration;

use service_auth::MachineJwtClient;
use tonic::transport::{Channel, Endpoint};
use tonic::{Request, Status};
use tracing::Instrument;

use crate::pb::bundle_schedule_service_client::BundleScheduleServiceClient;
use crate::pb::{BundleScheduleTarget, ListBundleSchedulesRequest};
use crate::{build_tls_config, is_caller_error, CircuitBreaker, HubClientError};

/// Machine-JWT scope hub-api requires for `ListBundleSchedules`.
pub const SCOPE: &str = "bundle-schedules:read";

/// Per-call deadline (hub-api rejects anything over 5s).
pub const DEADLINE: Duration = Duration::from_secs(3);

/// Optional listing filters; numeric ids only. `None` = unfiltered.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ScheduleFilter {
    pub tenant_id: Option<i64>,
    pub community_id: Option<i64>,
    pub app_id: Option<String>,
}

/// gRPC client for the scheduled-bundle listing. Clones share channel, token cache and breaker.
#[derive(Clone)]
pub struct BundleScheduleClient {
    client: BundleScheduleServiceClient<Channel>,
    token_client: Arc<MachineJwtClient>,
    breaker: Arc<CircuitBreaker>,
    deadline: Duration,
}

impl BundleScheduleClient {
    /// Dials hub-api's internal gRPC endpoint (e.g. `https://waddlebot-hub-api-v3:50204`) over
    /// TLS and builds the machine-JWT bootstrap against `token_endpoint`/`sa_token_path` with
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
    /// tests); same breaker/deadline policy as [`Self::connect`]. The token client must carry
    /// [`SCOPE`].
    #[must_use]
    pub fn with_channel(channel: Channel, token_client: Arc<MachineJwtClient>) -> Self {
        Self {
            client: BundleScheduleServiceClient::new(channel),
            token_client,
            breaker: Arc::new(CircuitBreaker::new(5, Duration::from_secs(30))),
            deadline: DEADLINE,
        }
    }

    /// Overrides the per-call deadline (tests).
    #[must_use]
    pub fn with_deadline(mut self, deadline: Duration) -> Self {
        self.deadline = deadline;
        self
    }

    /// Lists every active scheduled bundle scope matching `filter`.
    ///
    /// # Errors
    /// [`HubClientError::CircuitOpen`] while the breaker is open, [`HubClientError::Auth`] when no
    /// machine JWT can be minted, [`HubClientError::Grpc`] for any server refusal/failure
    /// (`PERMISSION_DENIED`, `INVALID_ARGUMENT`, `UNAVAILABLE`, ...).
    pub async fn list(
        &self,
        filter: &ScheduleFilter,
    ) -> Result<Vec<BundleScheduleTarget>, HubClientError> {
        let span = tracing::info_span!(
            "hub_client.bundle_schedules.list",
            rpc.system = "grpc",
            rpc.service = "waddles.hub.internal.v1.BundleScheduleService",
            rpc.method = "ListBundleSchedules",
            outcome = tracing::field::Empty,
            targets = tracing::field::Empty,
        );
        let recorder = span.clone();
        async move {
            if !self.breaker.allow() {
                recorder.record("outcome", "circuit_open");
                return Err(HubClientError::CircuitOpen);
            }
            let result = self.call(filter).await;
            match &result {
                Ok(targets) => {
                    self.breaker.record_success();
                    recorder.record("outcome", "ok");
                    recorder.record("targets", targets.len());
                }
                Err(HubClientError::Grpc(status)) if is_caller_error(status) => {
                    recorder.record("outcome", format!("{:?}", status.code()));
                }
                Err(err) => {
                    self.breaker.record_failure();
                    let code = match err {
                        HubClientError::Grpc(status) => format!("{:?}", status.code()),
                        HubClientError::Auth(_) => "auth".to_owned(),
                        _ => "error".to_owned(),
                    };
                    recorder.record("outcome", code);
                }
            }
            result
        }
        .instrument(span)
        .await
    }

    async fn call(
        &self,
        filter: &ScheduleFilter,
    ) -> Result<Vec<BundleScheduleTarget>, HubClientError> {
        let token = self.token_client.get_token().await?;
        let mut request = Request::new(ListBundleSchedulesRequest {
            tenant_id: filter.tenant_id,
            community_id: filter.community_id,
            app_id: filter.app_id.clone(),
        });
        request.set_timeout(self.deadline);
        request.metadata_mut().insert(
            "authorization",
            format!("Bearer {token}")
                .parse()
                .map_err(|_| Status::internal("invalid token metadata"))?,
        );
        let mut client = self.client.clone();
        let response = client.list_bundle_schedules(request).await?;
        Ok(response.into_inner().targets)
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use crate::pb::bundle_schedule_service_server::{
        BundleScheduleService, BundleScheduleServiceServer,
    };
    use crate::pb::ListBundleSchedulesResponse;
    use std::sync::Mutex;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    /// A real tonic server on loopback that records each request's bearer + filters and answers
    /// with a scripted result.
    struct FakeHub {
        seen: Arc<Mutex<Vec<(String, ListBundleSchedulesRequest)>>>,
        reply: Result<Vec<BundleScheduleTarget>, Status>,
    }

    #[tonic::async_trait]
    impl BundleScheduleService for FakeHub {
        async fn list_bundle_schedules(
            &self,
            request: Request<ListBundleSchedulesRequest>,
        ) -> Result<tonic::Response<ListBundleSchedulesResponse>, Status> {
            let bearer = request
                .metadata()
                .get("authorization")
                .and_then(|v| v.to_str().ok())
                .unwrap_or_default()
                .to_owned();
            self.seen
                .lock()
                .unwrap()
                .push((bearer, request.into_inner()));
            self.reply
                .clone()
                .map(|targets| tonic::Response::new(ListBundleSchedulesResponse { targets }))
        }
    }

    type Seen = Arc<Mutex<Vec<(String, ListBundleSchedulesRequest)>>>;

    async fn spawn_hub(reply: Result<Vec<BundleScheduleTarget>, Status>) -> (Channel, Seen) {
        let seen: Seen = Arc::default();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let svc = BundleScheduleServiceServer::new(FakeHub {
            seen: Arc::clone(&seen),
            reply,
        });
        tokio::spawn(async move {
            tonic::transport::Server::builder()
                .add_service(svc)
                .serve_with_incoming(tokio_stream_incoming(listener))
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

    fn tokio_stream_incoming(listener: TcpListener) -> tonic::transport::server::TcpIncoming {
        tonic::transport::server::TcpIncoming::from(listener)
    }

    /// Local HTTP token endpoint answering every request with a machine JWT.
    async fn token_server() -> String {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            while let Ok((mut sock, _)) = listener.accept().await {
                tokio::spawn(async move {
                    let mut buf = vec![0u8; 8192];
                    let _ = sock.read(&mut buf).await;
                    let body = r#"{"token":"jwt-abc","expires_in":900}"#;
                    let resp = format!(
                        "HTTP/1.1 200 X\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                        body.len()
                    );
                    let _ = sock.write_all(resp.as_bytes()).await;
                    let _ = sock.shutdown().await;
                });
            }
        });
        format!("http://{addr}")
    }

    fn sa_file(label: &str) -> String {
        let path =
            std::env::temp_dir().join(format!("hub_client_bs_{}_{label}", std::process::id()));
        std::fs::write(&path, "sa-token").unwrap();
        path.to_string_lossy().into_owned()
    }

    async fn client_over(channel: Channel) -> BundleScheduleClient {
        let tokens = Arc::new(MachineJwtClient::new(
            token_server().await,
            sa_file("ok"),
            SCOPE,
        ));
        BundleScheduleClient::with_channel(channel, tokens)
    }

    fn target(app: &str) -> BundleScheduleTarget {
        BundleScheduleTarget {
            tenant_id: 7,
            community_id: 0,
            tenant_slug: "acme".into(),
            community_name: None,
            app_id: app.into(),
            interval_s: 90,
            jitter: 0.25,
        }
    }

    #[test]
    fn scope_matches_the_server_contract() {
        assert_eq!(SCOPE, "bundle-schedules:read");
        assert!(
            DEADLINE < Duration::from_secs(5),
            "must stay under hub-api's ceiling"
        );
    }

    #[tokio::test]
    async fn list_sends_bearer_and_filters_and_returns_targets() {
        let (channel, seen) = spawn_hub(Ok(vec![target("a.b.c")])).await;
        let client = client_over(channel).await;
        let filter = ScheduleFilter {
            tenant_id: Some(7),
            community_id: Some(0),
            app_id: Some("a.b.c".into()),
        };
        let targets = client.list(&filter).await.unwrap();
        assert_eq!(targets, vec![target("a.b.c")]);
        let seen = seen.lock().unwrap();
        assert_eq!(seen.len(), 1);
        assert_eq!(seen[0].0, "Bearer jwt-abc");
        assert_eq!(seen[0].1.tenant_id, Some(7));
        assert_eq!(
            seen[0].1.community_id,
            Some(0),
            "an explicit community 0 must survive"
        );
        assert_eq!(seen[0].1.app_id.as_deref(), Some("a.b.c"));
    }

    #[tokio::test]
    async fn unset_filters_stay_unset_on_the_wire() {
        let (channel, seen) = spawn_hub(Ok(vec![])).await;
        let client = client_over(channel).await;
        assert!(client
            .list(&ScheduleFilter::default())
            .await
            .unwrap()
            .is_empty());
        let req = seen.lock().unwrap()[0].1.clone();
        assert_eq!(
            (req.tenant_id, req.community_id, req.app_id),
            (None, None, None)
        );
    }

    #[tokio::test]
    async fn server_refusal_surfaces_as_grpc_error_never_an_empty_list() {
        let (channel, _) = spawn_hub(Err(Status::permission_denied("forbidden"))).await;
        let client = client_over(channel).await;
        match client.list(&ScheduleFilter::default()).await {
            Err(HubClientError::Grpc(s)) => assert_eq!(s.code(), tonic::Code::PermissionDenied),
            other => panic!("expected PermissionDenied, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn caller_errors_are_breaker_neutral_and_server_errors_open_it() {
        let (channel, _) = spawn_hub(Err(Status::invalid_argument("bad"))).await;
        let client = client_over(channel).await;
        for _ in 0..10 {
            assert!(matches!(
                client.list(&ScheduleFilter::default()).await,
                Err(HubClientError::Grpc(_))
            ));
        }
        // 10 INVALID_ARGUMENT answers never tripped the breaker (still reaches the server).
        assert!(client.breaker.allow());

        let (channel, _) = spawn_hub(Err(Status::unavailable("down"))).await;
        let client = client_over(channel).await;
        for _ in 0..5 {
            let _ = client.list(&ScheduleFilter::default()).await;
        }
        assert!(matches!(
            client.list(&ScheduleFilter::default()).await,
            Err(HubClientError::CircuitOpen)
        ));
    }

    #[tokio::test]
    async fn token_failure_is_an_auth_error_and_never_calls_hub() {
        let (channel, seen) = spawn_hub(Ok(vec![])).await;
        let tokens = Arc::new(MachineJwtClient::new(
            "http://127.0.0.1:1/token",
            "/nonexistent/sa-token",
            SCOPE,
        ));
        let client = BundleScheduleClient::with_channel(channel, tokens);
        assert!(matches!(
            client.list(&ScheduleFilter::default()).await,
            Err(HubClientError::Auth(_))
        ));
        assert!(seen.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn connect_fails_closed_on_missing_ca_and_bad_endpoint() {
        let missing = BundleScheduleClient::connect(
            "https://127.0.0.1:1",
            "http://127.0.0.1:1/token",
            "/nonexistent",
            Some("/nonexistent/ca.pem"),
        )
        .await;
        assert!(matches!(missing, Err(HubClientError::TlsCaRead { .. })));
        let invalid = BundleScheduleClient::connect("not a uri", "t", "s", None).await;
        assert!(invalid.is_err());
        let refused = BundleScheduleClient::connect("https://127.0.0.1:1", "t", "s", None).await;
        assert!(matches!(refused, Err(HubClientError::Transport(_))));
    }
}
