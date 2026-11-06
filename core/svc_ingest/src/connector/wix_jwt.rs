//! Wix app-webhook JWT verification (`Scheme::WixJwt`).
//!
//! Wix delivers each webhook as a raw compact JWT (RS256) signed with the app
//! key pair; the app's PUBLIC key comes from the app dashboard. Verification is
//! HOST policy: the key is the connection's bearer secret resolved by route key
//! (never from the request), `alg` must be exactly `RS256` (no `none`, no
//! `HS*` key-confusion, no embedded `jwk`/`jku`/`x5*`/`crit` headers), freshness
//! comes from `iat`/`exp`, and the replay id is the Wix event id.
//!
//! Envelope (official shape, nested JSON strings): JWT payload `{data, iat,
//! exp}` -> `data` parses to `{instanceId, eventType, identity, data}` -> its
//! `data` parses to the domain event whose `id` is the unique event id.

use base64::Engine as _;
use serde_json::Value;
use subtle::ConstantTimeEq;

use super::webhook_verify::VerifyError;

/// A structurally valid, NOT yet signature-checked Wix webhook JWT.
#[derive(Debug)]
pub struct ParsedJwt {
    signing_input: Vec<u8>,
    signature: Vec<u8>,
    /// `iat` claim (unix seconds) when present.
    pub iat: Option<i64>,
    /// `exp` claim (unix seconds) when present.
    pub exp: Option<i64>,
    /// Wix event id (replay key). Untrusted until [`ParsedJwt::verify`] passes.
    pub event_id: String,
    instance_id: String,
}

const B64: base64::engine::GeneralPurpose = base64::engine::general_purpose::URL_SAFE_NO_PAD;

/// Header members that would let a token choose its own verification key.
const FORBIDDEN_HEADER: [&str; 5] = ["jwk", "jku", "x5u", "x5c", "crit"];

fn json_segment(seg: &str) -> Result<Value, VerifyError> {
    let raw = B64.decode(seg).map_err(|_| VerifyError::BadSignature)?;
    serde_json::from_slice(&raw).map_err(|_| VerifyError::BadSignature)
}

fn nested_json(v: &Value, key: &str) -> Result<Value, VerifyError> {
    let s = v
        .get(key)
        .and_then(Value::as_str)
        .ok_or(VerifyError::BadSignature)?;
    serde_json::from_str(s).map_err(|_| VerifyError::BadSignature)
}

fn nonempty_str(v: &Value, key: &str) -> Result<String, VerifyError> {
    v.get(key)
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .ok_or(VerifyError::BadSignature)
}

/// Parses `body` (the raw JWT) and enforces the freshness window. No key is
/// involved; nothing here is trusted until [`ParsedJwt::verify`] succeeds.
///
/// # Errors
/// [`VerifyError::BadSignature`] for any structural/algorithm violation,
/// [`VerifyError::OutsideWindow`] for a stale/expired/untimed token.
pub fn parse(
    body: &[u8],
    window_secs: u64,
    now_unix: i64,
    future_skew: i64,
) -> Result<ParsedJwt, VerifyError> {
    let text = std::str::from_utf8(body).map_err(|_| VerifyError::BadSignature)?;
    let text = text.trim_end_matches(['\r', '\n']);
    let mut parts = text.split('.');
    let (Some(h), Some(p), Some(s), None) =
        (parts.next(), parts.next(), parts.next(), parts.next())
    else {
        return Err(VerifyError::BadSignature);
    };
    let header = json_segment(h)?;
    let header_obj = header.as_object().ok_or(VerifyError::BadSignature)?;
    if header_obj.get("alg").and_then(Value::as_str) != Some("RS256")
        || FORBIDDEN_HEADER.iter().any(|k| header_obj.contains_key(*k))
    {
        return Err(VerifyError::BadSignature);
    }
    let payload = json_segment(p)?;
    let iat = payload.get("iat").and_then(Value::as_i64);
    let exp = payload.get("exp").and_then(Value::as_i64);
    if iat.is_none() && exp.is_none() {
        return Err(VerifyError::OutsideWindow);
    }
    let max = i64::try_from(window_secs).unwrap_or(i64::MAX);
    if let Some(iat) = iat {
        let age = now_unix.saturating_sub(iat);
        if age > max || age < -future_skew {
            return Err(VerifyError::OutsideWindow);
        }
    }
    if let Some(exp) = exp {
        if now_unix > exp.saturating_add(future_skew) {
            return Err(VerifyError::OutsideWindow);
        }
    }
    let envelope = nested_json(&payload, "data")?;
    let instance_id = nonempty_str(&envelope, "instanceId")?;
    nonempty_str(&envelope, "eventType")?;
    let event = nested_json(&envelope, "data")?;
    let event_id = nonempty_str(&event, "id")?;
    let signature = B64.decode(s).map_err(|_| VerifyError::BadSignature)?;
    Ok(ParsedJwt {
        signing_input: format!("{h}.{p}").into_bytes(),
        signature,
        iat,
        exp,
        event_id,
        instance_id,
    })
}

