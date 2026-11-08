//! `MockHub`: a real in-process tonic `ServiceAuthService` on a loopback port.
//!
//! Replaces the per-crate wiremock REST stubs for `/internal/service-token` and
//! `/internal/service-jwks.json` now that the bootstrap is gRPC. It is a genuine gRPC server
//! (real HTTP/2, real protobuf), so clients under test exercise their real transport and
//! codec; only hub-api's business logic is scripted. Replies are served in order and the last
//! one repeats. Compiled for this crate's tests and, behind `test-support`, for dependents'
//! tests; never part of a service binary.

use std::net::SocketAddr;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::sync::oneshot;
use tonic::{Request, Response, Status};

use crate::grpc::pb::service_auth_service_server::{ServiceAuthService, ServiceAuthServiceServer};
use crate::grpc::pb::{
    GetServiceJwksRequest, GetServiceJwksResponse, Jwk, MintServiceTokenRequest,
    MintServiceTokenResponse,
};

/// What the server saw for one `MintServiceToken` call.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MintCall {
    /// Bearer value from `authorization` metadata, `Bearer ` prefix stripped (the SA token).
    pub bearer: Option<String>,
    /// Requested scope.
    pub scope: String,
    /// Community binding, if sent.
    pub community_id: Option<i64>,
    /// Connection binding, if sent.
    pub connection_id: Option<i64>,
    /// `grpc-timeout` deadline the client sent, if any.
    pub deadline: Option<Duration>,
}

/// Callback computing a mint reply (token, expires_in) or a gRPC status from the call.
pub type MintFn = Arc<dyn Fn(&MintCall) -> Result<(String, u64), Status> + Send + Sync>;

/// One scripted `MintServiceToken` reply.
#[derive(Clone)]
pub enum MintReply {
    /// Fixed token + lifetime.
    Token {
        /// The JWT string.
        token: String,
        /// Lifetime in seconds.
        expires_in: u64,
    },
    /// Fail with this gRPC code.
    Reject(tonic::Code),
    /// Compute the reply from the call (e.g. sign a real JWT).
    With(MintFn),
}

impl MintReply {
    /// Shorthand for [`MintReply::Token`].
    #[must_use]
    pub fn token(token: &str, expires_in: u64) -> Self {
        Self::Token {
            token: token.to_owned(),
            expires_in,
        }
    }
}

/// One scripted `GetServiceJwks` reply.
#[derive(Clone)]
pub enum JwksReply {
    /// Serve these keys.
    Keys(Vec<Jwk>),
    /// Fail with this gRPC code.
    Reject(tonic::Code),
}

impl JwksReply {
    /// One Ed25519 key (`x` = base64url raw public key).
    #[must_use]
    pub fn ed25519(kid: &str, x: &str) -> Self {
        Self::Keys(vec![ed25519_jwk(kid, x)])
    }
}

/// An Ed25519 JWK entry as hub-api serves it.
#[must_use]
pub fn ed25519_jwk(kid: &str, x: &str) -> Jwk {
    Jwk {
        kid: kid.to_owned(),
        kty: "OKP".into(),
        crv: "Ed25519".into(),
        alg: "EdDSA".into(),
        r#use: "sig".into(),
        x: x.to_owned(),
        y: String::new(),
    }
}

struct State {
    mint: Vec<MintReply>,
    jwks: Vec<JwksReply>,
    delay: Duration,
    mint_calls: Mutex<Vec<MintCall>>,
    mint_hits: AtomicUsize,
    jwks_hits: AtomicUsize,
}

struct Svc(Arc<State>);

fn pick<T: Clone>(replies: &[T], n: usize) -> Option<T> {
    replies.get(n.min(replies.len().saturating_sub(1))).cloned()
}

#[tonic::async_trait]
impl ServiceAuthService for Svc {
    async fn mint_service_token(
        &self,
        request: Request<MintServiceTokenRequest>,
    ) -> Result<Response<MintServiceTokenResponse>, Status> {
        let n = self.0.mint_hits.fetch_add(1, Ordering::SeqCst);
        let deadline = request
            .metadata()
            .get("grpc-timeout")
            .and_then(|v| v.to_str().ok())
            .and_then(parse_grpc_timeout);
        let bearer = request
            .metadata()
            .get("authorization")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.strip_prefix("Bearer "))
            .map(str::to_owned);
        let body = request.into_inner();
        let call = MintCall {
            bearer,
            scope: body.scope,
            community_id: body.community_id,
            connection_id: body.connection_id,
            deadline,
        };
        if let Ok(mut calls) = self.0.mint_calls.lock() {
            calls.push(call.clone());
        }
        tokio::time::sleep(self.0.delay).await;
        let reply = pick(&self.0.mint, n)
            .ok_or_else(|| Status::unimplemented("MockHub: no mint replies scripted"))?;
        let (token, expires_in) = match reply {
            MintReply::Token { token, expires_in } => (token, expires_in),
            MintReply::Reject(code) => return Err(Status::new(code, "unauthorized")),
            MintReply::With(f) => f(&call)?,
        };
        Ok(Response::new(MintServiceTokenResponse {
            token,
            expires_in,
        }))
    }

    async fn get_service_jwks(
        &self,
        _request: Request<GetServiceJwksRequest>,
    ) -> Result<Response<GetServiceJwksResponse>, Status> {
        let n = self.0.jwks_hits.fetch_add(1, Ordering::SeqCst);
        tokio::time::sleep(self.0.delay).await;
        match pick(&self.0.jwks, n)
            .ok_or_else(|| Status::unimplemented("MockHub: no JWKS replies scripted"))?
        {
            JwksReply::Keys(keys) => Ok(Response::new(GetServiceJwksResponse { keys })),
            JwksReply::Reject(code) => Err(Status::new(code, "rejected")),
        }
    }
}

