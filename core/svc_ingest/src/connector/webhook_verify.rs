//! Scheme-parameterized webhook signature verification (P6a Task 1).
//!
//! A [`Scheme`] is HOST configuration attached to a registered webhook route
//! -- it is never derived from the request. Verification is two-phase so the
//! secret is only fetched once the cheap structural checks pass:
//!
//! 1. [`Scheme::prepare`] -- header presence, timestamp parse + replay
//!    window, signed-string construction. No secret involved.
//! 2. [`Prepared::check`] -- the cryptographic check against a secret the
//!    caller resolved BY ROUTE KEY (never from the body). Constant-time
//!    (`hmac::Mac::verify_slice`; `ring`'s Ed25519 verify). An empty/absent
//!    key fails closed.
//!
//! Every failure is a [`VerifyError`] with no detail beyond a fixed kind; the
//! HTTP layer collapses them all to one uniform 403.

use axum::http::{HeaderMap, HeaderName};
use base64::Engine as _;
use hmac::{Hmac, Mac};
use sha2::{Digest, Sha256};

type HmacSha256 = Hmac<Sha256>;

/// Future timestamps beyond this skew are rejected (clock drift allowance).
pub const FUTURE_SKEW_SECS: i64 = 60;

/// Why verification failed. Never carries header/body/secret content.
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum VerifyError {
    /// A required signature/timestamp/id header is absent or not UTF-8.
    #[error("missing or malformed header")]
    MissingHeader,
    /// Timestamp unparseable.
    #[error("bad timestamp")]
    BadTimestamp,
    /// Timestamp outside the route's replay window.
    #[error("outside replay window")]
    OutsideWindow,
    /// Signature does not match.
    #[error("bad signature")]
    BadSignature,
    /// Secret/key absent, empty or unusable -- fail closed.
    #[error("unusable key")]
    BadKey,
}

/// How a signature (and an Ed25519 key) is text-encoded.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Encoding {
    /// Lowercase/uppercase hex.
    Hex,
    /// Standard base64.
    Base64,
}

impl Encoding {
    fn decode(self, s: &str) -> Option<Vec<u8>> {
        match self {
            Self::Hex => decode_hex(s),
            Self::Base64 => base64::engine::general_purpose::STANDARD.decode(s).ok(),
        }
    }
}

/// Timestamp header format.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TimestampFormat {
    /// RFC 3339 (Twitch EventSub).
    Rfc3339,
    /// Decimal Unix seconds (Stripe, Discord, most others).
    UnixSeconds,
}

