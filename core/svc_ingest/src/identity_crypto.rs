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
//! **Every identity-bearing field is in scope, not just `actor`**
//! (coordinator follow-up to the original #440-parity cut): the top-level
//! `actor` field, every [`ACTOR_DUPLICATE_FIELDS`] payload field (the same
//! identity as `actor`, duplicated into payload by the Twitch IRC/EventSub
//! normalizers -- mirrors `core/svc_process`'s `feature/ingest-pii-
//! tokenization` branch's `pii_tokenize.rs::ACTOR_DUPLICATE_FIELDS`
//! exactly, so the two passes agree on what counts as identity),
//! [`BROADCASTER_LOGIN_FIELD`] (Twitch EventSub's channel-owner login --
//! a genuinely different identity from `actor`, same as that module's
//! treatment), and [`TEXT_FIELD`] (message body: raw chat/message text
//! routinely carries `@handle`/`<@id>` mentions, so it is user content in
//! its own right, not just metadata). Opaque platform ids (`user_id`,
//! `author_id`, `broadcaster_id`, `room_id`, `message_id`) stay plaintext
//! -- they are reference keys, not human-readable identity, the same
//! posture `critical-rules.md` PII Tokenization takes for UUID references.
//!
//! **Not encrypted, and deliberately so:** Twitch's `channel_name` payload
//! field (`normalize::normalize_twitch_irc`) duplicates the exact string
//! already present, in the clear, in this event's own Valkey **stream
//! key** (`waddles:t:<tenant>:c:<community>:src:twitch:<channel>:events`)
//! -- encrypting the payload copy while the identical value stays visible
//! in the key name it is written under would add ciphertext bytes with no
//! actual confidentiality gain. If a future change stops keying streams by
//! channel name, revisit this.
//!
//! One independent envelope per field (never one shared ciphertext for
//! multiple fields): the AAD's `field` component (see [`build_aad`]) names
//! the exact field, so a ciphertext for one field can never be swapped
//! into another field's slot and still decrypt.

use std::collections::HashMap;
use std::time::Duration;

use aes_gcm::aead::{Aead, KeyInit, OsRng};
use aes_gcm::{AeadCore, Aes256Gcm, Nonce};
use base64::Engine;
use serde::{Deserialize, Serialize};

const FORMAT_VERSION: u8 = 1;
const NONCE_LEN: usize = 12; // 96-bit GCM nonce, CSPRNG per NIST SP 800-38D
#[cfg(test)]
const DEK_LEN: usize = 32; // AES-256
const TAG_LEN: usize = 16;
const HEADER_LEN: usize = 1 + 4; // format version (1B) + dek_version (4B BE), matches Python's `>BI`

// The DEK provider (hub-api `KeyService` broker, TTL cache keyed by (numeric tenant id,
// key version), dev/alpha local KEK) lives in `hub_client::dek`, shared with svc-process's
// decrypt side so the two can never drift apart. Re-exported here so this crate's historical
// `identity_crypto::` paths keep resolving.
pub use hub_client::dek::{
    CachedDekProvider, ConfiguredDekProvider, Dek, DekBroker, DekInvalidate, DekProvider,
    DekUnavailableError, HubDekBroker, LocalDevDekProvider, LocalDevDekProviderError,
    DEFAULT_DEK_CACHE_TTL, INGEST_STREAM_PURPOSE, STREAM_DEK_SCOPE,
};

/// The top-level `PlatformEvent` field this module always encrypts when
/// present. Matches PR #440's original `IDENTITY_FIELDS` scope.
pub const IDENTITY_FIELDS: &[&str] = &["actor"];

/// `payload` fields carrying the *same* identity as `actor`, duplicated in
/// by the Twitch IRC/EventSub normalizers -- byte-identical to
/// `core/svc_process`'s `feature/ingest-pii-tokenization` branch's
/// `pii_tokenize.rs::ACTOR_DUPLICATE_FIELDS`. Keep these two lists in sync;
/// a field #429's tokenizer treats as an actor duplicate must also be
/// encrypted here, or `pii_tokenize` would see (and skip) an already-
/// plaintext field it never actually reads pre-decryption.
pub const ACTOR_DUPLICATE_FIELDS: &[&str] =
    &["author", "display_name", "user_login", "user_display_name"];

