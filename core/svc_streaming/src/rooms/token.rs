//! Short-lived signed room tokens (ES256 JWT, ECDSA P-256). The signing key is a
//! host-side secret that never reaches a bundle; bundles only ever receive the opaque
//! token string via the host API. HS256 (a shared secret) is NOT accepted anywhere: the
//! verifier pins `alg = ES256` and the `kid` of the configured key, so `alg: none`,
//! HS256 "signed" with the public key, or a token for another key are all refused.
//!
//! The key comes from a [`SigningKeySource`]: today [`EnvKeySource`] (PKCS#8 PEM in
//! `WEBRTC_ROOM_TOKEN_SIGNING_KEY` or the file named by
//! `WEBRTC_ROOM_TOKEN_SIGNING_KEY_FILE`, key id `WEBRTC_ROOM_TOKEN_KID`, default
//! `room-1`); a `platform_secrets`-backed source plugs in behind the same trait without
//! touching the signer. The public half is derived from the private key at load, and a
//! sign/verify self-test runs before the key is accepted, so a mismatched or non-P-256
//! key fails startup loudly instead of failing every join later.

use jsonwebtoken::jwk::{AlgorithmParameters, EllipticCurve, Jwk};
use jsonwebtoken::{
    decode, decode_header, encode, Algorithm, DecodingKey, EncodingKey, Header, Validation,
};
use serde::{Deserialize, Serialize};
use thiserror::Error;

/// JWT `iss` for room tokens.
pub const ROOM_TOKEN_ISSUER: &str = "svc-streaming";
/// JWT `aud` for room tokens (distinct from the REST API audience).
pub const ROOM_TOKEN_AUDIENCE: &str = "webrtc-room";
/// Hard lower TTL bound (seconds) for an issued token.
pub const MIN_TTL_SECS: i64 = 30;
/// Hard upper TTL bound (seconds) for an issued token.
pub const MAX_TTL_SECS: i64 = 3600;
/// TTL used when the caller does not name one (the join-token default of the shared grants
/// contract, `bundles/rust/webrtc/contract/grants.json`).
pub const DEFAULT_TTL_SECS: i64 = 60;
/// Longest TTL of a JOIN token (the contract's `join_ttl_max_secs`); the gRPC `JoinRoom`
/// refuses more. Host-issued endpoint tokens may go up to [`MAX_TTL_SECS`].
pub const JOIN_TTL_MAX_SECS: i64 = 900;
/// Env var holding the PKCS#8 PEM ES256 (P-256) signing key.
pub const ENV_SIGNING_KEY: &str = "WEBRTC_ROOM_TOKEN_SIGNING_KEY";
/// Env var naming a file holding that PEM (preferred: a mounted secret, not an env value).
pub const ENV_SIGNING_KEY_FILE: &str = "WEBRTC_ROOM_TOKEN_SIGNING_KEY_FILE";
/// Env var naming the key id stamped in the token header.
pub const ENV_KID: &str = "WEBRTC_ROOM_TOKEN_KID";
/// Key id used when [`ENV_KID`] is unset.
pub const DEFAULT_KID: &str = "room-1";
/// The retired HS256 shared-secret env var; setting it is a startup error, never ignored.
pub const RETIRED_ENV_SECRET: &str = "WEBRTC_ROOM_TOKEN_SECRET";

/// Errors from token signing/verification. Messages are closed strings --
/// never token text.
#[derive(Debug, Error, PartialEq, Eq)]
pub enum TokenError {
    #[error("room token signing key is not configured")]
    NotConfigured,
    #[error("room token signing key is unusable: {0}")]
    BadKey(&'static str),
    #[error(
        "{RETIRED_ENV_SECRET} (HS256) is no longer supported; configure {ENV_SIGNING_KEY} or \
         {ENV_SIGNING_KEY_FILE} with an ES256 (P-256, PKCS#8 PEM) key"
    )]
    RetiredSecret,
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
    /// Which data plane this token is for: `room` (rooms endpoints) or `pipeline` (WHEP
    /// viewing of a pipeline). A token for one is refused by the other.
    #[serde(default = "default_scope")]
    pub scope: String,
    /// Room epoch at issue; bumping the room's epoch invalidates every older token.
    #[serde(default)]
    pub epoch: u64,
}

