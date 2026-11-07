//! Kick webhook delivery verification and envelope construction.
//!
//! Scheme (official, <https://docs.kick.com/events/webhook-security>): every delivery carries
//! `Kick-Event-Message-Id` (ULID), `Kick-Event-Message-Timestamp` (RFC 3339),
//! `Kick-Event-Signature` (base64), `Kick-Event-Type` and `Kick-Event-Version`. The signature is
//! RSA PKCS#1 v1.5 / SHA-256 over `"{message_id}.{timestamp}.{raw body}"`, verified with Kick's
//! public key, which MUST be fetched from `GET https://api.kick.com/public/v1/public-key`
//! (never hardcoded: Kick rotates it). This replaces the legacy Python `X-Kick-Signature`
//! HMAC scheme, which is not part of Kick's documented API.
//!
//! Two phases, mirroring [`super::webhook_verify`]: [`prepare`] does the cheap structural work
//! (headers, timestamp window, signed-string construction) with no key involved; the returned
//! [`Prepared::verify`] runs the constant-time cryptographic check against a key the caller
//! resolved from the host-pinned public-key endpoint (never from the request). An empty or
//! unusable key fails closed. [`Prepared::envelope`] then builds the frame the Kick bundle
//! consumes (`{"type","version","message_id","timestamp","body"}`), because the event type
//! lives only in a header.
//!
//! Wiring status: this module is self-contained and fully tested; registering it as a
//! `Scheme` variant on a webhook route belongs to `webhook_verify.rs` (see the PR body,
//! "Needs from host seams").

use axum::http::{HeaderMap, HeaderName};
use base64::Engine as _;
use serde_json::{json, Value};

use super::webhook_verify::{VerifyError, FUTURE_SKEW_SECS};

/// The only place the verification key may come from (host constant).
pub const PUBLIC_KEY_URL: &str = "https://api.kick.com/public/v1/public-key";

const H_ID: HeaderName = HeaderName::from_static("kick-event-message-id");
const H_TS: HeaderName = HeaderName::from_static("kick-event-message-timestamp");
const H_SIG: HeaderName = HeaderName::from_static("kick-event-signature");
const H_TYPE: HeaderName = HeaderName::from_static("kick-event-type");
const H_VERSION: HeaderName = HeaderName::from_static("kick-event-version");

/// Longest accepted event-type header (`channel.subscription.renewal` is 28).
const MAX_TYPE_LEN: usize = 64;

/// A structurally valid, NOT yet signature-checked delivery.
#[derive(Debug)]
pub struct Prepared {
    signed: Vec<u8>,
    signature: Vec<u8>,
    event_type: String,
    version: u64,
    message_id: String,
    timestamp: String,
    /// Stable per-delivery id for dedup: the digest of the signed message, so a replay cannot
    /// dodge dedup by varying an unsigned header.
    pub dedup_id: String,
}

fn header_str<'a>(h: &'a HeaderMap, name: &HeaderName) -> Result<&'a str, VerifyError> {
    h.get(name)
        .and_then(|v| v.to_str().ok())
        .filter(|s| !s.is_empty())
        .ok_or(VerifyError::MissingHeader)
}

/// Phase 1: headers present and well-formed, timestamp inside `[now - window, now + skew]`,
/// signed string built from the EXACT raw body bytes.
///
/// # Errors
/// [`VerifyError::MissingHeader`], [`VerifyError::BadTimestamp`], [`VerifyError::OutsideWindow`]
/// or [`VerifyError::BadSignature`] (undecodable signature / malformed type or version).
pub fn prepare(
    headers: &HeaderMap,
    body: &[u8],
    now_unix: i64,
    window_secs: u64,
) -> Result<Prepared, VerifyError> {
    let message_id = header_str(headers, &H_ID)?;
    let timestamp = header_str(headers, &H_TS)?;
    let sig_b64 = header_str(headers, &H_SIG)?;
    let event_type = header_str(headers, &H_TYPE)?;
    let version = header_str(headers, &H_VERSION)?;

    let id_ok = message_id.len() <= 64
        && message_id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-');
    let type_ok = event_type.len() <= MAX_TYPE_LEN
        && event_type
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b == b'.' || b == b'_');
    let version = version
        .bytes()
        .all(|b| b.is_ascii_digit())
        .then(|| version.parse::<u64>().ok())
        .flatten();
    let (true, true, Some(version)) = (id_ok, type_ok, version) else {
        return Err(VerifyError::BadSignature);
    };

    let ts = chrono::DateTime::parse_from_rfc3339(timestamp)
        .map_err(|_| VerifyError::BadTimestamp)?
        .timestamp();
    let age = now_unix.saturating_sub(ts);
    let max = i64::try_from(window_secs).unwrap_or(i64::MAX);
    if age > max || age < -FUTURE_SKEW_SECS {
        return Err(VerifyError::OutsideWindow);
    }

    let signature = base64::engine::general_purpose::STANDARD
        .decode(sig_b64)
        .map_err(|_| VerifyError::BadSignature)?;

    let mut signed = Vec::with_capacity(message_id.len() + timestamp.len() + body.len() + 2);
    signed.extend_from_slice(message_id.as_bytes());
    signed.push(b'.');
    signed.extend_from_slice(timestamp.as_bytes());
    signed.push(b'.');
    signed.extend_from_slice(body);

    Ok(Prepared {
        dedup_id: super::webhook_verify::hex_encode(&<sha2::Sha256 as sha2::Digest>::digest(
            &signed,
        )),
        signed,
        signature,
        event_type: event_type.to_string(),
        version,
        message_id: message_id.to_string(),
        timestamp: timestamp.to_string(),
    })
}

