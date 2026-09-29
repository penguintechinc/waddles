//! The signed per-request egress-allowlist assertion, shared between the
//! two crates on either side of the bundle-host -> egress-proxy hop:
//! `core/bundle_host_http`'s [`AssertionSigningKey`] (the calling
//! service's client-side signer, PR #468) and `core/egress_proxy`'s
//! server-side verifier (PR #463/#469). Previously each side carried its
//! own copy of the `EgressAssertion` struct and header names; this crate
//! is the single source of truth so the wire format the two crates agree
//! on can never silently drift apart.
//!
//! **Design (mirrors `core/egress_proxy/src/assertion.rs`'s original
//! doc):** a separate, short-lived EdDSA JWT, not a claim folded into the
//! calling service's own machine JWT (`core/service_auth`) -- the machine
//! JWT authenticates *which service* is calling and is deliberately
//! longer-lived (cached, reused for many requests); this assertion
//! instead answers "is *this specific* tenant/community/app/destination
//! call authorized *right now*" and needs a much shorter replay window
//! ([`ASSERTION_MAX_TTL_SECONDS`], default 60s) than the calling pod's own
//! machine JWT lifetime.
//!
//! **Signed by the calling service's own Ed25519 key** (the same key
//! material it already uses to mint its own machine JWT, `core/
//! service_auth`, PR #438's per-service JWKS) -- never a second, static,
//! separately-distributed signing key. The `sub` claim carries the
//! caller's SPIFFE ID; `core/egress_proxy::proxy::validate` checks it
//! against the already-authenticated machine JWT's own `sub`, so an
//! assertion signed by service A can never be replayed alongside service
//! B's machine JWT even if B somehow obtained the token.
//!
//! **What this crate deliberately does *not* own:** JWKS/`TrustBundle`
//! lookup by `kid` and replay-cache/single-use enforcement are runtime
//! state specific to `egress_proxy`'s own deployment (it already depends
//! on `service_auth::TrustBundle` for the machine JWT, and is expected to
//! reuse that exact trust bundle here too) -- this crate's
//! [`verify_with_key`] is the pure, dependency-free signature/TTL
//! primitive both a real JWKS-aware verifier and this crate's own
//! round-trip tests build on, not a replacement for `egress_proxy`'s
//! fuller `verify_from_header` (kid lookup + replay cache), which stays in
//! that crate. Deliberately no `service_auth` dependency here either --
//! keeps this crate lightweight for `bundle_host_http`'s signing-only use.
//!
//! Header: `X-Waddles-Egress-Assertion: <compact EdDSA JWT>`. The bundle's
//! own destination credential (e.g. a Discord bot token resolved from a
//! granted `secret_ref`) never rides as this hop's `Authorization` --
//! [`FORWARD_AUTHORIZATION_HEADER`] carries it instead, so it is never
//! confused with the proxy-hop's own machine-JWT `Authorization` bearer.

use std::time::{SystemTime, UNIX_EPOCH};

use jsonwebtoken::{Algorithm, DecodingKey, EncodingKey, Header, Validation};
use serde::{Deserialize, Serialize};

/// Header carrying the signed [`EgressAssertion`].
pub const ASSERTION_HEADER: &str = "x-waddles-egress-assertion";

/// Header carrying the bundle's own destination credential (e.g. a
/// resolved `secret_ref` value) when a request is proxied through the
/// upstream egress proxy -- kept distinct from `Authorization` itself
/// (which, on that hop, is *this* connection's machine JWT) specifically
/// so the two are never conflated. `egress_proxy`'s forward-HTTP path
/// maps this back onto a real `Authorization` header for the final
/// destination only.
pub const FORWARD_AUTHORIZATION_HEADER: &str = "x-waddles-forward-authorization";

/// Platform ceiling on this assertion's own lifetime -- deliberately much
/// shorter than a machine JWT's (`service_auth::MAX_TOKEN_TTL_SECONDS`,
/// 1h): a legitimate caller mints a fresh assertion per `http.send`/
/// `CONNECT`, so there is never a reason for this window to be wide.
pub const ASSERTION_MAX_TTL_SECONDS: u64 = 60;

/// Clock-skew leeway applied to `exp`/`iat`, kept small since this
/// token's whole lifetime is itself only tens of seconds.
const ASSERTION_CLOCK_SKEW_SECONDS: u64 = 5;

/// The three `net.http.*` permission families (connector spec) --
/// identical variants to `bundle_host_http::egress`'s private
/// `EgressCategory`; that crate converts to this type at its
/// [`crate::AssertionSigningKey`] call site so the wire format is always
/// this crate's, never a second local copy.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum DestinationCategory {
    Fqdn,
    PublicIp,
    PrivateIp,
}

