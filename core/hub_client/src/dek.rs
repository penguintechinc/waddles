//! Tenant stream-DEK delivery for svc-ingest (encrypt) and svc-process (decrypt):
//! the broker-backed provider over `KeyService.GetStreamDek`, its TTL cache, and the
//! dev/alpha-only local fallback.
//!
//! # Wire contract (`sealed_dek`)
//!
//! Authoritative: hub-api's `KeyService.GetStreamDek` (`hub_api/services/stream_dek_broker.py`
//! `seal_context`/`seal_dek`). `proto/waddles/hub/internal/v1/key.proto` leaves the bytes
//! unspecified; the layout below is the contract, pinned by a cross-language vector
//! (`tests/fixtures/stream_dek_python_vector.json`, generated with the Python seal code).
//!
//! - Request: `tenant_id` = the **decimal numeric** tenant id (a slug is `INVALID_ARGUMENT`),
//!   `purpose` = [`INGEST_STREAM_PURPOSE`], `version` 0 = the active key (N = exactly that
//!   version, active or retired -- used to decrypt an envelope written before a rotation),
//!   `caller_ephemeral_public_key` = 32 raw X25519 bytes, **fresh per call**.
//! - Auth: the bearer machine JWT [`HubClient`] mints through `service_auth::MachineJwtClient`
//!   (`ServiceAuthService.MintServiceToken`, projected ServiceAccount token), scope
//!   [`STREAM_DEK_SCOPE`], granted only to svc-ingest and svc-process; hub-api also checks the
//!   caller's SPIFFE service name.
//! - `sealed_dek` = `server_ephemeral_x25519_pub (32 B) || AES-256-GCM(dek 32 B) + tag 16 B`
//!   = 80 bytes. `shared = X25519(caller_eph_priv, enc)`;
//!   `okm = HKDF-SHA256(salt = none, ikm = shared, info = ctx, L = 44)`; `key = okm[..32]`,
//!   `nonce = okm[32..44]`; the AAD is `ctx` too, where
//!   `ctx = "<svc>|<tenant_id>|ingest-stream|<version>"`, `<svc>` is the caller's own service
//!   name (`svc-ingest` / `svc-process`, the last segment of its SPIFFE id) and `<version>` the
//!   **response's** concrete version (>= 1, never 0).
//! - `ttl_seconds` (default 600, hub-api clamps 60..3600) is how long the unsealed key may be
//!   cached; the client clamps it to [`BROKER_TTL_FLOOR`]..=[`BROKER_TTL_CEILING`], so a
//!   misbehaving broker can neither disable the cache nor pin a key forever.
//! - The key is stable per `(tenant, version)` and identical for ingest and process.
//!
//! Every failure is fail-closed ([`DekUnavailableError`]): no plaintext fallback, no static
//! key, no logged token/DEK/plaintext.

use std::collections::HashMap;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::time::Duration;

use tokio::time::Instant;
use tracing::debug;

use aes_gcm::aead::{Aead, KeyInit, Payload};
use aes_gcm::{Aes256Gcm, Nonce};
use hkdf::Hkdf;
use sha2::Sha256;
use zeroize::Zeroizing;

use crate::HubClient;

/// AES-256 key length in bytes.
pub const DEK_LEN: usize = 32;
const NONCE_LEN: usize = 12;
const TAG_LEN: usize = 16;
const X25519_LEN: usize = 32;
/// Exact `sealed_dek` length: server ephemeral public key + DEK ciphertext + GCM tag.
const SEALED_DEK_LEN: usize = X25519_LEN + DEK_LEN + TAG_LEN;

/// The only key purpose the stream-field envelope encryption uses.
pub const INGEST_STREAM_PURPOSE: &str = "ingest-stream";

/// hub-api scope the `KeyService.GetStreamDek` RPC requires on the machine JWT.
pub const STREAM_DEK_SCOPE: &str = "key:stream-dek:read";

/// Default cache TTL (10 minutes, hub-api's default grant) of the dev/alpha local provider.
pub const DEFAULT_DEK_CACHE_TTL: Duration = Duration::from_secs(600);

/// Client-side ceiling on the cache lifetime a broker response may grant (hub-api's own max).
pub const BROKER_TTL_CEILING: Duration = Duration::from_secs(3600);

/// Client-side floor: a zero/tiny grant must not turn every event into a broker round trip.
pub const BROKER_TTL_FLOOR: Duration = Duration::from_secs(60);

/// A 32-byte AES-256 DEK that zeroizes on drop -- never `Debug`-printed, never logged.
pub type Dek = Zeroizing<[u8; DEK_LEN]>;

/// A tenant's DEK could not be resolved -- the caller MUST fail closed (never fall back to
/// plaintext, and never treat a still-encrypted value as plaintext).
#[derive(Debug, thiserror::Error, Clone)]
#[error("tenant DEK unavailable for tenant_id={tenant_id} dek_version={dek_version:?}: {reason}")]
pub struct DekUnavailableError {
    /// The numeric tenant id the DEK was requested for (non-PII).
    pub tenant_id: i64,
    /// The pinned version, `None` when the current version was requested.
    pub dek_version: Option<u32>,
    /// Why (never contains token or key material).
    pub reason: String,
}

impl DekUnavailableError {
    fn new(tenant_id: i64, dek_version: Option<u32>, reason: impl Into<String>) -> Self {
        Self {
            tenant_id,
            dek_version,
            reason: reason.into(),
        }
    }
}

/// Resolves a tenant's DEK by numeric tenant id.
///
/// `get_dek` returns the **current** active key and its concrete version (the encrypt path);
/// `get_dek_version` returns one **specific** version (the decrypt path, driven by the version
/// in the envelope header so a rotation never strands an in-flight event). Callers are generic
/// over this trait (async-fn-in-trait, not `dyn`) or go through [`ConfiguredDekProvider`].
pub trait DekProvider: Send + Sync {
    /// The current active DEK and its concrete version (>= 1).
    fn get_dek(
        &self,
        tenant_id: i64,
    ) -> impl std::future::Future<Output = Result<(Dek, u32), DekUnavailableError>> + Send;

    /// The DEK of exactly `version`. The default only serves the current version, which is
    /// correct for single-version providers and test doubles.
    fn get_dek_version(
        &self,
        tenant_id: i64,
        version: u32,
    ) -> impl std::future::Future<Output = Result<Dek, DekUnavailableError>> + Send {
        async move {
            let (dek, current) = self.get_dek(tenant_id).await?;
            if current == version {
                Ok(dek)
            } else {
                Err(DekUnavailableError::new(
                    tenant_id,
                    Some(version),
                    format!("provider only serves version {current}"),
                ))
            }
        }
    }
}

/// Something whose cached DEKs can be dropped on a rotation/revocation signal.
pub trait DekInvalidate: Send + Sync {
    /// Drops every cached version for `tenant_id`.
    fn invalidate(&self, tenant_id: i64);
}

/// One DEK as delivered by a [`DekBroker`], before caching.
pub struct BrokeredDek {
    /// The key material.
    pub dek: Dek,
    /// The concrete version it belongs to (>= 1).
    pub version: u32,
    /// Cache lifetime the broker granted (not yet clamped to the cache ceiling).
    pub ttl: Duration,
}

/// An uncached source of DEKs: the hub-api broker in production, the local KEK derivation in
/// dev/alpha. `version` 0 requests the current active version.
pub trait DekBroker: Send + Sync {
    /// Fetches `(tenant_id, version)`; the returned version is always concrete (>= 1) and,
    /// when `version != 0`, equals `version`.
    fn fetch(
        &self,
        tenant_id: i64,
        version: u32,
    ) -> impl std::future::Future<Output = Result<BrokeredDek, DekUnavailableError>> + Send;
}

/// [`DekBroker`] over hub-api's `KeyService.GetStreamDek`: a fresh X25519 ephemeral key per
/// call, the DEK opened per the module-level contract, every error fail-closed.
///
/// The wrapped [`HubClient`] must have been connected with scope [`STREAM_DEK_SCOPE`] (a
/// client is single-scope); its machine JWT is minted/cached/refreshed by
/// `service_auth::MachineJwtClient`, and its breaker + retry policy applies.
#[derive(Clone)]
pub struct HubDekBroker {
    client: Arc<HubClient>,
    /// This process's own service name (`svc-ingest` / `svc-process`): part of the seal context.
    service: String,
}

impl HubDekBroker {
    /// Wraps an already-connected [`HubClient`] holding the [`STREAM_DEK_SCOPE`] scope;
    /// `service` is the caller's own service name as hub-api derives it from its SPIFFE id.
    #[must_use]
    pub fn new(client: Arc<HubClient>, service: impl Into<String>) -> Self {
        Self {
            client,
            service: service.into(),
        }
    }
}

