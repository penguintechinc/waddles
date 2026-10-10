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
//! - [`destination_matches`]: re-exported directly from `egress_assertion`
//!   -- that crate's own version is now CIDR-aware (a `PrivateIp` grant may
//!   be a literal or a `192.168.1.0/24`-shaped CIDR, per the connector
//!   spec's `net.http.private-ip:<ip|cidr>` syntax), so this crate no
//!   longer needs a local override. Previously duplicated here because the
//!   shared crate's original version was plain string equality (couldn't
//!   match an IP dial target against a CIDR grant at all); now there is
//!   exactly one implementation, shared by both sides of the hop.
//! - [`resolved_matches`]: the DNS-rebinding re-check against the
//!   *resolved* address, not just the requested host literal --
//!   `egress_assertion` deliberately doesn't own DNS-resolution concepts,
//!   so this check stays here alongside `crate::proxy::validate`, the only
//!   caller. Delegates its own IP/CIDR containment logic to
//!   `egress_assertion::ip_matches_grant` -- the same primitive
//!   `destination_matches` uses -- so the two checks can never drift apart
//!   on what counts as "inside the grant".
//!
//! Header: `X-Waddles-Egress-Assertion: <compact EdDSA JWT>` (re-exported
//! from `egress_assertion` as [`ASSERTION_HEADER`]).

use std::collections::HashMap;
use std::sync::Mutex;

use std::time::Instant;

use service_auth::jwt_hardening::{
    inspect_header, report_outcome, JwtMetrics, KidPolicy, OUTCOME_OK, REASON_INVALID,
    REASON_INVALID_CLAIM, REASON_UNKNOWN_KID, VERIFIER_EGRESS_ASSERTION,
};
use service_auth::TrustBundle;

pub use egress_assertion::{
    destination_matches, DestinationCategory, EgressAssertion, ASSERTION_HEADER,
};

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

/// The assertion's one signing algorithm (RFC 8725 one-alg-per-verifier).
const ASSERTION_ALGORITHM: &str = "EdDSA";

/// Outcome of verifying one assertion token, with the closed-vocabulary
/// `alg` / `outcome` labels the metric and log line need.
struct Checked {
    result: Result<EgressAssertion, AssertionError>,
    alg: &'static str,
    outcome: &'static str,
}

impl Checked {
    fn rejected(error: AssertionError, alg: &'static str, outcome: &'static str) -> Self {
        Self {
            result: Err(error),
            alg,
            outcome,
        }
    }
}

/// Every check, in order, with no metrics or logging: JOSE header vetting
/// (`alg` must be exactly EdDSA; `alg: none`, `jku`/`jwk`/`x5u`/`x5c`/`crit`
/// and a hostile `kid` are refused before any key lookup), `kid` resolution
/// against `trust_bundle`, then signature / claims / TTL through
/// `egress_assertion::verify_with_key`.
async fn check(token: &str, trust_bundle: &dyn TrustBundle, max_ttl_secs: u64) -> Checked {
    let header = match inspect_header(token, &[ASSERTION_ALGORITHM], KidPolicy::Vet) {
        Ok(header) => header,
        Err(rejection) => {
            return Checked::rejected(
                AssertionError::Assertion(egress_assertion::AssertionError::Rejected(
                    rejection.reason,
                )),
                rejection.alg,
                rejection.reason,
            );
        }
    };
    let Some(kid) = header.kid else {
        return Checked::rejected(
            AssertionError::UnknownKeyId(None),
            "eddsa",
            REASON_UNKNOWN_KID,
        );
    };
    let Some(key) = trust_bundle.public_key(&kid).await else {
        return Checked::rejected(
            AssertionError::UnknownKeyId(Some(kid)),
            "eddsa",
            REASON_UNKNOWN_KID,
        );
    };

    match egress_assertion::verify_with_key(token, &key, max_ttl_secs) {
        Ok(claims) => Checked {
            result: Ok(claims),
            alg: "eddsa",
            outcome: OUTCOME_OK,
        },
        Err(error) => {
            let outcome = match &error {
                egress_assertion::AssertionError::Rejected(reason) => *reason,
                egress_assertion::AssertionError::TtlTooLong { .. } => REASON_INVALID_CLAIM,
                _ => REASON_INVALID,
            };
            Checked::rejected(AssertionError::Assertion(error), "eddsa", outcome)
        }
    }
}