/// Token scope for room endpoints.
pub const SCOPE_ROOM: &str = "room";
/// Token scope for WHEP pipeline viewing.
pub const SCOPE_PIPELINE: &str = "pipeline";

fn default_scope() -> String {
    SCOPE_ROOM.to_string()
}

/// Where the room-token signing key comes from. `Ok(None)` means "no key configured" (rooms
/// disabled, readiness NOT_SERVING); a configured-but-unusable key is an `Err` (fail loud).
/// Swap the implementation (e.g. a `platform_secrets` reader) without touching the signer.
pub trait SigningKeySource: Send + Sync {
    /// Loads the active signing key.
    fn load(&self) -> Result<Option<TokenKeys>, TokenError>;
}

/// [`SigningKeySource`] over the process environment (see the module docs).
#[derive(Debug, Default, Clone, Copy)]
pub struct EnvKeySource;

impl SigningKeySource for EnvKeySource {
    fn load(&self) -> Result<Option<TokenKeys>, TokenError> {
        EnvKeySource::load_from(|k| std::env::var(k).ok())
    }
}

impl EnvKeySource {
    /// [`SigningKeySource::load`] over an arbitrary lookup (tests; no process-env mutation).
    pub fn load_from(
        get: impl Fn(&str) -> Option<String>,
    ) -> Result<Option<TokenKeys>, TokenError> {
        let get = |k: &str| get(k).filter(|v| !v.trim().is_empty());
        let pem = match (get(ENV_SIGNING_KEY), get(ENV_SIGNING_KEY_FILE)) {
            (Some(_), Some(_)) => {
                return Err(TokenError::BadKey(
                    "set only one of the signing key env var and the signing key file env var",
                ))
            }
            (Some(pem), None) => Some(pem),
            (None, Some(path)) => Some(
                std::fs::read_to_string(path.trim())
                    .map_err(|_| TokenError::BadKey("signing key file is unreadable"))?,
            ),
            (None, None) => None,
        };
        match pem {
            Some(pem) => {
                let kid = get(ENV_KID).unwrap_or_else(|| DEFAULT_KID.to_string());
                TokenKeys::from_ec_pem(&kid, &pem).map(Some)
            }
            None if get(RETIRED_ENV_SECRET).is_some() => Err(TokenError::RetiredSecret),
            None => Ok(None),
        }
    }
}

/// Signs and verifies room tokens with one ES256 key pair.
#[derive(Clone)]
pub struct TokenKeys {
    kid: String,
    encoding: EncodingKey,
    decoding: DecodingKey,
}

impl std::fmt::Debug for TokenKeys {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("TokenKeys(***redacted***)")
    }
}

impl TokenKeys {
    /// Builds keys from a PKCS#8 PEM ECDSA P-256 private key. The public half is derived from
    /// it; the key must be P-256 and pass a sign/verify self-test before it is accepted.
    pub fn from_ec_pem(kid: &str, private_pem: &str) -> Result<Self, TokenError> {
        if kid.is_empty()
            || kid.len() > 64
            || !kid
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_' || b == b'.')
        {
            return Err(TokenError::BadKey(
                "key id must be 1..=64 of [A-Za-z0-9._-]",
            ));
        }
        let encoding = EncodingKey::from_ec_pem(private_pem.as_bytes())
            .map_err(|_| TokenError::BadKey("not a PKCS#8 PEM EC private key"))?;
        let jwk = Jwk::from_encoding_key(&encoding, Algorithm::ES256)
            .map_err(|_| TokenError::BadKey("cannot derive the public key"))?;
        match &jwk.algorithm {
            AlgorithmParameters::EllipticCurve(p) if p.curve == EllipticCurve::P256 => {}
            _ => return Err(TokenError::BadKey("key must be ECDSA P-256")),
        }
        let decoding = DecodingKey::from_jwk(&jwk)
            .map_err(|_| TokenError::BadKey("cannot build the verification key"))?;
        let keys = Self {
            kid: kid.to_string(),
            encoding,
            decoding,
        };
        keys.self_test()?;
        Ok(keys)
    }