impl DekBroker for HubDekBroker {
    async fn fetch(
        &self,
        tenant_id: i64,
        version: u32,
    ) -> Result<BrokeredDek, DekUnavailableError> {
        let pinned = (version != 0).then_some(version);
        let unavailable = |reason: String| DekUnavailableError::new(tenant_id, pinned, reason);
        if tenant_id < 1 {
            return Err(unavailable(
                "no numeric tenant id (legacy string-scope envelope): refusing to request a key"
                    .to_string(),
            ));
        }

        let secret = x25519_dalek::EphemeralSecret::random();
        let public = x25519_dalek::PublicKey::from(&secret);
        let response = self
            .client
            .get_stream_dek(
                tenant_id.to_string(),
                INGEST_STREAM_PURPOSE.to_string(),
                version,
                public.as_bytes().to_vec(),
            )
            .await
            .map_err(|e| unavailable(format!("key broker call failed: {e}")))?;

        if response.version == 0 {
            return Err(unavailable(
                "broker returned unresolved version 0".to_string(),
            ));
        }
        if version != 0 && response.version != version {
            return Err(unavailable(format!(
                "broker returned version {} for pinned version {version}",
                response.version
            )));
        }
        let dek = open_sealed_dek(
            secret,
            &response.sealed_dek,
            &self.service,
            tenant_id,
            INGEST_STREAM_PURPOSE,
            response.version,
        )
        .map_err(&unavailable)?;
        Ok(BrokeredDek {
            dek,
            version: response.version,
            ttl: Duration::from_secs(u64::from(response.ttl_seconds))
                .clamp(BROKER_TTL_FLOOR, BROKER_TTL_CEILING),
        })
    }
}

/// The HKDF info / AEAD AAD (`ctx`) of one seal: binds holder, tenant, purpose and version.
fn seal_info(service: &str, tenant_id: i64, purpose: &str, version: u32) -> Vec<u8> {
    format!("{service}|{tenant_id}|{purpose}|{version}").into_bytes()
}

/// Derives the AES-256-GCM key + nonce of one seal from the ECDH shared secret.
fn derive_seal_key(shared: &[u8], info: &[u8]) -> Result<([u8; DEK_LEN], [u8; NONCE_LEN]), String> {
    let hk = Hkdf::<Sha256>::new(None, shared);
    let mut okm = Zeroizing::new([0u8; DEK_LEN + NONCE_LEN]);
    hk.expand(info, okm.as_mut())
        .map_err(|e| format!("HKDF expand failed: {e}"))?;
    let mut key = [0u8; DEK_LEN];
    let mut nonce = [0u8; NONCE_LEN];
    key.copy_from_slice(&okm[..DEK_LEN]);
    nonce.copy_from_slice(&okm[DEK_LEN..]);
    Ok((key, nonce))
}

/// Opens a `sealed_dek` with the caller's single-use ephemeral `secret` (consumed: an
/// ephemeral key can never be reused). Any length/shape/AEAD failure is an error string
/// carrying no key material.
fn open_sealed_dek(
    secret: x25519_dalek::EphemeralSecret,
    sealed: &[u8],
    service: &str,
    tenant_id: i64,
    purpose: &str,
    version: u32,
) -> Result<Dek, String> {
    check_sealed_len(sealed)?;
    let mut server_pub = [0u8; X25519_LEN];
    server_pub.copy_from_slice(&sealed[..X25519_LEN]);
    let shared = secret.diffie_hellman(&x25519_dalek::PublicKey::from(server_pub));
    if !shared.was_contributory() {
        return Err("sealed_dek server public key is a low-order point".to_string());
    }
    open_with_shared(
        shared.as_bytes(),
        sealed,
        service,
        tenant_id,
        purpose,
        version,
    )
}

fn check_sealed_len(sealed: &[u8]) -> Result<(), String> {
    if sealed.len() == SEALED_DEK_LEN {
        Ok(())
    } else {
        Err(format!(
            "sealed_dek is {} bytes, expected {SEALED_DEK_LEN}",
            sealed.len()
        ))
    }
}

/// The key-schedule + AEAD half of [`open_sealed_dek`], from an already-computed ECDH secret
/// (split out so the cross-language vector test can drive it with a fixed caller key).
fn open_with_shared(
    shared: &[u8],
    sealed: &[u8],
    service: &str,
    tenant_id: i64,
    purpose: &str,
    version: u32,
) -> Result<Dek, String> {
    check_sealed_len(sealed)?;
    let ciphertext = &sealed[X25519_LEN..];
    let info = seal_info(service, tenant_id, purpose, version);
    let (key, nonce) = derive_seal_key(shared, &info)?;
    let key = Zeroizing::new(key);
    let cipher = Aes256Gcm::new_from_slice(key.as_ref())
        .map_err(|_| "derived key has an invalid length".to_string())?;
    let plaintext = Zeroizing::new(
        cipher
            .decrypt(
                Nonce::from_slice(&nonce),
                Payload {
                    msg: ciphertext,
                    aad: &info,
                },
            )
            .map_err(|_| "sealed_dek authentication failed".to_string())?,
    );
    if plaintext.len() != DEK_LEN {
        return Err(format!(
            "unsealed DEK is {} bytes, expected {DEK_LEN}",
            plaintext.len()
        ));
    }
    let mut out = [0u8; DEK_LEN];
    out.copy_from_slice(&plaintext);
    Ok(Zeroizing::new(out))
}

/// Base of the per-key negative-cache backoff after a failed broker call (doubles per
/// consecutive failure of the same key up to [`NEGATIVE_BACKOFF_CAP`], jittered +-25%).
pub const NEGATIVE_BACKOFF_BASE: Duration = Duration::from_millis(250);

/// Ceiling of the negative-cache backoff: short on purpose -- fail-closed callers dead-letter
/// while it lasts, so it only needs to be long enough to stop a retry stampede.
pub const NEGATIVE_BACKOFF_CAP: Duration = Duration::from_secs(5);

/// How long an expired negative entry is remembered (for backoff escalation) before it is swept.
const NEGATIVE_FORGET_AFTER: Duration = Duration::from_secs(60);

/// Soft cap on cached `(tenant, version)` keys; past it the soonest-to-expire entry is evicted,
/// so the cache is bounded in both lifetime (TTL) and size.
pub const DEK_CACHE_MAX_ENTRIES: usize = 4096;

/// Counter `waddles_hub_dek_cache{result}` (exposed as `..._total`): every stream-DEK
/// resolution by outcome. `hit` = served from the cache, `miss` = this caller made the broker
/// call (the single-flight leader), `inflight_wait` = waited behind a leader and was served its
/// fresh key, `negative` = answered with a recent (own or shared) failure without a broker call.
const METRIC_DEK_CACHE: &str = "waddles_hub_dek_cache";

/// Records one cache outcome on the process-global meter (no-op until a provider is installed;
/// telemetry never affects a key decision). `result` is a closed vocabulary -- never a tenant,
/// version or key identifier.
fn record_cache_result(result: &'static str) {
    static COUNTER: std::sync::OnceLock<opentelemetry::metrics::Counter<u64>> =
        std::sync::OnceLock::new();
    COUNTER
        .get_or_init(|| {
            opentelemetry::global::meter("hub_client.dek")
                .u64_counter(METRIC_DEK_CACHE)
                .with_description(
                    "Stream-DEK resolutions by result: hit, miss (broker call made), \
                     inflight_wait (joined a single-flight leader), negative (recent failure).",
                )
                .build()
        })
        .add(1, &[opentelemetry::KeyValue::new("result", result)]);
}

struct CacheEntry {
    dek: Dek,
    expires_at: Instant,
}

/// A recent broker failure for one key, replayed to every caller inside its window.
struct NegativeEntry {
    until: Instant,
    /// Consecutive failures of this key (drives the exponential backoff).
    failures: u32,
    error: DekUnavailableError,
}

/// `(numeric tenant id, key version)`; version 0 is the "current key" lookup.
type FlightKey = (i64, u32);

#[derive(Default)]
struct CacheState {
    /// `(tenant_id, version)` -> key.
    by_version: HashMap<(i64, u32), CacheEntry>,
    /// `tenant_id` -> `(current version, expiry of that "current" answer)`. Kept separate from
    /// `by_version` so an old, still-cached version can never be mistaken for the current one.
    current: HashMap<i64, (u32, Instant)>,
    /// Recent failures per key, so a failed broker call does not trigger a retry stampede.
    negative: HashMap<FlightKey, NegativeEntry>,
    /// Per-tenant invalidation generation: a leader whose fetch raced an
    /// [`DekInvalidate::invalidate`] must not write its (possibly pre-rotation) result back.
    generations: HashMap<i64, u64>,
}

/// Wraps a [`DekBroker`] with a TTL cache keyed by `(numeric tenant id, key version)` --
/// never a slug -- plus a process-wide **single-flight** and a short **negative cache**.
///
/// - **Cache:** a steady-state loop calls the broker once per tenant per TTL window; the cache
///   lifetime is the broker's grant clamped to `ttl` (default [`DEFAULT_DEK_CACHE_TTL`]) and the
///   map is capped at [`DEK_CACHE_MAX_ENTRIES`]. A rotated key is picked up within one TTL, or
///   at once on [`DekInvalidate::invalidate`]; a *new* version is simply a different key.
/// - **Single-flight:** callers that miss the same `(tenant, version)` queue behind one broker
///   call (the leader); the rest re-check the cache it filled. A cold start with hundreds of
///   concurrent per-app consumers is therefore one broker call (and, upstream, one token
///   mint), not hundreds. The cache is per provider -- i.e. per calling service identity
///   (`svc-ingest` / `svc-process`, fixed in the broker's seal context) -- and the key carries
///   the tenant, so tenants never share an entry or a flight.
/// - **Negative cache:** a failed call is recorded for a short jittered window
///   ([`NEGATIVE_BACKOFF_BASE`], doubling per consecutive failure up to
///   [`NEGATIVE_BACKOFF_CAP`]) and replayed to every waiter and to callers arriving inside it:
///   they all fail closed from **one** broker call and retry together only after the backoff.
///   A failure is never cached beyond that window, and a success clears it.
///
/// Fail-closed is unchanged: every failure is a [`DekUnavailableError`], never a fallback key.
pub struct CachedDekProvider<B: DekBroker> {
    broker: Arc<B>,
    ttl: Duration,
    negative_base: Duration,
    state: Arc<Mutex<CacheState>>,
    /// Per-key single-flight gates (held across the broker `.await`, hence tokio mutexes; the
    /// map itself is only touched in short sync sections).
    flights: Arc<Mutex<HashMap<FlightKey, Arc<tokio::sync::Mutex<()>>>>>,
}

