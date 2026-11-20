//! Real HTTP client against hub-api's already-shipped token ledger.
//!
//! TRANSCODE admission (design spec: "checks entitlement before starting a
//! job") debits `transcode_token_cost` tokens of `transcode_product_key`
//! (both `CliConfig` fields, see `crate::config`) via hub-api's real
//! `POST /api/v1/marketplace/communities/<id>/tokens/debit` endpoint --
//! this service holds no ledger DB grant of its own (single-writer, per
//! hub-api's own `token_billing_service` docstring), so an HTTP call is
//! the only way tokens are ever decremented. Ported from the Python
//! alpha's `services/token_ledger_client.py`; same wire contract and the
//! same BLOCK-WITH-FALLBACK semantics (see [`TokenDebitResult`]).
//!
//! Auth model (service identity, never user impersonation): the debit goes to
//! hub-api's INTERNAL route (`POST /api/v1/internal/communities/{id}/tokens/debit`)
//! authenticated with THIS service's own community-bound machine JWT (scope
//! `token.billing:debit`, minted via hub-api's `ServiceAuthService` with the pod's
//! projected ServiceAccount token -- [`service_auth::BoundMachineJwtClient`], the same
//! mechanism svc-process/svc-action use). The member who started the stream is named
//! as `actor_user_id` in the body; hub-api verifies that user is an ACTIVE member of
//! the community before debiting. A member's own JWT is never forwarded: hub-api's
//! user-facing debit route is community-admin only (`token.billing:manage`), so a
//! forwarded member token would be refused. See `crate::orchestrator`'s
//! ingest-triggered publish path, which has no actor at all and therefore does NOT
//! call this client (flagged in that module's own doc comment).
//!
//! Failure mapping is explicit, never silent: 401/403 from hub-api
//! ([`REASON_LEDGER_FORBIDDEN`], a misconfigured identity allow-list or a non-member
//! actor) and an unmintable/unconfigured service credential
//! ([`REASON_CREDENTIAL_UNAVAILABLE`]) are logged at ERROR with their own reason code
//! before the documented passthrough degrade below.
//!
//! BLOCK-WITH-FALLBACK: [`TokenLedgerClient::debit_transcoding_tokens`]
//! never returns an error type for a business-as-usual "can't afford it"
//! outcome -- it always resolves to a [`TokenDebitResult`] the caller
//! (`crate::api::lifecycle::start`) branches on to fall back to
//! passthrough (`VideoCodec::Copy`, no transcode) rather than blocking
//! stream start entirely. This mirrors the licensing model's node-overage
//! "burst freely, never block" posture (`rules/critical-rules.md`
//! Licensing Model) even though token-ledger admission is a metering
//! concept, not a node/seat license count -- the user-facing effect is the
//! same: a billing/entitlement check never takes down a live stream.

use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use serde::{Deserialize, Serialize};

use crate::telemetry::stream::{ExternalPeer, StreamMetrics};
use crate::telemetry::trace_context::inject_current_context;

/// Timeout for one debit attempt -- matches the Python alpha's
/// `_TIMEOUT_SECONDS = 5.0`.
const TIMEOUT: Duration = Duration::from_secs(5);

/// Stable, caller-facing vocabulary for why a debit didn't happen -- mirrors
/// hub-api's own `REASON_*` constants (`token_billing_service.py`) so a 402
/// from hub-api and a local network failure collapse into the same shape
/// for the caller's fallback branch.
pub const REASON_INSUFFICIENT_BALANCE: &str = "insufficient_balance";
pub const REASON_LEDGER_UNAVAILABLE: &str = "ledger_unavailable";
/// hub-api answered 401/403: this service's identity (or the acting member) was refused.
/// Distinct from `ledger_unavailable` so a misconfigured allow-list is never mistaken for an
/// outage; always logged at ERROR.
pub const REASON_LEDGER_FORBIDDEN: &str = "ledger_forbidden";
/// No service credential could be obtained (mint endpoint unset/unreachable/rejected).
pub const REASON_CREDENTIAL_UNAVAILABLE: &str = "credential_unavailable";

/// Scope of the community-bound machine JWT this service debits with.
pub const SCOPE_TOKEN_DEBIT: &str = "token.billing:debit";