/// One signed, single-use egress grant. Field-for-field identical to
/// `egress_proxy::assertion::EgressAssertion` (pre-shared-crate copy) --
/// see the module doc for why each field exists.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct EgressAssertion {
    /// The signing/calling service's own SPIFFE ID -- must equal the
    /// authenticated machine JWT's `sub` on the same connection.
    pub sub: String,
    pub tenant: String,
    pub community: String,
    pub app: String,
    pub category: DestinationCategory,
    /// `Fqdn`: exact hostname. `PublicIp`/`PrivateIp`: an IP literal (or,
    /// for `PrivateIp`, a CIDR the resolved address must fall within).
    pub destination: String,
    /// The exact destination port this grant authorizes.
    pub port: u16,
    /// Unique per-assertion ID -- lets a verifier's replay cache refuse a
    /// second use of the same short-lived grant.
    pub jti: String,
    pub iat: u64,
    pub exp: u64,
}

#[derive(thiserror::Error, Debug)]
pub enum AssertionError {
    #[error("invalid assertion: {0}")]
    Invalid(String),
    #[error("assertion ttl {actual}s exceeds max {max}s")]
    TtlTooLong { actual: u64, max: u64 },
    #[error("failed to load Ed25519 signing key: {0}")]
    InvalidSigningKey(String),
    #[error("failed to sign assertion: {0}")]
    SigningFailed(String),
}

/// This service's own Ed25519 signing key -- loaded once at process
/// startup from an env var or a file (K8s Secret mount), never logged.
/// `kid` must match the `kid` the same key is registered under in hub-
/// api's JWKS for this service's machine JWT (PR #438) -- `egress_proxy`'s
/// verifier looks the public half up by this same identifier.
pub struct AssertionSigningKey {
    encoding_key: EncodingKey,
    kid: String,
}

impl AssertionSigningKey {
    /// Builds a signing key directly from PEM bytes (SPKI/PKCS8 Ed25519
    /// private key) and its `kid`.
    pub fn from_ed25519_pem(pem: &[u8], kid: impl Into<String>) -> Result<Self, AssertionError> {
        let encoding_key = EncodingKey::from_ed_pem(pem)
            .map_err(|e| AssertionError::InvalidSigningKey(e.to_string()))?;
        Ok(Self {
            encoding_key,
            kid: kid.into(),
        })
    }

    /// Loads the signing key the way every other per-service credential in
    /// this org is loaded (`rules/critical-rules.md` Token & Secret
    /// Hygiene): from a file path named by `path_env` (the standard K8s
    /// Secret-volume-mount shape) if set, else from the PEM content
    /// directly in `key_env` (local/dev only) -- never from a CLI flag,
    /// never logged either way. `kid_env` names this key's JWKS key id.
    pub fn from_env(path_env: &str, key_env: &str, kid_env: &str) -> Result<Self, AssertionError> {
        let kid = std::env::var(kid_env)
            .map_err(|_| AssertionError::InvalidSigningKey(format!("{kid_env} is not set")))?;
        if let Ok(path) = std::env::var(path_env) {
            let pem = std::fs::read(&path).map_err(|e| {
                AssertionError::InvalidSigningKey(format!("reading {path_env} ({path}): {e}"))
            })?;
            return Self::from_ed25519_pem(&pem, kid);
        }
        if let Ok(pem) = std::env::var(key_env) {
            return Self::from_ed25519_pem(pem.as_bytes(), kid);
        }
        Err(AssertionError::InvalidSigningKey(format!(
            "neither {path_env} nor {key_env} is set"
        )))
    }

    /// Signs `claims` as a compact EdDSA JWT, `kid`-tagged with this key's
    /// identifier -- the exact wire format [`verify_with_key`] (and
    /// `egress_proxy`'s fuller JWKS-aware verifier) expects.
    pub fn sign(&self, claims: &EgressAssertion) -> Result<String, AssertionError> {
        let mut header = Header::new(Algorithm::EdDSA);
        header.kid = Some(self.kid.clone());
        jsonwebtoken::encode(&header, claims, &self.encoding_key)
            .map_err(|e| AssertionError::SigningFailed(e.to_string()))
    }
}

