//! Per-tenant at-rest sealing of object BODIES (owner requirement: bundle data is
//! cryptographically separated per tenant).
//!
//! Bodies are AES-256-GCM sealed (`bundle_host_crypto`) under a subkey derived from the
//! tenant's DEK, which the data plane fetches from hub-api's `GetTenantDek` broker. The
//! AAD binds `tenant | community | prefix-owner bundle | "object" | key`, so a body copied
//! across tenants, communities, bundles or keys fails to open -- one tenant's DEK can never
//! decrypt another tenant's objects even with bucket-level access.
//!
//! What stays plaintext (the host filters on it): object tags, content type, key names
//! and the size (the sealed size is the plaintext size plus [`OVERHEAD`], which the host
//! subtracts again for guest-visible sizes). Bodies are the confidential part.
//!
//! Fail-closed rules (an attacker with bucket WRITE access must never be able to turn a
//! sealed object into served plaintext):
//! - no usable key -> the put/get fails (never a plaintext fallback);
//! - an envelope is authoritative (opened even if the seal tag was stripped); a sealed-tagged
//!   body that is not an envelope is an integrity failure;
//! - plaintext is served ONLY if (a) the host itself wrote it while the kill-switch was on --
//!   proved by a `waddles-plain` HMAC tag over (scope, key, body) under the tenant key, which
//!   the attacker cannot forge -- or (b) it predates the operator-recorded legacy cutoff
//!   (server-assigned last-modified < cutoff; re-writing an object can only make it newer, so
//!   an attacker's replacement is always refused). The cutoff is opt-in and unset by default:
//!   by default NO unauthenticated plaintext is ever served.
//! - A host without sealing configured refuses sealed objects instead of returning ciphertext.
//!
//! Known edge: re-tagging a legacy object (`tag_add`) rewrites it server-side and moves its
//! last-modified past the cutoff; migrate legacy objects (re-put) instead of tagging them.

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

use bundle_host_crypto::{CryptoError, TenantCrypto, OVERHEAD};
use bytes::Bytes;

use crate::backend::ObjectMeta;
use crate::error::StorageError;
use crate::key::object_key;
use crate::tags::{system_tag, TAG_PLAIN_MAC, TAG_SEALED};

/// Metric/log surface label for [`TenantCrypto`].
pub const SURFACE: &str = "object";

/// Boxed future (the gate is dyn-safe).
pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// Operator kill-switch seam (`waddles.disable-tenant-at-rest-encryption`): when it says
/// "do not seal", writes are stored plaintext on purpose (WARN). Reads of sealed objects
/// always open regardless.
pub trait SealGate: Send + Sync {
    /// `true` (the default posture) seals new writes.
    fn seal_writes(&self) -> BoxFuture<'_, bool>;
}

/// Always seals.
pub struct AlwaysSeal;

impl SealGate for AlwaysSeal {
    fn seal_writes(&self) -> BoxFuture<'_, bool> {
        Box::pin(async { true })
    }
}

/// What the ledger knows about one object key.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct LedgerEntry {
    /// Counter of the live object IF it is host-written authenticated plaintext; `0` when the
    /// live object is sealed or was deleted (tombstone).
    pub current: u64,
    /// Counter allocated by an in-flight (write-ahead) mutation not yet committed; `0` = none.
    pub pending: u64,
}

/// Durable, host-side per-object-key mutation record that makes authenticated plaintext
/// rollback-proof. Metadata alone cannot do it: an attacker who can write the bucket can
/// restore a whole old object, tags included.
///
/// EVERY mutation of a key (sealed put, plaintext put, delete) goes through `begin` (write
/// ahead: allocate a fresh counter and record it as pending BEFORE the backend operation) and
/// then `commit` (after it succeeded). A plaintext-tagged object is served only if its counter
/// equals the entry's `current` (or the recorded `pending` one, adopted after a crash between
/// the backend put and the commit). A missing entry means refuse -- never "counter 0".
///
/// Requirements for a production implementation: durable (DB table or Valkey with
/// `noeviction` + persistence -- an evicted/lost entry makes plaintext-tagged objects
/// unreadable, which fails closed), and `begin` must be atomic per key (INCR / row lock).
/// `MemoryPlainLedger` exists only behind the `dev-memory-ledger` feature (and in tests).
pub trait PlainLedger: Send + Sync {
    /// Atomically allocates the next counter for `object_key` and records it as pending.
    fn begin<'a>(&'a self, object_key: &'a str) -> BoxFuture<'a, Result<u64, StorageError>>;
    /// Marks mutation `counter` done: `plain` = the live object is authenticated plaintext
    /// with that counter, otherwise (sealed put / delete) `current` becomes 0.
    fn commit<'a>(
        &'a self,
        object_key: &'a str,
        counter: u64,
        plain: bool,
    ) -> BoxFuture<'a, Result<(), StorageError>>;
    /// The entry for `object_key`, `None` if the ledger has never seen it (or lost it).
    fn entry<'a>(
        &'a self,
        object_key: &'a str,
    ) -> BoxFuture<'a, Result<Option<LedgerEntry>, StorageError>>;
}

