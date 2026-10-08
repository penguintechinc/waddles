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
//! Auth model: this call PASSES THROUGH the caller's own bearer JWT (the
//! same token that authenticated the `POST .../start` request to
//! svc-streaming) rather than minting a separate service-to-service
//! credential -- hub-api's `tokens/debit` route authorizes via "an active
//! community member spending their own community's tokens on a premium
//! feature", exactly the shape of a member starting their own community's
//! TRANSCODE-enabled forward. A dedicated internal service-identity
//! credential (SPIFFE mTLS / machine JWT, `rules/security.md`
//! Service-to-Service Auth) for a background/unattended metering path
//! (e.g. per-minute re-metering of an already-running job) is real
//! follow-up work once such a path exists; it is not needed for this
//! synchronous, caller-initiated admission check -- see
//! `crate::orchestrator`'s ingest-triggered publish path, which has no
//! caller JWT at all and therefore does NOT call this client (flagged in
//! that module's own doc comment).
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

use std::time::Duration;

use serde::{Deserialize, Serialize};

/// Timeout for one debit attempt -- matches the Python alpha's
/// `_TIMEOUT_SECONDS = 5.0`.
const TIMEOUT: Duration = Duration::from_secs(5);

/// Stable, caller-facing vocabulary for why a debit didn't happen -- mirrors
/// hub-api's own `REASON_*` constants (`token_billing_service.py`) so a 402
/// from hub-api and a local network failure collapse into the same shape
/// for the caller's fallback branch.
pub const REASON_INSUFFICIENT_BALANCE: &str = "insufficient_balance";
pub const REASON_LEDGER_UNAVAILABLE: &str = "ledger_unavailable";

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

    fn ledger_unavailable() -> Self {
        Self {
            ok: false,
            balance_after: None,
            blocked_reason: Some(REASON_LEDGER_UNAVAILABLE.to_string()),
        }
    }
}

#[derive(Debug, Serialize)]
struct DebitRequestBody<'a> {
    product_key: &'a str,
    amount: i64,
    reason: &'static str,
    #[serde(rename = "ref")]
    reference: &'a str,
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
}

impl Default for TokenLedgerClient {
    fn default() -> Self {
        Self::new()
    }
}

impl TokenLedgerClient {
    /// Builds a client with the fixed 5s admission timeout. Infallible:
    /// `reqwest::Client::builder()` with only a timeout set never fails to
    /// build (no TLS/proxy config that could error).
    pub fn new() -> Self {
        Self {
            http: reqwest::Client::builder()
                .timeout(TIMEOUT)
                .build()
                .expect("reqwest::Client::builder with only a timeout cannot fail"),
        }
    }

    /// Builds a client around an already-constructed `reqwest::Client` --
    /// used by tests that need a client pointed at a local mock server
    /// with the same (or a shorter) timeout.
    #[cfg(test)]
    fn with_client(http: reqwest::Client) -> Self {
        Self { http }
    }

