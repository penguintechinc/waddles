//! Platform Ed25519 artifact-signature verification (spec
//! `docs/superpowers/specs/2026-09-28-bundle-permissions-and-capability-gate.md`
//! SS5.6, Gemini review condition 9): hub-api signs the approved
//! component's digest at global-tier approval; this executor verifies that
//! signature against a configured platform public key **before**
//! instantiating any component it loads, for every load -- a missing or
//! invalid signature fails closed (the component is never instantiated,
//! never degraded to a warning).
//!
//! **Where the signature travels.** The wire protocol's `LoadBody`
//! (`penguin-bundle-host::wire`, an external, cross-repo crate) has no
//! signature-shaped field, and this crate does not own that schema. The
//! seam this module uses instead is the one already anticipated on both
//! sides of this contract: `hub_api/services/storage_service.py`'s own
//! docstring calls the `.json` sidecar object next to the compiled
//! component "a future milestone's scan-result/signature metadata slot",
//! and this crate's `crate::bucket` module doc already notes "the spec
//! SS7.6 step 4 Ed25519 signature check over the sidecar is a separate,
//! not-yet-wired verification step" -- this module is that wiring.
//! `crate::bucket::BucketComponentSource::fetch` now fetches the sidecar
//! bytes alongside the component bytes; `crate::invoke::Executor::on_load`
//! parses them as a [`SignedSidecar`] and calls [`verify_artifact_signature`].
//!
//! **Signed payload contract (must match
//! `hub_api/services/bundle_signing_service.py::build_signing_payload`
//! byte-for-byte, or every signature fails to verify):**
//! `app_id + "\0" + version + "\0" + digest + "\0" + approval_id`, UTF-8
//! encoded, signed directly with Ed25519 (no extra hashing layer -- Ed25519
//! already hashes the message internally). NUL-separated rather than a
//! canonical-JSON encoding deliberately: matching two independent
//! languages' JSON canonicalization (integer formatting, key ordering,
//! escaping) byte-for-byte is a real, recurring cross-language footgun;
//! four opaque, NUL-delimited fields sidesteps it entirely. Including
//! `app_id`/`version`/`digest`/`approval_id` together (task instruction)
//! is what prevents swapping: an attacker who swaps the bucket object at
//! `component_key` for a different, also-signed component cannot reuse
//! that other component's signature here, because the digest embedded in
//! its own signed payload will not match this component's actual bytes
//! (checked independently by `crate::invoke::verify_digest`) or its own
//! `app_id`/`version`/`approval_id`.

use std::collections::HashMap;

use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine as _;
use ed25519_dalek::{Signature, VerifyingKey};
use serde::Deserialize;

use crate::config::CliConfig;
use crate::error::ExecutorError;

/// The sidecar JSON document hub-api's `bundle_signing_service.
/// upload_signed_sidecar()` writes to `sidecar_key` at approval time.
/// Deliberately does NOT `#[serde(deny_unknown_fields)]`: the sidecar is
/// also a general scan-result/manifest metadata slot
/// (`storage_service.py`'s own doc), so an unrelated future field must
/// never break parsing here.
#[derive(Debug, Deserialize)]
pub struct SignedSidecar {
    pub app_id: String,
    pub version: String,
    /// `sha256:<64 hex>` -- same shape as `LoadBody.digest`.
    pub digest: String,
    pub approval_id: i64,
    pub key_id: String,
    pub algorithm: String,
    /// Base64-encoded 64-byte Ed25519 signature over
    /// [`signing_payload`]`(app_id, version, digest, approval_id)`.
    pub signature: String,
}

/// The exact byte payload that was signed -- see this module's doc for why
/// NUL-separated fields rather than JSON.
pub fn signing_payload(app_id: &str, version: &str, digest: &str, approval_id: i64) -> Vec<u8> {
    format!("{app_id}\0{version}\0{digest}\0{approval_id}").into_bytes()
}

/// Platform Ed25519 public keys, keyed by key id -- supports rotation
/// (spec SS5.6: "support rotation via a key id"): a signature made with a
/// retired key still verifies as long as its `key_id` entry remains in
/// this map; a newly-issued key id is added without removing the old one
/// until every artifact signed under it has been re-signed.
#[derive(Debug, Clone, Default)]
pub struct PlatformPublicKeys {
    keys: HashMap<String, VerifyingKey>,
}