impl TimestampFormat {
    fn to_unix(self, raw: &str) -> Result<i64, VerifyError> {
        match self {
            Self::Rfc3339 => chrono::DateTime::parse_from_rfc3339(raw)
                .map(|d| d.timestamp())
                .map_err(|_| VerifyError::BadTimestamp),
            Self::UnixSeconds => raw
                .trim()
                .parse::<i64>()
                .map_err(|_| VerifyError::BadTimestamp),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum Part {
    Lit(Vec<u8>),
    Id,
    Ts,
    Body,
}

/// A parsed signed-string template: literals plus `{id}`, `{ts}`, `{body}`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SignedTemplate(Vec<Part>);

impl SignedTemplate {
    /// Parses `template`. Rejects unknown placeholders and any template that
    /// does not sign the body or the timestamp (a signature that does not
    /// cover both cannot defend against tampering/replay).
    pub fn parse(template: &str) -> Result<Self, String> {
        let mut parts = Vec::new();
        let mut lit = String::new();
        let mut rest = template;
        while let Some(open) = rest.find('{') {
            lit.push_str(&rest[..open]);
            let after = &rest[open + 1..];
            let close = after.find('}').ok_or("unterminated placeholder")?;
            let part = match &after[..close] {
                "id" => Part::Id,
                "ts" => Part::Ts,
                "body" => Part::Body,
                other => return Err(format!("unknown placeholder {{{other}}}")),
            };
            if !lit.is_empty() {
                parts.push(Part::Lit(std::mem::take(&mut lit).into_bytes()));
            }
            parts.push(part);
            rest = &after[close + 1..];
        }
        lit.push_str(rest);
        if !lit.is_empty() {
            parts.push(Part::Lit(lit.into_bytes()));
        }
        if !parts.contains(&Part::Body) {
            return Err("template must include {body}".into());
        }
        if !parts.contains(&Part::Ts) {
            return Err("template must include {ts}".into());
        }
        Ok(Self(parts))
    }

    /// Rejects adjacent variable-length placeholders with no literal
    /// separator: byte boundaries could shift (`id|body`) and still yield the
    /// same signed string. A pair involving `{ts}` is allowed -- the
    /// timestamp must parse as a fixed-format value inside the replay window,
    /// so it cannot absorb arbitrary bytes (this keeps the Twitch
    /// `{id}{ts}{body}` preset valid).
    fn reject_ambiguous(&self) -> Result<(), String> {
        for w in self.0.windows(2) {
            let ph = |p: &Part| !matches!(p, Part::Lit(_));
            if ph(&w[0]) && ph(&w[1]) && w[0] != Part::Ts && w[1] != Part::Ts {
                return Err("adjacent placeholders need a literal separator".into());
            }
        }
        Ok(())
    }

    fn needs_id(&self) -> bool {
        self.0.contains(&Part::Id)
    }

    fn render(&self, id: &str, ts: &str, body: &[u8]) -> Vec<u8> {
        let mut out = Vec::with_capacity(body.len() + 64);
        for p in &self.0 {
            match p {
                Part::Lit(l) => out.extend_from_slice(l),
                Part::Id => out.extend_from_slice(id.as_bytes()),
                Part::Ts => out.extend_from_slice(ts.as_bytes()),
                Part::Body => out.extend_from_slice(body),
            }
        }
        out
    }
}

/// HMAC-SHA256 with a configurable signed string.
#[derive(Debug, Clone)]
pub struct HmacSpec {
    /// Header carrying the signature.
    pub signature_header: HeaderName,
    /// Header carrying the timestamp (mandatory: replay defense).
    pub timestamp_header: HeaderName,
    /// Header carrying a unique message id; required iff the template uses
    /// `{id}`, otherwise dedup keys on the signature.
    pub id_header: Option<HeaderName>,
    /// Signed-string template.
    pub template: SignedTemplate,
    /// Literal prefix stripped from the signature header (`sha256=`).
    pub signature_prefix: String,
    /// Signature encoding.
    pub encoding: Encoding,
    /// Timestamp format.
    pub timestamp_format: TimestampFormat,
}

/// Ed25519: the route's secret-ref resolves to the PUBLIC key (encoded per
/// `encoding`); the signed string is rendered from `template`.
#[derive(Debug, Clone)]
pub struct Ed25519Spec {
    /// Header carrying the signature.
    pub signature_header: HeaderName,
    /// Header carrying the timestamp (mandatory).
    pub timestamp_header: HeaderName,
    /// Optional unique id header (dedup + `{id}`).
    pub id_header: Option<HeaderName>,
    /// Signed-string template.
    pub template: SignedTemplate,
    /// Encoding of both the signature header and the resolved key.
    pub encoding: Encoding,
    /// Timestamp format.
    pub timestamp_format: TimestampFormat,
}

/// Stripe-style: `Stripe-Signature: t=<unix>,v1=<hex>[,v1=<hex>...]`,
/// HMAC-SHA256 over `"{t}.{body}"`.
#[derive(Debug, Clone)]
pub struct StripeSpec {
    /// Header carrying `t=...,v1=...`.
    pub signature_header: HeaderName,
}

/// The signature scheme of a registered route (host-side config only).
#[derive(Debug, Clone)]
pub enum Scheme {
    /// Generic HMAC-SHA256.
    HmacSha256(HmacSpec),
    /// Ed25519.
    Ed25519(Ed25519Spec),
    /// Stripe-style.
    Stripe(StripeSpec),
}

/// Result of phase 1: everything needed for the crypto check, no secret.
#[derive(Debug)]
pub struct Prepared {
    kind: PreparedKind,
    /// Stable per-delivery id for dedup (id header, else hash of signature).
    pub dedup_id: String,
}

#[derive(Debug)]
enum PreparedKind {
    Hmac {
        message: Vec<u8>,
        candidates: Vec<Vec<u8>>,
    },
    Ed25519 {
        message: Vec<u8>,
        signature: Vec<u8>,
        encoding: Encoding,
    },
}

fn header_str<'a>(h: &'a HeaderMap, name: &HeaderName) -> Result<&'a str, VerifyError> {
    h.get(name)
        .and_then(|v| v.to_str().ok())
        .filter(|s| !s.is_empty())
        .ok_or(VerifyError::MissingHeader)
}

fn check_window(ts: i64, now: i64, window_secs: u64) -> Result<(), VerifyError> {
    let age = now.saturating_sub(ts);
    let max = i64::try_from(window_secs).unwrap_or(i64::MAX);
    if age > max || age < -FUTURE_SKEW_SECS {
        return Err(VerifyError::OutsideWindow);
    }
    Ok(())
}

/// Dedup id = digest of the exact signed message. Everything in it is covered
/// by the signature (no unsigned header or attacker-chosen signature bytes can
/// change it), so a replay cannot dodge dedup by varying an id header or by
/// prepending junk signature candidates.
fn signed_message_id(message: &[u8]) -> String {
    hex_encode(&Sha256::digest(message))
}

/// Max `v1=` entries accepted in a Stripe-style header.
pub const MAX_STRIPE_V1: usize = 4;

impl Scheme {
    /// The Twitch EventSub scheme expressed as a generic HMAC route:
    /// `sha256=hex(HMAC(secret, id + ts + body))`, RFC 3339 timestamp.
    #[must_use]
    pub fn twitch_eventsub() -> Self {
        Self::HmacSha256(HmacSpec {
            signature_header: HeaderName::from_static("twitch-eventsub-message-signature"),
            timestamp_header: HeaderName::from_static("twitch-eventsub-message-timestamp"),
            id_header: Some(HeaderName::from_static("twitch-eventsub-message-id")),
            // Static, known-valid template.
            template: SignedTemplate(vec![Part::Id, Part::Ts, Part::Body]),
            signature_prefix: "sha256=".into(),
            encoding: Encoding::Hex,
            timestamp_format: TimestampFormat::Rfc3339,
        })
    }

