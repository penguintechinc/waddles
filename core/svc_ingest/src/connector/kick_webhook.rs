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
//! Wiring: [`super::webhook_verify::Scheme::Kick`] drives [`prepare`] / [`Prepared::verify_any`]
//! / [`Prepared::envelope`]; the key itself comes from [`super::kick_keys`] (cached, TTL, rotation).

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

/// Binding metadata key: the Kick user id of the broadcaster this connection is for. Kick signs
/// with ONE global key, so the signature alone cannot say which community a delivery belongs to;
/// the route is bound to this id and refuses every other broadcaster (like Wix's instance id).
pub const META_BROADCASTER_ID: &str = "broadcaster_user_id";

/// `true` for a Kick numeric user id: 1..=20 ASCII digits, not all zero.
#[must_use]
pub fn valid_broadcaster_id(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 20
        && s.bytes().all(|b| b.is_ascii_digit())
        && s.bytes().any(|b| b != b'0')
}

/// A body id as a string, whether Kick wrote a JSON number or a numeric string.
fn id_text(v: &Value) -> Option<String> {
    match v {
        Value::Number(n) => n.as_u64().map(|n| n.to_string()),
        Value::String(s) if valid_broadcaster_id(s) => Some(s.clone()),
        _ => None,
    }
}

/// The body keys that identify each handled event type. The `Kick-Event-Type` header is NOT
/// covered by the signature, so a captured delivery could be replayed under a different type;
/// requiring the claimed type's discriminators (and none of another type's) pins the type to
/// what the signed body actually is.
const DISCRIMINATORS: [(&[&str], &str); 5] = [
    (&["chat.message.sent"], "content"),
    (&["channel.followed"], "follower"),
    (
        &["channel.subscription.new", "channel.subscription.renewal"],
        "subscriber",
    ),
    (&["channel.subscription.gifts"], "giftees"),
    (&["livestream.status.updated"], "is_live"),
];

/// `true` when `body` has exactly the discriminator of `event_type` (an event type this host does
/// not discriminate passes: the guest ignores it).
fn shape_matches(event_type: &str, body: &Value) -> bool {
    let Some(own) = DISCRIMINATORS
        .iter()
        .find(|(types, _)| types.contains(&event_type))
    else {
        return true;
    };
    body.get(own.1).is_some()
        && DISCRIMINATORS
            .iter()
            .filter(|(_, key)| *key != own.1)
            .all(|(_, key)| body.get(*key).is_none())
}

/// A structurally valid, NOT yet signature-checked delivery.
#[derive(Debug)]
pub struct Prepared {
    signed: Vec<u8>,
    signature: Vec<u8>,
    event_type: String,
    version: u64,
    message_id: String,
    timestamp: String,
    /// `broadcaster.user_id` the body claims (trusted only after the signature verifies).
    broadcaster: Option<String>,
    /// The body is a JSON object whose shape matches the claimed event type.
    shape_ok: bool,
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

    let parsed: Option<Value> = serde_json::from_slice(body).ok().filter(Value::is_object);
    let broadcaster = parsed
        .as_ref()
        .and_then(|v| v.get("broadcaster")?.get("user_id"))
        .and_then(id_text);
    let shape_ok = parsed
        .as_ref()
        .is_some_and(|v| shape_matches(event_type, v));

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
        broadcaster,
        shape_ok,
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

    /// [`Prepared::verify`] against EVERY PEM block in `pems` (current key first, then the
    /// previous one during a rotation overlap); the first match wins. An input with no usable
    /// key fails closed as [`VerifyError::BadKey`]; usable keys that all mismatch are
    /// [`VerifyError::BadSignature`].
    ///
    /// # Errors
    /// As [`Prepared::verify`].
    pub fn verify_any(&self, pems: &str) -> Result<(), VerifyError> {
        const END: &str = "-----END PUBLIC KEY-----";
        let mut usable = false;
        for block in pems.split_inclusive(END).filter(|b| b.contains(END)) {
            match self.verify(block) {
                Ok(()) => return Ok(()),
                Err(VerifyError::BadKey) => {}
                Err(_) => usable = true,
            }
        }
        if usable {
            Err(VerifyError::BadSignature)
        } else {
            Err(VerifyError::BadKey)
        }
    }