impl PlatformPublicKeys {
    /// Parses `BUNDLE_SIGNING_PUBLIC_KEYS`'s JSON shape: an object mapping
    /// `key_id -> base64(32-byte Ed25519 public key)`. Every entry must
    /// decode to a well-formed key -- a malformed entry is a configuration
    /// error (fails closed at startup, `crate::lib::run`'s validation
    /// path), never a silently-skipped one.
    pub fn from_json(raw: &str) -> Result<Self, ExecutorError> {
        let entries: HashMap<String, String> = serde_json::from_str(raw).map_err(|e| {
            ExecutorError::Config(format!("BUNDLE_SIGNING_PUBLIC_KEYS is not valid JSON: {e}"))
        })?;
        let mut keys = HashMap::with_capacity(entries.len());
        for (key_id, encoded) in entries {
            let bytes = BASE64.decode(encoded.as_bytes()).map_err(|e| {
                ExecutorError::Config(format!(
                    "BUNDLE_SIGNING_PUBLIC_KEYS[{key_id:?}] is not valid base64: {e}"
                ))
            })?;
            let array: [u8; 32] = bytes.as_slice().try_into().map_err(|_| {
                ExecutorError::Config(format!(
                    "BUNDLE_SIGNING_PUBLIC_KEYS[{key_id:?}] must decode to exactly 32 bytes, got {}",
                    bytes.len()
                ))
            })?;
            let verifying_key = VerifyingKey::from_bytes(&array).map_err(|e| {
                ExecutorError::Config(format!(
                    "BUNDLE_SIGNING_PUBLIC_KEYS[{key_id:?}] is not a valid Ed25519 public key: {e}"
                ))
            })?;
            keys.insert(key_id, verifying_key);
        }
        Ok(Self { keys })
    }

    /// Resolves `cfg.bundle_signing_public_keys` leniently: unset or blank
    /// yields an empty key set (signature verification is then skipped by
    /// `crate::invoke::Executor::on_load` -- see that field's own doc for
    /// why this is safe: `crate::lib::run`'s production wiring calls
    /// [`Self::from_cli_required`] first and refuses to start otherwise,
    /// so `Executor::new` only ever sees an empty key set in a context
    /// -- tests -- that never claims to be enforcing signatures). A value
    /// that IS set but fails to parse still propagates as a hard `Config`
    /// error -- a typo in a real deployment's env var must never be
    /// silently treated the same as "not configured".
    pub fn from_cli(cfg: &CliConfig) -> Result<Self, ExecutorError> {
        match cfg.bundle_signing_public_keys.as_deref().map(str::trim) {
            None | Some("") => Ok(Self::default()),
            Some(raw) => Self::from_json(raw),
        }
    }

    /// The strict counterpart `crate::lib::run` calls before constructing
    /// the production `Executor`: fails closed if no platform key is
    /// configured at all, rather than silently running with signature
    /// verification off (spec SS5.6 has no "unsigned" mode; that state is
    /// only ever reachable in tests that construct `Executor` directly).
    pub fn from_cli_required(cfg: &CliConfig) -> Result<Self, ExecutorError> {
        let keys = Self::from_cli(cfg)?;
        if keys.is_empty() {
            return Err(ExecutorError::Config(
                "BUNDLE_SIGNING_PUBLIC_KEYS must be set to at least one key id -- artifact \
                 signature verification has no supported \"disabled\" mode in production"
                    .to_string(),
            ));
        }
        Ok(keys)
    }

    pub fn is_empty(&self) -> bool {
        self.keys.is_empty()
    }

    fn get(&self, key_id: &str) -> Option<&VerifyingKey> {
        self.keys.get(key_id)
    }
}