// Manual `Clone`: a derive would add a spurious `B: Clone` bound.
impl<B: DekBroker> Clone for CachedDekProvider<B> {
    fn clone(&self) -> Self {
        Self {
            broker: self.broker.clone(),
            ttl: self.ttl,
            negative_base: self.negative_base,
            state: self.state.clone(),
            flights: self.flights.clone(),
        }
    }
}

impl<B: DekBroker> CachedDekProvider<B> {
    /// Caches `broker` answers for at most `ttl` (also clamped by each response's own grant).
    pub fn new(broker: B, ttl: Duration) -> Self {
        Self {
            broker: Arc::new(broker),
            ttl,
            negative_base: NEGATIVE_BACKOFF_BASE,
            state: Arc::new(Mutex::new(CacheState::default())),
            flights: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    /// Replaces the negative-cache backoff base ([`NEGATIVE_BACKOFF_BASE`]); `Duration::ZERO`
    /// disables the negative cache (single-flight still coalesces concurrent callers).
    #[must_use]
    pub fn with_negative_backoff(mut self, base: Duration) -> Self {
        self.negative_base = base;
        self
    }

    /// Locks the cache, recovering a poisoned lock: the map holds only keys that were fully
    /// inserted, so there is no torn state to distrust.
    fn lock(&self) -> MutexGuard<'_, CacheState> {
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// The cached key for `(tenant, version)` (`version` 0 = the current key) with its
    /// concrete version, if still fresh.
    fn lookup(&self, tenant_id: i64, version: u32) -> Option<(Dek, u32)> {
        let state = self.lock();
        let now = Instant::now();
        if version == 0 {
            let (current, until) = state.current.get(&tenant_id)?;
            if *until <= now {
                return None;
            }
            let entry = state.by_version.get(&(tenant_id, *current))?;
            return (entry.expires_at > now).then(|| (entry.dek.clone(), *current));
        }
        state
            .by_version
            .get(&(tenant_id, version))
            .filter(|e| e.expires_at > now)
            .map(|e| (e.dek.clone(), version))
    }

    /// The still-live negative entry for `key`, replayed as its original error.
    fn negative(&self, key: FlightKey) -> Option<DekUnavailableError> {
        let state = self.lock();
        state
            .negative
            .get(&key)
            .filter(|n| Instant::now() < n.until)
            .map(|n| n.error.clone())
    }

    /// Records a failed broker call for `key`: exponential, jittered, capped backoff.
    fn record_failure(&self, key: FlightKey, error: &DekUnavailableError) {
        if self.negative_base.is_zero() {
            return;
        }
        let mut state = self.lock();
        let now = Instant::now();
        // Keep expired entries a while: their failure count drives the backoff escalation.
        state
            .negative
            .retain(|_, n| n.until + NEGATIVE_FORGET_AFTER > now);
        let failures = state
            .negative
            .get(&key)
            .map_or(0, |n| n.failures)
            .saturating_add(1);
        let backoff = self
            .negative_base
            .saturating_mul(1u32 << failures.saturating_sub(1).min(16))
            .min(NEGATIVE_BACKOFF_CAP);
        state.negative.insert(
            key,
            NegativeEntry {
                until: now + service_auth::jittered(backoff),
                failures,
                error: error.clone(),
            },
        );
    }

    /// The invalidation generation of `tenant_id` (0 = never invalidated).
    fn generation(&self, tenant_id: i64) -> u64 {
        self.lock()
            .generations
            .get(&tenant_id)
            .copied()
            .unwrap_or(0)
    }

    /// Caches `fetched` for `tenant_id` unless the tenant was invalidated since `generation`
    /// was read (the fetch raced a rotation signal: serve it, never cache it).
    fn store(&self, tenant_id: i64, generation: u64, fetched: &BrokeredDek, current: bool) {
        let ttl = fetched.ttl.min(self.ttl);
        if ttl.is_zero() {
            return;
        }
        let mut state = self.lock();
        if state.generations.get(&tenant_id).copied().unwrap_or(0) != generation {
            debug!(tenant_id, "dek.cache_store_skipped_invalidated_in_flight");
            return;
        }
        let now = Instant::now();
        let expires_at = now + ttl;
        state.by_version.retain(|_, e| e.expires_at > now);
        state.current.retain(|_, (_, until)| *until > now);
        state.by_version.insert(
            (tenant_id, fetched.version),
            CacheEntry {
                dek: fetched.dek.clone(),
                expires_at,
            },
        );
        if current {
            state
                .current
                .insert(tenant_id, (fetched.version, expires_at));
        }
        while state.by_version.len() > DEK_CACHE_MAX_ENTRIES {
            let Some(oldest) = state
                .by_version
                .iter()
                .min_by_key(|(_, e)| e.expires_at)
                .map(|(k, _)| *k)
            else {
                break;
            };
            state.by_version.remove(&oldest);
        }
    }

    /// The gate of `key`, created on first use.
    fn gate(&self, key: FlightKey) -> Arc<tokio::sync::Mutex<()>> {
        let mut flights = self.flights.lock().unwrap_or_else(PoisonError::into_inner);
        flights.entry(key).or_default().clone()
    }

    /// Drops `key`'s gate once nobody else holds it (only the map and `gate` remain).
    fn release_gate(&self, key: FlightKey, gate: Arc<tokio::sync::Mutex<()>>) {
        let mut flights = self.flights.lock().unwrap_or_else(PoisonError::into_inner);
        if Arc::strong_count(&gate) <= 2 {
            flights.remove(&key);
        }
    }

    /// Validates a broker answer against the request (`version` 0 = current).
    fn validate(
        tenant_id: i64,
        version: u32,
        fetched: &BrokeredDek,
    ) -> Result<(), DekUnavailableError> {
        let pinned = (version != 0).then_some(version);
        if fetched.version == 0 {
            return Err(DekUnavailableError::new(
                tenant_id,
                pinned,
                "broker returned unresolved version 0",
            ));
        }
        if version != 0 && fetched.version != version {
            return Err(DekUnavailableError::new(
                tenant_id,
                pinned,
                format!(
                    "broker returned version {} for pinned {version}",
                    fetched.version
                ),
            ));
        }
        Ok(())
    }

    /// Resolves `(tenant, version)` (`version` 0 = current): cache, then negative cache, then
    /// exactly one broker call shared by every concurrent caller of the same key.
    async fn resolve(
        &self,
        tenant_id: i64,
        version: u32,
    ) -> Result<(Dek, u32), DekUnavailableError> {
        if let Some(hit) = self.lookup(tenant_id, version) {
            record_cache_result("hit");
            return Ok(hit);
        }
        let key = (tenant_id, version);
        if let Some(err) = self.negative(key) {
            record_cache_result("negative");
            return Err(err);
        }

        let gate = self.gate(key);
        // Uncontended -> this caller is the leader; contended -> it waits for the leader.
        let (flight, waited) = match gate.try_lock() {
            Ok(guard) => (guard, false),
            Err(_) => (gate.lock().await, true),
        };
        let result = self.resolve_in_flight(tenant_id, version, waited).await;
        drop(flight);
        self.release_gate(key, gate);
        result
    }

    /// The body of [`Self::resolve`] once this caller holds the key's flight gate: re-check
    /// what a leader may have just stored, otherwise lead one broker call.
    async fn resolve_in_flight(
        &self,
        tenant_id: i64,
        version: u32,
        waited: bool,
    ) -> Result<(Dek, u32), DekUnavailableError> {
        let key = (tenant_id, version);
        if let Some(hit) = self.lookup(tenant_id, version) {
            record_cache_result(if waited { "inflight_wait" } else { "hit" });
            return Ok(hit);
        }
        if let Some(err) = self.negative(key) {
            record_cache_result("negative");
            return Err(err);
        }

        record_cache_result("miss");
        let generation = self.generation(tenant_id);
        let outcome = match self.broker.fetch(tenant_id, version).await {
            Ok(fetched) => Self::validate(tenant_id, version, &fetched).map(|()| fetched),
            Err(err) => Err(err),
        };
        match outcome {
            Ok(fetched) => {
                self.store(tenant_id, generation, &fetched, version == 0);
                self.lock().negative.remove(&key);
                Ok((fetched.dek, fetched.version))
            }
            Err(err) => {
                debug!(tenant_id, version, "dek.broker_call_failed_negative_cached");
                self.record_failure(key, &err);
                Err(err)
            }
        }
    }
}

impl<B: DekBroker> DekInvalidate for CachedDekProvider<B> {
    fn invalidate(&self, tenant_id: i64) {
        let mut state = self.lock();
        state.by_version.retain(|(t, _), _| *t != tenant_id);
        state.current.remove(&tenant_id);
        state.negative.retain(|(t, _), _| *t != tenant_id);
        // Bumped even for a non-resident tenant: an in-flight fetch of the key being rotated
        // must not repopulate the cache with a pre-rotation answer.
        *state.generations.entry(tenant_id).or_insert(0) += 1;
    }
}

impl<B: DekBroker> DekProvider for CachedDekProvider<B> {
    async fn get_dek(&self, tenant_id: i64) -> Result<(Dek, u32), DekUnavailableError> {
        self.resolve(tenant_id, 0).await
    }

    async fn get_dek_version(
        &self,
        tenant_id: i64,
        version: u32,
    ) -> Result<Dek, DekUnavailableError> {
        if version == 0 {
            return Err(DekUnavailableError::new(
                tenant_id,
                Some(0),
                "an envelope always names a concrete key version (>= 1)",
            ));
        }
        self.resolve(tenant_id, version).await.map(|(dek, _)| dek)
    }
}

/// The environments [`LocalDevDekProvider`] is permitted to run in -- checked at construction
/// so the dev/alpha derivation can never silently activate in beta/gamma/prod.
const LOCAL_DEV_ALLOWED_ENVIRONMENTS: &[&str] = &["alpha", "local"];

/// Dev/alpha-only DEK source: HKDF-derives a per-tenant DEK from a local KEK. **Not the
/// production mechanism** -- production is [`HubDekBroker`]. It exists so local/alpha
/// environments without a live key broker still exercise real AES-256-GCM end to end.
///
/// **Hard-gated**: construction fails unless `env_name` is `alpha` or `local`
/// (case-insensitive), regardless of whether a KEK happens to be configured.
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

// Never `Debug`-derived: the default derive would print the KEK bytes.
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
    /// Not alpha/local.
    #[error(
        "LocalDevDekProvider is refused outside alpha/local (WADDLES_ENV={0:?}) -- \
         never the production DEK mechanism"
    )]
    EnvironmentNotAllowed(String),
    /// The KEK env var is unset.
    #[error("{env_var} is not set -- LocalDevDekProvider requires an explicit dev KEK, never a hardcoded default")]
    KekNotSet {
        /// The env var name.
        env_var: String,
    },
    /// The KEK env var is shorter than 32 bytes.
    #[error("{env_var} must resolve to >= {min} bytes of KEK material")]
    KekTooShort {
        /// The env var name.
        env_var: String,
        /// Minimum byte length.
        min: usize,
    },
}

