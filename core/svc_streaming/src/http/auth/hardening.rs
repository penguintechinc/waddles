//! JWT verification hardening for svc-streaming's platform (HS256) verifier
//! (RFC 8725) -- a deliberate, name-for-name MIRROR of
//! `core/service_auth/src/jwt_hardening.rs`.
//!
//! Why a mirror and not a dependency: this crate's Docker build context is
//! its own directory (`Dockerfile.rust`: "no shared libs to pull in"), so a
//! `path = "../service_auth"` dependency would need a build-context change
//! and drag `reqwest`/`async-trait` and a second `jsonwebtoken` major into
//! this binary. The reasons, verifier labels, algorithm labels, metric and
//! label names are therefore repeated here and pinned by golden-vector tests
//! that are identical in both crates. Both mirror
//! `libs/flask_core/flask_core/jwt_hardening.py`, so Python and Rust
//! verifiers report into ONE metric stream. Contract:
//! `docs/JWT_VERIFICATION.md`. Follow-up: publish one implementation as a
//! penguin-libs crate and delete the copies.
//!
//! * **One algorithm per verifier** (RFC 8725 section 3.1): the JOSE header
//!   `alg` is compared with the verifier's own allow-list *before* any
//!   cryptography, then the same single algorithm is pinned on the
//!   `jsonwebtoken` `Validation`. A token cannot pick its own algorithm.
//! * **`alg: none` is a hard reject**, in any letter case.
//! * **Key-material headers are refused** -- `jku`, `jwk`, `x5u`, `x5c`
//!   (RFC 7515 section 4.1) tell a naive verifier where to find the key,
//!   i.e. let the token choose its own trust anchor; `crit` is refused
//!   because no critical extension is understood (RFC 7515 section 4.1.11).
//! * **`kid` hygiene**: charset and length are pinned so a hostile value can
//!   never reach a trust-bundle lookup, a log line or a metric label.
//! * **Observability**: `waddles_jwt_verifications_total{verifier,alg,outcome}`
//!   and `waddles_jwt_verification_seconds{verifier,alg}`.
//!
//! Logging is PII-free by construction: only members of the closed
//! vocabularies below (`REASON_*`, `VERIFIER_*`, algorithm labels) are ever
//! rendered -- every logging/metric entry point takes `&'static str`, so a
//! token, a claim value, a header value or a library error text cannot be
//! passed in by accident.

use std::fmt;
use std::sync::OnceLock;
use std::time::Instant;

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine as _;
use opentelemetry::metrics::{Counter, Histogram, MeterProvider};
use opentelemetry::{global, KeyValue};
use serde_json::{Map, Value};
use tracing::{debug, error, warn};

/// `verifier` label: user/session platform JWT, HS256 shared secret
/// (identical value to the Python `VERIFIER_PLATFORM_HS256`).
pub const VERIFIER_PLATFORM_HS256: &str = "platform_hs256";
/// `verifier` label: per-service machine JWT, EdDSA, hub-api JWKS
/// (identical value to the Python `VERIFIER_SERVICE_EDDSA`).
pub const VERIFIER_SERVICE_EDDSA: &str = "service_eddsa";
/// `verifier` label: the short-lived, single-use egress assertion
/// (`core/egress_assertion`), EdDSA. Rust-only -- Python has no such token.
pub const VERIFIER_EGRESS_ASSERTION: &str = "egress_assertion";

/// `outcome` label for an accepted token.
pub const OUTCOME_OK: &str = "ok";
/// Rejection reason: not a decodable three-segment JWS / oversized header.
pub const REASON_MALFORMED: &str = "malformed";
/// Rejection reason: `alg: none` in any letter case.
pub const REASON_ALG_NONE: &str = "alg_none";
/// Rejection reason: `alg` absent, non-string, or not the verifier's one algorithm.
pub const REASON_ALG_MISMATCH: &str = "alg_mismatch";
/// Rejection reason: header carries `jku`/`jwk`/`x5u`/`x5c`/`crit`.
pub const REASON_FORBIDDEN_HEADER: &str = "forbidden_header";
/// Rejection reason: `kid` outside the pinned charset/length (or required and absent).
pub const REASON_BAD_KID: &str = "bad_kid";
/// Rejection reason: well-formed `kid` the trust bundle does not know (or absent).
pub const REASON_UNKNOWN_KID: &str = "unknown_kid";
/// Rejection reason: the verifier has no (or an empty) key/secret configured.
pub const REASON_NO_KEY: &str = "no_key";
/// Rejection reason: signature does not verify.
pub const REASON_BAD_SIGNATURE: &str = "bad_signature";
/// Rejection reason: `exp` has passed.
pub const REASON_EXPIRED: &str = "expired";
/// Rejection reason: `nbf`/`iat` is in the future beyond the skew allowance.
pub const REASON_IMMATURE: &str = "immature";
/// Rejection reason: `iss` is not (or no expected `iss` is configured as) trusted.
pub const REASON_BAD_ISSUER: &str = "bad_issuer";
/// Rejection reason: `aud` does not contain the expected audience.
pub const REASON_BAD_AUDIENCE: &str = "bad_audience";
/// Rejection reason: a required claim is missing.
pub const REASON_MISSING_CLAIM: &str = "missing_claim";
/// Rejection reason: a claim is present but empty or of the wrong type.
pub const REASON_INVALID_CLAIM: &str = "invalid_claim";
/// Rejection reason: signature valid, but the token lacks the required scope.
pub const REASON_SCOPE_DENIED: &str = "scope_denied";
/// Rejection reason: anything else (an unexpected library failure).
pub const REASON_INVALID: &str = "invalid";

