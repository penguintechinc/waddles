//! Inbound caller authentication: every request must carry a valid
//! hub-api-issued machine JWT (`core/service_auth`, PR #438
//! feature/eddsa-machine-jwt) naming this proxy as `aud`, scoped
//! `egress:connect`, and minted for one of the three data-plane callers
//! (`ALLOWED_CALLER_SERVICES`). Runs before the assertion or destination
//! is even parsed -- an unauthenticated caller never reaches any egress
//! logic at all.

use service_auth::{ServiceAuthError, ServiceClaims, TrustBundle};

#[derive(thiserror::Error, Debug)]
pub enum AuthError {
    #[error("missing or malformed authorization header")]
    MissingToken,
    #[error("service auth: {0}")]
    ServiceAuth(#[from] ServiceAuthError),
    #[error("caller {0:?} is not an allowed data-plane service")]
    CallerNotAllowed(String),
}

/// Extracts a `Bearer` token from `Authorization` and verifies it against
/// `trust_bundle`, then checks `sub` (a SPIFFE ID,
/// `spiffe://penguintech.io/<env>/<service>`) ends in one of
/// `allowed_caller_services` -- defense in depth beyond `aud`/`scope`
/// alone, since a leaked/misissued token scoped `egress:connect` for an
/// unexpected service should still be rejected here.
pub async fn authenticate(
    headers: &http::HeaderMap,
    trust_bundle: &dyn TrustBundle,
    audience: &str,
    trusted_issuers: &[&str],
    required_scope: &str,
    allowed_caller_services: &[String],
) -> Result<ServiceClaims, AuthError> {
    let token = headers
        .get(http::header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .ok_or(AuthError::MissingToken)?;

    let claims = service_auth::verify(
        token,
        trust_bundle,
        audience,
        trusted_issuers,
        required_scope,
    )
    .await?;

    if !allowed_caller_services
        .iter()
        .any(|svc| claims.sub.ends_with(&format!("/{svc}")))
    {
        return Err(AuthError::CallerNotAllowed(claims.sub));
    }
    Ok(claims)
}
