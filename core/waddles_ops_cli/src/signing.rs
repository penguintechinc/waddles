//! Bundle signing payload, signer and sidecar document.
//!
//! Parity contract (pinned by `tests/parity.rs` against vectors produced by the
//! Python implementation): payload = `DOMAIN_SEPARATOR || u32be(len)||app_id ||
//! u32be(len)||version || u32be(len)||digest || u64be(approval_id)`, signed
//! directly with Ed25519; sidecar = compact JSON with sorted keys.

use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine as _;
use ed25519_dalek::{Signature, Signer, SigningKey, VerifyingKey};
use serde::{Deserialize, Serialize};

/// Prefix of every signed payload; identical to the Python and executor constants.
pub const DOMAIN_SEPARATOR: &[u8] = b"waddles-bundle-sig-v1";
/// Env var holding the base64 32-byte Ed25519 seed (never a CLI arg).
pub const PRIVATE_KEY_ENV: &str = "BUNDLE_SIGNING_PRIVATE_KEY";
/// Env var holding the active key id label.
pub const KEY_ID_ENV: &str = "BUNDLE_SIGNING_KEY_ID";
/// Env var holding the verifier key set (JSON `key_id -> base64 pubkey`).
pub const PUBLIC_KEYS_ENV: &str = "BUNDLE_SIGNING_PUBLIC_KEYS";

