//! Typed API error surface for axum handlers. Every handler returns
//! `Result<T, ApiError>` so the HTTP boundary never leaks a bare
//! `anyhow::Error` -- see `rules/security.md` Output Validation, which
//! applies equally to error bodies as to success bodies.
//!
//! `Unimplemented` is declared (mirrors `core/svc_streaming/src/error.rs`)
//! for P2-P4 to use once they add real overlay render/push handlers, but
//! P1 never constructs it: this scaffold mounts no route that could need
//! it -- `rules/general.md` Red Flags forbids a placeholder feature that
//! isn't actually done yet; a route returning `Unimplemented` on every
//! call is exactly that, so P1 mounts zero such routes rather than one.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde::Serialize;
use thiserror::Error;

/// The single error type returned by every axum handler in this service.
/// Each variant maps to a specific HTTP status; internal error detail is
/// logged via `tracing` and never echoed back to the caller.
#[derive(Debug, Error)]
pub enum ApiError {
    /// Missing/invalid/expired credential -- maps to 401.
    #[error("unauthorized: {0}")]
    Unauthorized(String),
    /// Authenticated but not permitted (tenant/community mismatch) -- maps to 403.
    #[error("forbidden: {0}")]
    Forbidden(String),
    /// Resource does not exist -- maps to 404.
    #[error("not found: {0}")]
    NotFound(String),
    /// Caller input failed validation -- maps to 400.
    #[error("bad request: {0}")]
    BadRequest(String),
    /// Request body exceeds the surface's size cap -- maps to 413.
    #[error("payload too large: {0}")]
    PayloadTooLarge(String),
    /// Body parsed but violates the surface contract -- maps to 422. The
    /// violations are (path, rule) pairs only, never the offending values.
    #[error("unprocessable: {message}")]
    Unprocessable {
        /// Short human message.
        message: String,
        /// Where and why validation failed.
        violations: Vec<crate::surfaces::contract::Violation>,
    },
    /// Rate limit hit -- maps to 429 with `Retry-After`.
    #[error("rate limited: retry after {retry_after_secs}s")]
    TooManyRequests {
        /// Whole seconds the caller should wait (at least 1).
        retry_after_secs: u64,
    },
    /// Route/feature exists but a later chunk (P2-P4) owns the
    /// implementation -- maps to 501. See module doc: unused in P1.
    #[error("not yet implemented: {0}")]
    Unimplemented(String),
    /// Anything else -- maps to 500, detail is logged not returned.
    #[error("internal error")]
    Internal(#[from] anyhow::Error),
}

/// JSON error body shape returned for every non-2xx response.
#[derive(Debug, Serialize)]
struct ErrorBody {
    error: &'static str,
    message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    violations: Option<Vec<crate::surfaces::contract::Violation>>,
}

impl ApiError {
    fn status_and_code(&self) -> (StatusCode, &'static str) {
        match self {
            ApiError::Unauthorized(_) => (StatusCode::UNAUTHORIZED, "unauthorized"),
            ApiError::Forbidden(_) => (StatusCode::FORBIDDEN, "forbidden"),
            ApiError::NotFound(_) => (StatusCode::NOT_FOUND, "not_found"),
            ApiError::BadRequest(_) => (StatusCode::BAD_REQUEST, "bad_request"),
            ApiError::PayloadTooLarge(_) => (StatusCode::PAYLOAD_TOO_LARGE, "payload_too_large"),
            ApiError::Unprocessable { .. } => {
                (StatusCode::UNPROCESSABLE_ENTITY, "unprocessable_entity")
            }
            ApiError::TooManyRequests { .. } => (StatusCode::TOO_MANY_REQUESTS, "rate_limited"),
            ApiError::Unimplemented(_) => (StatusCode::NOT_IMPLEMENTED, "not_implemented"),
            ApiError::Internal(_) => (StatusCode::INTERNAL_SERVER_ERROR, "internal_error"),
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let (status, code) = self.status_and_code();
        let message = match &self {
            ApiError::Internal(err) => {
                tracing::error!(error = %err, "internal error");
                "an internal error occurred".to_string()
            }
            other => other.to_string(),
        };
        let retry_after = match &self {
            ApiError::TooManyRequests { retry_after_secs } => Some(*retry_after_secs),
            _ => None,
        };
        let violations = match &self {
            ApiError::Unprocessable { violations, .. } => Some(violations.clone()),
            _ => None,
        };
        let mut response = (
            status,
            Json(ErrorBody {
                error: code,
                message,
                violations,
            }),
        )
            .into_response();
        if let Some(secs) = retry_after {
            response
                .headers_mut()
                .insert(axum::http::header::RETRY_AFTER, secs.into());
        }
        response
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::to_bytes;

    #[tokio::test]
    async fn unauthorized_maps_to_401() {
        let resp = ApiError::Unauthorized("missing bearer token".into()).into_response();
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let parsed: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(parsed["error"], "unauthorized");
    }

    #[tokio::test]
    async fn internal_error_hides_detail() {
        let resp =
            ApiError::Internal(anyhow::anyhow!("db connection string leaked")).into_response();
        assert_eq!(resp.status(), StatusCode::INTERNAL_SERVER_ERROR);
        let body = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let parsed: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(parsed["message"], "an internal error occurred");
    }

    #[tokio::test]
    async fn unimplemented_maps_to_501() {
        let resp = ApiError::Unimplemented("overlay render".into()).into_response();
        assert_eq!(resp.status(), StatusCode::NOT_IMPLEMENTED);
    }

    #[tokio::test]
    async fn not_found_maps_to_404() {
        let resp = ApiError::NotFound("community".into()).into_response();
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn forbidden_maps_to_403() {
        let resp = ApiError::Forbidden("wrong community".into()).into_response();
        assert_eq!(resp.status(), StatusCode::FORBIDDEN);
    }

    #[tokio::test]
    async fn bad_request_maps_to_400() {
        let resp = ApiError::BadRequest("invalid surface".into()).into_response();
        assert_eq!(resp.status(), StatusCode::BAD_REQUEST);
    }
}
