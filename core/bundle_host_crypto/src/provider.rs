//! Tenant DEK resolution: the [`DekProvider`] trait, the TTL cache, the
//! hub-api broker client, the alpha/local dev provider, and the Valkey
//! invalidation listener. Lifted from `core/svc_ingest/src/identity_crypto.rs`
//! with `purpose` made a parameter and every `expect` on a mutex removed.
//!
//! Handling rules for DEKs: in-memory only, TTL cache, zeroized on drop
//! ([`Dek`] is `Zeroizing`), never logged, serialized, or put in telemetry,
//! never written to disk or Valkey. No type here derives `Debug` over a key.

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::{Duration, Instant};

use aes_gcm::aead::{Aead, KeyInit};
use aes_gcm::{Aes256Gcm, Nonce};
use hkdf::Hkdf;
use sha2::Sha256;
use zeroize::Zeroizing;

use crate::envelope::KEY_LEN;
pub use crate::error::DekUnavailableError;
use crate::metrics;

const NONCE_LEN: usize = 12;

/// A 32-byte tenant DEK that zeroizes on drop. Never `Debug`-print one.
pub type Dek = Zeroizing<[u8; KEY_LEN]>;

/// Cache ceiling: no DEK is cached longer than this, whatever the broker says.
pub const DEFAULT_DEK_CACHE_TTL: Duration = Duration::from_secs(600);

/// The purpose label the data plane requests for bundle at-rest encryption
/// (`TenantDekPurpose::AtRest`); also the seal-context purpose label.
pub const BUNDLE_DATA_PURPOSE: &str = "at-rest";

/// Valkey stream carrying DEK rotation/revocation signals.
pub const DEK_INVALIDATION_STREAM: &str = "keys:tenant-dek:invalidate";

/// A boxed future, so dyn-safe wrappers can hide the async-fn-in-trait.
pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    // A poisoned lock only means another thread panicked mid-update of a
    // cache map; the map itself is still structurally valid.
    m.lock().unwrap_or_else(PoisonError::into_inner)
}

/// Resolves a tenant's active, or a specific, DEK.
#[allow(async_fn_in_trait)]
pub trait DekProvider: Send + Sync {
    /// The tenant's active DEK and its version.
    fn get_dek(
        &self,
        tenant_id: &str,
    ) -> impl Future<Output = Result<(Dek, u32), DekUnavailableError>> + Send;

    /// A specific (possibly retired) DEK version, for rotation-window reads.
    /// The default accepts only the active version.
    fn get_dek_version(
        &self,
        tenant_id: &str,
        version: u32,
    ) -> impl Future<Output = Result<Dek, DekUnavailableError>> + Send {
        async move {
            let (dek, active) = self.get_dek(tenant_id).await?;
            if active == version {
                Ok(dek)
            } else {
                Err(DekUnavailableError {
                    tenant_id: tenant_id.to_string(),
                    dek_version: Some(version),
                    reason: format!("version {version} requested, active is {active}"),
                })
            }
        }
    }

    /// Cache lifetime the broker granted for the last DEK returned for
    /// `tenant_id`; `None` means callers use their own TTL.
    fn granted_ttl(&self, _tenant_id: &str) -> Option<Duration> {
        None
    }
}

struct CacheEntry {
    dek: Dek,
    dek_version: u32,
    expires_at: Instant,
}

type CacheKey = (String, Option<u32>);

/// Wraps a [`DekProvider`] with a per-tenant TTL cache so steady-state
/// traffic does not call the broker per operation. Expired or unseen
/// entries always re-resolve through `inner`.
pub struct TtlCachedDekProvider<D: DekProvider> {
    inner: Arc<D>,
    ttl: Duration,
    cache: Arc<Mutex<CacheState>>,
}

/// Cache entries plus a per-tenant invalidation generation. A fetch records
/// the generation before calling the broker and only stores its result if the
/// generation is unchanged, so a revoke/rotate that lands mid-fetch is never
/// overwritten by the (now stale) DEK the in-flight fetch returns.
#[derive(Default)]
struct CacheState {
    entries: HashMap<CacheKey, CacheEntry>,
    generations: HashMap<String, u64>,
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
    /// New cache over `inner` with ceiling `ttl`.
    pub fn new(inner: D, ttl: Duration) -> Self {
        Self {
            inner: Arc::new(inner),
            ttl,
            cache: Arc::new(Mutex::new(CacheState::default())),
        }
    }

