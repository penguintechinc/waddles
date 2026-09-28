//! `POST /eventsub/twitch/webhook[/{callback_key}]` -- the axum adapter over
//! `crate::ingest::twitch_eventsub::handle_webhook`. Thin by design: path/
//! header/body extraction and response rendering only, so every branch of
//! the actual verification/dedup/dispatch logic is unit-tested in
//! `crate::ingest::twitch_eventsub` without axum or a live socket at all.
//!
//! Two routes, one handler: the bare legacy path (matches the original
//! `core/svc_ingest/eventsub.py` mount, so ingress/Cilium policy/WAF rules
//! for it never need to change) resolves its secret under
//! `twitch_eventsub::LEGACY_CALLBACK_KEY`; the new `/{callback_key}` path
//! carries an opaque per-subscription identifier a
//! [`crate::ingest::twitch_eventsub::SubscriptionSecretResolver`] looks the
//! secret up by -- see that module's own doc comment for why secret
//! resolution reads the URL path, never a body field. See
//! `docs/superpowers/specs/2026-09-28-connections-credentials-design.md`
//! §4.1/§4.4.

use std::sync::Arc;
use std::time::Instant;

use axum::body::Bytes;
use axum::extract::{DefaultBodyLimit, Path, State};
use axum::http::{header, HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::post;
use axum::{Json, Router};

use penguin_spine::{KeyRing, Scope, SpineClient};

use crate::error::ApiError;
use crate::http::AppState;
use crate::ingest::twitch_eventsub::{
    self, EnvSecretResolver, EventSubError, EventSubResponse, RawHeaders, RedisReplayGuard,
    RedisRevocationSink, LEGACY_CALLBACK_KEY,
};
use crate::telemetry::IngestMetrics;

/// Every dependency the Twitch EventSub webhook handler needs, built once
/// at startup (`crate::lib::try_build_eventsub_state`) and shared across
/// every request via `Arc` -- concrete production types throughout (no
/// `dyn`), matching this crate's established "generic bound, not trait
/// object" convention (see `crate::publish::EventAppender`'s doc comment):
/// there is exactly one production implementation of each dependency, so
/// runtime polymorphism buys nothing here that a concrete field doesn't
/// already give more cheaply.
pub struct EventSubState {
    pub resolver: EnvSecretResolver,
    pub dedup: RedisReplayGuard,
    pub revocation: RedisRevocationSink,
    pub appender: SpineClient,
    pub metrics: Arc<IngestMetrics>,
    pub keyring: KeyRing,
    pub active_kid: String,
    pub scope: Scope,
    /// Identity-field (`actor`) DEK provider -- `crate::identity_crypto`,
    /// forwarded to `handle_webhook` unchanged.
    pub dek_provider: crate::identity_crypto::ConfiguredDekProvider<
        crate::identity_crypto::UnimplementedMachineJwtProvider,
    >,
}

/// Body-size cap enforced at the HTTP layer via
/// [`axum::extract::DefaultBodyLimit`] -- rejects an oversized request
/// before axum ever finishes buffering the body, cheaper than
/// `crate::ingest::twitch_eventsub::handle_webhook`'s own defense-in-depth
/// re-check.
pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/eventsub/twitch/webhook", post(twitch_webhook_legacy))
        .route(
            "/eventsub/twitch/webhook/{callback_key}",
            post(twitch_webhook),
        )
        .route_layer(DefaultBodyLimit::max(
            twitch_eventsub::MAX_EVENTSUB_BODY_BYTES,
        ))
        .with_state(state)
}

/// `From<EventSubError> for ApiError`: collapses every verification-adjacent
/// failure to the same fixed `Forbidden` message -- `security.md`'s "uniform
/// error responses (no oracle)". Structural failures (bad content-type,
/// unparseable JSON) get their own, still-generic, message.
impl From<EventSubError> for ApiError {
    fn from(err: EventSubError) -> Self {
        match err {
            EventSubError::UnsupportedContentType => {
                ApiError::BadRequest("unsupported content type or body too large".to_string())
            }
            EventSubError::MalformedBody => {
                ApiError::BadRequest("malformed request body".to_string())
            }
            EventSubError::VerificationFailed => {
                ApiError::Forbidden("eventsub verification failed".to_string())
            }
        }
    }
}

