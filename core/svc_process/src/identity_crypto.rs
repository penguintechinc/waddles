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
//! unmerged as of this writing. [`decrypt_identity_fields`] is this module's
//! pre-stage entry point -- call it immediately before `pii_tokenize`
//! (or as `pii_tokenize`'s own first step) on every consumed
//! `penguin_spine::PlatformEvent`, using the *same*
//! `(tenant, stream, event_id)` triple svc-ingest encrypted under (the
//! `StageEnvelope`'s own `tenant`/`event_id` fields plus the granted
//! ingest-source stream key svc-process is draining -- see
//! `crate::source_supervisor`). Once #429 lands, its stage should call
//! [`decrypt_identity_fields`] as its first step rather than duplicating this
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
use hkdf::Hkdf;
use serde::{Deserialize, Serialize};
use sha2::Sha256;
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

/// Errors [`decrypt_identity_value`]/[`decrypt_identity_fields`] can return.
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

/// `payload` fields carrying the same identity as `actor`, duplicated in by
/// the Twitch IRC/EventSub normalizers -- byte-identical to
/// `core/svc_ingest/src/identity_crypto.rs::ACTOR_DUPLICATE_FIELDS` and to
/// this crate's own `pii_tokenize.rs::ACTOR_DUPLICATE_FIELDS` (once #429
/// lands). Keep all three lists in sync.
pub const ACTOR_DUPLICATE_FIELDS: &[&str] =
    &["author", "display_name", "user_login", "user_display_name"];

/// Twitch EventSub's channel-owner login -- a genuinely different identity
/// from `actor`. Matches `core/svc_ingest`'s identical constant.
pub const BROADCASTER_LOGIN_FIELD: &str = "broadcaster_login";

/// The message body -- user content that routinely carries `@handle`/
/// `<@id>` mentions. Matches `core/svc_ingest`'s identical constant.
pub const TEXT_FIELD: &str = "text";

/// Every `payload` field this module decrypts when present as a JSON
/// envelope string -- byte-identical list to
/// `core/svc_ingest/src/identity_crypto.rs::PAYLOAD_IDENTITY_FIELDS`.
pub const PAYLOAD_IDENTITY_FIELDS: &[&str] = &[
    "author",
    "display_name",
    "user_login",
    "user_display_name",
    "broadcaster_login",
    "text",
];

/// **Pre-stage entry point** (see this module's doc comment): decrypts a
/// consumed `PlatformEvent`'s `actor` field plus every
/// [`PAYLOAD_IDENTITY_FIELDS`] payload field present, in place, resolving
/// `tenant_id`'s DEK exactly once (the encrypt side used the same DEK for
/// every field of one event, only the AAD's field name differs). A field
/// that's absent, or whose value isn't a string, is left untouched. Fails
/// closed on any error -- callers must never fall through to treating a
/// still-encrypted string as plaintext.
pub async fn decrypt_identity_fields<D: DekProvider>(
    event: &mut penguin_spine::PlatformEvent,
    dek_provider: &D,
    tenant_id: &str,
    stream: &str,
    event_id: &str,
) -> Result<(), IdentityCryptoError> {
    let has_payload_identity_field = PAYLOAD_IDENTITY_FIELDS.iter().any(|field| {
        matches!(
            event.payload.get(*field),
            Some(serde_json::Value::String(_))
        )
    });
    if event.actor.is_none() && !has_payload_identity_field {
        return Ok(());
    }

    let (dek, _dek_version) = dek_provider.get_dek(tenant_id).await?;
    let decrypt_field = |raw: &str, field: &str| -> Result<String, IdentityCryptoError> {
        let envelope: JsonEnvelope = serde_json::from_str(raw)
            .map_err(|e| IdentityCryptoError::NotAnEnvelope(e.to_string()))?;
        decrypt_identity_value(&envelope, &dek, tenant_id, stream, field, event_id)
    };

    if let Some(raw) = event.actor.take() {
        event.actor = Some(decrypt_field(&raw, "actor")?);
    }
    for field in PAYLOAD_IDENTITY_FIELDS {
        let Some(serde_json::Value::String(raw)) = event.payload.get(*field).cloned() else {
            continue;
        };
        let plaintext = decrypt_field(&raw, field)?;
        event
            .payload
            .insert((*field).to_string(), serde_json::Value::String(plaintext));
    }
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

/// This is always what [`HubApiDekProvider`] requests -- see
/// `core/svc_ingest`'s identical constant.
pub const INGEST_STREAM_PURPOSE: &str = "ingest-stream";

/// Calls hub-api's tenant-DEK broker endpoint
/// (`POST /api/v1/internal/keys/tenant-dek`, `feature/tenant-dek-broker`/
/// #442 -- **server side not yet merged, and #442 is itself being
/// redesigned as of this writing; this client codes against the
/// coordinator-described contract and will be reconciled with #442's PR
/// description once it lands**). Byte-for-byte identical protocol to
/// `core/svc_ingest`'s `HubApiDekProvider` (see that module's doc comment
/// for the full HPKE-style seal/open rationale): a fresh X25519 keypair
/// per call, `{service_id, tenant_id, purpose, version}` sent alongside the
/// ephemeral public key, and the DEK sealed to it in the response.
pub struct HubApiDekProvider<J: MachineJwtProvider> {
    http: reqwest::Client,
    hub_api_url: String,
    jwt_provider: J,
    service_id: String,
}

impl<J: MachineJwtProvider + Clone> Clone for HubApiDekProvider<J> {
    fn clone(&self) -> Self {
        Self {
            http: self.http.clone(),
            hub_api_url: self.hub_api_url.clone(),
            jwt_provider: self.jwt_provider.clone(),
            service_id: self.service_id.clone(),
        }
    }
}

#[derive(Serialize)]
struct TenantDekRequest<'a> {
    tenant_id: &'a str,
    purpose: &'a str,
    version: u32,
    client_pubkey: String,
}

