//! Per-tenant envelope encryption for identity fields on the ingest->process
//! Valkey stream.
//!
//! Byte-exact wire-format port of the Python predecessor,
//! `core/svc_ingest/identity_crypto.py` (PR #440): AES-256-GCM, a versioned
//! envelope header, length-prefixed AAD -- so a raw platform
//! username/login/display-name/handle never sits in plaintext on the
//! `:process`/ingest-source Valkey stream, which lives outside the PII
//! boundary (`critical-rules.md` PII Tokenization: only hub-api holds raw
//! PII). `tests::golden_vector_matches_python_reference_implementation`
//! below pins this interop with a fixture generated from PR #440's actual
//! `identity_crypto.py` (fixed DEK/nonce/AAD inputs) -- any accidental
//! drift in byte layout, AAD field order, or encoding fails that test.
//!
//! `AAD = tenant_id|stream|field|event_id|dek_version`, length-prefixed per
//! field (never delimiter-joined, so no combination of values collides).
//!
//! **Fail closed, always.** If a tenant DEK cannot be resolved, the caller
//! ([`crate::publish::publish_event`]) must never fall back to writing
//! plaintext -- see [`DekUnavailableError`].
//!
//! Only [`IDENTITY_FIELDS`] (`actor`) is in scope, matching PR #440 exactly
//! -- `payload` fields (message text, ids, timestamps) stay plaintext.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use aes_gcm::aead::{Aead, KeyInit, OsRng};
use aes_gcm::{AeadCore, Aes256Gcm, Nonce};
use base64::Engine;
use hkdf::Hkdf;
use serde::{Deserialize, Serialize};
use sha2::Sha256;
use zeroize::{Zeroize, Zeroizing};

const FORMAT_VERSION: u8 = 1;
const NONCE_LEN: usize = 12; // 96-bit GCM nonce, CSPRNG per NIST SP 800-38D
const DEK_LEN: usize = 32; // AES-256
const TAG_LEN: usize = 16;
const HEADER_LEN: usize = 1 + 4; // format version (1B) + dek_version (4B BE), matches Python's `>BI`

/// Default per-tenant DEK cache TTL (10 minutes), matching the
/// tenant-envelope-encryption design and PR #440's
/// `DEFAULT_DEK_CACHE_TTL_S`.
pub const DEFAULT_DEK_CACHE_TTL: Duration = Duration::from_secs(600);

/// Identity fields recognized on a normalized `penguin_spine::PlatformEvent`
/// -- everything else (payload.text, timestamps, ids) stays plaintext.
/// Matches PR #440's `IDENTITY_FIELDS` exactly.
pub const IDENTITY_FIELDS: &[&str] = &["actor"];

/// A 32-byte AES-256 DEK that zeroizes on drop -- never `Debug`-printed,
/// never logged.
pub type Dek = Zeroizing<[u8; DEK_LEN]>;

/// A malformed, truncated, or unknown-version ciphertext envelope.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum CiphertextFormatError {
    #[error("envelope too short: {actual} < {min} bytes")]
    TooShort { actual: usize, min: usize },
    #[error("unsupported envelope format version: {0}")]
    UnsupportedVersion(u8),
    #[error("malformed ciphertext envelope: {0}")]
    Malformed(String),
}

/// AEAD decryption failed -- wrong tenant, wrong event, wrong field, wrong
/// key, or a corrupted ciphertext. Never distinguishes which, matching
/// AES-GCM's own constant-time tag-mismatch behavior (no oracle).
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
#[error("aead authentication failed")]
pub struct AeadError;

/// A tenant's DEK could not be resolved -- the caller MUST fail closed.
/// Never caught-and-ignored to fall back to plaintext.
#[derive(Debug, thiserror::Error, Clone)]
#[error("tenant DEK unavailable for tenant={tenant_id:?} dek_version={dek_version:?}: {reason}")]
pub struct DekUnavailableError {
    pub tenant_id: String,
    pub dek_version: Option<u32>,
    pub reason: String,
}

/// Length-prefix-encodes one AAD field (`u32` BE length + raw bytes) so no
/// combination of values can collide -- matches Python's `_encode_field`
/// (`struct.Struct(">I")`).
fn encode_field(value: &str) -> Vec<u8> {
    let raw = value.as_bytes();
    let mut out = Vec::with_capacity(4 + raw.len());
    out.extend_from_slice(&(raw.len() as u32).to_be_bytes());
    out.extend_from_slice(raw);
    out
}

