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
//! rationale.
//!
//! **Security review redesign (post-initial-landing): no more single
//! static hub-api signing key.** The *calling service itself* signs each
//! assertion with the same per-service Ed25519 key it already uses to
//! mint its own machine JWT (`core/service_auth`, PR #438's per-service
//! JWKS) -- the assertion's `kid` header names that same key, and this
//! module verifies it against the identical [`service_auth::TrustBundle`]
//! `auth::authenticate` already used to verify the machine JWT, not a
//! second, separately-mounted static key. This removes hub-api as an
//! extra minting hop on every `http.send`/`CONNECT` (the calling pod signs
//! locally) and ties the assertion's authenticity to the same identity
//! (and the same key-rotation story) as the machine JWT itself. The
//! `sub` claim carries the caller's SPIFFE ID and is checked by
//! [`crate::proxy::validate`] against the authenticated machine JWT's own
//! `sub` -- an assertion signed by service A can never be replayed by
//! service B, even if B somehow obtained the token, because the signature
//! itself is keyed to A's identity.
//!
//! Header: `X-Waddles-Egress-Assertion: <compact EdDSA JWT>`.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use jsonwebtoken::{Algorithm, Validation};
use serde::{Deserialize, Serialize};
use service_auth::TrustBundle;

use crate::ip_policy::DestinationCategory;

pub const ASSERTION_HEADER: &str = "x-waddles-egress-assertion";

/// Clock-skew leeway applied to `exp`, mirroring
/// `service_auth::CLOCK_SKEW_SECONDS` but kept small (this token's whole
/// lifetime is itself only tens of seconds).
const ASSERTION_CLOCK_SKEW_SECONDS: u64 = 5;

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub struct EgressAssertion {
    /// The signing/calling service's own SPIFFE ID -- must equal the
    /// authenticated machine JWT's `sub` (checked in
    /// `crate::proxy::validate`), binding this assertion to the same
    /// identity that authenticated the connection it rides on.
    pub sub: String,
    pub tenant: String,
    pub community: String,
    pub app: String,
    pub category: DestinationCategory,
    /// `Fqdn`: exact hostname (case-insensitive, no wildcard -- mirrors
    /// `bundle_host_http`'s own exact-match-only `host_matches`
    /// philosophy). `PublicIp`: exact IP literal. `PrivateIp`: an IP
    /// literal or a CIDR the resolved address must fall within.
    pub destination: String,
    /// The exact destination port this grant authorizes -- checked in
    /// [`destination_matches`] against the actually-requested port, not
    /// just the operator-wide port allowlist.
    pub port: u16,
    /// Unique per-assertion ID, checked against [`ReplayCache`] so the
    /// same short-lived grant can never authorize a second connection.
    pub jti: String,
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
    #[error("unknown signing key {0:?}")]
    UnknownKeyId(Option<String>),
    #[error("assertion sub {assertion_sub:?} does not match authenticated caller {jwt_sub:?}")]
    SubMismatch {
        assertion_sub: String,
        jwt_sub: String,
    },
    #[error("assertion jti {0:?} has already been used")]
    Replayed(String),
}

/// Replay protection for assertion `jti`s, scoped to this proxy instance's
/// lifetime and each `jti`'s own (short) `exp`.
///
/// **Multi-replica tradeoff (documented, accepted):** [`InMemoryReplayCache`]
/// is per-pod, not shared across replicas -- a compromised/leaked assertion
/// could in principle be replayed once against *each* replica within its
/// TTL window (default 60s) before naturally expiring. This is bounded risk,
/// not an open one: (1) the assertion's own destination binding
/// (`destination_matches`) means a replay can only ever reach the exact
/// host/port it was already scoped to, never a different target; (2) the
/// TTL ceiling (`ASSERTION_MAX_TTL_SECONDS`, default 60s) bounds the replay
/// window to, at most, a handful of seconds per additional replica; (3) a
/// legitimate caller never needs to replay -- it mints a fresh assertion
/// per call. Closing this fully requires a shared store (Valkey `SET NX EX`
/// keyed by `jti`, ttl = `exp - now`) -- deferred as a follow-up
/// ([`ReplayCache`] is a trait specifically so that swap is a new impl, not
/// a call-site rewrite) rather than blocking this landing on standing up a
/// shared Valkey deployment for every environment this proxy runs in.
pub trait ReplayCache: Send + Sync {
    /// Records `jti` (expiring at `exp`) and returns `Ok(())` the first
    /// time it's seen, or `Err(AssertionError::Replayed)` on any
    /// subsequent attempt before it expires.
    fn check_and_record(&self, jti: &str, exp: u64) -> Result<(), AssertionError>;
}