impl LocalDevDekProvider {
    /// Resolves the dev KEK from `kek_env_var`, gated by `env_name` (the deployment
    /// environment) -- refuses construction outside alpha/local unconditionally.
    pub fn new(env_name: &str, kek_env_var: &str) -> Result<Self, LocalDevDekProviderError> {
        if !LOCAL_DEV_ALLOWED_ENVIRONMENTS.contains(&env_name.to_ascii_lowercase().as_str()) {
            return Err(LocalDevDekProviderError::EnvironmentNotAllowed(
                env_name.to_string(),
            ));
        }
        let raw = std::env::var(kek_env_var).map_err(|_| LocalDevDekProviderError::KekNotSet {
            env_var: kek_env_var.to_string(),
        })?;
        if raw.len() < DEK_LEN {
            return Err(LocalDevDekProviderError::KekTooShort {
                env_var: kek_env_var.to_string(),
                min: DEK_LEN,
            });
        }
        let mut kek = [0u8; DEK_LEN];
        kek.copy_from_slice(&raw.as_bytes()[..DEK_LEN]);
        Self::from_kek(env_name, kek)
    }

    /// Like [`Self::new`] with the KEK bytes already in hand (no environment read) -- the same
    /// hard environment gate applies.
    pub fn from_kek(env_name: &str, kek: [u8; DEK_LEN]) -> Result<Self, LocalDevDekProviderError> {
        if !LOCAL_DEV_ALLOWED_ENVIRONMENTS.contains(&env_name.to_ascii_lowercase().as_str()) {
            return Err(LocalDevDekProviderError::EnvironmentNotAllowed(
                env_name.to_string(),
            ));
        }
        Ok(Self {
            kek: Zeroizing::new(kek),
        })
    }
}

impl DekBroker for LocalDevDekProvider {
    async fn fetch(
        &self,
        tenant_id: i64,
        version: u32,
    ) -> Result<BrokeredDek, DekUnavailableError> {
        const DEV_VERSION: u32 = 1;
        if version != 0 && version != DEV_VERSION {
            return Err(DekUnavailableError::new(
                tenant_id,
                Some(version),
                format!("the dev provider only has key version {DEV_VERSION}"),
            ));
        }
        let hk = Hkdf::<Sha256>::new(None, self.kek.as_ref());
        let mut dek = [0u8; DEK_LEN];
        let info = format!("waddles-ingest-dev-dek:{tenant_id}:{DEV_VERSION}");
        hk.expand(info.as_bytes(), &mut dek).map_err(|e| {
            DekUnavailableError::new(
                tenant_id,
                Some(DEV_VERSION),
                format!("HKDF expand failed: {e}"),
            )
        })?;
        Ok(BrokeredDek {
            dek: Zeroizing::new(dek),
            version: DEV_VERSION,
            ttl: DEFAULT_DEK_CACHE_TTL,
        })
    }
}

/// The SPIFFE trust-domain prefix every PenguinTech workload id carries.
const SPIFFE_PREFIX: &str = "spiffe://penguintech.io/";
/// Max length of the env segment (mirrors hub-api `_SPIFFE_RE`).
const SPIFFE_ENV_MAX: usize = 32;
/// Max length of the service segment (mirrors hub-api `_SPIFFE_RE`).
const SPIFFE_SERVICE_MAX: usize = 63;

/// A configured SPIFFE id that is missing or does not have the
/// `spiffe://penguintech.io/<env>/<service>` shape hub-api parses the seal-context service from.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("invalid SPIFFE id for the stream-DEK seal context: {reason}")]
pub struct SpiffeIdError {
    /// Why the id was refused (never echoes the id itself).
    pub reason: &'static str,
}

/// True when `seg` is a non-empty `[a-z0-9-]{1,max}` SPIFFE path segment.
fn valid_spiffe_segment(seg: &str, max: usize) -> bool {
    !seg.is_empty()
        && seg.len() <= max
        && seg
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
}

/// Returns the service name (last path segment) of `spiffe_id`, exactly as hub-api's key broker
/// derives `<svc>` for the seal context `<svc>|<tenant_id>|<purpose>|<version>`.
///
/// Deriving it from the workload's own SPIFFE id (not a compile-time constant) keeps the two
/// sides in lock-step across a service rename; a mismatch would make every unseal fail.
///
/// # Errors
/// [`SpiffeIdError`] when the id is empty or not `spiffe://penguintech.io/<env>/<service>` with
/// `[a-z0-9-]` segments (env <= 32, service <= 63 chars).
pub fn service_name_from_spiffe_id(spiffe_id: &str) -> Result<&str, SpiffeIdError> {
    let id = spiffe_id.trim();
    if id.is_empty() {
        return Err(SpiffeIdError {
            reason: "SPIFFE id is empty",
        });
    }
    let rest = id.strip_prefix(SPIFFE_PREFIX).ok_or(SpiffeIdError {
        reason: "SPIFFE id is not in the spiffe://penguintech.io trust domain",
    })?;
    let mut parts = rest.split('/');
    let (Some(env), Some(service), None) = (parts.next(), parts.next(), parts.next()) else {
        return Err(SpiffeIdError {
            reason: "SPIFFE id path must be exactly /<env>/<service>",
        });
    };
    if !valid_spiffe_segment(env, SPIFFE_ENV_MAX) {
        return Err(SpiffeIdError {
            reason: "SPIFFE id env segment is malformed",
        });
    }
    if !valid_spiffe_segment(service, SPIFFE_SERVICE_MAX) {
        return Err(SpiffeIdError {
            reason: "SPIFFE id service segment is malformed",
        });
    }
    Ok(service)
}

/// Runtime-selected provider: the hub-api broker (production) or the dev/alpha local KEK
/// ([`LocalDevDekProvider`]'s hard environment gate). An enum, not a `dyn` object, so it stays
/// `Clone` and `Send` across `tokio::spawn`ed receiver tasks without boxing futures.
#[derive(Clone)]
pub enum ConfiguredDekProvider {
    /// Production: hub-api `KeyService`.
    Hub(CachedDekProvider<HubDekBroker>),
    /// Dev/alpha only.
    LocalDev(CachedDekProvider<LocalDevDekProvider>),
}

impl ConfiguredDekProvider {
    /// Production provider over `client` (which must hold [`STREAM_DEK_SCOPE`]); `service` is
    /// the caller's own service name (`svc-ingest` / `svc-process`).
    #[must_use]
    pub fn hub(client: Arc<HubClient>, service: &str) -> Self {
        Self::Hub(CachedDekProvider::new(
            HubDekBroker::new(client, service),
            BROKER_TTL_CEILING,
        ))
    }

    /// Production provider whose seal-context service name is the last segment of this
    /// workload's own `spiffe_id` (see [`service_name_from_spiffe_id`]).
    ///
    /// # Errors
    /// [`SpiffeIdError`] when `spiffe_id` is missing or malformed -- callers must fail boot.
    pub fn hub_for_spiffe_id(
        client: Arc<HubClient>,
        spiffe_id: &str,
    ) -> Result<Self, SpiffeIdError> {
        Ok(Self::hub(client, service_name_from_spiffe_id(spiffe_id)?))
    }

    /// Dev/alpha provider.
    #[must_use]
    pub fn local_dev(provider: LocalDevDekProvider) -> Self {
        Self::LocalDev(CachedDekProvider::new(provider, DEFAULT_DEK_CACHE_TTL))
    }
}