    /// Evicts every cached entry (active and versioned) for `tenant_id` and
    /// bumps its generation so any fetch already in flight will not cache
    /// the DEK it is about to return.
    pub fn invalidate(&self, tenant_id: &str) {
        let mut state = lock(&self.cache);
        state.entries.retain(|(t, _), _| t != tenant_id);
        let generation = state.generations.entry(tenant_id.to_string()).or_insert(0);
        *generation = generation.wrapping_add(1);
    }

    fn generation(&self, tenant_id: &str) -> u64 {
        lock(&self.cache)
            .generations
            .get(tenant_id)
            .copied()
            .unwrap_or(0)
    }

    fn lookup(&self, key: &CacheKey) -> Option<(Dek, u32)> {
        let state = lock(&self.cache);
        state
            .entries
            .get(key)
            .filter(|e| e.expires_at > Instant::now())
            .map(|e| (e.dek.clone(), e.dek_version))
    }

    /// Stores a fetched DEK only if no invalidation for the tenant landed
    /// since `observed_generation` was read; otherwise the DEK is returned
    /// to the caller uncached and the next call re-fetches.
    fn store(
        &self,
        key: CacheKey,
        dek: &Dek,
        dek_version: u32,
        tenant_id: &str,
        observed_generation: u64,
    ) {
        let ttl = self
            .inner
            .granted_ttl(tenant_id)
            .map_or(self.ttl, |g| g.min(self.ttl));
        let mut state = lock(&self.cache);
        let current = state.generations.get(tenant_id).copied().unwrap_or(0);
        if current != observed_generation {
            tracing::info!("dek fetch raced an invalidation; not caching the fetched DEK");
            return;
        }
        state.entries.insert(
            key,
            CacheEntry {
                dek: dek.clone(),
                dek_version,
                expires_at: Instant::now() + ttl,
            },
        );
    }
}

impl<D: DekProvider> DekProvider for TtlCachedDekProvider<D> {
    async fn get_dek(&self, tenant_id: &str) -> Result<(Dek, u32), DekUnavailableError> {
        let key = (tenant_id.to_string(), None);
        if let Some(hit) = self.lookup(&key) {
            metrics::record_dek_cache_hit();
            return Ok(hit);
        }
        let observed = self.generation(tenant_id);
        let (dek, version) = self.inner.get_dek(tenant_id).await?;
        self.store(key, &dek, version, tenant_id, observed);
        Ok((dek, version))
    }

    async fn get_dek_version(
        &self,
        tenant_id: &str,
        version: u32,
    ) -> Result<Dek, DekUnavailableError> {
        if let Some((dek, active)) = self.lookup(&(tenant_id.to_string(), None)) {
            if active == version {
                metrics::record_dek_cache_hit();
                return Ok(dek);
            }
        }
        let key = (tenant_id.to_string(), Some(version));
        if let Some((dek, _)) = self.lookup(&key) {
            metrics::record_dek_cache_hit();
            return Ok(dek);
        }
        // Unknown version: exactly one forced broker call, then fail closed.
        let observed = self.generation(tenant_id);
        let dek = self.inner.get_dek_version(tenant_id, version).await?;
        self.store(key, &dek, version, tenant_id, observed);
        Ok(dek)
    }
}

/// One sealed-DEK reply from the broker, transport-agnostic: `enc` is the
/// server's one-time X25519 public key, `ciphertext` the AEAD-sealed DEK.
pub struct SealedDek {
    /// Server's one-time X25519 public key for this response.
    pub enc: [u8; 32],
    /// AEAD-sealed DEK bytes (HPKE-lite, see [`hpke_seal_open`]).
    pub ciphertext: Vec<u8>,
    /// DEK version the broker sealed.
    pub dek_version: u32,
    /// Server-asserted cache ceiling; clamped to [`DEFAULT_DEK_CACHE_TTL`].
    pub ttl_seconds: u64,
}

