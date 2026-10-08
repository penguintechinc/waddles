//! Every failure this crate can raise, mapped to an HTTP response.
//!
//! Split the same way `service_auth::ServiceAuthError`'s doc comment
//! describes: authn/authz rejections (`InvalidCommunityId`, `MissingKey`,
//! `InvalidKey`, `InactiveCredential`, `MissingBearer`, `ServiceAuth`)
//! must never leak *which* check failed to a remote caller (all collapse
//! to 401/403 in [`IntoResponse`]); operational failures (`Store`) map to
//! 500 and are safe to log with detail server-side (never sent to the
//! client body).

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};

/// Errors raised while validating a VIEW or PUSH overlay credential.
#[derive(thiserror::Error, Debug)]
pub enum OverlayAuthError {
    /// The `community` path segment isn't a valid community id.
    #[error("invalid community id")]
    InvalidCommunityId,
    /// No `key` query parameter (VIEW) or `Authorization` header (PUSH)
    /// was presented at all.
    #[error("missing credential")]
    MissingKey,
    /// A credential was presented but didn't validate against any known
    /// current/previous hash, or validated for a *different* community.
    #[error("invalid credential")]
    InvalidKey,
    /// The credential's underlying record exists but is `is_active = false`.
    #[error("credential inactive")]
    InactiveCredential,
    /// No `Authorization: Bearer <jwt>` header on a PUSH-gated route.
    #[error("missing bearer token")]
    MissingBearer,
    /// PUSH credential (machine JWT) failed `service_auth::verify`.
    #[error(transparent)]
    ServiceAuth(#[from] service_auth::ServiceAuthError),
    /// The backing store (DB) failed -- an operational error, not a
    /// rejected credential. Logged with detail, never echoed to the client.
    #[error("overlay credential store error: {0}")]
    Store(String),
}

impl IntoResponse for OverlayAuthError {
    fn into_response(self) -> Response {
        let (status, public_message) = match &self {
            OverlayAuthError::InvalidCommunityId => {
                (StatusCode::BAD_REQUEST, "invalid community id")
            }
            OverlayAuthError::MissingKey | OverlayAuthError::MissingBearer => {
                (StatusCode::UNAUTHORIZED, "unauthorized")
            }
            OverlayAuthError::InvalidKey
            | OverlayAuthError::InactiveCredential
            | OverlayAuthError::ServiceAuth(_) => (StatusCode::FORBIDDEN, "forbidden"),
            OverlayAuthError::Store(detail) => {
                tracing::error!(error = %detail, "overlay_auth store error");
                (StatusCode::INTERNAL_SERVER_ERROR, "internal error")
            }
        };
        (status, public_message).into_response()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn status_of(err: OverlayAuthError) -> StatusCode {
        err.into_response().status()
    }

    #[test]
    fn invalid_community_id_is_400() {
        assert_eq!(
            status_of(OverlayAuthError::InvalidCommunityId),
            StatusCode::BAD_REQUEST
        );
    }

    #[test]
    fn missing_key_is_401() {
        assert_eq!(
            status_of(OverlayAuthError::MissingKey),
            StatusCode::UNAUTHORIZED
        );
    }

    #[test]
    fn missing_bearer_is_401() {
        assert_eq!(
            status_of(OverlayAuthError::MissingBearer),
            StatusCode::UNAUTHORIZED
        );
    }

    #[test]
    fn invalid_key_is_403() {
        assert_eq!(
            status_of(OverlayAuthError::InvalidKey),
            StatusCode::FORBIDDEN
        );
    }

    #[test]
    fn inactive_credential_is_403() {
        assert_eq!(
            status_of(OverlayAuthError::InactiveCredential),
            StatusCode::FORBIDDEN
        );
    }

    #[test]
    fn service_auth_rejection_is_403() {
        let inner = service_auth::ServiceAuthError::InvalidToken("bad sig".into());
        assert_eq!(
            status_of(OverlayAuthError::ServiceAuth(inner)),
            StatusCode::FORBIDDEN
        );
    }

    #[test]
    fn store_failure_is_500_and_never_leaks_detail() {
        let err = OverlayAuthError::Store("connection refused".into());
        let response = err.into_response();
        assert_eq!(response.status(), StatusCode::INTERNAL_SERVER_ERROR);
    }
}
