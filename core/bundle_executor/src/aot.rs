//! Precompiled (`.cwasm`) artifact trust chain: object keys, the signed
//! payload, and the verification that MUST pass before the executor hands
//! bytes to the `unsafe` `Component::deserialize` (executor-scale CONTRACT,
//! task T5).
//!
//! `Component::deserialize` maps the bytes in as native code -- a forged or
//! tampered artifact is remote code execution inside the sandbox host. So the
//! executor deserializes ONLY when every check below passes:
//!
//! 1. a platform signing key is configured (`BUNDLE_SIGNING_PUBLIC_KEYS`);
//!    no key = no AOT, never "unsigned is fine in dev";
//! 2. the detached signature document parses, names `ed25519` and a known
//!    `key_id`, and its `digest` / `engine_fingerprint` equal what THIS
//!    executor asked for (so an artifact for another bundle or another engine
//!    build is refused even if genuinely signed);
//! 3. SHA-256 of the fetched `.cwasm` equals the signed `cwasm_sha256`;
//! 4. the Ed25519 signature verifies (`verify_strict`) over
//!    [`cwasm_signing_payload`].
//!
//! Any failure is returned as an error; the caller (`crate::invoke`) logs it
//! at ERROR, counts `executor_precompile_fallback_total{reason}`, and
//! JIT-compiles the verified source component instead. The precompiler
//! (`--precompile` mode, a separate PR) signs with exactly
//! [`cwasm_signing_payload`] and writes the [`CwasmSignature`] JSON shape.

use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine as _;
use ed25519_dalek::Signature;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::error::ExecutorError;
use crate::signing::{write_length_prefixed, PlatformPublicKeys};

/// Domain separator for `.cwasm` signatures -- distinct from the bundle
/// sidecar's `waddles-bundle-sig-v1`, so a bundle signature can never be
/// replayed as an artifact signature or vice versa.
pub const CWASM_DOMAIN_SEPARATOR: &[u8] = b"waddles-cwasm-sig-v1";

/// Object-key prefix for precompiled artifacts in the bundle bucket.
pub const PRECOMPILED_PREFIX: &str = "precompiled";

/// The bare lowercase hex of a `sha256:<hex>` (or bare-hex) digest.
fn digest_hex(digest: &str) -> String {
    digest
        .strip_prefix("sha256:")
        .unwrap_or(digest)
        .to_ascii_lowercase()
}

/// `precompiled/<engine_fp>/<digest hex>.cwasm` -- the CONTRACT key layout.
#[must_use]
pub fn cwasm_key(engine_fingerprint: &str, digest: &str) -> String {
    format!(
        "{PRECOMPILED_PREFIX}/{engine_fingerprint}/{}.cwasm",
        digest_hex(digest)
    )
}

/// The detached signature object stored next to the artifact.
#[must_use]
pub fn cwasm_signature_key(engine_fingerprint: &str, digest: &str) -> String {
    format!("{}.sig", cwasm_key(engine_fingerprint, digest))
}

/// CONTRACT artifact id: `sha256(bundle wasm digest || engine_fingerprint)`,
/// lowercase hex. Bound into the signed payload so a signature is valid for
/// exactly one (bundle, engine) pair.
#[must_use]
pub fn artifact_id(digest: &str, engine_fingerprint: &str) -> String {
    let mut h = Sha256::new();
    h.update(digest_hex(digest).as_bytes());
    h.update(engine_fingerprint.as_bytes());
    format!("{:x}", h.finalize())
}

/// The detached `.cwasm.sig` JSON document.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CwasmSignature {
    /// Always `ed25519`.
    pub algorithm: String,
    /// Key id into `BUNDLE_SIGNING_PUBLIC_KEYS`.
    pub key_id: String,
    /// `sha256:<hex>` of the SOURCE component this was compiled from.
    pub digest: String,
    /// [`crate::engine::engine_fingerprint`] of the compiling engine.
    pub engine_fingerprint: String,
    /// Lowercase hex SHA-256 of the `.cwasm` bytes.
    pub cwasm_sha256: String,
    /// Base64 64-byte Ed25519 signature over [`cwasm_signing_payload`].
    pub signature: String,
}

