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
//! - there is NO plaintext write path: if sealing is unavailable or switched off
//!   (`waddles.disable-tenant-at-rest-encryption`), object writes are REFUSED (loud error,
//!   `waddles_bundle_storage_denied{reason="sealing_unavailable"}`), never stored unencrypted;
//! - no usable key -> the put/get fails (never a plaintext fallback);
//! - an envelope is authoritative (opened even if the seal tag was stripped); a sealed-tagged
//!   body that is not an envelope is an integrity failure;
//! - plaintext is served ONLY for objects written before sealing existed, inside the
//!   operator-recorded cutoff window (server-assigned last-modified < cutoff; re-writing an
//!   object can only make it newer, so an attacker's replacement is always refused). The
//!   cutoff is opt-in and unset by default: by default NO plaintext is ever served;
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
use crate::tags::{system_tag, TAG_SEALED};

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

/// Sealing configuration of a [`crate::StorageHost`].
#[derive(Clone)]
pub struct ObjectSealing {
    crypto: TenantCrypto,
    gate: Arc<dyn SealGate>,
    /// Unix-seconds cutoff for serving legacy (pre-encryption, unauthenticated) plaintext;
    /// `None` (default) = never.
    legacy_plaintext_until: Option<u64>,
}

impl ObjectSealing {
    /// Seals every write with `crypto`; legacy plaintext reads allowed (migration window).
    #[must_use]
    pub fn new(crypto: TenantCrypto) -> Self {
        Self {
            crypto,
            gate: Arc::new(AlwaysSeal),
            legacy_plaintext_until: None,
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

    /// Opens a stored object body; a non-envelope body is served only as bounded legacy
    /// plaintext (see the module docs).
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