/// In-process [`PlainLedger`] for tests and single-process dev ONLY.
#[cfg(any(test, feature = "dev-memory-ledger"))]
#[derive(Default)]
pub struct MemoryPlainLedger(
    std::sync::Mutex<std::collections::HashMap<String, (u64, LedgerEntry)>>,
);

#[cfg(any(test, feature = "dev-memory-ledger"))]
impl MemoryPlainLedger {
    /// Test seam: forgets `object_key` (simulates eviction / loss).
    pub fn forget(&self, object_key: &str) {
        if let Ok(mut m) = self.0.lock() {
            m.remove(object_key);
        }
    }
}

#[cfg(any(test, feature = "dev-memory-ledger"))]
impl PlainLedger for MemoryPlainLedger {
    fn begin<'a>(&'a self, object_key: &'a str) -> BoxFuture<'a, Result<u64, StorageError>> {
        Box::pin(async move {
            let mut m = self
                .0
                .lock()
                .map_err(|_| StorageError::Backend("ledger poisoned".into()))?;
            let slot = m.entry(object_key.to_string()).or_insert((
                0,
                LedgerEntry {
                    current: 0,
                    pending: 0,
                },
            ));
            slot.0 += 1;
            slot.1.pending = slot.0;
            Ok(slot.0)
        })
    }

    fn commit<'a>(
        &'a self,
        object_key: &'a str,
        counter: u64,
        plain: bool,
    ) -> BoxFuture<'a, Result<(), StorageError>> {
        Box::pin(async move {
            let mut m = self
                .0
                .lock()
                .map_err(|_| StorageError::Backend("ledger poisoned".into()))?;
            let slot = m.get_mut(object_key).ok_or_else(|| {
                StorageError::Backend("ledger commit without a begun mutation".into())
            })?;
            if counter >= slot.1.current {
                slot.1.current = if plain { counter } else { 0 };
            }
            if slot.1.pending == counter {
                slot.1.pending = 0;
            }
            Ok(())
        })
    }

    fn entry<'a>(
        &'a self,
        object_key: &'a str,
    ) -> BoxFuture<'a, Result<Option<LedgerEntry>, StorageError>> {
        Box::pin(async move {
            let m = self
                .0
                .lock()
                .map_err(|_| StorageError::Backend("ledger poisoned".into()))?;
            Ok(m.get(object_key).map(|(_, e)| *e))
        })
    }
}

/// How many DEK versions back an authenticated-plaintext tag stays verifiable (the rotation
/// window); older tags are refused until the object is rewritten.
pub const PLAIN_MAC_VERSION_WINDOW: u32 = 1;

/// Sealing configuration of a [`crate::StorageHost`].
#[derive(Clone)]
pub struct ObjectSealing {
    crypto: TenantCrypto,
    gate: Arc<dyn SealGate>,
    /// Unix-seconds cutoff for serving legacy (pre-encryption, unauthenticated) plaintext;
    /// `None` (default) = never.
    legacy_plaintext_until: Option<u64>,
    ledger: Option<Arc<dyn PlainLedger>>,
}

impl ObjectSealing {
    /// Seals every write with `crypto`; legacy plaintext reads allowed (migration window).
    #[must_use]
    pub fn new(crypto: TenantCrypto) -> Self {
        Self {
            crypto,
            gate: Arc::new(AlwaysSeal),
            legacy_plaintext_until: None,
            ledger: None,
        }
    }

    /// Installs the kill-switch gate.
    #[must_use]
    pub fn with_gate(mut self, gate: Arc<dyn SealGate>) -> Self {
        self.gate = gate;
        self
    }