/// The exact bytes the precompiler signs: domain separator, then the
/// length-prefixed artifact id, digest hex, engine fingerprint and cwasm hash.
#[must_use]
pub fn cwasm_signing_payload(digest: &str, engine_fingerprint: &str, cwasm_sha256: &str) -> Vec<u8> {
    let digest = digest_hex(digest);
    let id = artifact_id(&digest, engine_fingerprint);
    let mut buf = Vec::with_capacity(CWASM_DOMAIN_SEPARATOR.len() + 16 + 64 * 4);
    buf.extend_from_slice(CWASM_DOMAIN_SEPARATOR);
    write_length_prefixed(&mut buf, id.as_bytes());
    write_length_prefixed(&mut buf, digest.as_bytes());
    write_length_prefixed(&mut buf, engine_fingerprint.as_bytes());
    write_length_prefixed(&mut buf, cwasm_sha256.to_ascii_lowercase().as_bytes());
    buf
}

/// Verifies a fetched artifact against its detached signature for the
/// `(digest, engine_fingerprint)` this executor needs. `Ok(())` is the ONLY
/// condition under which the caller may `Component::deserialize` the bytes.
pub fn verify_cwasm(
    cwasm: &[u8],
    signature_doc: &[u8],
    keys: &PlatformPublicKeys,
    expected_digest: &str,
    expected_engine_fingerprint: &str,
) -> Result<(), ExecutorError> {
    if keys.is_empty() {
        return Err(ExecutorError::SignatureInvalid(
            "no BUNDLE_SIGNING_PUBLIC_KEYS configured; refusing to deserialize an unverifiable .cwasm"
                .to_string(),
        ));
    }
    let sig: CwasmSignature = serde_json::from_slice(signature_doc).map_err(|e| {
        ExecutorError::SignatureInvalid(format!("cwasm signature is not valid JSON: {e}"))
    })?;
    if sig.algorithm != "ed25519" {
        return Err(ExecutorError::SignatureInvalid(format!(
            "unsupported cwasm signature algorithm {:?}",
            sig.algorithm
        )));
    }
    if digest_hex(&sig.digest) != digest_hex(expected_digest) {
        return Err(ExecutorError::SignatureInvalid(format!(
            "cwasm signed for digest {:?}, expected {expected_digest:?}",
            sig.digest
        )));
    }
    if sig.engine_fingerprint != expected_engine_fingerprint {
        return Err(ExecutorError::SignatureInvalid(format!(
            "cwasm engine fingerprint {:?} does not match this executor's {expected_engine_fingerprint:?}",
            sig.engine_fingerprint
        )));
    }
    let actual = format!("{:x}", Sha256::digest(cwasm));
    if !actual.eq_ignore_ascii_case(&sig.cwasm_sha256) {
        return Err(ExecutorError::SignatureInvalid(
            "cwasm bytes do not match the signed cwasm_sha256 (tampered or truncated)".to_string(),
        ));
    }
    let key = keys.get(&sig.key_id).ok_or_else(|| {
        ExecutorError::SignatureInvalid(format!("unknown cwasm signing key id {:?}", sig.key_id))
    })?;
    let raw = BASE64.decode(sig.signature.as_bytes()).map_err(|e| {
        ExecutorError::SignatureInvalid(format!("cwasm signature is not valid base64: {e}"))
    })?;
    let arr: [u8; 64] = raw.as_slice().try_into().map_err(|_| {
        ExecutorError::SignatureInvalid(format!(
            "cwasm signature must decode to 64 bytes, got {}",
            raw.len()
        ))
    })?;
    let payload = cwasm_signing_payload(&sig.digest, &sig.engine_fingerprint, &sig.cwasm_sha256);
    key.verify_strict(&payload, &Signature::from_bytes(&arr))
        .map_err(|e| ExecutorError::SignatureInvalid(format!("cwasm signature verification failed: {e}")))
}

