//! Server-side verification of the signed per-request allowlist assertion.
//!
//! The wire format itself (`EgressAssertion`, `DestinationCategory`, the
//! header name, and the dependency-free `verify_with_key` primitive) now
//! lives in `egress_assertion` -- a crate shared with `core/
//! bundle_host_http`'s client-side signer (PR #468) so the two sides of
//! this hop can never silently drift apart. This module keeps only what's
//! specific to being the *verifying* side of that hop:
//!
//! - JWKS `kid` lookup: [`verify`] reads the assertion's `kid` header and
//!   resolves it against the same [`TrustBundle`] `auth::authenticate`
//!   already uses for the machine JWT -- the calling service signs both
//!   tokens with its own per-service Ed25519 key (PR #438's per-service
//!   JWKS), so there is no second, separately-mounted static key here.
//! - Replay protection: [`ReplayCache`]/[`InMemoryReplayCache`], scoped to
//!   this proxy instance (see that trait's doc for the accepted
//!   multi-replica tradeoff).
//! - [`destination_matches`]: unlike `egress_assertion::destination_matches`
//!   (a plain string-equality check, all `bundle_host_http`'s single-host
//!   guard ever needs), this proxy's `PrivateIp` grants are CIDRs
//!   (`192.168.1.0/24`), not just literals -- the requested host must be
//!   parsed as an IP and checked for CIDR containment. Kept as a local,
//!   proxy-specific override rather than the shared crate's simpler
//!   version.
//! - [`resolved_matches`]: the DNS-rebinding re-check against the
//!   *resolved* address, not just the requested host literal --
//!   `egress_assertion` deliberately doesn't own this (it has no DNS
//!   concept at all), so it stays here alongside `crate::proxy::validate`,
//!   the only caller.
//!
//! Header: `X-Waddles-Egress-Assertion: <compact EdDSA JWT>` (re-exported
//! from `egress_assertion` as [`ASSERTION_HEADER`]).

use std::collections::HashMap;
use std::sync::Mutex;

use service_auth::TrustBundle;

pub use egress_assertion::{DestinationCategory, EgressAssertion, ASSERTION_HEADER};

#[derive(thiserror::Error, Debug)]
pub enum AssertionError {
    #[error("missing assertion header")]
    Missing,
    #[error(transparent)]
    Assertion(#[from] egress_assertion::AssertionError),
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
/// TTL ceiling (`egress_assertion::ASSERTION_MAX_TTL_SECONDS`, default 60s)
/// bounds the replay window to, at most, a handful of seconds per
/// additional replica; (3) a legitimate caller never needs to replay -- it
/// mints a fresh assertion per call. Closing this fully requires a shared
/// store (Valkey `SET NX EX` keyed by `jti`, ttl = `exp - now`) -- deferred
/// as a follow-up ([`ReplayCache`] is a trait specifically so that swap is
/// a new impl, not a call-site rewrite) rather than blocking this landing
/// on standing up a shared Valkey deployment for every environment this
/// proxy runs in.
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
        let now = egress_assertion::now_secs();
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
/// caller minting a long-lived assertion) via
/// `egress_assertion::verify_with_key`. Does **not** check replay --
/// callers that need that property use [`verify_from_header`] instead; this
/// bare form exists for the best-effort audit-context path in
/// `crate::proxy::handle_inner`, which must never itself consume a replay
/// slot for a request that already failed validation elsewhere.
pub async fn verify(
    token: &str,
    trust_bundle: &dyn TrustBundle,
    max_ttl_secs: u64,
) -> Result<EgressAssertion, AssertionError> {
    let header = jsonwebtoken::decode_header(token).map_err(|e| {
        AssertionError::Assertion(egress_assertion::AssertionError::Invalid(format!(
            "malformed header: {e}"
        )))
    })?;
    let kid = header.kid.clone();
    let Some(kid_value) = kid.as_deref() else {
        return Err(AssertionError::UnknownKeyId(None));
    };
    let Some(key) = trust_bundle.public_key(kid_value).await else {
        return Err(AssertionError::UnknownKeyId(kid));
    };

    let claims = egress_assertion::verify_with_key(token, &key, max_ttl_secs)?;
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
/// port. Deliberately a local override of
/// `egress_assertion::destination_matches`: this proxy's `PrivateIp`
/// grants are CIDRs the requested (and, in [`resolved_matches`], resolved)
/// address must fall within, not just an exact literal -- a distinction
/// `bundle_host_http`'s own single-host guard has no use for, so the
/// shared crate's version stays a plain string comparison.
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
/// fall in the granted range). Deliberately stays in this crate rather
/// than `egress_assertion` -- the shared crate has no DNS-resolution
/// concept at all, and this is the only caller.
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