    /// Opt-in, bounded migration window: unauthenticated plaintext objects whose server-side
    /// last-modified is BEFORE `cutoff_unix` (record the instant sealing was first enabled)
    /// are served with a WARN; anything newer is refused. Unset = no such reads.
    #[must_use]
    pub fn with_legacy_plaintext_until(mut self, cutoff_unix: u64) -> Self {
        self.legacy_plaintext_until = Some(cutoff_unix);
        self
    }

    /// Installs the rollback ledger. Required for kill-switch plaintext writes: without one
    /// they are refused (fail closed) because they could not be rollback-protected.
    #[must_use]
    pub fn with_plain_ledger(mut self, ledger: Arc<dyn PlainLedger>) -> Self {
        self.ledger = Some(ledger);
        self
    }

    pub(crate) async fn writes_sealed(&self) -> bool {
        self.gate.seal_writes().await
    }

    /// Seals `body` for the object at `(tenant, community, owner, key)`.
    pub(crate) async fn seal(
        &self,
        tenant: u64,
        community: u64,
        owner: &str,
        key: &str,
        body: &[u8],
    ) -> Result<Bytes, StorageError> {
        let (t, c) = (tenant.to_string(), community.to_string());
        self.crypto
            .seal(SURFACE, &t, &[&t, &c, owner, SURFACE, key], body)
            .await
            .map(Bytes::from)
            .map_err(map_err)
    }

    /// Write-ahead step of EVERY mutation (sealed put, plaintext put, delete): allocates and
    /// records the pending counter before the backend operation. `None` without a ledger.
    pub(crate) async fn begin_mutation(&self, full_key: &str) -> Result<Option<u64>, StorageError> {
        match &self.ledger {
            Some(l) => l.begin(full_key).await.map(Some),
            None => Ok(None),
        }
    }

    /// Completes a mutation after the backend operation succeeded.
    pub(crate) async fn commit_mutation(
        &self,
        full_key: &str,
        counter: Option<u64>,
        plain: bool,
    ) -> Result<(), StorageError> {
        match (&self.ledger, counter) {
            (Some(l), Some(n)) => l.commit(full_key, n, plain).await,
            _ => Ok(()),
        }
    }

    /// Authenticates a plaintext body written while the kill-switch was on, under the counter
    /// `begin_mutation` allocated. Returns the tag value `v<dek_version>:<counter>:<hex hmac>`;
    /// the MAC binds scope, key, DEK version, the counter and the body. Refused (fail closed)
    /// when no ledger is configured, because the write could not be rollback-protected.
    pub(crate) async fn plain_tag(
        &self,
        (tenant, community): (u64, u64),
        owner: &str,
        key: &str,
        counter: Option<u64>,
        body: &[u8],
    ) -> Result<String, StorageError> {
        let counter = counter.ok_or_else(|| {
            StorageError::Backend(
                "plaintext writes need a rollback ledger; none configured (refusing)".into(),
            )
        })?;
        let (t, c) = (tenant.to_string(), community.to_string());
        let ctr = counter.to_string();
        let (version, mac) = self
            .crypto
            .blind_index_versioned(
                SURFACE,
                &t,
                None,
                &[&t, &c, owner, "object-plain", key, &ctr],
                body,
            )
            .await
            .map_err(map_err)?;
        Ok(format!("v{version}:{counter}:{}", hex(&mac)))
    }