/// Verifies signature (against the *calling service's own* key in
/// `trust_bundle`, identified by the token's `kid` -- same trust bundle
/// `auth::authenticate` uses for the machine JWT), the pinned algorithm,
/// required claims (non-empty `sub`/`tenant`/`jti`), `exp`/`iat` (with
/// leeway) and the max-TTL ceiling (defense in depth against a
/// compromised/misconfigured caller minting a long-lived assertion) via
/// `egress_assertion::verify_with_key`. Does **not** check replay --
/// callers that need that property use [`verify_from_header`] instead; this
/// bare form exists for the best-effort audit-context path in
/// `crate::proxy::handle_inner`, which must never itself consume a replay
/// slot for a request that already failed validation elsewhere. It also
/// emits no `waddles_jwt_verifications_total` sample: the same token is
/// verified authoritatively by [`verify_from_header`], and counting it twice
/// would inflate the stream.
pub async fn verify(
    token: &str,
    trust_bundle: &dyn TrustBundle,
    max_ttl_secs: u64,
) -> Result<EgressAssertion, AssertionError> {
    check(token, trust_bundle, max_ttl_secs).await.result
}

/// [`verify`] plus exactly one `waddles_jwt_verifications_total` sample
/// (`verifier=egress_assertion`) and, for a rejection, one PII-free log line.
async fn verify_and_report(
    metrics: &JwtMetrics,
    token: &str,
    trust_bundle: &dyn TrustBundle,
    max_ttl_secs: u64,
) -> Result<EgressAssertion, AssertionError> {
    let started = Instant::now();
    let checked = check(token, trust_bundle, max_ttl_secs).await;
    report_outcome(
        metrics,
        VERIFIER_EGRESS_ASSERTION,
        started,
        checked.alg,
        checked.outcome,
    );
    checked.result
}

/// Extracts the assertion token from `headers`, verifies it (see
/// [`verify`]), and enforces single-use via `replay_cache`. This is the
/// path `crate::proxy::validate` uses for the real authorization decision;
/// [`verify`] alone is for non-authorizing, best-effort contexts only.
/// A missing header is not a verification (there is no token to count).
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
    let claims = verify_and_report(JwtMetrics::global(), token, trust_bundle, max_ttl_secs).await?;
    replay_cache.check_and_record(&claims.jti, claims.exp)?;
    Ok(claims)
}

