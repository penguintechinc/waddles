//! Thin REST client for hub-api's two internal provider-connection routes.
//!
//! - **List** `GET /api/v1/internal/provider-connections?provider=&capability=
//!   &after=&limit=` -> [`BindingPage`] (no tokens; cursor-paginated).
//! - **Resolve** `POST /api/v1/internal/communities/{community}/
//!   provider-connections/{connection}/resolve` -> [`ResolvedCredentials`] (the
//!   only secret-bearing response).
//!
//! **Authentication:** every call carries a short-lived EdDSA machine JWT
//! (`service_auth::MachineJwtClient`, scope `connections:resolve`, minted from
//! this pod's projected ServiceAccount token) as a bearer. hub-api additionally
//! requires the token's `tenant` claim to be the operator-plane `system`
//! tenant -- stamped by the issuer from its allow-list, never by this client.
//!
//! **Transport:** the REST routes are served by hub-api's plain-HTTP in-cluster
//! port (the same listener as `/internal/service-token`); `HUB_API_URL` is the
//! base. An `https://` base is supported with an explicit CA file
//! (`HUB_API_REST_CA_FILE`, never the system roots -- the svc_action
//! `HUB_API_GRPC_CA_FILE` approach). Redirects are never followed (a bearer must
//! not be replayed to a redirect target) and ambient proxy variables are
//! ignored.
//!
//! **Error taxonomy** ([`ClientError`]) is the contract the supervisor acts on:
//! `409`/`404` mean *drop this channel, do not retry*; `5xx`/`429`/`401`/`403`/
//! transport faults mean *retry with backoff*; any other `4xx` is a permanent
//! client-side fault. Error bodies are reduced to two short, charset-checked
//! fields (`error.code`, `error.details.reason`) -- the response body text is
//! never copied into an error, a log line, or a metric label.
//!
//! **Tokens are never logged**: the only place a token value is held is
//! [`AccessToken`], whose formatting traits are redacted.

use std::future::Future;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use reqwest::StatusCode;
use serde_json::Value;
use thiserror::Error;

use super::dto::{BindingPage, DataPlaneBinding, ResolvedCredentials};
use super::metrics::ConnectionsMetrics;

/// The machine-JWT scope hub-api's internal routes require
/// (`SCOPE_CONNECTIONS_RESOLVE`).
pub const SCOPE_CONNECTIONS_RESOLVE: &str = "connections:resolve";

/// Largest response body this client will buffer. A page of 1000 bindings is
/// well under 1 MiB; the cap only bounds a misbehaving server.
const MAX_BODY_BYTES: usize = 8 * 1024 * 1024;

/// Hard cap on list pages per full listing -- a server whose cursor never
/// terminates must not spin the supervisor forever.
const MAX_PAGES: usize = 10_000;

/// Longest `error.code` / `error.details.reason` value copied out of an error
/// body.
const MAX_ERROR_FIELD_LEN: usize = 64;

/// Why a hub-api call failed, classified for the supervisor's retry/drop
/// decision.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum ClientError {
    /// hub-api refused to hand out this connection (HTTP 409 `CONNECTION_NOT_
    /// USABLE`/`REFRESH_REJECTED`/..., or 404): **drop the channel, do not
    /// retry**.
    #[error("connection not usable (status {status}, code {code}, reason {reason})")]
    NotUsable {
        /// HTTP status (409 or 404).
        status: u16,
        /// hub-api `error.code`, charset-checked (`unknown` if absent).
        code: String,
        /// hub-api `error.details.reason`, charset-checked (`unknown` if absent).
        reason: String,
    },
    /// A fault worth retrying with backoff: 5xx (502/503/504), 429, 401/403
    /// (machine JWT rejected -- re-mint on the next attempt), a transport or
    /// timeout error, or an undecodable 2xx body.
    #[error("transient hub-api failure: {0}")]
    Transient(String),
    /// A client-side fault retrying cannot fix (any other 4xx, e.g. 400).
    #[error("hub-api rejected the request (status {status}, code {code})")]
    Permanent {
        /// HTTP status.
        status: u16,
        /// hub-api `error.code`, charset-checked.
        code: String,
    },
}

impl ClientError {
    /// The bounded metric/log label for this error's outcome class.
    #[must_use]
    pub fn outcome(&self) -> &'static str {
        match self {
            Self::NotUsable { .. } => "not_usable",
            Self::Transient(_) => "transient",
            Self::Permanent { .. } => "permanent",
        }
    }
}

