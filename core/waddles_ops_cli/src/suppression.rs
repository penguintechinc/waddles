//! Per-tenant keyed one-way suppression codes (port of `hub_api/services/suppression_keys.py`
//! plus the platform-KEK adapter bits it uses from `services/envelope/`).
//!
//! ```text
//! tenant_key = HKDF-SHA256(ikm = K_master, salt = none, info = "waddles-suppression/v1/tenant:<id>")
//! code       = HMAC-SHA256(tenant_key, "waddles-suppression/v1" 0x00 platform 0x00 platform_user_id)
//! key_ver    = hex(SHA256(K_master)[..8])
//! ```
//!
//! `tests/suppression_parity.rs` replays vectors from the Python implementation. Key material
//! never appears in `Debug`, errors or logs.

use hkdf::Hkdf;
use hmac::{Hmac, Mac};
use sha2::{Digest, Sha256};

use crate::pystr::py_strip;

/// Env var: current platform KEK (64 hex chars).
pub const ENV_CURRENT: &str = "TENANT_KEK_HEX";
/// Env var: previous platform KEK during a rotation window.
pub const ENV_PREVIOUS: &str = "TENANT_KEK_HEX_PREVIOUS";

const INFO_PREFIX: &[u8] = b"waddles-suppression/v1/tenant:";
const MESSAGE_PREFIX: &[u8] = b"waddles-suppression/v1\x00";
const MAX_PLATFORM_USER_ID: usize = 255;

/// The master key is missing or malformed. Never carries key material.
#[derive(Debug, thiserror::Error)]
#[error("{0}")]
pub struct SuppressionKeyError(pub String);

/// A tenant id / platform / platform user id failed validation (never echoes the value).
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
#[error("{0}")]
pub struct SuppressionInputError(pub &'static str);

/// One tenant's HMAC key plus the non-reversible id of the master key behind it.
pub struct TenantKey {
    pub key_ver: String,
    key: [u8; 32],
}

impl std::fmt::Debug for TenantKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("TenantKey")
            .field("key_ver", &self.key_ver)
            .field("key", &"<redacted>")
            .finish()
    }
}

impl TenantKey {
    /// Builds a key from raw bytes (tests and vector checks only).
    #[doc(hidden)]
    pub fn from_parts(key_ver: &str, key: [u8; 32]) -> Self {
        Self {
            key_ver: key_ver.to_string(),
            key,
        }
    }
}

/// A stored code: producing key version and 64 lowercase hex chars of HMAC.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SuppressionCode {
    pub key_ver: String,
    pub hmac: String,
}

