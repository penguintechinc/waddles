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

use std::net::IpAddr;
use std::time::{SystemTime, UNIX_EPOCH};

use ipnet::IpNet;
use jsonwebtoken::errors::ErrorKind;
use jsonwebtoken::{Algorithm, DecodingKey, EncodingKey, Header, Validation};
use serde::{Deserialize, Serialize};

/// Closed vocabulary of reasons [`verify_with_key`] can reject an assertion
/// with. The strings are identical to the `outcome`/`REASON_*` values in
/// `core/service_auth`'s `jwt_hardening` module (and the Python
/// `flask_core.jwt_hardening`), so `egress_proxy` can feed them straight
/// into `waddles_jwt_verifications_total{outcome}` without translation.
/// This crate deliberately has no `service_auth` dependency, so the literals
/// are repeated here and pinned by a cross-crate test in `egress_proxy`.
pub mod reason {
    /// Not a decodable JWS.
    pub const MALFORMED: &str = "malformed";
    /// Header `alg` is not EdDSA.
    pub const ALG_MISMATCH: &str = "alg_mismatch";
    /// Signature does not verify.
    pub const BAD_SIGNATURE: &str = "bad_signature";
    /// `exp` has passed.
    pub const EXPIRED: &str = "expired";
    /// `iat` is in the future beyond the skew allowance.
    pub const IMMATURE: &str = "immature";
    /// A required claim is missing.
    pub const MISSING_CLAIM: &str = "missing_claim";
    /// A claim is present but empty or of the wrong type.
    pub const INVALID_CLAIM: &str = "invalid_claim";
    /// Anything else (an unexpected library failure).
    pub const INVALID: &str = "invalid";

    /// Every reason above -- for cross-crate vocabulary checks.
    pub const ALL: [&str; 8] = [
        MALFORMED,
        ALG_MISMATCH,
        BAD_SIGNATURE,
        EXPIRED,
        IMMATURE,
        MISSING_CLAIM,
        INVALID_CLAIM,
        INVALID,
    ];
}

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
    /// The assertion failed verification; the payload is one of [`reason`]'s
    /// constants -- never `jsonwebtoken` error text, a header value or a
    /// claim value, so the error is safe to log and to stringify.
    #[error("invalid assertion: {0}")]
    Rejected(&'static str),
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

/// The assertion exactly as it arrives on the wire: every field optional so
/// a *missing* claim (`missing_claim`) is told apart from a *mistyped* one
/// (`invalid_claim`, via the deserializer error) without parsing library text.
#[derive(Deserialize)]
struct WireAssertion {
    sub: Option<String>,
    tenant: Option<String>,
    community: Option<String>,
    app: Option<String>,
    category: Option<DestinationCategory>,
    destination: Option<String>,
    port: Option<u16>,
    jti: Option<String>,
    iat: Option<u64>,
    exp: Option<u64>,
}

/// A required wire field, or `missing_claim`.
fn required<T>(field: Option<T>) -> Result<T, AssertionError> {
    field.ok_or(AssertionError::Rejected(reason::MISSING_CLAIM))
}

/// A required wire string that must carry at least one non-whitespace
/// character: an empty `sub`/`tenant`/`jti` is "missing" by another name and
/// must never authorize anything (fail closed, no default-tenant fallback).
fn required_text(field: Option<String>) -> Result<String, AssertionError> {
    let value = required(field)?;
    if value.trim().is_empty() {
        return Err(AssertionError::Rejected(reason::INVALID_CLAIM));
    }
    Ok(value)
}

impl WireAssertion {
    /// Shape-check the claims and build the typed [`EgressAssertion`].
    fn into_assertion(self) -> Result<EgressAssertion, AssertionError> {
        Ok(EgressAssertion {
            sub: required_text(self.sub)?,
            tenant: required_text(self.tenant)?,
            community: required(self.community)?,
            app: required(self.app)?,
            category: required(self.category)?,
            destination: required(self.destination)?,
            port: required(self.port)?,
            jti: required_text(self.jti)?,
            iat: required(self.iat)?,
            exp: required(self.exp)?,
        })
    }
}

/// Map a `jsonwebtoken` failure to a closed [`reason`] -- never its text.
fn classify_decode_error(kind: &ErrorKind) -> &'static str {
    match kind {
        ErrorKind::ExpiredSignature => reason::EXPIRED,
        ErrorKind::ImmatureSignature => reason::IMMATURE,
        ErrorKind::InvalidSignature => reason::BAD_SIGNATURE,
        ErrorKind::MissingRequiredClaim(_) => reason::MISSING_CLAIM,
        ErrorKind::InvalidClaimFormat(_) | ErrorKind::Json(_) => reason::INVALID_CLAIM,
        ErrorKind::InvalidAlgorithm
        | ErrorKind::InvalidAlgorithmName
        | ErrorKind::MissingAlgorithm => reason::ALG_MISMATCH,
        ErrorKind::InvalidToken | ErrorKind::Base64(_) | ErrorKind::Utf8(_) => reason::MALFORMED,
        _ => reason::INVALID,
    }
}