/// Failure modes; every one is fatal (fail-loud, no fallback).
#[derive(Debug, thiserror::Error)]
pub enum SigningError {
    #[error("{0} and {1} must both be set -- artifact signing has no disabled mode")]
    KeyUnavailable(&'static str, &'static str),
    #[error("{0}: {1}")]
    KeyInvalid(&'static str, String),
    #[error("{field} {value:?} contains characters outside [A-Za-z0-9._-]")]
    InvalidCharset { field: &'static str, value: String },
    #[error("field {0} too long for a u32 length prefix")]
    FieldTooLong(&'static str),
    #[error("signature verification failed: {0}")]
    Verify(String),
    #[error("sidecar json: {0}")]
    Json(String),
}

/// The signed sidecar document uploaded next to the component.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SignedSidecar {
    pub app_id: String,
    pub version: String,
    pub digest: String,
    pub approval_id: i64,
    pub key_id: String,
    pub algorithm: String,
    pub signature: String,
}

/// True when `s` is non-empty and only `[A-Za-z0-9._-]` (Python `^[A-Za-z0-9._-]+$`).
fn charset_ok(s: &str) -> bool {
    !s.is_empty()
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
}

fn check(field: &'static str, value: &str) -> Result<(), SigningError> {
    if charset_ok(value) {
        Ok(())
    } else {
        Err(SigningError::InvalidCharset {
            field,
            value: value.to_string(),
        })
    }
}

fn push_prefixed(out: &mut Vec<u8>, field: &'static str, bytes: &[u8]) -> Result<(), SigningError> {
    let len = u32::try_from(bytes.len()).map_err(|_| SigningError::FieldTooLong(field))?;
    out.extend_from_slice(&len.to_be_bytes());
    out.extend_from_slice(bytes);
    Ok(())
}

/// Builds the exact byte payload that is Ed25519-signed.
///
/// `approval_id` is `i64` (DB BIGINT) and must be non-negative; Python packs it as `u64`.
pub fn build_signing_payload(
    app_id: &str,
    version: &str,
    digest: &str,
    approval_id: i64,
) -> Result<Vec<u8>, SigningError> {
    check("app_id", app_id)?;
    check("version", version)?;
    let approval = u64::try_from(approval_id)
        .map_err(|_| SigningError::KeyInvalid("approval_id", "must be non-negative".into()))?;
    let mut out = Vec::with_capacity(DOMAIN_SEPARATOR.len() + 64);
    out.extend_from_slice(DOMAIN_SEPARATOR);
    push_prefixed(&mut out, "app_id", app_id.as_bytes())?;
    push_prefixed(&mut out, "version", version.as_bytes())?;
    push_prefixed(&mut out, "digest", digest.as_bytes())?;
    out.extend_from_slice(&approval.to_be_bytes());
    Ok(out)
}

/// The one active platform signer; never exposes key material.
pub struct PlatformSigner {
    key_id: String,
    key: SigningKey,
}

impl PlatformSigner {
    /// Builds a signer from a base64 seed + key id (strict base64, exactly 32 bytes).
    pub fn new(seed_b64: &str, key_id: &str) -> Result<Self, SigningError> {
        if seed_b64.is_empty() || key_id.is_empty() {
            return Err(SigningError::KeyUnavailable(PRIVATE_KEY_ENV, KEY_ID_ENV));
        }
        check("key_id", key_id)?;
        let raw = BASE64
            .decode(seed_b64.as_bytes())
            .map_err(|_| SigningError::KeyInvalid(PRIVATE_KEY_ENV, "is not valid base64".into()))?;
        let seed: [u8; 32] = raw.as_slice().try_into().map_err(|_| {
            SigningError::KeyInvalid(
                PRIVATE_KEY_ENV,
                format!("must decode to exactly 32 bytes, got {}", raw.len()),
            )
        })?;
        Ok(Self {
            key_id: key_id.to_string(),
            key: SigningKey::from_bytes(&seed),
        })
    }

    /// Loads the signer from `BUNDLE_SIGNING_PRIVATE_KEY` / `BUNDLE_SIGNING_KEY_ID`.
    pub fn from_env() -> Result<Self, SigningError> {
        // Unset -> empty -> `new` rejects with `KeyUnavailable`; a present but
        // non-unicode value is a distinct, explicit `KeyInvalid` error.
        let read = |name: &'static str| match std::env::var(name) {
            Ok(v) => Ok(v),
            Err(std::env::VarError::NotPresent) => Ok(String::new()),
            Err(std::env::VarError::NotUnicode(_)) => Err(SigningError::KeyInvalid(
                name,
                "is not valid unicode".into(),
            )),
        };
        let seed = read(PRIVATE_KEY_ENV)?;
        let id = read(KEY_ID_ENV)?;
        Self::new(&seed, &id)
    }

    /// Signs the tuple and returns the sidecar document (base64 signature).
    pub fn sign(
        &self,
        app_id: &str,
        version: &str,
        digest: &str,
        approval_id: i64,
    ) -> Result<SignedSidecar, SigningError> {
        let payload = build_signing_payload(app_id, version, digest, approval_id)?;
        let sig = self.key.sign(&payload);
        Ok(SignedSidecar {
            app_id: app_id.into(),
            version: version.into(),
            digest: digest.into(),
            approval_id,
            key_id: self.key_id.clone(),
            algorithm: "ed25519".into(),
            signature: BASE64.encode(sig.to_bytes()),
        })
    }

    /// Public key, base64 (for provisioning `BUNDLE_SIGNING_PUBLIC_KEYS`).
    pub fn public_key_b64(&self) -> String {
        BASE64.encode(self.key.verifying_key().to_bytes())
    }
}

impl SignedSidecar {
    /// Compact, sorted-key, ASCII-escaped JSON -- byte-identical to Python's
    /// `json.dumps(doc, sort_keys=True, separators=(",", ":"))` (default `ensure_ascii`),
    /// including for the unvalidated `digest`/`key_id` text.
    pub fn to_canonical_json(&self) -> Result<String, SigningError> {
        let value = serde_json::to_value(self).map_err(|e| SigningError::Json(e.to_string()))?;
        crate::pyjson::dumps_canonical(&value).map_err(|e| SigningError::Json(e.to_string()))
    }
}

/// Parses `{key_id: base64 pubkey}` JSON.
pub fn parse_public_keys(
    raw: &str,
) -> Result<std::collections::HashMap<String, VerifyingKey>, SigningError> {
    let entries: std::collections::HashMap<String, String> =
        serde_json::from_str(raw).map_err(|e| SigningError::Json(e.to_string()))?;
    let mut keys = std::collections::HashMap::new();
    for (id, enc) in entries {
        let bytes = BASE64.decode(enc.as_bytes()).map_err(|e| {
            SigningError::KeyInvalid(PUBLIC_KEYS_ENV, format!("{id:?} base64: {e}"))
        })?;
        let arr: [u8; 32] = bytes.as_slice().try_into().map_err(|_| {
            SigningError::KeyInvalid(PUBLIC_KEYS_ENV, format!("{id:?} must be 32 bytes"))
        })?;
        let vk = VerifyingKey::from_bytes(&arr)
            .map_err(|e| SigningError::KeyInvalid(PUBLIC_KEYS_ENV, format!("{id:?}: {e}")))?;
        keys.insert(id, vk);
    }
    Ok(keys)
}

/// Verifies a sidecar exactly as the executor does (`verify_strict` over the payload).
pub fn verify_sidecar(
    sidecar_json: &[u8],
    keys: &std::collections::HashMap<String, VerifyingKey>,
) -> Result<SignedSidecar, SigningError> {
    let sc: SignedSidecar =
        serde_json::from_slice(sidecar_json).map_err(|e| SigningError::Json(e.to_string()))?;
    if sc.algorithm != "ed25519" {
        return Err(SigningError::Verify(format!(
            "unsupported algorithm {:?}",
            sc.algorithm
        )));
    }
    let vk = keys
        .get(&sc.key_id)
        .ok_or_else(|| SigningError::Verify(format!("unknown key id {:?}", sc.key_id)))?;
    let raw = BASE64
        .decode(sc.signature.as_bytes())
        .map_err(|e| SigningError::Verify(format!("signature base64: {e}")))?;
    let arr: [u8; 64] = raw
        .as_slice()
        .try_into()
        .map_err(|_| SigningError::Verify(format!("signature is {} bytes, want 64", raw.len())))?;
    let payload = build_signing_payload(&sc.app_id, &sc.version, &sc.digest, sc.approval_id)?;
    vk.verify_strict(&payload, &Signature::from_bytes(&arr))
        .map_err(|e| SigningError::Verify(e.to_string()))?;
    Ok(sc)
}