impl ParsedJwt {
    /// RS256 check against `key_text` (the connection's public key) and the
    /// instance binding: the signed `instanceId` must equal `expected_instance`.
    ///
    /// # Errors
    /// [`VerifyError::BadKey`] for an absent/unusable key,
    /// [`VerifyError::BadSignature`] for a signature or instance mismatch.
    pub fn verify(&self, key_text: &str, expected_instance: &str) -> Result<(), VerifyError> {
        let key = public_key_pkcs1(key_text)?;
        ring::signature::UnparsedPublicKey::new(&ring::signature::RSA_PKCS1_2048_8192_SHA256, &key)
            .verify(&self.signing_input, &self.signature)
            .map_err(|_| VerifyError::BadSignature)?;
        let same: bool = self
            .instance_id
            .as_bytes()
            .ct_eq(expected_instance.as_bytes())
            .into();
        if same && !expected_instance.is_empty() {
            Ok(())
        } else {
            Err(VerifyError::BadSignature)
        }
    }
}

/// Reads one DER TLV: `(tag, content, rest)`.
fn tlv(buf: &[u8]) -> Result<(u8, &[u8], &[u8]), VerifyError> {
    let bad = VerifyError::BadKey;
    let (&tag, rest) = buf.split_first().ok_or(bad)?;
    let (&l0, rest) = rest.split_first().ok_or(bad)?;
    let (len, rest) = if l0 < 0x80 {
        (usize::from(l0), rest)
    } else {
        let n = usize::from(l0 & 0x7f);
        if n == 0 || n > 4 || rest.len() < n {
            return Err(bad);
        }
        let len = rest[..n]
            .iter()
            .fold(0usize, |a, b| (a << 8) | usize::from(*b));
        (len, &rest[n..])
    };
    if rest.len() < len {
        return Err(bad);
    }
    Ok((tag, &rest[..len], &rest[len..]))
}

/// DER of rsaEncryption's AlgorithmIdentifier OID 1.2.840.113549.1.1.1.
const OID_RSA: [u8; 9] = [0x2a, 0x86, 0x48, 0x86, 0xf7, 0x0d, 0x01, 0x01, 0x01];

/// Extracts the PKCS#1 `RSAPublicKey` DER from `text`: PEM (`PUBLIC KEY` or
/// `RSA PUBLIC KEY`), base64 of a PEM, or base64 SPKI/PKCS#1 DER.
fn public_key_pkcs1(text: &str) -> Result<Vec<u8>, VerifyError> {
    let text = text.trim();
    if text.is_empty() {
        return Err(VerifyError::BadKey);
    }
    let der = pem_or_der(text, 0)?;
    let (tag, outer, rest) = tlv(&der)?;
    if tag != 0x30 || !rest.is_empty() {
        return Err(VerifyError::BadKey);
    }
    let (first, _, _) = tlv(outer)?;
    match first {
        // PKCS#1: SEQUENCE { INTEGER n, INTEGER e }
        0x02 => Ok(der),
        // SPKI: SEQUENCE { SEQUENCE { OID rsaEncryption, NULL }, BIT STRING }
        0x30 => {
            let (_, alg, after_alg) = tlv(outer)?;
            let (oid_tag, oid, _) = tlv(alg)?;
            if oid_tag != 0x06 || oid != OID_RSA {
                return Err(VerifyError::BadKey);
            }
            let (bs_tag, bits, tail) = tlv(after_alg)?;
            if bs_tag != 0x03 || !tail.is_empty() || bits.first() != Some(&0) {
                return Err(VerifyError::BadKey);
            }
            Ok(bits[1..].to_vec())
        }
        _ => Err(VerifyError::BadKey),
    }
}

fn pem_or_der(text: &str, depth: u8) -> Result<Vec<u8>, VerifyError> {
    let std = base64::engine::general_purpose::STANDARD;
    let body: String = if text.starts_with("-----BEGIN") {
        let mut lines = text.lines().map(str::trim);
        let begin = lines.next().ok_or(VerifyError::BadKey)?;
        if begin != "-----BEGIN PUBLIC KEY-----" && begin != "-----BEGIN RSA PUBLIC KEY-----" {
            return Err(VerifyError::BadKey);
        }
        lines
            .take_while(|l| !l.starts_with("-----END"))
            .collect::<String>()
    } else {
        text.split_whitespace().collect()
    };
    let der = std.decode(body).map_err(|_| VerifyError::BadKey)?;
    if der.starts_with(b"-----BEGIN") {
        // base64-wrapped PEM: unwrap exactly once.
        if depth > 0 {
            return Err(VerifyError::BadKey);
        }
        let inner = String::from_utf8(der).map_err(|_| VerifyError::BadKey)?;
        return pem_or_der(inner.trim(), depth + 1);
    }
    Ok(der)
}