impl Prepared {
    /// Phase 2: RSA PKCS#1 v1.5 / SHA-256 check against `public_key_pem` (Kick's published PEM
    /// `PUBLIC KEY`).
    ///
    /// # Errors
    /// [`VerifyError::BadKey`] for an absent/unusable key, [`VerifyError::BadSignature`] for a
    /// mismatch.
    pub fn verify(&self, public_key_pem: &str) -> Result<(), VerifyError> {
        let key = super::wix_jwt::public_key_pkcs1(public_key_pem)?;
        ring::signature::UnparsedPublicKey::new(&ring::signature::RSA_PKCS1_2048_8192_SHA256, &key)
            .verify(&self.signed, &self.signature)
            .map_err(|_| VerifyError::BadSignature)
    }

    /// The verified event type from the `Kick-Event-Type` header.
    #[must_use]
    pub fn event_type(&self) -> &str {
        &self.event_type
    }

    /// Builds the bundle's delivery envelope. Call ONLY after [`Prepared::verify`] succeeded.
    /// `body` must be a JSON object (every documented Kick payload is); anything else is `None`.
    #[must_use]
    pub fn envelope(&self, body: &[u8]) -> Option<Vec<u8>> {
        let body: Value = serde_json::from_slice(body).ok().filter(Value::is_object)?;
        serde_json::to_vec(&json!({
            "type": self.event_type,
            "version": self.version,
            "message_id": self.message_id,
            "timestamp": self.timestamp,
            "body": body,
        }))
        .ok()
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;
    use crate::connector::wix_jwt::wix_sign::{generate, sign, WixKey};
    use axum::http::HeaderValue;
    use std::sync::OnceLock;

    const NOW: i64 = 1_792_995_200;
    const TS: &str = "2026-10-26T06:13:20Z";
    const ID: &str = "01KBHE78QE4HZY1617DK5FC7YD";
    const BODY: &[u8] = br#"{"broadcaster":{"user_id":1},"follower":{"user_id":2}}"#;

    fn key() -> &'static WixKey {
        static K: OnceLock<WixKey> = OnceLock::new();
        K.get_or_init(generate)
    }

    fn headers(sig: &[u8], ty: &str) -> HeaderMap {
        let mut h = HeaderMap::new();
        for (n, v) in [
            (H_ID, ID.to_string()),
            (H_TS, TS.to_string()),
            (H_SIG, base64::engine::general_purpose::STANDARD.encode(sig)),
            (H_TYPE, ty.to_string()),
            (H_VERSION, "1".to_string()),
        ] {
            h.insert(n, HeaderValue::from_str(&v).unwrap());
        }
        h
    }

    fn signed_for(body: &[u8]) -> Vec<u8> {
        sign(
            key(),
            format!("{ID}.{TS}.{}", String::from_utf8_lossy(body)).as_bytes(),
        )
    }

    #[test]
    fn a_correctly_signed_delivery_verifies_and_builds_the_envelope() {
        let h = headers(&signed_for(BODY), "channel.followed");
        let p = prepare(&h, BODY, NOW, 300).unwrap();
        p.verify(&key().public_pem).unwrap();
        assert_eq!(p.event_type(), "channel.followed");
        let env: Value = serde_json::from_slice(&p.envelope(BODY).unwrap()).unwrap();
        assert_eq!(env["type"], "channel.followed");
        assert_eq!(env["version"], 1);
        assert_eq!(env["message_id"], ID);
        assert_eq!(env["body"]["follower"]["user_id"], 2);
    }