/// Supplies the bearer token for hub-api calls. A trait so tests substitute a
/// static bearer for the real SA-token-minting client.
pub trait BearerSource: Send + Sync + 'static {
    /// A currently valid machine JWT.
    fn bearer(&self) -> impl Future<Output = Result<Arc<str>, ClientError>> + Send;
}

impl BearerSource for service_auth::MachineJwtClient {
    async fn bearer(&self) -> Result<Arc<str>, ClientError> {
        self.get_token().await.map_err(|err| {
            // `ServiceAuthError`'s display carries only a status/path -- never
            // a token (see `core/service_auth/src/lib.rs`).
            ClientError::Transient(format!("machine JWT mint failed: {err}"))
        })
    }
}

/// The two hub-api calls the supervisor depends on. A trait so the supervisor
/// and per-bot tasks are driven against a real HTTP mock in integration tests
/// and a scripted fake in unit tests, with identical production code.
pub trait ConnectionsApi: Send + Sync + 'static {
    /// One page of bindings after the `after` cursor.
    fn list_page(
        &self,
        provider: &str,
        capability: &str,
        after: i64,
        limit: u32,
    ) -> impl Future<Output = Result<BindingPage, ClientError>> + Send;

    /// A fresh access token for one binding (hub-api refreshes it if stale).
    fn resolve(
        &self,
        community_id: i64,
        connection_id: i64,
    ) -> impl Future<Output = Result<ResolvedCredentials, ClientError>> + Send;
}

/// Pages through every binding for `provider`/`capability` and returns the full
/// set -- or an error, never a partial list. A truncated listing diffed against
/// the running receivers would tear down every channel past the failure point,
/// so any page failure aborts the whole listing and the caller keeps its
/// current state.
pub async fn list_all<C: ConnectionsApi>(
    api: &C,
    provider: &str,
    capability: &str,
    page_size: u32,
) -> Result<Vec<DataPlaneBinding>, ClientError> {
    let mut all = Vec::new();
    let mut after: i64 = 0;
    for _ in 0..MAX_PAGES {
        let page = api
            .list_page(provider, capability, after, page_size)
            .await?;
        all.extend(page.items);
        match page.next_after {
            None => return Ok(all),
            Some(next) if next > after => after = next,
            Some(next) => {
                // A cursor that does not advance would loop forever.
                return Err(ClientError::Transient(format!(
                    "hub-api list cursor did not advance (after={after}, nextAfter={next})"
                )));
            }
        }
    }
    Err(ClientError::Transient(format!(
        "hub-api list exceeded {MAX_PAGES} pages"
    )))
}

/// Why [`HubConnectionsClient::new`] failed -- always a deployment
/// misconfiguration, reported at startup rather than on first use.
#[derive(Debug, Error)]
pub enum ClientBuildError {
    /// `HUB_API_URL` is not a usable `http(s)://host[:port]` base.
    #[error("invalid hub-api base URL: {0}")]
    InvalidUrl(String),
    /// The CA bundle could not be read or parsed.
    #[error("hub-api REST CA file {path}: {reason}")]
    Ca {
        /// Path as configured.
        path: String,
        /// Read/parse failure.
        reason: String,
    },
    /// A CA was configured for a plaintext `http://` base -- almost certainly a
    /// typo that would leave the operator believing the channel is TLS-protected.
    #[error("HUB_API_REST_CA_FILE is set but the hub-api base URL is plaintext http://")]
    CaWithPlaintextUrl,
    /// The underlying HTTP client could not be constructed.
    #[error("http client: {0}")]
    Http(String),
}

/// Construction parameters for [`HubConnectionsClient`].
#[derive(Debug, Clone)]
pub struct HubClientConfig {
    /// hub-api base URL (`HUB_API_URL`).
    pub base_url: String,
    /// PEM CA bundle for an `https://` base; `None` for plaintext in-cluster
    /// HTTP. When set it is the *only* trust anchor (system roots disabled).
    pub ca_file: Option<PathBuf>,
    /// Whole-request timeout.
    pub request_timeout: Duration,
    /// TCP connect timeout.
    pub connect_timeout: Duration,
}

/// The reqwest-backed [`ConnectionsApi`] implementation.
pub struct HubConnectionsClient<B: BearerSource> {
    http: reqwest::Client,
    base: String,
    bearer: B,
    metrics: Arc<ConnectionsMetrics>,
}