/// Source of the service's own bearer for one community-bound debit. Production uses
/// [`service_auth::BoundMachineJwtClient`]; tests substitute a fixed token.
#[async_trait]
pub trait ServiceBearer: Send + Sync {
    /// A machine JWT bound to exactly `community_id`, or a loggable (secret-free) error.
    async fn bearer_for(&self, community_id: i64) -> Result<Arc<str>, String>;
}

#[async_trait]
impl ServiceBearer for service_auth::BoundMachineJwtClient {
    async fn bearer_for(&self, community_id: i64) -> Result<Arc<str>, String> {
        self.get_token_for(community_id, None)
            .await
            .map_err(|err| err.to_string())
    }
}

/// Outcome of one transcode-admission debit attempt against hub-api's
/// ledger. Never constructed from a raised error -- every call path
/// (success, insufficient balance, unreachable, unexpected status) maps
/// into this one shape.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TokenDebitResult {
    pub ok: bool,
    pub balance_after: Option<i64>,
    pub blocked_reason: Option<String>,
}

impl TokenDebitResult {
    fn ok(balance_after: i64) -> Self {
        Self {
            ok: true,
            balance_after: Some(balance_after),
            blocked_reason: None,
        }
    }

    fn insufficient_balance() -> Self {
        Self {
            ok: false,
            balance_after: None,
            blocked_reason: Some(REASON_INSUFFICIENT_BALANCE.to_string()),
        }
    }

    fn blocked(reason: &str) -> Self {
        Self {
            ok: false,
            balance_after: None,
            blocked_reason: Some(reason.to_string()),
        }
    }

    fn ledger_unavailable() -> Self {
        Self {
            ok: false,
            balance_after: None,
            blocked_reason: Some(REASON_LEDGER_UNAVAILABLE.to_string()),
        }
    }
}

/// Wire body of hub-api's internal debit route. `actor_user_id` is the member the admission is
/// for (hub-api verifies active membership); `ref` is the audit reference.
#[derive(Debug, Serialize)]
struct DebitRequestBody<'a> {
    product_key: &'a str,
    amount: i64,
    reason: &'static str,
    actor_user_id: i64,
    #[serde(rename = "ref")]
    reference: &'a str,
}

/// hub-api's internal (machine-JWT) debit path for one community.
fn debit_url(hub_api_url: &str, community_id: i32) -> String {
    format!("{hub_api_url}/api/v1/internal/communities/{community_id}/tokens/debit")
}

#[derive(Debug, Deserialize)]
struct DebitSuccessBody {
    balance_after: i64,
}

/// Thin HTTP client for hub-api's token-debit endpoint. Holds its own
/// `reqwest::Client` (5s timeout) rather than sharing the loopback client
/// `crate::rtc::ingest_auth::InternalIngestAuthClient` uses -- this one
/// targets an external service address (`HUB_API_URL`), not this
/// process's own port.
#[derive(Clone)]
pub struct TokenLedgerClient {
    http: reqwest::Client,
    metrics: StreamMetrics,
    /// The service's own credential source; `None` = not configured (every debit then fails loud
    /// with [`REASON_CREDENTIAL_UNAVAILABLE`] instead of guessing a weaker credential).
    bearer: Option<Arc<dyn ServiceBearer>>,
}

impl Default for TokenLedgerClient {
    fn default() -> Self {
        Self::new()
    }
}

impl TokenLedgerClient {
    /// Builds the ONE pooled client (5s admission timeout) honouring the chart's
    /// internal-TLS policy (`INTERNAL_CA_FILE` / `INTERNAL_TLS_REQUIRED`, see
    /// [`crate::tls::internal_client`]). `AppState` holds this in an `Arc`, so every
    /// debit reuses the same keep-alive/HTTP2 connection instead of re-handshaking.
    ///
    /// # Panics
    /// On an invalid internal-TLS policy (unreadable CA, `REQUIRED` without a CA): a
    /// misconfigured pod must crash at boot rather than debit over plaintext.
    pub fn new() -> Self {
        Self {
            http: crate::tls::internal_client(TIMEOUT)
                .expect("internal TLS policy invalid for the hub-api ledger client"),
            metrics: StreamMetrics::shared(),
            bearer: None,
        }
    }

