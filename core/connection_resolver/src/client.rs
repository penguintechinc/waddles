//! HTTP client for hub-api's internal resolve/list endpoints.

use std::future::Future;
use std::sync::Arc;
use std::time::Duration;

use serde::Deserialize;
use tracing::Instrument;

use crate::breaker::{Breaker, Verdict};
use crate::error::{NotUsableReason, ResolveError};
use crate::types::{
    parse_page, parse_resolved, BindingPage, ConnectionKind, ConnectionRef, ResolvedConnection,
};

/// Scope for OAuth-only resolves (community-bound).
pub const SCOPE_CONNECTIONS_RESOLVE: &str = "connections:resolve";
/// Scope that additionally allows static-secret connections to resolve (community-bound).
pub const SCOPE_CONNECTIONS_RESOLVE_STATIC: &str = "connections:resolve:static";
/// Scope for the unbound data-plane binding list.
pub const SCOPE_CONNECTIONS_LIST: &str = "connections:list";

/// Static configuration of a [`ConnectionResolver`].
#[derive(Clone, Debug)]
pub struct ResolverConfig {
    /// hub-api base URL (`HUB_API_URL`); plaintext `http://` is accepted only in alpha.
    pub hub_api_url: String,
    /// Deployment environment name (`alpha`/`beta`/...), used for the transport policy.
    pub environment: String,
    /// hub-api machine-token endpoint (`.../internal/service-token`).
    pub token_endpoint: String,
    /// Path of the projected ServiceAccount token used to bootstrap machine JWTs.
    pub sa_token_path: String,
    /// Scope requested for resolve tokens: [`SCOPE_CONNECTIONS_RESOLVE`] or
    /// [`SCOPE_CONNECTIONS_RESOLVE_STATIC`] (the latter only for callers allow-listed for it).
    pub resolve_scope: String,
    /// Total per-request timeout (default 3s).
    pub request_timeout: Duration,
    /// Connect timeout (default 2s).
    pub connect_timeout: Duration,
    /// Consecutive transient failures before the breaker opens (default 5).
    pub breaker_threshold: u32,
    /// How long the breaker sheds load before probing (default 10s).
    pub breaker_cooldown: Duration,
}

impl ResolverConfig {
    /// Config with the standard timeouts/breaker and the OAuth-only resolve scope.
    pub fn new(
        hub_api_url: impl Into<String>,
        environment: impl Into<String>,
        token_endpoint: impl Into<String>,
        sa_token_path: impl Into<String>,
    ) -> Self {
        Self {
            hub_api_url: hub_api_url.into(),
            environment: environment.into(),
            token_endpoint: token_endpoint.into(),
            sa_token_path: sa_token_path.into(),
            resolve_scope: SCOPE_CONNECTIONS_RESOLVE.to_string(),
            request_timeout: Duration::from_secs(3),
            connect_timeout: Duration::from_secs(2),
            breaker_threshold: 5,
            breaker_cooldown: Duration::from_secs(10),
        }
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
    http: reqwest::Client,
    base_url: String,
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
        f.debug_struct("ConnectionResolver")
            .field("base_url", &self.inner.base_url)
            .finish_non_exhaustive()
    }
}

/// Plaintext `http://` is allowed only in alpha; anything but http(s) is refused.
/// Same policy as svc_ingest's `check_transport`.
fn check_transport(hub_api_url: &str, environment: &str) -> Result<(), ResolveError> {
    let lower = hub_api_url.trim().to_ascii_lowercase();
    if lower.starts_with("https://") {
        return Ok(());
    }
    if lower.starts_with("http://") {
        if environment.eq_ignore_ascii_case("alpha") {
            return Ok(());
        }
        return Err(ResolveError::Malformed(
            "plaintext http hub url refused outside alpha",
        ));
    }
    Err(ResolveError::Malformed("hub url must be http(s)"))
}

/// Owned response body that is zeroized on drop, so every exit path (success, parse error,
/// early `?`) wipes the plaintext secrets it may hold. reqwest's internal `Bytes` is outside
/// our control and remains an accepted residual.
struct ZeroizingBody(Vec<u8>);

impl std::ops::Deref for ZeroizingBody {
    type Target = [u8];
    fn deref(&self) -> &[u8] {
        &self.0
    }
}

impl Drop for ZeroizingBody {
    fn drop(&mut self) {
        wipe(&mut self.0);
    }
}

