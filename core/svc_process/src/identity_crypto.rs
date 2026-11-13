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
//! **Wired:** [`decrypt_identity_fields`] is this module's pre-stage entry
//! point; `crate::spine::handle_delivered` calls it on every consumed
//! `penguin_spine::PlatformEvent` after hop verification and strictly before
//! `pii_tokenize`/dispatch, using the *same* `(tenant, stream, event_id)`
//! triple svc-ingest encrypted under (the envelope's numeric tenant id -- the
//! AAD tenant, never the mutable slug -- the delivered entry's own stream key
//! and `event_id`). The DEK comes from hub-api's `KeyService` through
//! `hub_client::dek`, shared with svc-ingest so both sides speak one seal
//! contract; each field's key version is read from its own envelope header, so
//! a key rotation never strands an in-flight event.
//!
//! Wire format is identical to the encrypt side -- see that module's doc
//! comment for the AAD/header/JSON-envelope layout and
//! `tests::golden_vector_matches_python_reference_implementation` for the
//! cross-language interop proof (shared fixture,
//! `tests/fixtures/identity_crypto_golden_vector.json`).

use std::collections::HashMap;
use std::time::Duration;

use aes_gcm::aead::Aead;
use aes_gcm::{Aes256Gcm, KeyInit, Nonce};
use base64::Engine;
use serde::{Deserialize, Serialize};

const FORMAT_VERSION: u8 = 1;
const NONCE_LEN: usize = 12;
#[cfg(test)]
const DEK_LEN: usize = 32;
const TAG_LEN: usize = 16;
const HEADER_LEN: usize = 1 + 4;

// The DEK provider (hub-api `KeyService` broker, TTL cache keyed by (numeric tenant id, key
// version), dev/alpha local KEK) lives in `hub_client::dek`, shared with svc-ingest's encrypt
// side so the two can never drift apart.
pub use hub_client::dek::{
    CachedDekProvider, ConfiguredDekProvider, Dek, DekBroker, DekInvalidate, DekProvider,
    DekUnavailableError, HubDekBroker, LocalDevDekProvider, LocalDevDekProviderError,
    DEFAULT_DEK_CACHE_TTL, INGEST_STREAM_PURPOSE, STREAM_DEK_SCOPE,
};

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

impl IdentityCryptoError {
    /// Low-cardinality, PII-free label for the `reason` dimension of the
    /// `identity_decrypt_failures_total` metric.
    #[must_use]
    pub fn reason_label(&self) -> &'static str {
        match self {
            Self::Format(_) => "bad_envelope",
            Self::Aead(_) => "aead",
            Self::DekUnavailable(_) => "dek_unavailable",
            Self::NotAnEnvelope(_) => "not_an_envelope",
        }
    }
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

/// Decrypts one field's JSON-string envelope: the key version is read from the envelope's own
/// header and that exact version is fetched (never "whatever is current"), so an event written
/// before a rotation still decrypts.
async fn decrypt_field<D: DekProvider>(
    raw: &str,
    dek_provider: &D,
    tenant_id: i64,
    aad_tenant: &str,
    stream: &str,
    field: &str,
    event_id: &str,
) -> Result<String, IdentityCryptoError> {
    let envelope: JsonEnvelope =
        serde_json::from_str(raw).map_err(|e| IdentityCryptoError::NotAnEnvelope(e.to_string()))?;
    let dek_version = envelope_dek_version(&from_json_envelope(&envelope)?)?;
    let dek = dek_provider.get_dek_version(tenant_id, dek_version).await?;
    decrypt_identity_value(&envelope, &dek, aad_tenant, stream, field, event_id)
}

