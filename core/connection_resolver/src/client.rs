//! gRPC client for hub-api's internal `ProviderConnectionService` (resolve + list).
//!
//! Replaces the retired machine-JWT REST routes (`GET /api/v1/internal/provider-connections`,
//! `POST .../provider-connections/{id}/resolve`): intra-cluster calls are gRPC, REST is for
//! external traffic only. One long-lived lazily-connecting [`tonic::transport::Channel`] per
//! resolver (TLS pinned to the internal CA; plaintext only in alpha), a machine JWT in
//! `authorization` metadata, a bounded per-RPC deadline, a circuit breaker, and a `tracing`
//! span per call. Secrets never reach a log line, a label, or an error value.

use std::future::Future;
use std::sync::Arc;
use std::time::Duration;

use hub_client::pb::provider_connection_service_client::ProviderConnectionServiceClient;
use hub_client::pb::{ListBindingsRequest, ResolveConnectionRequest};
use hub_client::{bearer_request, lazy_channel};
use tonic::transport::Channel;
use tonic::{Code, Status};
use tracing::Instrument;

use crate::breaker::{Breaker, Verdict};
use crate::error::{NotUsableReason, ResolveError};
use crate::types::{
    page_from_pb, resolved_from_pb, BindingPage, ConnectionKind, ConnectionRef, ResolvedConnection,
};

/// Scope for OAuth-only resolves (community-bound).
pub const SCOPE_CONNECTIONS_RESOLVE: &str = "connections:resolve";
/// Scope that additionally allows static-secret connections to resolve (community-bound).
pub const SCOPE_CONNECTIONS_RESOLVE_STATIC: &str = "connections:resolve:static";
/// Scope for the unbound data-plane binding list.
pub const SCOPE_CONNECTIONS_LIST: &str = "connections:list";
/// Least-privilege resolve scope for the `gameserver` provider only (hub-api refuses it for any
/// other provider).
pub const SCOPE_CONNECTIONS_RESOLVE_GAMESERVER: &str = "connections:resolve:gameserver";
/// Least-privilege binding-list scope for the `gameserver` provider only.
pub const SCOPE_CONNECTIONS_LIST_GAMESERVER: &str = "connections:list:gameserver";

/// Prefix of the `FAILED_PRECONDITION` detail hub-api sends for a not-usable connection; the
/// fixed reason code follows it (`not_usable:disabled`).
const NOT_USABLE_PREFIX: &str = "not_usable:";

/// Static configuration of a [`ConnectionResolver`].
#[derive(Clone, Debug)]
pub struct ResolverConfig {
    /// hub-api's internal gRPC endpoint (`HUB_API_GRPC_ENDPOINT`, e.g.
    /// `https://waddlebot-hub-api-v3:50204`); plaintext `http://` is accepted only in alpha.
    pub hub_grpc_endpoint: String,
    /// PEM CA bundle that signed hub-api's gRPC server cert (`HUB_API_GRPC_CA_FILE`). `Some`
    /// trusts ONLY that CA; `None` falls back to the webpki roots. Hostname checks stay on.
    pub grpc_ca_path: Option<String>,
    /// Deployment environment name (`alpha`/`beta`/...), used for the transport policy.
    pub environment: String,
    /// hub-api's internal gRPC endpoint serving the machine-JWT mint
    /// (`ServiceAuthService.MintServiceToken`), e.g. `https://hub-api:50204`; the CA comes
    /// from `HUB_API_GRPC_CA_FILE`.
    pub token_endpoint: String,
    /// Path of the projected ServiceAccount token used to bootstrap machine JWTs.
    pub sa_token_path: String,
    /// Scope requested for resolve tokens: [`SCOPE_CONNECTIONS_RESOLVE`] or
    /// [`SCOPE_CONNECTIONS_RESOLVE_STATIC`] (the latter only for callers allow-listed for it).
    pub resolve_scope: String,
    /// Scope requested for the unbound binding list: [`SCOPE_CONNECTIONS_LIST`] (default) or
    /// [`SCOPE_CONNECTIONS_LIST_GAMESERVER`].
    pub list_scope: String,
    /// Per-RPC deadline (default 3s; the server rejects deadlines above 5s).
    pub request_timeout: Duration,
    /// Connect timeout for the token bootstrap HTTP client (default 2s).
    pub connect_timeout: Duration,
    /// Consecutive transient failures before the breaker opens (default 5).
    pub breaker_threshold: u32,
    /// How long the breaker sheds load before probing (default 10s).
    pub breaker_cooldown: Duration,
}