impl DekInvalidate for ConfiguredDekProvider {
    fn invalidate(&self, tenant_id: i64) {
        match self {
            Self::Hub(p) => p.invalidate(tenant_id),
            Self::LocalDev(p) => p.invalidate(tenant_id),
        }
    }
}

impl DekProvider for ConfiguredDekProvider {
    async fn get_dek(&self, tenant_id: i64) -> Result<(Dek, u32), DekUnavailableError> {
        match self {
            Self::Hub(p) => p.get_dek(tenant_id).await,
            Self::LocalDev(p) => p.get_dek(tenant_id).await,
        }
    }

    async fn get_dek_version(
        &self,
        tenant_id: i64,
        version: u32,
    ) -> Result<Dek, DekUnavailableError> {
        match self {
            Self::Hub(p) => p.get_dek_version(tenant_id, version).await,
            Self::LocalDev(p) => p.get_dek_version(tenant_id, version).await,
        }
    }
}

/// In-process `KeyService` fake implementing the module-level seal contract, for the real-path
/// tests of every crate that consumes a stream DEK. Enabled by the `test-support` feature.
#[cfg(any(test, feature = "test-support"))]
pub mod testing {
    use std::collections::{HashMap, HashSet};
    use std::net::SocketAddr;
    use std::sync::{Arc, Mutex};

    use aes_gcm::aead::{Aead, KeyInit, Payload};
    use aes_gcm::{Aes256Gcm, Nonce};
    use tonic::{Request, Response, Status};

    use super::{derive_seal_key, seal_info, DEK_LEN, INGEST_STREAM_PURPOSE, STREAM_DEK_SCOPE};
    use crate::pb::key_service_server::{KeyService, KeyServiceServer};
    use crate::pb::{GetStreamDekRequest, GetStreamDekResponse};

    /// The bearer token the fake hub accepts (the test's mint hands this out).
    pub const FAKE_TOKEN: &str = "fake-hub-key-token";

    /// Deterministic per-(tenant, version) key, so a test can encrypt independently.
    #[must_use]
    pub fn test_key(tenant_id: i64, version: u32) -> [u8; DEK_LEN] {
        let mut key = [0u8; DEK_LEN];
        for (i, b) in key.iter_mut().enumerate() {
            *b = (tenant_id as u8)
                .wrapping_mul(31)
                .wrapping_add((version as u8).wrapping_mul(7))
                .wrapping_add(i as u8)
                ^ 0x5a;
        }
        key
    }

    /// Seals `dek` for `caller_pub` exactly as hub-api's `KeyService` does.
    ///
    /// # Panics
    /// Never on valid input; panics only on an impossible crypto-parameter error.
    #[must_use]
    pub fn seal(
        caller_pub: &[u8],
        service: &str,
        tenant_id: i64,
        purpose: &str,
        version: u32,
        dek: &[u8; DEK_LEN],
    ) -> Vec<u8> {
        let mut pub_arr = [0u8; 32];
        pub_arr.copy_from_slice(caller_pub);
        let server_secret = x25519_dalek::EphemeralSecret::random();
        let server_pub = x25519_dalek::PublicKey::from(&server_secret);
        let shared = server_secret.diffie_hellman(&x25519_dalek::PublicKey::from(pub_arr));
        let info = seal_info(service, tenant_id, purpose, version);
        let (key, nonce) = derive_seal_key(shared.as_bytes(), &info).unwrap();
        let ct = Aes256Gcm::new_from_slice(&key)
            .unwrap()
            .encrypt(
                Nonce::from_slice(&nonce),
                Payload {
                    msg: dek.as_slice(),
                    aad: &info,
                },
            )
            .unwrap();
        let mut out = server_pub.as_bytes().to_vec();
        out.extend_from_slice(&ct);
        out
    }

    /// Mutable behavior of the fake hub; share it to rotate / break the hub mid-test.
    #[derive(Default)]
    pub struct FakeKeyState {
        /// Current version per tenant (default 1).
        pub current: HashMap<i64, u32>,
        /// Tenants the hub refuses (`PERMISSION_DENIED`).
        pub denied: HashSet<i64>,
        /// Makes every call fail with `UNAVAILABLE`.
        pub down: bool,
        /// TTL granted in each response.
        pub ttl_seconds: u32,
        /// `(tenant_id, requested_version, caller_pub)` of every accepted call.
        pub calls: Vec<(i64, u32, Vec<u8>)>,
        /// Override the sealed bytes of every response (malformed-response tests).
        pub corrupt_sealed: Option<Vec<u8>>,
        /// Override the response version (broker-lies tests).
        pub force_version: Option<u32>,
        /// The caller's service name the fake seals for (`svc-ingest` unless changed).
        pub service: String,
        /// Every call that reached the fake and authenticated, accepted or refused -- the count
        /// a stampede test asserts on (`calls` only records accepted ones).
        pub attempts: usize,
        /// Wait this long before answering, so concurrent callers genuinely overlap.
        pub delay: std::time::Duration,
    }

    /// `KeyService` fake enforcing bearer token + scope-by-token, tenant numeric ids, and the
    /// seal contract.
    #[derive(Clone)]
    pub struct FakeKeyService {
        /// Shared, mutable state.
        pub state: Arc<Mutex<FakeKeyState>>,
    }

    impl Default for FakeKeyService {
        fn default() -> Self {
            Self::new()
        }
    }

    impl FakeKeyService {
        /// A healthy hub: every tenant at version 1, 300 s TTL.
        #[must_use]
        pub fn new() -> Self {
            Self {
                state: Arc::new(Mutex::new(FakeKeyState {
                    ttl_seconds: 600,
                    service: "svc-ingest".to_string(),
                    ..FakeKeyState::default()
                })),
            }
        }

        /// A healthy hub serving the caller named `service` (`svc-process` for the decrypt side).
        ///
        /// # Panics
        /// Only if the state mutex is poisoned.
        #[must_use]
        pub fn for_service(service: &str) -> Self {
            let fake = Self::new();
            fake.state.lock().unwrap().service = service.to_string();
            fake
        }

        /// Number of accepted calls so far.
        ///
        /// # Panics
        /// Only if the state mutex is poisoned.
        #[must_use]
        pub fn call_count(&self) -> usize {
            self.state.lock().unwrap().calls.len()
        }

        /// Serves the fake on a loopback port; returns the `http://` endpoint.
        ///
        /// # Panics
        /// If the loopback listener cannot be bound.
        pub async fn serve(&self) -> String {
            let probe = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
            let addr: SocketAddr = probe.local_addr().unwrap();
            drop(probe);
            let svc = self.clone();
            tokio::spawn(async move {
                let _ = tonic::transport::Server::builder()
                    .add_service(KeyServiceServer::new(svc))
                    .serve(addr)
                    .await;
            });
            tokio::time::sleep(std::time::Duration::from_millis(100)).await;
            format!("http://{addr}")
        }
    }

    #[tonic::async_trait]
    impl KeyService for FakeKeyService {
        async fn get_stream_dek(
            &self,
            request: Request<GetStreamDekRequest>,
        ) -> Result<Response<GetStreamDekResponse>, Status> {
            let auth = request
                .metadata()
                .get("authorization")
                .and_then(|v| v.to_str().ok())
                .unwrap_or_default()
                .to_string();
            if auth != format!("Bearer {FAKE_TOKEN}") {
                return Err(Status::unauthenticated("unauthorized"));
            }
            let req = request.into_inner();
            let delay = {
                let mut state = self.state.lock().unwrap();
                state.attempts += 1;
                state.delay
            };
            if !delay.is_zero() {
                tokio::time::sleep(delay).await;
            }
            let tenant_id: i64 = req
                .tenant_id
                .parse()
                .map_err(|_| Status::invalid_argument("tenant_id must be numeric"))?;
            if req.purpose != INGEST_STREAM_PURPOSE {
                return Err(Status::invalid_argument("unknown purpose"));
            }
            if req.caller_ephemeral_public_key.len() != 32 {
                return Err(Status::invalid_argument("bad ephemeral key"));
            }
            let mut state = self.state.lock().unwrap();
            if state.down {
                return Err(Status::unavailable("hub down"));
            }
            if state.denied.contains(&tenant_id) {
                return Err(Status::permission_denied("tenant not allowed"));
            }
            let current = *state.current.get(&tenant_id).unwrap_or(&1);
            let version = if req.version == 0 {
                current
            } else {
                req.version
            };
            if version > current {
                return Err(Status::not_found("no such key version"));
            }
            state.calls.push((
                tenant_id,
                req.version,
                req.caller_ephemeral_public_key.clone(),
            ));
            let sealed = state.corrupt_sealed.clone().unwrap_or_else(|| {
                seal(
                    &req.caller_ephemeral_public_key,
                    &state.service,
                    tenant_id,
                    &req.purpose,
                    version,
                    &test_key(tenant_id, version),
                )
            });
            Ok(Response::new(GetStreamDekResponse {
                sealed_dek: sealed,
                version: state.force_version.unwrap_or(version),
                ttl_seconds: state.ttl_seconds,
            }))
        }
    }