/// Lowercase hex encoding.
pub fn hex_encode(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// Strict 64-hex-char (256-bit) decode; the error never echoes the value.
fn parse_kek(name: &str, raw: &str) -> Result<[u8; 32], SuppressionKeyError> {
    if raw.len() != 64 {
        return Err(SuppressionKeyError(format!(
            "{name} must be 64 hex characters (256-bit key)"
        )));
    }
    let mut out = [0u8; 32];
    for (i, pair) in raw.as_bytes().chunks(2).enumerate() {
        let hi = (pair[0] as char).to_digit(16);
        let lo = (pair[1] as char).to_digit(16);
        match (hi, lo) {
            (Some(h), Some(l)) => out[i] = ((h << 4) | l) as u8,
            _ => return Err(SuppressionKeyError(format!("{name} must be hex-encoded"))),
        }
    }
    Ok(out)
}

fn kek_id(kek: &[u8; 32]) -> String {
    hex_encode(&Sha256::digest(kek)[..8])
}

/// The platform KEK(s): current (wraps/writes) and optional previous (lookups only).
pub struct PlatformKek {
    keks: Vec<(String, [u8; 32])>,
}

impl PlatformKek {
    /// Builds from `TENANT_KEK_HEX` (+ optional `TENANT_KEK_HEX_PREVIOUS`) values.
    ///
    /// Fails closed: a missing/short/non-hex current key is an error, there is no default.
    pub fn from_values(
        current: Option<&str>,
        previous: Option<&str>,
    ) -> Result<Self, SuppressionKeyError> {
        let raw = py_strip(current.unwrap_or(""));
        if raw.is_empty() {
            return Err(SuppressionKeyError(format!(
                "{ENV_CURRENT} is not set -- the platform KEK is required"
            )));
        }
        let prev_raw = py_strip(previous.unwrap_or(""));
        let previous = if prev_raw.is_empty() {
            None
        } else {
            Some(parse_kek(ENV_PREVIOUS, prev_raw)?)
        };
        let current = parse_kek(ENV_CURRENT, raw)?;
        let mut keks = vec![(kek_id(&current), current)];
        if let Some(p) = previous {
            let id = kek_id(&p);
            if keks.iter().all(|(i, _)| *i != id) {
                keks.push((id, p));
            }
        }
        Ok(Self { keks })
    }

    /// Reads the KEK(s) from the process environment.
    pub fn from_env() -> Result<Self, SuppressionKeyError> {
        let cur = std::env::var(ENV_CURRENT).ok();
        let prev = std::env::var(ENV_PREVIOUS).ok();
        Self::from_values(cur.as_deref(), prev.as_deref())
    }

    /// Ids of every loaded KEK, current first.
    pub fn kek_ids(&self) -> Vec<String> {
        self.keks.iter().map(|(i, _)| i.clone()).collect()
    }

    /// The tenant's keys under every loaded KEK, current first.
    pub fn tenant_keys(&self, tenant_id: i64) -> Result<Vec<TenantKey>, SuppressionInputError> {
        let tenant_id = validate_tenant_id(tenant_id)?;
        let mut info = INFO_PREFIX.to_vec();
        info.extend_from_slice(tenant_id.to_string().as_bytes());
        let mut out = Vec::with_capacity(self.keks.len());
        for (id, kek) in &self.keks {
            let hk = Hkdf::<Sha256>::new(None, kek);
            let mut key = [0u8; 32];
            // 32 bytes is far below the HKDF-SHA256 255*32 limit; failure is impossible, but
            // is mapped to an error rather than unwrapped.
            hk.expand(&info, &mut key)
                .map_err(|_| SuppressionInputError("hkdf expand failed"))?;
            out.push(TenantKey {
                key_ver: id.clone(),
                key,
            });
        }
        Ok(out)
    }
}

/// Returns `tenant_id` if positive, else an input error.
pub fn validate_tenant_id(tenant_id: i64) -> Result<i64, SuppressionInputError> {
    if tenant_id < 1 {
        return Err(SuppressionInputError(
            "tenant_id must be a positive integer",
        ));
    }
    Ok(tenant_id)
}

/// Canonicalises a platform identity: platform stripped+lower-cased, id stripped, case kept.
pub fn normalize_identity(
    platform: &str,
    platform_user_id: &str,
) -> Result<(String, String), SuppressionInputError> {
    let plat = py_strip(platform).to_lowercase();
    let puid = py_strip(platform_user_id);
    let mut chars = plat.chars();
    let platform_ok = plat.len() <= 50
        && matches!(chars.next(), Some('a'..='z'))
        && chars.all(|c| matches!(c, 'a'..='z' | '0'..='9' | '_'));
    if !platform_ok {
        return Err(SuppressionInputError(
            "platform is not a valid platform token",
        ));
    }
    if puid.is_empty()
        || puid.chars().count() > MAX_PLATFORM_USER_ID
        || puid.chars().any(|c| (c as u32) < 0x20 || c as u32 == 0x7f)
    {
        return Err(SuppressionInputError(
            "platform_user_id is empty, too long or has control chars",
        ));
    }
    Ok((plat, puid.to_string()))
}

/// Keyed one-way code for one identity under one tenant key (deterministic).
pub fn compute_code(
    tenant_key: &TenantKey,
    platform: &str,
    platform_user_id: &str,
) -> Result<SuppressionCode, SuppressionInputError> {
    let (plat, puid) = normalize_identity(platform, platform_user_id)?;
    let mut msg = MESSAGE_PREFIX.to_vec();
    msg.extend_from_slice(plat.as_bytes());
    msg.push(0);
    msg.extend_from_slice(puid.as_bytes());
    let mut mac = <Hmac<Sha256> as Mac>::new_from_slice(&tenant_key.key)
        .map_err(|_| SuppressionInputError("invalid hmac key length"))?;
    mac.update(&msg);
    Ok(SuppressionCode {
        key_ver: tenant_key.key_ver.clone(),
        hmac: hex_encode(&mac.finalize().into_bytes()),
    })
}

/// Every code a stored row for this identity could carry (current, then previous key).
pub fn lookup_codes(
    kek: &PlatformKek,
    tenant_id: i64,
    platform: &str,
    platform_user_id: &str,
) -> Result<Vec<SuppressionCode>, SuppressionInputError> {
    kek.tenant_keys(tenant_id)?
        .iter()
        .map(|k| compute_code(k, platform, platform_user_id))
        .collect()
}
