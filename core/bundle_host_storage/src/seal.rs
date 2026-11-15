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
//! Fail-closed rules: no usable key -> the put/get fails (never a plaintext fallback); a
//! sealed object with sealing unconfigured is refused; a plaintext (pre-encryption) object
//! is served only while `legacy_plaintext_reads_allowed` (operator policy, WARN per read).

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

use bundle_host_crypto::{CryptoError, TenantCrypto, OVERHEAD};
use bytes::Bytes;

use crate::error::StorageError;

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
    legacy_plaintext_reads_allowed: bool,
}

impl ObjectSealing {
    /// Seals every write with `crypto`; legacy plaintext reads allowed (migration window).
    #[must_use]
    pub fn new(crypto: TenantCrypto) -> Self {
        Self {
            crypto,
            gate: Arc::new(AlwaysSeal),
            legacy_plaintext_reads_allowed: true,
        }
    }

    /// Installs the kill-switch gate.
    #[must_use]
    pub fn with_gate(mut self, gate: Arc<dyn SealGate>) -> Self {
        self.gate = gate;
        self
    }

    /// Operator policy for pre-encryption plaintext objects (`false` = refuse them).
    #[must_use]
    pub fn with_legacy_plaintext_reads_allowed(mut self, allowed: bool) -> Self {
        self.legacy_plaintext_reads_allowed = allowed;
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

    /// Opens a stored body. `tagged_sealed` is the host's own sealed tag; an envelope
    /// without the tag is still opened (the envelope is authoritative), and a tagged object
    /// whose body is not an envelope is an integrity failure, never plaintext.
    pub(crate) async fn open(
        &self,
        tenant: u64,
        community: u64,
        owner: &str,
        key: &str,
        stored: Bytes,
        tagged_sealed: bool,
    ) -> Result<Bytes, StorageError> {
        if !TenantCrypto::is_sealed(&stored) {
            if tagged_sealed {
                tracing::error!("tagged-sealed object body is not an envelope; refusing");
                return Err(StorageError::Backend(
                    "object integrity check failed".into(),
                ));
            }
            if self.legacy_plaintext_reads_allowed {
                tracing::warn!(
                    tenant,
                    community,
                    "serving a legacy plaintext bundle object (pre-encryption)"
                );
                return Ok(stored);
            }
            return Err(StorageError::Backend(
                "unsealed object refused by policy".into(),
            ));
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