impl<B: BearerSource> HubConnectionsClient<B> {
    /// Builds the client, validating the base URL and CA up front.
    pub fn new(
        cfg: &HubClientConfig,
        bearer: B,
        metrics: Arc<ConnectionsMetrics>,
    ) -> Result<Self, ClientBuildError> {
        let base = validate_base_url(&cfg.base_url)?;
        let mut builder = reqwest::Client::builder()
            .timeout(cfg.request_timeout)
            .connect_timeout(cfg.connect_timeout)
            .redirect(reqwest::redirect::Policy::none())
            .no_proxy();
        if let Some(path) = &cfg.ca_file {
            if base.starts_with("http://") {
                return Err(ClientBuildError::CaWithPlaintextUrl);
            }
            let pem = std::fs::read(path).map_err(|err| ClientBuildError::Ca {
                path: path.display().to_string(),
                reason: err.to_string(),
            })?;
            // reqwest's rustls backend defers PEM validation to `build()`, whose
            // error does not name the file -- reject an obviously non-PEM
            // bundle here so the operator sees the path.
            if !String::from_utf8_lossy(&pem).contains("-----BEGIN CERTIFICATE-----") {
                return Err(ClientBuildError::Ca {
                    path: path.display().to_string(),
                    reason: "no PEM CERTIFICATE block found".to_string(),
                });
            }
            let cert =
                reqwest::Certificate::from_pem(&pem).map_err(|err| ClientBuildError::Ca {
                    path: path.display().to_string(),
                    reason: err.to_string(),
                })?;
            // Explicit CA only -- the chart's internal CA is never part of the
            // system roots, and trusting both would widen the attack surface.
            builder = builder
                .tls_built_in_root_certs(false)
                .add_root_certificate(cert);
        }
        let http = builder
            .build()
            .map_err(|err| ClientBuildError::Http(err.without_url().to_string()))?;
        Ok(Self {
            http,
            base,
            bearer,
            metrics,
        })
    }

    /// True when the base URL is plaintext `http://` (logged once at startup:
    /// confidentiality then rests on the cluster network).
    #[must_use]
    pub fn is_plaintext(&self) -> bool {
        self.base.starts_with("http://")
    }

    async fn send(
        &self,
        op: &'static str,
        request: reqwest::RequestBuilder,
    ) -> Result<Vec<u8>, ClientError> {
        let started = Instant::now();
        let result = self.send_inner(request).await;
        let outcome = match &result {
            Ok(_) => "ok",
            Err(err) => err.outcome(),
        };
        self.metrics
            .observe_request(op, outcome, started.elapsed().as_secs_f64());
        result
    }

    async fn send_inner(&self, request: reqwest::RequestBuilder) -> Result<Vec<u8>, ClientError> {
        let bearer = self.bearer.bearer().await?;
        let mut response = request
            .bearer_auth(&*bearer)
            .header(reqwest::header::ACCEPT, "application/json")
            .send()
            .await
            .map_err(|err| {
                ClientError::Transient(format!("request failed: {}", err.without_url()))
            })?;
        let status = response.status();

        // Read at most MAX_BODY_BYTES; a body past the cap is a protocol fault.
        let mut body: Vec<u8> = Vec::new();
        while let Some(chunk) = response.chunk().await.map_err(|err| {
            ClientError::Transient(format!("body read failed: {}", err.without_url()))
        })? {
            if body.len() + chunk.len() > MAX_BODY_BYTES {
                return Err(ClientError::Transient(format!(
                    "response body exceeds {MAX_BODY_BYTES} bytes"
                )));
            }
            body.extend_from_slice(&chunk);
        }

        if status.is_success() {
            return Ok(body);
        }
        Err(classify_error(status, &body))
    }
}

/// Maps a non-2xx response to a [`ClientError`]. Only the two charset-checked
/// error fields are extracted; the body text itself is discarded.
fn classify_error(status: StatusCode, body: &[u8]) -> ClientError {
    let (code, reason) = parse_error_fields(body);
    match status.as_u16() {
        409 | 404 => ClientError::NotUsable {
            status: status.as_u16(),
            code,
            reason: if status == StatusCode::NOT_FOUND && reason == "unknown" {
                "not_found".to_string()
            } else {
                reason
            },
        },
        401 | 403 | 408 | 425 | 429 => {
            ClientError::Transient(format!("hub-api answered {status} (code {code})"))
        }
        s if (500..=599).contains(&s) => {
            ClientError::Transient(format!("hub-api answered {status} (code {code})"))
        }
        s => ClientError::Permanent { status: s, code },
    }
}