/// Twitch EventSub's channel-owner login -- a genuinely different identity
/// from `actor` (e.g. a raid's actor is the raider, not the broadcaster
/// being raided). Matches `pii_tokenize.rs::BROADCASTER_LOGIN_FIELD`.
pub const BROADCASTER_LOGIN_FIELD: &str = "broadcaster_login";

/// The message body -- user content that routinely carries `@handle`
/// (Twitch/IRC) or `<@id>` (Discord) mentions, encrypted as a whole field
/// like any other identity-bearing value (this module does not attempt to
/// tokenize individual mentions in place -- that per-mention pass is
/// `pii_tokenize.rs`'s job, run on the *decrypted* text in svc-process).
pub const TEXT_FIELD: &str = "text";

/// Every `payload` field this module encrypts when present as a JSON
/// string value -- the full identity surface beyond top-level `actor`.
/// [`crate::publish::publish_event`] iterates this list; `core/svc_process`
/// decrypts the same list in its pre-stage pass.
pub const PAYLOAD_IDENTITY_FIELDS: &[&str] = &[
    "author",
    "display_name",
    "user_login",
    "user_display_name",
    "broadcaster_login",
    "text",
];

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
// DEK cache invalidation (Valkey stream).
// ---------------------------------------------------------------------

/// The Valkey stream every DEK cache consumer MUST subscribe to and act on:
/// on any entry naming `(tenant, purpose)` this process cares about, the
/// cached DEK for that tenant is dropped immediately, regardless of its
/// TTL -- the broker's rotation/revocation signal always overrides the
/// cache's own timer. Exact field names are #442's to finalize; this
/// module reads `tenant`/`purpose`/`version` defensively (a missing field
/// is logged and the entry skipped, never treated as fatal).
pub const DEK_INVALIDATION_STREAM: &str = "keys:tenant-dek:invalidate";

/// Abstraction over polling [`DEK_INVALIDATION_STREAM`] -- lets
/// [`run_dek_invalidation_listener`]'s dispatch logic (which
/// `(tenant, purpose)` entries actually trigger an invalidation) be
/// unit-tested without a live Valkey connection. Implemented for
/// `redis::aio::MultiplexedConnection` via `XREAD BLOCK` starting from `$`
/// (only entries published after this listener started -- a missed
/// invalidation during a restart is bounded by [`DEFAULT_DEK_CACHE_TTL`]
/// anyway, since the cache entry expires on its own).
#[allow(async_fn_in_trait)]
pub trait InvalidationSource: Send {
    /// Blocks (bounded) until at least one entry newer than `last_id` is
    /// available, or returns `(last_id, [])` on a read timeout with
    /// nothing new -- never blocks forever, so a listener loop always gets
    /// a chance to check its shutdown signal.
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

/// Consumes [`DEK_INVALIDATION_STREAM`] forever (until `shutdown`
/// resolves), invalidating `cache` for every entry whose `purpose` matches
/// `expected_purpose` (this crate always passes [`INGEST_STREAM_PURPOSE`]).
/// A read error is logged and retried after a short backoff -- never
/// treated as fatal, since a transient Valkey blip must not stop this
/// process from serving (worst case, a stale cached DEK is used for up to
/// [`DEFAULT_DEK_CACHE_TTL`] longer than the broker intended).
///
/// **Not yet spawned from `src/lib.rs`** -- wiring this into the service
/// startup path (alongside a real `redis::aio::MultiplexedConnection`) is
/// a documented follow-up once #442's exact stream field names/message
/// shape are published; the function itself is complete and unit-tested
/// today against a fake [`InvalidationSource`].
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
                        // Caches are keyed by NUMERIC tenant id, so the signal must name one.
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

#[cfg(test)]
mod tests {
    use super::*;
    use zeroize::Zeroizing;

    fn test_dek() -> Dek {
        Zeroizing::new([7u8; DEK_LEN])
    }