    /// Structural validation at registration time (fail loud, not per-request).
    pub fn validate(&self) -> Result<(), String> {
        let (template, id_header) = match self {
            Self::HmacSha256(s) => (&s.template, &s.id_header),
            Self::Ed25519(s) => (&s.template, &s.id_header),
            Self::Stripe(_) => return Ok(()),
        };
        if template.needs_id() && id_header.is_none() {
            return Err("template uses {id} but no id_header configured".into());
        }
        template.reject_ambiguous()
    }

    /// Phase 1 -- no secret: headers, timestamp window, signed string.
    pub fn prepare(
        &self,
        headers: &HeaderMap,
        body: &[u8],
        window_secs: u64,
        now_unix: i64,
    ) -> Result<Prepared, VerifyError> {
        match self {
            Self::HmacSha256(s) => {
                let ts_raw = header_str(headers, &s.timestamp_header)?;
                check_window(s.timestamp_format.to_unix(ts_raw)?, now_unix, window_secs)?;
                let sig_raw = header_str(headers, &s.signature_header)?;
                let id = match &s.id_header {
                    Some(h) => Some(header_str(headers, h)?),
                    None => None,
                };
                let hex = sig_raw
                    .strip_prefix(s.signature_prefix.as_str())
                    .ok_or(VerifyError::BadSignature)?;
                let sig = s.encoding.decode(hex).ok_or(VerifyError::BadSignature)?;
                let message = s.template.render(id.unwrap_or(""), ts_raw, body);
                let dedup_id = signed_message_id(&message);
                Ok(Prepared {
                    kind: PreparedKind::Hmac {
                        message,
                        candidates: vec![sig],
                    },
                    dedup_id,
                })
            }
            Self::Ed25519(s) => {
                let ts_raw = header_str(headers, &s.timestamp_header)?;
                check_window(s.timestamp_format.to_unix(ts_raw)?, now_unix, window_secs)?;
                let sig_raw = header_str(headers, &s.signature_header)?;
                let id = match &s.id_header {
                    Some(h) => Some(header_str(headers, h)?),
                    None => None,
                };
                let sig = s
                    .encoding
                    .decode(sig_raw)
                    .ok_or(VerifyError::BadSignature)?;
                let message = s.template.render(id.unwrap_or(""), ts_raw, body);
                let dedup_id = signed_message_id(&message);
                Ok(Prepared {
                    kind: PreparedKind::Ed25519 {
                        message,
                        signature: sig,
                        encoding: s.encoding,
                    },
                    dedup_id,
                })
            }
            Self::Stripe(s) => {
                let raw = header_str(headers, &s.signature_header)?;
                let mut t: Option<&str> = None;
                let mut candidates = Vec::new();
                let mut v1_seen = 0usize;
                for item in raw.split(',') {
                    match item.trim().split_once('=') {
                        Some(("t", v)) => {
                            if t.replace(v).is_some() {
                                return Err(VerifyError::BadSignature);
                            }
                        }
                        Some(("v1", v)) => {
                            v1_seen += 1;
                            if v1_seen > MAX_STRIPE_V1 {
                                return Err(VerifyError::BadSignature);
                            }
                            if let Some(d) = decode_hex(v) {
                                candidates.push(d);
                            }
                        }
                        _ => {}
                    }
                }
                let t = t.ok_or(VerifyError::MissingHeader)?;
                check_window(
                    TimestampFormat::UnixSeconds.to_unix(t)?,
                    now_unix,
                    window_secs,
                )?;
                if candidates.is_empty() {
                    return Err(VerifyError::BadSignature);
                }
                let mut message = Vec::with_capacity(body.len() + t.len() + 1);
                message.extend_from_slice(t.as_bytes());
                message.push(b'.');
                message.extend_from_slice(body);
                let dedup_id = signed_message_id(&message);
                Ok(Prepared {
                    kind: PreparedKind::Hmac {
                        message,
                        candidates,
                    },
                    dedup_id,
                })
            }
        }
    }
}

impl Prepared {
    /// Phase 2 -- the constant-time cryptographic check against `secret`
    /// (HMAC key bytes, or the encoded Ed25519 public key). Empty secret
    /// fails closed.
    pub fn check(&self, secret: &str) -> Result<(), VerifyError> {
        if secret.is_empty() {
            return Err(VerifyError::BadKey);
        }
        match &self.kind {
            PreparedKind::Hmac {
                message,
                candidates,
            } => {
                let mut base = HmacSha256::new_from_slice(secret.as_bytes())
                    .map_err(|_| VerifyError::BadKey)?;
                base.update(message);
                // `verify_slice` compares in constant time. Evaluate every
                // candidate (no early exit on the first) and fold.
                let mut ok = false;
                for c in candidates {
                    ok |= base.clone().verify_slice(c).is_ok();
                }
                if ok {
                    Ok(())
                } else {
                    Err(VerifyError::BadSignature)
                }
            }
            PreparedKind::Ed25519 {
                message,
                signature,
                encoding,
            } => {
                let key = encoding.decode(secret.trim()).ok_or(VerifyError::BadKey)?;
                if key.len() != 32 {
                    return Err(VerifyError::BadKey);
                }
                ring::signature::UnparsedPublicKey::new(&ring::signature::ED25519, &key)
                    .verify(message, signature)
                    .map_err(|_| VerifyError::BadSignature)
            }
        }
    }
}

/// Lowercase hex encode.
#[must_use]
pub fn hex_encode(bytes: &[u8]) -> String {
    use std::fmt::Write as _;
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        let _ = write!(s, "{b:02x}");
    }
    s
}