#[derive(Deserialize)]
struct TenantDekResponse {
    enc: String,
    ciphertext: String,
    dek_version: u32,
    ttl_seconds: u64,
}

impl<J: MachineJwtProvider> HubApiDekProvider<J> {
    pub fn new(
        http: reqwest::Client,
        hub_api_url: impl Into<String>,
        jwt_provider: J,
        service_id: impl Into<String>,
    ) -> Self {
        Self {
            http,
            hub_api_url: hub_api_url.into(),
            jwt_provider,
            service_id: service_id.into(),
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

        let client_secret = x25519_dalek::EphemeralSecret::random();
        let client_public = x25519_dalek::PublicKey::from(&client_secret);

        let url = format!(
            "{}/api/v1/internal/keys/tenant-dek",
            self.hub_api_url.trim_end_matches('/')
        );
        let resp = self
            .http
            .post(&url)
            .bearer_auth(jwt)
            .json(&TenantDekRequest {
                tenant_id,
                purpose: INGEST_STREAM_PURPOSE,
                version: 1,
                client_pubkey: base64::engine::general_purpose::STANDARD
                    .encode(client_public.as_bytes()),
            })
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

        let context = format!(
            "{}|{}|{}|{}",
            self.service_id, tenant_id, INGEST_STREAM_PURPOSE, 1
        );
        let dek_bytes = hpke_seal_open(
            client_secret,
            &body.enc,
            &body.ciphertext,
            context.as_bytes(),
        )
        .map_err(&unavailable)?;

        let ttl = Duration::from_secs(body.ttl_seconds).min(DEFAULT_DEK_CACHE_TTL);
        let _ = ttl; // TODO(#442): thread a per-response TTL through once TtlCachedDekProvider accepts one.
        Ok((Zeroizing::new(dek_bytes), body.dek_version))
    }
}

/// HPKE-lite seal-open -- byte-identical construction to
/// `core/svc_ingest`'s identical function; see that module's doc comment
/// for the full rationale and the RFC 9180-conformance caveat.
fn hpke_seal_open(
    client_secret: x25519_dalek::EphemeralSecret,
    enc_b64: &str,
    ciphertext_b64: &str,
    context: &[u8],
) -> Result<[u8; DEK_LEN], String> {
    let enc_bytes = base64::engine::general_purpose::STANDARD
        .decode(enc_b64)
        .map_err(|e| format!("malformed enc: {e}"))?;
    let enc_arr: [u8; 32] = enc_bytes
        .try_into()
        .map_err(|_| "enc must be exactly 32 bytes".to_string())?;
    let server_public = x25519_dalek::PublicKey::from(enc_arr);
    let shared_secret = client_secret.diffie_hellman(&server_public);

    let hk = Hkdf::<Sha256>::new(None, shared_secret.as_bytes());
    let mut key_and_nonce = [0u8; DEK_LEN + NONCE_LEN];
    hk.expand(context, &mut key_and_nonce)
        .map_err(|e| format!("HKDF expand failed: {e}"))?;
    let (key_bytes, nonce_bytes) = key_and_nonce.split_at(DEK_LEN);

    let ciphertext = base64::engine::general_purpose::STANDARD
        .decode(ciphertext_b64)
        .map_err(|e| format!("malformed ciphertext: {e}"))?;
    let cipher = Aes256Gcm::new_from_slice(key_bytes).expect("key_bytes is always 32 bytes");
    let nonce = Nonce::from_slice(nonce_bytes);
    let plaintext = cipher
        .decrypt(
            nonce,
            aes_gcm::aead::Payload {
                msg: &ciphertext,
                aad: context,
            },
        )
        .map_err(|_| "HPKE-lite unseal failed (AEAD authentication error)".to_string())?;
    if plaintext.len() != DEK_LEN {
        return Err(format!(
            "unsealed DEK is {} bytes, expected {DEK_LEN}",
            plaintext.len()
        ));
    }
    let mut out = [0u8; DEK_LEN];
    out.copy_from_slice(&plaintext);
    Ok(out)
}

/// The Valkey stream every DEK cache consumer MUST subscribe to -- see
/// `core/svc_ingest`'s identical constant/doc comment.
pub const DEK_INVALIDATION_STREAM: &str = "keys:tenant-dek:invalidate";

/// See `core/svc_ingest`'s identical trait for the full doc.
#[allow(async_fn_in_trait)]
pub trait InvalidationSource: Send {
    async fn read_after(
        &mut self,
        last_id: &str,
    ) -> Result<(String, Vec<HashMap<String, String>>), String>;
}

impl InvalidationSource for redis::aio::MultiplexedConnection {
    async fn read_after(
        &mut self,
        last_id: &str,
    ) -> Result<(String, Vec<HashMap<String, String>>), String> {
        let opts = redis::streams::StreamReadOptions::default().block(5000);
        let reply: redis::streams::StreamReadReply = redis::AsyncCommands::xread_options(
            self,
            &[DEK_INVALIDATION_STREAM],
            &[last_id],
            &opts,
        )
        .await
        .map_err(|e| e.to_string())?;

        let mut newest_id = last_id.to_string();
        let mut entries = Vec::new();
        for key in reply.keys {
            for stream_id in key.ids {
                newest_id = stream_id.id.clone();
                let mut fields = HashMap::new();
                for (field, value) in stream_id.map {
                    if let redis::Value::BulkString(bytes) = value {
                        if let Ok(s) = String::from_utf8(bytes) {
                            fields.insert(field, s);
                        }
                    }
                }
                entries.push(fields);
            }
        }
        Ok((newest_id, entries))
    }
}

/// See `core/svc_ingest`'s identical function for the full doc -- **not yet
/// spawned from `src/lib.rs`**, same documented-follow-up status.
pub async fn run_dek_invalidation_listener<D: DekProvider, S: InvalidationSource>(
    mut source: S,
    cache: TtlCachedDekProvider<D>,
    expected_purpose: &str,
    mut shutdown: tokio::sync::oneshot::Receiver<()>,
) {
    let mut last_id = "$".to_string();
    loop {
        tokio::select! {
            _ = &mut shutdown => return,
            result = source.read_after(&last_id) => match result {
                Ok((new_last_id, fields_list)) => {
                    last_id = new_last_id;
                    for fields in fields_list {
                        if fields.get("purpose").map(String::as_str) != Some(expected_purpose) {
                            continue;
                        }
                        match fields.get("tenant") {
                            Some(tenant) => cache.invalidate(tenant),
                            None => tracing::warn!(
                                "dek invalidation entry missing 'tenant' field, skipping"
                            ),
                        }
                    }
                }
                Err(err) => {
                    tracing::warn!(error = %err, "dek invalidation stream read failed, retrying");
                    tokio::select! {
                        _ = &mut shutdown => return,
                        () = tokio::time::sleep(Duration::from_secs(1)) => {}
                    }
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_dek() -> Dek {
        Zeroizing::new([7u8; DEK_LEN])
    }

    /// Manually seals `dek_plain` the way a real #442 broker would -- see
    /// `core/svc_ingest`'s identical test helper for the full rationale.
    fn seal_for_test(
        client_public: &x25519_dalek::PublicKey,
        context: &[u8],
        dek_plain: &[u8; DEK_LEN],
    ) -> (String, String) {
        let server_secret = x25519_dalek::EphemeralSecret::random();
        let server_public = x25519_dalek::PublicKey::from(&server_secret);
        let shared = server_secret.diffie_hellman(client_public);
        let hk = Hkdf::<Sha256>::new(None, shared.as_bytes());
        let mut key_and_nonce = [0u8; DEK_LEN + NONCE_LEN];
        hk.expand(context, &mut key_and_nonce).unwrap();
        let (key_bytes, nonce_bytes) = key_and_nonce.split_at(DEK_LEN);
        let cipher = Aes256Gcm::new_from_slice(key_bytes).unwrap();
        let nonce = Nonce::from_slice(nonce_bytes);
        let ct = cipher
            .encrypt(
                nonce,
                aes_gcm::aead::Payload {
                    msg: dek_plain.as_slice(),
                    aad: context,
                },
            )
            .unwrap();
        (
            base64::engine::general_purpose::STANDARD.encode(server_public.as_bytes()),
            base64::engine::general_purpose::STANDARD.encode(ct),
        )
    }

    #[test]
    fn hpke_seal_open_round_trips_a_sealed_dek() {
        let client_secret = x25519_dalek::EphemeralSecret::random();
        let client_public = x25519_dalek::PublicKey::from(&client_secret);
        let context = b"svc-process|acme|ingest-stream|1";
        let dek_plain = [42u8; DEK_LEN];
        let (enc_b64, ct_b64) = seal_for_test(&client_public, context, &dek_plain);
        let opened = hpke_seal_open(client_secret, &enc_b64, &ct_b64, context).unwrap();
        assert_eq!(opened, dek_plain);
    }

    #[test]
    fn hpke_seal_open_fails_closed_on_context_mismatch() {
        let client_secret = x25519_dalek::EphemeralSecret::random();
        let client_public = x25519_dalek::PublicKey::from(&client_secret);
        let (enc_b64, ct_b64) = seal_for_test(
            &client_public,
            b"svc-process|acme|ingest-stream|1",
            &[1u8; DEK_LEN],
        );
        let err = hpke_seal_open(
            client_secret,
            &enc_b64,
            &ct_b64,
            b"svc-process|other-tenant|ingest-stream|1",
        )
        .unwrap_err();
        assert!(err.contains("AEAD"));
    }

    fn invalidation_entry(fields: &[(&str, &str)]) -> HashMap<String, String> {
        fields
            .iter()
            .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
            .collect()
    }

    struct FakeInvalidationSource {
        batches: Vec<Vec<HashMap<String, String>>>,
        next: usize,
    }

    impl InvalidationSource for FakeInvalidationSource {
        async fn read_after(
            &mut self,
            last_id: &str,
        ) -> Result<(String, Vec<HashMap<String, String>>), String> {
            if self.next < self.batches.len() {
                let batch = self.batches[self.next].clone();
                self.next += 1;
                Ok((format!("{}-1", self.next), batch))
            } else {
                tokio::task::yield_now().await;
                Ok((last_id.to_string(), Vec::new()))
            }
        }
    }

    #[tokio::test]
    async fn invalidation_listener_drops_cache_for_matching_purpose() {
        struct CountingProvider(std::sync::atomic::AtomicU32);
        impl DekProvider for CountingProvider {
            async fn get_dek(&self, _tenant_id: &str) -> Result<(Dek, u32), DekUnavailableError> {
                self.0.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                Ok((Zeroizing::new([1u8; DEK_LEN]), 1))
            }
        }
        let cache = TtlCachedDekProvider::new(
            CountingProvider(std::sync::atomic::AtomicU32::new(0)),
            Duration::from_secs(60),
        );
        cache.get_dek("acme").await.unwrap();
        cache.get_dek("acme").await.unwrap();
        assert_eq!(cache.inner.0.load(std::sync::atomic::Ordering::SeqCst), 1);

        let source = FakeInvalidationSource {
            batches: vec![vec![invalidation_entry(&[
                ("tenant", "acme"),
                ("purpose", INGEST_STREAM_PURPOSE),
            ])]],
            next: 0,
        };
        let (tx, rx) = tokio::sync::oneshot::channel();
        let cache_clone = cache.clone();
        let handle = tokio::spawn(run_dek_invalidation_listener(
            source,
            cache_clone,
            INGEST_STREAM_PURPOSE,
            rx,
        ));
        tokio::time::sleep(Duration::from_millis(20)).await;
        let _ = tx.send(());
        handle.await.unwrap();

        cache.get_dek("acme").await.unwrap();
        assert_eq!(cache.inner.0.load(std::sync::atomic::Ordering::SeqCst), 2);
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
    async fn decrypt_identity_fields_is_a_noop_for_a_none_actor_and_no_payload_fields() {
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
        decrypt_identity_fields(&mut event, &AlwaysFailProvider, "acme", "stream-a", "evt-1")
            .await
            .unwrap();
        assert_eq!(event.actor, None);
    }

    #[tokio::test]
    async fn decrypt_identity_fields_decrypts_actor_in_place() {
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
        decrypt_identity_fields(&mut event, &FixedDekProvider, "acme", "stream-a", "evt-1")
            .await
            .unwrap();
        assert_eq!(event.actor.as_deref(), Some("someuser"));
    }

    #[tokio::test]
    async fn decrypt_identity_fields_fails_closed_on_dek_unavailable() {
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
        let err =
            decrypt_identity_fields(&mut event, &FailingProvider, "acme", "stream-a", "evt-1")
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

    /// `decrypt_identity_fields` decrypts every present payload identity
    /// field, not just `actor` -- one independent envelope per field, same
    /// DEK, field name in the AAD.
    #[tokio::test]
    async fn decrypt_identity_fields_decrypts_every_payload_identity_field() {
        struct FixedDekProvider;
        impl DekProvider for FixedDekProvider {
            async fn get_dek(&self, _tenant_id: &str) -> Result<(Dek, u32), DekUnavailableError> {
                Ok((test_dek(), 1))
            }
        }
        let field_plaintexts: &[(&str, &str)] = &[
            ("actor", "raider_login"),
            ("author", "chatterbox99"),
            ("display_name", "ChatterBox99"),
            ("user_login", "raider_login"),
            ("user_display_name", "RaiderDisplayName"),
            ("broadcaster_login", "channelowner_login"),
            ("text", "hey @moderator_jane check this out"),
        ];
        assert!(
            !field_plaintexts.is_empty(),
            "table test must examine at least one field"
        );

        let mut payload = serde_json::Map::new();
        let mut actor_envelope = None;
        for (field, plaintext) in field_plaintexts {
            let aad = build_aad("acme", "stream-a", field, "evt-1", 1);
            let raw = encrypt_via_fixture_format(plaintext, &test_dek(), &aad, [5u8; NONCE_LEN]);
            let envelope = serde_json::to_string(&to_json(&raw)).unwrap();
            if *field == "actor" {
                actor_envelope = Some(envelope);
            } else {
                payload.insert((*field).to_string(), serde_json::Value::String(envelope));
            }
        }

        let mut event = penguin_spine::PlatformEvent {
            platform: "twitch".to_string(),
            event_type: "chat.message".to_string(),
            actor: actor_envelope,
            payload,
            occurred_at: "2026-09-14T12:00:00.000Z".to_string(),
            source: None,
        };
        decrypt_identity_fields(&mut event, &FixedDekProvider, "acme", "stream-a", "evt-1")
            .await
            .unwrap();

        let mut examined = 0;
        for (field, plaintext) in field_plaintexts {
            let actual = if *field == "actor" {
                event.actor.clone()
            } else {
                event
                    .payload
                    .get(*field)
                    .and_then(|v| v.as_str())
                    .map(str::to_string)
            };
            assert_eq!(
                actual.as_deref(),
                Some(*plaintext),
                "field {field:?} did not decrypt correctly"
            );
            examined += 1;
        }
        assert_eq!(examined, field_plaintexts.len());
    }

    /// Table test (coordinator follow-up): mirrors
    /// `core/svc_ingest/src/publish.rs`'s identical table test from the
    /// decrypt side -- for realistic Discord and Twitch normalizer-shaped
    /// events (already encrypted, as `svc_ingest::publish::publish_event`
    /// would produce them), `decrypt_identity_fields` recovers every
    /// original plaintext, proving the pre-stage entry point handles both
    /// platforms' full identity-field surface. Non-zero denominator
    /// asserted directly.
    #[tokio::test]
    async fn decrypt_identity_fields_recovers_twitch_and_discord_fixtures() {
        struct FixedDekProvider;
        impl DekProvider for FixedDekProvider {
            async fn get_dek(&self, _tenant_id: &str) -> Result<(Dek, u32), DekUnavailableError> {
                Ok((test_dek(), 1))
            }
        }

        fn encrypt_field(
            tenant: &str,
            stream: &str,
            field: &str,
            event_id: &str,
            plaintext: &str,
        ) -> String {
            let aad = build_aad(tenant, stream, field, event_id, 1);
            let raw = encrypt_via_fixture_format(plaintext, &test_dek(), &aad, [8u8; NONCE_LEN]);
            serde_json::to_string(&to_json(&raw)).unwrap()
        }

        struct Case {
            name: &'static str,
            event: penguin_spine::PlatformEvent,
            expected: Vec<(&'static str, &'static str)>,
        }

        let stream = "waddles:t:acme:c:_tenant:src:twitch:tw-somechannel:events";
        let mut twitch_payload = serde_json::Map::new();
        twitch_payload.insert(
            "text".to_string(),
            serde_json::Value::String(encrypt_field(
                "acme",
                stream,
                "text",
                "evt-twitch",
                "hey @moderator_jane",
            )),
        );
        twitch_payload.insert(
            "author".to_string(),
            serde_json::Value::String(encrypt_field(
                "acme",
                stream,
                "author",
                "evt-twitch",
                "chatterbox99",
            )),
        );
        twitch_payload.insert(
            "display_name".to_string(),
            serde_json::Value::String(encrypt_field(
                "acme",
                stream,
                "display_name",
                "evt-twitch",
                "ChatterBox99",
            )),
        );

        let discord_stream = "waddles:t:acme:c:_tenant:src:discord:dg-111:events";
        let mut discord_payload = serde_json::Map::new();
        discord_payload.insert(
            "text".to_string(),
            serde_json::Value::String(encrypt_field(
                "acme",
                discord_stream,
                "text",
                "evt-discord",
                "thanks <@999888777>",
            )),
        );

        let cases = vec![
            Case {
                name: "twitch_irc",
                event: penguin_spine::PlatformEvent {
                    platform: "twitch".to_string(),
                    event_type: "chat.message".to_string(),
                    actor: Some(encrypt_field(
                        "acme",
                        stream,
                        "actor",
                        "evt-twitch",
                        "chatterbox99",
                    )),
                    payload: twitch_payload,
                    occurred_at: "2026-09-14T12:00:00.000Z".to_string(),
                    source: None,
                },
                expected: vec![
                    ("actor", "chatterbox99"),
                    ("text", "hey @moderator_jane"),
                    ("author", "chatterbox99"),
                    ("display_name", "ChatterBox99"),
                ],
            },
            Case {
                name: "discord_gateway",
                event: penguin_spine::PlatformEvent {
                    platform: "discord".to_string(),
                    event_type: "message".to_string(),
                    actor: Some(encrypt_field(
                        "acme",
                        discord_stream,
                        "actor",
                        "evt-discord",
                        "discorduser42",
                    )),
                    payload: discord_payload,
                    occurred_at: "2026-09-14T12:00:00.000Z".to_string(),
                    source: None,
                },
                expected: vec![("actor", "discorduser42"), ("text", "thanks <@999888777>")],
            },
        ];
        assert!(
            !cases.is_empty(),
            "table test must examine at least one case"
        );

        let mut examined = 0;
        for case in cases {
            let mut event = case.event;
            let (stream_for_case, event_id_for_case) = if case.name == "twitch_irc" {
                (stream, "evt-twitch")
            } else {
                (discord_stream, "evt-discord")
            };
            decrypt_identity_fields(
                &mut event,
                &FixedDekProvider,
                "acme",
                stream_for_case,
                event_id_for_case,
            )
            .await
            .unwrap_or_else(|e| panic!("case {:?}: decrypt failed: {e}", case.name));

            for (field, expected) in &case.expected {
                let actual = if *field == "actor" {
                    event.actor.clone()
                } else {
                    event
                        .payload
                        .get(*field)
                        .and_then(|v| v.as_str())
                        .map(str::to_string)
                };
                assert_eq!(
                    actual.as_deref(),
                    Some(*expected),
                    "case {:?} field {field:?} did not decrypt correctly",
                    case.name
                );
            }
            examined += 1;
        }
        assert_eq!(
            examined, 2,
            "expected to examine exactly the 2 defined platform cases"
        );
    }
}