/// The tenant-DEK broker transport. Production is gRPC (hub-api's internal
/// key service over a long-lived TLS channel with service-JWT metadata,
/// implemented in svc_process/svc_action over `core/hub_client`); the crate
/// itself carries no transport so the wire contract can evolve without
/// touching the crypto. Boxed future so it is dyn-safe.
pub trait DekBroker: Send + Sync {
    /// Requests `tenant_id`'s DEK for `purpose`, sealed to
    /// `client_public_key`. `version` 0 means the active version. Errors are
    /// reason strings that never contain key material.
    fn fetch_sealed<'a>(
        &'a self,
        tenant_id: &'a str,
        purpose: &'a str,
        version: u32,
        client_public_key: [u8; 32],
    ) -> BoxFuture<'a, Result<SealedDek, String>>;
}

/// Resolves DEKs through a [`DekBroker`].
///
/// A fresh X25519 keypair is generated per call; the response seals the DEK
/// to that ephemeral key so the bytes are meaningless to anything but this
/// caller. `ttl_seconds` is clamped to [`DEFAULT_DEK_CACHE_TTL`]. Fails
/// closed on every error.
pub struct HubApiDekProvider<B: DekBroker> {
    broker: B,
    service_id: String,
    purpose: String,
    granted_ttls: Mutex<HashMap<String, Duration>>,
}

impl<B: DekBroker> HubApiDekProvider<B> {
    /// `service_id` is this process's label in the bound context
    /// (`"svc-process"`/`"svc-action"`); `purpose` is normally
    /// [`BUNDLE_DATA_PURPOSE`].
    pub fn new(broker: B, service_id: impl Into<String>, purpose: impl Into<String>) -> Self {
        Self {
            broker,
            service_id: service_id.into(),
            purpose: purpose.into(),
            granted_ttls: Mutex::new(HashMap::new()),
        }
    }

    async fn fetch(
        &self,
        tenant_id: &str,
        version: Option<u32>,
    ) -> Result<(Dek, u32), DekUnavailableError> {
        let result = self.fetch_inner(tenant_id, version).await;
        metrics::record_dek_fetch(if result.is_ok() { "ok" } else { "error" });
        result
    }

    async fn fetch_inner(
        &self,
        tenant_id: &str,
        version: Option<u32>,
    ) -> Result<(Dek, u32), DekUnavailableError> {
        let unavailable = |reason: String| DekUnavailableError {
            tenant_id: tenant_id.to_string(),
            dek_version: version,
            reason,
        };
        let client_secret = x25519_dalek::EphemeralSecret::random();
        let client_public = x25519_dalek::PublicKey::from(&client_secret);

        let sealed = self
            .broker
            .fetch_sealed(
                tenant_id,
                &self.purpose,
                version.unwrap_or(0),
                *client_public.as_bytes(),
            )
            .await
            .map_err(&unavailable)?;
        if let Some(want) = version {
            if sealed.dek_version != want {
                return Err(unavailable(format!(
                    "broker returned version {} for requested {want}",
                    sealed.dek_version
                )));
            }
        }

        // The trailing `1` is the fixed seal-protocol version (hub-api
        // `key.proto`), not the DEK version.
        let context = format!("{}|{}|{}|1", self.service_id, tenant_id, self.purpose);
        let dek = hpke_seal_open(
            client_secret,
            &sealed.enc,
            &sealed.ciphertext,
            context.as_bytes(),
        )
        .map_err(&unavailable)?;

        let ttl = Duration::from_secs(sealed.ttl_seconds).min(DEFAULT_DEK_CACHE_TTL);
        lock(&self.granted_ttls).insert(tenant_id.to_string(), ttl);
        Ok((dek, sealed.dek_version))
    }
}

impl<B: DekBroker> DekProvider for HubApiDekProvider<B> {
    fn granted_ttl(&self, tenant_id: &str) -> Option<Duration> {
        lock(&self.granted_ttls).get(tenant_id).copied()
    }

    async fn get_dek(&self, tenant_id: &str) -> Result<(Dek, u32), DekUnavailableError> {
        self.fetch(tenant_id, None).await
    }

    async fn get_dek_version(
        &self,
        tenant_id: &str,
        version: u32,
    ) -> Result<Dek, DekUnavailableError> {
        self.fetch(tenant_id, Some(version)).await.map(|(d, _)| d)
    }
}

