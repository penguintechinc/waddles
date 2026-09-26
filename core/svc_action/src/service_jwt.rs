//! Mints the short-lived HS256 service JWT `crate::distribution::fetch_bundles`
//! presents to hub-api's `GET /api/v1/distribution/bundles?stage=action`
//! poll.
//!
//! **This reuses the existing platform-wide machine-JWT scheme verbatim --
//! it does not invent a new one.** Every flask_core-based Python
//! stage-runner (`core/svc_process`/`core/svc_ingest`'s own `app.py::
//! _jwt_provider()`) already mints a token this exact same shape via
//! `libs/flask_core/flask_core/auth.py::create_jwt_token`, signed with the
//! shared HS256 `SECRET_KEY`, and hub-api's distribution endpoint
//! (`hub_api/blueprints/v1/distribution.py`) verifies it via
//! `flask_core.tenancy.tenant_middleware` + `flask_core.authz.require_scope`
//! -- both of which call `flask_core.auth.verify_jwt_token(token,
//! require_secret_key())` under the hood. Bug root cause: `core/svc_action`'s
//! Rust `fetch_bundles` sent no `Authorization` header at all, so every poll
//! 401'd and the bundle catalog never populated.
//!
//! Claim shape mirrors `create_jwt_token`'s payload closely enough for
//! `verify_jwt_token` to accept it: mandatory `sub`/`iat`/`exp` (checked
//! explicitly), `tenant` (security.md Tenant Isolation), `scope`
//! (`require_scope`'s claim), plus `iss`/`aud` (rejected only if
//! present-and-mismatched) and `teams`/`roles` (shape-parity with every
//! other caller; unused by this particular route). A fresh token is minted
//! on every poll tick rather than cached across ticks -- identical to
//! `_jwt_provider()`'s own call-every-time behavior, and correct-by-
//! construction: `crate::distribution::fetch_bundles` never risks
//! presenting an expired token, no separate refresh/expiry-tracking logic
//! needed, and the ~5s poll cadence (`POLL_INTERVAL_S`) sits nowhere near
//! the 1h lifetime (security.md JWT Claims: 1h default ceiling).

use jsonwebtoken::{encode, EncodingKey, Header};
use serde::{Deserialize, Serialize};
use thiserror::Error;

use crate::config::Secret;

/// The exact scope hub-api's distribution endpoint requires
/// (`hub_api/blueprints/v1/distribution.py`'s
/// `@require_scope("distribution:read")`, `core/svc_process/config.py`'s
/// `JWT_SCOPE` constant of the same value).
pub const DISTRIBUTION_READ_SCOPE: &str = "distribution:read";

/// `sub`/`roles` identify this pod as a machine caller, never a human user
/// -- `roles` is audit/display only (security.md: authz decisions are made
/// on `scope`, never `roles`), mirroring `_jwt_provider()`'s own
/// `user_id="svc-process", roles=["service"]` call shape.
const SERVICE_SUBJECT: &str = "svc-action";
const SERVICE_ROLE: &str = "service";

/// Service JWTs are minted fresh per poll tick with this fixed lifetime --
/// security.md JWT Claims' 1h default ceiling, matching
/// `_jwt_provider()`'s own `expiration_hours=1`.
const TOKEN_LIFETIME_SECS: i64 = 3600;

