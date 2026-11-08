//! Per-tenant at-rest sealing of `kv` values (spec
//! `docs/superpowers/specs/2026-11-08-tenant-at-rest-encryption.md` section 3).
//!
//! Values are AES-256-GCM sealed under the tenant DEK (via
//! `bundle_host_crypto`); the AAD binds `tenant_id | community_id | app_id |
//! "kv" | guest key | dek_version`, so a ciphertext copied to another key,
//! app, community or tenant fails to open. Guest keys stay plaintext (they
//! are charset-restricted, PII-free by rule, and drive the live index).
//!
//! Rules enforced here:
//! - **Never a plaintext write on key trouble.** A missing/unavailable DEK or
//!   a scope without numeric ids refuses the write with a typed error. The
//!   only plaintext-write path is the operator kill-switch ([`SealingGate`]).
//! - **Reads tolerate legacy plaintext** (values written before sealing
//!   shipped), counted by `bundle_crypto_legacy_plaintext_reads_total`. A
//!   value that *is* an envelope but fails to open is an integrity error.
//! - The CAS digest (`#1139`: `sha1` of the stored value) is computed by the
//!   Lua scripts over whatever is stored, i.e. the ciphertext, so no
//!   plaintext hash is ever exposed and no script change is needed.
//! - `increment` counters stay plaintext integers (`INCRBY` cannot operate on
//!   ciphertext); see `docs/tenant-at-rest-encryption.md`.

use std::sync::Arc;
use std::time::Duration;

use bundle_host_crypto::{metrics as crypto_metrics, CryptoError, TenantCrypto};

use crate::backend::BoxFuture;
use crate::scope::KvScope;
use crate::KvError;

const SURFACE: &str = "kv";

/// Operator kill-switch seam (`waddles.disable-tenant-at-rest-encryption`).
/// Evaluated per write; `true` means "seal" (the default: unseen/OFF flag).
pub trait SealingGate: Send + Sync {
    /// Whether writes must be sealed right now.
    fn seal_writes(&self) -> BoxFuture<'_, bool>;
}

/// Gate that always seals (no kill-switch wired).
pub struct AlwaysSeal;

impl SealingGate for AlwaysSeal {
    fn seal_writes(&self) -> BoxFuture<'_, bool> {
        Box::pin(async { true })
    }
}

/// The crypto handle and kill-switch a [`crate::KvHost`] seals with.
#[derive(Clone)]
pub struct Sealing {
    crypto: TenantCrypto,
    gate: Arc<dyn SealingGate>,
}

impl Sealing {
    /// Builds a sealing handle over `crypto`, gated by `gate`.
    pub fn new(crypto: TenantCrypto, gate: Arc<dyn SealingGate>) -> Self {
        Self { crypto, gate }
    }

    /// Returns the bytes to store for `value`: a sealed envelope, or the
    /// plaintext only when the operator kill-switch is ON.
    pub(crate) async fn seal_for_write(
        &self,
        scope: &KvScope,
        key: &str,
        value: &[u8],
    ) -> Result<Vec<u8>, KvError> {
        if !self.gate.seal_writes().await {
            // Operator-chosen emergency rollback; visible, never automatic.
            tracing::warn!(
                app_id = %scope.app_id,
                "kv value written PLAINTEXT: waddles.disable-tenant-at-rest-encryption is ON"
            );
            crypto_metrics::record_op("seal", SURFACE, "disabled", Duration::ZERO);
            return Ok(value.to_vec());
        }
        let ctx = AadContext::of(scope)?;
        let parts = ctx.parts(&scope.app_id, key);
        self.crypto
            .seal(SURFACE, &ctx.tenant, &parts, value)
            .await
            .map_err(map_err)
    }

    /// Opens a stored value: envelopes are opened (always, regardless of the
    /// kill-switch); anything else is legacy plaintext and passes through.
    pub(crate) async fn open_for_read(
        &self,
        scope: &KvScope,
        key: &str,
        stored: Vec<u8>,
    ) -> Result<Vec<u8>, KvError> {
        if !TenantCrypto::is_sealed(&stored) {
            crypto_metrics::record_legacy_plaintext_read(SURFACE);
            return Ok(stored);
        }
        let ctx = AadContext::of(scope)?;
        let parts = ctx.parts(&scope.app_id, key);
        self.crypto
            .open(SURFACE, &ctx.tenant, &parts, &stored)
            .await
            .map_err(map_err)
    }
}

/// Numeric scope ids rendered once for DEK lookup and AAD binding.
struct AadContext {
    tenant: String,
    community: String,
}

impl AadContext {
    fn of(scope: &KvScope) -> Result<Self, KvError> {
        let (tenant_id, community_id) = scope.ids.ok_or_else(|| {
            KvError::KeyUnavailable(
                "kv scope has no numeric tenant/community ids; refusing to seal or open"
                    .to_string(),
            )
        })?;
        Ok(Self {
            tenant: tenant_id.to_string(),
            community: community_id.to_string(),
        })
    }

    fn parts<'a>(&'a self, app_id: &'a str, key: &'a str) -> [&'a str; 5] {
        [&self.tenant, &self.community, app_id, "kv", key]
    }
}

fn map_err(e: CryptoError) -> KvError {
    match e {
        CryptoError::KeyUnavailable(_) => {
            KvError::KeyUnavailable("tenant data key unavailable".to_string())
        }
        CryptoError::Integrity | CryptoError::Format(_) | CryptoError::NotEnvelope => {
            KvError::Integrity("stored value failed integrity verification".to_string())
        }
    }
}