#[cfg(test)]
#[path = "../../tests/support/wix_sign.rs"]
mod wix_sign;

#[cfg(test)]
mod tests {
    use super::wix_sign;
    use super::*;
    use serde_json::json;
    use std::sync::OnceLock;

    const NOW: i64 = 1_800_000_000;
    const WIN: u64 = 300;
    const SKEW: i64 = 60;
    const INST: &str = "inst-1";

    fn key() -> &'static wix_sign::WixKey {
        static K: OnceLock<wix_sign::WixKey> = OnceLock::new();
        K.get_or_init(wix_sign::generate)
    }

    fn other_key() -> &'static wix_sign::WixKey {
        static K: OnceLock<wix_sign::WixKey> = OnceLock::new();
        K.get_or_init(wix_sign::generate)
    }

    fn token(header: &Value, iat: Option<i64>, exp: Option<i64>) -> String {
        let inner = wix_sign::domain_event("evt-1", "actionEvent", &json!({"body": "{}"}));
        wix_sign::jwt(
            key(),
            header,
            INST,
            "wix.test.v1.entity_x",
            &inner,
            iat,
            exp,
        )
    }

    fn ok_token() -> String {
        token(&wix_sign::rs256(), Some(NOW - 5), Some(NOW + 55))
    }

    fn parse_at(t: &str) -> Result<ParsedJwt, VerifyError> {
        parse(t.as_bytes(), WIN, NOW, SKEW)
    }

    #[test]
    fn happy_path_verifies_and_exposes_event_id() {
        let p = parse_at(&ok_token()).unwrap();
        assert_eq!(p.event_id, "evt-1");
        p.verify(&key().public_pem, INST).unwrap();
    }

    #[test]
    fn trailing_newline_is_tolerated_and_key_forms_accepted() {
        let p = parse_at(&format!("{}\n", ok_token())).unwrap();
        p.verify(&key().public_pem, INST).unwrap();
        let b64_pem = base64::engine::general_purpose::STANDARD.encode(&key().public_pem);
        p.verify(&b64_pem, INST).unwrap();
        let body: String = key()
            .public_pem
            .lines()
            .filter(|l| !l.starts_with("-----"))
            .collect();
        p.verify(&body, INST).unwrap();
    }

    #[test]
    fn wrong_key_is_bad_signature() {
        let p = parse_at(&ok_token()).unwrap();
        assert_eq!(
            p.verify(&other_key().public_pem, INST),
            Err(VerifyError::BadSignature)
        );
    }

    #[test]
    fn tampered_payload_or_signature_is_bad_signature() {
        let t = ok_token();
        let mut parts: Vec<String> = t.split('.').map(str::to_owned).collect();
        // Swap the payload for another well-formed one, keep the signature.
        let forged_inner = wix_sign::domain_event("evt-9", "actionEvent", &json!({}));
        let forged = wix_sign::jwt(
            other_key(),
            &wix_sign::rs256(),
            INST,
            "x",
            &forged_inner,
            Some(NOW),
            None,
        );
        parts[1] = forged.split('.').nth(1).unwrap().to_owned();
        let p = parse_at(&parts.join(".")).unwrap();
        assert_eq!(
            p.verify(&key().public_pem, INST),
            Err(VerifyError::BadSignature)
        );
        let mut sig = B64.decode(&parts[2]).unwrap();
        sig[0] ^= 1;
        let bad = format!("{}.{}.{}", parts[0], parts[1], B64.encode(sig));
        let p = parse_at(&bad).unwrap();
        assert_eq!(
            p.verify(&key().public_pem, INST),
            Err(VerifyError::BadSignature)
        );
    }

    #[test]
    fn alg_confusion_is_rejected() {
        for alg in ["none", "HS256", "HS512", "RS512", "rs256", "ES256", "PS256"] {
            let t = token(&json!({"alg": alg}), Some(NOW), None);
            assert_eq!(parse_at(&t).err(), Some(VerifyError::BadSignature), "{alg}");
        }
        // Unsigned `alg=none` token with an empty signature segment.
        let h = B64.encode(r#"{"alg":"none"}"#);
        let t = ok_token();
        let p = t.split('.').nth(1).unwrap();
        assert!(parse_at(&format!("{h}.{p}.")).is_err());
        // Missing alg and key-selection headers.
        assert!(parse_at(&token(&json!({"typ": "JWT"}), Some(NOW), None)).is_err());
        for h in ["jwk", "jku", "x5u", "x5c", "crit"] {
            let t = token(&json!({"alg": "RS256", h: "x"}), Some(NOW), None);
            assert_eq!(parse_at(&t).err(), Some(VerifyError::BadSignature), "{h}");
        }
    }

    #[test]
    fn hs256_signed_with_the_public_key_text_cannot_verify() {
        // Classic confusion: HMAC the token with the PEM text as the secret.
        use hmac::{Hmac, Mac};
        let t = ok_token();
        let mut parts = t.split('.');
        let (_, p) = (parts.next(), parts.next().unwrap());
        let h = B64.encode(r#"{"alg":"HS256","typ":"JWT"}"#);
        let input = format!("{h}.{p}");
        let mut m = Hmac::<sha2::Sha256>::new_from_slice(key().public_pem.as_bytes()).unwrap();
        m.update(input.as_bytes());
        let forged = format!("{input}.{}", B64.encode(m.finalize().into_bytes()));
        assert_eq!(parse_at(&forged).err(), Some(VerifyError::BadSignature));
    }

    #[test]
    fn freshness_window_is_enforced() {
        let h = wix_sign::rs256();
        let cases = [
            (Some(NOW - 301), None, false),
            (Some(NOW - 300), None, true),
            (Some(NOW + 61), None, false),
            (Some(NOW + 60), None, true),
            (Some(NOW), Some(NOW - 61), false),
            (Some(NOW), Some(NOW - 60), true),
            (None, Some(NOW + 30), true),
            (None, None, false),
        ];
        for (iat, exp, ok) in cases {
            let r = parse_at(&token(&h, iat, exp));
            assert_eq!(r.is_ok(), ok, "iat={iat:?} exp={exp:?}");
            if !ok {
                assert_eq!(r.err(), Some(VerifyError::OutsideWindow));
            }
        }
    }

    #[test]
    fn instance_mismatch_and_empty_binding_fail() {
        let p = parse_at(&ok_token()).unwrap();
        assert_eq!(
            p.verify(&key().public_pem, "inst-2"),
            Err(VerifyError::BadSignature)
        );
        assert_eq!(
            p.verify(&key().public_pem, ""),
            Err(VerifyError::BadSignature)
        );
    }

    #[test]
    fn structural_garbage_is_rejected() {
        for t in [
            "",
            "a.b",
            "a.b.c.d",
            "!!.!!.!!",
            &format!("{}=", ok_token()),
        ] {
            assert!(parse_at(t).is_err(), "{t}");
        }
        // Missing nested id / instance / eventType.
        let h = B64.encode(r#"{"alg":"RS256"}"#);
        for payload in [
            json!({"iat": NOW, "data": "not json"}),
            json!({"iat": NOW, "data": "{}"}),
            json!({"iat": NOW, "data": r#"{"instanceId":"i","eventType":"e","data":"{}"}"#}),
        ] {
            let p = B64.encode(payload.to_string());
            assert!(parse_at(&format!("{h}.{p}.AAAA")).is_err());
        }
        assert!(parse(&[0xff, 0xfe], WIN, NOW, SKEW).is_err());
    }

    #[test]
    fn bad_keys_fail_closed() {
        let p = parse_at(&ok_token()).unwrap();
        for k in [
            "",
            "   ",
            "garbage",
            &format!(
                "-----BEGIN {} KEY-----\nAAAA\n-----END {0} KEY-----",
                "PRIV\u{41}TE"
            ),
            "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----",
            "MAAA",
        ] {
            assert_eq!(p.verify(k, INST), Err(VerifyError::BadKey), "{k}");
        }
        // Non-RSA SPKI (Ed25519) is refused.
        let ed = "-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEAGb9ECWmEzf6FQbrBZ9w7lshQhqowtrbLDFw4rXAxZuE=\n-----END PUBLIC KEY-----";
        assert_eq!(p.verify(ed, INST), Err(VerifyError::BadKey));
        // base64-wrapped PEM only unwraps once.
        let twice = base64::engine::general_purpose::STANDARD
            .encode(base64::engine::general_purpose::STANDARD.encode(&key().public_pem));
        assert_eq!(p.verify(&twice, INST), Err(VerifyError::BadKey));
    }

    #[test]
    fn tlv_rejects_truncated_and_oversized_lengths() {
        assert!(tlv(&[]).is_err());
        assert!(tlv(&[0x30]).is_err());
        assert!(tlv(&[0x30, 0x05, 0x01]).is_err());
        assert!(tlv(&[0x30, 0x80]).is_err());
        assert!(tlv(&[0x30, 0x85, 0, 0, 0, 0, 1]).is_err());
        assert_eq!(
            tlv(&[0x02, 0x01, 0x07, 0xaa]).unwrap(),
            (0x02, &[7u8][..], &[0xaau8][..])
        );
    }
}
