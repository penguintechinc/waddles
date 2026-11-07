//! Short-lived signed room join tokens (HS256 JWT). The signing secret is a
//! host-side secret (`WEBRTC_ROOM_TOKEN_SECRET`) that never reaches a bundle;
//! bundles only ever receive the opaque token string via the host API.

use jsonwebtoken::{decode, encode, Algorithm, DecodingKey, EncodingKey, Header, Validation};
use serde::{Deserialize, Serialize};
use thiserror::Error;

/// JWT `iss` for room tokens.
pub const ROOM_TOKEN_ISSUER: &str = "svc-streaming";
/// JWT `aud` for room tokens (distinct from the REST API audience).
pub const ROOM_TOKEN_AUDIENCE: &str = "webrtc-room";
/// Minimum accepted secret length in bytes.
pub const MIN_SECRET_LEN: usize = 32;
/// Hard lower TTL bound (seconds) for an issued token.
pub const MIN_TTL_SECS: i64 = 30;
/// Hard upper TTL bound (seconds) for an issued token.
pub const MAX_TTL_SECS: i64 = 3600;
/// TTL used when the caller does not name one.
pub const DEFAULT_TTL_SECS: i64 = 900;

/// Errors from token signing/verification. Messages are closed strings --
/// never token text.
#[derive(Debug, Error, PartialEq, Eq)]
pub enum TokenError {
    #[error("room token secret is not configured")]
    NotConfigured,
    #[error("room token secret must be at least {MIN_SECRET_LEN} bytes")]
    WeakSecret,
    #[error("token invalid or expired")]
    Invalid,
    #[error("token signing failed")]
    Signing,
}

/// Claims carried by a room join token. Room- and community-scoped: the data
/// plane re-checks both against the live room, never trusting the token alone.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RoomClaims {
    pub iss: String,
    pub aud: String,
    /// Participant id (opaque UUID, never PII).
    pub sub: String,
    /// Room UUID this token is scoped to.
    pub room: String,
    /// Numeric community id the room belongs to (decimal string).
    pub community: String,
    pub publish: bool,
    pub subscribe: bool,
    /// When set, a subscribe token is restricted to this publisher.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source: Option<String>,
    /// Token id, the revocation handle.
    pub jti: String,
    pub iat: i64,
    pub exp: i64,
}

/// Signs and verifies room tokens with the host-side secret.
#[derive(Clone)]
pub struct TokenKeys {
    secret: Vec<u8>,
}

impl std::fmt::Debug for TokenKeys {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("TokenKeys(***redacted***)")
    }
}

impl TokenKeys {
    /// Builds keys from a raw secret, enforcing a minimum length.
    pub fn new(secret: &str) -> Result<Self, TokenError> {
        if secret.len() < MIN_SECRET_LEN {
            return Err(TokenError::WeakSecret);
        }
        Ok(Self {
            secret: secret.as_bytes().to_vec(),
        })
    }

    /// Reads `WEBRTC_ROOM_TOKEN_SECRET`; `Ok(None)` when unset (rooms disabled).
    pub fn from_env() -> Result<Option<Self>, TokenError> {
        match std::env::var("WEBRTC_ROOM_TOKEN_SECRET") {
            Ok(v) => Self::new(&v).map(Some),
            Err(_) => Ok(None),
        }
    }

    /// Signs `claims`.
    pub fn sign(&self, claims: &RoomClaims) -> Result<String, TokenError> {
        encode(
            &Header::new(Algorithm::HS256),
            claims,
            &EncodingKey::from_secret(&self.secret),
        )
        .map_err(|_| TokenError::Signing)
    }

    /// Verifies signature, `exp`, `iss`, `aud`; algorithm pinned to HS256.
    pub fn verify(&self, token: &str) -> Result<RoomClaims, TokenError> {
        let mut v = Validation::new(Algorithm::HS256);
        v.leeway = 0;
        v.set_issuer(&[ROOM_TOKEN_ISSUER]);
        v.set_audience(&[ROOM_TOKEN_AUDIENCE]);
        v.set_required_spec_claims(&["exp", "iss", "aud", "sub"]);
        decode::<RoomClaims>(token, &DecodingKey::from_secret(&self.secret), &v)
            .map(|d| d.claims)
            .map_err(|_| TokenError::Invalid)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SECRET: &str = "0123456789abcdef0123456789abcdef";

    fn claims(exp_off: i64) -> RoomClaims {
        let now = chrono::Utc::now().timestamp();
        RoomClaims {
            iss: ROOM_TOKEN_ISSUER.into(),
            aud: ROOM_TOKEN_AUDIENCE.into(),
            sub: uuid::Uuid::new_v4().to_string(),
            room: uuid::Uuid::new_v4().to_string(),
            community: "7".into(),
            publish: true,
            subscribe: true,
            source: None,
            jti: uuid::Uuid::new_v4().to_string(),
            iat: now,
            exp: now + exp_off,
        }
    }

    #[test]
    fn round_trip() {
        let k = TokenKeys::new(SECRET).unwrap();
        let c = claims(60);
        assert_eq!(k.verify(&k.sign(&c).unwrap()).unwrap(), c);
    }

    #[test]
    fn expired_wrong_key_and_wrong_aud_are_rejected() {
        let k = TokenKeys::new(SECRET).unwrap();
        assert_eq!(
            k.verify(&k.sign(&claims(-5)).unwrap()),
            Err(TokenError::Invalid)
        );
        let other = TokenKeys::new("ffffffffffffffffffffffffffffffff").unwrap();
        assert_eq!(
            k.verify(&other.sign(&claims(60)).unwrap()),
            Err(TokenError::Invalid)
        );
        let mut c = claims(60);
        c.aud = "svc-streaming".into();
        assert_eq!(k.verify(&k.sign(&c).unwrap()), Err(TokenError::Invalid));
        assert_eq!(k.verify("garbage"), Err(TokenError::Invalid));
    }

    #[test]
    fn weak_secret_rejected() {
        assert_eq!(TokenKeys::new("short").unwrap_err(), TokenError::WeakSecret);
    }
}