    /// Generates a fresh in-memory key pair (unit tests only; never a production source).
    #[cfg(test)]
    pub fn generate_for_tests(kid: &str) -> Self {
        let pem = rcgen::KeyPair::generate()
            .expect("generating an ECDSA P-256 key")
            .serialize_pem();
        Self::from_ec_pem(kid, &pem).expect("a generated P-256 key is valid")
    }

    /// Reads the key from the process environment ([`EnvKeySource`]); `Ok(None)` when no key
    /// is configured (rooms disabled).
    pub fn from_env() -> Result<Option<Self>, TokenError> {
        EnvKeySource.load()
    }

    /// The key id stamped in (and required of) every token.
    pub fn kid(&self) -> &str {
        &self.kid
    }

    fn self_test(&self) -> Result<(), TokenError> {
        let now = chrono::Utc::now().timestamp();
        let probe = RoomClaims {
            iss: ROOM_TOKEN_ISSUER.into(),
            aud: ROOM_TOKEN_AUDIENCE.into(),
            sub: "00000000-0000-0000-0000-000000000000".into(),
            room: "00000000-0000-0000-0000-000000000000".into(),
            community: "0".into(),
            publish: false,
            subscribe: false,
            source: None,
            jti: "00000000-0000-0000-0000-000000000000".into(),
            iat: now,
            exp: now + 60,
            scope: SCOPE_ROOM.into(),
            epoch: 0,
        };
        let token = self
            .sign(&probe)
            .map_err(|_| TokenError::BadKey("key cannot sign"))?;
        self.verify(&token)
            .map(|_| ())
            .map_err(|_| TokenError::BadKey("derived public key does not verify the private key"))
    }

    /// Signs `claims` (header `alg = ES256`, `kid`).
    pub fn sign(&self, claims: &RoomClaims) -> Result<String, TokenError> {
        let mut header = Header::new(Algorithm::ES256);
        header.kid = Some(self.kid.clone());
        encode(&header, claims, &self.encoding).map_err(|_| TokenError::Signing)
    }

    /// Verifies signature, `exp`, `iss`, `aud`; algorithm pinned to ES256 and the header `kid`
    /// must be this key's (so `none`, HS256 and foreign-key tokens never verify).
    pub fn verify(&self, token: &str) -> Result<RoomClaims, TokenError> {
        self.verify_inner(token, true)
    }

    /// [`Self::verify`] without the `exp` check: for ending an already-established session,
    /// where the token only ever gated admission. Everything else is still enforced.
    pub fn verify_allow_expired(&self, token: &str) -> Result<RoomClaims, TokenError> {
        self.verify_inner(token, false)
    }

