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
//! - **Legacy plaintext reads are a migration allowance, not a default
//!   trust.** A stored value that is not an envelope is accepted only when
//!   (a) the operator allows it (`legacy_plaintext_reads_allowed`, ON only
//!   during migration, planned flip to OFF) **and** (b) the scope has no
//!   *sealed-since* marker (`bundlekv:...:sealed`, persisted in Valkey the
//!   first time the scope gets a sealed write, so a restart cannot forget
//!   it). Otherwise it is [`KvError::Integrity`]: anyone able to write raw
//!   Valkey could otherwise plant plaintext that is silently accepted. Every
//!   accepted legacy read logs WARN (app id + key hash, never key or value)
//!   and bumps `bundle_crypto_legacy_plaintext_reads_total`. While the
//!   operator kill-switch is ON (emergency plaintext rollback) plaintext is
//!   by definition in use, so reads accept it.
//! - A value that *is* an envelope but fails to open is an integrity error.
//! - `increment` counters are sealed too: a Lua compare-and-set loop reads,
//!   opens, adds and re-seals (see `KvHost::sealed_increment`). A canonical
//!   plaintext integer (a pre-sealing `INCRBY` counter) is tolerated only
//!   while `legacy_plaintext_reads_allowed` is ON and is replaced by a sealed
//!   value on the next increment.
//! - The CAS digest (`#1139`: `sha1` of the stored value) is computed by the
//!   Lua scripts over whatever is stored, i.e. the ciphertext, so no
//!   plaintext hash is ever exposed and no script change is needed.

use std::collections::HashSet;
use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;

use bundle_host_crypto::{metrics as crypto_metrics, CryptoError, TenantCrypto};

use crate::backend::{BoxFuture, KvBackend};
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

/// The crypto handle, kill-switch and legacy-read policy a
/// [`crate::KvHost`] seals with.
#[derive(Clone)]
pub struct Sealing {
    crypto: TenantCrypto,
    gate: Arc<dyn SealingGate>,
    legacy_plaintext_reads_allowed: bool,
    /// Scope prefixes already known (this process) to carry the persisted
    /// sealed-since marker, so the marker is written once, not per write.
    marked: Arc<Mutex<HashSet<String>>>,
}

impl Sealing {
    /// Builds a sealing handle over `crypto`, gated by `gate`. Legacy
    /// plaintext reads default to **allowed** (migration window); flip with
    /// [`Self::with_legacy_plaintext_reads_allowed`] once migrated.
    pub fn new(crypto: TenantCrypto, gate: Arc<dyn SealingGate>) -> Self {
        Self {
            crypto,
            gate,
            legacy_plaintext_reads_allowed: true,
            marked: Arc::new(Mutex::new(HashSet::new())),
        }
    }

    /// Sets the operator `legacy_plaintext_reads_allowed` policy.
    #[must_use]
    pub fn with_legacy_plaintext_reads_allowed(mut self, allowed: bool) -> Self {
        self.legacy_plaintext_reads_allowed = allowed;
        self
    }

    fn is_marked_locally(&self, marker_key: &str) -> bool {
        self.marked
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .contains(marker_key)
    }

    fn remember_marked(&self, marker_key: &str) {
        self.marked
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .insert(marker_key.to_string());
    }

    /// Returns the bytes to store for `value`: a sealed envelope, or the
    /// plaintext only when the operator kill-switch is ON. A sealed write
    /// first persists the scope's sealed-since marker; if that fails the
    /// write is refused.
    pub(crate) async fn seal_for_write<B: KvBackend>(
        &self,
        backend: &B,
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
        let sealed = self
            .crypto
            .seal(SURFACE, &ctx.tenant, &parts, value)
            .await
            .map_err(map_err)?;
        let marker_key = scope.sealed_marker_key();
        if !self.is_marked_locally(&marker_key) {
            backend
                .mark_sealed(&marker_key)
                .await
                .map_err(KvError::Backend)?;
            self.remember_marked(&marker_key);
        }
        Ok(sealed)
    }