/// Verifies signature, pinned algorithm (EdDSA only), required claims,
/// `exp`/`iat` (bounded skew) and the max-TTL ceiling against a single
/// already-resolved `decoding_key` -- the dependency-free primitive a
/// JWKS-aware verifier (looking `kid` up first, and vetting the JOSE header
/// for `alg: none` / `jku` / `jwk` / `x5u` / `x5c` / `crit` via
/// `service_auth::jwt_hardening`) builds on. Every rejection is
/// [`AssertionError::Rejected`] with a closed [`reason`]. Does **not** check
/// `sub` against a caller identity, replay/`jti` uniqueness, or
/// destination/port match -- those are the calling verifier's job
/// ([`destination_matches`]/[`resolved_matches`] below cover the latter;
/// `egress_proxy::proxy::validate` covers the former two).
///
/// There is no `iss`/`aud` here by design: an assertion is a per-call grant
/// bound to its signer through `sub`, which `egress_proxy` compares with the
/// machine JWT it authenticated on the same connection; `sub`, `tenant` and
/// `jti` must be non-empty (there is no default-tenant fallback).
pub fn verify_with_key(
    token: &str,
    decoding_key: &DecodingKey,
    max_ttl_secs: u64,
) -> Result<EgressAssertion, AssertionError> {
    let mut validation = Validation::new(Algorithm::EdDSA);
    validation.leeway = ASSERTION_CLOCK_SKEW_SECONDS;
    validation.set_required_spec_claims(&["exp", "sub"]);
    validation.validate_aud = false;

    let data = jsonwebtoken::decode::<WireAssertion>(token, decoding_key, &validation)
        .map_err(|e| AssertionError::Rejected(classify_decode_error(e.kind())))?;
    let claims = data.claims.into_assertion()?;

    if claims.iat > now_secs().saturating_add(ASSERTION_CLOCK_SKEW_SECONDS) {
        return Err(AssertionError::Rejected(reason::IMMATURE));
    }
    let ttl = claims.exp.saturating_sub(claims.iat);
    if ttl > max_ttl_secs {
        return Err(AssertionError::TtlTooLong {
            actual: ttl,
            max: max_ttl_secs,
        });
    }
    Ok(claims)
}

/// Normalizes an IPv4-mapped IPv6 address (`::ffff:a.b.c.d`) down to its
/// plain IPv4 form so a grant written as a bare IPv4 literal/CIDR still
/// matches a request/resolved address that arrives in mapped form (and
/// vice versa is a non-issue: a real IPv4 address never needs unmapping).
/// Deliberately narrower than `bundle_host_http::egress::embedded_ipv4`
/// (which also canonicalizes NAT64 and deprecated IPv4-compatible forms
/// for its own resolved-address SSRF classification) -- the assertion's
/// `destination` is always written by this org's own signer, never by an
/// attacker-controlled resolver response, so the mapped-address case is
/// the only one worth guarding here.
fn normalize_ip(ip: IpAddr) -> IpAddr {
    match ip {
        IpAddr::V6(v6) => v6
            .to_ipv4_mapped()
            .map(IpAddr::V4)
            .unwrap_or(IpAddr::V6(v6)),
        v4 @ IpAddr::V4(_) => v4,
    }
}