    fn verify_inner(&self, token: &str, check_exp: bool) -> Result<RoomClaims, TokenError> {
        let header = decode_header(token).map_err(|_| TokenError::Invalid)?;
        if header.alg != Algorithm::ES256 || header.kid.as_deref() != Some(self.kid.as_str()) {
            return Err(TokenError::Invalid);
        }
        let mut v = Validation::new(Algorithm::ES256);
        v.leeway = 0;
        v.validate_exp = check_exp;
        v.set_issuer(&[ROOM_TOKEN_ISSUER]);
        v.set_audience(&[ROOM_TOKEN_AUDIENCE]);
        v.set_required_spec_claims(&["exp", "iss", "aud", "sub"]);
        decode::<RoomClaims>(token, &self.decoding, &v)
            .map(|d| d.claims)
            .map_err(|_| TokenError::Invalid)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use base64::engine::general_purpose::URL_SAFE_NO_PAD;
    use base64::Engine as _;

    fn keys() -> TokenKeys {
        TokenKeys::generate_for_tests("room-test")
    }

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
            scope: SCOPE_ROOM.into(),
            epoch: 0,
        }
    }

    #[test]
    fn round_trip_is_es256_with_the_kid_in_the_header() {
        let k = keys();
        let c = claims(60);
        let token = k.sign(&c).unwrap();
        let header: serde_json::Value = serde_json::from_slice(
            &URL_SAFE_NO_PAD
                .decode(token.split('.').next().unwrap())
                .unwrap(),
        )
        .unwrap();
        assert_eq!(header["alg"], "ES256");
        assert_eq!(header["kid"], "room-test");
        assert_eq!(k.verify(&token).unwrap(), c);
    }

    #[test]
    fn expired_foreign_key_wrong_aud_and_garbage_are_rejected() {
        let k = keys();
        assert_eq!(
            k.verify(&k.sign(&claims(-5)).unwrap()),
            Err(TokenError::Invalid)
        );
        // Same kid, different key pair: signature does not verify.
        let imposter = TokenKeys::generate_for_tests("room-test");
        assert_eq!(
            k.verify(&imposter.sign(&claims(60)).unwrap()),
            Err(TokenError::Invalid)
        );
        // Right key pair, other kid: refused (a token must name the configured key).
        let other_kid = TokenKeys {
            kid: "other".into(),
            ..k.clone()
        };
        assert_eq!(
            k.verify(&other_kid.sign(&claims(60)).unwrap()),
            Err(TokenError::Invalid)
        );
        let mut c = claims(60);
        c.aud = "svc-streaming".into();
        assert_eq!(k.verify(&k.sign(&c).unwrap()), Err(TokenError::Invalid));
        let mut c = claims(60);
        c.iss = "evil".into();
        assert_eq!(k.verify(&k.sign(&c).unwrap()), Err(TokenError::Invalid));
        assert_eq!(k.verify("garbage"), Err(TokenError::Invalid));
    }

    #[test]
    fn hs256_none_and_other_algorithms_never_verify() {
        let k = keys();
        let c = claims(60);
        // HS256 with a shared secret, with and without the right kid.
        for kid in [Some("room-test".to_string()), None] {
            let mut h = Header::new(Algorithm::HS256);
            h.kid = kid;
            let t = encode(
                &h,
                &c,
                &EncodingKey::from_secret(b"0123456789abcdef0123456789abcdef"),
            )
            .unwrap();
            assert_eq!(k.verify(&t), Err(TokenError::Invalid), "HS256");
        }
        // Hand-built `alg: none` (any case) with an empty or bogus signature segment, and
        // algorithms we never issue.
        let b64 = |v: serde_json::Value| URL_SAFE_NO_PAD.encode(serde_json::to_vec(&v).unwrap());
        let payload = b64(serde_json::to_value(&c).unwrap());
        for (alg, sig) in [
            ("none", ""),
            ("None", ""),
            ("none", "AAAA"),
            ("RS256", "AAAA"),
            ("ES384", "AAAA"),
            ("EdDSA", "AAAA"),
        ] {
            let t = format!(
                "{}.{payload}.{sig}",
                b64(serde_json::json!({"alg": alg, "kid": "room-test", "typ": "JWT"}))
            );
            assert_eq!(k.verify(&t), Err(TokenError::Invalid), "alg {alg}");
        }
        // A token with no kid cannot pick its key.
        let t = encode(&Header::new(Algorithm::ES256), &c, &k.encoding).unwrap();
        assert_eq!(k.verify(&t), Err(TokenError::Invalid));
    }

    fn pem_of(kp: &rcgen::KeyPair) -> String {
        kp.serialize_pem()
    }

    #[test]
    fn key_loading_validates_curve_format_and_kid() {
        let good = rcgen::KeyPair::generate().unwrap();
        assert!(TokenKeys::from_ec_pem("k1", &pem_of(&good)).is_ok());
        // Not a key at all, a public key, and a P-384 key are all refused.
        assert!(matches!(
            TokenKeys::from_ec_pem("k1", "nope"),
            Err(TokenError::BadKey(_))
        ));
        assert!(matches!(
            TokenKeys::from_ec_pem("k1", &good.public_key_pem()),
            Err(TokenError::BadKey(_))
        ));
        let p384 = rcgen::KeyPair::generate_for(&rcgen::PKCS_ECDSA_P384_SHA384).unwrap();
        assert!(matches!(
            TokenKeys::from_ec_pem("k1", &pem_of(&p384)),
            Err(TokenError::BadKey(_))
        ));
        for kid in ["", "has space", "x".repeat(65).as_str()] {
            assert!(matches!(
                TokenKeys::from_ec_pem(kid, &pem_of(&good)),
                Err(TokenError::BadKey(_))
            ));
        }
    }

    #[test]
    fn env_source_reads_inline_or_file_and_refuses_the_retired_secret() {
        let kp = rcgen::KeyPair::generate().unwrap();
        let pem = pem_of(&kp);
        let env = |pairs: Vec<(&'static str, String)>| {
            EnvKeySource::load_from(move |k| {
                pairs.iter().find(|(n, _)| *n == k).map(|(_, v)| v.clone())
            })
        };
        // Nothing configured: rooms disabled, not an error.
        assert!(env(vec![]).unwrap().is_none());
        // Inline PEM with and without a kid.
        let k = env(vec![(ENV_SIGNING_KEY, pem.clone())]).unwrap().unwrap();
        assert_eq!(k.kid(), DEFAULT_KID);
        let k = env(vec![
            (ENV_SIGNING_KEY, pem.clone()),
            (ENV_KID, "rot-2".into()),
        ])
        .unwrap()
        .unwrap();
        assert_eq!(k.kid(), "rot-2");
        // File form.
        let dir = std::env::temp_dir().join(format!("room-key-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("key.pem");
        std::fs::write(&path, &pem).unwrap();
        let k = env(vec![(ENV_SIGNING_KEY_FILE, path.display().to_string())])
            .unwrap()
            .unwrap();
        assert!(k.verify(&k.sign(&claims(60)).unwrap()).is_ok());
        assert_eq!(
            env(vec![(
                ENV_SIGNING_KEY_FILE,
                dir.join("missing.pem").display().to_string()
            )])
            .unwrap_err(),
            TokenError::BadKey("signing key file is unreadable")
        );
        let _ = std::fs::remove_dir_all(&dir);
        // Both forms at once is ambiguous: refused.
        assert!(env(vec![
            (ENV_SIGNING_KEY, pem.clone()),
            (ENV_SIGNING_KEY_FILE, "/nonexistent".into())
        ])
        .is_err());
        // The retired HS256 secret alone is a loud startup error, never silently ignored...
        assert_eq!(
            env(vec![(
                RETIRED_ENV_SECRET,
                "0123456789abcdef0123456789abcdef".into()
            )])
            .unwrap_err(),
            TokenError::RetiredSecret
        );
        // ...and never accepted as a key.
        assert!(env(vec![(
            ENV_SIGNING_KEY,
            "0123456789abcdef0123456789abcdef".into()
        )])
        .is_err());
    }

    #[test]
    fn ttl_constants_match_the_shared_grants_contract() {
        assert_eq!((MIN_TTL_SECS, MAX_TTL_SECS), (30, 3600));
        assert_eq!(DEFAULT_TTL_SECS, 60);
        assert_eq!(JOIN_TTL_MAX_SECS, 900);
        assert!((MIN_TTL_SECS..=JOIN_TTL_MAX_SECS).contains(&DEFAULT_TTL_SECS));
    }
}