/// Parses `sidecar_bytes` as a [`SignedSidecar`] and verifies its embedded
/// Ed25519 signature against `keys`, cross-checking every swap-prevention
/// field (`app_id`/`version`/`digest`) against the values the **stage**
/// actually sent in the `load` frame -- never trusting the sidecar's own
/// claimed identity over the load frame's, since the sidecar is
/// bucket-fetched content the same untrusted party that could swap
/// `component_key` could also have swapped.
///
/// Fails closed (an `ExecutorError::SignatureInvalid`, mapped to
/// `ErrorCode::LoadFailed` by `crate::invoke::on_load`) on: malformed/
/// non-JSON sidecar bytes, an unsupported `algorithm`, a mismatched
/// `app_id`/`version`/`digest`, an unknown `key_id`, a malformed base64/
/// length signature, or a signature that does not verify.
pub fn verify_artifact_signature(
    sidecar_bytes: &[u8],
    keys: &PlatformPublicKeys,
    expected_app_id: &str,
    expected_version: &str,
    expected_digest: &str,
) -> Result<(), ExecutorError> {
    let sidecar: SignedSidecar = serde_json::from_slice(sidecar_bytes)
        .map_err(|e| ExecutorError::SignatureInvalid(format!("sidecar is not valid JSON: {e}")))?;

    if sidecar.algorithm != "ed25519" {
        return Err(ExecutorError::SignatureInvalid(format!(
            "unsupported signature algorithm {:?}",
            sidecar.algorithm
        )));
    }
    if sidecar.app_id != expected_app_id {
        return Err(ExecutorError::SignatureInvalid(format!(
            "sidecar app_id {:?} does not match the load frame's app_id {expected_app_id:?}",
            sidecar.app_id
        )));
    }
    if sidecar.version != expected_version {
        return Err(ExecutorError::SignatureInvalid(format!(
            "sidecar version {:?} does not match the load frame's version {expected_version:?}",
            sidecar.version
        )));
    }
    if sidecar.digest != expected_digest {
        return Err(ExecutorError::SignatureInvalid(format!(
            "sidecar digest {:?} does not match the load frame's digest {expected_digest:?}",
            sidecar.digest
        )));
    }

    let verifying_key = keys.get(&sidecar.key_id).ok_or_else(|| {
        ExecutorError::SignatureInvalid(format!(
            "unknown artifact-signing key id {:?}",
            sidecar.key_id
        ))
    })?;

    let signature_bytes = BASE64.decode(sidecar.signature.as_bytes()).map_err(|e| {
        ExecutorError::SignatureInvalid(format!("signature is not valid base64: {e}"))
    })?;
    let signature_array: [u8; 64] = signature_bytes.as_slice().try_into().map_err(|_| {
        ExecutorError::SignatureInvalid(format!(
            "signature must decode to exactly 64 bytes, got {}",
            signature_bytes.len()
        ))
    })?;
    let signature = Signature::from_bytes(&signature_array);

    let payload = signing_payload(
        &sidecar.app_id,
        &sidecar.version,
        &sidecar.digest,
        sidecar.approval_id,
    );
    verifying_key
        .verify_strict(&payload, &signature)
        .map_err(|e| ExecutorError::SignatureInvalid(format!("signature verification failed: {e}")))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use ed25519_dalek::{Signer, SigningKey};
    use serde_json::json;

    use super::*;

    /// Deterministic test keypair -- fine for tests (never a real platform
    /// key), avoids pulling `rand_core`'s OS-RNG feature into this crate's
    /// non-dev dependency set purely for test fixtures.
    fn test_signing_key() -> SigningKey {
        SigningKey::from_bytes(&[7u8; 32])
    }

    fn test_public_keys_json(key_id: &str, signing_key: &SigningKey) -> String {
        let encoded = BASE64.encode(signing_key.verifying_key().to_bytes());
        json!({ key_id: encoded }).to_string()
    }

    fn sign_sidecar(
        signing_key: &SigningKey,
        key_id: &str,
        app_id: &str,
        version: &str,
        digest: &str,
        approval_id: i64,
    ) -> Vec<u8> {
        let payload = signing_payload(app_id, version, digest, approval_id);
        let signature = signing_key.sign(&payload);
        serde_json::to_vec(&json!({
            "app_id": app_id,
            "version": version,
            "digest": digest,
            "approval_id": approval_id,
            "key_id": key_id,
            "algorithm": "ed25519",
            "signature": BASE64.encode(signature.to_bytes()),
        }))
        .expect("serializable fixture")
    }

    const APP_ID: &str = "waddles.test.signing";
    const VERSION: &str = "1";
    const DIGEST: &str = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const APPROVAL_ID: i64 = 42;

    #[test]
    fn platform_public_keys_from_cli_is_empty_when_unset() -> Result<(), ExecutorError> {
        use clap::Parser;
        let cfg = CliConfig::try_parse_from([
            "bundle-executor",
            "--stage-host-api-addr",
            "svc-process:8301",
        ])
        .expect("static test args always parse");
        let keys = PlatformPublicKeys::from_cli(&cfg)?;
        assert!(keys.is_empty());
        Ok(())
    }

    #[test]
    fn platform_public_keys_from_cli_required_fails_closed_when_unset() {
        use clap::Parser;
        let cfg = CliConfig::try_parse_from([
            "bundle-executor",
            "--stage-host-api-addr",
            "svc-process:8301",
        ])
        .expect("static test args always parse");
        assert!(matches!(
            PlatformPublicKeys::from_cli_required(&cfg),
            Err(ExecutorError::Config(_))
        ));
    }

    #[test]
    fn from_json_rejects_malformed_json() {
        assert!(matches!(
            PlatformPublicKeys::from_json("not json"),
            Err(ExecutorError::Config(_))
        ));
    }

    #[test]
    fn from_json_rejects_non_base64_entries() {
        assert!(matches!(
            PlatformPublicKeys::from_json(r#"{"k1":"not-base64!!"}"#),
            Err(ExecutorError::Config(_))
        ));
    }

    #[test]
    fn from_json_rejects_a_wrong_length_key() {
        let short = BASE64.encode([1u8; 16]);
        let raw = json!({ "k1": short }).to_string();
        assert!(matches!(
            PlatformPublicKeys::from_json(&raw),
            Err(ExecutorError::Config(_))
        ));
    }

    #[test]
    fn verify_artifact_signature_accepts_a_valid_signature() -> Result<(), ExecutorError> {
        let signing_key = test_signing_key();
        let keys = PlatformPublicKeys::from_json(&test_public_keys_json("k1", &signing_key))?;
        let sidecar = sign_sidecar(&signing_key, "k1", APP_ID, VERSION, DIGEST, APPROVAL_ID);
        verify_artifact_signature(&sidecar, &keys, APP_ID, VERSION, DIGEST)
    }

    #[test]
    fn verify_artifact_signature_rejects_a_tampered_digest() -> Result<(), ExecutorError> {
        let signing_key = test_signing_key();
        let keys = PlatformPublicKeys::from_json(&test_public_keys_json("k1", &signing_key))?;
        // Signed for DIGEST, but the load frame (and the sidecar's own
        // claimed digest check) asks for a different one -- this is
        // exactly the "prevent swapping" property the task requires.
        let tampered_digest =
            "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
        let sidecar = sign_sidecar(&signing_key, "k1", APP_ID, VERSION, DIGEST, APPROVAL_ID);
        let err = verify_artifact_signature(&sidecar, &keys, APP_ID, VERSION, tampered_digest);
        assert!(matches!(err, Err(ExecutorError::SignatureInvalid(_))));
        Ok(())
    }

    #[test]
    fn verify_artifact_signature_rejects_a_sidecar_forged_with_a_different_digest_field(
    ) -> Result<(), ExecutorError> {
        // A sidecar whose *claimed* digest field was edited after signing
        // (so the claim matches the load frame) must still fail: the
        // signature was computed over the ORIGINAL digest, not the edited
        // claim, so `verify_strict` itself catches this even though the
        // three-field pre-check above would pass.
        let signing_key = test_signing_key();
        let keys = PlatformPublicKeys::from_json(&test_public_keys_json("k1", &signing_key))?;
        let tampered_digest =
            "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc";
        let payload = signing_payload(APP_ID, VERSION, DIGEST, APPROVAL_ID);
        let signature = signing_key.sign(&payload);
        let forged = serde_json::to_vec(&json!({
            "app_id": APP_ID,
            "version": VERSION,
            "digest": tampered_digest, // claim edited to match the load frame...
            "approval_id": APPROVAL_ID,
            "key_id": "k1",
            "algorithm": "ed25519",
            "signature": BASE64.encode(signature.to_bytes()), // ...but the signature wasn't
        }))
        .expect("serializable fixture");
        let err = verify_artifact_signature(&forged, &keys, APP_ID, VERSION, tampered_digest);
        assert!(matches!(err, Err(ExecutorError::SignatureInvalid(_))));
        Ok(())
    }

    #[test]
    fn verify_artifact_signature_rejects_a_wrong_app_id() -> Result<(), ExecutorError> {
        let signing_key = test_signing_key();
        let keys = PlatformPublicKeys::from_json(&test_public_keys_json("k1", &signing_key))?;
        let sidecar = sign_sidecar(&signing_key, "k1", APP_ID, VERSION, DIGEST, APPROVAL_ID);
        let err = verify_artifact_signature(
            &sidecar,
            &keys,
            "waddles.test.a-different-app",
            VERSION,
            DIGEST,
        );
        assert!(matches!(err, Err(ExecutorError::SignatureInvalid(_))));
        Ok(())
    }

    #[test]
    fn verify_artifact_signature_rejects_a_wrong_version() -> Result<(), ExecutorError> {
        let signing_key = test_signing_key();
        let keys = PlatformPublicKeys::from_json(&test_public_keys_json("k1", &signing_key))?;
        let sidecar = sign_sidecar(&signing_key, "k1", APP_ID, VERSION, DIGEST, APPROVAL_ID);
        let err = verify_artifact_signature(&sidecar, &keys, APP_ID, "2", DIGEST);
        assert!(matches!(err, Err(ExecutorError::SignatureInvalid(_))));
        Ok(())
    }

    #[test]
    fn verify_artifact_signature_rejects_an_unknown_key_id() -> Result<(), ExecutorError> {
        let signing_key = test_signing_key();
        // `keys` only knows about "k1"; the sidecar claims "k2".
        let keys = PlatformPublicKeys::from_json(&test_public_keys_json("k1", &signing_key))?;
        let sidecar = sign_sidecar(&signing_key, "k2", APP_ID, VERSION, DIGEST, APPROVAL_ID);
        let err = verify_artifact_signature(&sidecar, &keys, APP_ID, VERSION, DIGEST);
        assert!(matches!(err, Err(ExecutorError::SignatureInvalid(_))));
        Ok(())
    }

    #[test]
    fn verify_artifact_signature_rejects_a_signature_from_the_wrong_key(
    ) -> Result<(), ExecutorError> {
        // The sidecar's `key_id` IS known to `keys`, but the bytes were
        // actually signed by a different, unregistered key -- proves
        // verification checks the cryptographic signature itself, not
        // merely that the claimed `key_id` happens to exist.
        let real_key = test_signing_key();
        let impostor_key = SigningKey::from_bytes(&[9u8; 32]);
        let keys = PlatformPublicKeys::from_json(&test_public_keys_json("k1", &real_key))?;
        let sidecar = sign_sidecar(&impostor_key, "k1", APP_ID, VERSION, DIGEST, APPROVAL_ID);
        let err = verify_artifact_signature(&sidecar, &keys, APP_ID, VERSION, DIGEST);
        assert!(matches!(err, Err(ExecutorError::SignatureInvalid(_))));
        Ok(())
    }

    #[test]
    fn verify_artifact_signature_rejects_an_unsupported_algorithm() -> Result<(), ExecutorError> {
        let signing_key = test_signing_key();
        let keys = PlatformPublicKeys::from_json(&test_public_keys_json("k1", &signing_key))?;
        let payload = signing_payload(APP_ID, VERSION, DIGEST, APPROVAL_ID);
        let signature = signing_key.sign(&payload);
        let sidecar = serde_json::to_vec(&json!({
            "app_id": APP_ID,
            "version": VERSION,
            "digest": DIGEST,
            "approval_id": APPROVAL_ID,
            "key_id": "k1",
            "algorithm": "ed25519ph",
            "signature": BASE64.encode(signature.to_bytes()),
        }))
        .expect("serializable fixture");
        let err = verify_artifact_signature(&sidecar, &keys, APP_ID, VERSION, DIGEST);
        assert!(matches!(err, Err(ExecutorError::SignatureInvalid(_))));
        Ok(())
    }

    #[test]
    fn verify_artifact_signature_rejects_a_missing_signature_field() {
        let sidecar = serde_json::to_vec(&json!({})).expect("serializable fixture");
        let keys = PlatformPublicKeys::default();
        let err = verify_artifact_signature(&sidecar, &keys, APP_ID, VERSION, DIGEST);
        assert!(matches!(err, Err(ExecutorError::SignatureInvalid(_))));
    }

    #[test]
    fn verify_artifact_signature_rejects_an_empty_sidecar_stub() {
        // `storage_service.upload_bundle_component()`'s pre-signing `{}`
        // stub sidecar -- an unsigned, not-yet-approved artifact must
        // never verify.
        let keys = PlatformPublicKeys::default();
        let err = verify_artifact_signature(b"{}", &keys, APP_ID, VERSION, DIGEST);
        assert!(matches!(err, Err(ExecutorError::SignatureInvalid(_))));
    }

    #[test]
    fn key_rotation_a_new_key_id_verifies_alongside_a_retained_old_one() -> Result<(), ExecutorError>
    {
        let old_key = test_signing_key();
        let new_key = SigningKey::from_bytes(&[3u8; 32]);
        let raw = json!({
            "platform-2026-01": BASE64.encode(old_key.verifying_key().to_bytes()),
            "platform-2026-09": BASE64.encode(new_key.verifying_key().to_bytes()),
        })
        .to_string();
        let keys = PlatformPublicKeys::from_json(&raw)?;

        // An artifact signed under the OLD key (before rotation) still
        // verifies -- rotation adds a key, it does not retroactively
        // invalidate artifacts signed under a still-listed old one.
        let old_sidecar = sign_sidecar(
            &old_key,
            "platform-2026-01",
            APP_ID,
            VERSION,
            DIGEST,
            APPROVAL_ID,
        );
        verify_artifact_signature(&old_sidecar, &keys, APP_ID, VERSION, DIGEST)?;

        // A freshly (re-)signed artifact under the NEW key also verifies.
        let new_sidecar = sign_sidecar(
            &new_key,
            "platform-2026-09",
            APP_ID,
            VERSION,
            DIGEST,
            APPROVAL_ID,
        );
        verify_artifact_signature(&new_sidecar, &keys, APP_ID, VERSION, DIGEST)?;

        Ok(())
    }

    #[test]
    fn key_rotation_removing_the_retired_key_id_fails_closed_for_its_old_signatures(
    ) -> Result<(), ExecutorError> {
        let old_key = test_signing_key();
        let sidecar = sign_sidecar(
            &old_key,
            "platform-2026-01",
            APP_ID,
            VERSION,
            DIGEST,
            APPROVAL_ID,
        );

        // Rotation completed and the old key id was removed from the
        // configured set entirely -- its signatures must now fail closed,
        // never silently accepted.
        let new_key = SigningKey::from_bytes(&[3u8; 32]);
        let keys = PlatformPublicKeys::from_json(
            &json!({ "platform-2026-09": BASE64.encode(new_key.verifying_key().to_bytes()) })
                .to_string(),
        )?;
        let err = verify_artifact_signature(&sidecar, &keys, APP_ID, VERSION, DIGEST);
        assert!(matches!(err, Err(ExecutorError::SignatureInvalid(_))));
        Ok(())
    }

    #[test]
    fn signing_payload_is_stable_and_distinguishes_every_field() {
        let a = signing_payload("app.a", "1", "sha256:x", 1);
        let b = signing_payload("app.a", "1", "sha256:x", 2);
        let c = signing_payload("app.a", "2", "sha256:x", 1);
        let d = signing_payload("app.b", "1", "sha256:x", 1);
        let e = signing_payload("app.a", "1", "sha256:y", 1);
        assert_ne!(a, b);
        assert_ne!(a, c);
        assert_ne!(a, d);
        assert_ne!(a, e);
        assert_eq!(a, signing_payload("app.a", "1", "sha256:x", 1));
    }
}