    /// The scope the fake's mint must grant (documented here so tests agree with the client).
    pub const REQUIRED_SCOPE: &str = STREAM_DEK_SCOPE;
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicU32, Ordering};

    use service_auth::test_support::{MintReply, MockHub};
    use service_auth::MachineJwtClient;

    use super::testing::{test_key, FakeKeyService, FAKE_TOKEN, REQUIRED_SCOPE};
    use super::*;
    use crate::lazy_channel;

    async fn hub_client(endpoint: &str, tag: &str) -> (Arc<HubClient>, MockHub) {
        let mint = MockHub::builder()
            .mint(vec![MintReply::when(
                |c| c.scope == REQUIRED_SCOPE,
                FAKE_TOKEN,
                3600,
            )])
            .start()
            .await;
        let sa =
            std::env::temp_dir().join(format!("hub-client-dek-sa-{tag}-{}", std::process::id()));
        std::fs::write(&sa, "projected-sa-token").unwrap();
        let tokens = Arc::new(MachineJwtClient::new(
            mint.endpoint.clone(),
            sa.to_string_lossy().to_string(),
            STREAM_DEK_SCOPE,
        ));
        let channel = lazy_channel(endpoint, None).unwrap();
        (Arc::new(HubClient::with_channel(channel, tokens)), mint)
    }

    #[tokio::test]
    async fn broker_opens_the_sealed_dek_current_and_pinned() {
        let fake = FakeKeyService::new();
        fake.state.lock().unwrap().current.insert(7, 2);
        let (client, _mint) = hub_client(&fake.serve().await, "open").await;
        let broker = HubDekBroker::new(client, "svc-ingest");

        let cur = broker.fetch(7, 0).await.unwrap();
        assert_eq!(cur.version, 2);
        assert_eq!(*cur.dek, test_key(7, 2));
        let old = broker.fetch(7, 1).await.unwrap();
        assert_eq!(old.version, 1);
        assert_eq!(*old.dek, test_key(7, 1));
    }

    #[tokio::test]
    async fn fresh_ephemeral_key_per_call_and_ttl_is_clamped() {
        let fake = FakeKeyService::new();
        fake.state.lock().unwrap().ttl_seconds = 86_400;
        let (client, _mint) = hub_client(&fake.serve().await, "eph").await;
        let broker = HubDekBroker::new(client, "svc-ingest");
        let a = broker.fetch(3, 0).await.unwrap();
        broker.fetch(3, 0).await.unwrap();
        assert_eq!(
            a.ttl, BROKER_TTL_CEILING,
            "a day-long grant is clamped to 3600 s"
        );
        let calls = fake.state.lock().unwrap().calls.clone();
        assert_eq!(calls.len(), 2);
        assert_ne!(calls[0].2, calls[1].2, "ephemeral key must never repeat");
        assert_eq!(calls[0].0, 3, "numeric tenant id on the wire");
    }

    #[tokio::test]
    async fn broker_fails_closed_on_every_bad_response() {
        // Truncated sealed bytes.
        let fake = FakeKeyService::new();
        fake.state.lock().unwrap().corrupt_sealed = Some(vec![1; 79]);
        let (client, _m) = hub_client(&fake.serve().await, "short").await;
        let err = HubDekBroker::new(client, "svc-ingest")
            .fetch(1, 0)
            .await
            .err()
            .unwrap();
        assert!(err.reason.contains("79 bytes"), "{err}");

        // Right length, wrong content -> AEAD failure.
        let fake = FakeKeyService::new();
        fake.state.lock().unwrap().corrupt_sealed = Some(vec![9; 80]);
        let (client, _m) = hub_client(&fake.serve().await, "aead").await;
        let err = HubDekBroker::new(client, "svc-ingest")
            .fetch(1, 0)
            .await
            .err()
            .unwrap();
        assert!(
            err.reason.contains("authentication failed") || err.reason.contains("low-order"),
            "{err}"
        );

        // Broker lies about the version of a pinned request.
        let fake = FakeKeyService::new();
        fake.state.lock().unwrap().current.insert(1, 5);
        fake.state.lock().unwrap().force_version = Some(4);
        let (client, _m) = hub_client(&fake.serve().await, "lie").await;
        let err = HubDekBroker::new(client, "svc-ingest")
            .fetch(1, 3)
            .await
            .err()
            .unwrap();
        assert!(err.reason.contains("pinned version 3"), "{err}");

        // Unresolved version 0 in the response.
        let fake = FakeKeyService::new();
        fake.state.lock().unwrap().force_version = Some(0);
        let (client, _m) = hub_client(&fake.serve().await, "zero").await;
        let err = HubDekBroker::new(client, "svc-ingest")
            .fetch(1, 0)
            .await
            .err()
            .unwrap();
        assert!(err.reason.contains("version 0"), "{err}");
    }

    #[tokio::test]
    async fn broker_refuses_a_non_positive_tenant_id_without_calling_the_hub() {
        let fake = FakeKeyService::new();
        let (client, _m) = hub_client(&fake.serve().await, "zero-tenant").await;
        let err = HubDekBroker::new(client, "svc-ingest")
            .fetch(0, 0)
            .await
            .err()
            .unwrap();
        assert!(err.reason.contains("no numeric tenant id"));
        assert_eq!(fake.call_count(), 0);
    }

    #[tokio::test]
    async fn broker_surfaces_hub_denial_and_outage_as_dek_unavailable() {
        let fake = FakeKeyService::new();
        fake.state.lock().unwrap().denied.insert(9);
        let (client, _m) = hub_client(&fake.serve().await, "deny").await;
        let broker = HubDekBroker::new(client, "svc-ingest");
        assert!(broker.fetch(9, 0).await.is_err());
        fake.state.lock().unwrap().down = true;
        assert!(broker.fetch(2, 0).await.is_err());
    }

    #[tokio::test]
    async fn broker_fails_closed_when_no_machine_jwt_can_be_minted() {
        let fake = FakeKeyService::new();
        let endpoint = fake.serve().await;
        let tokens = Arc::new(MachineJwtClient::new(
            "http://127.0.0.1:1",
            "/nonexistent/sa-token",
            STREAM_DEK_SCOPE,
        ));
        let client = Arc::new(HubClient::with_channel(
            lazy_channel(&endpoint, None).unwrap(),
            tokens,
        ));
        let err = HubDekBroker::new(client, "svc-ingest")
            .fetch(1, 0)
            .await
            .err()
            .unwrap();
        assert!(err.reason.contains("key broker call failed"), "{err}");
        assert_eq!(fake.call_count(), 0, "no token, no call");
    }

    #[tokio::test]
    async fn cache_serves_repeat_reads_and_keys_by_tenant_and_version() {
        let fake = FakeKeyService::new();
        fake.state.lock().unwrap().current.insert(5, 2);
        let (client, _m) = hub_client(&fake.serve().await, "cache").await;
        let cache = CachedDekProvider::new(
            HubDekBroker::new(client, "svc-ingest"),
            DEFAULT_DEK_CACHE_TTL,
        );

        let (dek, v) = cache.get_dek(5).await.unwrap();
        assert_eq!((v, *dek), (2, test_key(5, 2)));
        cache.get_dek(5).await.unwrap();
        assert_eq!(fake.call_count(), 1, "second current read is a cache hit");

        // Decrypt path: the version-2 key is already cached; version 1 needs one fetch.
        assert_eq!(*cache.get_dek_version(5, 2).await.unwrap(), test_key(5, 2));
        assert_eq!(fake.call_count(), 1);
        assert_eq!(*cache.get_dek_version(5, 1).await.unwrap(), test_key(5, 1));
        cache.get_dek_version(5, 1).await.unwrap();
        assert_eq!(fake.call_count(), 2);

        // Tenant isolation: a different tenant never sees tenant 5's keys.
        let (other, _) = cache.get_dek(6).await.unwrap();
        assert_ne!(*other, test_key(5, 1));
        assert_eq!(*other, test_key(6, 1));
        assert_eq!(fake.call_count(), 3);
    }

    #[tokio::test]
    async fn cache_picks_up_a_rotation_after_invalidate_and_after_ttl() {
        let fake = FakeKeyService::new();
        let (client, _m) = hub_client(&fake.serve().await, "rot").await;
        let cache = CachedDekProvider::new(
            HubDekBroker::new(client, "svc-ingest"),
            DEFAULT_DEK_CACHE_TTL,
        );
        assert_eq!(cache.get_dek(4).await.unwrap().1, 1);

        fake.state.lock().unwrap().current.insert(4, 2);
        assert_eq!(cache.get_dek(4).await.unwrap().1, 1, "still cached");
        cache.invalidate(4);
        assert_eq!(
            cache.get_dek(4).await.unwrap().1,
            2,
            "invalidate forces a re-resolve"
        );

        // A zero-TTL grant is floored to 60 s, not turned into a round trip per event.
        let fake = FakeKeyService::new();
        fake.state.lock().unwrap().ttl_seconds = 0;
        let (client, _m) = hub_client(&fake.serve().await, "ttl0").await;
        let cache =
            CachedDekProvider::new(HubDekBroker::new(client, "svc-ingest"), BROKER_TTL_CEILING);
        cache.get_dek(4).await.unwrap();
        cache.get_dek(4).await.unwrap();
        assert_eq!(fake.call_count(), 1);
    }

    #[tokio::test]
    async fn cache_never_exceeds_its_own_ttl_ceiling() {
        struct CountingBroker(AtomicU32);
        impl DekBroker for CountingBroker {
            async fn fetch(&self, _t: i64, _v: u32) -> Result<BrokeredDek, DekUnavailableError> {
                self.0.fetch_add(1, Ordering::SeqCst);
                Ok(BrokeredDek {
                    dek: Zeroizing::new([1; DEK_LEN]),
                    version: 1,
                    ttl: Duration::from_secs(3600),
                })
            }
        }
        let cache =
            CachedDekProvider::new(CountingBroker(AtomicU32::new(0)), Duration::from_millis(30));
        cache.get_dek(1).await.unwrap();
        cache.get_dek(1).await.unwrap();
        assert_eq!(cache.broker.0.load(Ordering::SeqCst), 1);
        tokio::time::sleep(Duration::from_millis(50)).await;
        cache.get_dek(1).await.unwrap();
        assert_eq!(
            cache.broker.0.load(Ordering::SeqCst),
            2,
            "expired at the cache ceiling"
        );
    }

    #[tokio::test]
    async fn failures_are_not_cached_and_version_zero_is_rejected() {
        let fake = FakeKeyService::new();
        fake.state.lock().unwrap().down = true;
        let (client, _m) = hub_client(&fake.serve().await, "nofail").await;
        let cache = CachedDekProvider::new(
            HubDekBroker::new(client, "svc-ingest"),
            DEFAULT_DEK_CACHE_TTL,
        )
        .with_negative_backoff(Duration::ZERO);
        assert!(cache.get_dek(1).await.is_err());
        fake.state.lock().unwrap().down = false;
        assert!(
            cache.get_dek(1).await.is_ok(),
            "an outage must not poison the cache"
        );
        assert!(cache.get_dek_version(1, 0).await.is_err());
    }

    #[tokio::test]
    async fn local_dev_provider_is_environment_gated_and_per_tenant() {
        std::env::set_var("DEK_TEST_KEK", "k".repeat(40));
        assert!(matches!(
            LocalDevDekProvider::new("production", "DEK_TEST_KEK"),
            Err(LocalDevDekProviderError::EnvironmentNotAllowed(_))
        ));
        assert!(matches!(
            LocalDevDekProvider::new("alpha", "DEK_TEST_UNSET_KEK"),
            Err(LocalDevDekProviderError::KekNotSet { .. })
        ));
        let provider = ConfiguredDekProvider::local_dev(
            LocalDevDekProvider::new("alpha", "DEK_TEST_KEK").unwrap(),
        );
        let (a, va) = provider.get_dek(1).await.unwrap();
        let (b, _) = provider.get_dek(2).await.unwrap();
        assert_eq!(va, 1);
        assert_ne!(*a, *b);
        assert_eq!(*provider.get_dek_version(1, 1).await.unwrap(), *a);
        assert!(provider.get_dek_version(1, 2).await.is_err());
    }

    #[test]
    fn default_get_dek_version_only_serves_the_current_version() {
        struct Fixed;
        impl DekProvider for Fixed {
            async fn get_dek(&self, _t: i64) -> Result<(Dek, u32), DekUnavailableError> {
                Ok((Zeroizing::new([3; DEK_LEN]), 4))
            }
        }
        let rt = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        rt.block_on(async {
            assert!(Fixed.get_dek_version(1, 4).await.is_ok());
            assert!(Fixed.get_dek_version(1, 3).await.is_err());
        });
    }

    #[tokio::test]
    async fn a_seal_for_another_service_never_opens() {
        // The ctx binds the holder: a key sealed for svc-process is useless to svc-ingest.
        let fake = FakeKeyService::for_service("svc-process");
        let (client, _m) = hub_client(&fake.serve().await, "svc-bind").await;
        let err = HubDekBroker::new(client, "svc-ingest")
            .fetch(1, 0)
            .await
            .err()
            .unwrap();
        assert!(err.reason.contains("authentication failed"), "{err}");

        let fake = FakeKeyService::for_service("svc-process");
        let (client, _m) = hub_client(&fake.serve().await, "svc-bind2").await;
        let ok = HubDekBroker::new(client, "svc-process")
            .fetch(1, 0)
            .await
            .unwrap();
        assert_eq!(*ok.dek, test_key(1, 1));
    }

    #[tokio::test]
    async fn ctx_service_comes_from_the_spiffe_id_segment() {
        // hub-api seals for the SVID's last segment; the client must open with the same name,
        // so a renamed workload (svc-gateway) still unseals without a code change.
        let fake = FakeKeyService::for_service("svc-gateway");
        let (client, _m) = hub_client(&fake.serve().await, "spiffe-ctx").await;
        let provider = ConfiguredDekProvider::hub_for_spiffe_id(
            client,
            "spiffe://penguintech.io/alpha/svc-gateway",
        )
        .unwrap();
        let (dek, version) = provider.get_dek(1).await.unwrap();
        assert_eq!(version, 1);
        assert_eq!(*dek, test_key(1, 1));

        // The old compile-time name against the same seal fails closed (ctx mismatch).
        let fake = FakeKeyService::for_service("svc-gateway");
        let (client, _m) = hub_client(&fake.serve().await, "spiffe-ctx2").await;
        let provider = ConfiguredDekProvider::hub_for_spiffe_id(
            client,
            "spiffe://penguintech.io/alpha/svc-ingest",
        )
        .unwrap();
        assert!(provider.get_dek(1).await.is_err());
    }

    #[test]
    fn service_name_is_the_last_spiffe_segment() {
        for (id, svc) in [
            ("spiffe://penguintech.io/alpha/svc-ingest", "svc-ingest"),
            ("spiffe://penguintech.io/prod/svc-gateway", "svc-gateway"),
            (" spiffe://penguintech.io/beta/svc-process\n", "svc-process"),
        ] {
            assert_eq!(service_name_from_spiffe_id(id).unwrap(), svc, "{id}");
        }
    }

    #[test]
    fn malformed_spiffe_ids_are_refused() {
        let long_env = format!("spiffe://penguintech.io/{}/svc-ingest", "a".repeat(33));
        let long_svc = format!("spiffe://penguintech.io/alpha/{}", "s".repeat(64));
        for id in [
            "",
            "   ",
            "svc-ingest",
            "spiffe://evil.example/alpha/svc-ingest",
            "spiffe://penguintech.io/svc-ingest",
            "spiffe://penguintech.io/alpha/svc-ingest/extra",
            "spiffe://penguintech.io/alpha/",
            "spiffe://penguintech.io//svc-ingest",
            "spiffe://penguintech.io/alpha/Svc-Ingest",
            "spiffe://penguintech.io/alpha/svc_ingest",
            "https://penguintech.io/alpha/svc-ingest",
            long_env.as_str(),
            long_svc.as_str(),
        ] {
            assert!(service_name_from_spiffe_id(id).is_err(), "accepted {id:?}");
        }
    }

    #[tokio::test]
    async fn hub_for_spiffe_id_refuses_a_malformed_id() {
        let fake = FakeKeyService::new();
        let (client, _m) = hub_client(&fake.serve().await, "spiffe-bad").await;
        let err = ConfiguredDekProvider::hub_for_spiffe_id(client, "")
            .err()
            .unwrap();
        assert_eq!(err.reason, "SPIFFE id is empty");
    }

    /// Cross-language vector: `tests/fixtures/stream_dek_python_vector.json` was generated by
    /// running hub-api's real `seal_context`/`seal_dek` (PR #1246) with fixed keys; the Rust
    /// open must recover the DEK byte for byte.
    #[test]
    fn opens_a_python_sealed_vector_from_the_hub_api_seal_code() {
        #[derive(serde::Deserialize)]
        struct Vector {
            caller_private_hex: String,
            service: String,
            tenant_id: i64,
            purpose: String,
            version: u32,
            dek_hex: String,
            sealed_dek_hex: String,
        }
        let v: Vector = serde_json::from_str(include_str!(
            "../tests/fixtures/stream_dek_python_vector.json"
        ))
        .unwrap();
        let unhex = |h: &str| -> Vec<u8> {
            (0..h.len())
                .step_by(2)
                .map(|i| u8::from_str_radix(&h[i..i + 2], 16).unwrap())
                .collect()
        };
        let mut priv_bytes = [0u8; 32];
        priv_bytes.copy_from_slice(&unhex(&v.caller_private_hex));
        let secret = x25519_dalek::StaticSecret::from(priv_bytes);
        let sealed = unhex(&v.sealed_dek_hex);
        let mut server_pub = [0u8; 32];
        server_pub.copy_from_slice(&sealed[..32]);
        let shared = secret.diffie_hellman(&x25519_dalek::PublicKey::from(server_pub));
        let dek = open_with_shared(
            shared.as_bytes(),
            &sealed,
            &v.service,
            v.tenant_id,
            &v.purpose,
            v.version,
        )
        .unwrap();
        assert_eq!(dek.as_slice(), unhex(&v.dek_hex).as_slice());
        // Any change to the bound context fails closed.
        for (svc, tenant, ver) in [
            ("svc-process", 42, 3),
            ("svc-ingest", 43, 3),
            ("svc-ingest", 42, 4),
        ] {
            assert!(
                open_with_shared(shared.as_bytes(), &sealed, svc, tenant, &v.purpose, ver).is_err()
            );
        }
    }
}