    /// Yields one batch of fake stream entries per call, then blocks
    /// (simulated: just returns empty) forever -- lets
    /// `dek_invalidation_listener_*` tests drive exactly the entries they
    /// want without a live Valkey stream.
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
                // No more scripted batches -- simulate an indefinite block
                // by yielding once and returning empty, matching a real
                // `XREAD BLOCK` timeout with nothing new.
                tokio::task::yield_now().await;
                Ok((last_id.to_string(), Vec::new()))
            }
        }
    }

    fn entry(fields: &[(&str, &str)]) -> HashMap<String, String> {
        fields
            .iter()
            .map(|(k, v)| ((*k).to_string(), (*v).to_string()))
            .collect()
    }

    /// Records which tenants were invalidated.
    #[derive(Clone, Default)]
    struct Recorder(std::sync::Arc<std::sync::Mutex<Vec<i64>>>);

    impl DekInvalidate for Recorder {
        fn invalidate(&self, tenant_id: i64) {
            self.0.lock().unwrap().push(tenant_id);
        }
    }

    async fn run_listener(batches: Vec<Vec<HashMap<String, String>>>, rec: Recorder) {
        let source = FakeInvalidationSource { batches, next: 0 };
        let (tx, rx) = tokio::sync::oneshot::channel();
        let handle = tokio::spawn(run_dek_invalidation_listener(
            source,
            rec,
            INGEST_STREAM_PURPOSE,
            rx,
        ));
        tokio::time::sleep(Duration::from_millis(20)).await;
        let _ = tx.send(());
        handle.await.unwrap();
    }

    #[tokio::test]
    async fn invalidation_listener_invalidates_the_numeric_tenant_for_matching_purpose() {
        let rec = Recorder::default();
        run_listener(
            vec![vec![entry(&[
                ("tenant", "42"),
                ("purpose", INGEST_STREAM_PURPOSE),
            ])]],
            rec.clone(),
        )
        .await;
        assert_eq!(*rec.0.lock().unwrap(), vec![42]);
    }

    #[tokio::test]
    async fn invalidation_listener_ignores_a_different_purpose_and_non_numeric_tenants() {
        let rec = Recorder::default();
        run_listener(
            vec![vec![
                entry(&[("tenant", "42"), ("purpose", "some-other-purpose")]),
                entry(&[("tenant", "acme"), ("purpose", INGEST_STREAM_PURPOSE)]),
                entry(&[("purpose", INGEST_STREAM_PURPOSE)]),
            ]],
            rec.clone(),
        )
        .await;
        assert!(rec.0.lock().unwrap().is_empty());
    }

    #[tokio::test]
    async fn invalidation_listener_forces_a_real_cache_to_re_resolve() {
        use std::sync::atomic::{AtomicU32, Ordering};
        struct Counting(std::sync::Arc<AtomicU32>);
        impl DekBroker for Counting {
            async fn fetch(
                &self,
                _t: i64,
                _v: u32,
            ) -> Result<hub_client::dek::BrokeredDek, DekUnavailableError> {
                self.0.fetch_add(1, Ordering::SeqCst);
                Ok(hub_client::dek::BrokeredDek {
                    dek: Zeroizing::new([1u8; DEK_LEN]),
                    version: 1,
                    ttl: Duration::from_secs(60),
                })
            }
        }
        let calls = std::sync::Arc::new(AtomicU32::new(0));
        let cache = CachedDekProvider::new(Counting(calls.clone()), Duration::from_secs(60));
        cache.get_dek(42).await.unwrap();
        cache.get_dek(42).await.unwrap();
        assert_eq!(calls.load(Ordering::SeqCst), 1);

        let source = FakeInvalidationSource {
            batches: vec![vec![entry(&[
                ("tenant", "42"),
                ("purpose", INGEST_STREAM_PURPOSE),
            ])]],
            next: 0,
        };
        let (tx, rx) = tokio::sync::oneshot::channel();
        let handle = tokio::spawn(run_dek_invalidation_listener(
            source,
            cache.clone(),
            INGEST_STREAM_PURPOSE,
            rx,
        ));
        tokio::time::sleep(Duration::from_millis(20)).await;
        let _ = tx.send(());
        handle.await.unwrap();

        cache.get_dek(42).await.unwrap();
        assert_eq!(
            calls.load(Ordering::SeqCst),
            2,
            "invalidation must force a re-resolve"
        );
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