/// Every `REASON_*` -- the closed set a rejection's `outcome` label is drawn
/// from (plus [`OUTCOME_OK`]). Used by cross-crate vocabulary checks.
pub const ALL_REASONS: [&str; 16] = [
    REASON_MALFORMED,
    REASON_ALG_NONE,
    REASON_ALG_MISMATCH,
    REASON_FORBIDDEN_HEADER,
    REASON_BAD_KID,
    REASON_UNKNOWN_KID,
    REASON_NO_KEY,
    REASON_BAD_SIGNATURE,
    REASON_EXPIRED,
    REASON_IMMATURE,
    REASON_BAD_ISSUER,
    REASON_BAD_AUDIENCE,
    REASON_MISSING_CLAIM,
    REASON_INVALID_CLAIM,
    REASON_SCOPE_DENIED,
    REASON_INVALID,
];

/// JOSE header parameters that let a token pick its own verification key
/// (see the module doc). Identical set to the Python `FORBIDDEN_HEADER_PARAMS`.
pub const FORBIDDEN_HEADER_PARAMS: [&str; 5] = ["jku", "jwk", "x5u", "x5c", "crit"];

/// `alg` label when the header carries no (or a JSON `null`) `alg`.
pub const ALG_LABEL_ABSENT: &str = "absent";
/// `alg` label for `alg: none`.
pub const ALG_LABEL_NONE: &str = "none";
/// `alg` label for any algorithm outside the closed set (bounds cardinality).
pub const ALG_LABEL_OTHER: &str = "other";

/// Counter instrument name -- identical to the Python verifiers'.
pub const METRIC_VERIFICATIONS: &str = "waddles_jwt_verifications_total";
/// Histogram instrument name -- identical to the Python verifiers'.
pub const METRIC_DURATION: &str = "waddles_jwt_verification_seconds";

/// Explicit histogram bucket boundaries (seconds). The OTel default
/// boundaries (0, 5, 10, 25 ... 10000) are millisecond-scale and would put
/// every sub-millisecond JWT verification into the first bucket.
pub const LATENCY_BOUNDARIES_SECONDS: [f64; 16] = [
    0.000_01, 0.000_05, 0.000_1, 0.000_25, 0.000_5, 0.001, 0.002_5, 0.005, 0.01, 0.025, 0.05, 0.1,
    0.25, 0.5, 1.0, 2.5,
];

/// A real JOSE header is ~50 bytes; anything this large is hostile or broken
/// (a pasted `x5c` chain, a decompression-style payload) and is refused
/// before JSON parsing.
const MAX_HEADER_SEGMENT_CHARS: usize = 16 * 1024;

/// Longest `kid` accepted (first char + up to 63 more).
const MAX_KID_LEN: usize = 64;

/// Algorithms that may appear as a metric label verbatim; anything else
/// collapses to `other` so an attacker-chosen `alg` can never mint
/// unbounded label values. Identical to the Python `_KNOWN_ALGS`.
const KNOWN_ALG_LABELS: [&str; 14] = [
    "hs256", "hs384", "hs512", "rs256", "rs384", "rs512", "ps256", "ps384", "ps512", "es256",
    "es384", "es512", "es256k", "eddsa",
];

/// Rejections that mean "somebody is probing" or "something is mis-issuing"
/// (the signature already verified for the claim-level ones) -> ERROR; the
/// rest is the ordinary noise of expired / garbage tokens -> WARN.
const ALARMING_REASONS: [&str; 9] = [
    REASON_ALG_NONE,
    REASON_ALG_MISMATCH,
    REASON_FORBIDDEN_HEADER,
    REASON_BAD_KID,
    REASON_BAD_SIGNATURE,
    REASON_BAD_ISSUER,
    REASON_BAD_AUDIENCE,
    REASON_MISSING_CLAIM,
    REASON_INVALID_CLAIM,
];

/// A token failed verification; `reason` is a `REASON_*` constant and `alg`
/// a label-safe algorithm label -- never token data. `Display` renders the
/// reason only, so a handler that stringifies it cannot leak claims, header
/// values or key material.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct JwtRejection {
    /// Closed-vocabulary rejection reason (the `outcome` metric label).
    pub reason: &'static str,
    /// Bounded algorithm label (`hs256` ... `eddsa`, `none`, `absent`, `other`).
    pub alg: &'static str,
}