/// Real-path single-flight / negative-cache tests: an in-process tonic `KeyService` fake and a
/// real `MachineJwtClient` against a counting `MockHub`, so every assertion is on calls that
/// actually reached the wire (hub-api's two stampede amplifiers: DEK fetches and token mints).
#[cfg(test)]
mod single_flight_tests {
    use std::time::Duration;

    use service_auth::test_support::{MintReply, MockHub};
    use service_auth::MachineJwtClient;
    use tokio::task::JoinSet;

    use super::testing::{test_key, FakeKeyService, FAKE_TOKEN, REQUIRED_SCOPE};
    use super::*;
    use crate::lazy_channel;

    const SERVICE: &str = "svc-process";

    /// One wired stack: provider -> `HubClient` -> (fake `KeyService`, counting mint hub).
    struct Stack {
        provider: CachedDekProvider<HubDekBroker>,
        client: Arc<HubClient>,
        fake: FakeKeyService,
        mint: MockHub,
    }

    impl Stack {
        /// `KeyService` calls that reached the fake (accepted or refused).
        fn attempts(&self) -> usize {
            self.fake.state.lock().unwrap().attempts
        }
    }

    /// Builds a stack; `mint_reply` scripts the token endpoint, the delays make concurrent
    /// callers genuinely overlap on the wire.
    async fn stack(
        tag: &str,
        mint_reply: MintReply,
        fake_delay: Duration,
        mint_delay: Duration,
        ttl: Duration,
        negative: Duration,
    ) -> Stack {
        let fake = FakeKeyService::for_service(SERVICE);
        fake.state.lock().unwrap().delay = fake_delay;
        let endpoint = fake.serve().await;
        let mint = MockHub::builder()
            .mint(vec![mint_reply])
            .delay(mint_delay)
            .start()
            .await;
        let sa =
            std::env::temp_dir().join(format!("hub-client-sf-sa-{tag}-{}", std::process::id()));
        std::fs::write(&sa, "projected-sa-token").unwrap();
        let tokens = Arc::new(MachineJwtClient::new(
            mint.endpoint.clone(),
            sa.to_string_lossy().to_string(),
            STREAM_DEK_SCOPE,
        ));
        let client = Arc::new(HubClient::with_channel(
            lazy_channel(&endpoint, None).unwrap(),
            tokens,
        ));
        let provider = CachedDekProvider::new(HubDekBroker::new(client.clone(), SERVICE), ttl)
            .with_negative_backoff(negative);
        Stack {
            provider,
            client,
            fake,
            mint,
        }
    }

