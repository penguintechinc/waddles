//! The sealed-value envelope and the primitives behind it.
//!
//! Layout: `magic(4) | alg(1) | dek_version(4 BE) | nonce(12) | ciphertext||tag`.
//! The magic starts with `0xFE`, which can never begin valid UTF-8, so a
//! legacy plaintext *text* value is never mistaken for an envelope. Keys are
//! AES-256-GCM subkeys HKDF-derived from the tenant DEK (never the DEK
//! itself), domain-separated per purpose.

use aes_gcm::aead::{Aead, KeyInit, OsRng, Payload};
use aes_gcm::{AeadCore, Aes256Gcm, Nonce};
use hkdf::Hkdf;
use hmac::{Hmac, Mac};
use sha2::Sha256;
use zeroize::Zeroizing;

use crate::error::CryptoError;

/// Envelope magic. `0xFE` is an invalid UTF-8 lead byte.
pub const MAGIC: [u8; 4] = [0xFE, b'W', b'B', b'D'];
/// Algorithm id 1: AES-256-GCM under the HKDF data subkey.
pub const ALG_AES256GCM_V1: u8 = 1;
/// AES-256 key length.
pub const KEY_LEN: usize = 32;
const NONCE_LEN: usize = 12;
const TAG_LEN: usize = 16;
/// Bytes before the nonce: magic + alg + dek_version.
pub const HEADER_LEN: usize = 4 + 1 + 4;
/// Fixed overhead an envelope adds to its plaintext.
pub const OVERHEAD: usize = HEADER_LEN + NONCE_LEN + TAG_LEN;

/// HKDF salt, identical to `hub_api/services/envelope/crypto.py::_HKDF_SALT`.
const HKDF_SALT: &[u8] = b"waddles-envelope-v1";

/// A 32-byte key that zeroizes on drop. `Zeroizing`'s `Debug` prints its
/// contents, so no type holding one may derive `Debug`.
pub type Key = Zeroizing<[u8; KEY_LEN]>;

/// Purpose a subkey is derived for (HKDF `info` domain separation).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Purpose {
    /// AES-256-GCM data subkey (`bundle-data-v1`).
    Data,
    /// HMAC-SHA256 blind-index subkey (`bundle-bidx-v1`).
    BlindIndex,
}

impl Purpose {
    fn label(self) -> &'static str {
        match self {
            Self::Data => "waddles-bundle-data-v1",
            Self::BlindIndex => "waddles-bundle-bidx-v1",
        }
    }
}

/// Derives the per-tenant, per-purpose subkey from a tenant DEK. Mirrors
/// `derive_data_subkey` in the Python envelope module (same salt, same
/// `|tenant:{id}` info shape) with a bundle-specific purpose label.
pub fn derive_subkey(
    dek: &[u8; KEY_LEN],
    tenant_id: &str,
    purpose: Purpose,
) -> Result<Key, CryptoError> {
    let hk = Hkdf::<Sha256>::new(Some(HKDF_SALT), dek);
    let info = format!("{}|tenant:{}", purpose.label(), tenant_id);
    let mut out = [0u8; KEY_LEN];
    hk.expand(info.as_bytes(), &mut out)
        .map_err(|e| CryptoError::Format(format!("HKDF expand failed: {e}")))?;
    Ok(Zeroizing::new(out))
}

fn encode_field(out: &mut Vec<u8>, value: &str) {
    let raw = value.as_bytes();
    out.extend_from_slice(&(raw.len() as u32).to_be_bytes());
    out.extend_from_slice(raw);
}

/// Builds the canonical length-prefixed AAD from ordered `parts` plus the
/// `dek_version` (always last). Length prefixes (never delimiters) mean no
/// two part lists can collide.
#[must_use]
pub fn build_aad(parts: &[&str], dek_version: u32) -> Vec<u8> {
    let mut aad = Vec::new();
    for p in parts {
        encode_field(&mut aad, p);
    }
    encode_field(&mut aad, &dek_version.to_string());
    aad
}