/// Test/precompiler helper: builds a signed [`CwasmSignature`] document.
/// Signing keys never live in the executor's runtime path; this exists for
/// tests and the `--precompile` mode, which holds the platform signing key.
#[must_use]
pub fn sign_cwasm(
    signing_key: &ed25519_dalek::SigningKey,
    key_id: &str,
    digest: &str,
    engine_fingerprint: &str,
    cwasm: &[u8],
) -> CwasmSignature {
    use ed25519_dalek::Signer;
    let cwasm_sha256 = format!("{:x}", Sha256::digest(cwasm));
    let payload = cwasm_signing_payload(digest, engine_fingerprint, &cwasm_sha256);
    CwasmSignature {
        algorithm: "ed25519".to_string(),
        key_id: key_id.to_string(),
        digest: format!("sha256:{}", digest_hex(digest)),
        engine_fingerprint: engine_fingerprint.to_string(),
        cwasm_sha256,
        signature: BASE64.encode(signing_key.sign(&payload).to_bytes()),
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use ed25519_dalek::SigningKey;

    const DIGEST: &str = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const FP: &str = "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff";

    fn key() -> SigningKey {
        SigningKey::from_bytes(&[9u8; 32])
    }

    fn keys() -> PlatformPublicKeys {
        let pk = BASE64.encode(key().verifying_key().to_bytes());
        PlatformPublicKeys::from_json(&format!(r#"{{"k1":"{pk}"}}"#)).unwrap()
    }

    fn signed(bytes: &[u8]) -> Vec<u8> {
        serde_json::to_vec(&sign_cwasm(&key(), "k1", DIGEST, FP, bytes)).unwrap()
    }

    #[test]
    fn key_layout_matches_the_contract() {
        assert_eq!(
            cwasm_key(FP, DIGEST),
            format!("precompiled/{FP}/{}.cwasm", &DIGEST[7..])
        );
        assert_eq!(cwasm_signature_key(FP, DIGEST), format!("{}.sig", cwasm_key(FP, DIGEST)));
    }

    #[test]
    fn a_genuine_artifact_verifies() {
        verify_cwasm(b"code", &signed(b"code"), &keys(), DIGEST, FP).unwrap();
    }

    #[test]
    fn tampered_bytes_are_refused() {
        let err = verify_cwasm(b"c0de", &signed(b"code"), &keys(), DIGEST, FP).unwrap_err();
        assert!(err.to_string().contains("tampered"), "{err}");
    }

    #[test]
    fn a_forged_hash_field_fails_the_signature() {
        let mut sig = sign_cwasm(&key(), "k1", DIGEST, FP, b"code");
        sig.cwasm_sha256 = format!("{:x}", Sha256::digest(b"evil"));
        let doc = serde_json::to_vec(&sig).unwrap();
        let err = verify_cwasm(b"evil", &doc, &keys(), DIGEST, FP).unwrap_err();
        assert!(err.to_string().contains("verification failed"), "{err}");
    }

    #[test]
    fn wrong_engine_fingerprint_is_refused() {
        let other = "0".repeat(64);
        let err = verify_cwasm(b"code", &signed(b"code"), &keys(), DIGEST, &other).unwrap_err();
        assert!(err.to_string().contains("fingerprint"), "{err}");
    }

    #[test]
    fn wrong_digest_is_refused() {
        let other = format!("sha256:{}", "b".repeat(64));
        let err = verify_cwasm(b"code", &signed(b"code"), &keys(), &other, FP).unwrap_err();
        assert!(err.to_string().contains("digest"), "{err}");
    }

    #[test]
    fn unknown_key_and_no_keys_are_refused() {
        let doc = serde_json::to_vec(&sign_cwasm(&key(), "k2", DIGEST, FP, b"code")).unwrap();
        assert!(verify_cwasm(b"code", &doc, &keys(), DIGEST, FP).is_err());
        let err =
            verify_cwasm(b"code", &signed(b"code"), &PlatformPublicKeys::default(), DIGEST, FP)
                .unwrap_err();
        assert!(err.to_string().contains("no BUNDLE_SIGNING_PUBLIC_KEYS"), "{err}");
    }

    #[test]
    fn garbage_signature_document_is_refused() {
        assert!(verify_cwasm(b"code", b"not json", &keys(), DIGEST, FP).is_err());
    }
}
