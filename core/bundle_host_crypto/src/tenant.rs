//! [`TenantCrypto`]: the dyn-safe seal/open/blind-index facade host
//! capabilities hold. It resolves the tenant DEK through a [`DekProvider`],
//! derives the purpose subkey, and records OTel (no key material, no values).

use std::sync::Arc;
use std::time::Instant;

use crate::envelope::{
    self, blind_index_with_subkey, build_aad, derive_subkey, envelope_dek_version,
    open_with_subkey, seal_with_subkey, Purpose,
};
use crate::error::{CryptoError, DekUnavailableError};
use crate::metrics;
use crate::provider::{BoxFuture, Dek, DekProvider};

/// Dyn-safe view of a [`DekProvider`] (boxed futures).
trait DynDekSource: Send + Sync {
    fn active<'a>(
        &'a self,
        tenant: &'a str,
    ) -> BoxFuture<'a, Result<(Dek, u32), DekUnavailableError>>;
    fn version<'a>(
        &'a self,
        tenant: &'a str,
        version: u32,
    ) -> BoxFuture<'a, Result<Dek, DekUnavailableError>>;
}

impl<D: DekProvider> DynDekSource for D {
    fn active<'a>(
        &'a self,
        tenant: &'a str,
    ) -> BoxFuture<'a, Result<(Dek, u32), DekUnavailableError>> {
        Box::pin(self.get_dek(tenant))
    }

    fn version<'a>(
        &'a self,
        tenant: &'a str,
        version: u32,
    ) -> BoxFuture<'a, Result<Dek, DekUnavailableError>> {
        Box::pin(self.get_dek_version(tenant, version))
    }
}

/// Per-tenant seal/open over a shared DEK provider. Cheap to clone.
#[derive(Clone)]
pub struct TenantCrypto {
    source: Arc<dyn DynDekSource>,
}

impl std::fmt::Debug for TenantCrypto {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("TenantCrypto").finish_non_exhaustive()
    }
}

impl TenantCrypto {
    /// Wraps `provider` (typically a TTL-cached one).
    pub fn new<D: DekProvider + 'static>(provider: D) -> Self {
        Self {
            source: Arc::new(provider),
        }
    }

    /// Seals `plaintext` under the tenant's active DEK. `aad_parts` are the
    /// ordered context fields (tenant, community, app, surface, key...); the
    /// DEK version is appended to the AAD automatically. Fails closed with
    /// [`CryptoError::KeyUnavailable`] -- never a plaintext fallback.
    pub async fn seal(
        &self,
        surface: &'static str,
        tenant_id: &str,
        aad_parts: &[&str],
        plaintext: &[u8],
    ) -> Result<Vec<u8>, CryptoError> {
        let start = Instant::now();
        let result = self.seal_inner(tenant_id, aad_parts, plaintext).await;
        finish("seal", surface, start, &result);
        result
    }

    async fn seal_inner(
        &self,
        tenant_id: &str,
        aad_parts: &[&str],
        plaintext: &[u8],
    ) -> Result<Vec<u8>, CryptoError> {
        let (dek, version) = self.source.active(tenant_id).await?;
        let subkey = derive_subkey(&dek, tenant_id, Purpose::Data)?;
        seal_with_subkey(&subkey, version, &build_aad(aad_parts, version), plaintext)
    }

    /// Opens an envelope using the DEK version named in its header.
    pub async fn open(
        &self,
        surface: &'static str,
        tenant_id: &str,
        aad_parts: &[&str],
        envelope: &[u8],
    ) -> Result<Vec<u8>, CryptoError> {
        let start = Instant::now();
        let result = self.open_inner(tenant_id, aad_parts, envelope).await;
        finish("open", surface, start, &result);
        result
    }

    async fn open_inner(
        &self,
        tenant_id: &str,
        aad_parts: &[&str],
        envelope: &[u8],
    ) -> Result<Vec<u8>, CryptoError> {
        let version = envelope_dek_version(envelope)?;
        let dek = self.source.version(tenant_id, version).await?;
        let subkey = derive_subkey(&dek, tenant_id, Purpose::Data)?;
        open_with_subkey(&subkey, envelope, &build_aad(aad_parts, version))
    }

    /// HMAC blind index of `value` under the tenant's active DEK, bound to
    /// `context` (e.g. community, app, table, column).
    pub async fn blind_index(
        &self,
        surface: &'static str,
        tenant_id: &str,
        context: &[&str],
        value: &[u8],
    ) -> Result<[u8; 32], CryptoError> {
        let start = Instant::now();
        let result = async {
            let (dek, _) = self.source.active(tenant_id).await?;
            let subkey = derive_subkey(&dek, tenant_id, Purpose::BlindIndex)?;
            blind_index_with_subkey(&subkey, context, value)
        }
        .await;
        finish("blind_index", surface, start, &result);
        result
    }

    /// True when `bytes` is a sealed envelope (vs legacy plaintext).
    #[must_use]
    pub fn is_sealed(bytes: &[u8]) -> bool {
        envelope::is_envelope(bytes)
    }
}

fn finish<T>(op: &'static str, surface: &'static str, start: Instant, r: &Result<T, CryptoError>) {
    let label = match r {
        Ok(_) => "ok",
        Err(e) => e.result_label(),
    };
    if let Err(e) = r {
        if matches!(e, CryptoError::KeyUnavailable(_)) {
            metrics::record_key_unavailable(surface);
        }
        // Tenant id and surface only: never the value, key, or AAD parts.
        tracing::warn!(op, surface, result = label, "bundle crypto op failed");
    }
    metrics::record_op(op, surface, label, start.elapsed());
}