impl JwtRejection {
    /// Build a rejection with a reason and an already label-safe `alg`.
    pub const fn new(reason: &'static str, alg: &'static str) -> Self {
        Self { reason, alg }
    }

    /// Build a rejection raised before any `alg` was read (label `absent`).
    pub const fn without_alg(reason: &'static str) -> Self {
        Self::new(reason, ALG_LABEL_ABSENT)
    }
}

impl fmt::Display for JwtRejection {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.reason)
    }
}

impl std::error::Error for JwtRejection {}

/// The vetted subset of a JOSE header a verifier is allowed to act on.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JoseHeader {
    /// The header `alg`, exactly one of the verifier's allow-list.
    pub alg: String,
    /// The `kid`, if present and within the pinned charset/length.
    pub kid: Option<String>,
}

/// How a verifier treats the header `kid`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KidPolicy {
    /// Do not look at `kid` at all.
    Ignore,
    /// A present `kid` must match the pinned charset/length; absence is tolerated.
    Vet,
    /// `kid` must be present AND match the pinned charset/length.
    Require,
}

/// Collapse an arbitrary header `alg` (`None` = absent/null) to a bounded,
/// log- and metric-safe label from the closed set.
pub fn alg_label(alg: Option<&str>) -> &'static str {
    let Some(alg) = alg else {
        return ALG_LABEL_ABSENT;
    };
    let lowered = alg.to_ascii_lowercase();
    if lowered == ALG_LABEL_NONE {
        return ALG_LABEL_NONE;
    }
    if lowered == ALG_LABEL_ABSENT {
        return ALG_LABEL_ABSENT;
    }
    KNOWN_ALG_LABELS
        .iter()
        .find(|known| **known == lowered)
        .copied()
        .unwrap_or(ALG_LABEL_OTHER)
}

/// Return true if `kid` is within the pinned charset and length
/// (`[A-Za-z0-9_][A-Za-z0-9_.:-]{0,63}`).
pub fn is_valid_kid(kid: &str) -> bool {
    let mut chars = kid.chars();
    let Some(first) = chars.next() else {
        return false;
    };
    if !(first.is_ascii_alphanumeric() || first == '_') {
        return false;
    }
    kid.len() <= MAX_KID_LEN
        && chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | ':' | '-'))
}

/// Decode the first compact-JWS segment ourselves, as a JSON object, or
/// reject as malformed.
///
/// Deliberately NOT `jsonwebtoken::decode_header`: it maps `alg: none` and
/// unknown algorithms to an anonymous parse error, which would collapse a
/// named attack into "malformed" in the metric, and it has no view of `crit`.
fn decode_header_object(token: &str) -> Result<Map<String, Value>, JwtRejection> {
    let mut segments = token.split('.');
    let (Some(header), Some(_payload), Some(_signature), None) = (
        segments.next(),
        segments.next(),
        segments.next(),
        segments.next(),
    ) else {
        return Err(JwtRejection::without_alg(REASON_MALFORMED));
    };
    if header.is_empty() || header.len() > MAX_HEADER_SEGMENT_CHARS {
        return Err(JwtRejection::without_alg(REASON_MALFORMED));
    }
    let Ok(raw) = URL_SAFE_NO_PAD.decode(header) else {
        return Err(JwtRejection::without_alg(REASON_MALFORMED));
    };
    // serde_json bounds nesting depth (128), so a deeply nested header is an
    // error here rather than a stack overflow.
    match serde_json::from_slice::<Value>(&raw) {
        Ok(Value::Object(map)) => Ok(map),
        _ => Err(JwtRejection::without_alg(REASON_MALFORMED)),
    }
}

/// Vet a token's unverified JOSE header and return the safe subset.
///
/// Order matters: `alg: none` is named first, then key-material parameters,
/// then the algorithm allow-list, then `kid` -- the first failure wins so the
/// metric names the most dangerous property of the token. Returns
/// `Err(JwtRejection)` for any header a verifier should not act on.
pub fn inspect_header(
    token: &str,
    allowed_algs: &[&str],
    kid_policy: KidPolicy,
) -> Result<JoseHeader, JwtRejection> {
    let header = decode_header_object(token)?;

    let raw_alg = header.get("alg").filter(|value| !value.is_null());
    let label = match raw_alg {
        None => ALG_LABEL_ABSENT,
        Some(Value::String(alg)) => alg_label(Some(alg)),
        Some(_) => ALG_LABEL_OTHER,
    };
    if matches!(raw_alg, Some(Value::String(alg)) if alg.eq_ignore_ascii_case("none")) {
        return Err(JwtRejection::new(REASON_ALG_NONE, ALG_LABEL_NONE));
    }
    if FORBIDDEN_HEADER_PARAMS
        .iter()
        .any(|param| header.contains_key(*param))
    {
        return Err(JwtRejection::new(REASON_FORBIDDEN_HEADER, label));
    }
    // Case-sensitive on purpose: "eddsa" is not "EdDSA".
    let Some(Value::String(alg)) = raw_alg else {
        return Err(JwtRejection::new(REASON_ALG_MISMATCH, label));
    };
    if !allowed_algs.contains(&alg.as_str()) {
        return Err(JwtRejection::new(REASON_ALG_MISMATCH, label));
    }

    let kid = match (
        header.get("kid").filter(|value| !value.is_null()),
        kid_policy,
    ) {
        (None, KidPolicy::Require) => {
            return Err(JwtRejection::new(REASON_BAD_KID, label));
        }
        (None, _) => None,
        (Some(_), KidPolicy::Ignore) => None,
        (Some(Value::String(kid)), _) if is_valid_kid(kid) => Some(kid.clone()),
        (Some(_), _) => return Err(JwtRejection::new(REASON_BAD_KID, label)),
    };
    Ok(JoseHeader {
        alg: alg.clone(),
        kid,
    })
}