/// Parse a `grpc-timeout` header value (`<digits><H|M|S|m|u|n>`).
fn parse_grpc_timeout(raw: &str) -> Option<Duration> {
    let (digits, unit) = raw.split_at(raw.len().checked_sub(1)?);
    let n: u64 = digits.parse().ok()?;
    Some(match unit {
        "H" => Duration::from_secs(n * 3600),
        "M" => Duration::from_secs(n * 60),
        "S" => Duration::from_secs(n),
        "m" => Duration::from_millis(n),
        "u" => Duration::from_micros(n),
        "n" => Duration::from_nanos(n),
        _ => return None,
    })
}

/// Builder for [`MockHub`].
#[derive(Default)]
pub struct MockHubBuilder {
    mint: Vec<MintReply>,
    jwks: Vec<JwksReply>,
    delay: Duration,
}

impl MockHubBuilder {
    /// Scripted `MintServiceToken` replies (in order; the last repeats).
    #[must_use]
    pub fn mint(mut self, replies: Vec<MintReply>) -> Self {
        self.mint = replies;
        self
    }

    /// Scripted `GetServiceJwks` replies (in order; the last repeats).
    #[must_use]
    pub fn jwks(mut self, replies: Vec<JwksReply>) -> Self {
        self.jwks = replies;
        self
    }

    /// Wait this long before answering, so concurrent callers genuinely overlap.
    #[must_use]
    pub fn delay(mut self, delay: Duration) -> Self {
        self.delay = delay;
        self
    }

    /// Bind loopback plaintext and start serving.
    pub async fn start(self) -> MockHub {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .unwrap_or_else(|e| panic!("MockHub: bind: {e}"));
        let addr = listener
            .local_addr()
            .unwrap_or_else(|e| panic!("MockHub: local_addr: {e}"));
        self.spawn(listener, addr, None)
    }

    /// As [`MockHubBuilder::start`] for synchronous fixtures: binds with `std` and registers the
    /// socket with the *current* tokio runtime, so it must be called from within one.
    pub fn start_sync(self) -> MockHub {
        let std_listener = std::net::TcpListener::bind("127.0.0.1:0")
            .unwrap_or_else(|e| panic!("MockHub: bind: {e}"));
        std_listener
            .set_nonblocking(true)
            .unwrap_or_else(|e| panic!("MockHub: set_nonblocking: {e}"));
        let addr = std_listener
            .local_addr()
            .unwrap_or_else(|e| panic!("MockHub: local_addr: {e}"));
        let listener = tokio::net::TcpListener::from_std(std_listener)
            .unwrap_or_else(|e| panic!("MockHub: from_std (needs a tokio runtime): {e}"));
        self.spawn(listener, addr, None)
    }

    /// Serve on an already-bound listener, optionally over TLS (tests supply the server config).
    pub fn spawn(
        self,
        listener: tokio::net::TcpListener,
        addr: SocketAddr,
        tls: Option<tonic::transport::ServerTlsConfig>,
    ) -> MockHub {
        let state = Arc::new(State {
            mint: self.mint,
            jwks: self.jwks,
            delay: self.delay,
            mint_calls: Mutex::new(Vec::new()),
            mint_hits: AtomicUsize::new(0),
            jwks_hits: AtomicUsize::new(0),
        });
        let scheme = if tls.is_some() { "https" } else { "http" };
        let (tx, rx) = oneshot::channel::<()>();
        let mut server = tonic::transport::Server::builder();
        if let Some(tls) = tls {
            server = server
                .tls_config(tls)
                .unwrap_or_else(|e| panic!("MockHub: tls_config: {e}"));
        }
        let incoming = tonic::transport::server::TcpIncoming::from(listener);
        let svc = ServiceAuthServiceServer::new(Svc(state.clone()));
        tokio::spawn(async move {
            let _ = server
                .add_service(svc)
                .serve_with_incoming_shutdown(incoming, async {
                    let _ = rx.await;
                })
                .await;
        });
        MockHub {
            endpoint: format!("{scheme}://{addr}"),
            state,
            _shutdown: tx,
        }
    }
}

/// A running mock hub-api `ServiceAuthService`. Dropping it shuts the server down.
pub struct MockHub {
    /// `http://127.0.0.1:<port>` -- pass as the hub gRPC endpoint.
    pub endpoint: String,
    state: Arc<State>,
    _shutdown: oneshot::Sender<()>,
}

impl MockHub {
    /// Start building a mock.
    #[must_use]
    pub fn builder() -> MockHubBuilder {
        MockHubBuilder::default()
    }

    /// `MintServiceToken` calls received so far.
    #[must_use]
    pub fn mint_calls(&self) -> Vec<MintCall> {
        self.state
            .mint_calls
            .lock()
            .map(|c| c.clone())
            .unwrap_or_default()
    }

    /// Number of `MintServiceToken` calls received.
    #[must_use]
    pub fn mint_hits(&self) -> usize {
        self.state.mint_hits.load(Ordering::SeqCst)
    }

    /// Number of `GetServiceJwks` calls received.
    #[must_use]
    pub fn jwks_hits(&self) -> usize {
        self.state.jwks_hits.load(Ordering::SeqCst)
    }
}