/// Zero every byte of the buffer's allocation and clear it.
fn wipe(buf: &mut Vec<u8>) {
    zeroize::Zeroize::zeroize(buf);
}

#[derive(Deserialize)]
struct NotUsableWire {
    reason: Option<String>,
}

/// Map a non-success status (and, for 409, the flat `not_usable` body) to an error.
fn map_status(status: reqwest::StatusCode, body: &[u8]) -> ResolveError {
    match status.as_u16() {
        404 => ResolveError::NotFound,
        409 => match serde_json::from_slice::<NotUsableWire>(body) {
            Ok(w) => ResolveError::NotUsable(
                w.reason
                    .as_deref()
                    .map_or(NotUsableReason::Other, NotUsableReason::from_wire),
            ),
            Err(_) => ResolveError::Malformed("invalid not_usable body"),
        },
        401 | 403 => ResolveError::Auth,
        408 | 429 => ResolveError::Transient("hub rate limited or timed out"),
        s if s >= 500 => ResolveError::Transient("hub server error"),
        _ => ResolveError::Malformed("unexpected status"),
    }
}

/// Map a token-mint failure: a rejected bootstrap is auth, operational failures are transient.
fn map_auth_error(e: &service_auth::ServiceAuthError) -> ResolveError {
    use service_auth::ServiceAuthError as E;
    match e {
        E::BootstrapRejected(_) | E::InvalidToken(_) | E::UnknownKeyId(_) => ResolveError::Auth,
        E::Http(_) => ResolveError::Transient("token endpoint unreachable"),
        E::Io(_) => ResolveError::Transient("service account token unreadable"),
    }
}