/// Renders the non-error [`EventSubResponse`] variants. Every branch is a
/// 200 (Twitch redelivers on anything else); `Challenge` is `text/plain`
/// verbatim (the raw challenge string, never wrapped in JSON) -- Twitch's
/// own `webhook_callback_verification` contract requires the response body
/// to be exactly the challenge value.
fn render(response: EventSubResponse) -> Response {
    match response {
        EventSubResponse::Challenge(challenge) => (
            StatusCode::OK,
            [(header::CONTENT_TYPE, "text/plain; charset=utf-8")],
            challenge,
        )
            .into_response(),
        EventSubResponse::Ack => {
            (StatusCode::OK, Json(serde_json::json!({"status": "ok"}))).into_response()
        }
        EventSubResponse::DuplicateIgnored => (
            StatusCode::OK,
            Json(serde_json::json!({"status": "duplicate_ignored"})),
        )
            .into_response(),
        EventSubResponse::Acknowledged => (
            StatusCode::OK,
            Json(serde_json::json!({"status": "acknowledged"})),
        )
            .into_response(),
        EventSubResponse::Ignored => (
            StatusCode::OK,
            Json(serde_json::json!({"status": "ignored"})),
        )
            .into_response(),
        EventSubResponse::UnknownType => (
            StatusCode::OK,
            Json(serde_json::json!({"status": "unknown_type"})),
        )
            .into_response(),
    }
}

/// `outcome` label used for the eventsub metrics/latency histogram --
/// distinct from the internal verification-outcome labels
/// (`crate::telemetry::IngestMetrics::record_eventsub_verification`), this
/// one names the terminal HTTP-visible result.
fn outcome_label(result: &Result<EventSubResponse, EventSubError>) -> &'static str {
    match result {
        Ok(EventSubResponse::Challenge(_)) => "challenge",
        Ok(EventSubResponse::Ack) => "ack",
        Ok(EventSubResponse::DuplicateIgnored) => "duplicate_ignored",
        Ok(EventSubResponse::Acknowledged) => "acknowledged",
        Ok(EventSubResponse::Ignored) => "ignored",
        Ok(EventSubResponse::UnknownType) => "unknown_type",
        Err(EventSubError::UnsupportedContentType) => "unsupported_content_type",
        Err(EventSubError::MalformedBody) => "malformed_body",
        Err(EventSubError::VerificationFailed) => "verification_failed",
    }
}