/// True when `bytes` begins with the envelope magic.
#[must_use]
pub fn is_envelope(bytes: &[u8]) -> bool {
    bytes.len() >= MAGIC.len() && bytes[..MAGIC.len()] == MAGIC
}

/// Reads the `dek_version` from an envelope header without decrypting.
pub fn envelope_dek_version(envelope: &[u8]) -> Result<u32, CryptoError> {
    if !is_envelope(envelope) {
        return Err(CryptoError::NotEnvelope);
    }
    if envelope.len() < OVERHEAD {
        return Err(CryptoError::Format(format!(
            "envelope too short: {} < {OVERHEAD}",
            envelope.len()
        )));
    }
    if envelope[4] != ALG_AES256GCM_V1 {
        return Err(CryptoError::Format(format!(
            "unsupported algorithm id {}",
            envelope[4]
        )));
    }
    let mut v = [0u8; 4];
    v.copy_from_slice(&envelope[5..9]);
    Ok(u32::from_be_bytes(v))
}

/// Seals `plaintext` under `subkey` with a fresh CSPRNG nonce.
pub fn seal_with_subkey(
    subkey: &Key,
    dek_version: u32,
    aad: &[u8],
    plaintext: &[u8],
) -> Result<Vec<u8>, CryptoError> {
    seal_with_nonce(
        subkey,
        dek_version,
        aad,
        plaintext,
        Aes256Gcm::generate_nonce(&mut OsRng),
    )
}

/// [`seal_with_subkey`] with an explicit nonce. Production never calls this
/// directly; it exists so tests can reproduce the Python golden vector.
pub fn seal_with_nonce(
    subkey: &Key,
    dek_version: u32,
    aad: &[u8],
    plaintext: &[u8],
    nonce: Nonce<<Aes256Gcm as AeadCore>::NonceSize>,
) -> Result<Vec<u8>, CryptoError> {
    let cipher = Aes256Gcm::new_from_slice(subkey.as_ref())
        .map_err(|_| CryptoError::Format("subkey is not 32 bytes".to_string()))?;
    let ct = cipher
        .encrypt(
            &nonce,
            Payload {
                msg: plaintext,
                aad,
            },
        )
        .map_err(|_| CryptoError::Integrity)?;
    let mut out = Vec::with_capacity(HEADER_LEN + NONCE_LEN + ct.len());
    out.extend_from_slice(&MAGIC);
    out.push(ALG_AES256GCM_V1);
    out.extend_from_slice(&dek_version.to_be_bytes());
    out.extend_from_slice(nonce.as_slice());
    out.extend_from_slice(&ct);
    Ok(out)
}

/// Opens an envelope under `subkey` and `aad`.
pub fn open_with_subkey(subkey: &Key, envelope: &[u8], aad: &[u8]) -> Result<Vec<u8>, CryptoError> {
    envelope_dek_version(envelope)?;
    let nonce = Nonce::from_slice(&envelope[HEADER_LEN..HEADER_LEN + NONCE_LEN]);
    let ct = &envelope[HEADER_LEN + NONCE_LEN..];
    let cipher = Aes256Gcm::new_from_slice(subkey.as_ref())
        .map_err(|_| CryptoError::Format("subkey is not 32 bytes".to_string()))?;
    cipher
        .decrypt(nonce, Payload { msg: ct, aad })
        .map_err(|_| CryptoError::Integrity)
}

/// HMAC-SHA256 blind index of `value` bound to `context` parts.
pub fn blind_index_with_subkey(
    subkey: &Key,
    context: &[&str],
    value: &[u8],
) -> Result<[u8; 32], CryptoError> {
    let mut mac = <Hmac<Sha256> as Mac>::new_from_slice(subkey.as_ref())
        .map_err(|_| CryptoError::Format("blind-index subkey rejected".to_string()))?;
    let mut ctx = Vec::new();
    for p in context {
        encode_field(&mut ctx, p);
    }
    mac.update(&ctx);
    mac.update(value);
    let mut out = [0u8; 32];
    out.copy_from_slice(&mac.finalize().into_bytes());
    Ok(out)
}