/// HPKE-lite open: X25519 ECDH with the server's one-time key, then
/// [`open_with_shared`]. Takes the ephemeral secret by value so it can never
/// be reused; the `x25519-dalek` secret and shared-secret types zeroize on
/// drop.
fn hpke_seal_open(
    client_secret: x25519_dalek::EphemeralSecret,
    enc: &[u8; 32],
    ciphertext: &[u8],
    context: &[u8],
) -> Result<Dek, String> {
    let shared_secret = client_secret.diffie_hellman(&x25519_dalek::PublicKey::from(*enc));
    open_with_shared(shared_secret.as_bytes(), ciphertext, context)
}

/// `key||nonce` (44 bytes) = HKDF-SHA256(salt none, ikm `shared`, info
/// `context`); DEK = AES-256-GCM-open(key, nonce, aad = `context`,
/// `ciphertext`). Exactly the construction in hub-api's `key.proto`; pure so
/// the cross-language vector (`tests/fixtures/hpke_vector.json`) can pin it.
fn open_with_shared(shared: &[u8; 32], ciphertext: &[u8], context: &[u8]) -> Result<Dek, String> {
    let hk = Hkdf::<Sha256>::new(None, shared);
    let mut key_and_nonce = Zeroizing::new([0u8; KEY_LEN + NONCE_LEN]);
    hk.expand(context, key_and_nonce.as_mut())
        .map_err(|e| format!("HKDF expand failed: {e}"))?;
    let (key_bytes, nonce_bytes) = key_and_nonce.split_at(KEY_LEN);

    let cipher = Aes256Gcm::new_from_slice(key_bytes).map_err(|_| "bad derived key".to_string())?;
    let plaintext = Zeroizing::new(
        cipher
            .decrypt(
                Nonce::from_slice(nonce_bytes),
                aes_gcm::aead::Payload {
                    msg: ciphertext,
                    aad: context,
                },
            )
            .map_err(|_| "HPKE-lite unseal failed (AEAD authentication error)".to_string())?,
    );
    if plaintext.len() != KEY_LEN {
        return Err(format!(
            "unsealed DEK is {} bytes, expected {KEY_LEN}",
            plaintext.len()
        ));
    }
    let mut out: Dek = Zeroizing::new([0u8; KEY_LEN]);
    out.copy_from_slice(&plaintext);
    Ok(out)
}

/// Environments [`LocalDevDekProvider`] may run in.
const LOCAL_DEV_ALLOWED_ENVIRONMENTS: &[&str] = &["alpha", "local"];

/// Dev/alpha-only provider: HKDF-derives a per-tenant DEK from a local KEK.
/// Hard-gated to `alpha`/`local` at construction; never the production
/// mechanism and never a plaintext stand-in (it performs real AES-GCM).
pub struct LocalDevDekProvider {
    kek: Zeroizing<[u8; KEY_LEN]>,
}

impl Clone for LocalDevDekProvider {
    fn clone(&self) -> Self {
        Self {
            kek: self.kek.clone(),
        }
    }
}

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
    /// Environment is not `alpha`/`local`.
    #[error("LocalDevDekProvider is refused outside alpha/local (WADDLES_ENV={0:?})")]
    EnvironmentNotAllowed(String),
    /// KEK env var unset.
    #[error("{0} is not set -- LocalDevDekProvider requires an explicit dev KEK")]
    KekNotSet(String),
    /// KEK env var shorter than 32 bytes.
    #[error("{0} must resolve to >= 32 bytes of KEK material")]
    KekTooShort(String),
}

impl LocalDevDekProvider {
    /// Resolves the dev KEK from `kek_env_var`, gated by `env_name`.
    pub fn new(env_name: &str, kek_env_var: &str) -> Result<Self, LocalDevDekProviderError> {
        if !LOCAL_DEV_ALLOWED_ENVIRONMENTS.contains(&env_name.to_ascii_lowercase().as_str()) {
            return Err(LocalDevDekProviderError::EnvironmentNotAllowed(
                env_name.to_string(),
            ));
        }
        let raw = std::env::var(kek_env_var)
            .map_err(|_| LocalDevDekProviderError::KekNotSet(kek_env_var.to_string()))?;
        Self::from_kek_bytes(raw.as_bytes(), kek_env_var)
    }