/// Extracts `error.code` and `error.details.reason` from an error body, each
/// accepted only if short and `[A-Za-z0-9_.-]`; otherwise `unknown`. A body
/// that is not the expected JSON envelope yields `("unknown", "unknown")`.
fn parse_error_fields(body: &[u8]) -> (String, String) {
    let value: Value = serde_json::from_slice(body).unwrap_or(Value::Null);
    let error = value.get("error");
    let pick = |v: Option<&Value>| -> String {
        v.and_then(Value::as_str)
            .filter(|s| {
                !s.is_empty()
                    && s.len() <= MAX_ERROR_FIELD_LEN
                    && s.bytes()
                        .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'-'))
            })
            .map_or_else(|| "unknown".to_string(), str::to_string)
    };
    let code = pick(error.and_then(|e| e.get("code")));
    let reason = pick(
        error
            .and_then(|e| e.get("details"))
            .and_then(|d| d.get("reason")),
    );
    (code, reason)
}

/// Validates `raw` as `http(s)://host[:port]` with no credentials, query or
/// fragment, and returns it without a trailing slash.
fn validate_base_url(raw: &str) -> Result<String, ClientBuildError> {
    let url = reqwest::Url::parse(raw.trim())
        .map_err(|err| ClientBuildError::InvalidUrl(err.to_string()))?;
    if !matches!(url.scheme(), "http" | "https") {
        return Err(ClientBuildError::InvalidUrl(format!(
            "scheme must be http or https, got {:?}",
            url.scheme()
        )));
    }
    if url.host_str().is_none() {
        return Err(ClientBuildError::InvalidUrl("missing host".to_string()));
    }
    if !url.username().is_empty() || url.password().is_some() {
        return Err(ClientBuildError::InvalidUrl(
            "credentials in the URL are not allowed".to_string(),
        ));
    }
    if url.query().is_some() || url.fragment().is_some() {
        return Err(ClientBuildError::InvalidUrl(
            "query/fragment not allowed in the base URL".to_string(),
        ));
    }
    Ok(url.as_str().trim_end_matches('/').to_string())
}

impl<B: BearerSource> ConnectionsApi for HubConnectionsClient<B> {
    #[tracing::instrument(name = "hubconn.list", skip_all, fields(provider = %provider, capability = %capability, after = after, limit = limit))]
    async fn list_page(
        &self,
        provider: &str,
        capability: &str,
        after: i64,
        limit: u32,
    ) -> Result<BindingPage, ClientError> {
        let request = self
            .http
            .get(format!(
                "{}/api/v1/internal/provider-connections",
                self.base
            ))
            .query(&[
                ("provider", provider),
                ("capability", capability),
                ("after", &after.to_string()),
                ("limit", &limit.to_string()),
            ]);
        let body = self.send("list", request).await?;
        let page: BindingPage = serde_json::from_slice(&body)
            .map_err(|err| ClientError::Transient(format!("undecodable list response: {err}")))?;
        tracing::debug!(
            returned = page.items.len(),
            next_after = ?page.next_after,
            "hubconn list page"
        );
        Ok(page)
    }

