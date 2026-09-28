//! Decrypt-side port of `core/svc_ingest/src/identity_crypto.rs` (itself a
//! byte-exact Rust port of the Python predecessor, `core/svc_ingest/
//! identity_crypto.py`, PR #440). svc-ingest AES-256-GCM envelope-encrypts
//! identity fields (`actor`) before writing an event onto the ingest-source
//! Valkey stream (PII boundary: only hub-api holds raw PII); svc-process
//! reads that stream and must decrypt `actor` back to plaintext before any
//! stage that needs it -- most notably `feature/ingest-pii-tokenization`'s
//! (PR #429) `pii_tokenize` stage, which maps a raw username to its
//! tokenized UUID and therefore needs the real value, not the ciphertext
//! envelope.
//!
//! **Integration point (documented, not yet wired):** PR #429 was
//! unmerged as of this writing. [`decrypt_actor_field`] is this module's
//! pre-stage entry point -- call it immediately before `pii_tokenize`
//! (or as `pii_tokenize`'s own first step) on every consumed
//! `penguin_spine::PlatformEvent`, using the *same*
//! `(tenant, stream, event_id)` triple svc-ingest encrypted under (the
//! `StageEnvelope`'s own `tenant`/`event_id` fields plus the granted
//! ingest-source stream key svc-process is draining -- see
//! `crate::source_supervisor`). Once #429 lands, its stage should call
//! [`decrypt_actor_field`] as its first step rather than duplicating this
//! logic.
//!
//! Wire format is identical to the encrypt side -- see that module's doc
//! comment for the AAD/header/JSON-envelope layout and
//! `tests::golden_vector_matches_python_reference_implementation` for the
//! cross-language interop proof (shared fixture,
//! `tests/fixtures/identity_crypto_golden_vector.json`).

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use aes_gcm::aead::Aead;
use aes_gcm::{Aes256Gcm, KeyInit, Nonce};
use base64::Engine;
use serde::{Deserialize, Serialize};
use zeroize::Zeroizing;

const FORMAT_VERSION: u8 = 1;
const NONCE_LEN: usize = 12;
const DEK_LEN: usize = 32;
const TAG_LEN: usize = 16;
const HEADER_LEN: usize = 1 + 4;

/// Default per-tenant DEK cache TTL (10 minutes) -- matches
/// `core/svc_ingest/src/identity_crypto.rs::DEFAULT_DEK_CACHE_TTL`.
pub const DEFAULT_DEK_CACHE_TTL: Duration = Duration::from_secs(600);

/// A 32-byte AES-256 DEK that zeroizes on drop.
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
/// key, or a corrupted ciphertext.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
#[error("aead authentication failed")]
pub struct AeadError;

/// A tenant's DEK could not be resolved -- callers MUST fail closed (never
/// pass the still-encrypted envelope string through as if it were
/// plaintext).
#[derive(Debug, thiserror::Error, Clone)]
#[error("tenant DEK unavailable for tenant={tenant_id:?} dek_version={dek_version:?}: {reason}")]
pub struct DekUnavailableError {
    pub tenant_id: String,
    pub dek_version: Option<u32>,
    pub reason: String,
}