    fn from_kek_bytes(raw: &[u8], label: &str) -> Result<Self, LocalDevDekProviderError> {
        if raw.len() < KEY_LEN {
            return Err(LocalDevDekProviderError::KekTooShort(label.to_string()));
        }
        let mut kek = [0u8; KEY_LEN];
        kek.copy_from_slice(&raw[..KEY_LEN]);
        Ok(Self {
            kek: Zeroizing::new(kek),
        })
    }

    fn derive(&self, tenant_id: &str, version: u32) -> Result<Dek, DekUnavailableError> {
        let hk = Hkdf::<Sha256>::new(None, self.kek.as_ref());
        let mut dek = [0u8; KEY_LEN];
        let info = format!("waddles-bundle-dev-dek:{tenant_id}:{version}");
        hk.expand(info.as_bytes(), &mut dek)
            .map_err(|e| DekUnavailableError {
                tenant_id: tenant_id.to_string(),
                dek_version: Some(version),
                reason: format!("HKDF expand failed: {e}"),
            })?;
        Ok(Zeroizing::new(dek))
    }
}

impl DekProvider for LocalDevDekProvider {
    async fn get_dek(&self, tenant_id: &str) -> Result<(Dek, u32), DekUnavailableError> {
        Ok((self.derive(tenant_id, 1)?, 1))
    }
}

/// Runtime-selected provider: production broker or the dev fallback. An
/// enum, not `dyn`, because async-fn-in-trait is not object-safe.
#[derive(Clone)]
pub enum ConfiguredDekProvider<B: DekBroker + 'static> {
    /// Broker client behind the TTL cache (production).
    HubApi(TtlCachedDekProvider<HubApiDekProvider<B>>),
    /// Alpha/local derived-DEK provider behind the TTL cache.
    LocalDev(TtlCachedDekProvider<LocalDevDekProvider>),
}

impl<B: DekBroker + 'static> ConfiguredDekProvider<B> {
    /// Evicts the cached DEK(s) for `tenant_id` (rotation/revocation signal).
    pub fn invalidate(&self, tenant_id: &str) {
        match self {
            Self::HubApi(p) => p.invalidate(tenant_id),
            Self::LocalDev(p) => p.invalidate(tenant_id),
        }
    }
}

impl<B: DekBroker + 'static> DekProvider for ConfiguredDekProvider<B> {
    async fn get_dek(&self, tenant_id: &str) -> Result<(Dek, u32), DekUnavailableError> {
        match self {
            Self::HubApi(p) => p.get_dek(tenant_id).await,
            Self::LocalDev(p) => p.get_dek(tenant_id).await,
        }
    }

    async fn get_dek_version(
        &self,
        tenant_id: &str,
        version: u32,
    ) -> Result<Dek, DekUnavailableError> {
        match self {
            Self::HubApi(p) => p.get_dek_version(tenant_id, version).await,
            Self::LocalDev(p) => p.get_dek_version(tenant_id, version).await,
        }
    }
}

/// Polls [`DEK_INVALIDATION_STREAM`]; abstracted so the dispatch logic is
/// testable without Valkey.
#[allow(async_fn_in_trait)]
pub trait InvalidationSource: Send {
    /// Blocks (bounded) for entries newer than `last_id`; returns
    /// `(newest_id, entries)`, empty on a quiet read timeout.
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

/// Anything whose cached DEKs can be evicted per tenant.
pub trait Invalidate: Send + Sync {
    /// Drop cached DEKs for `tenant_id`.
    fn invalidate_tenant(&self, tenant_id: &str);
}

impl<D: DekProvider> Invalidate for TtlCachedDekProvider<D> {
    fn invalidate_tenant(&self, tenant_id: &str) {
        self.invalidate(tenant_id);
    }
}

impl<B: DekBroker + 'static> Invalidate for ConfiguredDekProvider<B> {
    fn invalidate_tenant(&self, tenant_id: &str) {
        self.invalidate(tenant_id);
    }
}