    #[tracing::instrument(name = "hubconn.resolve", skip_all, fields(community_id = community_id, connection_id = connection_id))]
    async fn resolve(
        &self,
        community_id: i64,
        connection_id: i64,
    ) -> Result<ResolvedCredentials, ClientError> {
        let request = self.http.post(format!(
            "{}/api/v1/internal/communities/{community_id}/provider-connections/{connection_id}/resolve",
            self.base
        ));
        let body = self.send("resolve", request).await?;
        // The decode error is `serde_json`'s -- it names a field/position, never
        // echoes the value, so a malformed body cannot leak the token.
        let resolved: ResolvedCredentials = serde_json::from_slice(&body).map_err(|err| {
            ClientError::Transient(format!("undecodable resolve response: {err}"))
        })?;
        if resolved.access_token.is_empty() {
            return Err(ClientError::Transient(
                "hub-api resolve returned an empty access token".to_string(),
            ));
        }
        tracing::debug!(
            token = %resolved.access_token,
            has_expiry = resolved.expires_at.is_some(),
            "hubconn resolve ok (token redacted)"
        );
        Ok(resolved)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::sync::Mutex;

    struct StaticBearer(&'static str);
    impl BearerSource for StaticBearer {
        async fn bearer(&self) -> Result<Arc<str>, ClientError> {
            Ok(Arc::from(self.0))
        }
    }

    fn metrics() -> Arc<ConnectionsMetrics> {
        Arc::new(ConnectionsMetrics::register(&prometheus::Registry::new()).unwrap())
    }

    #[test]
    fn classify_409_and_404_are_not_usable_with_bounded_fields() {
        let body = json!({"success": false, "error": {
            "message": "x", "code": "CONNECTION_NOT_USABLE",
            "details": {"reason": "revoked_grant"}}})
        .to_string();
        assert_eq!(
            classify_error(StatusCode::CONFLICT, body.as_bytes()),
            ClientError::NotUsable {
                status: 409,
                code: "CONNECTION_NOT_USABLE".into(),
                reason: "revoked_grant".into()
            }
        );
        let nf = classify_error(StatusCode::NOT_FOUND, b"{}");
        assert_eq!(
            nf,
            ClientError::NotUsable {
                status: 404,
                code: "unknown".into(),
                reason: "not_found".into()
            }
        );
    }

    #[test]
    fn classify_retryable_statuses_are_transient() {
        for status in [
            StatusCode::BAD_GATEWAY,
            StatusCode::SERVICE_UNAVAILABLE,
            StatusCode::GATEWAY_TIMEOUT,
            StatusCode::INTERNAL_SERVER_ERROR,
            StatusCode::TOO_MANY_REQUESTS,
            StatusCode::UNAUTHORIZED,
            StatusCode::FORBIDDEN,
        ] {
            assert!(
                matches!(classify_error(status, b""), ClientError::Transient(_)),
                "{status} must be retryable"
            );
        }
    }

    #[test]
    fn classify_other_4xx_is_permanent() {
        assert!(matches!(
            classify_error(
                StatusCode::BAD_REQUEST,
                br#"{"error":{"code":"BAD_REQUEST"}}"#
            ),
            ClientError::Permanent { status: 400, .. }
        ));
    }

    #[test]
    fn error_fields_reject_oversize_and_hostile_values_and_never_echo_the_body() {
        let hostile =
            json!({"error": {"code": "has space", "details": {"reason": "x".repeat(200)}}})
                .to_string();
        let (code, reason) = parse_error_fields(hostile.as_bytes());
        assert_eq!((code.as_str(), reason.as_str()), ("unknown", "unknown"));
        let (c, r) = parse_error_fields(b"<html>not json oauth:SECRET</html>");
        assert_eq!((c.as_str(), r.as_str()), ("unknown", "unknown"));
        // `error` as a bare string (the auth failures' shape) is tolerated.
        let (c, r) = parse_error_fields(br#"{"error": "unauthorized"}"#);
        assert_eq!((c.as_str(), r.as_str()), ("unknown", "unknown"));
    }

    #[test]
    fn base_url_validation() {
        assert_eq!(
            validate_base_url("http://hub-api:8204/").unwrap(),
            "http://hub-api:8204"
        );
        assert!(validate_base_url("https://hub-api:8204").is_ok());
        for bad in [
            "ftp://x",
            "not a url",
            "http://user:pw@hub-api",
            "http://hub-api/?q=1",
            "http://hub-api/#f",
            "",
        ] {
            assert!(validate_base_url(bad).is_err(), "{bad:?} must be rejected");
        }
    }

    #[test]
    fn ca_with_plaintext_url_fails_loud() {
        let cfg = HubClientConfig {
            base_url: "http://hub-api:8204".into(),
            ca_file: Some(PathBuf::from("/nonexistent/ca.pem")),
            request_timeout: Duration::from_secs(1),
            connect_timeout: Duration::from_secs(1),
        };
        let err = HubConnectionsClient::new(&cfg, StaticBearer("t"), metrics())
            .err()
            .expect("must fail");
        assert!(matches!(err, ClientBuildError::CaWithPlaintextUrl));
    }

    #[test]
    fn missing_or_bad_ca_file_fails_loud() {
        let mut cfg = HubClientConfig {
            base_url: "https://hub-api:8204".into(),
            ca_file: Some(PathBuf::from("/nonexistent/ca.pem")),
            request_timeout: Duration::from_secs(1),
            connect_timeout: Duration::from_secs(1),
        };
        assert!(matches!(
            HubConnectionsClient::new(&cfg, StaticBearer("t"), metrics()).err(),
            Some(ClientBuildError::Ca { .. })
        ));
        let dir = tempdir_path("badca");
        std::fs::create_dir_all(&dir).unwrap();
        let bad = dir.join("bad.pem");
        std::fs::write(&bad, b"this is not a certificate").unwrap();
        cfg.ca_file = Some(bad);
        assert!(matches!(
            HubConnectionsClient::new(&cfg, StaticBearer("t"), metrics()).err(),
            Some(ClientBuildError::Ca { .. })
        ));
        let _ = std::fs::remove_dir_all(dir);
    }

    fn tempdir_path(tag: &str) -> PathBuf {
        std::env::temp_dir().join(format!("svc-ingest-{tag}-{}", std::process::id()))
    }

    /// Scripted [`ConnectionsApi`] for `list_all`'s pagination tests.
    struct PagedApi {
        pages: Mutex<Vec<Result<BindingPage, ClientError>>>,
        seen_after: Mutex<Vec<i64>>,
    }

    impl ConnectionsApi for PagedApi {
        async fn list_page(
            &self,
            _provider: &str,
            _capability: &str,
            after: i64,
            _limit: u32,
        ) -> Result<BindingPage, ClientError> {
            self.seen_after.lock().unwrap().push(after);
            self.pages.lock().unwrap().remove(0)
        }
        async fn resolve(&self, _c: i64, _k: i64) -> Result<ResolvedCredentials, ClientError> {
            unreachable!("list_all never resolves")
        }
    }

    fn binding(grant_id: i64) -> DataPlaneBinding {
        serde_json::from_value(json!({
            "grantId": grant_id, "connectionId": grant_id, "communityId": 1,
            "tenantId": 1, "provider": "twitch"
        }))
        .unwrap()
    }

    fn page(ids: &[i64], next: Option<i64>) -> Result<BindingPage, ClientError> {
        Ok(BindingPage {
            success: true,
            items: ids.iter().copied().map(binding).collect(),
            next_after: next,
        })
    }

    #[tokio::test]
    async fn list_all_follows_the_cursor_across_pages() {
        let api = PagedApi {
            pages: Mutex::new(vec![
                page(&[1, 2], Some(2)),
                page(&[3, 4], Some(4)),
                page(&[5], None),
            ]),
            seen_after: Mutex::new(vec![]),
        };
        let all = list_all(&api, "twitch", "chat_ingest", 2).await.unwrap();
        assert_eq!(
            all.iter().map(|b| b.grant_id).collect::<Vec<_>>(),
            vec![1, 2, 3, 4, 5]
        );
        assert_eq!(*api.seen_after.lock().unwrap(), vec![0, 2, 4]);
    }

    #[tokio::test]
    async fn list_all_discards_a_partial_listing_when_a_later_page_fails() {
        let api = PagedApi {
            pages: Mutex::new(vec![
                page(&[1, 2], Some(2)),
                Err(ClientError::Transient("boom".into())),
            ]),
            seen_after: Mutex::new(vec![]),
        };
        let err = list_all(&api, "twitch", "chat_ingest", 2)
            .await
            .unwrap_err();
        assert!(matches!(err, ClientError::Transient(_)));
    }

    #[tokio::test]
    async fn list_all_rejects_a_cursor_that_does_not_advance() {
        let api = PagedApi {
            pages: Mutex::new(vec![page(&[1], Some(5)), page(&[2], Some(5))]),
            seen_after: Mutex::new(vec![]),
        };
        let err = list_all(&api, "twitch", "chat_ingest", 1)
            .await
            .unwrap_err();
        assert!(matches!(&err, ClientError::Transient(m) if m.contains("did not advance")));
    }

    #[test]
    fn outcome_labels_are_bounded() {
        assert_eq!(
            ClientError::NotUsable {
                status: 409,
                code: "c".into(),
                reason: "r".into()
            }
            .outcome(),
            "not_usable"
        );
        assert_eq!(ClientError::Transient("x".into()).outcome(), "transient");
        assert_eq!(
            ClientError::Permanent {
                status: 400,
                code: "c".into()
            }
            .outcome(),
            "permanent"
        );
    }
}