fn decode_hex(s: &str) -> Option<Vec<u8>> {
    if !s.len().is_multiple_of(2) || !s.is_ascii() {
        return None;
    }
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).ok())
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use ring::signature::{Ed25519KeyPair, KeyPair};

    const NOW: i64 = 1_800_000_000;
    const WINDOW: u64 = 300;
    const SECRET: &str = "whsec_test_secret";

    fn hmac_hex(secret: &str, msg: &[u8]) -> String {
        let mut m = HmacSha256::new_from_slice(secret.as_bytes()).unwrap();
        m.update(msg);
        hex_encode(&m.finalize().into_bytes())
    }

    fn generic_hmac() -> Scheme {
        Scheme::HmacSha256(HmacSpec {
            signature_header: HeaderName::from_static("x-sig"),
            timestamp_header: HeaderName::from_static("x-ts"),
            id_header: None,
            template: SignedTemplate::parse("v0:{ts}:{body}").unwrap(),
            signature_prefix: "v0=".into(),
            encoding: Encoding::Hex,
            timestamp_format: TimestampFormat::UnixSeconds,
        })
    }

    fn ed_scheme() -> Scheme {
        Scheme::Ed25519(Ed25519Spec {
            signature_header: HeaderName::from_static("x-sig"),
            timestamp_header: HeaderName::from_static("x-ts"),
            id_header: None,
            template: SignedTemplate::parse("{ts}{body}").unwrap(),
            encoding: Encoding::Hex,
            timestamp_format: TimestampFormat::UnixSeconds,
        })
    }

    fn stripe() -> Scheme {
        Scheme::Stripe(StripeSpec {
            signature_header: HeaderName::from_static("stripe-signature"),
        })
    }

    fn hdrs(pairs: &[(&'static str, String)]) -> HeaderMap {
        let mut h = HeaderMap::new();
        for (k, v) in pairs {
            h.insert(HeaderName::from_static(k), v.parse().unwrap());
        }
        h
    }

    /// (scheme, secret, signed headers for `body` at `ts`)
    fn signed(kind: &str, body: &[u8], ts: i64) -> (Scheme, String, HeaderMap) {
        match kind {
            "hmac" => {
                let msg = [format!("v0:{ts}:").as_bytes(), body].concat();
                let h = hdrs(&[
                    ("x-ts", ts.to_string()),
                    ("x-sig", format!("v0={}", hmac_hex(SECRET, &msg))),
                ]);
                (generic_hmac(), SECRET.into(), h)
            }
            "stripe" => {
                let msg = [format!("{ts}.").as_bytes(), body].concat();
                let h = hdrs(&[(
                    "stripe-signature",
                    format!("t={ts},v1=deadbeef,v1={}", hmac_hex(SECRET, &msg)),
                )]);
                (stripe(), SECRET.into(), h)
            }
            "ed25519" => {
                let kp = Ed25519KeyPair::from_seed_unchecked(&[7u8; 32]).unwrap();
                let msg = [ts.to_string().as_bytes(), body].concat();
                let h = hdrs(&[
                    ("x-ts", ts.to_string()),
                    ("x-sig", hex_encode(kp.sign(&msg).as_ref())),
                ]);
                (ed_scheme(), hex_encode(kp.public_key().as_ref()), h)
            }
            "twitch" => {
                let id = "msg-1";
                let ts_s = "2027-01-15T08:00:00Z";
                let msg = [id.as_bytes(), ts_s.as_bytes(), body].concat();
                let h = hdrs(&[
                    ("twitch-eventsub-message-id", id.into()),
                    ("twitch-eventsub-message-timestamp", ts_s.into()),
                    (
                        "twitch-eventsub-message-signature",
                        format!("sha256={}", hmac_hex(SECRET, &msg)),
                    ),
                ]);
                (Scheme::twitch_eventsub(), SECRET.into(), h)
            }
            _ => unreachable!(),
        }
    }

    fn now_for(kind: &str) -> i64 {
        if kind == "twitch" {
            chrono::DateTime::parse_from_rfc3339("2027-01-15T08:00:30Z")
                .unwrap()
                .timestamp()
        } else {
            NOW
        }
    }

    const KINDS: [&str; 4] = ["hmac", "stripe", "ed25519", "twitch"];

    #[test]
    fn valid_signature_passes_for_every_scheme() {
        for k in KINDS {
            let (s, secret, h) = signed(k, b"{\"a\":1}", NOW);
            let p = s.prepare(&h, b"{\"a\":1}", WINDOW, now_for(k)).unwrap();
            assert_eq!(p.check(&secret), Ok(()), "{k}");
            assert!(!p.dedup_id.is_empty());
        }
    }

    #[test]
    fn tampered_body_is_rejected_for_every_scheme() {
        for k in KINDS {
            let (s, secret, h) = signed(k, b"{\"a\":1}", NOW);
            let p = s.prepare(&h, b"{\"a\":2}", WINDOW, now_for(k)).unwrap();
            assert_eq!(p.check(&secret), Err(VerifyError::BadSignature), "{k}");
        }
    }

    #[test]
    fn wrong_secret_is_rejected_for_every_scheme() {
        for k in KINDS {
            let (s, _secret, h) = signed(k, b"x", NOW);
            let p = s.prepare(&h, b"x", WINDOW, now_for(k)).unwrap();
            let wrong = if k == "ed25519" {
                let kp = Ed25519KeyPair::from_seed_unchecked(&[9u8; 32]).unwrap();
                hex_encode(kp.public_key().as_ref())
            } else {
                "other".to_string()
            };
            assert_eq!(p.check(&wrong), Err(VerifyError::BadSignature), "{k}");
        }
    }

    #[test]
    fn stale_and_far_future_timestamps_are_rejected_for_every_scheme() {
        for k in ["hmac", "stripe", "ed25519"] {
            let (s, _sec, h) = signed(k, b"x", NOW);
            for now in [NOW + WINDOW as i64 + 1, NOW - FUTURE_SKEW_SECS - 1] {
                assert_eq!(
                    s.prepare(&h, b"x", WINDOW, now).unwrap_err(),
                    VerifyError::OutsideWindow,
                    "{k}"
                );
            }
        }
        let (s, _sec, h) = signed("twitch", b"x", NOW);
        let late = now_for("twitch") + 10_000;
        assert_eq!(
            s.prepare(&h, b"x", WINDOW, late).unwrap_err(),
            VerifyError::OutsideWindow
        );
    }

    #[test]
    fn empty_or_missing_secret_fails_closed_for_every_scheme() {
        for k in KINDS {
            let (s, _secret, h) = signed(k, b"x", NOW);
            let p = s.prepare(&h, b"x", WINDOW, now_for(k)).unwrap();
            assert_eq!(p.check(""), Err(VerifyError::BadKey), "{k}");
        }
    }

    #[test]
    fn missing_headers_are_rejected_for_every_scheme() {
        for k in KINDS {
            let (s, _secret, _h) = signed(k, b"x", NOW);
            assert_eq!(
                s.prepare(&HeaderMap::new(), b"x", WINDOW, now_for(k))
                    .unwrap_err(),
                VerifyError::MissingHeader,
                "{k}"
            );
        }
    }

    #[test]
    fn ed25519_malformed_key_is_bad_key() {
        let (s, _k, h) = signed("ed25519", b"x", NOW);
        let p = s.prepare(&h, b"x", WINDOW, NOW).unwrap();
        assert_eq!(p.check("zz"), Err(VerifyError::BadKey));
        assert_eq!(p.check("abcd"), Err(VerifyError::BadKey));
    }

    #[test]
    fn stripe_accepts_any_matching_v1_and_rejects_when_none_match() {
        let (s, secret, h) = signed("stripe", b"x", NOW);
        assert!(s
            .prepare(&h, b"x", WINDOW, NOW)
            .unwrap()
            .check(&secret)
            .is_ok());
        let h2 = hdrs(&[("stripe-signature", format!("t={NOW},v1=deadbeef"))]);
        assert_eq!(
            s.prepare(&h2, b"x", WINDOW, NOW).unwrap().check(&secret),
            Err(VerifyError::BadSignature)
        );
    }

    #[test]
    fn twitch_preset_matches_the_legacy_verify_signature() {
        let (s, secret, h) = signed("twitch", b"{\"x\":1}", NOW);
        let sig = h
            .get("twitch-eventsub-message-signature")
            .unwrap()
            .to_str()
            .unwrap();
        assert!(crate::ingest::twitch_eventsub::verify_signature(
            &secret,
            "msg-1",
            "2027-01-15T08:00:00Z",
            b"{\"x\":1}",
            sig
        ));
        let p = s
            .prepare(&h, b"{\"x\":1}", WINDOW, now_for("twitch"))
            .unwrap();
        assert!(p.check(&secret).is_ok());
        assert_eq!(p.dedup_id.len(), 64);
    }

    #[test]
    fn template_parse_rejects_unsafe_templates() {
        assert!(SignedTemplate::parse("{ts}").is_err());
        assert!(SignedTemplate::parse("{body}").is_err());
        assert!(SignedTemplate::parse("{ts}{body}{nope}").is_err());
        assert!(SignedTemplate::parse("{ts{body}").is_err());
        assert!(SignedTemplate::parse("{ts}.{body}").is_ok());
    }

    #[test]
    fn scheme_validate_requires_id_header_when_template_uses_id() {
        let mut s = match generic_hmac() {
            Scheme::HmacSha256(s) => s,
            _ => unreachable!(),
        };
        s.template = SignedTemplate::parse("{id}{ts}{body}").unwrap();
        assert!(Scheme::HmacSha256(s).validate().is_err());
        assert!(Scheme::twitch_eventsub().validate().is_ok());
    }

    #[test]
    fn replay_with_a_new_unsigned_id_header_keeps_the_same_dedup_id() {
        // Template omits {id}; the id header is NOT signed.
        let spec = |_: ()| HmacSpec {
            signature_header: HeaderName::from_static("x-sig"),
            timestamp_header: HeaderName::from_static("x-ts"),
            id_header: Some(HeaderName::from_static("x-id")),
            template: SignedTemplate::parse("{ts}.{body}").unwrap(),
            signature_prefix: String::new(),
            encoding: Encoding::Hex,
            timestamp_format: TimestampFormat::UnixSeconds,
        };
        let s = Scheme::HmacSha256(spec(()));
        let msg = format!("{NOW}.x");
        let sig = hmac_hex(SECRET, msg.as_bytes());
        let mk = |id: &str| {
            hdrs(&[
                ("x-ts", NOW.to_string()),
                ("x-sig", sig.clone()),
                ("x-id", id.to_string()),
            ])
        };
        let a = s.prepare(&mk("one"), b"x", WINDOW, NOW).unwrap();
        let b = s.prepare(&mk("two"), b"x", WINDOW, NOW).unwrap();
        assert!(a.check(SECRET).is_ok() && b.check(SECRET).is_ok());
        assert_eq!(a.dedup_id, b.dedup_id);
    }

    #[test]
    fn stripe_prepended_junk_v1_keeps_the_same_dedup_id_and_cap_is_enforced() {
        let (s, secret, h) = signed("stripe", b"x", NOW);
        let real = h.get("stripe-signature").unwrap().to_str().unwrap();
        let junk = hdrs(&[(
            "stripe-signature",
            format!("v1=00ff,{}", real.replace("v1=deadbeef,", "")),
        )]);
        let a = s.prepare(&h, b"x", WINDOW, NOW).unwrap();
        let b = s.prepare(&junk, b"x", WINDOW, NOW).unwrap();
        assert!(a.check(&secret).is_ok() && b.check(&secret).is_ok());
        assert_eq!(a.dedup_id, b.dedup_id);
        let many = hdrs(&[(
            "stripe-signature",
            format!("t={NOW},v1=00,v1=01,v1=02,v1=03,v1=04"),
        )]);
        assert!(s.prepare(&many, b"x", WINDOW, NOW).is_err());
        let two_t = hdrs(&[("stripe-signature", format!("t={NOW},t={NOW},v1=00"))]);
        assert!(s.prepare(&two_t, b"x", WINDOW, NOW).is_err());
    }

    #[test]
    fn adjacent_variable_placeholders_are_rejected_at_validate() {
        let mk = |t: &str| {
            Scheme::HmacSha256(HmacSpec {
                signature_header: HeaderName::from_static("x-sig"),
                timestamp_header: HeaderName::from_static("x-ts"),
                id_header: Some(HeaderName::from_static("x-id")),
                template: SignedTemplate::parse(t).unwrap(),
                signature_prefix: String::new(),
                encoding: Encoding::Hex,
                timestamp_format: TimestampFormat::UnixSeconds,
            })
        };
        assert!(mk("{id}{body}{ts}").validate().is_err());
        assert!(mk("{ts}{body}{id}").validate().is_err());
        assert!(mk("{id}.{body}.{ts}").validate().is_ok());
        assert!(mk("{id}{ts}{body}").validate().is_ok());
        assert!(Scheme::twitch_eventsub().validate().is_ok());
    }
}