    /// Opens a stored value: envelopes are opened (always, regardless of the
    /// kill-switch or legacy policy); a non-envelope is legacy plaintext and
    /// passes through only if the legacy policy allows it for this scope.
    pub(crate) async fn open_for_read<B: KvBackend>(
        &self,
        backend: &B,
        scope: &KvScope,
        key: &str,
        stored: Vec<u8>,
    ) -> Result<Vec<u8>, KvError> {
        if TenantCrypto::is_sealed(&stored) {
            let ctx = AadContext::of(scope)?;
            let parts = ctx.parts(&scope.app_id, key);
            return self
                .crypto
                .open(SURFACE, &ctx.tenant, &parts, &stored)
                .await
                .map_err(map_err);
        }
        if self.legacy_plaintext_reads_allowed && is_counter_value(&stored) {
            // A pre-sealing plaintext `INCRBY` counter: tolerated while the
            // operator allows legacy reads (no marker check, so counters
            // written before the scope's first sealed write stay readable)
            // and migrated to a sealed value by the next `increment`.
            crypto_metrics::record_legacy_plaintext_read(SURFACE);
            tracing::warn!(
                app_id = %scope.app_id,
                key_hash = %crate::key_hash(key),
                "kv read returned a LEGACY PLAINTEXT counter (not sealed)"
            );
            return Ok(stored);
        }
        self.admit_legacy_plaintext(backend, scope, key).await?;
        crypto_metrics::record_legacy_plaintext_read(SURFACE);
        tracing::warn!(
            app_id = %scope.app_id,
            key_hash = %crate::key_hash(key),
            "kv read returned a LEGACY PLAINTEXT value (not sealed)"
        );
        Ok(stored)
    }

    /// The current integer value of a counter for a sealed `increment`:
    /// absent = 0; a sealed value is opened; a legacy plaintext integer is
    /// accepted under the same policy as reads. Anything that is not a
    /// decimal `i64` is a (loud) backend error, like `INCRBY` on a
    /// non-integer.
    pub(crate) async fn read_counter<B: KvBackend>(
        &self,
        backend: &B,
        scope: &KvScope,
        key: &str,
        stored: Option<Vec<u8>>,
    ) -> Result<i64, KvError> {
        let Some(raw) = stored else {
            return Ok(0);
        };
        let plain = self.open_for_read(backend, scope, key, raw).await?;
        std::str::from_utf8(&plain)
            .ok()
            .and_then(|s| s.parse::<i64>().ok())
            .ok_or_else(|| KvError::Backend("value is not an integer or out of range".to_string()))
    }

    /// Decides whether a non-envelope value may be returned as legacy
    /// plaintext; every refusal is an integrity error, never a silent pass.
    async fn admit_legacy_plaintext<B: KvBackend>(
        &self,
        backend: &B,
        scope: &KvScope,
        key: &str,
    ) -> Result<(), KvError> {
        if !self.gate.seal_writes().await {
            return Ok(());
        }
        let refuse = |why: &str| {
            crypto_metrics::record_op("open", SURFACE, "plaintext_refused", Duration::ZERO);
            tracing::warn!(
                app_id = %scope.app_id,
                key_hash = %crate::key_hash(key),
                reason = why,
                "kv plaintext value refused: integrity failure"
            );
            Err(KvError::Integrity(
                "stored value is not sealed and plaintext is not accepted for this scope"
                    .to_string(),
            ))
        };
        if !self.legacy_plaintext_reads_allowed {
            return refuse("legacy_plaintext_reads_allowed=false");
        }
        let marker_key = scope.sealed_marker_key();
        if self.is_marked_locally(&marker_key) {
            return refuse("scope has a sealed-since marker");
        }
        let marked = backend
            .get(&marker_key)
            .await
            .map_err(KvError::Backend)?
            .is_some();
        if marked {
            self.remember_marked(&marker_key);
            return refuse("scope has a sealed-since marker");
        }
        Ok(())
    }
}

/// True for a canonical decimal integer (what `INCRBY` leaves behind).
fn is_counter_value(bytes: &[u8]) -> bool {
    let digits = bytes.strip_prefix(b"-").unwrap_or(bytes);
    !digits.is_empty()
        && digits.len() <= 20
        && digits.iter().all(u8::is_ascii_digit)
        && (digits.len() == 1 || digits[0] != b'0')
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

#[cfg(test)]
mod counter_tests {
    use super::is_counter_value;

    #[test]
    fn counter_detection_is_strict() {
        for ok in ["0", "7", "-3", "1234567890"] {
            assert!(is_counter_value(ok.as_bytes()), "{ok}");
        }
        for bad in [
            "",
            "-",
            "01",
            "1.5",
            "12a",
            "{\"a\":1}",
            "123456789012345678901",
        ] {
            assert!(!is_counter_value(bad.as_bytes()), "{bad}");
        }
    }
}