/// Errors minting the service JWT.
#[derive(Debug, Error)]
pub enum ServiceJwtError {
    #[error("failed to encode service jwt: {0}")]
    Encode(#[from] jsonwebtoken::errors::Error),
    /// The system clock reports a time before the Unix epoch -- treated as
    /// an error rather than saturating to `0`, since a wildly wrong clock
    /// would otherwise silently mint a token with a nonsensical `iat`/`exp`.
    #[error("system clock is before the Unix epoch")]
    ClockBeforeEpoch,
}

/// Everything [`ServiceJwtConfig::mint`] needs, bundled so
/// `crate::distribution::PollLoopConfig` carries one field instead of four
/// (mirroring that struct's own `LoadLimits` bundling). `Debug`-derivable
/// safely: [`Secret`]'s own `Debug` impl redacts `secret`.
#[derive(Clone, Debug)]
pub struct ServiceJwtConfig {
    /// Shared HS256 signing secret -- `SECRET_KEY` env var
    /// (`crate::config::Config::secret_key`), the exact same secret every
    /// flask_core-based Python service (hub-api included) verifies incoming
    /// bearer tokens against.
    pub secret: Secret,
    /// `iss` claim -- `crate::config::CliConfig::jwt_issuer`.
    pub issuer: String,
    /// `aud` claim -- `crate::config::CliConfig::jwt_audience`.
    pub audience: String,
    /// `tenant` claim -- `crate::config::CliConfig::runner_tenant_slug`.
    pub tenant: String,
}

/// Wire shape matching `libs/flask_core/flask_core/auth.py::create_jwt_token`'s
/// payload closely enough for `verify_jwt_token` to accept it -- see the
/// module doc for exactly which claims that function actually enforces.
/// `Deserialize` is only needed by this module's own round-trip tests
/// below (a real caller of this token is hub-api, not this crate).
#[derive(Serialize, Deserialize)]
struct Claims {
    sub: String,
    iss: String,
    aud: String,
    iat: i64,
    exp: i64,
    scope: String,
    tenant: String,
    teams: Vec<String>,
    roles: Vec<String>,
}

impl ServiceJwtConfig {
    /// Mints a fresh service JWT carrying [`DISTRIBUTION_READ_SCOPE`],
    /// scoped to `self.tenant`, valid for [`TOKEN_LIFETIME_SECS`] from now.
    pub fn mint(&self) -> Result<String, ServiceJwtError> {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_err(|_| ServiceJwtError::ClockBeforeEpoch)?
            .as_secs() as i64;
        let claims = Claims {
            sub: SERVICE_SUBJECT.to_string(),
            iss: self.issuer.clone(),
            aud: self.audience.clone(),
            iat: now,
            exp: now + TOKEN_LIFETIME_SECS,
            scope: DISTRIBUTION_READ_SCOPE.to_string(),
            tenant: self.tenant.clone(),
            teams: Vec::new(),
            roles: vec![SERVICE_ROLE.to_string()],
        };
        let key = EncodingKey::from_secret(self.secret.expose().as_bytes());
        let token = encode(&Header::default(), &claims, &key)?;
        Ok(token)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use jsonwebtoken::{decode, Algorithm, DecodingKey, Validation};

    fn sample_config() -> ServiceJwtConfig {
        ServiceJwtConfig {
            secret: Secret::new("test-signing-secret"),
            issuer: "waddlebot".to_string(),
            audience: "waddlebot-services".to_string(),
            tenant: "global".to_string(),
        }
    }

    fn decode_with_secret(token: &str, secret: &str) -> jsonwebtoken::TokenData<Claims> {
        let mut validation = Validation::new(Algorithm::HS256);
        validation.set_issuer(&["waddlebot"]);
        validation.set_audience(&["waddlebot-services"]);
        decode::<Claims>(
            token,
            &DecodingKey::from_secret(secret.as_bytes()),
            &validation,
        )
        .expect("token decodes and verifies")
    }

    #[test]
    fn mint_produces_a_token_carrying_the_distribution_read_scope_and_tenant() {
        let config = sample_config();
        let token = config.mint().expect("mint succeeds");
        let data = decode_with_secret(&token, "test-signing-secret");
        assert_eq!(data.claims.sub, "svc-action");
        assert_eq!(data.claims.scope, "distribution:read");
        assert_eq!(data.claims.tenant, "global");
        assert_eq!(data.claims.iss, "waddlebot");
        assert_eq!(data.claims.aud, "waddlebot-services");
        assert_eq!(data.claims.roles, vec!["service".to_string()]);
        assert!(data.claims.teams.is_empty());
    }

    #[test]
    fn mint_sets_a_one_hour_expiry() {
        let config = sample_config();
        let token = config.mint().expect("mint succeeds");
        let data = decode_with_secret(&token, "test-signing-secret");
        assert_eq!(data.claims.exp - data.claims.iat, 3600);
    }

    #[test]
    fn mint_honors_a_custom_tenant() {
        let mut config = sample_config();
        config.tenant = "acme".to_string();
        let token = config.mint().expect("mint succeeds");
        let data = decode_with_secret(&token, "test-signing-secret");
        assert_eq!(data.claims.tenant, "acme");
    }

    /// A token minted with one secret must not verify against a different
    /// one -- proves this isn't accidentally unsigned/alg=none or verifying
    /// against a hardcoded key.
    #[test]
    fn mint_fails_to_verify_against_the_wrong_secret() {
        let config = sample_config();
        let token = config.mint().expect("mint succeeds");
        let validation = Validation::new(Algorithm::HS256);
        let result = decode::<serde_json::Value>(
            &token,
            &DecodingKey::from_secret(b"wrong-secret"),
            &validation,
        );
        assert!(result.is_err());
    }

    #[test]
    fn service_jwt_config_debug_never_prints_the_secret() {
        let config = sample_config();
        let rendered = format!("{config:?}");
        assert!(!rendered.contains("test-signing-secret"));
        assert!(rendered.contains("redacted"));
    }
}