/// Consumes [`DEK_INVALIDATION_STREAM`] until `shutdown` resolves, evicting
/// `cache` for every entry whose `purpose` equals `expected_purpose`. A read
/// error is logged and retried after a short backoff, never fatal: the TTL
/// bounds how long a stale DEK can outlive a missed signal.
pub async fn run_dek_invalidation_listener<C: Invalidate, S: InvalidationSource>(
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
                Ok((new_last_id, entries)) => {
                    last_id = new_last_id;
                    for fields in entries {
                        if fields.get("purpose").map(String::as_str) != Some(expected_purpose) {
                            continue;
                        }
                        match fields.get("tenant") {
                            Some(tenant) => cache.invalidate_tenant(tenant),
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
    use std::sync::atomic::{AtomicU32, Ordering};

    struct CountingProvider {
        calls: AtomicU32,
        version: u32,
    }

    impl DekProvider for CountingProvider {
        async fn get_dek(&self, tenant_id: &str) -> Result<(Dek, u32), DekUnavailableError> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            if tenant_id == "denied" {
                return Err(DekUnavailableError {
                    tenant_id: tenant_id.to_string(),
                    dek_version: None,
                    reason: "test denial".to_string(),
                });
            }
            Ok((Zeroizing::new([7u8; KEY_LEN]), self.version))
        }
    }

    #[tokio::test]
    async fn cache_serves_second_call_without_inner() {
        let c = TtlCachedDekProvider::new(
            CountingProvider {
                calls: AtomicU32::new(0),
                version: 1,
            },
            Duration::from_secs(60),
        );
        c.get_dek("1")
            .await
            .map(|_| ())
            .unwrap_or_else(|e| panic!("{e}"));
        c.get_dek("1")
            .await
            .map(|_| ())
            .unwrap_or_else(|e| panic!("{e}"));
        assert_eq!(c.inner.calls.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn invalidate_forces_refetch() {
        let c = TtlCachedDekProvider::new(
            CountingProvider {
                calls: AtomicU32::new(0),
                version: 1,
            },
            Duration::from_secs(60),
        );
        let _ = c.get_dek("1").await;
        c.invalidate("1");
        let _ = c.get_dek("1").await;
        assert_eq!(c.inner.calls.load(Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn unavailable_is_not_cached_and_propagates() {
        let c = TtlCachedDekProvider::new(
            CountingProvider {
                calls: AtomicU32::new(0),
                version: 1,
            },
            Duration::from_secs(60),
        );
        assert!(c.get_dek("denied").await.is_err());
        assert!(c.get_dek("denied").await.is_err());
        assert_eq!(c.inner.calls.load(Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn unknown_version_fails_closed() {
        let c = TtlCachedDekProvider::new(
            CountingProvider {
                calls: AtomicU32::new(0),
                version: 1,
            },
            Duration::from_secs(60),
        );
        assert!(c.get_dek_version("1", 1).await.is_ok());
        assert!(c.get_dek_version("1", 9).await.is_err());
    }

    /// Provider whose fetch parks mid-flight until released, so a test can
    /// land an invalidation while `get_dek` is in progress.
    struct GatedProvider {
        calls: AtomicU32,
        started: tokio::sync::Notify,
        release: tokio::sync::Notify,
    }

    impl DekProvider for GatedProvider {
        async fn get_dek(&self, _tenant_id: &str) -> Result<(Dek, u32), DekUnavailableError> {
            let n = self.calls.fetch_add(1, Ordering::SeqCst);
            if n == 0 {
                self.started.notify_one();
                self.release.notified().await;
            }
            Ok((Zeroizing::new([7u8; KEY_LEN]), 1))
        }
    }

    // regression: revocation landing during an in-flight fetch must not be
    // overwritten by the stale DEK that fetch then returns.
    #[tokio::test]
    async fn invalidation_during_inflight_fetch_is_not_overwritten() {
        let cache = TtlCachedDekProvider::new(
            GatedProvider {
                calls: AtomicU32::new(0),
                started: tokio::sync::Notify::new(),
                release: tokio::sync::Notify::new(),
            },
            Duration::from_secs(60),
        );
        let inflight = {
            let cache = cache.clone();
            tokio::spawn(async move { cache.get_dek("1").await.map(|(_, v)| v) })
        };
        cache.inner.started.notified().await;
        cache.invalidate("1");
        cache.inner.release.notify_one();
        assert_eq!(inflight.await.ok().and_then(Result::ok), Some(1));
        // The racing fetch's DEK was returned but not cached: next call
        // re-fetches instead of serving the stale entry.
        let _ = cache.get_dek("1").await;
        assert_eq!(cache.inner.calls.load(Ordering::SeqCst), 2);
        // And an uncontended fetch still caches normally.
        let _ = cache.get_dek("1").await;
        assert_eq!(cache.inner.calls.load(Ordering::SeqCst), 2);
    }

    #[test]
    fn hpke_open_matches_python_reference_vector() {
        let v: serde_json::Value =
            serde_json::from_str(include_str!("../tests/fixtures/hpke_vector.json"))
                .unwrap_or_else(|e| panic!("fixture: {e}"));
        let hex = |k: &str| -> Vec<u8> {
            let s = v[k].as_str().unwrap_or_default();
            (0..s.len())
                .step_by(2)
                .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap_or(0))
                .collect()
        };
        let shared: [u8; 32] = hex("shared_hex")
            .try_into()
            .unwrap_or_else(|_| panic!("32-byte shared"));
        let context = v["context"]
            .as_str()
            .unwrap_or_default()
            .as_bytes()
            .to_vec();
        let dek = open_with_shared(&shared, &hex("ciphertext_hex"), &context)
            .unwrap_or_else(|e| panic!("{e}"));
        assert_eq!(&dek[..], &hex("dek_hex")[..]);
        // Flipped ciphertext byte, or a different context, must not open.
        let mut bad = hex("ciphertext_hex");
        bad[0] ^= 1;
        assert!(open_with_shared(&shared, &bad, &context).is_err());
        assert!(open_with_shared(&shared, &hex("ciphertext_hex"), b"svc-x|1|at-rest|1").is_err());
    }

    #[test]
    fn local_dev_refuses_outside_alpha_local() {
        let err = LocalDevDekProvider::new("prod", "WHATEVER").err();
        assert_eq!(
            err,
            Some(LocalDevDekProviderError::EnvironmentNotAllowed(
                "prod".to_string()
            ))
        );
    }

    #[test]
    fn local_dev_rejects_short_kek_and_redacts_debug() {
        assert!(LocalDevDekProvider::from_kek_bytes(b"short", "K").is_err());
        let p =
            LocalDevDekProvider::from_kek_bytes(&[b'k'; 32], "K").unwrap_or_else(|e| panic!("{e}"));
        let dbg = format!("{p:?}");
        assert!(dbg.contains("redacted") && !dbg.contains("107"));
    }

    struct Source(Vec<HashMap<String, String>>);

    impl InvalidationSource for Source {
        async fn read_after(
            &mut self,
            last_id: &str,
        ) -> Result<(String, Vec<HashMap<String, String>>), String> {
            if self.0.is_empty() {
                tokio::time::sleep(Duration::from_millis(5)).await;
                return Ok((last_id.to_string(), vec![]));
            }
            Ok(("1-0".to_string(), std::mem::take(&mut self.0)))
        }
    }

    struct Recorder(Arc<Mutex<Vec<String>>>);
    impl Invalidate for Recorder {
        fn invalidate_tenant(&self, t: &str) {
            lock(&self.0).push(t.to_string());
        }
    }

    #[tokio::test]
    async fn listener_invalidates_only_matching_purpose() {
        let mk = |t: &str, p: &str| {
            HashMap::from([
                ("tenant".to_string(), t.to_string()),
                ("purpose".to_string(), p.to_string()),
            ])
        };
        let seen = Arc::new(Mutex::new(Vec::new()));
        let (tx, rx) = tokio::sync::oneshot::channel();
        let task = tokio::spawn(run_dek_invalidation_listener(
            Source(vec![mk("5", BUNDLE_DATA_PURPOSE), mk("6", "ingest-stream")]),
            Recorder(seen.clone()),
            BUNDLE_DATA_PURPOSE,
            rx,
        ));
        tokio::time::sleep(Duration::from_millis(50)).await;
        let _ = tx.send(());
        let _ = task.await;
        assert_eq!(*lock(&seen), vec!["5".to_string()]);
    }
}