/// Builds the canonical AAD binding one encrypted field to its exact
/// context: `tenant_id|stream|field|event_id|dek_version`, length-prefixed.
/// GCM's auth tag covers this, so copying raw ciphertext bytes into another
/// tenant/stream/field/event fails to decrypt rather than silently
/// decrypting under the wrong context.
#[must_use]
pub fn build_aad(
    tenant_id: &str,
    stream: &str,
    field: &str,
    event_id: &str,
    dek_version: u32,
) -> Vec<u8> {
    let mut aad = Vec::new();
    aad.extend(encode_field(tenant_id));
    aad.extend(encode_field(stream));
    aad.extend(encode_field(field));
    aad.extend(encode_field(event_id));
    aad.extend(encode_field(&dek_version.to_string()));
    aad
}

/// Encrypts one identity field value into a versioned envelope ciphertext:
/// `version(1B) || dek_version(4B BE) || nonce(12B) || ciphertext||tag`.
fn envelope_encrypt(plaintext: &str, dek: &Dek, dek_version: u32, aad: &[u8]) -> Vec<u8> {
    envelope_encrypt_with_nonce(
        plaintext,
        dek,
        dek_version,
        aad,
        Aes256Gcm::generate_nonce(&mut OsRng),
    )
}

/// [`envelope_encrypt`] with an explicit nonce -- production code always
/// goes through [`envelope_encrypt`] (a fresh CSPRNG nonce per call); this
/// exists only so [`tests::golden_vector_matches_python_reference_implementation`]
/// can reproduce PR #440's fixed-nonce fixture byte-for-byte.
fn envelope_encrypt_with_nonce(
    plaintext: &str,
    dek: &Dek,
    dek_version: u32,
    aad: &[u8],
    nonce: aes_gcm::Nonce<<Aes256Gcm as AeadCore>::NonceSize>,
) -> Vec<u8> {
    let cipher = Aes256Gcm::new_from_slice(dek.as_ref()).expect("Dek is always exactly 32 bytes");
    let ct = cipher
        .encrypt(
            &nonce,
            aes_gcm::aead::Payload {
                msg: plaintext.as_bytes(),
                aad,
            },
        )
        .expect("AES-256-GCM encryption with a valid key/nonce never fails");
    let mut out = Vec::with_capacity(HEADER_LEN + NONCE_LEN + ct.len());
    out.push(FORMAT_VERSION);
    out.extend_from_slice(&dek_version.to_be_bytes());
    out.extend_from_slice(nonce.as_slice());
    out.extend_from_slice(&ct);
    out
}

/// Decrypts a versioned envelope ciphertext produced by [`envelope_encrypt`].
/// Returns [`AeadError`] on any AAD/key/ciphertext mismatch -- wrong tenant,
/// wrong event, wrong field, or a stale `dek_version` passed the wrong key.
fn envelope_decrypt(envelope: &[u8], dek: &Dek, aad: &[u8]) -> Result<String, IdentityCryptoError> {
    let min_len = HEADER_LEN + NONCE_LEN + TAG_LEN;
    if envelope.len() < min_len {
        return Err(CiphertextFormatError::TooShort {
            actual: envelope.len(),
            min: min_len,
        }
        .into());
    }
    let version = envelope[0];
    if version != FORMAT_VERSION {
        return Err(CiphertextFormatError::UnsupportedVersion(version).into());
    }
    let nonce_start = HEADER_LEN;
    let ct_start = nonce_start + NONCE_LEN;
    let nonce = Nonce::from_slice(&envelope[nonce_start..ct_start]);
    let ct = &envelope[ct_start..];
    let cipher = Aes256Gcm::new_from_slice(dek.as_ref()).expect("Dek is always exactly 32 bytes");
    let plaintext = cipher
        .decrypt(nonce, aes_gcm::aead::Payload { msg: ct, aad })
        .map_err(|_| AeadError)?;
    let s = String::from_utf8(plaintext)
        .map_err(|e| CiphertextFormatError::Malformed(e.to_string()))?;
    Ok(s)
}

/// Reads the `dek_version` out of an envelope header without decrypting it.
fn envelope_dek_version(envelope: &[u8]) -> Result<u32, CiphertextFormatError> {
    if envelope.len() < HEADER_LEN {
        return Err(CiphertextFormatError::TooShort {
            actual: envelope.len(),
            min: HEADER_LEN,
        });
    }
    let version = envelope[0];
    if version != FORMAT_VERSION {
        return Err(CiphertextFormatError::UnsupportedVersion(version));
    }
    Ok(u32::from_be_bytes(
        envelope[1..5]
            .try_into()
            .expect("checked len >= HEADER_LEN"),
    ))
}

/// The JSON-safe `{v, dek_version, nonce, ct}` shape stored on the stream --
/// byte-identical field names/base64 encoding to PR #440's
/// `to_json_envelope`/`from_json_envelope`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct JsonEnvelope {
    pub v: u8,
    pub dek_version: u32,
    pub nonce: String,
    pub ct: String,
}