/// The only [`ReplayCache`] wired today -- see the trait doc for the
/// accepted multi-replica tradeoff.
#[derive(Default)]
pub struct InMemoryReplayCache {
    seen: Mutex<HashMap<String, u64>>,
}

impl InMemoryReplayCache {
    pub fn new() -> Self {
        Self::default()
    }
}

impl ReplayCache for InMemoryReplayCache {
    fn check_and_record(&self, jti: &str, exp: u64) -> Result<(), AssertionError> {
        let now = now_secs();
        let mut seen = self.seen.lock().unwrap_or_else(|e| e.into_inner());
        // Opportunistic prune on every call -- keeps this map bounded by
        // "assertions seen in the last max-TTL window", never unbounded.
        seen.retain(|_, expires_at| *expires_at > now);
        if seen.contains_key(jti) {
            return Err(AssertionError::Replayed(jti.to_string()));
        }
        seen.insert(jti.to_string(), exp);
        Ok(())
    }
}

/// Verifies signature (against the *calling service's own* key in
/// `trust_bundle`, identified by the token's `kid` -- same trust bundle
/// `auth::authenticate` uses for the machine JWT), `exp` (with leeway) and
/// the max-TTL ceiling (defense in depth against a compromised/misconfigured
/// caller minting a long-lived assertion). Does **not** check replay --
/// callers that need that property use [`verify_from_header`] instead; this
/// bare form exists for the best-effort audit-context path in
/// `crate::proxy::handle_inner`, which must never itself consume a replay
/// slot for a request that already failed validation elsewhere.
pub async fn verify(
    token: &str,
    trust_bundle: &dyn TrustBundle,
    max_ttl_secs: u64,
) -> Result<EgressAssertion, AssertionError> {
    let header = jsonwebtoken::decode_header(token)
        .map_err(|e| AssertionError::Invalid(format!("malformed header: {e}")))?;
    let kid = header.kid.clone();
    let Some(kid_value) = kid.as_deref() else {
        return Err(AssertionError::UnknownKeyId(None));
    };
    let Some(key) = trust_bundle.public_key(kid_value).await else {
        return Err(AssertionError::UnknownKeyId(kid));
    };

    let mut validation = Validation::new(Algorithm::EdDSA);
    validation.leeway = ASSERTION_CLOCK_SKEW_SECONDS;
    validation.set_required_spec_claims(&["exp", "iat"]);
    // No `aud`/`iss` check: this token has exactly one purpose and one
    // verifier (this proxy), unlike the machine JWT.
    validation.validate_aud = false;

    let data = jsonwebtoken::decode::<EgressAssertion>(token, &key, &validation)
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

/// Extracts the assertion token from `headers`, verifies it (see
/// [`verify`]), and enforces single-use via `replay_cache`. This is the
/// path `crate::proxy::validate` uses for the real authorization decision;
/// [`verify`] alone is for non-authorizing, best-effort contexts only.
pub async fn verify_from_header(
    headers: &http::HeaderMap,
    trust_bundle: &dyn TrustBundle,
    max_ttl_secs: u64,
    replay_cache: &dyn ReplayCache,
) -> Result<EgressAssertion, AssertionError> {
    let token = headers
        .get(ASSERTION_HEADER)
        .and_then(|v| v.to_str().ok())
        .ok_or(AssertionError::Missing)?;
    let claims = verify(token, trust_bundle, max_ttl_secs).await?;
    replay_cache.check_and_record(&claims.jti, claims.exp)?;
    Ok(claims)
}

/// Step 4 of the pipeline (spec-equivalent to `bundle_host_http`'s
/// declared-host check): does the *requested* destination (host **and**
/// port) match what the assertion actually grants, before any DNS
/// resolution happens. The port check is exact -- a grant for `:443` never
/// authorizes the same host on a different (even operator-allowlisted)
/// port.
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