/// **Pre-stage entry point** (see this module's doc comment): decrypts a
/// consumed `PlatformEvent`'s `actor` field plus every
/// [`PAYLOAD_IDENTITY_FIELDS`] payload field present, in place. `tenant_id`
/// is the NUMERIC tenant id the DEK is requested for; `aad_tenant` is the
/// tenant string svc-ingest bound into the AAD (the numeric id rendered
/// decimal on a schema-v3 envelope, the slug on the legacy one). A field
/// that's absent, or whose value isn't a string, is left untouched.
///
/// **All-or-nothing, fail closed:** every field is decrypted into staging
/// first and `event` is only mutated once ALL succeeded, so an error never
/// leaves a half-decrypted event, and a still-encrypted string is never
/// passed through as if it were plaintext.
pub async fn decrypt_identity_fields<D: DekProvider>(
    event: &mut penguin_spine::PlatformEvent,
    dek_provider: &D,
    tenant_id: i64,
    aad_tenant: &str,
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

    let actor = match event.actor.as_deref() {
        Some(raw) => Some(
            decrypt_field(
                raw,
                dek_provider,
                tenant_id,
                aad_tenant,
                stream,
                "actor",
                event_id,
            )
            .await?,
        ),
        None => None,
    };
    let mut payload_plaintexts: Vec<(&'static str, String)> = Vec::new();
    for field in PAYLOAD_IDENTITY_FIELDS {
        let Some(serde_json::Value::String(raw)) = event.payload.get(*field) else {
            continue;
        };
        let plaintext = decrypt_field(
            raw,
            dek_provider,
            tenant_id,
            aad_tenant,
            stream,
            field,
            event_id,
        )
        .await?;
        payload_plaintexts.push((field, plaintext));
    }

    if actor.is_some() {
        event.actor = actor;
    }
    for (field, plaintext) in payload_plaintexts {
        event
            .payload
            .insert(field.to_string(), serde_json::Value::String(plaintext));
    }
    Ok(())
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
pub async fn run_dek_invalidation_listener<C: DekInvalidate, S: InvalidationSource>(
    mut source: S,
    cache: C,
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
                        match fields.get("tenant").map(|t| t.parse::<i64>()) {
                            Some(Ok(tenant_id)) => cache.invalidate(tenant_id),
                            Some(Err(_)) => tracing::warn!(
                                "dek invalidation entry 'tenant' is not a numeric tenant id, skipping"
                            ),
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

/// Shared test fixtures: a hard-gated local DEK source and the encrypt side of the envelope
/// format (svc-process is decrypt-only in production), so the rest of this crate's tests can
/// feed `handle_delivered` the ciphertext svc-ingest would have written.
#[cfg(test)]
pub(crate) mod test_support {
    use super::*;
    use aes_gcm::aead::OsRng;
    use aes_gcm::AeadCore;

    const TEST_KEK: [u8; DEK_LEN] = [0x42; DEK_LEN];

    /// A `ConfiguredDekProvider` serving [`dek`]'s keys.
    pub(crate) fn dek_provider() -> ConfiguredDekProvider {
        ConfiguredDekProvider::local_dev(
            LocalDevDekProvider::from_kek("local", TEST_KEK).expect("local env is allowed"),
        )
    }

    /// The DEK [`dek_provider`] serves for `tenant_id` (key version 1).
    pub(crate) fn dek(tenant_id: i64) -> Dek {
        let hk = hkdf::Hkdf::<sha2::Sha256>::new(None, &TEST_KEK);
        let mut out = [0u8; DEK_LEN];
        hk.expand(
            format!("waddles-ingest-dev-dek:{tenant_id}:1").as_bytes(),
            &mut out,
        )
        .expect("32 bytes is a valid HKDF length");
        zeroize::Zeroizing::new(out)
    }

    /// Encrypts one value into the JSON-string envelope svc-ingest writes.
    pub(crate) fn encrypt_value(
        plaintext: &str,
        dek: &Dek,
        dek_version: u32,
        aad_tenant: &str,
        stream: &str,
        field: &str,
        event_id: &str,
    ) -> String {
        let aad = build_aad(aad_tenant, stream, field, event_id, dek_version);
        let nonce = Aes256Gcm::generate_nonce(&mut OsRng);
        let ct = Aes256Gcm::new_from_slice(dek.as_ref())
            .expect("32-byte key")
            .encrypt(
                &nonce,
                aes_gcm::aead::Payload {
                    msg: plaintext.as_bytes(),
                    aad: &aad,
                },
            )
            .expect("AES-GCM encrypt");
        let b64 = base64::engine::general_purpose::STANDARD;
        serde_json::to_string(&JsonEnvelope {
            v: FORMAT_VERSION,
            dek_version,
            nonce: b64.encode(nonce.as_slice()),
            ct: b64.encode(&ct),
        })
        .expect("envelope serializes")
    }

    /// Encrypts a delivered entry's identity fields in place under [`dek_provider`]'s key,
    /// binding the entry's own tenant/stream/event id exactly as svc-ingest does. `actor`
    /// controls whether the top-level actor is encrypted too (a test that swaps only the
    /// payload after the fixture was built passes `false`).
    pub(crate) fn encrypt_delivered(d: &mut penguin_spine::Delivered, actor: bool) {
        let tenant_id = d.env.tenant_id.unwrap_or(0);
        let aad_tenant = d
            .env
            .tenant_id
            .map_or_else(|| d.env.tenant.clone(), |id| id.to_string());
        let key = dek(tenant_id);
        let (stream, event_id) = (d.stream.clone(), d.env.event_id.clone());
        if actor {
            if let Some(a) = d.env.event.actor.take() {
                d.env.event.actor = Some(encrypt_value(
                    &a,
                    &key,
                    1,
                    &aad_tenant,
                    &stream,
                    "actor",
                    &event_id,
                ));
            }
        }
        for field in PAYLOAD_IDENTITY_FIELDS {
            if let Some(serde_json::Value::String(v)) = d.env.event.payload.get(*field).cloned() {
                d.env.event.payload.insert(
                    (*field).to_string(),
                    serde_json::Value::String(encrypt_value(
                        &v,
                        &key,
                        1,
                        &aad_tenant,
                        &stream,
                        field,
                        &event_id,
                    )),
                );
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use zeroize::Zeroizing;

    fn test_dek() -> Dek {
        Zeroizing::new([7u8; DEK_LEN])
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

    #[derive(Clone, Default)]
    struct Recorder(std::sync::Arc<std::sync::Mutex<Vec<i64>>>);

    impl DekInvalidate for Recorder {
        fn invalidate(&self, tenant_id: i64) {
            self.0.lock().unwrap().push(tenant_id);
        }
    }

    #[tokio::test]
    async fn invalidation_listener_invalidates_numeric_tenants_for_matching_purpose_only() {
        let rec = Recorder::default();
        let source = FakeInvalidationSource {
            batches: vec![vec![
                invalidation_entry(&[("tenant", "42"), ("purpose", INGEST_STREAM_PURPOSE)]),
                invalidation_entry(&[("tenant", "43"), ("purpose", "other")]),
                invalidation_entry(&[("tenant", "acme"), ("purpose", INGEST_STREAM_PURPOSE)]),
            ]],
            next: 0,
        };
        let (tx, rx) = tokio::sync::oneshot::channel();
        let handle = tokio::spawn(run_dek_invalidation_listener(
            source,
            rec.clone(),
            INGEST_STREAM_PURPOSE,
            rx,
        ));
        tokio::time::sleep(Duration::from_millis(20)).await;
        let _ = tx.send(());
        handle.await.unwrap();
        assert_eq!(*rec.0.lock().unwrap(), vec![42]);
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
            async fn get_dek(&self, _tenant_id: i64) -> Result<(Dek, u32), DekUnavailableError> {
                Err(DekUnavailableError {
                    tenant_id: 0,
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
        decrypt_identity_fields(
            &mut event,
            &AlwaysFailProvider,
            1,
            "acme",
            "stream-a",
            "evt-1",
        )
        .await
        .unwrap();
        assert_eq!(event.actor, None);
    }

    #[tokio::test]
    async fn decrypt_identity_fields_decrypts_actor_in_place() {
        struct FixedDekProvider;
        impl DekProvider for FixedDekProvider {
            async fn get_dek(&self, _tenant_id: i64) -> Result<(Dek, u32), DekUnavailableError> {
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
        decrypt_identity_fields(
            &mut event,
            &FixedDekProvider,
            1,
            "acme",
            "stream-a",
            "evt-1",
        )
        .await
        .unwrap();
        assert_eq!(event.actor.as_deref(), Some("someuser"));
    }

    #[tokio::test]
    async fn decrypt_identity_fields_fails_closed_on_dek_unavailable() {
        struct FailingProvider;
        impl DekProvider for FailingProvider {
            async fn get_dek(&self, _tenant_id: i64) -> Result<(Dek, u32), DekUnavailableError> {
                Err(DekUnavailableError {
                    tenant_id: 0,
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
            decrypt_identity_fields(&mut event, &FailingProvider, 1, "acme", "stream-a", "evt-1")
                .await
                .unwrap_err();
        assert!(matches!(err, IdentityCryptoError::DekUnavailable(_)));
    }

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
            async fn get_dek(&self, _tenant_id: i64) -> Result<(Dek, u32), DekUnavailableError> {
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
        decrypt_identity_fields(
            &mut event,
            &FixedDekProvider,
            1,
            "acme",
            "stream-a",
            "evt-1",
        )
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
            async fn get_dek(&self, _tenant_id: i64) -> Result<(Dek, u32), DekUnavailableError> {
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
                1,
                "acme",
                stream_for_case,
                event_id_for_case,
            )
            .await
            .map_err(|e| format!("case {:?}: decrypt failed: {e}", case.name))
            .expect("table case must decrypt");

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