/// The verification counter + latency histogram, bound to one meter provider.
#[derive(Clone)]
pub struct JwtMetrics {
    verifications: Counter<u64>,
    duration: Histogram<f64>,
}

impl JwtMetrics {
    /// Build the instruments on `provider` (tests inject an in-memory SDK
    /// provider; production uses [`JwtMetrics::global`]).
    pub fn new(provider: &dyn MeterProvider) -> Self {
        let meter = provider.meter("waddles.service_auth.jwt");
        Self {
            verifications: meter
                .u64_counter(METRIC_VERIFICATIONS)
                .with_unit("{verification}")
                .with_description(
                    "JWT verifications by verifier, header algorithm label and outcome. Watch \
                     alg=hs256 drain and alg=es256 rise across the asymmetric cutover; any \
                     alg=none/other or outcome=alg_*/forbidden_header is a probe.",
                )
                .build(),
            duration: meter
                .f64_histogram(METRIC_DURATION)
                .with_unit("s")
                .with_description(
                    "Wall time verifying one JWT, by verifier and header algorithm label.",
                )
                .with_boundaries(LATENCY_BOUNDARIES_SECONDS.to_vec())
                .build(),
        }
    }

    /// The process-wide instruments, bound to the global meter provider on
    /// first use. Verification happens at request time, after the service's
    /// telemetry bootstrap installed its provider; before that the
    /// instruments would be no-ops (which never affects a verdict).
    pub fn global() -> &'static JwtMetrics {
        static GLOBAL: OnceLock<JwtMetrics> = OnceLock::new();
        GLOBAL.get_or_init(|| JwtMetrics::new(global::meter_provider().as_ref()))
    }

    /// Record one verification: bump the counter and observe the elapsed
    /// time since `started`. Infallible by construction -- a dead exporter
    /// can never change an authentication verdict.
    pub fn record(
        &self,
        verifier: &'static str,
        alg: &'static str,
        outcome: &'static str,
        started: Instant,
    ) {
        self.verifications.add(
            1,
            &[
                KeyValue::new("verifier", verifier),
                KeyValue::new("alg", alg),
                KeyValue::new("outcome", outcome),
            ],
        );
        self.duration.record(
            started.elapsed().as_secs_f64(),
            &[
                KeyValue::new("verifier", verifier),
                KeyValue::new("alg", alg),
            ],
        );
    }
}

/// Log one rejected token: closed-vocabulary fields only, never token data.
/// `no_key` (a deployment bug that would otherwise verify forgeable tokens)
/// carries `severity=critical`; probing/mis-issuing reasons log at ERROR;
/// ordinary expired/garbage tokens at WARN.
pub fn log_rejection(verifier: &'static str, reason: &'static str, alg: &'static str) {
    if reason == REASON_NO_KEY {
        error!(
            verifier,
            reason,
            alg,
            severity = "critical",
            "JWT rejected: verifier has no signing key configured"
        );
    } else if ALARMING_REASONS.contains(&reason) {
        error!(verifier, reason, alg, "JWT rejected");
    } else {
        warn!(verifier, reason, alg, "JWT rejected");
    }
}

/// Emit the metric and the log line for one finished verification:
/// `outcome` is [`OUTCOME_OK`] or a `REASON_*`. Successes log at DEBUG only.
pub fn report_outcome(
    metrics: &JwtMetrics,
    verifier: &'static str,
    started: Instant,
    alg: &'static str,
    outcome: &'static str,
) {
    metrics.record(verifier, alg, outcome, started);
    if outcome == OUTCOME_OK {
        debug!(verifier, alg, "JWT verified");
    } else {
        log_rejection(verifier, outcome, alg);
    }
}

#[cfg(test)]
/// In-memory metrics capture for tests: an SDK meter provider whose exporter
/// snapshots every data point, so a test can assert the exact instrument
/// names, units and label sets without any network or global state.
pub(crate) mod test_support {
    use std::collections::BTreeMap;
    use std::sync::{Arc, Mutex};
    use std::time::Duration;

    use opentelemetry_sdk::error::OTelSdkResult;
    use opentelemetry_sdk::metrics::data::{AggregatedMetrics, MetricData, ResourceMetrics};
    use opentelemetry_sdk::metrics::exporter::PushMetricExporter;
    use opentelemetry_sdk::metrics::{PeriodicReader, SdkMeterProvider, Temporality};