/// Errors [`decrypt_identity_value`]/[`decrypt_actor_field`] can return.
#[derive(Debug, thiserror::Error)]
pub enum IdentityCryptoError {
    #[error(transparent)]
    Format(#[from] CiphertextFormatError),
    #[error(transparent)]
    Aead(#[from] AeadError),
    #[error(transparent)]
    DekUnavailable(#[from] DekUnavailableError),
    /// The stored `actor` string was not a JSON envelope object at all --
    /// e.g. a pre-encryption-rollout event, or a bug upstream. Callers
    /// should treat this the same as a format error: fail closed, never
    /// guess.
    #[error("actor field is not a valid ciphertext envelope: {0}")]
    NotAnEnvelope(String),
}

fn encode_field(value: &str) -> Vec<u8> {
    let raw = value.as_bytes();
    let mut out = Vec::with_capacity(4 + raw.len());
    out.extend_from_slice(&(raw.len() as u32).to_be_bytes());
    out.extend_from_slice(raw);
    out
}

/// Builds the canonical AAD -- byte-identical to the encrypt-side
/// `identity_crypto::build_aad` (`core/svc_ingest`) and to PR #440's
/// Python `build_aad`.
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

/// The JSON-safe `{v, dek_version, nonce, ct}` shape -- identical to the
/// encrypt side.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct JsonEnvelope {
    pub v: u8,
    pub dek_version: u32,
    pub nonce: String,
    pub ct: String,
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

/// Decrypts one identity field's JSON envelope, re-deriving the exact same
/// AAD the encrypt side bound it under.
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

/// **Pre-stage entry point** (see this module's doc comment): decrypts a
/// consumed `PlatformEvent`'s `actor` field in place, using `dek_provider`
/// to resolve `tenant_id`'s DEK. A `None` actor is a no-op (nothing to
/// decrypt). Fails closed on any error -- callers must never fall through
/// to treating the still-encrypted string as a plaintext username.
pub async fn decrypt_actor_field<D: DekProvider>(
    event: &mut penguin_spine::PlatformEvent,
    dek_provider: &D,
    tenant_id: &str,
    stream: &str,
    event_id: &str,
) -> Result<(), IdentityCryptoError> {
    let Some(raw) = event.actor.take() else {
        return Ok(());
    };
    let envelope: JsonEnvelope = serde_json::from_str(&raw)
        .map_err(|e| IdentityCryptoError::NotAnEnvelope(e.to_string()))?;
    let (dek, _dek_version) = dek_provider.get_dek(tenant_id).await?;
    let plaintext = decrypt_identity_value(&envelope, &dek, tenant_id, stream, "actor", event_id)?;
    event.actor = Some(plaintext);
    Ok(())
}

// ---------------------------------------------------------------------
// DEK provider -- decrypt-side mirror of `core/svc_ingest`'s provider.
// Duplicated rather than shared: svc_ingest and svc_process are separate
// binary crates with no shared `penguin-libs` crate for this yet (same
// posture PR #440's own module doc calls out for the Python side --
// `penguin_security.crypto.envelope` is unmerged/unpublished).
// ---------------------------------------------------------------------

/// Resolves a tenant's DEK. Fails closed (`DekUnavailableError`) on any
/// resolution failure -- never a silent plaintext fallback.
#[allow(async_fn_in_trait)]
pub trait DekProvider: Send + Sync {
    async fn get_dek(&self, tenant_id: &str) -> Result<(Dek, u32), DekUnavailableError>;
}

struct CacheEntry {
    dek: Dek,
    dek_version: u32,
    expires_at: Instant,
}

/// Per-tenant TTL cache wrapping another [`DekProvider`] -- identical
/// behavior to `core/svc_ingest`'s `TtlCachedDekProvider`.
pub struct TtlCachedDekProvider<D: DekProvider> {
    inner: Arc<D>,
    ttl: Duration,
    cache: Arc<Mutex<HashMap<String, CacheEntry>>>,
}

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
/// hub-api calls with -- see `core/svc_ingest`'s identical trait doc
/// comment (PR #438, `feature/eddsa-machine-jwt`, unmerged as of this
/// writing).
pub trait MachineJwtProvider: Send + Sync {
    fn mint(&self) -> Result<String, String>;
}

/// Placeholder [`MachineJwtProvider`] -- see `core/svc_ingest`'s identical
/// type for the full rationale.
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
/// (`POST /api/v1/internal/keys/tenant-dek`, `feature/tenant-dek-broker` --
/// **server side not yet merged as of this writing**). See
/// `core/svc_ingest`'s identical type for the full doc.
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
        let raw = base64::engine::general_purpose::STANDARD
            .decode(&body.dek)
            .map_err(|e| unavailable(format!("malformed DEK: {e}")))?;
        if raw.len() != DEK_LEN {
            return Err(unavailable(format!(
                "hub-api returned a {}-byte DEK, expected {DEK_LEN}",
                raw.len()
            )));
        }
        let mut dek_bytes = [0u8; DEK_LEN];
        dek_bytes.copy_from_slice(&raw);
        Ok((Zeroizing::new(dek_bytes), body.dek_version))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_dek() -> Dek {
        Zeroizing::new([7u8; DEK_LEN])
    }

    /// Uses `core/svc_ingest`'s `envelope_encrypt`/`to_json_envelope`
    /// indirectly via the shared golden vector fixture instead of
    /// re-implementing encryption here -- this crate is decrypt-only by
    /// design (svc-process never re-encrypts an identity field).
    fn encrypt_via_fixture_format(
        plaintext: &str,
        dek: &Dek,
        aad: &[u8],
        nonce: [u8; NONCE_LEN],
    ) -> Vec<u8> {
        let cipher = Aes256Gcm::new_from_slice(dek.as_ref()).unwrap();
        let n = Nonce::from_slice(&nonce);
        let ct = cipher
            .encrypt(
                n,
                aes_gcm::aead::Payload {
                    msg: plaintext.as_bytes(),
                    aad,
                },
            )
            .unwrap();
        let mut out = vec![FORMAT_VERSION];
        out.extend_from_slice(&1u32.to_be_bytes());
        out.extend_from_slice(&nonce);
        out.extend_from_slice(&ct);
        out
    }

    fn to_json(envelope: &[u8]) -> JsonEnvelope {
        let dek_version = u32::from_be_bytes(envelope[1..5].try_into().unwrap());
        let nonce = &envelope[HEADER_LEN..HEADER_LEN + NONCE_LEN];
        let ct = &envelope[HEADER_LEN + NONCE_LEN..];
        JsonEnvelope {
            v: envelope[0],
            dek_version,
            nonce: base64::engine::general_purpose::STANDARD.encode(nonce),
            ct: base64::engine::general_purpose::STANDARD.encode(ct),
        }
    }

    #[test]
    fn round_trips_a_plaintext_value() {
        let dek = test_dek();
        let aad = build_aad("acme", "stream-a", "actor", "evt-1", 1);
        let raw = encrypt_via_fixture_format("someuser", &dek, &aad, [1u8; NONCE_LEN]);
        let envelope = to_json(&raw);
        let plaintext =
            decrypt_identity_value(&envelope, &dek, "acme", "stream-a", "actor", "evt-1").unwrap();
        assert_eq!(plaintext, "someuser");
    }

    #[test]
    fn wrong_tenant_fails_closed() {
        let dek = test_dek();
        let aad = build_aad("acme", "stream-a", "actor", "evt-1", 1);
        let raw = encrypt_via_fixture_format("someuser", &dek, &aad, [1u8; NONCE_LEN]);
        let envelope = to_json(&raw);
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

    #[tokio::test]
    async fn decrypt_actor_field_is_a_noop_for_a_none_actor() {
        struct AlwaysFailProvider;
        impl DekProvider for AlwaysFailProvider {
            async fn get_dek(&self, tenant_id: &str) -> Result<(Dek, u32), DekUnavailableError> {
                Err(DekUnavailableError {
                    tenant_id: tenant_id.to_string(),
                    dek_version: None,
                    reason: "should never be called".to_string(),
                })
            }
        }
        let mut event = penguin_spine::PlatformEvent {
            platform: "twitch".to_string(),
            event_type: "chat.message".to_string(),
            actor: None,
            payload: serde_json::Map::new(),
            occurred_at: "2026-09-14T12:00:00.000Z".to_string(),
            source: None,
        };
        decrypt_actor_field(&mut event, &AlwaysFailProvider, "acme", "stream-a", "evt-1")
            .await
            .unwrap();
        assert_eq!(event.actor, None);
    }

    #[tokio::test]
    async fn decrypt_actor_field_decrypts_in_place() {
        struct FixedDekProvider;
        impl DekProvider for FixedDekProvider {
            async fn get_dek(&self, _tenant_id: &str) -> Result<(Dek, u32), DekUnavailableError> {
                Ok((test_dek(), 1))
            }
        }
        let aad = build_aad("acme", "stream-a", "actor", "evt-1", 1);
        let raw = encrypt_via_fixture_format("someuser", &test_dek(), &aad, [3u8; NONCE_LEN]);
        let envelope = serde_json::to_string(&to_json(&raw)).unwrap();
        let mut event = penguin_spine::PlatformEvent {
            platform: "twitch".to_string(),
            event_type: "chat.message".to_string(),
            actor: Some(envelope),
            payload: serde_json::Map::new(),
            occurred_at: "2026-09-14T12:00:00.000Z".to_string(),
            source: None,
        };
        decrypt_actor_field(&mut event, &FixedDekProvider, "acme", "stream-a", "evt-1")
            .await
            .unwrap();
        assert_eq!(event.actor.as_deref(), Some("someuser"));
    }

    #[tokio::test]
    async fn decrypt_actor_field_fails_closed_on_dek_unavailable() {
        struct FailingProvider;
        impl DekProvider for FailingProvider {
            async fn get_dek(&self, tenant_id: &str) -> Result<(Dek, u32), DekUnavailableError> {
                Err(DekUnavailableError {
                    tenant_id: tenant_id.to_string(),
                    dek_version: None,
                    reason: "no broker configured in this test".to_string(),
                })
            }
        }
        let envelope = serde_json::to_string(&JsonEnvelope {
            v: 1,
            dek_version: 1,
            nonce: "AQIDBAUGBwgJCgsM".to_string(),
            ct: "doU3sJnnlfQegO6py0BwDqls53KZH6GA".to_string(),
        })
        .unwrap();
        let mut event = penguin_spine::PlatformEvent {
            platform: "twitch".to_string(),
            event_type: "chat.message".to_string(),
            actor: Some(envelope),
            payload: serde_json::Map::new(),
            occurred_at: "2026-09-14T12:00:00.000Z".to_string(),
            source: None,
        };
        let err = decrypt_actor_field(&mut event, &FailingProvider, "acme", "stream-a", "evt-1")
            .await
            .unwrap_err();
        assert!(matches!(err, IdentityCryptoError::DekUnavailable(_)));
    }

    #[test]
    fn ttl_cache_type_is_send_sync() {
        fn assert_send_sync<T: Send + Sync>() {}
        struct Noop;
        impl DekProvider for Noop {
            async fn get_dek(&self, tenant_id: &str) -> Result<(Dek, u32), DekUnavailableError> {
                Err(DekUnavailableError {
                    tenant_id: tenant_id.to_string(),
                    dek_version: None,
                    reason: String::new(),
                })
            }
        }
        assert_send_sync::<TtlCachedDekProvider<Noop>>();
    }

    /// Fixture shape mirrors `core/svc_ingest`'s identical struct --
    /// generated once, offline, from PR #440's actual Python
    /// `identity_crypto.py`. See this module's doc comment.
    #[derive(Deserialize)]
    struct GoldenVector {
        dek_hex: String,
        tenant_id: String,
        stream: String,
        field: String,
        event_id: String,
        plaintext: String,
        envelope: JsonEnvelope,
    }

    /// Cross-language interop: decrypts a ciphertext produced by PR #440's
    /// real Python `identity_crypto.encrypt_identity_value` -- the same
    /// fixture `core/svc_ingest/src/identity_crypto.rs` uses, proving the
    /// decrypt side of *this* service also speaks the shared wire format.
    #[test]
    fn golden_vector_matches_python_reference_implementation() {
        let raw = include_str!("../tests/fixtures/identity_crypto_golden_vector.json");
        let vector: GoldenVector = serde_json::from_str(raw).unwrap();
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
        .expect("svc-process decrypt must accept a Python-produced envelope");
        assert_eq!(plaintext, vector.plaintext);
    }
}
