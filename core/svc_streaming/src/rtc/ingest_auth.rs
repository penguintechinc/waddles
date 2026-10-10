//! WHIP token authorization: [`WhipTokenAuthorizer`] plus
//! [`InternalIngestAuthClient`], the HTTP-backed implementation that calls
//! this service's own `POST /api/v1/internal/streaming/ingest-auth`
//! (owned by chunk S2, `src/api/internal.rs`) -- that module's doc comment
//! names the mechanism explicitly: "called by the ingest listeners (S4
//! RTMP/S5 SRT/S6 WHIP) to authorize a stream key at connect time".
//!
//! **Currently blocked on an already-documented gap outside this chunk's
//! file ownership:** `src/api/internal.rs`'s own module doc records that
//! `src/http/mod.rs` wraps the *entire* `/api/v1` nest (including
//! `/internal/*`) in `route_layer(auth::require_auth)` instead of
//! exempting service-to-service routes from the user-JWT gate, so a real
//! call through [`InternalIngestAuthClient`] 401s on that layer before
//! `ServiceKey` is even checked, until `src/http/mod.rs` (outside
//! `src/rtc/**`/`src/ingest/whip.rs`/`src/egress/whep.rs`) is fixed. The
//! client and [`WhipTokenAuthorizer`] abstraction below are both correct
//! and ready for that fix -- see `src/ingest/whip.rs`'s router construction
//! for how a caller supplies a different [`WhipTokenAuthorizer`] in the
//! meantime (tests use an in-memory fake).

use std::time::Duration;

use serde::{Deserialize, Serialize};
use thiserror::Error;

use crate::config::Config;
use crate::telemetry::stream::{ExternalPeer, StreamMetrics};
use crate::telemetry::trace_context::inject_current_context;

/// Errors calling the ingest-auth endpoint.
#[derive(Debug, Error)]
pub enum IngestAuthError {
    #[error("ingest-auth request failed: {0}")]
    Request(String),
    #[error("ingest-auth returned HTTP {0}")]
    Status(u16),
}

/// Authorizes a WHIP token (the stream key presented in `POST
/// /whip/{token}`) before a session is created. `async_trait` so
/// `src/ingest/whip.rs` can hold `Arc<dyn WhipTokenAuthorizer>` and swap a
/// real HTTP-backed implementation for a test fake.
#[async_trait::async_trait]
pub trait WhipTokenAuthorizer: Send + Sync {
    /// `Ok(true)` = authorized, `Ok(false)` = a well-formed decision that
    /// denies the token (maps to 401 in the WHIP handler), `Err` = the
    /// authorization check itself failed (maps to 500 -- a caller must not
    /// treat this as "allowed", per fail-closed authz).
    async fn authorize(&self, token: &str) -> Result<bool, IngestAuthError>;
}

/// Request body for the loopback ingest-auth hop. Deliberately not `Debug`:
/// `key` is the presented WHIP token, a bearer credential.
#[derive(Serialize)]
struct IngestAuthRequestBody<'a> {
    kind: &'a str,
    key: &'a str,
}

/// Mirrors `src/api/response.rs`'s `{"status", "data", "meta"}` envelope --
/// only `data` is needed here.
#[derive(Debug, Deserialize)]
struct Envelope<T> {
    data: T,
}

/// Mirrors `src/api/internal.rs::IngestAuthResponse`'s wire shape.
#[derive(Debug, Deserialize)]
struct IngestAuthResponseBody {
    allowed: bool,
}

/// Calls this process's own control-plane HTTP port
/// (`http://127.0.0.1:{MODULE_PORT}/api/v1/internal/streaming/ingest-auth`)
/// rather than an external URL -- the ingest-auth route lives in the same
/// binary, so a loopback call avoids a network hop and a second TLS
/// negotiation for what is, in production, always a same-process check.
pub struct InternalIngestAuthClient {
    http: reqwest::Client,
    base_url: String,
    service_key: String,
    metrics: StreamMetrics,
}

impl std::fmt::Debug for InternalIngestAuthClient {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("InternalIngestAuthClient")
            .field("base_url", &self.base_url)
            .field("service_key", &"***redacted***")
            .finish()
    }
}

