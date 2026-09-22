//! Typed API error surface for axum handlers. Every handler returns
//! `Result<T, ApiError>` so the HTTP boundary never leaks a bare
//! `anyhow::Error` -- see `rules/security.md` Output Validation, which
//! applies equally to error bodies as to success bodies.
//!
//! Identical to `core/svc_streaming/src/error.rs` (the M4 reference
//! template).

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
    /// Authenticated but not permitted (tenant/scope mismatch) -- maps to 403.
    #[error("forbidden: {0}")]
    Forbidden(String),
    /// Resource does not exist -- maps to 404.
    #[error("not found: {0}")]
    NotFound(String),
    /// Caller input failed validation -- maps to 400.
    #[error("bad request: {0}")]
    BadRequest(String),
    /// Route/feature exists but a later chunk owns the implementation
    /// (e.g. executor integration, `// TODO(M4)` seams) -- maps to 501.
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
}

impl ApiError {
    fn status_and_code(&self) -> (StatusCode, &'static str) {
        match self {
            ApiError::Unauthorized(_) => (StatusCode::UNAUTHORIZED, "unauthorized"),
            ApiError::Forbidden(_) => (StatusCode::FORBIDDEN, "forbidden"),
            ApiError::NotFound(_) => (StatusCode::NOT_FOUND, "not_found"),
            ApiError::BadRequest(_) => (StatusCode::BAD_REQUEST, "bad_request"),
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
        (
            status,
            Json(ErrorBody {
                error: code,
                message,
            }),
        )
            .into_response()
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
        let resp = ApiError::Unimplemented("executor integration".into()).into_response();
        assert_eq!(resp.status(), StatusCode::NOT_IMPLEMENTED);
    }
}