impl ResolverConfig {
    /// Config with the standard timeouts/breaker and the OAuth-only resolve scope.
    pub fn new(
        hub_grpc_endpoint: impl Into<String>,
        environment: impl Into<String>,
        token_endpoint: impl Into<String>,
        sa_token_path: impl Into<String>,
    ) -> Self {
        Self {
            hub_grpc_endpoint: hub_grpc_endpoint.into(),
            grpc_ca_path: None,
            environment: environment.into(),
            token_endpoint: token_endpoint.into(),
            sa_token_path: sa_token_path.into(),
            resolve_scope: SCOPE_CONNECTIONS_RESOLVE.to_string(),
            list_scope: SCOPE_CONNECTIONS_LIST.to_string(),
            request_timeout: Duration::from_secs(3),
            connect_timeout: Duration::from_secs(2),
            breaker_threshold: 5,
            breaker_cooldown: Duration::from_secs(10),
        }
    }

    /// Pin TLS trust to the internal CA at `path` (builder style).
    #[must_use]
    pub fn with_grpc_ca(mut self, path: Option<String>) -> Self {
        self.grpc_ca_path = path.filter(|p| !p.trim().is_empty());
        self
    }
}

/// Telemetry sink; implementations must never put secrets or ids in labels.
pub trait ResolverMetrics: Send + Sync {
    /// A resolve that returned a connection (`outcome` is always `"ok"`).
    fn resolve(&self, kind: ConnectionKind, outcome: &'static str, latency: Duration);
    /// A resolve that failed (the kind is unknown on failure); `outcome` is a fixed label.
    /// Default is a no-op so existing sinks keep compiling; real sinks should override.
    fn resolve_failed(&self, _outcome: &'static str, _latency: Duration) {}
    /// A list call; `outcome` is `"ok"` or a fixed error label.
    fn list(&self, outcome: &'static str);
}

/// Test seam for consumers' unit tests (real-path tests use [`ConnectionResolver`]).
pub trait ConnectionResolve: Send + Sync {
    /// See [`ConnectionResolver::resolve`].
    fn resolve(
        &self,
        r: ConnectionRef,
    ) -> impl Future<Output = Result<ResolvedConnection, ResolveError>> + Send;
    /// See [`ConnectionResolver::list_bindings`].
    fn list_bindings(
        &self,
        provider: &str,
        capability: Option<&str>,
        after: i64,
        limit: u32,
    ) -> impl Future<Output = Result<BindingPage, ResolveError>> + Send;
}

struct Inner {
    rpc: ProviderConnectionServiceClient<Channel>,
    deadline: Duration,
    bound: service_auth::BoundMachineJwtClient,
    list_jwt: service_auth::MachineJwtClient,
    breaker: Breaker,
    metrics: Arc<dyn ResolverMetrics>,
}

/// Resolves connections against hub-api. Cheap to clone (shared `Arc`).
#[derive(Clone)]
pub struct ConnectionResolver {
    inner: Arc<Inner>,
}

impl std::fmt::Debug for ConnectionResolver {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ConnectionResolver").finish_non_exhaustive()
    }
}

/// Plaintext `http://` is allowed only in alpha; anything but http(s) is refused.
/// Same policy as svc_ingest's `check_transport`.
fn check_transport(endpoint: &str, environment: &str) -> Result<(), ResolveError> {
    let lower = endpoint.trim().to_ascii_lowercase();
    if lower.starts_with("https://") {
        return Ok(());
    }
    if lower.starts_with("http://") {
        if environment.eq_ignore_ascii_case("alpha") {
            return Ok(());
        }
        return Err(ResolveError::Malformed(
            "plaintext http hub endpoint refused outside alpha",
        ));
    }
    Err(ResolveError::Malformed("hub endpoint must be http(s)"))
}

/// Map a non-OK gRPC status to a [`ResolveError`]. Only the status CODE (and, for the one
/// documented `not_usable:<reason>` detail, a fixed reason code) is read -- never free-form
/// server text, so a secret-looking message cannot reach an error value or a log line.
fn map_status(status: &Status) -> ResolveError {
    match status.code() {
        Code::NotFound => ResolveError::NotFound,
        Code::FailedPrecondition => match status.message().strip_prefix(NOT_USABLE_PREFIX) {
            Some(reason) => ResolveError::NotUsable(NotUsableReason::from_wire(reason)),
            None => ResolveError::Malformed("invalid not_usable status"),
        },
        Code::Unauthenticated | Code::PermissionDenied => ResolveError::Auth,
        Code::ResourceExhausted | Code::DeadlineExceeded | Code::Cancelled => {
            ResolveError::Transient("hub rate limited or timed out")
        }
        Code::Unavailable | Code::Internal | Code::Unknown | Code::DataLoss | Code::Aborted => {
            ResolveError::Transient("hub server error")
        }
        _ => ResolveError::Malformed("unexpected status"),
    }
}

/// Map a token-mint failure: a rejected bootstrap is auth, operational failures are transient.
fn map_auth_error(e: &service_auth::ServiceAuthError) -> ResolveError {
    use service_auth::ServiceAuthError as E;
    match e {
        E::BootstrapRejected(_) | E::InvalidToken(_) | E::UnknownKeyId(_) => ResolveError::Auth,
        E::Grpc(_) => ResolveError::Transient("token endpoint unreachable"),
        E::Config(_) => ResolveError::Malformed("token endpoint misconfigured"),
        E::Io(_) => ResolveError::Transient("service account token unreadable"),
    }
}

impl ConnectionResolver {
    /// Build a resolver; refuses a plaintext hub endpoint outside alpha and must be called
    /// inside a tokio runtime (the long-lived channel spawns its connection worker).
    pub fn new(
        cfg: ResolverConfig,
        metrics: Arc<dyn ResolverMetrics>,
    ) -> Result<Self, ResolveError> {
        check_transport(&cfg.hub_grpc_endpoint, &cfg.environment)?;
        if tokio::runtime::Handle::try_current().is_err() {
            return Err(ResolveError::Malformed(
                "connection resolver must be built inside a tokio runtime",
            ));
        }
        let channel = lazy_channel(cfg.hub_grpc_endpoint.trim(), cfg.grpc_ca_path.as_deref())
            .map_err(|_| ResolveError::Malformed("cannot build hub grpc channel"))?;
        let timeouts = service_auth::HttpTimeouts {
            connect: cfg.connect_timeout,
            request: cfg.request_timeout,
        };
        let bound = service_auth::BoundMachineJwtClient::with_timeouts(
            cfg.token_endpoint.clone(),
            cfg.sa_token_path.clone(),
            cfg.resolve_scope,
            timeouts,
        )
        .map_err(|_| ResolveError::Malformed("cannot build token client"))?;
        let list_jwt = service_auth::MachineJwtClient::with_timeouts(
            cfg.token_endpoint,
            cfg.sa_token_path,
            cfg.list_scope,
            timeouts,
        )
        .map_err(|_| ResolveError::Malformed("cannot build token client"))?;
        Ok(Self {
            inner: Arc::new(Inner {
                rpc: ProviderConnectionServiceClient::new(channel),
                deadline: cfg.request_timeout,
                bound,
                list_jwt,
                breaker: Breaker::new(cfg.breaker_threshold, cfg.breaker_cooldown),
                metrics,
            }),
        })
    }

    /// Resolve one connection: exactly one authenticated hub call, no credential cache
    /// (only the community-bound bearer JWT is cached, inside `BoundMachineJwtClient`).
    pub async fn resolve(&self, r: ConnectionRef) -> Result<ResolvedConnection, ResolveError> {
        let span = tracing::info_span!(
            "connection_resolve",
            rpc.system = "grpc",
            rpc.method = "ProviderConnectionService/ResolveConnection",
            community_id = r.community_id,
            connection_id = r.connection_id,
            kind = tracing::field::Empty,
            outcome = tracing::field::Empty,
        );
        let started = tokio::time::Instant::now();
        let result = self.resolve_inner(r).instrument(span.clone()).await;
        let latency = started.elapsed();
        match &result {
            Ok(c) => {
                span.record("kind", c.kind.code());
                span.record("outcome", "ok");
                self.inner.metrics.resolve(c.kind, "ok", latency);
            }
            Err(e) => {
                span.record("outcome", e.outcome());
                self.inner.metrics.resolve_failed(e.outcome(), latency);
            }
        }
        result
    }

    async fn resolve_inner(&self, r: ConnectionRef) -> Result<ResolvedConnection, ResolveError> {
        self.inner.breaker.allow()?;
        let result = self.resolve_call(r).await;
        self.inner.breaker.record(match &result {
            Ok(_) => Verdict::Success,
            Err(e) => Verdict::of_error(e),
        });
        result
    }

    async fn resolve_call(&self, r: ConnectionRef) -> Result<ResolvedConnection, ResolveError> {
        let token = self
            .inner
            .bound
            .get_token_for(r.community_id, Some(r.connection_id))
            .await
            .map_err(|e| map_auth_error(&e))?;
        let request = bearer_request(
            ResolveConnectionRequest {
                community_id: r.community_id,
                connection_id: r.connection_id,
            },
            &token,
            self.inner.deadline,
        )
        .map_err(|_| ResolveError::Malformed("invalid token metadata"))?;
        let mut rpc = self.inner.rpc.clone();
        let response = rpc
            .resolve_connection(request)
            .await
            .map_err(|s| map_status(&s))?;
        resolved_from_pb(response.into_inner(), r)
    }

    /// One page of bindings (no secrets), via the unbound `connections:list` token.
    pub async fn list_bindings(
        &self,
        provider: &str,
        capability: Option<&str>,
        after: i64,
        limit: u32,
    ) -> Result<BindingPage, ResolveError> {
        let result = match self.inner.breaker.allow() {
            Err(e) => Err(e),
            Ok(()) => {
                let r = self
                    .list_call(provider, capability, after, limit)
                    .instrument(tracing::info_span!(
                        "connection_list_bindings",
                        rpc.system = "grpc",
                        rpc.method = "ProviderConnectionService/ListBindings",
                    ))
                    .await;
                self.inner.breaker.record(match &r {
                    Ok(_) => Verdict::Success,
                    Err(e) => Verdict::of_error(e),
                });
                r
            }
        };
        self.inner
            .metrics
            .list(result.as_ref().map_or_else(ResolveError::outcome, |_| "ok"));
        result
    }

    async fn list_call(
        &self,
        provider: &str,
        capability: Option<&str>,
        after: i64,
        limit: u32,
    ) -> Result<BindingPage, ResolveError> {
        let token = self
            .inner
            .list_jwt
            .get_token()
            .await
            .map_err(|e| map_auth_error(&e))?;
        let request = bearer_request(
            ListBindingsRequest {
                provider: provider.to_string(),
                capability: capability.unwrap_or_default().to_string(),
                after,
                limit,
            },
            &token,
            self.inner.deadline,
        )
        .map_err(|_| ResolveError::Malformed("invalid token metadata"))?;
        let mut rpc = self.inner.rpc.clone();
        let response = rpc
            .list_bindings(request)
            .await
            .map_err(|s| map_status(&s))?;
        page_from_pb(response.into_inner())
    }
}

impl ConnectionResolve for ConnectionResolver {
    fn resolve(
        &self,
        r: ConnectionRef,
    ) -> impl Future<Output = Result<ResolvedConnection, ResolveError>> + Send {
        ConnectionResolver::resolve(self, r)
    }

    fn list_bindings(
        &self,
        provider: &str,
        capability: Option<&str>,
        after: i64,
        limit: u32,
    ) -> impl Future<Output = Result<BindingPage, ResolveError>> + Send {
        ConnectionResolver::list_bindings(self, provider, capability, after, limit)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct NoMetrics;
    impl ResolverMetrics for NoMetrics {
        fn resolve(&self, _: ConnectionKind, _: &'static str, _: Duration) {}
        fn list(&self, _: &'static str) {}
    }

    #[test]
    fn status_mapping_table() {
        let table: Vec<(Status, ResolveError)> = vec![
            (Status::not_found("x"), ResolveError::NotFound),
            (
                Status::failed_precondition("not_usable:pending_approval"),
                ResolveError::NotUsable(NotUsableReason::PendingApproval),
            ),
            (
                Status::failed_precondition("not_usable:token_expired"),
                ResolveError::NotUsable(NotUsableReason::TokenExpired),
            ),
            (
                Status::failed_precondition("not_usable:brand_new_code"),
                ResolveError::NotUsable(NotUsableReason::Other),
            ),
            (
                Status::failed_precondition("not_usable:"),
                ResolveError::NotUsable(NotUsableReason::Other),
            ),
            (
                Status::failed_precondition("<html>"),
                ResolveError::Malformed("invalid not_usable status"),
            ),
            (Status::unauthenticated("x"), ResolveError::Auth),
            (Status::permission_denied("x"), ResolveError::Auth),
            (
                Status::internal("x"),
                ResolveError::Transient("hub server error"),
            ),
            (
                Status::unavailable("x"),
                ResolveError::Transient("hub server error"),
            ),
            (
                Status::resource_exhausted("x"),
                ResolveError::Transient("hub rate limited or timed out"),
            ),
            (
                Status::deadline_exceeded("x"),
                ResolveError::Transient("hub rate limited or timed out"),
            ),
            (
                Status::invalid_argument("x"),
                ResolveError::Malformed("unexpected status"),
            ),
            (
                Status::unimplemented("x"),
                ResolveError::Malformed("unexpected status"),
            ),
        ];
        for (status, want) in table {
            assert_eq!(map_status(&status), want, "{:?}", status.code());
        }
    }

    #[test]
    fn status_mapping_never_echoes_server_text() {
        for status in [
            Status::internal("SECRET-LOOKING-sk-live-abc123 password=hunter2"),
            Status::invalid_argument("SECRET-LOOKING-sk-live-abc123 password=hunter2"),
            Status::failed_precondition("SECRET-LOOKING-sk-live-abc123 password=hunter2"),
        ] {
            let e = map_status(&status);
            let rendered = format!("{e} | {e:?}");
            assert!(
                !rendered.contains("SECRET") && !rendered.contains("hunter2"),
                "{rendered}"
            );
        }
    }

    #[tokio::test]
    async fn plaintext_http_hub_endpoint_refused_outside_alpha() {
        let mk = |url: &str, env: &str| {
            ConnectionResolver::new(
                ResolverConfig::new(url, env, "http://127.0.0.1:1", "/sa"),
                Arc::new(NoMetrics),
            )
        };
        assert!(mk("http://hub:50204", "beta").is_err());
        assert!(mk("http://hub:50204", "").is_err());
        assert!(mk("http://hub:50204", "prod").is_err());
        assert!(mk("http://hub:50204", "alpha").is_ok());
        assert!(mk("HTTP://hub:50204", "ALPHA").is_ok());
        assert!(mk("ftp://hub", "alpha").is_err());
        assert!(mk("hub:50204", "alpha").is_err());
    }

    #[tokio::test]
    async fn https_endpoint_with_unreadable_ca_is_refused_loudly() {
        let cfg = ResolverConfig::new(
            "https://hub:50204",
            "beta",
            "http://127.0.0.1:1",
            "/sa",
        )
        .with_grpc_ca(Some("/nonexistent/ca.pem".into()));
        assert_eq!(
            ConnectionResolver::new(cfg, Arc::new(NoMetrics)).unwrap_err(),
            ResolveError::Malformed("cannot build hub grpc channel")
        );
    }

    #[test]
    fn building_outside_a_tokio_runtime_is_an_error_not_a_panic() {
        let cfg = ResolverConfig::new("http://hub:50204", "alpha", "http://127.0.0.1:1", "/sa");
        assert_eq!(
            ConnectionResolver::new(cfg, Arc::new(NoMetrics)).unwrap_err(),
            ResolveError::Malformed("connection resolver must be built inside a tokio runtime")
        );
    }

    #[test]
    fn blank_ca_path_means_unset() {
        let cfg =
            ResolverConfig::new("https://h", "beta", "t", "/sa").with_grpc_ca(Some("  ".into()));
        assert!(cfg.grpc_ca_path.is_none());
    }
}