    /// Verifies an authenticated-plaintext tag: version inside the rotation window, MAC valid
    /// under that retained version, counter not older than the ledger's newest (rollback).
    async fn verify_plain(
        &self,
        (tenant, community): (u64, u64),
        owner: &str,
        key: &str,
        full_key: &str,
        claimed: &str,
        body: &[u8],
    ) -> Result<(), StorageError> {
        let bad = || StorageError::Backend("object integrity check failed".into());
        let mut parts = claimed.splitn(3, ':');
        let version: u32 = parts
            .next()
            .and_then(|v| v.strip_prefix('v'))
            .and_then(|v| v.parse().ok())
            .ok_or_else(bad)?;
        let counter: u64 = parts.next().and_then(|v| v.parse().ok()).ok_or_else(bad)?;
        let hex_mac = parts.next().ok_or_else(bad)?;
        let ledger = self.ledger.as_ref().ok_or_else(bad)?;
        let (t, c) = (tenant.to_string(), community.to_string());
        let active = self.crypto.active_dek_version(&t).await.map_err(map_err)?;
        if version == 0 || version > active || active - version > PLAIN_MAC_VERSION_WINDOW {
            return Err(bad());
        }
        let ctr = counter.to_string();
        let (_, mac) = self
            .crypto
            .blind_index_versioned(
                SURFACE,
                &t,
                Some(version),
                &[&t, &c, owner, "object-plain", key, &ctr],
                body,
            )
            .await
            .map_err(map_err)?;
        if !ct_eq(hex_mac.as_bytes(), hex(&mac).as_bytes()) {
            return Err(bad());
        }
        // The ledger decides whether THIS is still the live object. No entry (evicted / lost /
        // never recorded) is an integrity failure, not "counter 0".
        let Some(entry) = ledger.entry(full_key).await? else {
            tracing::error!(
                tenant,
                community,
                "no ledger entry for plaintext object; refusing"
            );
            return Err(bad());
        };
        if counter == entry.current {
            return Ok(());
        }
        if counter == entry.pending && counter > entry.current {
            // Crash between the backend put and the commit: the write-ahead record names this
            // exact counter, so adopt it.
            ledger.commit(full_key, counter, true).await?;
            return Ok(());
        }
        tracing::error!(
            tenant,
            community,
            "stale or superseded plaintext object (rollback); refusing"
        );
        Err(bad())
    }

    /// Opens (or, for authenticated/legacy plaintext, validates) a stored object body.
    pub(crate) async fn open(
        &self,
        tenant: u64,
        community: u64,
        owner: &str,
        key: &str,
        stored: Bytes,
        meta: &ObjectMeta,
    ) -> Result<Bytes, StorageError> {
        let integrity = || StorageError::Backend("object integrity check failed".into());
        if !TenantCrypto::is_sealed(&stored) {
            let tagged_sealed = meta.tags.contains(&system_tag(TAG_SEALED, 1));
            let mac_prefix = format!("{TAG_PLAIN_MAC}=");
            let plain_mac = meta.tags.iter().find_map(|t| t.strip_prefix(&mac_prefix));
            if let Some(claimed) = plain_mac {
                if !tagged_sealed
                    && self
                        .verify_plain(
                            (tenant, community),
                            owner,
                            key,
                            &object_key(owner, tenant, community, key),
                            claimed,
                            &stored,
                        )
                        .await
                        .is_ok()
                {
                    return Ok(stored);
                }
                tracing::error!(
                    tenant,
                    community,
                    "plaintext object failed its MAC/version/rollback check; refusing"
                );
                return Err(integrity());
            }
            if tagged_sealed {
                tracing::error!("tagged-sealed object body is not an envelope; refusing");
                return Err(integrity());
            }
            return match (self.legacy_plaintext_until, meta.modified_unix) {
                (Some(cutoff), Some(modified)) if modified < cutoff => {
                    tracing::warn!(
                        tenant,
                        community,
                        "serving a legacy plaintext bundle object (pre-encryption, inside the operator window)"
                    );
                    Ok(stored)
                }
                _ => {
                    tracing::error!(
                        tenant,
                        community,
                        "unauthenticated plaintext object refused (outside the legacy window)"
                    );
                    Err(StorageError::Backend(
                        "unsealed object refused by policy".into(),
                    ))
                }
            };
        }
        let (t, c) = (tenant.to_string(), community.to_string());
        self.crypto
            .open(SURFACE, &t, &[&t, &c, owner, SURFACE, key], &stored)
            .await
            .map(Bytes::from)
            .map_err(map_err)
    }
}

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

/// Constant-time equality of two byte strings.
fn ct_eq(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

/// Collapses crypto failures to the storage surface: always fail closed, no detail that
/// distinguishes wrong-tenant from tampered.
fn map_err(e: CryptoError) -> StorageError {
    match e {
        CryptoError::KeyUnavailable(_) => {
            StorageError::Backend("tenant data key unavailable".into())
        }
        _ => StorageError::Backend("object integrity check failed".into()),
    }
}

/// Size of the sealed form of a `plain`-byte body.
#[must_use]
pub fn sealed_len(plain: u64) -> u64 {
    plain + OVERHEAD as u64
}

/// True when `body` starts with the sealed-envelope magic.
#[must_use]
pub fn looks_sealed(body: &[u8]) -> bool {
    TenantCrypto::is_sealed(body)
}