    fn good_mint() -> MintReply {
        MintReply::when(|c| c.scope == REQUIRED_SCOPE, FAKE_TOKEN, 3600)
    }

    /// `n` concurrent pinned-version decrypts of `(tenant, version)`.
    async fn decrypt_all(
        provider: &CachedDekProvider<HubDekBroker>,
        n: usize,
        tenant: i64,
        version: u32,
    ) -> Vec<Result<[u8; DEK_LEN], DekUnavailableError>> {
        let mut set = JoinSet::new();
        for _ in 0..n {
            let provider = provider.clone();
            set.spawn(async move {
                provider
                    .get_dek_version(tenant, version)
                    .await
                    .map(|dek| *dek)
            });
        }
        let mut out = Vec::with_capacity(n);
        while let Some(joined) = set.join_next().await {
            out.push(joined.unwrap());
        }
        out
    }

    /// Jumps the tokio clock forward without letting auto-advance skip real I/O deadlines.
    async fn skip(by: Duration) {
        tokio::time::pause();
        tokio::time::advance(by).await;
        tokio::time::resume();
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn cold_start_stampede_is_one_broker_call_and_one_token_mint() {
        let s = stack(
            "cold",
            good_mint(),
            Duration::from_millis(150),
            Duration::from_millis(150),
            DEFAULT_DEK_CACHE_TTL,
            NEGATIVE_BACKOFF_BASE,
        )
        .await;
        let results = decrypt_all(&s.provider, 160, 7, 1).await;
        assert_eq!(results.len(), 160);
        for r in &results {
            assert_eq!(
                r.as_ref().unwrap(),
                &test_key(7, 1),
                "every waiter gets the key"
            );
        }
        assert_eq!(
            s.attempts(),
            1,
            "160 concurrent cold decrypts must be exactly one KeyService call"
        );
        assert_eq!(s.mint.mint_hits(), 1, "...and exactly one token mint");
        assert!(s.client.circuits.key.allow());
    }

    #[tokio::test]
    async fn sustained_load_costs_one_broker_call_per_ttl_window() {
        let s = stack(
            "sustained",
            good_mint(),
            Duration::ZERO,
            Duration::ZERO,
            Duration::from_secs(10),
            NEGATIVE_BACKOFF_BASE,
        )
        .await;
        let mut ok = 0;
        for batch in [334usize, 333, 333] {
            for r in decrypt_all(&s.provider, batch, 5, 1).await {
                assert_eq!(r.unwrap(), test_key(5, 1));
                ok += 1;
            }
            skip(Duration::from_secs(11)).await; // past the 10 s cache TTL
        }
        assert_eq!(ok, 1000);
        assert_eq!(
            s.attempts(),
            3,
            "1000 decrypts across 3 TTL windows = 3 broker calls"
        );
        assert_eq!(s.mint.mint_hits(), 1, "one token served all three windows");
    }

    #[tokio::test]
    async fn a_new_key_version_is_refetched_and_tenants_never_share_flights() {
        let s = stack(
            "rotate",
            good_mint(),
            Duration::ZERO,
            Duration::ZERO,
            DEFAULT_DEK_CACHE_TTL,
            NEGATIVE_BACKOFF_BASE,
        )
        .await;
        assert_eq!(s.provider.get_dek(4).await.unwrap().1, 1);
        // Rotation: version 2 is a different cache key, so a decrypt naming it must fetch.
        s.fake.state.lock().unwrap().current.insert(4, 2);
        assert_eq!(
            *s.provider.get_dek_version(4, 2).await.unwrap(),
            test_key(4, 2)
        );
        assert_eq!(s.attempts(), 2);
        // The old version (cached by the first `get_dek`) is still served from memory.
        assert_eq!(
            *s.provider.get_dek_version(4, 1).await.unwrap(),
            test_key(4, 1)
        );
        assert_eq!(s.attempts(), 2);
        // Another tenant is its own key and its own flight.
        let other = decrypt_all(&s.provider, 20, 6, 1).await;
        assert!(other.iter().all(|r| r.as_ref().unwrap() == &test_key(6, 1)));
        assert_eq!(s.attempts(), 3);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn an_invalidate_during_a_fetch_is_served_but_never_cached() {
        let s = stack(
            "race",
            good_mint(),
            Duration::from_millis(250),
            Duration::ZERO,
            DEFAULT_DEK_CACHE_TTL,
            NEGATIVE_BACKOFF_BASE,
        )
        .await;
        let p = s.provider.clone();
        let inflight = tokio::spawn(async move { p.get_dek(9).await });
        tokio::time::sleep(Duration::from_millis(80)).await;
        s.provider.invalidate(9); // rotation signal lands mid-fetch
        assert_eq!(
            inflight.await.unwrap().unwrap().1,
            1,
            "the caller is still served"
        );
        s.fake.state.lock().unwrap().current.insert(9, 2);
        assert_eq!(
            s.provider.get_dek(9).await.unwrap().1,
            2,
            "the raced result was not cached: the next read re-resolves to the rotated key"
        );
    }

    #[tokio::test]
    async fn broker_failure_fails_every_waiter_from_one_call_then_retries_after_backoff() {
        let s = stack(
            "fail",
            good_mint(),
            Duration::from_millis(100),
            Duration::ZERO,
            DEFAULT_DEK_CACHE_TTL,
            Duration::from_secs(2),
        )
        .await;
        s.fake.state.lock().unwrap().down = true;

        let first = decrypt_all(&s.provider, 160, 3, 1).await;
        assert!(first.iter().all(Result::is_err), "all 160 fail closed");
        let after_first = s.attempts();
        assert_eq!(
            after_first, 3,
            "one leader call = 1 try + 2 retries; 159 waiters made no call"
        );
        assert!(
            s.client.circuits.key.allow(),
            "one coalesced failure (3 attempts < threshold 5) must not open the breaker"
        );

        // Inside the negative window: another 160 callers are shed without any wire traffic.
        let second = decrypt_all(&s.provider, 160, 3, 1).await;
        assert!(second.iter().all(Result::is_err));
        assert_eq!(s.attempts(), after_first);

        // After the backoff the hub is retried -- and a recovered hub is served again.
        skip(Duration::from_secs(3)).await;
        s.fake.state.lock().unwrap().down = false;
        assert_eq!(
            *s.provider.get_dek_version(3, 1).await.unwrap(),
            test_key(3, 1)
        );
        assert_eq!(s.attempts(), after_first + 1);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn a_failing_token_mint_is_one_mint_for_every_waiter_and_never_reaches_the_broker() {
        let s = stack(
            "mintfail",
            MintReply::Reject(tonic::Code::Unavailable),
            Duration::ZERO,
            Duration::from_millis(100),
            DEFAULT_DEK_CACHE_TTL,
            NEGATIVE_BACKOFF_BASE,
        )
        .await;
        let results = decrypt_all(&s.provider, 160, 2, 1).await;
        assert!(results.iter().all(Result::is_err), "fail closed, no key");
        assert_eq!(s.mint.mint_hits(), 1, "160 callers, one mint attempt");
        assert_eq!(s.attempts(), 0, "no token, no KeyService call");
        assert!(
            s.client.circuits.key.allow(),
            "a single coalesced failure never opens it"
        );
    }

    #[tokio::test]
    async fn zero_negative_backoff_disables_only_the_negative_cache() {
        let s = stack(
            "zero",
            good_mint(),
            Duration::ZERO,
            Duration::ZERO,
            DEFAULT_DEK_CACHE_TTL,
            Duration::ZERO,
        )
        .await;
        s.fake.state.lock().unwrap().denied.insert(8);
        assert!(s.provider.get_dek_version(8, 1).await.is_err());
        s.fake.state.lock().unwrap().denied.clear();
        assert!(
            s.provider.get_dek_version(8, 1).await.is_ok(),
            "no negative window"
        );
    }
}