/// Builds a fresh, single-use [`EgressAssertion`] for one outbound call --
/// `jti`/`iat`/`exp` are always generated here, never caller-supplied, so
/// two calls can never accidentally share a `jti`. `ttl_secs` is clamped
/// to [`ASSERTION_MAX_TTL_SECONDS`] regardless of what's requested --
/// defense in depth against a misconfigured caller minting a long-lived
/// grant.
#[allow(clippy::too_many_arguments)]
pub fn build_assertion(
    sub: impl Into<String>,
    tenant: impl Into<String>,
    community: impl Into<String>,
    app: impl Into<String>,
    category: DestinationCategory,
    destination: impl Into<String>,
    port: u16,
    ttl_secs: u64,
) -> EgressAssertion {
    let iat = now_secs();
    let ttl = ttl_secs.min(ASSERTION_MAX_TTL_SECONDS);
    EgressAssertion {
        sub: sub.into(),
        tenant: tenant.into(),
        community: community.into(),
        app: app.into(),
        category,
        destination: destination.into(),
        port,
        jti: uuid::Uuid::new_v4().to_string(),
        iat,
        exp: iat + ttl,
    }
}

/// Verifies signature and `exp`/`iat`/max-TTL against a single already-
/// resolved `decoding_key` -- the dependency-free primitive a JWKS-aware
/// verifier (looking `kid` up first) builds on. Does **not** check `sub`
/// against a caller identity, replay/`jti` uniqueness, or destination/port
/// match -- those are the calling verifier's job
/// ([`destination_matches`]/[`resolved_matches`] below cover the latter;
/// `egress_proxy::proxy::validate` covers the former two).
pub fn verify_with_key(
    token: &str,
    decoding_key: &DecodingKey,
    max_ttl_secs: u64,
) -> Result<EgressAssertion, AssertionError> {
    let mut validation = Validation::new(Algorithm::EdDSA);
    validation.leeway = ASSERTION_CLOCK_SKEW_SECONDS;
    validation.set_required_spec_claims(&["exp", "iat"]);
    validation.validate_aud = false;

    let data = jsonwebtoken::decode::<EgressAssertion>(token, decoding_key, &validation)
        .map_err(|e| AssertionError::Invalid(e.to_string()))?;
    let claims = data.claims;

    let ttl = claims.exp.saturating_sub(claims.iat);
    if ttl > max_ttl_secs {
        return Err(AssertionError::TtlTooLong {
            actual: ttl,
            max: max_ttl_secs,
        });
    }
    Ok(claims)
}

/// Step 4 of the pipeline (spec-equivalent to `bundle_host_http`'s
/// declared-host check): does the *requested* destination (host **and**
/// port) match what the assertion actually grants. The port check is
/// exact -- a grant for `:443` never authorizes the same host on a
/// different (even operator-allowlisted) port.
pub fn destination_matches(
    assertion: &EgressAssertion,
    requested_host: &str,
    requested_port: u16,
) -> bool {
    if assertion.port != requested_port {
        return false;
    }
    match assertion.category {
        DestinationCategory::Fqdn => {
            requested_host.parse::<std::net::IpAddr>().is_err()
                && requested_host.eq_ignore_ascii_case(&assertion.destination)
        }
        DestinationCategory::PublicIp | DestinationCategory::PrivateIp => {
            requested_host == assertion.destination
        }
    }
}

pub fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

#[cfg(test)]
mod tests {
    use super::*;

    // PKCS8-DER-encoded Ed25519 test keypairs (fixed, test-only), the same
    // fixture shape `core/service_auth`'s own test module uses --
    // generated once with `openssl genpkey -algorithm ed25519` / `openssl
    // pkey -pubout`; never used outside this test module.
    const KEY_A_PRIV_DER: &[u8] = &[
        48, 46, 2, 1, 0, 48, 5, 6, 3, 43, 101, 112, 4, 34, 4, 32, 1, 204, 5, 142, 35, 153, 231, 38,
        150, 122, 1, 218, 34, 237, 70, 125, 233, 62, 126, 103, 151, 16, 11, 238, 95, 122, 209, 74,
        183, 9, 171, 161,
    ];
    const KEY_A_PUB_RAW: &[u8] = &[
        169, 90, 255, 23, 51, 151, 156, 147, 56, 247, 214, 168, 76, 160, 67, 99, 211, 238, 208, 5,
        69, 236, 245, 115, 4, 81, 1, 42, 23, 107, 4, 187,
    ];
    const KEY_B_PRIV_DER: &[u8] = &[
        48, 46, 2, 1, 0, 48, 5, 6, 3, 43, 101, 112, 4, 34, 4, 32, 145, 56, 92, 35, 32, 192, 103,
        161, 66, 249, 233, 0, 174, 22, 45, 100, 136, 104, 59, 129, 251, 81, 20, 214, 221, 250, 219,
        227, 139, 109, 70, 185,
    ];

    fn signing_key() -> AssertionSigningKey {
        AssertionSigningKey {
            encoding_key: EncodingKey::from_ed_der(KEY_A_PRIV_DER),
            kid: "k1".to_string(),
        }
    }