impl InternalIngestAuthClient {
    /// Builds a client targeting this process's own `MODULE_PORT` and
    /// authenticating with `SERVICE_API_KEY` -- both already loaded onto
    /// `config` by `crate::config::Config::load`, so no new env var is
    /// introduced.
    pub fn new(config: &Config) -> Result<Self, IngestAuthError> {
        let http = reqwest::Client::builder()
            .timeout(Duration::from_secs(3))
            .build()
            .map_err(|err| IngestAuthError::Request(err.to_string()))?;
        Ok(Self {
            http,
            base_url: format!("http://127.0.0.1:{}/api/v1", config.cli.http_port),
            service_key: config.service_api_key.expose().to_string(),
            metrics: StreamMetrics::shared(),
        })
    }

    /// Replaces the process-wide stream instruments with an explicit handle
    /// -- for tests that install their own meter provider.
    pub fn with_stream_metrics(mut self, metrics: StreamMetrics) -> Self {
        self.metrics = metrics;
        self
    }

    /// Builds a client against an explicit base URL -- used by tests
    /// pointed at a local mock server instead of a real process port.
    #[cfg(test)]
    fn with_base_url(base_url: String, service_key: String) -> Self {
        Self {
            http: reqwest::Client::builder()
                .timeout(Duration::from_secs(3))
                .build()
                .expect("client builds"),
            base_url,
            service_key,
            metrics: StreamMetrics::shared(),
        }
    }
}

#[async_trait::async_trait]
impl WhipTokenAuthorizer for InternalIngestAuthClient {
    async fn authorize(&self, token: &str) -> Result<bool, IngestAuthError> {
        let url = format!("{}/internal/streaming/ingest-auth", self.base_url);
        // The loopback hop re-enters this service's own router, whose
        // request span parents itself to this `traceparent` -- the whole
        // WHIP authorization is one trace.
        let mut trace_headers = reqwest::header::HeaderMap::new();
        inject_current_context(&mut trace_headers);
        let response = self
            .metrics
            .time_external(
                ExternalPeer::IngestAuth,
                self.http
                    .post(url)
                    .headers(trace_headers)
                    .header("x-service-key", &self.service_key)
                    .json(&IngestAuthRequestBody {
                        kind: "whip",
                        key: token,
                    })
                    .send(),
            )
            .await
            .map_err(|err| IngestAuthError::Request(err.to_string()))?;

        if !response.status().is_success() {
            return Err(IngestAuthError::Status(response.status().as_u16()));
        }

        let envelope: Envelope<IngestAuthResponseBody> = response
            .json()
            .await
            .map_err(|err| IngestAuthError::Request(err.to_string()))?;
        Ok(envelope.data.allowed)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::routing::post;
    use axum::{Json, Router};

    async fn spawn_mock(allowed: bool, status: axum::http::StatusCode) -> String {
        let app = Router::new().route(
            "/api/v1/internal/streaming/ingest-auth",
            post(move || async move {
                (
                    status,
                    Json(serde_json::json!({
                        "status": "success",
                        "data": {"allowed": allowed, "community_id": null, "config_id": null},
                        "meta": {"version": 1, "timestamp": "2026-09-11T00:00:00Z"}
                    })),
                )
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        format!("http://{addr}/api/v1")
    }

    #[tokio::test]
    async fn allowed_true_authorizes() {
        let base_url = spawn_mock(true, axum::http::StatusCode::OK).await;
        let client = InternalIngestAuthClient::with_base_url(base_url, "svc-key".into());
        assert!(client.authorize("tok_abc").await.unwrap());
    }

    #[tokio::test]
    async fn allowed_false_denies_without_erroring() {
        let base_url = spawn_mock(false, axum::http::StatusCode::OK).await;
        let client = InternalIngestAuthClient::with_base_url(base_url, "svc-key".into());
        assert!(!client.authorize("tok_unknown").await.unwrap());
    }

    #[tokio::test]
    async fn non_success_status_is_an_error_not_a_denial() {
        let base_url = spawn_mock(true, axum::http::StatusCode::UNAUTHORIZED).await;
        let client = InternalIngestAuthClient::with_base_url(base_url, "wrong-key".into());
        let err = client.authorize("tok_abc").await.unwrap_err();
        assert!(matches!(err, IngestAuthError::Status(401)));
    }

    #[tokio::test]
    async fn unreachable_base_url_is_a_request_error() {
        // Port 1 -- reserved/unassigned, connection refused immediately
        // rather than timing out the whole test.
        let client =
            InternalIngestAuthClient::with_base_url("http://127.0.0.1:1".into(), "k".into());
        let err = client.authorize("tok_abc").await.unwrap_err();
        assert!(matches!(err, IngestAuthError::Request(_)));
    }
}