impl ConnectionResolver {
    /// Build a resolver; refuses a plaintext hub URL outside alpha.
    pub fn new(
        cfg: ResolverConfig,
        metrics: Arc<dyn ResolverMetrics>,
    ) -> Result<Self, ResolveError> {
        check_transport(&cfg.hub_api_url, &cfg.environment)?;
        let timeouts = service_auth::HttpTimeouts {
            connect: cfg.connect_timeout,
            request: cfg.request_timeout,
        };
        let http = reqwest::Client::builder()
            .connect_timeout(cfg.connect_timeout)
            .timeout(cfg.request_timeout)
            .build()
            .map_err(|_| ResolveError::Malformed("cannot build http client"))?;
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
            SCOPE_CONNECTIONS_LIST,
            timeouts,
        )
        .map_err(|_| ResolveError::Malformed("cannot build token client"))?;
        Ok(Self {
            inner: Arc::new(Inner {
                http,
                base_url: cfg.hub_api_url.trim().trim_end_matches('/').to_string(),
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
        let url = format!(
            "{}/api/v1/internal/communities/{}/provider-connections/{}/resolve",
            self.inner.base_url, r.community_id, r.connection_id
        );
        let (status, body) = self
            .send(self.inner.http.post(url).bearer_auth(&*token))
            .await?;
        if status.is_success() {
            parse_resolved(&body, r)
        } else {
            Err(map_status(status, &body))
        }
    }

    /// Send a request and read the whole body; network/timeout failures are transient.
    async fn send(
        &self,
        req: reqwest::RequestBuilder,
    ) -> Result<(reqwest::StatusCode, ZeroizingBody), ResolveError> {
        let resp = req
            .send()
            .await
            .map_err(|_| ResolveError::Transient("hub request failed"))?;
        let status = resp.status();
        let body = resp
            .bytes()
            .await
            .map_err(|_| ResolveError::Transient("hub response read failed"))?;
        Ok((status, ZeroizingBody(Vec::from(body))))
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
                let r = self.list_call(provider, capability, after, limit).await;
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
        let mut query = vec![
            ("provider", provider.to_string()),
            ("after", after.to_string()),
            ("limit", limit.to_string()),
        ];
        if let Some(c) = capability {
            query.push(("capability", c.to_string()));
        }
        let url = format!(
            "{}/api/v1/internal/provider-connections",
            self.inner.base_url
        );
        let req = self.inner.http.get(url).bearer_auth(&*token).query(&query);
        let (status, body) = self.send(req).await?;
        if status.is_success() {
            parse_page(&body)
        } else {
            Err(map_status(status, &body))
        }
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
pub(crate) mod test_support {
    //! Fake-hub helpers shared by this crate's unit and contract tests.

    use super::*;
    use std::sync::Mutex;
    use wiremock::matchers::{body_partial_json, header, method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    /// Metrics sink recording every call as `kind:outcome` strings.
    #[derive(Default)]
    pub(crate) struct Rec {
        pub(crate) calls: Mutex<Vec<String>>,
    }

    impl ResolverMetrics for Rec {
        fn resolve(&self, kind: ConnectionKind, outcome: &'static str, _l: Duration) {
            self.calls
                .lock()
                .unwrap()
                .push(format!("resolve:{}:{outcome}", kind.code()));
        }
        fn resolve_failed(&self, outcome: &'static str, _l: Duration) {
            self.calls
                .lock()
                .unwrap()
                .push(format!("resolve_failed:{outcome}"));
        }
        fn list(&self, outcome: &'static str) {
            self.calls.lock().unwrap().push(format!("list:{outcome}"));
        }
    }

    /// Resolver config pointed at `server`, with a throwaway SA token file.
    pub(crate) fn cfg(server: &MockServer) -> ResolverConfig {
        let sa = std::env::temp_dir().join(format!(
            "conn-resolver-sa-{}-{}",
            std::process::id(),
            server.address().port()
        ));
        std::fs::write(&sa, "projected-sa-token").unwrap();
        let mut c = ResolverConfig::new(
            server.uri(),
            "alpha",
            format!("{}/internal/service-token", server.uri()),
            sa.to_string_lossy().to_string(),
        );
        c.request_timeout = Duration::from_millis(500);
        c.breaker_threshold = 1000;
        c
    }

    /// Mount a token mint answering only for the exact bound request, with `token`.
    pub(crate) async fn mount_bound_token(
        server: &MockServer,
        community: i64,
        conn: i64,
        token: &str,
    ) {
        Mock::given(method("POST"))
            .and(path("/internal/service-token"))
            .and(body_partial_json(serde_json::json!({
                "scope": SCOPE_CONNECTIONS_RESOLVE,
                "community_id": community,
                "connection_id": conn,
            })))
            .respond_with(
                ResponseTemplate::new(200)
                    .set_body_json(serde_json::json!({"token": token, "expires_in": 3600})),
            )
            .mount(server)
            .await;
    }

    /// Mount a resolve answering only when the bearer is `token`.
    pub(crate) async fn mount_resolve(
        server: &MockServer,
        community: i64,
        conn: i64,
        token: &str,
        resp: ResponseTemplate,
    ) {
        Mock::given(method("POST"))
            .and(path(format!(
                "/api/v1/internal/communities/{community}/provider-connections/{conn}/resolve"
            )))
            .and(header("authorization", format!("Bearer {token}").as_str()))
            .respond_with(resp)
            .mount(server)
            .await;
    }

    /// Count requests the fake hub received on `path_suffix`.
    pub(crate) async fn hits(server: &MockServer, path_suffix: &str) -> usize {
        server
            .received_requests()
            .await
            .unwrap()
            .iter()
            .filter(|r| r.url.path().ends_with(path_suffix))
            .count()
    }

    pub(crate) const OAUTH_BODY: &str = r#"{"success":true,"communityId":7,"connectionId":12,
        "provider":"twitch","kind":1,"accessToken":"PLAIN-ACCESS","expiresAt":null,
        "endpoint":{"url":null,"region":null,"function":null,"project":null},
        "metadata":{},"secret":null}"#;

    pub(crate) const R: ConnectionRef = ConnectionRef {
        community_id: 7,
        connection_id: 12,
    };
}

#[cfg(test)]
mod tests {
    use super::test_support::*;
    use super::*;
    use crate::types::ConnectionSecret;
    use std::sync::Mutex;
    use wiremock::matchers::{body_partial_json, method, path, query_param};
    use wiremock::{Mock, MockServer, Request, Respond, ResponseTemplate};

    fn ok_oauth() -> ResponseTemplate {
        ResponseTemplate::new(200).set_body_raw(OAUTH_BODY, "application/json")
    }

    async fn resolver(server: &MockServer) -> (ConnectionResolver, Arc<Rec>) {
        let rec = Arc::new(Rec::default());
        let r = ConnectionResolver::new(cfg(server), rec.clone()).unwrap();
        (r, rec)
    }

    #[test]
    fn wipe_zeroes_the_whole_allocation() {
        let mut v = b"PLAIN-secret-body".to_vec();
        let len = v.len();
        wipe(&mut v);
        assert!(v.is_empty());
        assert!(v.capacity() >= len);
        // SAFETY: the first `len` bytes of the allocation were initialised (zeroed by wipe).
        unsafe { v.set_len(len) };
        assert!(v.iter().all(|b| *b == 0), "{v:?}");
        let guard = ZeroizingBody(b"abc".to_vec());
        assert_eq!(&guard[..], b"abc");
    }

    #[tokio::test]
    async fn two_resolves_make_two_hub_calls() {
        let server = MockServer::start().await;
        mount_bound_token(&server, 7, 12, "tok-7-12").await;
        mount_resolve(&server, 7, 12, "tok-7-12", ok_oauth()).await;
        let (r, _) = resolver(&server).await;
        for _ in 0..2 {
            let c = r.resolve(R).await.unwrap();
            match &c.secret {
                ConnectionSecret::OAuthAccessToken { access_token } => {
                    assert_eq!(access_token.expose(), "PLAIN-ACCESS");
                }
                other => panic!("unexpected secret {other:?}"),
            }
        }
        assert_eq!(hits(&server, "/resolve").await, 2, "no credential cache");
        assert_eq!(
            hits(&server, "/service-token").await,
            1,
            "only the bearer is cached"
        );
    }

    #[tokio::test]
    async fn resolve_sends_bound_bearer_for_that_community() {
        let server = MockServer::start().await;
        mount_bound_token(&server, 7, 12, "tok-7-12").await;
        mount_bound_token(&server, 8, 13, "tok-8-13").await;
        // Each resolve only answers 200 to its own community's bearer (default is 404).
        mount_resolve(&server, 7, 12, "tok-7-12", ok_oauth()).await;
        mount_resolve(
            &server,
            8,
            13,
            "tok-8-13",
            ResponseTemplate::new(200).set_body_raw(
                OAUTH_BODY
                    .replace("\"communityId\":7", "\"communityId\":8")
                    .replace("12,", "13,"),
                "application/json",
            ),
        )
        .await;
        let (r, _) = resolver(&server).await;
        assert!(r.resolve(R).await.is_ok());
        let other = ConnectionRef {
            community_id: 8,
            connection_id: 13,
        };
        assert!(r.resolve(other).await.is_ok());
        // The 7-bearer must not open 8's resource: a body for another connection is refused.
        let mismatched = ConnectionRef {
            community_id: 7,
            connection_id: 99,
        };
        mount_bound_token(&server, 7, 99, "tok-7-99").await;
        mount_resolve(&server, 7, 99, "tok-7-99", ok_oauth()).await;
        assert_eq!(
            r.resolve(mismatched).await.unwrap_err(),
            ResolveError::Malformed("resolve body is for another connection")
        );
    }

    #[tokio::test]
    async fn status_mapping_table() {
        let table: Vec<(u16, &str, ResolveError)> = vec![
            (404, "{}", ResolveError::NotFound),
            (
                409,
                r#"{"success":false,"error":"not_usable","reason":"pending_approval"}"#,
                ResolveError::NotUsable(NotUsableReason::PendingApproval),
            ),
            (
                409,
                r#"{"success":false,"error":"not_usable","reason":"token_expired"}"#,
                ResolveError::NotUsable(NotUsableReason::TokenExpired),
            ),
            (
                409,
                r#"{"success":false,"error":"not_usable","reason":"brand_new_code"}"#,
                ResolveError::NotUsable(NotUsableReason::Other),
            ),
            (
                409,
                r#"{"success":false,"error":"not_usable"}"#,
                ResolveError::NotUsable(NotUsableReason::Other),
            ),
            (
                409,
                "<html>",
                ResolveError::Malformed("invalid not_usable body"),
            ),
            (401, "{}", ResolveError::Auth),
            (403, "{}", ResolveError::Auth),
            (500, "{}", ResolveError::Transient("hub server error")),
            (
                429,
                "{}",
                ResolveError::Transient("hub rate limited or timed out"),
            ),
            (
                408,
                "{}",
                ResolveError::Transient("hub rate limited or timed out"),
            ),
            (503, "{}", ResolveError::Transient("hub server error")),
            (400, "{}", ResolveError::Malformed("unexpected status")),
            (
                200,
                "not json",
                ResolveError::Malformed("invalid resolve body"),
            ),
        ];
        for (status, body, want) in table {
            let server = MockServer::start().await;
            mount_bound_token(&server, 7, 12, "t").await;
            mount_resolve(
                &server,
                7,
                12,
                "t",
                ResponseTemplate::new(status).set_body_raw(body, "application/json"),
            )
            .await;
            let (r, _) = resolver(&server).await;
            assert_eq!(
                r.resolve(R).await.unwrap_err(),
                want,
                "status {status} body {body}"
            );
        }
    }

    #[tokio::test]
    async fn timeout_and_unreachable_hub_are_transient() {
        let server = MockServer::start().await;
        mount_bound_token(&server, 7, 12, "t").await;
        mount_resolve(
            &server,
            7,
            12,
            "t",
            ok_oauth().set_delay(Duration::from_secs(2)),
        )
        .await;
        let (r, _) = resolver(&server).await;
        let e = r.resolve(R).await.unwrap_err();
        assert_eq!(e, ResolveError::Transient("hub request failed"));
        assert!(e.is_transient());

        // Hub down entirely: mint succeeds from a live token server, resolve connects to a dead port.
        let mut c = cfg(&server);
        c.hub_api_url = "http://127.0.0.1:1".to_string();
        let r2 = ConnectionResolver::new(c, Arc::new(Rec::default())).unwrap();
        assert!(matches!(
            r2.resolve(R).await,
            Err(ResolveError::Transient(_))
        ));
    }

    #[tokio::test]
    async fn token_failures_map_to_auth_or_transient() {
        // Token endpoint rejects the bootstrap -> Auth.
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/internal/service-token"))
            .respond_with(ResponseTemplate::new(401))
            .mount(&server)
            .await;
        let (r, _) = resolver(&server).await;
        assert_eq!(r.resolve(R).await.unwrap_err(), ResolveError::Auth);
        assert_eq!(
            r.list_bindings("twitch", None, 0, 10).await.unwrap_err(),
            ResolveError::Auth
        );

        // Token endpoint unreachable -> Transient.
        let mut c = cfg(&server);
        c.token_endpoint = "http://127.0.0.1:1/internal/service-token".to_string();
        let r2 = ConnectionResolver::new(c, Arc::new(Rec::default())).unwrap();
        assert_eq!(
            r2.resolve(R).await.unwrap_err(),
            ResolveError::Transient("token endpoint unreachable")
        );

        // Unreadable SA token file -> Transient.
        let mut c = cfg(&server);
        c.sa_token_path = "/nonexistent/sa-token".to_string();
        let r3 = ConnectionResolver::new(c, Arc::new(Rec::default())).unwrap();
        assert_eq!(
            r3.resolve(R).await.unwrap_err(),
            ResolveError::Transient("service account token unreadable")
        );
    }

    #[tokio::test]
    async fn auth_and_notfound_are_not_transient() {
        for status in [401u16, 404] {
            let server = MockServer::start().await;
            mount_bound_token(&server, 7, 12, "t").await;
            mount_resolve(&server, 7, 12, "t", ResponseTemplate::new(status)).await;
            let mut c = cfg(&server);
            c.breaker_threshold = 2;
            let r = ConnectionResolver::new(c, Arc::new(Rec::default())).unwrap();
            for _ in 0..6 {
                let e = r.resolve(R).await.unwrap_err();
                assert!(!e.is_transient(), "{e:?}");
                assert_ne!(e, ResolveError::CircuitOpen, "breaker must stay neutral");
            }
            assert_eq!(hits(&server, "/resolve").await, 6);
        }
    }

    #[tokio::test]
    async fn breaker_sheds_after_threshold_without_hitting_hub() {
        let server = MockServer::start().await;
        mount_bound_token(&server, 7, 12, "t").await;
        mount_resolve(&server, 7, 12, "t", ResponseTemplate::new(500)).await;
        let mut c = cfg(&server);
        c.breaker_threshold = 2;
        c.breaker_cooldown = Duration::from_secs(60);
        let rec = Arc::new(Rec::default());
        let r = ConnectionResolver::new(c, rec.clone()).unwrap();
        for _ in 0..2 {
            assert!(matches!(
                r.resolve(R).await,
                Err(ResolveError::Transient(_))
            ));
        }
        for _ in 0..3 {
            let e = r.resolve(R).await.unwrap_err();
            assert_eq!(e, ResolveError::CircuitOpen);
            assert!(e.is_transient());
        }
        assert_eq!(
            r.list_bindings("twitch", None, 0, 1).await.unwrap_err(),
            ResolveError::CircuitOpen
        );
        assert_eq!(
            hits(&server, "/resolve").await,
            2,
            "open breaker must not call the hub"
        );
        let calls = rec.calls.lock().unwrap().clone();
        assert!(calls.contains(&"resolve_failed:circuit_open".to_string()));
        assert!(calls.contains(&"list:circuit_open".to_string()));
    }

    #[test]
    fn plaintext_http_hub_url_refused_outside_alpha() {
        let mk = |url: &str, env: &str| {
            let mut c = ResolverConfig::new(url, env, "http://t/internal/service-token", "/sa");
            c.breaker_threshold = 5;
            ConnectionResolver::new(c, Arc::new(Rec::default()))
        };
        assert!(mk("http://hub:8204", "beta").is_err());
        assert!(mk("http://hub:8204", "").is_err());
        assert!(mk("http://hub:8204", "prod").is_err());
        assert!(mk("http://hub:8204", "alpha").is_ok());
        assert!(mk("HTTP://hub:8204", "ALPHA").is_ok());
        assert!(mk("https://hub:8204", "beta").is_ok());
        assert!(mk("ftp://hub", "alpha").is_err());
        assert!(mk("hub:8204", "alpha").is_err());
    }

    #[tokio::test]
    async fn list_bindings_uses_unbound_list_scope_and_paginates() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/internal/service-token"))
            .and(body_partial_json(
                serde_json::json!({"scope": SCOPE_CONNECTIONS_LIST}),
            ))
            .respond_with(
                ResponseTemplate::new(200)
                    .set_body_json(serde_json::json!({"token": "list-tok", "expires_in": 3600})),
            )
            .mount(&server)
            .await;
        let page = |after: &'static str, items: &str, next: &str| {
            let body = format!(r#"{{"success":true,"items":[{items}],"nextAfter":{next}}}"#);
            Mock::given(method("GET"))
                .and(path("/api/v1/internal/provider-connections"))
                .and(wiremock::matchers::header(
                    "authorization",
                    "Bearer list-tok",
                ))
                .and(query_param("provider", "twitch"))
                .and(query_param("capability", "chat.send"))
                .and(query_param("limit", "1"))
                .and(query_param("after", after))
                .respond_with(ResponseTemplate::new(200).set_body_raw(body, "application/json"))
        };
        let item = |c: i64| {
            format!(
                r#"{{"communityId":{c},"connectionId":{c},"provider":"twitch","metadata":{{"channel":"x"}}}}"#
            )
        };
        page("0", &item(1), "1").mount(&server).await;
        page("1", &item(2), "null").mount(&server).await;
        let (r, rec) = resolver(&server).await;
        let p1 = r
            .list_bindings("twitch", Some("chat.send"), 0, 1)
            .await
            .unwrap();
        assert_eq!(p1.next_after, Some(1));
        assert_eq!(p1.items[0].community_id, 1);
        assert_eq!(
            p1.items[0].metadata.get("channel").map(String::as_str),
            Some("x")
        );
        let p2 = r
            .list_bindings("twitch", Some("chat.send"), 1, 1)
            .await
            .unwrap();
        assert_eq!(p2.next_after, None);
        // The list token request carried no binding fields (unbound scope).
        let mints: Vec<_> = server
            .received_requests()
            .await
            .unwrap()
            .into_iter()
            .filter(|q| q.url.path().ends_with("/service-token"))
            .collect();
        assert_eq!(mints.len(), 1);
        let body = String::from_utf8_lossy(&mints[0].body).to_string();
        assert!(
            !body.contains("community_id") && !body.contains("connection_id"),
            "{body}"
        );
        assert_eq!(
            rec.calls
                .lock()
                .unwrap()
                .iter()
                .filter(|c| *c == "list:ok")
                .count(),
            2
        );
    }

    #[tokio::test]
    async fn list_bindings_maps_errors_without_capability() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/internal/service-token"))
            .respond_with(
                ResponseTemplate::new(200)
                    .set_body_json(serde_json::json!({"token":"t","expires_in":3600})),
            )
            .mount(&server)
            .await;
        Mock::given(method("GET"))
            .and(path("/api/v1/internal/provider-connections"))
            .respond_with(ResponseTemplate::new(200).set_body_raw("{", "application/json"))
            .mount(&server)
            .await;
        let (r, rec) = resolver(&server).await;
        assert_eq!(
            r.list_bindings("x", None, 0, 5).await.unwrap_err(),
            ResolveError::Malformed("invalid list body")
        );
        let q: Vec<_> = server
            .received_requests()
            .await
            .unwrap()
            .into_iter()
            .filter(|q| q.url.path().ends_with("/provider-connections"))
            .collect();
        assert!(!q[0].url.query().unwrap_or("").contains("capability"));
        assert_eq!(rec.calls.lock().unwrap().as_slice(), ["list:malformed"]);
    }

    #[tokio::test]
    async fn resolve_error_never_contains_response_body() {
        let server = MockServer::start().await;
        mount_bound_token(&server, 7, 12, "t").await;
        for status in [500u16, 400, 409] {
            server.reset().await;
            mount_bound_token(&server, 7, 12, "t").await;
            mount_resolve(
                &server,
                7,
                12,
                "t",
                ResponseTemplate::new(status).set_body_raw(
                    "SECRET-LOOKING-sk-live-abc123 password=hunter2",
                    "text/plain",
                ),
            )
            .await;
            let (r, _) = resolver(&server).await;
            let e = r.resolve(R).await.unwrap_err();
            let rendered = format!("{e} | {e:?}");
            assert!(
                !rendered.contains("SECRET") && !rendered.contains("hunter2"),
                "{rendered}"
            );
        }
    }

    #[tokio::test]
    async fn metrics_emitted_once_per_resolve_without_secret_labels() {
        let server = MockServer::start().await;
        mount_bound_token(&server, 7, 12, "t").await;
        mount_resolve(&server, 7, 12, "t", ok_oauth()).await;
        mount_bound_token(&server, 7, 13, "t2").await;
        mount_resolve(&server, 7, 13, "t2", ResponseTemplate::new(404)).await;
        let (r, rec) = resolver(&server).await;
        r.resolve(R).await.unwrap();
        r.resolve(ConnectionRef {
            community_id: 7,
            connection_id: 13,
        })
        .await
        .unwrap_err();
        let calls = rec.calls.lock().unwrap().clone();
        assert_eq!(
            calls,
            vec![
                "resolve:1:ok".to_string(),
                "resolve_failed:not_found".to_string()
            ]
        );
        for c in &calls {
            assert!(!c.contains("PLAIN") && !c.contains("tok"), "{c}");
        }
    }

    #[tokio::test]
    async fn trait_seam_delegates_to_the_real_client() {
        async fn through<T: ConnectionResolve>(t: &T) -> (bool, bool) {
            (
                t.resolve(R).await.is_ok(),
                t.list_bindings("p", None, 0, 1).await.is_err(),
            )
        }
        let server = MockServer::start().await;
        mount_bound_token(&server, 7, 12, "t").await;
        mount_resolve(&server, 7, 12, "t", ok_oauth()).await;
        let (r, _) = resolver(&server).await;
        assert_eq!(through(&r).await, (true, true));
        assert!(format!("{r:?}").contains("ConnectionResolver"));
    }

    struct Seq(Mutex<u32>);
    impl Respond for Seq {
        fn respond(&self, _: &Request) -> ResponseTemplate {
            *self.0.lock().unwrap() += 1;
            ResponseTemplate::new(200).set_body_raw(OAUTH_BODY, "application/json")
        }
    }

    #[tokio::test]
    async fn each_resolve_hits_hub_even_concurrently() {
        let server = MockServer::start().await;
        mount_bound_token(&server, 7, 12, "t").await;
        Mock::given(method("POST"))
            .and(path(
                "/api/v1/internal/communities/7/provider-connections/12/resolve",
            ))
            .respond_with(Seq(Mutex::new(0)))
            .mount(&server)
            .await;
        let (r, _) = resolver(&server).await;
        let (a, b, c) = tokio::join!(r.resolve(R), r.resolve(R), r.resolve(R));
        assert!(a.is_ok() && b.is_ok() && c.is_ok());
        assert_eq!(hits(&server, "/resolve").await, 3);
    }
}