    /// [`Prepared::verify_any`], then the connection binding: the (now authenticated) body's
    /// broadcaster must be `expected_broadcaster` and its shape must match the claimed event
    /// type. Order matters: nothing about the binding is revealed for a forged signature.
    ///
    /// # Errors
    /// As [`Prepared::verify_any`], plus [`VerifyError::NotBound`] for a genuine Kick delivery
    /// that belongs to another broadcaster/connection or whose type does not match its body.
    pub fn verify_bound(&self, pems: &str, expected_broadcaster: &str) -> Result<(), VerifyError> {
        self.verify_any(pems)?;
        if self.shape_ok && self.broadcaster.as_deref() == Some(expected_broadcaster) {
            Ok(())
        } else {
            Err(VerifyError::NotBound)
        }
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

    #[test]
    fn a_genuine_delivery_for_another_broadcaster_is_refused_after_the_signature() {
        let sig = signed_for(BODY);
        let p = prepare(&headers(&sig, "channel.followed"), BODY, NOW, 300).unwrap();
        // BODY's broadcaster is user 1: bound to 1 verifies, to anyone else is NotBound.
        p.verify_bound(&key().public_pem, "1").unwrap();
        for other in ["2", "10", "01", ""] {
            assert_eq!(
                p.verify_bound(&key().public_pem, other),
                Err(VerifyError::NotBound),
                "{other:?}"
            );
        }
        // A forged signature never reveals the binding result.
        let forged = prepare(&headers(&[7u8; 256], "channel.followed"), BODY, NOW, 300).unwrap();
        assert_eq!(
            forged.verify_bound(&key().public_pem, "2"),
            Err(VerifyError::BadSignature)
        );
        // String ids bind the same way as numbers.
        let body = br#"{"broadcaster":{"user_id":"77"},"follower":{"user_id":2}}"#;
        let p = prepare(
            &headers(&signed_for(body), "channel.followed"),
            body,
            NOW,
            300,
        )
        .unwrap();
        p.verify_bound(&key().public_pem, "77").unwrap();
    }

    #[test]
    fn a_body_without_a_usable_broadcaster_is_never_bound() {
        for body in [
            &br#"{"follower":{"user_id":2}}"#[..],
            br#"{"broadcaster":{},"follower":{"user_id":2}}"#,
            br#"{"broadcaster":{"user_id":-1},"follower":{"user_id":2}}"#,
            br#"{"broadcaster":{"user_id":"x1"},"follower":{"user_id":2}}"#,
            br#"[1]"#,
        ] {
            let p = prepare(
                &headers(&signed_for(body), "channel.followed"),
                body,
                NOW,
                300,
            )
            .unwrap();
            assert_eq!(
                p.verify_bound(&key().public_pem, "1"),
                Err(VerifyError::NotBound),
                "{}",
                String::from_utf8_lossy(body)
            );
        }
    }

    /// The type header is unsigned: a genuine delivery replayed under another type is refused.
    #[test]
    fn the_unsigned_type_header_must_match_the_signed_body() {
        let sig = signed_for(BODY);
        for lie in [
            "chat.message.sent",
            "channel.subscription.new",
            "channel.subscription.renewal",
            "channel.subscription.gifts",
            "livestream.status.updated",
        ] {
            let p = prepare(&headers(&sig, lie), BODY, NOW, 300).unwrap();
            assert_eq!(
                p.verify_bound(&key().public_pem, "1"),
                Err(VerifyError::NotBound),
                "{lie}"
            );
        }
        // A body mixing two event shapes is refused under either type.
        let mixed = br#"{"broadcaster":{"user_id":1},"follower":{"user_id":2},"is_live":true}"#;
        let sig = signed_for(mixed);
        for ty in ["channel.followed", "livestream.status.updated"] {
            let p = prepare(&headers(&sig, ty), mixed, NOW, 300).unwrap();
            assert_eq!(
                p.verify_bound(&key().public_pem, "1"),
                Err(VerifyError::NotBound)
            );
        }
        // The honest type verifies, and an event type this host does not discriminate passes.
        let live = br#"{"broadcaster":{"user_id":1},"is_live":true}"#;
        for ty in ["livestream.status.updated", "kicks.gifted"] {
            let p = prepare(&headers(&signed_for(live), ty), live, NOW, 300).unwrap();
            p.verify_bound(&key().public_pem, "1").unwrap();
        }
    }

    #[test]
    fn verify_any_tries_each_key_block_and_fails_closed_without_a_usable_one() {
        let sig = signed_for(BODY);
        let p = prepare(&headers(&sig, "channel.followed"), BODY, NOW, 300).unwrap();
        let other = generate();
        let both = format!("{}\n{}", other.public_pem, key().public_pem);
        p.verify_any(&both).unwrap();
        assert_eq!(
            p.verify_any(&other.public_pem),
            Err(VerifyError::BadSignature)
        );
        for none in [
            "",
            "garbage",
            "-----BEGIN PUBLIC KEY-----\n!!\n-----END PUBLIC KEY-----",
        ] {
            assert_eq!(p.verify_any(none), Err(VerifyError::BadKey), "{none:?}");
        }
    }

    #[test]
    fn broadcaster_ids_are_numeric_user_ids() {
        for ok in ["1", "123456", "18446744073709551615"] {
            assert!(valid_broadcaster_id(ok), "{ok}");
        }
        for bad in ["", "0", "000", "-1", "1a", " 1", "123456789012345678901"] {
            assert!(!valid_broadcaster_id(bad), "{bad:?}");
        }
    }
}