/// `Fqdn` grant comparison: exact match, case-insensitive, with a
/// trailing-dot (DNS root label) normalized away on both sides -- no
/// implicit subdomain wildcard (matches `bundle_host_http::egress::
/// host_matches`, the connector-spec-defined semantics for
/// `net.http.fqdn:<host>`; a wildcard grant is a distinct, explicit manifest
/// syntax this crate doesn't need to know about since it only ever sees
/// the fully-resolved grant string handed to it).
fn fqdn_matches(requested_host: &str, granted: &str) -> bool {
    fn normalize(host: &str) -> String {
        host.trim_end_matches('.').to_ascii_lowercase()
    }
    normalize(requested_host) == normalize(granted)
}

/// Does a resolved/literal address `ip` satisfy a `PublicIp`/`PrivateIp`
/// grant string. Shared by [`destination_matches`] (checked against the
/// *requested* host, once it's confirmed to parse as a literal) and, in
/// `egress_proxy`, the DNS-rebinding re-check against the *resolved*
/// address -- the one CIDR/IP-parsing implementation both call, so the two
/// can never silently diverge (the bug this function replaces: a plain
/// `==` string comparison that could never match a CIDR grant against an
/// IP dial target at all).
///
/// - `PublicIp` grants are always a single IP literal (connector spec:
///   `net.http.public-ip:<ip>`, no CIDR) -- exact address equality after
///   [`normalize_ip`].
/// - `PrivateIp` grants may be a single IP literal or a CIDR block
///   (connector spec: `net.http.private-ip:<ip|cidr>`) -- parsed as an
///   [`IpNet`] first (covers both shapes: `ipnet` parses a bare address
///   with no `/prefix` as an error, so a plain-literal grant falls back to
///   [`IpAddr`] parsing below); containment is checked after normalizing
///   both sides, so a v4-mapped-v6 grant or target still matches its plain
///   v4 counterpart.
///
/// An unparsable grant, or a `granted`/`ip` family that can never overlap
/// (e.g. a native IPv6 address against an IPv4 grant), is rejected --
/// fail-closed, never matched. `Fqdn` has no IP concept here and always
/// returns `false`; callers with a DNS-resolution concept (this crate
/// deliberately has none) must handle that category themselves.
pub fn ip_matches_grant(category: DestinationCategory, granted: &str, ip: IpAddr) -> bool {
    let ip = normalize_ip(ip);
    match category {
        DestinationCategory::Fqdn => false,
        DestinationCategory::PublicIp => granted
            .parse::<IpAddr>()
            .map(|g| normalize_ip(g) == ip)
            .unwrap_or(false),
        DestinationCategory::PrivateIp => {
            if let Ok(net) = granted.parse::<IpNet>() {
                net_contains(net, ip)
            } else {
                granted
                    .parse::<IpAddr>()
                    .map(|g| normalize_ip(g) == ip)
                    .unwrap_or(false)
            }
        }
    }
}

/// [`IpNet::contains`] requires matching address families -- this bridges
/// an IPv4-mapped-IPv6 grant/target pair to the family the other side of
/// the comparison is actually in, rather than failing the match outright.
fn net_contains(net: IpNet, ip: IpAddr) -> bool {
    match (net, ip) {
        (IpNet::V4(net), IpAddr::V4(ip)) => net.contains(&ip),
        (IpNet::V6(net), IpAddr::V6(ip)) => net.contains(&ip),
        (IpNet::V6(net), IpAddr::V4(ip)) => net.contains(&ip.to_ipv6_mapped()),
        (IpNet::V4(net), IpAddr::V6(ip)) => ip
            .to_ipv4_mapped()
            .map(|ip| net.contains(&ip))
            .unwrap_or(false),
    }
}