fn to_json_envelope(envelope: &[u8]) -> JsonEnvelope {
    let version = envelope[0];
    let dek_version = u32::from_be_bytes(envelope[1..5].try_into().expect("checked by caller"));
    let nonce = &envelope[HEADER_LEN..HEADER_LEN + NONCE_LEN];
    let ct = &envelope[HEADER_LEN + NONCE_LEN..];
    JsonEnvelope {
        v: version,
        dek_version,
        nonce: base64::engine::general_purpose::STANDARD.encode(nonce),
        ct: base64::engine::general_purpose::STANDARD.encode(ct),
    }
}

fn from_json_envelope(obj: &JsonEnvelope) -> Result<Vec<u8>, CiphertextFormatError> {
    let nonce = base64::engine::general_purpose::STANDARD
        .decode(&obj.nonce)
        .map_err(|e| CiphertextFormatError::Malformed(e.to_string()))?;
    let ct = base64::engine::general_purpose::STANDARD
        .decode(&obj.ct)
        .map_err(|e| CiphertextFormatError::Malformed(e.to_string()))?;
    let mut out = Vec::with_capacity(HEADER_LEN + nonce.len() + ct.len());
    out.push(obj.v);
    out.extend_from_slice(&obj.dek_version.to_be_bytes());
    out.extend_from_slice(&nonce);
    out.extend_from_slice(&ct);
    Ok(out)
}