    /// Installs the service's own credential source (see [`ServiceBearer`]).
    pub fn with_bearer_source(mut self, bearer: Arc<dyn ServiceBearer>) -> Self {
        self.bearer = Some(bearer);
        self
    }

    /// Replaces the process-wide stream instruments with an explicit handle
    /// -- for tests that install their own meter provider.
    pub fn with_stream_metrics(mut self, metrics: StreamMetrics) -> Self {
        self.metrics = metrics;
        self
    }

    /// Builds a client around an already-constructed `reqwest::Client` --
    /// used by tests that need a client pointed at a local mock server
    /// with the same (or a shorter) timeout.
    #[cfg(test)]
    fn with_client(http: reqwest::Client) -> Self {
        Self {
            http,
            metrics: StreamMetrics::shared(),
            bearer: None,
        }
    }

    /// POSTs a real debit to hub-api's token ledger; never returns an
    /// `Err` for a blocked/unreachable outcome -- callers branch on
    /// [`TokenDebitResult::ok`], matching the Python
    /// `debit_transcoding_tokens` contract ("never raises for a
    /// blocked/unreachable outcome").
    ///
    /// `actor_user_id` is the member the admission is for (the stream starter's JWT `sub`);
    /// the request itself is authenticated with this service's own machine JWT.
    ///
    /// `reference` should uniquely identify the admission attempt (e.g.
    /// `format!("stream:{config_id}:{started_at}")`) -- hub-api's ledger
    /// is not idempotency-keyed on this field (it's the
    /// `credit_tokens`/`debit_tokens` audit `ref`, not an idempotency
    /// key), so a genuine retry after a network timeout can double-debit;
    /// callers must treat a retried `start()` as a new admission attempt
    /// rather than relying on a dedup guarantee this client doesn't
    /// provide.
    pub async fn debit_transcoding_tokens(
        &self,
        hub_api_url: &str,
        actor_user_id: i64,
        community_id: i32,
        amount: i64,
        product_key: &str,
        reference: &str,
    ) -> TokenDebitResult {
        let url = debit_url(hub_api_url, community_id);
        let body = DebitRequestBody {
            product_key,
            amount,
            reason: "svc_streaming_transcode_admission",
            actor_user_id,
            reference,
        };

        let Some(source) = self.bearer.as_ref() else {
            tracing::error!(
                community_id,
                reason = REASON_CREDENTIAL_UNAVAILABLE,
                "token_ledger: no service credential configured (SERVICE_JWT_TOKEN_ENDPOINT unset); transcode admission cannot be debited"
            );
            return TokenDebitResult::blocked(REASON_CREDENTIAL_UNAVAILABLE);
        };
        let bearer_token = match source.bearer_for(i64::from(community_id)).await {
            Ok(token) => token,
            Err(err) => {
                tracing::error!(
                    community_id,
                    error = %err,
                    reason = REASON_CREDENTIAL_UNAVAILABLE,
                    "token_ledger: could not obtain the service machine JWT"
                );
                return TokenDebitResult::blocked(REASON_CREDENTIAL_UNAVAILABLE);
            }
        };

        // W3C trace context rides on the call so hub-api's ledger work
        // shows up as a child of this admission check.
        let mut trace_headers = reqwest::header::HeaderMap::new();
        inject_current_context(&mut trace_headers);

        let response = match self
            .metrics
            .time_external(
                ExternalPeer::TokenLedger,
                self.http
                    .post(&url)
                    .headers(trace_headers)
                    .bearer_auth(&*bearer_token)
                    .json(&body)
                    .send(),
            )
            .await
        {
            Ok(resp) => resp,
            Err(err) => {
                tracing::error!(
                    community_id,
                    error = %err,
                    "token_ledger: hub-api token ledger unreachable"
                );
                return TokenDebitResult::ledger_unavailable();
            }
        };

        match response.status() {
            reqwest::StatusCode::OK => match response.json::<DebitSuccessBody>().await {
                Ok(parsed) => TokenDebitResult::ok(parsed.balance_after),
                Err(err) => {
                    tracing::error!(
                        community_id,
                        error = %err,
                        "token_ledger: malformed 200 response body from hub-api"
                    );
                    TokenDebitResult::ledger_unavailable()
                }
            },
            reqwest::StatusCode::PAYMENT_REQUIRED => TokenDebitResult::insufficient_balance(),
            status @ (reqwest::StatusCode::UNAUTHORIZED | reqwest::StatusCode::FORBIDDEN) => {
                tracing::error!(
                    community_id,
                    %status,
                    reason = REASON_LEDGER_FORBIDDEN,
                    "token_ledger: hub-api refused the service identity or the acting member (check serviceJwt.identities.svc-streaming and actor membership)"
                );
                TokenDebitResult::blocked(REASON_LEDGER_FORBIDDEN)
            }
            status => {
                tracing::error!(
                    community_id,
                    %status,
                    "token_ledger: unexpected status from hub-api"
                );
                TokenDebitResult::ledger_unavailable()
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::{HeaderMap, StatusCode};
    use axum::routing::post;
    use axum::{Json, Router};
    use std::sync::Mutex;
    use std::time::Duration as StdDuration;

    /// What the mock hub-api saw: request path, `Authorization` header, JSON body.
    #[derive(Debug, Clone)]
    struct Captured {
        path: String,
        authorization: Option<String>,
        body: serde_json::Value,
    }

    async fn spawn_mock(
        status: StatusCode,
        body: serde_json::Value,
    ) -> (String, Arc<Mutex<Vec<Captured>>>) {
        let seen: Arc<Mutex<Vec<Captured>>> = Arc::new(Mutex::new(Vec::new()));
        let sink = seen.clone();
        let app = Router::new().route(
            "/api/v1/internal/communities/{community_id}/tokens/debit",
            post(
                move |axum::extract::Path(community_id): axum::extract::Path<i32>,
                      headers: HeaderMap,
                      axum::Json(received): axum::Json<serde_json::Value>| {
                    let sink = sink.clone();
                    let body = body.clone();
                    async move {
                        sink.lock().unwrap().push(Captured {
                            path: format!(
                                "/api/v1/internal/communities/{community_id}/tokens/debit"
                            ),
                            authorization: headers
                                .get("authorization")
                                .and_then(|v| v.to_str().ok())
                                .map(str::to_string),
                            body: received,
                        });
                        (status, Json(body))
                    }
                },
            ),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        (format!("http://{addr}"), seen)
    }

    /// Fixed-token credential source standing in for the machine-JWT mint.
    struct FixedBearer(Result<&'static str, &'static str>);

    #[async_trait]
    impl ServiceBearer for FixedBearer {
        async fn bearer_for(&self, community_id: i64) -> Result<Arc<str>, String> {
            match self.0 {
                Ok(token) => Ok(Arc::from(format!("{token}-c{community_id}"))),
                Err(err) => Err(err.to_string()),
            }
        }
    }

    fn http() -> reqwest::Client {
        reqwest::Client::builder()
            .timeout(StdDuration::from_secs(3))
            .build()
            .expect("client builds")
    }

    fn test_client() -> TokenLedgerClient {
        TokenLedgerClient::with_client(http())
            .with_bearer_source(Arc::new(FixedBearer(Ok("svc-jwt"))))
    }

    async fn debit(client: &TokenLedgerClient, base_url: &str) -> TokenDebitResult {
        client
            .debit_transcoding_tokens(base_url, 42, 7, 5, "transcoding_minutes", "ref-1")
            .await
    }

    #[test]
    fn debit_url_targets_the_internal_machine_route() {
        assert_eq!(
            debit_url("https://hub-api:8204", 7),
            "https://hub-api:8204/api/v1/internal/communities/7/tokens/debit"
        );
    }

    #[tokio::test]
    async fn request_shape_uses_service_bearer_and_names_the_member_actor() {
        let (base_url, seen) = spawn_mock(
            StatusCode::OK,
            serde_json::json!({"success": true, "balance_after": 55, "transaction_id": 1}),
        )
        .await;

        let result = debit(&test_client(), &base_url).await;

        assert!(result.ok);
        assert_eq!(result.balance_after, Some(55));
        let captured = seen.lock().unwrap().pop().expect("one request");
        assert_eq!(captured.path, "/api/v1/internal/communities/7/tokens/debit");
        // The SERVICE's community-bound credential, never a forwarded member token.
        assert_eq!(captured.authorization.as_deref(), Some("Bearer svc-jwt-c7"));
        assert_eq!(captured.body["actor_user_id"], 42);
        assert_eq!(captured.body["product_key"], "transcoding_minutes");
        assert_eq!(captured.body["amount"], 5);
        assert_eq!(captured.body["reason"], "svc_streaming_transcode_admission");
        assert_eq!(captured.body["ref"], "ref-1");
    }

    #[tokio::test]
    async fn debit_insufficient_balance_maps_402_to_blocked_reason() {
        let (base_url, _seen) = spawn_mock(
            StatusCode::PAYMENT_REQUIRED,
            serde_json::json!({"success": false, "blocked_reason": "insufficient_balance"}),
        )
        .await;

        let result = debit(&test_client(), &base_url).await;

        assert!(!result.ok);
        assert_eq!(
            result.blocked_reason.as_deref(),
            Some(REASON_INSUFFICIENT_BALANCE)
        );
        assert_eq!(result.balance_after, None);
    }

    #[tokio::test]
    async fn forbidden_and_unauthorized_map_to_explicit_ledger_forbidden() {
        for status in [StatusCode::FORBIDDEN, StatusCode::UNAUTHORIZED] {
            let (base_url, _seen) = spawn_mock(status, serde_json::json!({})).await;
            let result = debit(&test_client(), &base_url).await;
            assert!(!result.ok, "{status} must not admit");
            assert_eq!(
                result.blocked_reason.as_deref(),
                Some(REASON_LEDGER_FORBIDDEN),
                "{status} must be its own reason, not ledger_unavailable"
            );
        }
    }

    #[tokio::test]
    async fn debit_unexpected_status_degrades_to_ledger_unavailable() {
        let (base_url, _seen) =
            spawn_mock(StatusCode::INTERNAL_SERVER_ERROR, serde_json::json!({})).await;

        let result = debit(&test_client(), &base_url).await;

        assert!(!result.ok);
        assert_eq!(
            result.blocked_reason.as_deref(),
            Some(REASON_LEDGER_UNAVAILABLE)
        );
    }

    #[tokio::test]
    async fn missing_credential_source_fails_loud_without_calling_hub_api() {
        let (base_url, seen) = spawn_mock(StatusCode::OK, serde_json::json!({})).await;
        let client = TokenLedgerClient::with_client(http()); // no bearer source

        let result = debit(&client, &base_url).await;

        assert!(!result.ok);
        assert_eq!(
            result.blocked_reason.as_deref(),
            Some(REASON_CREDENTIAL_UNAVAILABLE)
        );
        assert!(
            seen.lock().unwrap().is_empty(),
            "no request without a credential"
        );
    }

    #[tokio::test]
    async fn credential_mint_failure_maps_to_credential_unavailable() {
        let (base_url, seen) = spawn_mock(StatusCode::OK, serde_json::json!({})).await;
        let client = TokenLedgerClient::with_client(http())
            .with_bearer_source(Arc::new(FixedBearer(Err("bootstrap rejected"))));

        let result = debit(&client, &base_url).await;

        assert_eq!(
            result.blocked_reason.as_deref(),
            Some(REASON_CREDENTIAL_UNAVAILABLE)
        );
        assert!(seen.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn debit_network_failure_is_ledger_unavailable_not_a_panic() {
        // Port 1 -- reserved/unassigned, connection refused immediately
        // rather than timing out the whole test.
        let result = debit(&test_client(), "http://127.0.0.1:1").await;

        assert!(!result.ok);
        assert_eq!(
            result.blocked_reason.as_deref(),
            Some(REASON_LEDGER_UNAVAILABLE)
        );
    }

    #[tokio::test]
    async fn debit_malformed_success_body_degrades_to_ledger_unavailable() {
        let (base_url, _seen) = spawn_mock(
            StatusCode::OK,
            serde_json::json!({"success": true}), // missing balance_after
        )
        .await;

        let result = debit(&test_client(), &base_url).await;

        assert!(!result.ok);
        assert_eq!(
            result.blocked_reason.as_deref(),
            Some(REASON_LEDGER_UNAVAILABLE)
        );
    }
}