    /// POSTs a real debit to hub-api's token ledger; never returns an
    /// `Err` for a blocked/unreachable outcome -- callers branch on
    /// [`TokenDebitResult::ok`], matching the Python
    /// `debit_transcoding_tokens` contract ("never raises for a
    /// blocked/unreachable outcome").
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
        bearer_token: &str,
        community_id: i32,
        amount: i64,
        product_key: &str,
        reference: &str,
    ) -> TokenDebitResult {
        let url =
            format!("{hub_api_url}/api/v1/marketplace/communities/{community_id}/tokens/debit");
        let body = DebitRequestBody {
            product_key,
            amount,
            reason: "svc_streaming_transcode_admission",
            reference,
        };

        let response = match self
            .http
            .post(&url)
            .bearer_auth(bearer_token)
            .json(&body)
            .send()
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
    use axum::routing::post;
    use axum::{Json, Router};
    use std::time::Duration as StdDuration;

    async fn spawn_mock(
        status: axum::http::StatusCode,
        body: serde_json::Value,
    ) -> (String, tokio::sync::oneshot::Receiver<serde_json::Value>) {
        let (tx, rx) = tokio::sync::oneshot::channel();
        let tx = std::sync::Arc::new(std::sync::Mutex::new(Some(tx)));
        let app = Router::new().route(
            "/api/v1/marketplace/communities/{community_id}/tokens/debit",
            post(move |axum::Json(received): axum::Json<serde_json::Value>| {
                let tx = tx.clone();
                let body = body.clone();
                async move {
                    if let Some(tx) = tx.lock().unwrap().take() {
                        let _ = tx.send(received);
                    }
                    (status, Json(body))
                }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        (format!("http://{addr}"), rx)
    }

    fn test_client() -> TokenLedgerClient {
        TokenLedgerClient::with_client(
            reqwest::Client::builder()
                .timeout(StdDuration::from_secs(3))
                .build()
                .expect("client builds"),
        )
    }

    #[tokio::test]
    async fn debit_success_builds_correct_request_and_returns_balance() {
        let (base_url, rx) = spawn_mock(
            axum::http::StatusCode::OK,
            serde_json::json!({"success": true, "balance_after": 55, "transaction_id": 1}),
        )
        .await;

        let result = test_client()
            .debit_transcoding_tokens(
                &base_url,
                "the-callers-jwt",
                7,
                5,
                "transcoding_minutes",
                "stream:1:2026-09-01T00:00:00",
            )
            .await;

        assert!(result.ok);
        assert_eq!(result.balance_after, Some(55));
        assert_eq!(result.blocked_reason, None);

        let received = rx.await.unwrap();
        assert_eq!(received["product_key"], "transcoding_minutes");
        assert_eq!(received["amount"], 5);
        assert_eq!(received["reason"], "svc_streaming_transcode_admission");
        assert_eq!(received["ref"], "stream:1:2026-09-01T00:00:00");
    }

    #[tokio::test]
    async fn debit_insufficient_balance_maps_402_to_blocked_reason() {
        let (base_url, _rx) = spawn_mock(
            axum::http::StatusCode::PAYMENT_REQUIRED,
            serde_json::json!({"success": false, "blocked_reason": "insufficient_balance"}),
        )
        .await;

        let result = test_client()
            .debit_transcoding_tokens(&base_url, "tok", 7, 5, "transcoding_minutes", "ref-1")
            .await;

        assert!(!result.ok);
        assert_eq!(
            result.blocked_reason.as_deref(),
            Some(REASON_INSUFFICIENT_BALANCE)
        );
        assert_eq!(result.balance_after, None);
    }

    #[tokio::test]
    async fn debit_unexpected_status_degrades_to_ledger_unavailable() {
        let (base_url, _rx) = spawn_mock(
            axum::http::StatusCode::INTERNAL_SERVER_ERROR,
            serde_json::json!({}),
        )
        .await;

        let result = test_client()
            .debit_transcoding_tokens(&base_url, "tok", 7, 5, "transcoding_minutes", "ref-3")
            .await;

        assert!(!result.ok);
        assert_eq!(
            result.blocked_reason.as_deref(),
            Some(REASON_LEDGER_UNAVAILABLE)
        );
    }

    #[tokio::test]
    async fn debit_network_failure_is_ledger_unavailable_not_a_panic() {
        // Port 1 -- reserved/unassigned, connection refused immediately
        // rather than timing out the whole test.
        let result = test_client()
            .debit_transcoding_tokens(
                "http://127.0.0.1:1",
                "tok",
                7,
                5,
                "transcoding_minutes",
                "ref-2",
            )
            .await;

        assert!(!result.ok);
        assert_eq!(
            result.blocked_reason.as_deref(),
            Some(REASON_LEDGER_UNAVAILABLE)
        );
    }

    #[tokio::test]
    async fn debit_malformed_success_body_degrades_to_ledger_unavailable() {
        let (base_url, _rx) = spawn_mock(
            axum::http::StatusCode::OK,
            serde_json::json!({"success": true}), // missing balance_after
        )
        .await;

        let result = test_client()
            .debit_transcoding_tokens(&base_url, "tok", 7, 5, "transcoding_minutes", "ref-4")
            .await;

        assert!(!result.ok);
        assert_eq!(
            result.blocked_reason.as_deref(),
            Some(REASON_LEDGER_UNAVAILABLE)
        );
    }
}