    use super::{JwtMetrics, METRIC_VERIFICATIONS};

    /// One exported data point: counter value, or histogram observation count.
    #[derive(Debug, Clone, PartialEq)]
    pub struct Point {
        /// Instrument name (`waddles_jwt_verifications_total` / `..._seconds`).
        pub name: String,
        /// Instrument unit.
        pub unit: String,
        /// Exact label set, name -> value.
        pub attrs: BTreeMap<String, String>,
        /// Counter value, or histogram observation count.
        pub value: u64,
        /// Explicit histogram bucket boundaries (empty for the counter).
        pub bounds: Vec<f64>,
    }

    #[derive(Clone, Debug, Default)]
    struct Snapshot {
        points: Arc<Mutex<Vec<Point>>>,
    }

    impl PushMetricExporter for Snapshot {
        async fn export(&self, metrics: &ResourceMetrics) -> OTelSdkResult {
            let mut out = Vec::new();
            for scope in metrics.scope_metrics() {
                for metric in scope.metrics() {
                    let attrs_of = |iter: &mut dyn Iterator<Item = &opentelemetry::KeyValue>| {
                        iter.map(|kv| (kv.key.to_string(), kv.value.as_str().to_string()))
                            .collect::<BTreeMap<_, _>>()
                    };
                    match metric.data() {
                        AggregatedMetrics::U64(MetricData::Sum(sum)) => {
                            for dp in sum.data_points() {
                                out.push(Point {
                                    name: metric.name().to_string(),
                                    unit: metric.unit().to_string(),
                                    attrs: attrs_of(&mut dp.attributes()),
                                    value: dp.value(),
                                    bounds: Vec::new(),
                                });
                            }
                        }
                        AggregatedMetrics::F64(MetricData::Histogram(hist)) => {
                            for dp in hist.data_points() {
                                out.push(Point {
                                    name: metric.name().to_string(),
                                    unit: metric.unit().to_string(),
                                    attrs: attrs_of(&mut dp.attributes()),
                                    value: dp.count(),
                                    bounds: dp.bounds().collect(),
                                });
                            }
                        }
                        _ => {}
                    }
                }
            }
            *self.points.lock().expect("snapshot lock") = out;
            Ok(())
        }

        fn force_flush(&self) -> OTelSdkResult {
            Ok(())
        }

        fn shutdown_with_timeout(&self, _timeout: Duration) -> OTelSdkResult {
            Ok(())
        }

        fn temporality(&self) -> Temporality {
            Temporality::Cumulative
        }
    }

    /// A capturing provider plus the [`JwtMetrics`] bound to it.
    pub struct Capture {
        snapshot: Snapshot,
        provider: SdkMeterProvider,
        pub metrics: JwtMetrics,
    }

    impl Default for Capture {
        fn default() -> Self {
            Self::new()
        }
    }

    impl Capture {
        /// A fresh provider with an hour-long export interval (flushed by hand).
        pub fn new() -> Self {
            let snapshot = Snapshot::default();
            let reader = PeriodicReader::builder(snapshot.clone())
                .with_interval(Duration::from_secs(3600))
                .build();
            let provider = SdkMeterProvider::builder().with_reader(reader).build();
            let metrics = JwtMetrics::new(&provider);
            Self {
                snapshot,
                provider,
                metrics,
            }
        }

        /// Every exported data point named `name`, after a forced flush.
        pub fn points(&self, name: &str) -> Vec<Point> {
            self.provider.force_flush().expect("flush meter provider");
            self.snapshot
                .points
                .lock()
                .expect("snapshot lock")
                .iter()
                .filter(|point| point.name == name)
                .cloned()
                .collect()
        }

        /// The counter value for the exact `(verifier, alg, outcome)` label set.
        pub fn count(&self, verifier: &str, alg: &str, outcome: &str) -> u64 {
            self.points(METRIC_VERIFICATIONS)
                .into_iter()
                .filter(|p| {
                    p.attrs.get("verifier").map(String::as_str) == Some(verifier)
                        && p.attrs.get("alg").map(String::as_str) == Some(alg)
                        && p.attrs.get("outcome").map(String::as_str) == Some(outcome)
                })
                .map(|p| p.value)
                .sum()
        }

