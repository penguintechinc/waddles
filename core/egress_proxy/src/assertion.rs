//! The signed per-request allowlist assertion (design choice: a
//! **separate, short-lived EdDSA JWT**, not a claim folded into the
//! machine JWT `service_auth` already verifies).
//!
//! **Why a second token, not a machine-JWT claim:** the machine JWT
//! (`core/service_auth`) authenticates *which service* is calling and is
//! deliberately long-lived-ish (up to `service_auth::MAX_TOKEN_TTL_SECONDS`
//! = 1h, cached and reused for many requests -- see
//! `service_auth::MachineJwtClient`). The allowlist assertion instead
//! answers "is *this specific* tenant/community/app/destination call
//! authorized *right now*" and needs a much shorter replay window (default
//! 60s, `ASSERTION_MAX_TTL_SECONDS`) than the calling pod's own machine
//! JWT lifetime. Folding a fast-expiring, per-request claim into a
//! slow-rotating bearer token would force minting a fresh machine JWT per
//! `http.send`/`CONNECT`, defeating `MachineJwtClient`'s whole cache
//! rationale. Keeping it a separate token also matches the already-landed
//! Helm skeleton (`values.yaml` `egressProxy.allowlistSigning`): hub-api
//! mints this token with its own key, distinct from the per-service
//! machine-JWT signing key `service_auth` verifies against, and the
//! public half is mounted into this pod as a plain file
//! (`ALLOWLIST_SIGNING_KEY_PATH`) -- a single static Ed25519 key, not a
//! JWKS set, since there is exactly one signer (hub-api) for this token
//! type, unlike the per-service machine-JWT trust bundle.
//!
//! Header: `X-Waddles-Egress-Assertion: <compact EdDSA JWT>`.

use std::time::{SystemTime, UNIX_EPOCH};

use jsonwebtoken::{Algorithm, DecodingKey, Validation};
use serde::{Deserialize, Serialize};

use crate::ip_policy::DestinationCategory;

pub const ASSERTION_HEADER: &str = "x-waddles-egress-assertion";

/// Clock-skew leeway applied to `exp`, mirroring
/// `service_auth::CLOCK_SKEW_SECONDS` but kept small (this token's whole
/// lifetime is itself only tens of seconds).
const ASSERTION_CLOCK_SKEW_SECONDS: u64 = 5;

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub struct EgressAssertion {
    pub tenant: String,
    pub community: String,
    pub app: String,
    pub category: DestinationCategory,
    /// `Fqdn`: exact hostname (case-insensitive, no wildcard -- mirrors
    /// `bundle_host_http`'s own exact-match-only `host_matches`
    /// philosophy). `PublicIp`: exact IP literal. `PrivateIp`: an IP
    /// literal or a CIDR the resolved address must fall within.
    pub destination: String,
    pub iat: u64,
    pub exp: u64,
}

#[derive(thiserror::Error, Debug)]
pub enum AssertionError {
    #[error("missing assertion header")]
    Missing,
    #[error("invalid assertion: {0}")]
    Invalid(String),
    #[error("assertion ttl {actual}s exceeds max {max}s")]
    TtlTooLong { actual: u64, max: u64 },
}

/// Loads the single static Ed25519 verification key mounted at
/// `ALLOWLIST_SIGNING_KEY_PATH` (SPKI PEM -- the "public-key.pem" secret
/// key already named in `values.yaml`).
pub fn load_signing_key(path: &str) -> Result<DecodingKey, AssertionError> {
    let pem =
        std::fs::read(path).map_err(|e| AssertionError::Invalid(format!("reading {path}: {e}")))?;
    DecodingKey::from_ed_pem(&pem)
        .map_err(|e| AssertionError::Invalid(format!("parsing {path}: {e}")))
}

/// Verifies signature + `exp` (with leeway) and enforces the max-TTL
/// ceiling (defense in depth against a compromised/misconfigured signer
/// minting a long-lived assertion).
pub fn verify(
    token: &str,
    key: &DecodingKey,
    max_ttl_secs: u64,
) -> Result<EgressAssertion, AssertionError> {
    let mut validation = Validation::new(Algorithm::EdDSA);
    validation.leeway = ASSERTION_CLOCK_SKEW_SECONDS;
    validation.set_required_spec_claims(&["exp", "iat"]);
    // No `aud`/`iss` check: this token has exactly one purpose and one
    // verifier (this proxy), unlike the machine JWT.
    validation.validate_aud = false;

    let data = jsonwebtoken::decode::<EgressAssertion>(token, key, &validation)
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

/// Extracts the assertion token from `headers` and verifies it.
pub fn verify_from_header(
    headers: &http::HeaderMap,
    key: &DecodingKey,
    max_ttl_secs: u64,
) -> Result<EgressAssertion, AssertionError> {
    let token = headers
        .get(ASSERTION_HEADER)
        .and_then(|v| v.to_str().ok())
        .ok_or(AssertionError::Missing)?;
    verify(token, key, max_ttl_secs)
}

/// Step 4 of the pipeline (spec-equivalent to `bundle_host_http`'s
/// declared-host check): does the *requested* destination match what the
/// assertion actually grants, before any DNS resolution happens.
pub fn destination_matches(assertion: &EgressAssertion, requested_host: &str) -> bool {
    match assertion.category {
        DestinationCategory::Fqdn => {
            // Exact match only -- an IP literal request never satisfies an
            // Fqdn-category grant, and vice versa (category-crossing is a
            // separate grant, connector-spec explicit-deferral note).
            requested_host.parse::<std::net::IpAddr>().is_err()
                && requested_host.eq_ignore_ascii_case(&assertion.destination)
        }
        DestinationCategory::PublicIp => requested_host == assertion.destination,
        DestinationCategory::PrivateIp => match requested_host.parse::<std::net::IpAddr>() {
            Ok(ip) => match assertion.destination.parse::<ipnet::IpNet>() {
                Ok(net) => net.contains(&ip),
                Err(_) => requested_host == assertion.destination,
            },
            Err(_) => false,
        },
    }
}

/// Whether the resolved address itself still satisfies the assertion's
/// category (guards against DNS rebinding: an `Fqdn` grant only ever
/// authorizes the address `is_forbidden_address`/`ip_policy::is_denied`
/// would already treat as public -- this check is a second, explicit
/// belt-and-suspenders gate specifically for the private-ip CIDR case,
/// where the *resolved* address, not just the requested literal, must
/// fall in the granted range).
pub fn resolved_matches(assertion: &EgressAssertion, resolved: std::net::IpAddr) -> bool {
    match assertion.category {
        DestinationCategory::Fqdn => true, // enforced by ip_policy::is_denied instead
        DestinationCategory::PublicIp => resolved.to_string() == assertion.destination,
        DestinationCategory::PrivateIp => match assertion.destination.parse::<ipnet::IpNet>() {
            Ok(net) => net.contains(&resolved),
            Err(_) => assertion.destination.parse::<std::net::IpAddr>() == Ok(resolved),
        },
    }
}

pub fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