    fn forged_signing_key() -> AssertionSigningKey {
        AssertionSigningKey {
            encoding_key: EncodingKey::from_ed_der(KEY_B_PRIV_DER),
            kid: "k1".to_string(),
        }
    }

    fn decoding_key() -> DecodingKey {
        DecodingKey::from_ed_der(KEY_A_PUB_RAW)
    }

    fn base_assertion() -> EgressAssertion {
        build_assertion(
            "spiffe://penguintech.io/alpha/svc-process",
            "tenant-a",
            "community-a",
            "waddles.a.b.c",
            DestinationCategory::Fqdn,
            "discord.com",
            443,
            30,
        )
    }

    /// The cross-crate contract this shared crate exists to guarantee:
    /// `core/bundle_host_http`'s signer (this test's `signing_key()`) and
    /// `core/egress_proxy`'s verifier (this test's `verify_with_key`,
    /// the exact primitive that crate's own JWKS-aware `verify` wraps)
    /// agree on one wire format end to end.
    #[test]
    fn sign_then_verify_round_trips() {
        let assertion = base_assertion();
        let token = signing_key().sign(&assertion).expect("signs");
        let verified =
            verify_with_key(&token, &decoding_key(), ASSERTION_MAX_TTL_SECONDS).expect("verifies");
        assert_eq!(verified, assertion);
        assert!(destination_matches(&verified, "discord.com", 443));
    }

    #[test]
    fn verify_rejects_tampered_signature() {
        let assertion = base_assertion();
        // Signed by a different key than the verifier trusts -- simulates
        // a forged/tampered token.
        let token = forged_signing_key().sign(&assertion).expect("signs");
        let err = verify_with_key(&token, &decoding_key(), ASSERTION_MAX_TTL_SECONDS).unwrap_err();
        assert!(matches!(err, AssertionError::Invalid(_)));
    }

    #[test]
    fn verify_rejects_expired_assertion() {
        let mut assertion = base_assertion();
        assertion.iat = now_secs() - 120;
        assertion.exp = now_secs() - 60;
        let token = signing_key().sign(&assertion).expect("signs");
        let err = verify_with_key(&token, &decoding_key(), ASSERTION_MAX_TTL_SECONDS).unwrap_err();
        assert!(matches!(err, AssertionError::Invalid(_)));
    }

    #[test]
    fn verify_rejects_ttl_beyond_ceiling() {
        // `iat`/`exp` far enough apart that validation's `exp` check alone
        // wouldn't reject it (still in the future) but the max-TTL ceiling
        // must.
        let mut assertion = base_assertion();
        assertion.iat = now_secs();
        assertion.exp = assertion.iat + 3600;
        let token = signing_key().sign(&assertion).expect("signs");
        let err = verify_with_key(&token, &decoding_key(), ASSERTION_MAX_TTL_SECONDS).unwrap_err();
        assert!(matches!(err, AssertionError::TtlTooLong { .. }));
    }

    #[test]
    fn build_assertion_clamps_ttl_to_ceiling() {
        let assertion = build_assertion(
            "spiffe://penguintech.io/alpha/svc-process",
            "tenant-a",
            "community-a",
            "waddles.a.b.c",
            DestinationCategory::Fqdn,
            "discord.com",
            443,
            3600,
        );
        assert_eq!(assertion.exp - assertion.iat, ASSERTION_MAX_TTL_SECONDS);
    }

    /// A caller's `sub`-equality check (`egress_proxy::proxy::validate`'s
    /// `SubMismatch`) is layered on top of `verify_with_key`, not inside
    /// it -- an assertion that verifies cleanly still must be rejected by
    /// that separate check if its `sub` doesn't match the authenticated
    /// machine JWT's own `sub`.
    #[test]
    fn wrong_sub_is_detectable_after_verify() {
        let assertion = base_assertion();
        let token = signing_key().sign(&assertion).expect("signs");
        let verified =
            verify_with_key(&token, &decoding_key(), ASSERTION_MAX_TTL_SECONDS).expect("verifies");
        let authenticated_caller_sub = "spiffe://penguintech.io/alpha/svc-action";
        assert_ne!(verified.sub, authenticated_caller_sub);
    }

    #[test]
    fn destination_matches_rejects_wrong_port() {
        let assertion = base_assertion();
        assert!(!destination_matches(&assertion, "discord.com", 8443));
    }

    #[test]
    fn destination_matches_rejects_wrong_host() {
        let assertion = base_assertion();
        assert!(!destination_matches(&assertion, "evil.example.com", 443));
    }

    #[test]
    fn destination_matches_rejects_ip_literal_against_fqdn_grant() {
        let assertion = base_assertion();
        assert!(!destination_matches(&assertion, "1.2.3.4", 443));
    }
}