        /// The sum of every counter data point (total verifications recorded).
        pub fn total(&self) -> u64 {
            self.points(METRIC_VERIFICATIONS)
                .iter()
                .map(|p| p.value)
                .sum()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn b64(raw: &str) -> String {
        URL_SAFE_NO_PAD.encode(raw)
    }

    fn token_with_header(header_json: &str) -> String {
        format!("{}.{}.sig", b64(header_json), b64("{}"))
    }

    #[test]
    fn alg_label_is_closed_and_case_insensitive() {
        assert_eq!(alg_label(None), "absent");
        assert_eq!(alg_label(Some("EdDSA")), "eddsa");
        assert_eq!(alg_label(Some("HS256")), "hs256");
        assert_eq!(alg_label(Some("ES256K")), "es256k");
        assert_eq!(alg_label(Some("NoNe")), "none");
        assert_eq!(alg_label(Some("ABSENT")), "absent");
        assert_eq!(alg_label(Some("totally-attacker-chosen")), "other");
        assert_eq!(alg_label(Some("")), "other");
    }

    #[test]
    fn every_known_alg_label_round_trips() {
        for known in KNOWN_ALG_LABELS {
            assert_eq!(alg_label(Some(known)), known);
            assert_eq!(alg_label(Some(&known.to_ascii_uppercase())), known);
        }
    }

    #[test]
    fn kid_charset_and_length_are_pinned() {
        for ok in ["k1", "hs256-v1", "_a", "A.b:c-d", &"a".repeat(64)] {
            assert!(is_valid_kid(ok), "{ok} should be valid");
        }
        for bad in [
            "",
            "-leading",
            ".leading",
            "has space",
            "slash/y",
            "uni\u{e9}",
            "semi;colon",
            "new\nline",
            &"a".repeat(65),
        ] {
            assert!(!is_valid_kid(bad), "{bad:?} should be invalid");
        }
    }

    #[test]
    fn plain_eddsa_header_passes_with_kid() {
        let token = token_with_header(r#"{"alg":"EdDSA","typ":"JWT","kid":"k1"}"#);
        let header = inspect_header(&token, &["EdDSA"], KidPolicy::Vet).expect("clean header");
        assert_eq!(header.alg, "EdDSA");
        assert_eq!(header.kid.as_deref(), Some("k1"));
    }

    #[test]
    fn alg_none_is_named_in_every_letter_case() {
        for alg in ["none", "None", "NONE", "nOnE"] {
            let token = token_with_header(&format!(r#"{{"alg":"{alg}"}}"#));
            let err = inspect_header(&token, &["EdDSA"], KidPolicy::Vet).unwrap_err();
            assert_eq!(err, JwtRejection::new(REASON_ALG_NONE, "none"), "{alg}");
        }
    }

    #[test]
    fn alg_none_wins_over_forbidden_header() {
        let token = token_with_header(r#"{"alg":"none","jku":"https://evil"}"#);
        let err = inspect_header(&token, &["EdDSA"], KidPolicy::Vet).unwrap_err();
        assert_eq!(err.reason, REASON_ALG_NONE);
    }

    #[test]
    fn every_key_material_header_is_refused() {
        for param in FORBIDDEN_HEADER_PARAMS {
            let token =
                token_with_header(&format!(r#"{{"alg":"EdDSA","kid":"k1","{param}":"x"}}"#));
            let err = inspect_header(&token, &["EdDSA"], KidPolicy::Vet).unwrap_err();
            assert_eq!(
                err,
                JwtRejection::new(REASON_FORBIDDEN_HEADER, "eddsa"),
                "{param}"
            );
        }
    }

    #[test]
    fn forbidden_header_with_any_value_type_is_refused() {
        for value in ["null", "{}", "[]", "true", "1", "\"\""] {
            let token = token_with_header(&format!(r#"{{"alg":"EdDSA","crit":{value}}}"#));
            let err = inspect_header(&token, &["EdDSA"], KidPolicy::Vet).unwrap_err();
            assert_eq!(err.reason, REASON_FORBIDDEN_HEADER, "crit={value}");
        }
    }

    #[test]
    fn forbidden_header_is_named_before_alg_mismatch() {
        let token = token_with_header(r#"{"alg":"HS256","jwk":{"kty":"oct"}}"#);
        let err = inspect_header(&token, &["EdDSA"], KidPolicy::Vet).unwrap_err();
        assert_eq!(err, JwtRejection::new(REASON_FORBIDDEN_HEADER, "hs256"));
    }

    #[test]
    fn other_algorithms_are_a_mismatch_with_a_bounded_label() {
        for (alg, label) in [
            ("HS256", "hs256"),
            ("HS512", "hs512"),
            ("RS256", "rs256"),
            ("ES256", "es256"),
            ("eddsa", "eddsa"),
            ("EdDSA ", "other"),
            ("whatever", "other"),
        ] {
            let token = token_with_header(&format!(r#"{{"alg":"{alg}","kid":"k1"}}"#));
            let err = inspect_header(&token, &["EdDSA"], KidPolicy::Vet).unwrap_err();
            assert_eq!(err, JwtRejection::new(REASON_ALG_MISMATCH, label), "{alg}");
        }
    }

    #[test]
    fn absent_null_and_non_string_alg_are_a_mismatch() {
        for (header, label) in [
            (r#"{"kid":"k1"}"#, "absent"),
            (r#"{"alg":null}"#, "absent"),
            (r#"{"alg":5}"#, "other"),
            (r#"{"alg":["EdDSA"]}"#, "other"),
            (r#"{"alg":{"x":1}}"#, "other"),
        ] {
            let token = token_with_header(header);
            let err = inspect_header(&token, &["EdDSA"], KidPolicy::Vet).unwrap_err();
            assert_eq!(
                err,
                JwtRejection::new(REASON_ALG_MISMATCH, label),
                "{header}"
            );
        }
    }

    #[test]
    fn allow_list_is_the_only_gate() {
        let token = token_with_header(r#"{"alg":"HS256"}"#);
        assert!(inspect_header(&token, &["HS256"], KidPolicy::Vet).is_ok());
        assert!(inspect_header(&token, &["EdDSA"], KidPolicy::Vet).is_err());
        assert!(inspect_header(&token, &[], KidPolicy::Vet).is_err());
    }

    #[test]
    fn kid_policy_vet_require_ignore() {
        let absent = token_with_header(r#"{"alg":"EdDSA"}"#);
        let null = token_with_header(r#"{"alg":"EdDSA","kid":null}"#);
        let bad = token_with_header(r#"{"alg":"EdDSA","kid":"has space"}"#);
        let numeric = token_with_header(r#"{"alg":"EdDSA","kid":7}"#);
        let long = token_with_header(&format!(r#"{{"alg":"EdDSA","kid":"{}"}}"#, "k".repeat(65)));

        for token in [&absent, &null] {
            assert_eq!(
                inspect_header(token, &["EdDSA"], KidPolicy::Vet)
                    .expect("absent ok")
                    .kid,
                None
            );
            assert_eq!(
                inspect_header(token, &["EdDSA"], KidPolicy::Require)
                    .unwrap_err()
                    .reason,
                REASON_BAD_KID
            );
        }
        for token in [&bad, &numeric, &long] {
            assert_eq!(
                inspect_header(token, &["EdDSA"], KidPolicy::Vet).unwrap_err(),
                JwtRejection::new(REASON_BAD_KID, "eddsa")
            );
            assert_eq!(
                inspect_header(token, &["EdDSA"], KidPolicy::Require)
                    .unwrap_err()
                    .reason,
                REASON_BAD_KID
            );
            // `Ignore` never looks at (and never returns) the kid.
            assert_eq!(
                inspect_header(token, &["EdDSA"], KidPolicy::Ignore)
                    .expect("ignored")
                    .kid,
                None
            );
        }
    }

    #[test]
    fn structurally_broken_tokens_are_malformed() {
        let ok_header = b64(r#"{"alg":"EdDSA"}"#);
        let huge = b64(&format!(
            r#"{{"alg":"EdDSA","x":"{}"}}"#,
            "a".repeat(20_000)
        ));
        let cases: Vec<String> = vec![
            String::new(),
            "not-a-jwt".into(),
            "a.b".into(),
            format!("{ok_header}.b"),
            format!("{ok_header}.b.c.d"),
            format!(".b.c"),
            "!!!.b.c".into(),
            format!("{}=.b.c", b64(r#"{"alg":"EdDSA"}"#)),
            format!("{}.b.c", b64("not json")),
            format!("{}.b.c", b64("[1,2]")),
            format!("{}.b.c", b64("\"EdDSA\"")),
            format!("{}.b.c", b64("null")),
            format!("{huge}.b.c"),
            format!("{}.b.c", b64(&"[".repeat(2000))),
        ];
        for token in cases {
            let err = inspect_header(&token, &["EdDSA"], KidPolicy::Vet).unwrap_err();
            assert_eq!(
                err,
                JwtRejection::without_alg(REASON_MALFORMED),
                "{token:.40}"
            );
        }
    }

    #[test]
    fn header_segment_size_cap_is_an_exact_inclusive_bound() {
        // base64url length of an N-byte header is exactly 4N/3 when N % 3 == 0.
        // `{"alg":"EdDSA","p":""}` is 22 bytes of JSON scaffolding.
        let at_cap = format!(
            r#"{{"alg":"EdDSA","p":"{}"}}"#,
            "a".repeat(MAX_HEADER_SEGMENT_CHARS / 4 * 3 - 22)
        );
        let encoded = URL_SAFE_NO_PAD.encode(&at_cap);
        assert_eq!(encoded.len(), MAX_HEADER_SEGMENT_CHARS);
        let token = format!("{encoded}.b.c");
        assert!(inspect_header(&token, &["EdDSA"], KidPolicy::Vet).is_ok());

        let over_cap = format!(
            r#"{{"alg":"EdDSA","p":"{}"}}"#,
            "a".repeat(MAX_HEADER_SEGMENT_CHARS / 4 * 3 - 22 + 3)
        );
        let token = format!("{}.b.c", URL_SAFE_NO_PAD.encode(&over_cap));
        assert_eq!(
            inspect_header(&token, &["EdDSA"], KidPolicy::Vet).unwrap_err(),
            JwtRejection::without_alg(REASON_MALFORMED)
        );
    }

    #[test]
    fn all_reasons_are_unique_snake_case_and_include_the_ok_free_set() {
        let mut sorted = ALL_REASONS.to_vec();
        sorted.sort_unstable();
        sorted.dedup();
        assert_eq!(sorted.len(), ALL_REASONS.len(), "no duplicates");
        assert!(!ALL_REASONS.contains(&OUTCOME_OK), "ok is not a rejection");
        for reason in ALL_REASONS {
            assert!(
                reason.chars().all(|c| c.is_ascii_lowercase() || c == '_'),
                "{reason} must be a bounded snake_case label"
            );
        }
        for alarming in ALARMING_REASONS {
            assert!(ALL_REASONS.contains(&alarming), "{alarming}");
        }
    }

    #[test]
    fn rejection_display_is_the_reason_only() {
        let rejection = JwtRejection::new(REASON_BAD_KID, "eddsa");
        assert_eq!(rejection.to_string(), "bad_kid");
        assert_eq!(JwtRejection::without_alg(REASON_INVALID).alg, "absent");
    }

    #[test]
    fn global_metrics_are_constructible_without_a_provider() {
        // No provider installed in this test binary -> no-op instruments;
        // recording must still be a harmless call.
        let metrics = JwtMetrics::global();
        metrics.record(VERIFIER_SERVICE_EDDSA, "eddsa", OUTCOME_OK, Instant::now());
    }

    #[test]
    fn counter_and_histogram_carry_the_python_instrument_contract() {
        let capture = test_support::Capture::new();
        capture
            .metrics
            .record(VERIFIER_PLATFORM_HS256, "hs256", OUTCOME_OK, Instant::now());
        capture
            .metrics
            .record(VERIFIER_PLATFORM_HS256, "hs256", OUTCOME_OK, Instant::now());
        capture.metrics.record(
            VERIFIER_PLATFORM_HS256,
            "none",
            REASON_ALG_NONE,
            Instant::now(),
        );

        let counters = capture.points("waddles_jwt_verifications_total");
        assert_eq!(counters.len(), 2, "one series per (verifier, alg, outcome)");
        for point in &counters {
            assert_eq!(point.unit, "{verification}");
            let keys: Vec<&str> = point.attrs.keys().map(String::as_str).collect();
            assert_eq!(keys, ["alg", "outcome", "verifier"], "exact label names");
        }
        assert_eq!(capture.count("platform_hs256", "hs256", "ok"), 2);
        assert_eq!(capture.count("platform_hs256", "none", "alg_none"), 1);

        let histograms = capture.points("waddles_jwt_verification_seconds");
        assert_eq!(histograms.len(), 2, "one series per (verifier, alg)");
        for point in &histograms {
            assert_eq!(point.unit, "s");
            let keys: Vec<&str> = point.attrs.keys().map(String::as_str).collect();
            assert_eq!(
                keys,
                ["alg", "verifier"],
                "no outcome label on the histogram"
            );
            assert_eq!(point.bounds, LATENCY_BOUNDARIES_SECONDS.to_vec());
        }
        let hs256_ok: u64 = histograms
            .iter()
            .filter(|p| p.attrs["alg"] == "hs256")
            .map(|p| p.value)
            .sum();
        assert_eq!(
            hs256_ok, 2,
            "every verification observes the latency histogram"
        );
    }

    #[test]
    fn report_outcome_records_ok_and_rejections() {
        let capture = test_support::Capture::new();
        report_outcome(
            &capture.metrics,
            VERIFIER_EGRESS_ASSERTION,
            Instant::now(),
            "eddsa",
            OUTCOME_OK,
        );
        report_outcome(
            &capture.metrics,
            VERIFIER_EGRESS_ASSERTION,
            Instant::now(),
            "eddsa",
            REASON_BAD_SIGNATURE,
        );
        assert_eq!(capture.count("egress_assertion", "eddsa", "ok"), 1);
        assert_eq!(
            capture.count("egress_assertion", "eddsa", "bad_signature"),
            1
        );
        assert_eq!(capture.total(), 2);
    }

    #[test]
    fn latency_boundaries_are_strictly_increasing_and_second_scaled() {
        let bounds = LATENCY_BOUNDARIES_SECONDS;
        assert!(bounds.windows(2).all(|pair| pair[0] < pair[1]));
        assert!(bounds.iter().any(|b| *b < 0.001), "resolves sub-ms work");
        assert!(
            bounds.iter().any(|b| *b >= 1.0),
            "resolves a slow JWKS fetch"
        );
    }

    /// The production code of this module is a byte-for-byte mirror of
    /// `core/service_auth/src/jwt_hardening.rs` (only the module docs and
    /// the test scaffolding differ). Edit BOTH or neither: this is what
    /// keeps the Python / `service_auth` / `svc_streaming` verifiers on one
    /// vocabulary and one metric contract.
    #[test]
    fn production_code_is_identical_to_the_service_auth_original() {
        const ORIGINAL: &str = include_str!("../../../../service_auth/src/jwt_hardening.rs");
        const MIRROR: &str = include_str!("hardening.rs");

        fn production_body(source: &'static str) -> &'static str {
            let start = source.find("use std::fmt;").expect("production code start");
            let end = source.find("#[cfg(test)]").expect("test scaffolding start");
            &source[start..end]
        }

        assert!(!production_body(ORIGINAL).is_empty());
        assert_eq!(production_body(ORIGINAL), production_body(MIRROR));
    }
}