    #[test]
    fn tampered_body_type_or_key_fails_closed() {
        let sig = signed_for(BODY);
        // Body changed after signing.
        let p = prepare(&headers(&sig, "channel.followed"), b"{\"x\":1}", NOW, 300).unwrap();
        assert_eq!(p.verify(&key().public_pem), Err(VerifyError::BadSignature));
        // A different key.
        let other = generate();
        let p = prepare(&headers(&sig, "channel.followed"), BODY, NOW, 300).unwrap();
        assert_eq!(p.verify(&other.public_pem), Err(VerifyError::BadSignature));
        // Absent / junk key.
        for bad in [
            "",
            "   ",
            "not a key",
            "-----BEGIN PUBLIC KEY-----\n!!\n-----END PUBLIC KEY-----",
        ] {
            assert_eq!(p.verify(bad), Err(VerifyError::BadKey), "{bad:?}");
        }
        // Garbage signature bytes of a valid base64 shape.
        let p = prepare(&headers(&[7u8; 256], "channel.followed"), BODY, NOW, 300).unwrap();
        assert_eq!(p.verify(&key().public_pem), Err(VerifyError::BadSignature));
    }

    #[test]
    fn id_and_timestamp_are_part_of_the_signed_string() {
        let sig = signed_for(BODY);
        let mut h = headers(&sig, "channel.followed");
        h.insert(H_ID, HeaderValue::from_static("01OTHERID"));
        let p = prepare(&h, BODY, NOW, 300).unwrap();
        assert_eq!(p.verify(&key().public_pem), Err(VerifyError::BadSignature));
    }

    #[test]
    fn structural_failures_are_typed() {
        let sig = signed_for(BODY);
        for name in [H_ID, H_TS, H_SIG, H_TYPE, H_VERSION] {
            let mut h = headers(&sig, "channel.followed");
            h.remove(&name);
            assert_eq!(
                prepare(&h, BODY, NOW, 300).unwrap_err(),
                VerifyError::MissingHeader,
                "{name}"
            );
        }
        let mut h = headers(&sig, "channel.followed");
        h.insert(H_TS, HeaderValue::from_static("yesterday"));
        assert_eq!(
            prepare(&h, BODY, NOW, 300).unwrap_err(),
            VerifyError::BadTimestamp
        );
        // Outside the replay window (old and far-future).
        assert_eq!(
            prepare(&headers(&sig, "channel.followed"), BODY, NOW + 301, 300).unwrap_err(),
            VerifyError::OutsideWindow
        );
        assert_eq!(
            prepare(&headers(&sig, "channel.followed"), BODY, NOW - 120, 300).unwrap_err(),
            VerifyError::OutsideWindow
        );
        let mut h = headers(&sig, "channel.followed");
        h.insert(H_SIG, HeaderValue::from_static("***not-base64***"));
        assert_eq!(
            prepare(&h, BODY, NOW, 300).unwrap_err(),
            VerifyError::BadSignature
        );
        for (n, v) in [
            (H_TYPE, "Channel.Followed"),
            (H_TYPE, "a b"),
            (H_VERSION, "x1"),
            (H_ID, "a.b"),
        ] {
            let mut h = headers(&sig, "channel.followed");
            h.insert(n.clone(), HeaderValue::from_static(v));
            assert_eq!(
                prepare(&h, BODY, NOW, 300).unwrap_err(),
                VerifyError::BadSignature,
                "{n}={v}"
            );
        }
        let long = "a".repeat(MAX_TYPE_LEN + 1);
        let mut h = headers(&sig, "channel.followed");
        h.insert(H_TYPE, HeaderValue::from_str(&long).unwrap());
        assert_eq!(
            prepare(&h, BODY, NOW, 300).unwrap_err(),
            VerifyError::BadSignature
        );
    }

    #[test]
    fn dedup_id_is_stable_per_signed_message_and_envelope_rejects_non_objects() {
        let sig = signed_for(BODY);
        let a = prepare(&headers(&sig, "channel.followed"), BODY, NOW, 300).unwrap();
        let b = prepare(&headers(&sig, "channel.followed"), BODY, NOW + 5, 300).unwrap();
        assert_eq!(a.dedup_id, b.dedup_id);
        assert_eq!(a.dedup_id.len(), 64);
        for bad in [&b"[1]"[..], b"nope", b"\"s\"", b""] {
            assert!(a.envelope(bad).is_none());
        }
    }

    #[test]
    fn the_key_source_is_the_documented_endpoint() {
        assert_eq!(PUBLIC_KEY_URL, "https://api.kick.com/public/v1/public-key");
    }
}