/// Step 4 of the pipeline (spec-equivalent to `bundle_host_http`'s
/// declared-host check): does the *requested* destination (host **and**
/// port) match what the assertion actually grants. The port check is
/// exact -- a grant for `:443` never authorizes the same host on a
/// different (even operator-allowlisted) port.
///
/// `PublicIp`/`PrivateIp` grants are matched via [`ip_matches_grant`] (CIDR-
/// aware for `PrivateIp`) rather than a plain string comparison -- fixes a
/// bug where a `net.http.private-ip` grant expressed as a CIDR (the
/// connector spec's own `<ip|cidr>` syntax) could never match an IP dial
/// target at all, since no dialed literal is ever byte-for-byte equal to a
/// `a.b.c.d/24`-shaped string.
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
            requested_host.parse::<IpAddr>().is_err()
                && fqdn_matches(requested_host, &assertion.destination)
        }
        DestinationCategory::PublicIp | DestinationCategory::PrivateIp => requested_host
            .parse::<IpAddr>()
            .map(|ip| ip_matches_grant(assertion.category, &assertion.destination, ip))
            .unwrap_or(false),
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
        assert!(matches!(err, AssertionError::Rejected(_)));
    }

    #[test]
    fn verify_rejects_expired_assertion() {
        let mut assertion = base_assertion();
        assertion.iat = now_secs() - 120;
        assertion.exp = now_secs() - 60;
        let token = signing_key().sign(&assertion).expect("signs");
        let err = verify_with_key(&token, &decoding_key(), ASSERTION_MAX_TTL_SECONDS).unwrap_err();
        assert!(matches!(err, AssertionError::Rejected(_)));
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

    #[test]
    fn destination_matches_fqdn_is_case_insensitive_and_ignores_trailing_dot() {
        let assertion = base_assertion(); // grants "discord.com"
        assert!(destination_matches(&assertion, "DISCORD.COM", 443));
        assert!(destination_matches(&assertion, "discord.com.", 443));
        assert!(destination_matches(&assertion, "Discord.Com.", 443));
    }

    #[test]
    fn destination_matches_fqdn_has_no_implicit_subdomain_wildcard() {
        let assertion = base_assertion(); // grants "discord.com"
        assert!(!destination_matches(&assertion, "api.discord.com", 443));
    }

    fn ip_assertion(
        category: DestinationCategory,
        destination: &str,
        port: u16,
    ) -> EgressAssertion {
        build_assertion(
            "spiffe://penguintech.io/alpha/svc-process",
            "tenant-a",
            "community-a",
            "waddles.a.b.c",
            category,
            destination,
            port,
            30,
        )
    }

    /// Table-driven coverage of [`destination_matches`]'s `PublicIp`/
    /// `PrivateIp` arms: single-IP grants, CIDR grants (`PrivateIp` only --
    /// the connector spec's `net.http.public-ip:<ip>` syntax never allows a
    /// CIDR), IPv6, IPv4-mapped-IPv6 normalization both directions, and the
    /// fail-closed unparsable/mismatch cases. Each case is independent --
    /// no shared mutable state -- so a new row is the only edit needed to
    /// extend coverage.
    #[test]
    fn destination_matches_public_and_private_ip_table() {
        struct Case {
            name: &'static str,
            category: DestinationCategory,
            granted: &'static str,
            requested_host: &'static str,
            requested_port: u16,
            expected: bool,
        }
        let cases = [
            Case {
                name: "public_ip exact literal match",
                category: DestinationCategory::PublicIp,
                granted: "93.184.216.34",
                requested_host: "93.184.216.34",
                requested_port: 443,
                expected: true,
            },
            Case {
                name: "public_ip mismatched literal",
                category: DestinationCategory::PublicIp,
                granted: "93.184.216.34",
                requested_host: "93.184.216.35",
                requested_port: 443,
                expected: false,
            },
            Case {
                name: "public_ip grant is never CIDR-widened",
                category: DestinationCategory::PublicIp,
                granted: "93.184.216.0/24",
                requested_host: "93.184.216.34",
                requested_port: 443,
                // `ipnet` fails to parse a bare-literal PublicIp grant as
                // an IpAddr once it's actually a CIDR string, and
                // `ip_matches_grant`'s PublicIp arm never falls back to
                // IpNet containment -- an operator who mistakenly writes a
                // CIDR under `public-ip` gets a hard deny, not a silent
                // widen.
                expected: false,
            },
            Case {
                name: "private_ip exact literal match",
                category: DestinationCategory::PrivateIp,
                granted: "192.168.1.10",
                requested_host: "192.168.1.10",
                requested_port: 8080,
                expected: true,
            },
            Case {
                name: "private_ip CIDR containment -- the bug this fix closes",
                category: DestinationCategory::PrivateIp,
                granted: "192.168.1.0/24",
                requested_host: "192.168.1.200",
                requested_port: 8080,
                expected: true,
            },
            Case {
                name: "private_ip CIDR -- outside the block",
                category: DestinationCategory::PrivateIp,
                granted: "192.168.1.0/24",
                requested_host: "192.168.2.1",
                requested_port: 8080,
                expected: false,
            },
            Case {
                name: "private_ip IPv6 ULA CIDR containment",
                category: DestinationCategory::PrivateIp,
                granted: "fd00::/8",
                requested_host: "fd12:3456::1",
                requested_port: 8080,
                expected: true,
            },
            Case {
                name: "private_ip IPv4-mapped-IPv6 target normalizes against a plain v4 CIDR grant",
                category: DestinationCategory::PrivateIp,
                granted: "192.168.1.0/24",
                requested_host: "::ffff:192.168.1.200",
                requested_port: 8080,
                expected: true,
            },
            Case {
                name:
                    "public_ip IPv4-mapped-IPv6 target normalizes against a plain v4 literal grant",
                category: DestinationCategory::PublicIp,
                granted: "93.184.216.34",
                requested_host: "::ffff:93.184.216.34",
                requested_port: 443,
                expected: true,
            },
            Case {
                name: "private_ip unparsable grant is rejected, not matched",
                category: DestinationCategory::PrivateIp,
                granted: "not-a-cidr",
                requested_host: "192.168.1.10",
                requested_port: 8080,
                expected: false,
            },
            Case {
                name: "public_ip unparsable requested host is rejected",
                category: DestinationCategory::PublicIp,
                granted: "93.184.216.34",
                requested_host: "not-an-ip",
                requested_port: 443,
                expected: false,
            },
            Case {
                name: "private_ip CIDR with abusive/invalid prefix length is rejected outright",
                category: DestinationCategory::PrivateIp,
                granted: "192.168.1.0/33",
                requested_host: "192.168.1.10",
                requested_port: 8080,
                expected: false,
            },
        ];
        for case in cases {
            let assertion = ip_assertion(case.category, case.granted, case.requested_port);
            let actual = destination_matches(&assertion, case.requested_host, case.requested_port);
            assert_eq!(
                actual, case.expected,
                "case {:?}: expected {}, got {}",
                case.name, case.expected, actual
            );
        }
    }

    /// The always-denied ranges (loopback, link-local/metadata) are
    /// enforced separately from `destination_matches`
    /// (`bundle_host_http::egress::is_forbidden_address` /
    /// `egress_proxy::ip_policy::is_denied`), never inside it -- this
    /// crate's job is only "does the requested address fall in the
    /// granted range", not "is the granted range itself safe to reach".
    /// This test documents that boundary: a maximally-wide `PrivateIp`
    /// grant of `0.0.0.0/0` legitimately *matches* the cloud-metadata and
    /// loopback addresses at this layer -- the separate always-deny check
    /// is what actually blocks them, proven end-to-end in
    /// `egress_proxy`'s `tests/e2e.rs`
    /// (`metadata_and_cluster_cidrs_are_always_blocked_even_with_private_ip_grant`).
    #[test]
    fn destination_matches_wide_open_private_cidr_still_matches_metadata_and_loopback() {
        let assertion = ip_assertion(DestinationCategory::PrivateIp, "0.0.0.0/0", 443);
        assert!(destination_matches(&assertion, "169.254.169.254", 443));
        assert!(destination_matches(&assertion, "127.0.0.1", 443));
    }

    /// Wraps the fixed test-only DER fixture in PEM at runtime, so no PEM
    /// literal is committed to source.
    fn key_a_pem() -> String {
        const T: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
        let mut b64 = String::new();
        for chunk in KEY_A_PRIV_DER.chunks(3) {
            let b = [
                chunk[0],
                *chunk.get(1).unwrap_or(&0),
                *chunk.get(2).unwrap_or(&0),
            ];
            let n = (u32::from(b[0]) << 16) | (u32::from(b[1]) << 8) | u32::from(b[2]);
            for i in 0..4 {
                if i <= chunk.len() {
                    b64.push(T[((n >> (18 - 6 * i)) & 63) as usize] as char);
                } else {
                    b64.push('=');
                }
            }
        }
        format!(
            "-----BEGIN {0}-----\n{b64}\n-----END {0}-----\n",
            "PRIVATE KEY"
        )
    }

    #[test]
    fn from_ed25519_pem_loads_and_signs_verifiably() {
        let key = AssertionSigningKey::from_ed25519_pem(key_a_pem().as_bytes(), "k1")
            .expect("valid pem loads");
        let token = key.sign(&base_assertion()).expect("sign");
        verify_with_key(&token, &decoding_key(), ASSERTION_MAX_TTL_SECONDS)
            .expect("PEM-loaded key signs a token the matching public key verifies");
    }

    #[test]
    fn from_ed25519_pem_rejects_garbage() {
        let err = AssertionSigningKey::from_ed25519_pem(b"not a pem", "k1")
            .err()
            .expect("must fail");
        assert!(matches!(err, AssertionError::InvalidSigningKey(_)));
        assert!(err.to_string().contains("signing key"));
    }

    #[test]
    fn from_env_requires_kid() {
        let err = AssertionSigningKey::from_env("EA_T1_PATH", "EA_T1_KEY", "EA_T1_KID")
            .err()
            .expect("must fail");
        assert!(matches!(err, AssertionError::InvalidSigningKey(m) if m.contains("EA_T1_KID")));
    }

    #[test]
    fn from_env_fails_when_neither_source_set() {
        std::env::set_var("EA_T2_KID", "k1");
        let err = AssertionSigningKey::from_env("EA_T2_PATH", "EA_T2_KEY", "EA_T2_KID")
            .err()
            .expect("must fail");
        assert!(matches!(err, AssertionError::InvalidSigningKey(m) if m.contains("neither")));
    }

    #[test]
    fn from_env_loads_inline_pem() {
        std::env::set_var("EA_T3_KID", "k1");
        std::env::set_var("EA_T3_KEY", key_a_pem());
        assert!(AssertionSigningKey::from_env("EA_T3_PATH", "EA_T3_KEY", "EA_T3_KID").is_ok());
    }

    #[test]
    fn from_env_rejects_bad_inline_pem() {
        std::env::set_var("EA_T4_KID", "k1");
        std::env::set_var("EA_T4_KEY", "garbage");
        assert!(AssertionSigningKey::from_env("EA_T4_PATH", "EA_T4_KEY", "EA_T4_KID").is_err());
    }

    #[test]
    fn from_env_loads_pem_from_file_and_prefers_it() {
        let path = std::env::temp_dir().join(format!("ea_key_{}.pem", std::process::id()));
        std::fs::write(&path, key_a_pem()).expect("write");
        std::env::set_var("EA_T5_KID", "k1");
        std::env::set_var("EA_T5_PATH", &path);
        std::env::set_var("EA_T5_KEY", "garbage-ignored-when-path-set");
        let res = AssertionSigningKey::from_env("EA_T5_PATH", "EA_T5_KEY", "EA_T5_KID");
        let _ = std::fs::remove_file(path);
        assert!(res.is_ok());
    }

    #[test]
    fn from_env_unreadable_path_fails_closed() {
        std::env::set_var("EA_T6_KID", "k1");
        std::env::set_var("EA_T6_PATH", "/nonexistent/ea/key.pem");
        let err = AssertionSigningKey::from_env("EA_T6_PATH", "EA_T6_KEY", "EA_T6_KID")
            .err()
            .expect("must fail");
        assert!(matches!(err, AssertionError::InvalidSigningKey(m) if m.contains("reading")));
    }

    #[test]
    fn ip_matches_grant_fqdn_never_matches_an_ip() {
        let ip: IpAddr = "1.2.3.4".parse().expect("ip");
        assert!(!ip_matches_grant(DestinationCategory::Fqdn, "1.2.3.4", ip));
    }

    #[test]
    fn ip_matches_grant_v6_cidr_contains_v4_mapped_target() {
        let ip: IpAddr = "10.1.2.3".parse().expect("ip");
        assert!(ip_matches_grant(
            DestinationCategory::PrivateIp,
            "::ffff:10.0.0.0/104",
            ip
        ));
        assert!(!ip_matches_grant(
            DestinationCategory::PrivateIp,
            "::ffff:192.168.0.0/112",
            ip
        ));
    }

    #[test]
    fn net_contains_v4_cidr_with_v6_targets() {
        let net: IpNet = "10.0.0.0/8".parse().expect("net");
        let mapped: IpAddr = "::ffff:10.1.1.1".parse().expect("ip");
        let native: IpAddr = "2001:db8::1".parse().expect("ip");
        assert!(net_contains(net, mapped));
        assert!(
            !net_contains(net, native),
            "native v6 never matches a v4 grant"
        );
    }

    #[test]
    fn error_display_strings() {
        assert!(AssertionError::Rejected(reason::EXPIRED)
            .to_string()
            .contains("invalid assertion: expired"));
        assert!(AssertionError::TtlTooLong { actual: 9, max: 5 }
            .to_string()
            .contains("exceeds max"));
        assert!(AssertionError::SigningFailed("x".into())
            .to_string()
            .contains("failed to sign"));
    }

    // ---- Phase-0 hardening (RFC 8725) ------------------------------------

    use base64::Engine as _;
    use serde_json::{json, Value};

    fn b64(value: &Value) -> String {
        base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(value.to_string())
    }

    /// Sign arbitrary header + claims JSON with key A.
    fn raw_token(header: &Value, claims: &Value) -> String {
        let message = format!("{}.{}", b64(header), b64(claims));
        let signature = jsonwebtoken::crypto::sign(
            message.as_bytes(),
            &EncodingKey::from_ed_der(KEY_A_PRIV_DER),
            Algorithm::EdDSA,
        )
        .expect("sign");
        format!("{message}.{signature}")
    }

    fn good_claims() -> Value {
        let now = now_secs();
        json!({
            "sub": "spiffe://penguintech.io/alpha/svc-process",
            "tenant": "tenant-a", "community": "community-a", "app": "waddles.a.b.c",
            "category": "fqdn", "destination": "discord.com", "port": 443,
            "jti": "jti-1", "iat": now, "exp": now + 30,
        })
    }

    fn verify_raw(claims: &Value) -> Result<EgressAssertion, AssertionError> {
        let header = json!({"alg": "EdDSA", "kid": "k1"});
        verify_with_key(
            &raw_token(&header, claims),
            &decoding_key(),
            ASSERTION_MAX_TTL_SECONDS,
        )
    }

    fn rejected_reason(result: Result<EgressAssertion, AssertionError>) -> &'static str {
        match result {
            Err(AssertionError::Rejected(reason)) => reason,
            other => panic!("expected a Rejected assertion, got {other:?}"),
        }
    }

    #[test]
    fn a_hand_built_token_with_every_claim_verifies() {
        let verified = verify_raw(&good_claims()).expect("complete claim set verifies");
        assert_eq!(verified.tenant, "tenant-a");
        assert_eq!(verified.jti, "jti-1");
        assert_eq!(verified.port, 443);
    }

    #[test]
    fn tampered_and_expired_assertions_name_their_reason() {
        let assertion = base_assertion();
        let token = signing_key().sign(&assertion).expect("signs");
        let mut parts: Vec<&str> = token.split('.').collect();
        let forged = forged_signing_key().sign(&assertion).expect("signs");
        let forged_sig = forged.split('.').nth(2).expect("sig").to_string();
        parts[2] = &forged_sig;
        assert_eq!(
            rejected_reason(verify_with_key(
                &parts.join("."),
                &decoding_key(),
                ASSERTION_MAX_TTL_SECONDS
            )),
            reason::BAD_SIGNATURE
        );

        let mut claims = good_claims();
        claims["iat"] = json!(now_secs() - 120);
        claims["exp"] = json!(now_secs() - 60);
        assert_eq!(rejected_reason(verify_raw(&claims)), reason::EXPIRED);
    }

    #[test]
    fn every_required_claim_missing_is_missing_claim() {
        for name in [
            "sub",
            "tenant",
            "community",
            "app",
            "category",
            "destination",
            "port",
            "jti",
            "iat",
            "exp",
        ] {
            let mut claims = good_claims();
            claims.as_object_mut().expect("object").remove(name);
            assert_eq!(
                rejected_reason(verify_raw(&claims)),
                reason::MISSING_CLAIM,
                "{name}"
            );
        }
    }

    #[test]
    fn empty_identity_claims_fail_closed_with_no_default_tenant() {
        for name in ["sub", "tenant", "jti"] {
            for empty in ["", "   ", "\t\n"] {
                let mut claims = good_claims();
                claims[name] = json!(empty);
                assert_eq!(
                    rejected_reason(verify_raw(&claims)),
                    reason::INVALID_CLAIM,
                    "{name}={empty:?}"
                );
            }
        }
    }

    #[test]
    fn mistyped_claims_are_invalid_claim() {
        for (name, value) in [
            ("port", json!("443")),
            ("port", json!(70000)),
            ("port", json!(-1)),
            ("category", json!("bogus")),
            ("tenant", json!(7)),
            ("jti", json!(["x"])),
            ("destination", json!(false)),
            ("iat", json!("now")),
        ] {
            let mut claims = good_claims();
            claims[name] = value.clone();
            assert_eq!(
                rejected_reason(verify_raw(&claims)),
                reason::INVALID_CLAIM,
                "{name}={value}"
            );
        }
    }

    #[test]
    fn iat_in_the_future_is_bounded_by_the_skew() {
        let mut claims = good_claims();
        claims["iat"] = json!(now_secs() + ASSERTION_CLOCK_SKEW_SECONDS + 60);
        claims["exp"] = json!(now_secs() + ASSERTION_CLOCK_SKEW_SECONDS + 90);
        assert_eq!(rejected_reason(verify_raw(&claims)), reason::IMMATURE);

        let mut claims = good_claims();
        claims["iat"] = json!(now_secs() + ASSERTION_CLOCK_SKEW_SECONDS - 2);
        claims["exp"] = json!(now_secs() + ASSERTION_CLOCK_SKEW_SECONDS + 20);
        verify_raw(&claims).expect("iat inside the skew allowance verifies");
    }

    #[test]
    fn alg_none_and_alg_confusion_are_refused_by_the_pinned_validation() {
        // The JWKS-aware verifier vets the header first (see `egress_proxy`);
        // this proves the primitive itself is independently pinned to EdDSA.
        let claims = good_claims();
        let none = format!("{}.{}.", b64(&json!({"alg": "none"})), b64(&claims));
        assert!(matches!(
            verify_with_key(&none, &decoding_key(), ASSERTION_MAX_TTL_SECONDS),
            Err(AssertionError::Rejected(_))
        ));

        for alg in [Algorithm::HS256, Algorithm::HS384, Algorithm::HS512] {
            let token = jsonwebtoken::encode(
                &Header::new(alg),
                &claims,
                &EncodingKey::from_secret(KEY_A_PUB_RAW),
            )
            .expect("encode");
            assert_eq!(
                rejected_reason(verify_with_key(
                    &token,
                    &decoding_key(),
                    ASSERTION_MAX_TTL_SECONDS
                )),
                reason::ALG_MISMATCH,
                "{alg:?}"
            );
        }
    }

    #[test]
    fn structurally_broken_tokens_are_rejected_with_a_closed_reason() {
        for token in ["", "not-a-jwt", "a.b", "a.b.c", "%%%.%%%.%%%"] {
            let reason = rejected_reason(verify_with_key(
                token,
                &decoding_key(),
                ASSERTION_MAX_TTL_SECONDS,
            ));
            assert!(
                super::reason::ALL.contains(&reason),
                "{token:?} -> {reason}"
            );
        }
    }

    #[test]
    fn rejection_is_a_closed_vocabulary_with_no_library_text() {
        let err = AssertionError::Rejected(reason::BAD_SIGNATURE);
        assert_eq!(err.to_string(), "invalid assertion: bad_signature");
        assert_eq!(reason::ALL.len(), 8);
        let mut sorted = reason::ALL.to_vec();
        sorted.sort_unstable();
        sorted.dedup();
        assert_eq!(sorted.len(), reason::ALL.len(), "no duplicate reasons");
    }

    #[test]
    fn decode_errors_map_to_closed_reasons() {
        for (kind, expected) in [
            (ErrorKind::ExpiredSignature, reason::EXPIRED),
            (ErrorKind::ImmatureSignature, reason::IMMATURE),
            (ErrorKind::InvalidSignature, reason::BAD_SIGNATURE),
            (
                ErrorKind::MissingRequiredClaim("sub".into()),
                reason::MISSING_CLAIM,
            ),
            (
                ErrorKind::InvalidClaimFormat("exp".into()),
                reason::INVALID_CLAIM,
            ),
            (ErrorKind::InvalidAlgorithm, reason::ALG_MISMATCH),
            (ErrorKind::InvalidAlgorithmName, reason::ALG_MISMATCH),
            (ErrorKind::MissingAlgorithm, reason::ALG_MISMATCH),
            (ErrorKind::InvalidToken, reason::MALFORMED),
            (ErrorKind::InvalidEddsaKey, reason::INVALID),
        ] {
            assert_eq!(classify_decode_error(&kind), expected, "{kind:?}");
        }
    }
}