/// Errors surfaced by [`encrypt_identity_value`]/[`decrypt_identity_value`].
#[derive(Debug, thiserror::Error)]
pub enum IdentityCryptoError {
    #[error(transparent)]
    Format(#[from] CiphertextFormatError),
    #[error(transparent)]
    Aead(#[from] AeadError),
}

/// Encrypts one identity field value to the JSON envelope shape, AAD-bound
/// to its exact `(tenant, stream, field, event_id, dek_version)` context.
/// The returned [`JsonEnvelope`] is what gets `serde_json::to_string`'d into
/// the `PlatformEvent`'s (e.g.) `actor` string field before it is written
/// onto the stream -- see `crate::publish::publish_event`.
#[must_use]
pub fn encrypt_identity_value(
    plaintext: &str,
    dek: &Dek,
    dek_version: u32,
    tenant_id: &str,
    stream: &str,
    field: &str,
    event_id: &str,
) -> JsonEnvelope {
    let aad = build_aad(tenant_id, stream, field, event_id, dek_version);
    let envelope = envelope_encrypt(plaintext, dek, dek_version, &aad);
    to_json_envelope(&envelope)
}

/// Decrypts one identity field's JSON envelope, re-deriving the exact same
/// AAD. Used by `svc_process`'s decrypt-side port of this module.
pub fn decrypt_identity_value(
    obj: &JsonEnvelope,
    dek: &Dek,
    tenant_id: &str,
    stream: &str,
    field: &str,
    event_id: &str,
) -> Result<String, IdentityCryptoError> {
    let envelope = from_json_envelope(obj)?;
    let dek_version = envelope_dek_version(&envelope)?;
    let aad = build_aad(tenant_id, stream, field, event_id, dek_version);
    envelope_decrypt(&envelope, dek, &aad)
}

// ---------------------------------------------------------------------
// DEK provider: hub-api-issued, TTL-cached, never hardcoded.
// ---------------------------------------------------------------------

/// Resolves a tenant's active (or a specific, for rotation-window reads)
/// DEK. No `dyn`-safe trait object here (async-fn-in-trait) -- callers are
/// generic (`crate::publish::publish_event`'s `D: DekProvider` bound) or go
/// through [`ConfiguredDekProvider`], the enum this crate threads through
/// `tokio::spawn`ed receiver tasks (see `src/lib.rs::build_dek_provider`).
#[allow(async_fn_in_trait)]
pub trait DekProvider: Send + Sync {
    /// Returns `(dek, dek_version)`. Fails closed (`DekUnavailableError`) on
    /// any resolution failure -- never a silent plaintext fallback.
    async fn get_dek(&self, tenant_id: &str) -> Result<(Dek, u32), DekUnavailableError>;
}

struct CacheEntry {
    dek: Dek,
    dek_version: u32,
    expires_at: Instant,
}

/// Wraps another [`DekProvider`] with a per-tenant TTL cache (default 10
/// minutes, matching the design's hub-api-side cache) so a steady-state
/// ingest loop does not call the broker on every event. A cache miss
/// (expired or never-seen) always re-resolves via `inner`, so a rotated DEK
/// is picked up within one TTL window without a restart.
pub struct TtlCachedDekProvider<D: DekProvider> {
    inner: Arc<D>,
    ttl: Duration,
    cache: Arc<Mutex<HashMap<String, CacheEntry>>>,
}

// Manual `Clone` impl (not `#[derive(Clone)]`): a derive would add a
// spurious `D: Clone` bound -- `Arc<D>` is `Clone` regardless of whether
// `D` itself is, which is exactly what lets this provider be shared cheaply
// across every `tokio::spawn`ed receiver task.
impl<D: DekProvider> Clone for TtlCachedDekProvider<D> {
    fn clone(&self) -> Self {
        Self {
            inner: self.inner.clone(),
            ttl: self.ttl,
            cache: self.cache.clone(),
        }
    }
}

impl<D: DekProvider> TtlCachedDekProvider<D> {
    pub fn new(inner: D, ttl: Duration) -> Self {
        Self {
            inner: Arc::new(inner),
            ttl,
            cache: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    /// Evicts every cached entry for `tenant_id` -- call on a
    /// `dek-rotated:{tenant}` signal, once that pub/sub channel exists.
    pub fn invalidate(&self, tenant_id: &str) {
        self.cache
            .lock()
            .expect("cache mutex poisoned")
            .remove(tenant_id);
    }
}

impl<D: DekProvider> DekProvider for TtlCachedDekProvider<D> {
    async fn get_dek(&self, tenant_id: &str) -> Result<(Dek, u32), DekUnavailableError> {
        {
            let cache = self.cache.lock().expect("cache mutex poisoned");
            if let Some(entry) = cache.get(tenant_id) {
                if entry.expires_at > Instant::now() {
                    return Ok((entry.dek.clone(), entry.dek_version));
                }
            }
        }
        let (dek, dek_version) = self.inner.get_dek(tenant_id).await?;
        let mut cache = self.cache.lock().expect("cache mutex poisoned");
        cache.insert(
            tenant_id.to_string(),
            CacheEntry {
                dek: dek.clone(),
                dek_version,
                expires_at: Instant::now() + self.ttl,
            },
        );
        Ok((dek, dek_version))
    }
}

/// Mints the short-lived EdDSA machine JWT this provider authenticates
/// hub-api calls with. Implemented against `core/service_auth`'s interface
/// (PR #438, `feature/eddsa-machine-jwt`) -- **unmerged as of this writing**;
/// this trait exists so `HubApiDekProvider` compiles and is unit-testable
/// today against a fake, and swaps to the real `service_auth` minter as a
/// one-line change once #438 lands.
pub trait MachineJwtProvider: Send + Sync {
    fn mint(&self) -> Result<String, String>;
}

/// Placeholder [`MachineJwtProvider`] used until `core/service_auth`'s real
/// EdDSA machine-JWT minter lands (PR #438, `feature/eddsa-machine-jwt` --
/// **unmerged as of this writing**). Every call fails closed with a clear
/// reason, which propagates as a [`DekUnavailableError`] from
/// [`HubApiDekProvider::get_dek`] -- never a silent unauthenticated call to
/// hub-api. Swap for the real minter as a one-line change once #438 lands.
#[derive(Clone, Copy, Debug, Default)]
pub struct UnimplementedMachineJwtProvider;

impl MachineJwtProvider for UnimplementedMachineJwtProvider {
    fn mint(&self) -> Result<String, String> {
        Err(
            "core/service_auth EdDSA machine JWT minting is not yet available \
             (PR #438 / feature/eddsa-machine-jwt is unmerged)"
                .to_string(),
        )
    }
}

/// Calls hub-api's tenant-DEK broker endpoint
/// (`POST /api/v1/internal/keys/tenant-dek`, per the
/// `feature/tenant-dek-broker` branch -- **server side not yet merged as of
/// this writing**, same documented gap PR #440's own `HubApiDekProvider`
/// docstring calls out for the Python side).
///
/// Fails closed unconditionally: any non-2xx response, network error, or
/// malformed body returns [`DekUnavailableError`] -- never a silent
/// plaintext fallback. Expects hub-api to have already unwrapped the DEK
/// server-side (tenant-envelope-encryption design S5: "hub-api decrypts,
/// everyone else asks").
pub struct HubApiDekProvider<J: MachineJwtProvider> {
    http: reqwest::Client,
    hub_api_url: String,
    jwt_provider: J,
}

impl<J: MachineJwtProvider + Clone> Clone for HubApiDekProvider<J> {
    fn clone(&self) -> Self {
        Self {
            http: self.http.clone(),
            hub_api_url: self.hub_api_url.clone(),
            jwt_provider: self.jwt_provider.clone(),
        }
    }
}

#[derive(Serialize)]
struct TenantDekRequest<'a> {
    tenant_id: &'a str,
}

#[derive(Deserialize)]
struct TenantDekResponse {
    dek: String,
    dek_version: u32,
}

impl<J: MachineJwtProvider> HubApiDekProvider<J> {
    pub fn new(http: reqwest::Client, hub_api_url: impl Into<String>, jwt_provider: J) -> Self {
        Self {
            http,
            hub_api_url: hub_api_url.into(),
            jwt_provider,
        }
    }
}

impl<J: MachineJwtProvider> DekProvider for HubApiDekProvider<J> {
    async fn get_dek(&self, tenant_id: &str) -> Result<(Dek, u32), DekUnavailableError> {
        let unavailable = |reason: String| DekUnavailableError {
            tenant_id: tenant_id.to_string(),
            dek_version: None,
            reason,
        };
        let jwt = self.jwt_provider.mint().map_err(&unavailable)?;
        let url = format!(
            "{}/api/v1/internal/keys/tenant-dek",
            self.hub_api_url.trim_end_matches('/')
        );
        let resp = self
            .http
            .post(&url)
            .bearer_auth(jwt)
            .json(&TenantDekRequest { tenant_id })
            .timeout(Duration::from_secs(5))
            .send()
            .await
            .map_err(|e| unavailable(e.to_string()))?;
        if !resp.status().is_success() {
            return Err(unavailable(format!(
                "hub-api returned HTTP {}",
                resp.status()
            )));
        }
        let body: TenantDekResponse = resp.json().await.map_err(|e| unavailable(e.to_string()))?;
        let mut raw = base64::engine::general_purpose::STANDARD
            .decode(&body.dek)
            .map_err(|e| unavailable(format!("malformed DEK: {e}")))?;
        if raw.len() != DEK_LEN {
            raw.zeroize();
            return Err(unavailable(format!(
                "hub-api returned a {}-byte DEK, expected {DEK_LEN}",
                raw.len()
            )));
        }
        let mut dek_bytes = [0u8; DEK_LEN];
        dek_bytes.copy_from_slice(&raw);
        raw.zeroize();
        Ok((Zeroizing::new(dek_bytes), body.dek_version))
    }
}

/// The environments [`LocalDevDekProvider`] is permitted to run in --
/// checked at construction time so the dev/alpha DEK-derivation fallback
/// can never silently activate in beta/gamma/prod. Stricter than PR #440's
/// Python `LocalDevDekProvider` (which gates only on `INGEST_DEV_KEK` being
/// set, with no environment check of its own) -- this Rust port adds the
/// hard environment gate PR #440 lacks, per this task's explicit
/// requirement.
const LOCAL_DEV_ALLOWED_ENVIRONMENTS: &[&str] = &["alpha", "local"];

/// Dev/alpha-only fallback DEK provider -- HKDF-derives a per-tenant DEK
/// from a local KEK. **Not the production mechanism** -- production is
/// always [`HubApiDekProvider`]. Exists only so local/alpha environments
/// without a live hub-api broker endpoint can still exercise real
/// AES-256-GCM encryption end-to-end, never a no-op/plaintext stand-in.
///
/// **Hard-gated**: construction fails unless `env_name` (read from
/// `WADDLES_ENV`, case-insensitive) is `alpha` or `local` -- refuses
/// unconditionally in beta/gamma/production, regardless of whether a dev
/// KEK happens to be configured there.
pub struct LocalDevDekProvider {
    kek: Zeroizing<[u8; DEK_LEN]>,
}

impl Clone for LocalDevDekProvider {
    fn clone(&self) -> Self {
        Self {
            kek: self.kek.clone(),
        }
    }
}

// Never `Debug`-derived: the default derive would print `kek`'s raw bytes.
impl std::fmt::Debug for LocalDevDekProvider {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("LocalDevDekProvider")
            .field("kek", &"***redacted***")
            .finish()
    }
}

/// Why [`LocalDevDekProvider::new`] refused to construct.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum LocalDevDekProviderError {
    #[error(
        "LocalDevDekProvider is refused outside alpha/local (WADDLES_ENV={0:?}) -- \
         never the production DEK mechanism"
    )]
    EnvironmentNotAllowed(String),
    #[error("{env_var} is not set -- LocalDevDekProvider requires an explicit dev KEK, never a hardcoded default")]
    KekNotSet { env_var: &'static str },
    #[error("{env_var} must resolve to >= {min} bytes of KEK material")]
    KekTooShort { env_var: &'static str, min: usize },
}

impl LocalDevDekProvider {
    /// Resolves the dev KEK from `kek_env_var`, gated by `env_name` (the
    /// deployment environment, e.g. `WADDLES_ENV`) -- refuses construction
    /// outside alpha/local unconditionally.
    pub fn new(env_name: &str, kek_env_var: &str) -> Result<Self, LocalDevDekProviderError> {
        if !LOCAL_DEV_ALLOWED_ENVIRONMENTS.contains(&env_name.to_ascii_lowercase().as_str()) {
            return Err(LocalDevDekProviderError::EnvironmentNotAllowed(
                env_name.to_string(),
            ));
        }
        let raw = std::env::var(kek_env_var).map_err(|_| LocalDevDekProviderError::KekNotSet {
            env_var: Box::leak(kek_env_var.to_string().into_boxed_str()),
        })?;
        if raw.len() < DEK_LEN {
            return Err(LocalDevDekProviderError::KekTooShort {
                env_var: Box::leak(kek_env_var.to_string().into_boxed_str()),
                min: DEK_LEN,
            });
        }
        let mut kek = [0u8; DEK_LEN];
        kek.copy_from_slice(&raw.as_bytes()[..DEK_LEN]);
        Ok(Self {
            kek: Zeroizing::new(kek),
        })
    }
}

impl DekProvider for LocalDevDekProvider {
    async fn get_dek(&self, tenant_id: &str) -> Result<(Dek, u32), DekUnavailableError> {
        let version: u32 = 1;
        let hk = Hkdf::<Sha256>::new(None, self.kek.as_ref());
        let mut dek = [0u8; DEK_LEN];
        let info = format!("waddles-ingest-dev-dek:{tenant_id}:{version}");
        hk.expand(info.as_bytes(), &mut dek)
            .map_err(|e| DekUnavailableError {
                tenant_id: tenant_id.to_string(),
                dek_version: Some(version),
                reason: format!("HKDF expand failed: {e}"),
            })?;
        Ok((Zeroizing::new(dek), version))
    }
}

/// Runtime-selected [`DekProvider`] -- production (`HubApi`) or dev/alpha
/// fallback (`LocalDev`, see [`LocalDevDekProvider`]'s hard environment
/// gate). Not `dyn`-boxed: `async fn` in a trait object needs an extra
/// boxing dependency this crate doesn't otherwise need; an enum dispatch is
/// simpler and just as usable across `tokio::spawn`ed receiver tasks.
#[derive(Clone)]
pub enum ConfiguredDekProvider<J: MachineJwtProvider + Clone> {
    HubApi(TtlCachedDekProvider<HubApiDekProvider<J>>),
    LocalDev(TtlCachedDekProvider<LocalDevDekProvider>),
}

impl<J: MachineJwtProvider + Clone> DekProvider for ConfiguredDekProvider<J> {
    async fn get_dek(&self, tenant_id: &str) -> Result<(Dek, u32), DekUnavailableError> {
        match self {
            Self::HubApi(p) => p.get_dek(tenant_id).await,
            Self::LocalDev(p) => p.get_dek(tenant_id).await,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_dek() -> Dek {
        Zeroizing::new([7u8; DEK_LEN])
    }

    #[test]
    fn round_trips_a_plaintext_value() {
        let dek = test_dek();
        let envelope =
            encrypt_identity_value("someuser", &dek, 1, "acme", "stream-a", "actor", "evt-1");
        let plaintext =
            decrypt_identity_value(&envelope, &dek, "acme", "stream-a", "actor", "evt-1").unwrap();
        assert_eq!(plaintext, "someuser");
    }

    #[test]
    fn wrong_tenant_fails_closed() {
        let dek = test_dek();
        let envelope =
            encrypt_identity_value("someuser", &dek, 1, "acme", "stream-a", "actor", "evt-1");
        let err = decrypt_identity_value(
            &envelope,
            &dek,
            "other-tenant",
            "stream-a",
            "actor",
            "evt-1",
        )
        .unwrap_err();
        assert!(matches!(err, IdentityCryptoError::Aead(_)));
    }

    #[test]
    fn cross_event_replay_fails_closed() {
        let dek = test_dek();
        let envelope =
            encrypt_identity_value("someuser", &dek, 1, "acme", "stream-a", "actor", "evt-1");
        let err = decrypt_identity_value(&envelope, &dek, "acme", "stream-a", "actor", "evt-2")
            .unwrap_err();
        assert!(matches!(err, IdentityCryptoError::Aead(_)));
    }

    #[test]
    fn cross_field_reuse_fails_closed() {
        let dek = test_dek();
        let envelope =
            encrypt_identity_value("someuser", &dek, 1, "acme", "stream-a", "actor", "evt-1");
        let err =
            decrypt_identity_value(&envelope, &dek, "acme", "stream-a", "display_name", "evt-1")
                .unwrap_err();
        assert!(matches!(err, IdentityCryptoError::Aead(_)));
    }

    #[test]
    fn wrong_dek_fails_closed() {
        let dek = test_dek();
        let other_dek = Zeroizing::new([9u8; DEK_LEN]);
        let envelope =
            encrypt_identity_value("someuser", &dek, 1, "acme", "stream-a", "actor", "evt-1");
        let err =
            decrypt_identity_value(&envelope, &other_dek, "acme", "stream-a", "actor", "evt-1")
                .unwrap_err();
        assert!(matches!(err, IdentityCryptoError::Aead(_)));
    }

    #[test]
    fn ciphertext_never_contains_the_plaintext_bytes() {
        let dek = test_dek();
        let envelope = encrypt_identity_value(
            "a-very-distinctive-username",
            &dek,
            1,
            "acme",
            "stream-a",
            "actor",
            "evt-1",
        );
        let ct_bytes = base64::engine::general_purpose::STANDARD
            .decode(&envelope.ct)
            .unwrap();
        assert!(!ct_bytes.windows(4).any(|w| w == b"very"));
    }

    #[test]
    fn rotation_dek_version_round_trips() {
        let dek = test_dek();
        let envelope =
            encrypt_identity_value("someuser", &dek, 7, "acme", "stream-a", "actor", "evt-1");
        assert_eq!(envelope.dek_version, 7);
        let plaintext =
            decrypt_identity_value(&envelope, &dek, "acme", "stream-a", "actor", "evt-1").unwrap();
        assert_eq!(plaintext, "someuser");
    }

    #[test]
    fn truncated_envelope_is_a_format_error_not_a_panic() {
        let obj = JsonEnvelope {
            v: 1,
            dek_version: 1,
            nonce: "AQID".to_string(),
            ct: "".to_string(),
        };
        let dek = test_dek();
        let err = decrypt_identity_value(&obj, &dek, "acme", "s", "actor", "e").unwrap_err();
        assert!(matches!(err, IdentityCryptoError::Format(_)));
    }

    #[tokio::test]
    async fn ttl_cached_provider_caches_within_ttl() {
        struct CountingProvider(std::sync::atomic::AtomicU32);
        impl DekProvider for CountingProvider {
            async fn get_dek(&self, _tenant_id: &str) -> Result<(Dek, u32), DekUnavailableError> {
                self.0.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                Ok((Zeroizing::new([1u8; DEK_LEN]), 1))
            }
        }
        let provider = TtlCachedDekProvider::new(
            CountingProvider(std::sync::atomic::AtomicU32::new(0)),
            Duration::from_secs(60),
        );
        provider.get_dek("acme").await.unwrap();
        provider.get_dek("acme").await.unwrap();
        provider.get_dek("acme").await.unwrap();
        assert_eq!(
            provider.inner.0.load(std::sync::atomic::Ordering::SeqCst),
            1
        );
    }

    #[tokio::test]
    async fn ttl_cached_provider_re_resolves_after_expiry() {
        struct CountingProvider(std::sync::atomic::AtomicU32);
        impl DekProvider for CountingProvider {
            async fn get_dek(&self, _tenant_id: &str) -> Result<(Dek, u32), DekUnavailableError> {
                self.0.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                Ok((Zeroizing::new([1u8; DEK_LEN]), 1))
            }
        }
        let provider = TtlCachedDekProvider::new(
            CountingProvider(std::sync::atomic::AtomicU32::new(0)),
            Duration::from_millis(1),
        );
        provider.get_dek("acme").await.unwrap();
        tokio::time::sleep(Duration::from_millis(20)).await;
        provider.get_dek("acme").await.unwrap();
        assert_eq!(
            provider.inner.0.load(std::sync::atomic::Ordering::SeqCst),
            2
        );
    }

    #[tokio::test]
    async fn ttl_cached_provider_invalidate_forces_re_resolve() {
        struct CountingProvider(std::sync::atomic::AtomicU32);
        impl DekProvider for CountingProvider {
            async fn get_dek(&self, _tenant_id: &str) -> Result<(Dek, u32), DekUnavailableError> {
                self.0.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                Ok((Zeroizing::new([1u8; DEK_LEN]), 1))
            }
        }
        let provider = TtlCachedDekProvider::new(
            CountingProvider(std::sync::atomic::AtomicU32::new(0)),
            Duration::from_secs(60),
        );
        provider.get_dek("acme").await.unwrap();
        provider.invalidate("acme");
        provider.get_dek("acme").await.unwrap();
        assert_eq!(
            provider.inner.0.load(std::sync::atomic::Ordering::SeqCst),
            2
        );
    }

    #[test]
    fn local_dev_provider_refuses_outside_alpha_local() {
        for env in ["beta", "gamma", "production", "prod", ""] {
            let err =
                LocalDevDekProvider::new(env, "WADDLES_TEST_NONEXISTENT_KEK_VAR").unwrap_err();
            assert!(matches!(
                err,
                LocalDevDekProviderError::EnvironmentNotAllowed(_)
            ));
        }
    }

    #[test]
    fn local_dev_provider_allows_alpha_and_local_case_insensitively() {
        // SAFETY: test-only env var mutation, single-threaded within this test.
        std::env::set_var("WADDLES_TEST_DEV_KEK", "x".repeat(DEK_LEN));
        for env in ["alpha", "Alpha", "LOCAL", "local"] {
            LocalDevDekProvider::new(env, "WADDLES_TEST_DEV_KEK").unwrap();
        }
        std::env::remove_var("WADDLES_TEST_DEV_KEK");
    }

    #[test]
    fn local_dev_provider_requires_the_kek_env_var_set() {
        std::env::remove_var("WADDLES_TEST_UNSET_KEK");
        let err = LocalDevDekProvider::new("alpha", "WADDLES_TEST_UNSET_KEK").unwrap_err();
        assert!(matches!(err, LocalDevDekProviderError::KekNotSet { .. }));
    }

    #[test]
    fn local_dev_provider_requires_kek_at_least_32_bytes() {
        std::env::set_var("WADDLES_TEST_SHORT_KEK", "tooshort");
        let err = LocalDevDekProvider::new("alpha", "WADDLES_TEST_SHORT_KEK").unwrap_err();
        assert!(matches!(err, LocalDevDekProviderError::KekTooShort { .. }));
        std::env::remove_var("WADDLES_TEST_SHORT_KEK");
    }

    #[tokio::test]
    async fn local_dev_provider_is_deterministic_per_tenant() {
        std::env::set_var("WADDLES_TEST_DETERMINISTIC_KEK", "k".repeat(DEK_LEN));
        let provider = LocalDevDekProvider::new("alpha", "WADDLES_TEST_DETERMINISTIC_KEK").unwrap();
        let (dek_a1, v1) = provider.get_dek("tenant-a").await.unwrap();
        let (dek_a2, v2) = provider.get_dek("tenant-a").await.unwrap();
        let (dek_b, _) = provider.get_dek("tenant-b").await.unwrap();
        assert_eq!(*dek_a1, *dek_a2);
        assert_eq!(v1, v2);
        assert_ne!(*dek_a1, *dek_b);
        std::env::remove_var("WADDLES_TEST_DETERMINISTIC_KEK");
    }

    /// Fixture shape mirrors `gen_golden.py`'s printed JSON -- generated
    /// once, offline, from PR #440's actual `identity_crypto.py` with
    /// `os.urandom` monkeypatched to a fixed 12-byte nonce so the output is
    /// reproducible. See this module's doc comment.
    #[derive(Deserialize)]
    struct GoldenVector {
        dek_hex: String,
        dek_version: u32,
        tenant_id: String,
        stream: String,
        field: String,
        event_id: String,
        plaintext: String,
        envelope: JsonEnvelope,
    }

    fn golden_vector() -> GoldenVector {
        let raw = include_str!("../tests/fixtures/identity_crypto_golden_vector.json");
        serde_json::from_str(raw).expect("golden vector fixture must be valid JSON")
    }

    /// Cross-language interop: decrypts a ciphertext produced by PR #440's
    /// real Python `identity_crypto.encrypt_identity_value` and asserts the
    /// plaintext matches -- proves the AAD construction, header layout, and
    /// AES-256-GCM parameters are byte-compatible between the two services.
    #[test]
    fn golden_vector_matches_python_reference_implementation() {
        let vector = golden_vector();
        let dek_bytes: Vec<u8> = (0..vector.dek_hex.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&vector.dek_hex[i..i + 2], 16).unwrap())
            .collect();
        let mut dek_arr = [0u8; DEK_LEN];
        dek_arr.copy_from_slice(&dek_bytes);
        let dek = Zeroizing::new(dek_arr);

        let plaintext = decrypt_identity_value(
            &vector.envelope,
            &dek,
            &vector.tenant_id,
            &vector.stream,
            &vector.field,
            &vector.event_id,
        )
        .expect("Rust decrypt must accept a Python-produced envelope");
        assert_eq!(plaintext, vector.plaintext);
        assert_eq!(vector.envelope.dek_version, vector.dek_version);
    }

    /// The stronger direction: re-encrypts the same plaintext with the same
    /// fixed nonce/AAD inputs and asserts byte-for-byte ciphertext equality
    /// with Python's output -- proves the two implementations don't merely
    /// happen to decrypt each other's output but produce identical bytes
    /// given identical inputs (AAD field order/length-prefixing, GCM tag
    /// placement, base64 alphabet all match).
    #[test]
    fn golden_vector_rust_encryption_is_byte_identical_to_python() {
        let vector = golden_vector();
        let dek_bytes: Vec<u8> = (0..vector.dek_hex.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&vector.dek_hex[i..i + 2], 16).unwrap())
            .collect();
        let mut dek_arr = [0u8; DEK_LEN];
        dek_arr.copy_from_slice(&dek_bytes);
        let dek = Zeroizing::new(dek_arr);

        let nonce_bytes = base64::engine::general_purpose::STANDARD
            .decode(&vector.envelope.nonce)
            .unwrap();
        let aad = build_aad(
            &vector.tenant_id,
            &vector.stream,
            &vector.field,
            &vector.event_id,
            vector.dek_version,
        );
        let envelope = envelope_encrypt_with_nonce(
            &vector.plaintext,
            &dek,
            vector.dek_version,
            &aad,
            *Nonce::from_slice(&nonce_bytes),
        );
        let json = to_json_envelope(&envelope);
        assert_eq!(json, vector.envelope, "Rust-produced envelope must be byte-identical to Python's, given the same nonce/AAD inputs");
    }
}