/// `POST /eventsub/twitch/webhook` (legacy, no `callback_key` segment) --
/// resolves its secret under [`LEGACY_CALLBACK_KEY`] for alpha backward
/// compatibility with subscriptions already registered against this bare
/// URL.
async fn twitch_webhook_legacy(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Response, ApiError> {
    process(state, LEGACY_CALLBACK_KEY, headers, body).await
}

/// `POST /eventsub/twitch/webhook/{callback_key}`. `callback_key` is an
/// opaque path segment (never trusted for anything beyond secret lookup --
/// it carries no tenant/scope information itself, matching `security.md`'s
/// "never trust an id from the request path for routing/authz" for the
/// *body*, though this one legitimately IS the routing key by design, see
/// `crate::ingest::twitch_eventsub`'s module doc).
async fn twitch_webhook(
    State(state): State<AppState>,
    Path(callback_key): Path<String>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Response, ApiError> {
    process(state, &callback_key, headers, body).await
}

/// Shared body for both routes: builds the raw header/content-type view and
/// delegates to `crate::ingest::twitch_eventsub::handle_webhook`. Returns
/// `503` (not the `ApiError`/`EventSubError` surface) when this instance has
/// no configured eventsub secret/keyring/spine connection -- see
/// `crate::lib::try_build_eventsub_state`'s own graceful-degradation
/// contract, same shape as every other fixed-platform receiver in this
/// crate.
async fn process(
    state: AppState,
    callback_key: &str,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Response, ApiError> {
    let start = Instant::now();
    let Some(es) = state.eventsub.as_ref() else {
        tracing::warn!("eventsub receiver not configured; rejecting Twitch EventSub delivery");
        return Ok((
            StatusCode::SERVICE_UNAVAILABLE,
            "twitch eventsub receiver not configured",
        )
            .into_response());
    };

    let content_type = headers
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok());
    let raw_headers = RawHeaders {
        message_type: headers
            .get(twitch_eventsub::HEADER_MESSAGE_TYPE)
            .and_then(|v| v.to_str().ok()),
        message_id: headers
            .get(twitch_eventsub::HEADER_MESSAGE_ID)
            .and_then(|v| v.to_str().ok()),
        timestamp: headers
            .get(twitch_eventsub::HEADER_MESSAGE_TIMESTAMP)
            .and_then(|v| v.to_str().ok()),
        signature: headers
            .get(twitch_eventsub::HEADER_MESSAGE_SIGNATURE)
            .and_then(|v| v.to_str().ok()),
    };

    let result = twitch_eventsub::handle_webhook(
        &es.resolver,
        &es.dedup,
        &es.revocation,
        &es.appender,
        es.metrics.as_ref(),
        &es.keyring,
        &es.active_kid,
        &es.scope,
        &es.dek_provider,
        callback_key,
        content_type,
        &raw_headers,
        &body,
    )
    .await;

    es.metrics
        .observe_eventsub_duration(outcome_label(&result), start.elapsed().as_secs_f64());

    match result {
        Ok(response) => Ok(render(response)),
        Err(err) => Err(ApiError::from(err)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{CliConfig, Config};
    use axum::body::Body;
    use axum::http::Request;
    use clap::Parser;
    use http_body_util::BodyExt;
    use tower::ServiceExt;

    fn test_state() -> AppState {
        let cli = CliConfig::parse_from(["svc-ingest"]);
        let config = Config::from_cli(cli).expect("defaults require no secrets");
        AppState::new(config, prometheus::Registry::new())
    }

    #[tokio::test]
    async fn unconfigured_eventsub_returns_503() {
        let app = router(test_state());
        let response = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/eventsub/twitch/webhook")
                    .header("content-type", "application/json")
                    .body(Body::from("{}"))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    #[tokio::test]
    async fn unconfigured_eventsub_returns_503_on_the_callback_key_route_too() {
        let app = router(test_state());
        let response = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/eventsub/twitch/webhook/some-callback-key")
                    .header("content-type", "application/json")
                    .body(Body::from("{}"))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
    }

    #[tokio::test]
    async fn oversized_body_is_rejected_by_the_body_limit_layer() {
        let app = router(test_state());
        let oversized = vec![b'a'; twitch_eventsub::MAX_EVENTSUB_BODY_BYTES + 1];
        let response = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/eventsub/twitch/webhook")
                    .header("content-type", "application/json")
                    .body(Body::from(oversized))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::PAYLOAD_TOO_LARGE);
    }

    #[tokio::test]
    async fn wrong_method_is_not_mounted() {
        let app = router(test_state());
        let response = app
            .oneshot(
                Request::builder()
                    .method("GET")
                    .uri("/eventsub/twitch/webhook")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::METHOD_NOT_ALLOWED);
    }

    #[test]
    fn eventsub_error_maps_to_forbidden_uniformly() {
        let api_err: ApiError = EventSubError::VerificationFailed.into();
        assert!(matches!(api_err, ApiError::Forbidden(_)));
    }

    #[test]
    fn eventsub_error_content_type_maps_to_bad_request() {
        let api_err: ApiError = EventSubError::UnsupportedContentType.into();
        assert!(matches!(api_err, ApiError::BadRequest(_)));
    }

    #[test]
    fn eventsub_error_malformed_body_maps_to_bad_request() {
        let api_err: ApiError = EventSubError::MalformedBody.into();
        assert!(matches!(api_err, ApiError::BadRequest(_)));
    }

    #[tokio::test]
    async fn challenge_response_is_plain_text_not_json() {
        let response = render(EventSubResponse::Challenge("chal-123".to_string()));
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(
            response
                .headers()
                .get(header::CONTENT_TYPE)
                .and_then(|v| v.to_str().ok()),
            Some("text/plain; charset=utf-8")
        );
        let body = response.into_body().collect().await.unwrap().to_bytes();
        assert_eq!(&body[..], b"chal-123");
    }

    #[test]
    fn outcome_label_covers_every_variant() {
        assert_eq!(outcome_label(&Ok(EventSubResponse::Ack)), "ack");
        assert_eq!(
            outcome_label(&Ok(EventSubResponse::DuplicateIgnored)),
            "duplicate_ignored"
        );
        assert_eq!(
            outcome_label(&Err(EventSubError::VerificationFailed)),
            "verification_failed"
        );
    }
}