/// Whether the resolved address itself still satisfies the assertion's
/// category (guards against DNS rebinding: an `Fqdn` grant only ever
/// authorizes the address `is_forbidden_address`/`ip_policy::is_denied`
/// would already treat as public -- this check is a second, explicit
/// belt-and-suspenders gate specifically for the private-ip CIDR case,
/// where the *resolved* address, not just the requested literal, must
/// fall in the granted range). Deliberately stays in this crate rather
/// than `egress_assertion` -- the shared crate has no DNS-resolution
/// concept at all, and this is the only caller. The actual IP/CIDR
/// containment check delegates to `egress_assertion::ip_matches_grant` --
/// the same primitive [`destination_matches`] uses for the *requested*
/// literal -- so the requested-vs-resolved checks can never diverge on
/// what counts as "inside the grant".
pub fn resolved_matches(assertion: &EgressAssertion, resolved: std::net::IpAddr) -> bool {
    match assertion.category {
        DestinationCategory::Fqdn => true, // enforced by ip_policy::is_denied instead
        DestinationCategory::PublicIp | DestinationCategory::PrivateIp => {
            egress_assertion::ip_matches_grant(assertion.category, &assertion.destination, resolved)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use base64::engine::general_purpose::URL_SAFE_NO_PAD;
    use base64::Engine as _;
    use egress_assertion::reason as assertion_reason;
    use jsonwebtoken::{Algorithm, DecodingKey, EncodingKey, Header};
    use serde_json::{json, Value};
    use service_auth::jwt_hardening::{
        ALL_REASONS, FORBIDDEN_HEADER_PARAMS, REASON_ALG_MISMATCH, REASON_ALG_NONE, REASON_BAD_KID,
        REASON_BAD_SIGNATURE, REASON_EXPIRED, REASON_FORBIDDEN_HEADER, REASON_IMMATURE,
        REASON_MALFORMED, REASON_MISSING_CLAIM,
    };
    use service_auth::test_support::Capture;
    use std::sync::atomic::{AtomicUsize, Ordering};

    // Fixed, test-only Ed25519 keypairs (same fixture as `core/service_auth`).
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

    const MAX_TTL: u64 = 60;

    /// Trust bundle with one key (`k1`) that counts lookups, to prove a
    /// hostile header never reaches key resolution.
    struct Bundle {
        lookups: AtomicUsize,
    }

    #[async_trait::async_trait]
    impl TrustBundle for Bundle {
        async fn public_key(&self, kid: &str) -> Option<DecodingKey> {
            self.lookups.fetch_add(1, Ordering::SeqCst);
            (kid == "k1").then(|| DecodingKey::from_ed_der(KEY_A_PUB_RAW))
        }
    }

    fn bundle() -> Bundle {
        Bundle {
            lookups: AtomicUsize::new(0),
        }
    }

    fn b64(value: &Value) -> String {
        URL_SAFE_NO_PAD.encode(value.to_string())
    }

    fn now() -> u64 {
        egress_assertion::now_secs()
    }

    fn claims() -> Value {
        json!({
            "sub": "spiffe://penguintech.io/alpha/svc-process",
            "tenant": "tenant-a", "community": "community-a", "app": "app-a",
            "category": "fqdn", "destination": "discord.com", "port": 443,
            "jti": "jti-1", "iat": now(), "exp": now() + 30,
        })
    }

    fn good_header() -> Value {
        json!({"alg": "EdDSA", "typ": "JWT", "kid": "k1"})
    }

    fn sign_with(key_der: &[u8], header: &Value, claims: &Value) -> String {
        let message = format!("{}.{}", b64(header), b64(claims));
        let signature = jsonwebtoken::crypto::sign(
            message.as_bytes(),
            &EncodingKey::from_ed_der(key_der),
            Algorithm::EdDSA,
        )
        .expect("sign");
        format!("{message}.{signature}")
    }

    fn raw_token(header: &Value, claims: &Value) -> String {
        sign_with(KEY_A_PRIV_DER, header, claims)
    }

    /// Run the reporting verifier against a capturing meter provider.
    async fn run(
        token: &str,
        bundle: &Bundle,
    ) -> (Result<EgressAssertion, AssertionError>, Capture) {
        let capture = Capture::new();
        let result = verify_and_report(&capture.metrics, token, bundle, MAX_TTL).await;
        (result, capture)
    }

    /// Assert `token` is refused with `outcome` under `alg`, counted exactly
    /// once as `egress_assertion`, and (iff `before_lookup`) never resolved a key.
    async fn assert_refused(token: &str, outcome: &str, alg: &str, before_lookup: bool) {
        let bundle = bundle();
        let (result, capture) = run(token, &bundle).await;
        assert!(result.is_err(), "{outcome} must be refused");
        assert_eq!(
            capture.count("egress_assertion", alg, outcome),
            1,
            "{outcome}/{alg}"
        );
        assert_eq!(capture.total(), 1, "exactly one verification recorded");
        if before_lookup {
            assert_eq!(
                bundle.lookups.load(Ordering::SeqCst),
                0,
                "{outcome} reached the trust bundle"
            );
        }
    }

    #[tokio::test]
    async fn a_valid_assertion_is_counted_ok_as_eddsa() {
        let bundle = bundle();
        let (result, capture) = run(&raw_token(&good_header(), &claims()), &bundle).await;
        let verified = result.expect("verifies");
        assert_eq!(verified.tenant, "tenant-a");
        assert_eq!(capture.count("egress_assertion", "eddsa", "ok"), 1);
        assert_eq!(capture.total(), 1);
        assert_eq!(capture.points("waddles_jwt_verification_seconds").len(), 1);
    }

    #[tokio::test]
    async fn alg_none_is_refused_in_every_letter_case_before_key_lookup() {
        for alg in ["none", "None", "NONE"] {
            let token = format!(
                "{}.{}.",
                b64(&json!({"alg": alg, "kid": "k1"})),
                b64(&claims())
            );
            assert_refused(&token, REASON_ALG_NONE, "none", true).await;
        }
    }

    #[tokio::test]
    async fn alg_confusion_hmac_with_the_public_key_is_refused_before_key_lookup() {
        for (alg, label) in [
            (Algorithm::HS256, "hs256"),
            (Algorithm::HS384, "hs384"),
            (Algorithm::HS512, "hs512"),
        ] {
            let mut header = Header::new(alg);
            header.kid = Some("k1".into());
            let token =
                jsonwebtoken::encode(&header, &claims(), &EncodingKey::from_secret(KEY_A_PUB_RAW))
                    .expect("encode");
            assert_refused(&token, REASON_ALG_MISMATCH, label, true).await;
        }
        let header = json!({"alg": "RS256", "kid": "k1"});
        assert_refused(
            &raw_token(&header, &claims()),
            REASON_ALG_MISMATCH,
            "rs256",
            true,
        )
        .await;
    }

    #[tokio::test]
    async fn key_material_headers_are_refused_even_with_a_valid_signature() {
        for param in FORBIDDEN_HEADER_PARAMS {
            let mut header = good_header();
            header[param] = json!("https://attacker.example/keys");
            assert_refused(
                &raw_token(&header, &claims()),
                REASON_FORBIDDEN_HEADER,
                "eddsa",
                true,
            )
            .await;
        }
    }

    #[tokio::test]
    async fn hostile_kids_never_reach_the_trust_bundle() {
        for kid in [
            json!("k1;x"),
            json!("../k1"),
            json!(1),
            json!("k".repeat(65)),
        ] {
            let mut header = good_header();
            header["kid"] = kid;
            assert_refused(
                &raw_token(&header, &claims()),
                REASON_BAD_KID,
                "eddsa",
                true,
            )
            .await;
        }
    }

    #[tokio::test]
    async fn absent_and_unknown_kid_are_unknown_kid() {
        let bundle = bundle();
        let (result, capture) = run(&raw_token(&json!({"alg": "EdDSA"}), &claims()), &bundle).await;
        assert!(matches!(result, Err(AssertionError::UnknownKeyId(None))));
        assert_eq!(capture.count("egress_assertion", "eddsa", "unknown_kid"), 1);
        assert_eq!(bundle.lookups.load(Ordering::SeqCst), 0);

        let header = json!({"alg": "EdDSA", "kid": "retired"});
        let (result, capture) = run(&raw_token(&header, &claims()), &bundle).await;
        assert!(matches!(&result, Err(AssertionError::UnknownKeyId(Some(k))) if k == "retired"));
        assert_eq!(capture.count("egress_assertion", "eddsa", "unknown_kid"), 1);
        assert_eq!(bundle.lookups.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn malformed_tokens_are_counted_with_no_alg() {
        for token in ["", "not-a-jwt", "a.b", "a.b.c.d", "%%%.x.y"] {
            assert_refused(token, REASON_MALFORMED, "absent", true).await;
        }
    }

    #[tokio::test]
    async fn claim_level_failures_are_named() {
        let mut empty_tenant = claims();
        empty_tenant["tenant"] = json!("");
        let mut no_jti = claims();
        no_jti.as_object_mut().expect("object").remove("jti");
        let mut expired = claims();
        expired["iat"] = json!(now() - 300);
        expired["exp"] = json!(now() - 200);
        let mut future = claims();
        future["iat"] = json!(now() + 300);
        future["exp"] = json!(now() + 330);
        let mut long_ttl = claims();
        long_ttl["exp"] = json!(now() + 3600);

        for (claims, outcome) in [
            (empty_tenant, "invalid_claim"),
            (no_jti, REASON_MISSING_CLAIM),
            (expired, REASON_EXPIRED),
            (future, REASON_IMMATURE),
            (long_ttl, "invalid_claim"),
        ] {
            assert_refused(&raw_token(&good_header(), &claims), outcome, "eddsa", false).await;
        }
    }

    #[tokio::test]
    async fn a_forged_signature_is_bad_signature() {
        let token = sign_with(KEY_B_PRIV_DER, &good_header(), &claims());
        assert_refused(&token, REASON_BAD_SIGNATURE, "eddsa", false).await;
    }

    #[tokio::test]
    async fn the_bare_verify_does_not_report_and_does_not_log_a_second_time() {
        let bundle = bundle();
        let token = raw_token(&good_header(), &claims());
        let capture = Capture::new();
        verify(&token, &bundle, MAX_TTL).await.expect("verifies");
        let none = format!("{}.{}.", b64(&json!({"alg": "none"})), b64(&claims()));
        assert!(verify(&none, &bundle, MAX_TTL).await.is_err());
        assert_eq!(capture.total(), 0, "the audit-context path is not counted");
    }

    #[tokio::test]
    async fn verify_from_header_reports_once_and_enforces_replay() {
        let capture = Capture::new();
        let bundle = bundle();
        let token = raw_token(&good_header(), &claims());
        let mut headers = http::HeaderMap::new();
        headers.insert(ASSERTION_HEADER, token.parse().expect("header value"));
        let cache = InMemoryReplayCache::new();
        let first = verify_and_report(&capture.metrics, &token, &bundle, MAX_TTL)
            .await
            .expect("first use");
        cache
            .check_and_record(&first.jti, first.exp)
            .expect("first");
        assert!(matches!(
            cache.check_and_record(&first.jti, first.exp),
            Err(AssertionError::Replayed(_))
        ));
        assert_eq!(capture.count("egress_assertion", "eddsa", "ok"), 1);

        // Missing header: not a verification at all.
        let empty = http::HeaderMap::new();
        assert!(matches!(
            verify_from_header(&empty, &bundle, MAX_TTL, &cache).await,
            Err(AssertionError::Missing)
        ));
        assert_eq!(capture.total(), 1);
        // Public entry point against the (no-op) global provider still decides.
        verify_from_header(&headers, &bundle, MAX_TTL, &InMemoryReplayCache::new())
            .await
            .expect("global path verifies");
    }

    #[test]
    fn every_reason_egress_assertion_can_emit_is_in_the_shared_vocabulary() {
        // `egress_assertion` repeats the reason literals (it deliberately has
        // no `service_auth` dependency); this pins them to the vocabulary the
        // Python verifiers and the metric stream use.
        for reason in assertion_reason::ALL {
            assert!(
                ALL_REASONS.contains(&reason),
                "{reason} is not a shared reason"
            );
        }
    }
}
