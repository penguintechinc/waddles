//! The bundle `kv` host capability (`wit/waddle-bundle/stage.wit`
//! `interface kv`: `get`/`set`/`delete`/`increment`), shared by
//! `core/svc_process` and `core/svc_action` so the security-critical parts
//! -- key derivation, isolation, and quota enforcement -- exist in exactly
//! one place rather than two copies that could quietly drift apart.
//!
//! **Security model.** This capability's own manifest-declared-capability
//! gate is deliberately thin today (`crate::authorize::authorize_kv`) --
//! see that module's doc for why and for what replaces it. The actual
//! security boundary for a multi-tenant deployment running untrusted
//! vendor bundles is [`scope::KvScope`]: every Valkey key a guest can ever
//! read, write, or delete is namespaced under a prefix built exclusively
//! from the invocation's own authenticated `(tenant, community, app_id)`,
//! never from anything the guest supplies. The only guest-controlled input
//! that ever reaches a Valkey key is the trailing "guest key" segment,
//! which [`scope::validate_guest_key`] restricts to a charset that
//! excludes `:` (the namespace separator this whole scheme relies on) and
//! every glob metacharacter -- so a guest cannot construct a key that
//! escapes its own app's prefix, by construction, not by convention.
//!
//! **Usage.** Construct one [`KvHost`] per Valkey connection (production:
//! `redis::aio::MultiplexedConnection`, already opened by each stage for
//! `relay`/usage metering) and call its four methods with the invocation's
//! [`scope::KvScope`] and the host-API `call_id`
//! (`penguin_bundle_host::wire::HostCallBody::call_id`) every host-call
//! already carries -- see `core/svc_process::capabilities` and
//! `core/svc_action::capabilities`'s `handle_kv` for the call sites.

pub mod authorize;
pub mod backend;
mod limits;
mod metrics;
pub mod policy;
pub mod scope;

use std::sync::Arc;
use std::time::{Duration, Instant};

use sha2::{Digest, Sha256};

pub use authorize::CapabilitySnapshot;
pub use backend::{
    BoxFuture, CasDelete, CasOutcome, KvBackend, QuotaOutcome, ReconcileOutcome, VERSION_SCHEME,
};
pub use limits::{
    LIST_SCAN_CAP, MAX_BYTES_PER_INVOKE, MAX_GUEST_KEY_LEN, MAX_KEYS_PER_APP, MAX_LISTS_PER_INVOKE,
    MAX_LIST_LIMIT, MAX_OPS_PER_INVOKE, MAX_TTL_SECONDS, MAX_VALUE_BYTES,
};
pub use scope::KvScope;

/// The `kv` capability's error surface. Deliberately richer than
/// `stage.wit`'s two-variant `kv.error` (`too-large`/`backend`) --
/// [`KvError::wire_code`]/[`KvError::wire_message`] collapse to that exact
/// contract at the host-call boundary, while the extra variants give
/// `core/svc_process`/`core/svc_action`'s own logs and
/// [`metrics::record_error`]'s `kind` label a precise reason without
/// string-matching a message.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum KvError {
    /// The `kv` capability is not granted to this app
    /// ([`authorize::authorize_kv`] denied it).
    #[error("kv not granted: {0}")]
    NotGranted(String),
    /// The guest key failed [`scope::validate_guest_key`]'s charset check
    /// (length violations use [`KvError::TooLarge`] instead, matching the
    /// WIT variant a bundle SDK already knows how to render).
    #[error("invalid key: {0}")]
    InvalidKey(String),
    /// A key or value exceeded its size quota. Carries the offending size
    /// in bytes -- maps 1:1 onto `kv.error::too-large(u64)`.
    #[error("too large: {0} bytes")]
    TooLarge(u64),
    /// [`MAX_KEYS_PER_APP`] would be exceeded by admitting a new key.
    #[error("quota exceeded: {0}")]
    QuotaExceeded(String),
    /// [`MAX_OPS_PER_INVOKE`] was exceeded by this invocation.
    #[error("rate limited: {0}")]
    RateLimited(String),
    /// [`backend::ReconcileOutcome::Locked`]: the live index was missing and
    /// another caller already holds the reconciliation lock -- fail
    /// closed rather than guess a count (low-severity fix, PR #425
    /// security review: live-index eviction self-heal).
    #[error("quota reconciliation in progress: {0}")]
    Reconciling(String),
    /// Valkey itself failed (network, protocol, script error).
    #[error("backend error: {0}")]
    Backend(String),
}

impl KvError {
    /// The `stage.wit` `kv.error` variant this collapses to at the
    /// host-call boundary: `"too_large"` reaches the guest as
    /// `kv.error::too-large`, everything else as `kv.error::backend`
    /// (`core/bundle_executor::host::imports::kv_error_from` only special-
    /// cases the `"too_large"` code) -- but see [`Self::code`] for the
    /// finer-grained string this crate's own logs/metrics use instead.
    pub fn wire_code(&self) -> &'static str {
        match self {
            KvError::TooLarge(_) => "too_large",
            _ => "backend",
        }
    }

    /// The exact string a `denied(code, message)` host-call error should
    /// carry as its `message` -- for [`KvError::TooLarge`] this is the
    /// decimal byte count, which `kv_error_from` parses back into the
    /// `u64` the WIT variant carries; for every other variant it is a
    /// human-readable reason (never a raw key or value).
    pub fn wire_message(&self) -> String {
        match self {
            KvError::TooLarge(n) => n.to_string(),
            KvError::NotGranted(m)
            | KvError::InvalidKey(m)
            | KvError::QuotaExceeded(m)
            | KvError::RateLimited(m)
            | KvError::Reconciling(m)
            | KvError::Backend(m) => m.clone(),
        }
    }

    /// A stable, fine-grained reason string for logs and the
    /// `waddles_bundle_kv_op_errors_total{kind=...}` metric -- distinct
    /// from [`Self::wire_code`], which only distinguishes what the *guest*
    /// can observe (two variants), not what operators need to triage.
    pub fn code(&self) -> &'static str {
        match self {
            KvError::NotGranted(_) => "not_granted",
            KvError::InvalidKey(_) => "invalid_key",
            KvError::TooLarge(_) => "too_large",
            KvError::QuotaExceeded(_) => "quota_exceeded",
            KvError::RateLimited(_) => "rate_limited",
            KvError::Reconciling(_) => "reconciling",
            KvError::Backend(_) => "backend",
        }
    }
}

fn validate_key(key: &str) -> Result<(), KvError> {
    scope::validate_guest_key(key).map_err(|e| match e {
        scope::KeyValidationError::TooLong(n) => KvError::TooLarge(n),
        scope::KeyValidationError::InvalidChars => {
            KvError::InvalidKey("key contains characters outside [A-Za-z0-9_.-]".to_string())
        }
    })
}

/// First 16 hex chars of `sha256(key)` -- enough to distinguish keys in a
/// DEBUG log without ever printing (or letting an operator infer) the raw
/// guest key (task requirement: "Logs with no values or keys at INFO; hash
/// keys at DEBUG").
fn key_hash(key: &str) -> String {
    let digest = Sha256::digest(key.as_bytes());
    digest.iter().take(8).map(|b| format!("{b:02x}")).collect()
}

/// Emits the shared latency/error/log tail every `kv` op ends with,
/// regardless of which of the four ops ran or how it turned out. Never
/// includes the raw guest key or value at any level (task requirement).
fn finish<T>(
    op: &'static str,
    scope: &KvScope,
    key_hash: &str,
    elapsed: Duration,
    result: &Result<T, KvError>,
) {
    let outcome = if result.is_ok() { "ok" } else { "error" };
    metrics::record_op_duration(op, outcome, elapsed.as_secs_f64());

    let community = scope.community.as_deref().unwrap_or("");
    match result {
        Ok(_) => {
            tracing::info!(
                tenant = %scope.tenant,
                community,
                app_id = %scope.app_id,
                op,
                permission = authorize::KV_PERMISSION_ID,
                "bundle kv op"
            );
            tracing::debug!(
                tenant = %scope.tenant,
                community,
                app_id = %scope.app_id,
                op,
                key_hash,
                "bundle kv op detail"
            );
        }
        Err(err) => {
            let kind = err.code();
            metrics::record_error(op, kind);
            match err {
                KvError::QuotaExceeded(_) => metrics::record_quota_rejection(op, "key_count"),
                KvError::RateLimited(_) => metrics::record_quota_rejection(op, "rate_limit"),
                _ => {}
            }
            tracing::info!(
                tenant = %scope.tenant,
                community,
                app_id = %scope.app_id,
                op,
                permission = authorize::KV_PERMISSION_ID,
                error_kind = kind,
                "bundle kv op failed"
            );
            tracing::debug!(
                tenant = %scope.tenant,
                community,
                app_id = %scope.app_id,
                op,
                key_hash,
                error_kind = kind,
                "bundle kv op failed detail"
            );
        }
    }
}

/// Longest error detail written to a log line (a backend error can embed a long server reply).
const MAX_ERROR_DETAIL_CHARS: usize = 300;

/// The operator-facing detail of a failed kv call: the [`KvError`] message with control
/// characters stripped and length capped. Never contains the guest key or value (every
/// `KvError` message is built without them) and never a connection URL or credential.
pub fn error_detail(err: &KvError) -> String {
    err.to_string()
        .chars()
        .map(|c| if c.is_control() { ' ' } else { c })
        .take(MAX_ERROR_DETAIL_CHARS)
        .collect()
}

/// Reports a failed kv host call from the invoking stage: ERROR log with the underlying
/// error kind + message and `waddles_bundle_kv_errors_total{stage,op,kind}`.
///
/// Called by `core/svc_process` and `core/svc_action` (`stage` = `"process"`/`"action"`)
/// because the guest only ever sees the collapsed wire error, and a bundle's own log line
/// ("`count.kv_failure`") carries none of the host-side reason -- before this, a kv outage
/// was a single INFO line with a bare kind. Guest-caused rejections (`invalid_key`,
/// `too_large`, `quota_exceeded`, `rate_limited`) log at WARN so a chatty bundle cannot
/// flood ERROR; everything the operator must act on (`not_granted`, `backend`,
/// `reconciling`) logs at ERROR. No key, value or secret is ever included.
pub fn report_failure(stage: &'static str, op: &str, scope: &KvScope, err: &KvError) {
    let kind = err.code();
    metrics::record_stage_error(stage, op, kind);
    let community = scope.community.as_deref().unwrap_or("");
    let detail = error_detail(err);
    let op = metrics::normalise_op(op);
    match err {
        KvError::InvalidKey(_)
        | KvError::TooLarge(_)
        | KvError::QuotaExceeded(_)
        | KvError::RateLimited(_) => tracing::warn!(
            stage,
            tenant = %scope.tenant,
            community,
            app_id = %scope.app_id,
            op,
            error_kind = kind,
            error = %detail,
            "bundle kv call rejected"
        ),
        KvError::NotGranted(_) | KvError::Reconciling(_) | KvError::Backend(_) => {
            tracing::error!(
                stage,
                tenant = %scope.tenant,
                community,
                app_id = %scope.app_id,
                op,
                error_kind = kind,
                error = %detail,
                "bundle kv call failed"
            );
        }
    }
}

/// The `kv` host capability, generic over [`KvBackend`] so production
/// (`redis::aio::MultiplexedConnection`) and unit tests (an in-memory
/// fake) share every byte of orchestration logic below.
pub struct KvHost<B: KvBackend> {
    backend: B,
    /// The manifest-declared-capability snapshot [`authorize::authorize_kv`]
    /// checks -- see that module's doc for where it comes from and why
    /// "undeclared means denied" is the default for any `app_id` it has
    /// never been told about.
    capabilities: Arc<CapabilitySnapshot>,
}

impl<B: KvBackend> KvHost<B> {
    pub fn new(backend: B, capabilities: Arc<CapabilitySnapshot>) -> Self {
        Self {
            backend,
            capabilities,
        }
    }

    /// Self-heal, low-severity fix (PR #425 security review): if the live
    /// index is missing (e.g. evicted under an `allkeys-*`
    /// `maxmemory-policy`, `crate::policy`'s doc, or not yet built after an
    /// upgrade from the old counter), rebuilds it from a bounded `SCAN`
    /// before the caller's quota-checked write proceeds.
    /// Fails closed (denies the write) if another caller already holds
    /// the reconciliation lock, rather than racing a second `SCAN` or
    /// guessing a count.
    async fn ensure_count_reconciled(&self, scope: &KvScope) -> Result<(), KvError> {
        match self
            .backend
            .reconcile_count_if_missing(
                &scope.live_key(),
                &scope.data_scan_pattern(),
                &scope.reconcile_lock_key(),
                limits::RECONCILE_LOCK_TTL_MS,
                limits::RECONCILE_SCAN_LIMIT,
            )
            .await
            .map_err(KvError::Backend)?
        {
            ReconcileOutcome::AlreadyPresent => Ok(()),
            ReconcileOutcome::Reconciled(count) => {
                tracing::warn!(
                    tenant = %scope.tenant,
                    community = scope.community.as_deref().unwrap_or(""),
                    app_id = %scope.app_id,
                    reconciled_count = count,
                    "bundle kv: live key index was missing (evicted or pre-upgrade), reconciled via SCAN"
                );
                Ok(())
            }
            ReconcileOutcome::Locked => Err(KvError::Reconciling(format!(
                "app {} kv key-count is being reconciled; retry shortly",
                scope.app_id
            ))),
        }
    }

    async fn check_rate(&self, scope: &KvScope, call_id: u64) -> Result<(), KvError> {
        let rate_key = scope.rate_key(call_id);
        let count = self
            .backend
            .increment_rate(&rate_key, limits::RATE_LIMIT_WINDOW_SECONDS)
            .await
            .map_err(KvError::Backend)?;
        if count > u64::from(limits::MAX_OPS_PER_INVOKE) {
            return Err(KvError::RateLimited(format!(
                "invocation exceeded {} kv ops",
                limits::MAX_OPS_PER_INVOKE
            )));
        }
        Ok(())
    }

    /// Charges `bytes` (key + value bytes moved) against this invocation's
    /// byte budget; over [`limits::MAX_BYTES_PER_INVOKE`] is `RateLimited`.
    /// Together with the op ceiling this is the per-invocation brake: a flat
    /// op count alone either starves legitimate many-small-op bundles or lets a
    /// few huge ops through.
    async fn charge_bytes(
        &self,
        scope: &KvScope,
        call_id: u64,
        bytes: usize,
    ) -> Result<(), KvError> {
        let total = self
            .backend
            .add_budget(
                &scope.budget_key(call_id),
                limits::RATE_LIMIT_WINDOW_SECONDS,
                bytes as u64,
            )
            .await
            .map_err(KvError::Backend)?;
        if total > limits::MAX_BYTES_PER_INVOKE {
            return Err(KvError::RateLimited(format!(
                "invocation exceeded {} kv bytes",
                limits::MAX_BYTES_PER_INVOKE
            )));
        }
        Ok(())
    }

    /// `stage.wit` `kv.get`.
    pub async fn get(
        &self,
        scope: &KvScope,
        call_id: u64,
        key: &str,
    ) -> Result<Option<Vec<u8>>, KvError> {
        let start = Instant::now();
        let result = self.get_inner(scope, call_id, key).await;
        finish("get", scope, &key_hash(key), start.elapsed(), &result);
        result
    }

    async fn get_inner(
        &self,
        scope: &KvScope,
        call_id: u64,
        key: &str,
    ) -> Result<Option<Vec<u8>>, KvError> {
        authorize::authorize_kv(scope, &self.capabilities)
            .map_err(|d| KvError::NotGranted(d.message))?;
        validate_key(key)?;
        self.check_rate(scope, call_id).await?;
        self.charge_bytes(scope, call_id, key.len()).await?;
        let value = self
            .backend
            .get(&scope.data_key(key))
            .await
            .map_err(KvError::Backend)?;
        if let Some(v) = &value {
            self.charge_bytes(scope, call_id, v.len()).await?;
        }
        Ok(value)
    }

    /// `stage.wit` `kv.set`. `ttl_seconds` of `0` means no expiry; a value
    /// above [`MAX_TTL_SECONDS`] is clamped, never rejected (matches the
    /// WIT interface's own doc comment).
    pub async fn set(
        &self,
        scope: &KvScope,
        call_id: u64,
        key: &str,
        value: &[u8],
        ttl_seconds: u32,
    ) -> Result<(), KvError> {
        let start = Instant::now();
        let result = self
            .set_inner(scope, call_id, key, value, ttl_seconds)
            .await;
        finish("set", scope, &key_hash(key), start.elapsed(), &result);
        result
    }

    async fn set_inner(
        &self,
        scope: &KvScope,
        call_id: u64,
        key: &str,
        value: &[u8],
        ttl_seconds: u32,
    ) -> Result<(), KvError> {
        authorize::authorize_kv(scope, &self.capabilities)
            .map_err(|d| KvError::NotGranted(d.message))?;
        validate_key(key)?;
        if value.len() > limits::MAX_VALUE_BYTES {
            return Err(KvError::TooLarge(value.len() as u64));
        }
        self.check_rate(scope, call_id).await?;
        self.charge_bytes(scope, call_id, key.len() + value.len())
            .await?;
        self.ensure_count_reconciled(scope).await?;
        let ttl = limits::clamp_ttl_seconds(ttl_seconds);
        match self
            .backend
            .set_with_quota(
                &scope.data_key(key),
                &scope.live_key(),
                &scope.legacy_count_key(),
                value,
                ttl,
                limits::MAX_KEYS_PER_APP,
            )
            .await
            .map_err(KvError::Backend)?
        {
            QuotaOutcome::Admitted(()) => Ok(()),
            QuotaOutcome::QuotaExceeded => Err(KvError::QuotaExceeded(format!(
                "app already holds the maximum {} live keys",
                limits::MAX_KEYS_PER_APP
            ))),
        }
    }

    /// `stage.wit` `kv.delete`. Deleting an absent key is not an error
    /// (idempotent, matching typical `DEL`-semantics bundle authors expect).
    pub async fn delete(&self, scope: &KvScope, call_id: u64, key: &str) -> Result<(), KvError> {
        let start = Instant::now();
        let result = self.delete_inner(scope, call_id, key).await;
        finish("delete", scope, &key_hash(key), start.elapsed(), &result);
        result
    }

    async fn delete_inner(&self, scope: &KvScope, call_id: u64, key: &str) -> Result<(), KvError> {
        authorize::authorize_kv(scope, &self.capabilities)
            .map_err(|d| KvError::NotGranted(d.message))?;
        validate_key(key)?;
        self.check_rate(scope, call_id).await?;
        self.charge_bytes(scope, call_id, key.len()).await?;
        self.backend
            .delete(
                &scope.data_key(key),
                &scope.live_key(),
                &scope.legacy_count_key(),
            )
            .await
            .map(|_existed| ())
            .map_err(KvError::Backend)
    }

    /// `stage.wit` `kv.increment`. `ttl_seconds` follows the same clamp/
    /// `0`-means-no-expiry rule as [`Self::set`], applied on every call
    /// (not just the first) -- passing `0` clears any TTL a prior call set.
    pub async fn increment(
        &self,
        scope: &KvScope,
        call_id: u64,
        key: &str,
        delta: i64,
        ttl_seconds: u32,
    ) -> Result<i64, KvError> {
        let start = Instant::now();
        let result = self
            .increment_inner(scope, call_id, key, delta, ttl_seconds)
            .await;
        finish("increment", scope, &key_hash(key), start.elapsed(), &result);
        result
    }

    async fn increment_inner(
        &self,
        scope: &KvScope,
        call_id: u64,
        key: &str,
        delta: i64,
        ttl_seconds: u32,
    ) -> Result<i64, KvError> {
        authorize::authorize_kv(scope, &self.capabilities)
            .map_err(|d| KvError::NotGranted(d.message))?;
        validate_key(key)?;
        self.check_rate(scope, call_id).await?;
        self.charge_bytes(scope, call_id, key.len()).await?;
        self.ensure_count_reconciled(scope).await?;
        let ttl = limits::clamp_ttl_seconds(ttl_seconds);
        match self
            .backend
            .increment_with_quota(
                &scope.data_key(key),
                &scope.live_key(),
                &scope.legacy_count_key(),
                delta,
                ttl,
                limits::MAX_KEYS_PER_APP,
            )
            .await
            .map_err(KvError::Backend)?
        {
            QuotaOutcome::Admitted(value) => Ok(value),
            QuotaOutcome::QuotaExceeded => Err(KvError::QuotaExceeded(format!(
                "app already holds the maximum {} live keys",
                limits::MAX_KEYS_PER_APP
            ))),
        }
    }
}

/// One page of a `list` call.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KvListPage {
    /// Guest keys under the prefix, lexicographic.
    pub keys: Vec<String>,
    /// Pass back as `cursor` for the next page; `None` when exhausted.
    pub next_cursor: Option<String>,
}

/// Result of a v2 compare-and-swap (a version conflict is an expected,
/// retryable outcome, not an error).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KvCasResult {
    /// Written; the key's new version.
    Swapped(u64),
    /// Not written; the key's current version (`0` = absent).
    Conflict(u64),
}

/// Result of a v2 conditional delete.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KvDeleteIfResult {
    Deleted,
    /// Not deleted; the key's current version (`0` = absent).
    Conflict(u64),
}

impl<B: KvBackend> KvHost<B> {
    /// kv v2 `get-versioned`: the value plus its version token.
    pub async fn get_versioned(
        &self,
        scope: &KvScope,
        call_id: u64,
        key: &str,
    ) -> Result<Option<(Vec<u8>, u64)>, KvError> {
        let start = Instant::now();
        let result = self.get_versioned_inner(scope, call_id, key).await;
        finish(
            "get_versioned",
            scope,
            &key_hash(key),
            start.elapsed(),
            &result,
        );
        result
    }

    async fn get_versioned_inner(
        &self,
        scope: &KvScope,
        call_id: u64,
        key: &str,
    ) -> Result<Option<(Vec<u8>, u64)>, KvError> {
        authorize::authorize_kv(scope, &self.capabilities)
            .map_err(|d| KvError::NotGranted(d.message))?;
        validate_key(key)?;
        self.check_rate(scope, call_id).await?;
        self.charge_bytes(scope, call_id, key.len()).await?;
        let found = self
            .backend
            .get_versioned(&scope.data_key(key))
            .await
            .map_err(KvError::Backend)?;
        if let Some((v, _)) = &found {
            self.charge_bytes(scope, call_id, v.len()).await?;
        }
        Ok(found)
    }

    /// kv v2 `cas`: write `value` iff the key's version is `expected_version`
    /// (`0` = create only if absent). Atomic inside Valkey.
    pub async fn cas(
        &self,
        scope: &KvScope,
        call_id: u64,
        key: &str,
        expected_version: u64,
        value: &[u8],
        ttl_seconds: u32,
    ) -> Result<KvCasResult, KvError> {
        let start = Instant::now();
        let result = self
            .cas_inner(scope, call_id, key, expected_version, value, ttl_seconds)
            .await;
        finish("cas", scope, &key_hash(key), start.elapsed(), &result);
        result
    }

    async fn cas_inner(
        &self,
        scope: &KvScope,
        call_id: u64,
        key: &str,
        expected_version: u64,
        value: &[u8],
        ttl_seconds: u32,
    ) -> Result<KvCasResult, KvError> {
        authorize::authorize_kv(scope, &self.capabilities)
            .map_err(|d| KvError::NotGranted(d.message))?;
        validate_key(key)?;
        if value.len() > limits::MAX_VALUE_BYTES {
            return Err(KvError::TooLarge(value.len() as u64));
        }
        self.check_rate(scope, call_id).await?;
        self.charge_bytes(scope, call_id, key.len() + value.len())
            .await?;
        self.ensure_count_reconciled(scope).await?;
        let ttl = limits::clamp_ttl_seconds(ttl_seconds);
        match self
            .backend
            .cas_with_quota(
                &scope.data_key(key),
                &scope.live_key(),
                &scope.legacy_count_key(),
                expected_version,
                value,
                ttl,
                limits::MAX_KEYS_PER_APP,
            )
            .await
            .map_err(KvError::Backend)?
        {
            CasOutcome::Swapped(v) => Ok(KvCasResult::Swapped(v)),
            CasOutcome::Conflict(v) => Ok(KvCasResult::Conflict(v)),
            CasOutcome::QuotaExceeded => Err(KvError::QuotaExceeded(format!(
                "app already holds the maximum {} live keys",
                limits::MAX_KEYS_PER_APP
            ))),
        }
    }

    /// kv v2 `delete-if`: delete iff the key's version is `expected_version`.
    pub async fn delete_if(
        &self,
        scope: &KvScope,
        call_id: u64,
        key: &str,
        expected_version: u64,
    ) -> Result<KvDeleteIfResult, KvError> {
        let start = Instant::now();
        let result = self
            .delete_if_inner(scope, call_id, key, expected_version)
            .await;
        finish("delete_if", scope, &key_hash(key), start.elapsed(), &result);
        result
    }

    async fn delete_if_inner(
        &self,
        scope: &KvScope,
        call_id: u64,
        key: &str,
        expected_version: u64,
    ) -> Result<KvDeleteIfResult, KvError> {
        authorize::authorize_kv(scope, &self.capabilities)
            .map_err(|d| KvError::NotGranted(d.message))?;
        validate_key(key)?;
        if expected_version == 0 {
            return Err(KvError::InvalidKey(
                "delete-if needs a real version (>= 1)".to_string(),
            ));
        }
        self.check_rate(scope, call_id).await?;
        self.charge_bytes(scope, call_id, key.len()).await?;
        Ok(
            match self
                .backend
                .delete_if(
                    &scope.data_key(key),
                    &scope.live_key(),
                    &scope.legacy_count_key(),
                    expected_version,
                )
                .await
                .map_err(KvError::Backend)?
            {
                CasDelete::Deleted => KvDeleteIfResult::Deleted,
                CasDelete::Conflict(v) => KvDeleteIfResult::Conflict(v),
            },
        )
    }

    /// kv v2 `list`: keys under `prefix` (may be empty = all), lexicographic,
    /// strictly after `cursor` (a guest key returned by the previous page),
    /// at most `limit` (1..=[`MAX_LIST_LIMIT`]). Only keys are returned (use
    /// `get` for values) so a page's byte cost is bounded by key sizes.
    pub async fn list(
        &self,
        scope: &KvScope,
        call_id: u64,
        prefix: &str,
        cursor: Option<&str>,
        limit: u32,
    ) -> Result<KvListPage, KvError> {
        let start = Instant::now();
        let result = self.list_inner(scope, call_id, prefix, cursor, limit).await;
        finish("list", scope, &key_hash(prefix), start.elapsed(), &result);
        result
    }

    async fn list_inner(
        &self,
        scope: &KvScope,
        call_id: u64,
        prefix: &str,
        cursor: Option<&str>,
        limit: u32,
    ) -> Result<KvListPage, KvError> {
        authorize::authorize_kv(scope, &self.capabilities)
            .map_err(|d| KvError::NotGranted(d.message))?;
        if !prefix.is_empty() {
            validate_key(prefix)?;
        }
        if limit == 0 || limit > limits::MAX_LIST_LIMIT {
            return Err(KvError::InvalidKey(format!(
                "list limit must be 1..={}",
                limits::MAX_LIST_LIMIT
            )));
        }
        let after = match cursor {
            Some(c) => {
                validate_key(c)?;
                if !c.starts_with(prefix) {
                    return Err(KvError::InvalidKey(
                        "cursor does not belong to this prefix".to_string(),
                    ));
                }
                scope.data_key(c)
            }
            None => String::new(),
        };
        self.check_rate(scope, call_id).await?;
        let lists = self
            .backend
            .add_budget(
                &scope.list_budget_key(call_id),
                limits::RATE_LIMIT_WINDOW_SECONDS,
                1,
            )
            .await
            .map_err(KvError::Backend)?;
        if lists > limits::MAX_LISTS_PER_INVOKE {
            return Err(KvError::RateLimited(format!(
                "invocation exceeded {} kv list calls",
                limits::MAX_LISTS_PER_INVOKE
            )));
        }
        self.charge_bytes(scope, call_id, limits::LIST_BASE_COST_BYTES + prefix.len())
            .await?;
        self.ensure_count_reconciled(scope).await?;
        // One extra to learn whether another page follows.
        let mut keys = self
            .backend
            .list_keys(
                &scope.live_key(),
                &scope.data_prefix(prefix),
                &after,
                limit + 1,
            )
            .await
            .map_err(KvError::Backend)?
            .iter()
            .filter_map(|k| scope.guest_key_of(k).map(str::to_string))
            .collect::<Vec<_>>();
        let next_cursor = if keys.len() > limit as usize {
            keys.truncate(limit as usize);
            keys.last().cloned()
        } else {
            None
        };
        self.charge_bytes(scope, call_id, keys.iter().map(String::len).sum())
            .await?;
        Ok(KvListPage { keys, next_cursor })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use backend::fake::FakeBackend;

    fn scope_a() -> KvScope {
        KvScope::new("acme", Some("main".to_string()), "waddles.bot.a")
    }

    fn scope_b_app() -> KvScope {
        KvScope::new("acme", Some("main".to_string()), "waddles.bot.b")
    }

    fn scope_other_tenant() -> KvScope {
        KvScope::new("globex", Some("main".to_string()), "waddles.bot.a")
    }

    /// A [`CapabilitySnapshot`] granting `storage.kv` to every `app_id`
    /// listed -- what a real deployment's `bundle_loader` would have
    /// populated from an approved manifest declaring it. Every test in
    /// this module other than the `authorize_*` ones below is testing
    /// something *other* than the gate itself, so they all grant up front.
    fn granting(app_ids: &[&str]) -> Arc<CapabilitySnapshot> {
        let snapshot = CapabilitySnapshot::new();
        for app_id in app_ids {
            snapshot.update(*app_id, [authorize::KV_PERMISSION_ID.to_string()]);
        }
        Arc::new(snapshot)
    }

    fn host_for(backend: FakeBackend, app_ids: &[&str]) -> KvHost<FakeBackend> {
        KvHost::new(backend, granting(app_ids))
    }

    fn host(app_ids: &[&str]) -> KvHost<FakeBackend> {
        host_for(FakeBackend::new(), app_ids)
    }

    #[tokio::test]
    async fn set_then_get_round_trips_the_value() {
        let host = host(&["waddles.bot.a"]);
        let scope = scope_a();
        host.set(&scope, 1, "counter", b"hello", 0).await.unwrap();
        let got = host.get(&scope, 2, "counter").await.unwrap();
        assert_eq!(got, Some(b"hello".to_vec()));
    }

    #[tokio::test]
    async fn get_of_an_absent_key_is_none_not_an_error() {
        let host = host(&["waddles.bot.a"]);
        let scope = scope_a();
        assert_eq!(host.get(&scope, 1, "absent").await.unwrap(), None);
    }

    #[tokio::test]
    async fn delete_then_get_returns_none() {
        let host = host(&["waddles.bot.a"]);
        let scope = scope_a();
        host.set(&scope, 1, "counter", b"v", 0).await.unwrap();
        host.delete(&scope, 2, "counter").await.unwrap();
        assert_eq!(host.get(&scope, 3, "counter").await.unwrap(), None);
    }

    #[tokio::test]
    async fn delete_of_an_absent_key_is_not_an_error() {
        let host = host(&["waddles.bot.a"]);
        let scope = scope_a();
        host.delete(&scope, 1, "never-existed").await.unwrap();
    }

    #[tokio::test]
    async fn increment_from_absent_starts_at_delta() {
        let host = host(&["waddles.bot.a"]);
        let scope = scope_a();
        let v = host.increment(&scope, 1, "hits", 5, 0).await.unwrap();
        assert_eq!(v, 5);
        let v2 = host.increment(&scope, 2, "hits", 3, 0).await.unwrap();
        assert_eq!(v2, 8);
    }

    #[tokio::test]
    async fn increment_accepts_negative_deltas() {
        let host = host(&["waddles.bot.a"]);
        let scope = scope_a();
        host.increment(&scope, 1, "hits", 10, 0).await.unwrap();
        let v = host.increment(&scope, 2, "hits", -3, 0).await.unwrap();
        assert_eq!(v, 7);
    }

    // -- authorize(): declared -> allowed, undeclared -> denied --
    // (`crate::authorize`'s own tests cover `authorize_kv` directly against
    // a bare `CapabilitySnapshot`; these exercise the identical contract
    // through the full `KvHost` call path.)

    #[tokio::test]
    async fn kv_call_succeeds_when_the_app_declares_storage_kv() {
        let host = host(&["waddles.bot.a"]);
        host.set(&scope_a(), 1, "k", b"v", 0).await.unwrap();
    }

    #[tokio::test]
    async fn kv_call_is_denied_when_the_app_never_declared_storage_kv() {
        let host = host(&[]); // no app_id granted anything
        let err = host.set(&scope_a(), 1, "k", b"v", 0).await.unwrap_err();
        assert_eq!(err.code(), "not_granted");
    }

    #[tokio::test]
    async fn kv_call_is_denied_for_an_app_id_the_snapshot_has_never_seen() {
        // Granting a *different* app_id must not accidentally grant this one.
        let host = host(&["waddles.bot.other"]);
        let err = host.get(&scope_a(), 1, "k").await.unwrap_err();
        assert_eq!(err.code(), "not_granted");
    }

    // -- Isolation: cross-app and cross-tenant --

    #[tokio::test]
    async fn one_app_cannot_read_another_apps_key_in_the_same_community() {
        let host = host(&["waddles.bot.a", "waddles.bot.b"]);
        host.set(&scope_a(), 1, "secret", b"a-only", 0)
            .await
            .unwrap();
        let got = host.get(&scope_b_app(), 1, "secret").await.unwrap();
        assert_eq!(
            got, None,
            "app b must never see app a's value under the same key name"
        );
    }

    #[tokio::test]
    async fn one_app_cannot_delete_another_apps_key() {
        let host = host(&["waddles.bot.a", "waddles.bot.b"]);
        host.set(&scope_a(), 1, "secret", b"a-only", 0)
            .await
            .unwrap();
        host.delete(&scope_b_app(), 1, "secret").await.unwrap();
        assert_eq!(
            host.get(&scope_a(), 2, "secret").await.unwrap(),
            Some(b"a-only".to_vec()),
            "app b's delete must not affect app a's key"
        );
    }

    #[tokio::test]
    async fn same_app_id_in_a_different_tenant_is_a_fully_separate_namespace() {
        let host = host(&["waddles.bot.a"]);
        host.set(&scope_a(), 1, "secret", b"acme-value", 0)
            .await
            .unwrap();
        let got = host.get(&scope_other_tenant(), 1, "secret").await.unwrap();
        assert_eq!(
            got, None,
            "identical app_id in a different tenant must not see acme's value"
        );
    }

    #[tokio::test]
    async fn increment_counters_are_isolated_per_app() {
        let host = host(&["waddles.bot.a", "waddles.bot.b"]);
        host.increment(&scope_a(), 1, "hits", 100, 0).await.unwrap();
        let b = host
            .increment(&scope_b_app(), 1, "hits", 1, 0)
            .await
            .unwrap();
        assert_eq!(
            b, 1,
            "app b's counter must start fresh, unaffected by app a's"
        );
    }

    // -- Key validation / escape attempts --

    #[tokio::test]
    async fn a_key_containing_a_colon_is_rejected_not_silently_namespaced_elsewhere() {
        let host = host(&["waddles.bot.a"]);
        let err = host
            .set(&scope_a(), 1, "waddles.bot.b:data:secret", b"x", 0)
            .await
            .unwrap_err();
        assert_eq!(err.code(), "invalid_key");
    }

    #[tokio::test]
    async fn a_key_that_looks_like_another_apps_full_valkey_key_still_lands_under_the_caller_prefix(
    ) {
        // Even if this were accepted, `KvScope::data_key` always appends
        // it after `bundlekv:{tenant}:{community}:{app_id}:data:`, so it
        // could only ever shadow a key inside the caller's own namespace --
        // but the leading colon is rejected outright regardless.
        let host = host(&["waddles.bot.a"]);
        let err = host
            .set(
                &scope_a(),
                1,
                "bundlekv:acme:main:waddles.bot.b:data:secret",
                b"x",
                0,
            )
            .await
            .unwrap_err();
        assert_eq!(err.code(), "invalid_key");
    }

    #[tokio::test]
    async fn an_oversized_key_is_rejected_as_too_large() {
        let host = host(&["waddles.bot.a"]);
        let long_key = "a".repeat(MAX_GUEST_KEY_LEN + 1);
        let err = host
            .set(&scope_a(), 1, &long_key, b"x", 0)
            .await
            .unwrap_err();
        assert_eq!(err.wire_code(), "too_large");
    }

    // -- Quotas --

    #[tokio::test]
    async fn set_beyond_the_key_count_quota_is_rejected() {
        let backend = FakeBackend::new();
        let scope = scope_a();
        backend.seed_count(&scope.live_key(), MAX_KEYS_PER_APP);
        let host = host_for(backend, &["waddles.bot.a"]);
        let err = host
            .set(&scope, 1, "one-too-many", b"x", 0)
            .await
            .unwrap_err();
        assert_eq!(err.code(), "quota_exceeded");
    }

    #[tokio::test]
    async fn overwriting_an_existing_key_is_exempt_from_the_key_count_quota() {
        let backend = FakeBackend::new();
        let scope = scope_a();
        let host = host_for(backend, &["waddles.bot.a"]);
        host.set(&scope, 1, "existing", b"v1", 0).await.unwrap();
        // Fill the app's index to its ceiling (the existing key counts
        // toward it): the quota is now full.
        host.backend.seed_count(&scope.live_key(), MAX_KEYS_PER_APP);

        // Overwriting the already-live key succeeds despite the quota
        // being full: it never allocates a new slot.
        host.set(&scope, 2, "existing", b"v2", 0).await.unwrap();
        assert_eq!(
            host.get(&scope, 3, "existing").await.unwrap(),
            Some(b"v2".to_vec())
        );

        // A genuinely new key is still rejected under a full quota:
        let err = host.set(&scope, 4, "brand-new", b"x", 0).await.unwrap_err();
        assert_eq!(err.code(), "quota_exceeded");
    }

    #[tokio::test]
    async fn a_value_over_the_size_quota_is_rejected_as_too_large() {
        let host = host(&["waddles.bot.a"]);
        let big = vec![0u8; MAX_VALUE_BYTES + 1];
        let err = host.set(&scope_a(), 1, "k", &big, 0).await.unwrap_err();
        assert_eq!(err, KvError::TooLarge((MAX_VALUE_BYTES + 1) as u64));
    }

    #[tokio::test]
    async fn exceeding_the_per_invocation_op_rate_limit_is_rejected() {
        let host = host(&["waddles.bot.a"]);
        let scope = scope_a();
        let call_id = 42;
        for _ in 0..MAX_OPS_PER_INVOKE {
            host.get(&scope, call_id, "k").await.unwrap();
        }
        let err = host.get(&scope, call_id, "k").await.unwrap_err();
        assert_eq!(err.code(), "rate_limited");
    }

    #[tokio::test]
    async fn the_rate_limit_is_scoped_per_invocation_not_per_app() {
        let host = host(&["waddles.bot.a"]);
        let scope = scope_a();
        for _ in 0..MAX_OPS_PER_INVOKE {
            host.get(&scope, 1, "k").await.unwrap();
        }
        // A fresh call_id (a new invocation) is unaffected by call_id 1's
        // exhausted budget.
        host.get(&scope, 2, "k").await.unwrap();
    }

    // -- TTL expiry frees quota (regression: TTL keys leaked their slot) --

    const TTL_SECONDS: u32 = 60;
    const PAST_TTL: Duration = Duration::from_secs(61);

    /// Writes `count` keys named `{prefix}{i}` with `ttl_seconds`, each under
    /// its own `call_id` so the per-invocation rate limit never interferes.
    async fn fill(
        host: &KvHost<FakeBackend>,
        scope: &KvScope,
        prefix: &str,
        count: u64,
        ttl_seconds: u32,
    ) {
        for i in 0..count {
            host.set(scope, i, &format!("{prefix}{i}"), b"v", ttl_seconds)
                .await
                .unwrap();
        }
    }

    #[tokio::test]
    async fn ttl_keys_that_lapse_stop_counting_against_the_quota() {
        let host = host(&["waddles.bot.a"]);
        let scope = scope_a();
        fill(&host, &scope, "cooldown.", MAX_KEYS_PER_APP, TTL_SECONDS).await;
        assert_eq!(host.backend.live_count(&scope.live_key()), MAX_KEYS_PER_APP);

        // At the ceiling a new key is rejected -- the quota bounds LIVE keys.
        let err = host.set(&scope, 1, "new", b"x", 0).await.unwrap_err();
        assert_eq!(err.code(), "quota_exceeded");

        // Every key lapses: the app is back to zero live keys and a new
        // write is admitted. Before the fix the leaked counter stayed at the
        // ceiling forever and this was rejected.
        host.backend.advance(PAST_TTL);
        assert_eq!(host.backend.live_count(&scope.live_key()), 0);
        host.set(&scope, 2, "new", b"x", 0).await.unwrap();
        assert_eq!(host.backend.live_count(&scope.live_key()), 1);
        assert_eq!(host.get(&scope, 3, "cooldown.0").await.unwrap(), None);
    }

    #[tokio::test]
    async fn the_app_can_cycle_far_more_ttl_keys_than_the_lifetime_ceiling() {
        // 3 full generations of ceiling-sized TTL keys -- 30k lifetime keys
        // through a 10k quota -- only works if lapsed keys free their slots.
        let host = host(&["waddles.bot.a"]);
        let scope = scope_a();
        for generation in 0..3u64 {
            fill(
                &host,
                &scope,
                &format!("gen{generation}."),
                MAX_KEYS_PER_APP,
                TTL_SECONDS,
            )
            .await;
            host.backend.advance(PAST_TTL);
        }
        assert_eq!(host.backend.live_count(&scope.live_key()), 0);
    }

    #[tokio::test]
    async fn only_lapsed_keys_free_slots_permanent_keys_keep_theirs() {
        let host = host(&["waddles.bot.a"]);
        let scope = scope_a();
        let permanent = 4_000;
        fill(&host, &scope, "perm.", permanent, 0).await;
        fill(
            &host,
            &scope,
            "ttl.",
            MAX_KEYS_PER_APP - permanent,
            TTL_SECONDS,
        )
        .await;
        let err = host.set(&scope, 1, "over", b"x", 0).await.unwrap_err();
        assert_eq!(err.code(), "quota_exceeded");

        host.backend.advance(PAST_TTL);
        assert_eq!(host.backend.live_count(&scope.live_key()), permanent);

        // Exactly the freed slots are available again, and no more.
        for i in 0..(MAX_KEYS_PER_APP - permanent) {
            host.set(&scope, i, &format!("again.{i}"), b"x", 0)
                .await
                .unwrap();
        }
        let err = host.set(&scope, 1, "over", b"x", 0).await.unwrap_err();
        assert_eq!(err.code(), "quota_exceeded");
        assert_eq!(host.backend.live_count(&scope.live_key()), MAX_KEYS_PER_APP);
    }

    #[tokio::test]
    async fn live_count_is_exact_across_set_expire_overwrite_and_delete() {
        let host = host(&["waddles.bot.a"]);
        let scope = scope_a();
        let live = scope.live_key();
        let count = || host.backend.live_count(&live);

        host.set(&scope, 1, "short", b"v", 10).await.unwrap();
        host.set(&scope, 2, "perm", b"v", 0).await.unwrap();
        host.set(&scope, 3, "long", b"v", 100).await.unwrap();
        assert_eq!(count(), 3);

        host.backend.advance(Duration::from_secs(11));
        assert_eq!(count(), 2, "the 10s key lapsed");

        // Re-creating a lapsed key counts once -- the phantom of its first
        // life is not carried over and not double-counted.
        host.set(&scope, 4, "short", b"v2", 10).await.unwrap();
        assert_eq!(count(), 3);

        host.delete(&scope, 5, "perm").await.unwrap();
        assert_eq!(count(), 2);
        // Deleting what is already gone (or already lapsed) changes nothing
        // and can never push the count below the true live set.
        host.delete(&scope, 6, "perm").await.unwrap();
        host.delete(&scope, 7, "never-existed").await.unwrap();
        assert_eq!(count(), 2);

        host.backend.advance(Duration::from_secs(200));
        assert_eq!(count(), 0, "every remaining key lapsed");
        host.delete(&scope, 8, "long").await.unwrap();
        host.delete(&scope, 9, "short").await.unwrap();
        assert_eq!(count(), 0);

        // And the app is fully usable again afterwards.
        host.set(&scope, 10, "fresh", b"v", 0).await.unwrap();
        assert_eq!(count(), 1);
    }

    #[tokio::test]
    async fn overwriting_a_ttl_key_refreshes_its_lifetime_without_double_counting() {
        let host = host(&["waddles.bot.a"]);
        let scope = scope_a();
        let live = scope.live_key();

        host.set(&scope, 1, "k", b"v1", 10).await.unwrap();
        host.backend.advance(Duration::from_secs(6));
        host.set(&scope, 2, "k", b"v2", 10).await.unwrap();
        assert_eq!(host.backend.live_count(&live), 1);

        // 12s after the first write: the original TTL would have lapsed,
        // the refreshed one has not.
        host.backend.advance(Duration::from_secs(6));
        assert_eq!(
            host.get(&scope, 3, "k").await.unwrap(),
            Some(b"v2".to_vec())
        );
        assert_eq!(host.backend.live_count(&live), 1);

        host.backend.advance(Duration::from_secs(5));
        assert_eq!(host.get(&scope, 4, "k").await.unwrap(), None);
        assert_eq!(host.backend.live_count(&live), 0);
    }

    #[tokio::test]
    async fn overwriting_with_ttl_zero_makes_the_key_permanent() {
        let host = host(&["waddles.bot.a"]);
        let scope = scope_a();
        host.set(&scope, 1, "k", b"v1", 10).await.unwrap();
        host.set(&scope, 2, "k", b"v2", 0).await.unwrap();

        host.backend.advance(Duration::from_secs(3_600));
        assert_eq!(
            host.get(&scope, 3, "k").await.unwrap(),
            Some(b"v2".to_vec())
        );
        assert_eq!(host.backend.live_count(&scope.live_key()), 1);
    }

    #[tokio::test]
    async fn increment_ttl_keys_free_their_slot_and_restart_from_zero() {
        let host = host(&["waddles.bot.a"]);
        let scope = scope_a();
        let live = scope.live_key();

        assert_eq!(host.increment(&scope, 1, "hits", 5, 5).await.unwrap(), 5);
        assert_eq!(host.increment(&scope, 2, "hits", 2, 5).await.unwrap(), 7);
        assert_eq!(host.backend.live_count(&live), 1);

        host.backend.advance(Duration::from_secs(6));
        assert_eq!(host.backend.live_count(&live), 0);
        assert_eq!(host.increment(&scope, 3, "hits", 1, 5).await.unwrap(), 1);
        assert_eq!(host.backend.live_count(&live), 1);

        // `ttl == 0` clears the TTL on an existing key, applied on every call.
        host.increment(&scope, 4, "hits", 1, 0).await.unwrap();
        host.backend.advance(Duration::from_secs(3_600));
        assert_eq!(host.backend.live_count(&live), 1);
        assert_eq!(host.increment(&scope, 5, "hits", 1, 0).await.unwrap(), 3);
    }

    #[tokio::test]
    async fn a_failed_increment_leaves_the_live_index_untouched() {
        let host = host(&["waddles.bot.a"]);
        let scope = scope_a();
        host.set(&scope, 1, "text", b"not-a-number", 0)
            .await
            .unwrap();
        let before = host.backend.live_count(&scope.live_key());

        let err = host.increment(&scope, 2, "text", 1, 30).await.unwrap_err();
        assert_eq!(err.code(), "backend");
        assert_eq!(host.backend.live_count(&scope.live_key()), before);
    }

    #[tokio::test]
    async fn an_emptied_index_is_not_mistaken_for_an_evicted_one() {
        // Every key lapsed => the index is empty but must still exist (the
        // sentinel's job on real Valkey), so the next write does not
        // trigger a keyspace `SCAN` reconciliation.
        let host = host(&["waddles.bot.a"]);
        let scope = scope_a();
        host.set(&scope, 1, "cooldown", b"v", 5).await.unwrap();
        host.backend.advance(Duration::from_secs(6));
        host.set(&scope, 2, "cooldown", b"v", 5).await.unwrap();
        host.backend.advance(Duration::from_secs(6));
        assert!(host.backend.index_exists(&scope.live_key()));
        assert_eq!(host.backend.live_count(&scope.live_key()), 0);
    }

    #[tokio::test]
    async fn deleting_the_last_key_does_not_make_the_index_look_evicted() {
        let host = host(&["waddles.bot.a"]);
        let scope = scope_a();
        host.set(&scope, 1, "only", b"v", 0).await.unwrap();
        host.delete(&scope, 2, "only").await.unwrap();
        assert!(host.backend.index_exists(&scope.live_key()));
        assert_eq!(host.backend.live_count(&scope.live_key()), 0);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn concurrent_new_keys_never_exceed_the_live_quota() {
        let backend = FakeBackend::new();
        let scope = scope_a();
        let headroom = 25u64;
        backend.seed_count(&scope.live_key(), MAX_KEYS_PER_APP - headroom);
        let host = Arc::new(host_for(backend, &["waddles.bot.a"]));

        let mut tasks = Vec::new();
        for i in 0..200u64 {
            let host = Arc::clone(&host);
            let scope = scope.clone();
            tasks.push(tokio::spawn(async move {
                let ttl = if i % 2 == 0 { 0 } else { TTL_SECONDS };
                host.set(&scope, i, &format!("racer.{i}"), b"v", ttl).await
            }));
        }
        let mut admitted = 0u64;
        for task in tasks {
            match task.await.unwrap() {
                Ok(()) => admitted += 1,
                Err(e) => assert_eq!(e.code(), "quota_exceeded"),
            }
        }
        assert_eq!(admitted, headroom);
        assert_eq!(host.backend.live_count(&scope.live_key()), MAX_KEYS_PER_APP);
    }

    // -- Self-heal: live-index eviction / upgrade reconciliation --

    #[tokio::test]
    async fn set_reconciles_a_missing_live_index_before_admitting_a_new_key() {
        let backend = FakeBackend::new();
        let scope = scope_a();
        // A brand-new app: the live index has never existed at all.
        backend.seed_unindexed(&scope.data_key("pre-existing"), b"v", None);
        let host = host_for(backend, &["waddles.bot.a"]);

        // A brand-new key still succeeds -- reconciliation rebuilds the
        // index from the true live keys (1), well under quota.
        host.set(&scope, 1, "brand-new", b"x", 0).await.unwrap();
        assert_eq!(
            host.get(&scope, 2, "brand-new").await.unwrap(),
            Some(b"x".to_vec())
        );
        assert_eq!(host.backend.live_count(&scope.live_key()), 2);
    }

    #[tokio::test]
    async fn reconcile_rebuilds_an_evicted_index_to_the_true_live_keys() {
        // Direct backend-level test (bypassing KvHost/quota, which can't
        // itself distinguish a reconciled count of 0 vs. 2 without
        // artificially hitting a 10,000-key quota) -- proves
        // `evict_count` (simulating a live index actually lost to Valkey
        // eviction, not merely "never existed") is rebuilt to the *true*
        // live-key set, not reset to 0.
        let backend = FakeBackend::new();
        let scope = scope_a();
        backend.seed_unindexed(&scope.data_key("existing-1"), b"v1", None);
        backend.seed_unindexed(&scope.data_key("existing-2"), b"v2", None);
        backend.seed_count(&scope.live_key(), 2);
        backend.evict_count(&scope.live_key());

        let outcome = backend
            .reconcile_count_if_missing(
                &scope.live_key(),
                &scope.data_scan_pattern(),
                &scope.reconcile_lock_key(),
                limits::RECONCILE_LOCK_TTL_MS,
                limits::RECONCILE_SCAN_LIMIT,
            )
            .await
            .unwrap();
        assert_eq!(outcome, ReconcileOutcome::Reconciled(2));
        assert_eq!(backend.live_count(&scope.live_key()), 2);
    }

    #[tokio::test]
    async fn reconcile_scores_ttl_keys_by_remaining_lifetime_and_skips_lapsed_ones() {
        let backend = FakeBackend::new();
        let scope = scope_a();
        backend.seed_unindexed(&scope.data_key("perm"), b"v", None);
        backend.seed_unindexed(&scope.data_key("ttl"), b"v", Some(30_000));
        backend.seed_unindexed(&scope.data_key("gone"), b"v", Some(1_000));
        backend.advance(Duration::from_secs(2)); // "gone" lapsed before the rebuild

        let outcome = backend
            .reconcile_count_if_missing(
                &scope.live_key(),
                &scope.data_scan_pattern(),
                &scope.reconcile_lock_key(),
                limits::RECONCILE_LOCK_TTL_MS,
                limits::RECONCILE_SCAN_LIMIT,
            )
            .await
            .unwrap();
        assert_eq!(outcome, ReconcileOutcome::Reconciled(2));

        // The rebuilt "ttl" member keeps its own expiry, so it lapses on
        // schedule instead of being pinned forever by the rebuild.
        backend.advance(Duration::from_secs(29));
        assert_eq!(backend.live_count(&scope.live_key()), 1);
    }

    #[tokio::test]
    async fn increment_reconciles_a_missing_live_index_the_same_way() {
        let host = host(&["waddles.bot.a"]);
        let scope = scope_a();
        // No prior seeding at all -- the live index has never existed, the
        // "brand-new app" shape of the same reconciliation path.
        let v = host.increment(&scope, 1, "hits", 1, 0).await.unwrap();
        assert_eq!(v, 1);
    }

    #[tokio::test]
    async fn a_write_fails_closed_while_another_caller_holds_the_reconcile_lock() {
        let backend = FakeBackend::new();
        let scope = scope_a();
        backend.hold_lock(&scope.reconcile_lock_key());
        let host = host_for(backend, &["waddles.bot.a"]);

        let err = host.set(&scope, 1, "k", b"v", 0).await.unwrap_err();
        assert_eq!(err.code(), "reconciling");
    }

    #[tokio::test]
    async fn a_healthy_live_index_never_triggers_reconciliation() {
        // A live index already at the quota ceiling: if reconciliation ran
        // anyway, it would rebuild from a `SCAN` of the (empty) data
        // keyspace and wrongly reset the count to 0, admitting a key that
        // must actually be rejected -- proves `AlreadyPresent` short-
        // circuits before any `SCAN` when the index is healthy.
        let backend = FakeBackend::new();
        let scope = scope_a();
        backend.seed_count(&scope.live_key(), MAX_KEYS_PER_APP);
        let host = host_for(backend, &["waddles.bot.a"]);
        let err = host
            .set(&scope, 1, "one-too-many", b"x", 0)
            .await
            .unwrap_err();
        assert_eq!(err.code(), "quota_exceeded");
    }
}

#[cfg(test)]
mod v2_tests {
    use super::*;
    use backend::fake::FakeBackend;

    fn scope() -> KvScope {
        KvScope::new("acme", Some("main".to_string()), "waddles.bot.a")
    }

    fn host() -> KvHost<FakeBackend> {
        let caps = Arc::new(CapabilitySnapshot::new());
        caps.update("waddles.bot.a", [authorize::KV_PERMISSION_ID.to_string()]);
        KvHost::new(FakeBackend::new(), caps)
    }

    #[tokio::test]
    async fn cas_creates_only_if_absent_then_detects_conflicts() {
        let h = host();
        let s = scope();
        let KvCasResult::Swapped(v1) = h.cas(&s, 1, "k", 0, b"one", 0).await.unwrap() else {
            panic!("create must swap")
        };
        // a second create-if-absent conflicts and reports the live version
        assert_eq!(
            h.cas(&s, 1, "k", 0, b"x", 0).await.unwrap(),
            KvCasResult::Conflict(v1)
        );
        let (val, ver) = h.get_versioned(&s, 1, "k").await.unwrap().unwrap();
        assert_eq!((val.as_slice(), ver), (&b"one"[..], v1));
        let KvCasResult::Swapped(v2) = h.cas(&s, 1, "k", v1, b"two", 0).await.unwrap() else {
            panic!("matching version must swap")
        };
        assert_ne!(v1, v2);
        // stale token loses
        assert_eq!(
            h.cas(&s, 1, "k", v1, b"lost", 0).await.unwrap(),
            KvCasResult::Conflict(v2)
        );
        assert_eq!(h.get(&s, 1, "k").await.unwrap().unwrap(), b"two");
    }

    #[tokio::test]
    async fn a_v1_write_invalidates_a_v2_version_so_mixed_use_is_safe() {
        let h = host();
        let s = scope();
        let KvCasResult::Swapped(v1) = h.cas(&s, 1, "k", 0, b"a", 0).await.unwrap() else {
            panic!()
        };
        h.set(&s, 1, "k", b"b", 0).await.unwrap();
        assert!(matches!(
            h.cas(&s, 1, "k", v1, b"c", 0).await.unwrap(),
            KvCasResult::Conflict(_)
        ));
    }

    #[tokio::test]
    async fn delete_if_honours_the_version_and_rejects_zero() {
        let h = host();
        let s = scope();
        let KvCasResult::Swapped(v) = h.cas(&s, 1, "k", 0, b"a", 0).await.unwrap() else {
            panic!()
        };
        assert!(matches!(
            h.delete_if(&s, 1, "k", v + 1).await.unwrap(),
            KvDeleteIfResult::Conflict(_)
        ));
        assert_eq!(
            h.delete_if(&s, 1, "k", v).await.unwrap(),
            KvDeleteIfResult::Deleted
        );
        assert!(h.get(&s, 1, "k").await.unwrap().is_none());
        assert!(matches!(
            h.delete_if(&s, 1, "k", 0).await,
            Err(KvError::InvalidKey(_))
        ));
    }

    #[tokio::test]
    async fn list_is_prefix_scoped_sorted_and_paginated() {
        let h = host();
        let s = scope();
        for k in ["ev.03", "ev.01", "ev.02", "ev.05", "ev.04", "other.1"] {
            h.set(&s, 1, k, b"x", 0).await.unwrap();
        }
        let p1 = h.list(&s, 2, "ev.", None, 2).await.unwrap();
        assert_eq!(p1.keys, ["ev.01", "ev.02"]);
        let p2 = h
            .list(&s, 2, "ev.", p1.next_cursor.as_deref(), 2)
            .await
            .unwrap();
        assert_eq!(p2.keys, ["ev.03", "ev.04"]);
        let p3 = h
            .list(&s, 2, "ev.", p2.next_cursor.as_deref(), 2)
            .await
            .unwrap();
        assert_eq!(p3.keys, ["ev.05"]);
        assert!(p3.next_cursor.is_none());
        let all = h.list(&s, 2, "", None, 200).await.unwrap();
        assert_eq!(all.keys.len(), 6);
        // a foreign cursor / bad limits / bad prefix are explicit errors
        assert!(h.list(&s, 2, "ev.", Some("other.1"), 2).await.is_err());
        assert!(h.list(&s, 2, "ev.", None, 0).await.is_err());
        assert!(h
            .list(&s, 2, "ev.", None, MAX_LIST_LIMIT + 1)
            .await
            .is_err());
        assert!(h.list(&s, 2, "a:b", None, 5).await.is_err());
    }

    #[tokio::test]
    async fn list_and_cas_never_cross_apps_or_communities() {
        let h = host();
        let s = scope();
        h.set(&s, 1, "ev.1", b"x", 0).await.unwrap();
        let other_comm = KvScope::new("acme", Some("other".to_string()), "waddles.bot.a");
        assert!(h
            .list(&other_comm, 2, "", None, 10)
            .await
            .unwrap()
            .keys
            .is_empty());
        let other_tenant = KvScope::new("globex", Some("main".to_string()), "waddles.bot.a");
        assert!(h
            .list(&other_tenant, 3, "", None, 10)
            .await
            .unwrap()
            .keys
            .is_empty());
        assert_eq!(
            h.cas(&other_comm, 4, "ev.1", 0, b"mine", 0).await.unwrap(),
            KvCasResult::Swapped(
                h.get_versioned(&other_comm, 4, "ev.1")
                    .await
                    .unwrap()
                    .unwrap()
                    .1
            ),
            "same key name in another community is a different, absent key"
        );
    }

    #[tokio::test]
    async fn ops_beyond_64_are_now_allowed_but_the_byte_budget_still_brakes() {
        let h = host();
        let s = scope();
        // 200 small ops in one invocation: fine (old cap was 64)
        for i in 0..200 {
            h.set(&s, 9, &format!("k{i}"), b"v", 0).await.unwrap();
        }
        // ops ceiling still applies
        let over_ops = {
            let mut last = Ok(());
            for i in 0..100 {
                last = h.set(&s, 9, &format!("m{i}"), b"v", 0).await;
                if last.is_err() {
                    break;
                }
            }
            last
        };
        assert!(matches!(over_ops, Err(KvError::RateLimited(m)) if m.contains("kv ops")));
        // byte budget: a few big values exhaust 1 MiB well before 256 ops
        let big = vec![0u8; 60 * 1024];
        let mut hit = None;
        for i in 0..40 {
            if let Err(e) = h.set(&s, 10, &format!("big{i}"), &big, 0).await {
                hit = Some((i, e));
                break;
            }
        }
        let (i, e) = hit.expect("byte budget must trip");
        assert!(
            i < 20 && matches!(&e, KvError::RateLimited(m) if m.contains("bytes")),
            "{i} {e:?}"
        );
        // a fresh invocation (new call_id) starts with a fresh budget
        h.set(&s, 11, "big-again", &big, 0).await.unwrap();
    }

    #[tokio::test]
    async fn v2_calls_on_a_v1_only_backend_fail_loud() {
        // A backend that never implemented v2 must error, not pretend.
        struct V1Only;
        impl KvBackend for V1Only {
            fn get<'a>(&'a self, _: &'a str) -> BoxFuture<'a, Result<Option<Vec<u8>>, String>> {
                Box::pin(async { Ok(None) })
            }
            fn set_with_quota<'a>(
                &'a self,
                _: &'a str,
                _: &'a str,
                _: &'a str,
                _: &'a [u8],
                _: u32,
                _: u64,
            ) -> BoxFuture<'a, Result<QuotaOutcome<()>, String>> {
                Box::pin(async { Ok(QuotaOutcome::Admitted(())) })
            }
            fn delete<'a>(
                &'a self,
                _: &'a str,
                _: &'a str,
                _: &'a str,
            ) -> BoxFuture<'a, Result<bool, String>> {
                Box::pin(async { Ok(false) })
            }
            fn increment_with_quota<'a>(
                &'a self,
                _: &'a str,
                _: &'a str,
                _: &'a str,
                _: i64,
                _: u32,
                _: u64,
            ) -> BoxFuture<'a, Result<QuotaOutcome<i64>, String>> {
                Box::pin(async { Ok(QuotaOutcome::Admitted(0)) })
            }
            fn increment_rate<'a>(
                &'a self,
                _: &'a str,
                _: u64,
            ) -> BoxFuture<'a, Result<u64, String>> {
                Box::pin(async { Ok(1) })
            }
            fn reconcile_count_if_missing<'a>(
                &'a self,
                _: &'a str,
                _: &'a str,
                _: &'a str,
                _: u64,
                _: u64,
            ) -> BoxFuture<'a, Result<ReconcileOutcome, String>> {
                Box::pin(async { Ok(ReconcileOutcome::AlreadyPresent) })
            }
        }
        let caps = Arc::new(CapabilitySnapshot::new());
        caps.update("waddles.bot.a", [authorize::KV_PERMISSION_ID.to_string()]);
        let h = KvHost::new(V1Only, caps);
        let s = scope();
        assert!(
            matches!(h.cas(&s, 1, "k", 0, b"x", 0).await, Err(KvError::Backend(m)) if m.contains("not supported"))
        );
        assert!(matches!(
            h.get_versioned(&s, 1, "k").await,
            Err(KvError::Backend(_))
        ));
        assert!(matches!(
            h.list(&s, 1, "", None, 5).await,
            Err(KvError::Backend(_))
        ));
    }
}

/// Tests for [`report_failure`]/[`error_detail`]: a captured-event subscriber (tracing core API
/// only, no new dev-dependency) proves the level, the error kind + message, and that nothing
/// sensitive reaches the line.
#[cfg(test)]
mod report_failure_tests {
    use super::*;
    use std::fmt::Write as _;
    use std::sync::Mutex;

    /// Collects `(level, "field=value ...")` for every event emitted while it is the default.
    #[derive(Default, Clone)]
    struct Capture(Arc<Mutex<Vec<(tracing::Level, String)>>>);

    struct Fields(String);

    impl tracing::field::Visit for Fields {
        fn record_debug(&mut self, f: &tracing::field::Field, v: &dyn std::fmt::Debug) {
            let _ = write!(self.0, "{}={:?} ", f.name(), v);
        }
    }

    impl tracing::Subscriber for Capture {
        fn enabled(&self, _: &tracing::Metadata<'_>) -> bool {
            true
        }
        fn new_span(&self, _: &tracing::span::Attributes<'_>) -> tracing::span::Id {
            tracing::span::Id::from_u64(1)
        }
        fn record(&self, _: &tracing::span::Id, _: &tracing::span::Record<'_>) {}
        fn record_follows_from(&self, _: &tracing::span::Id, _: &tracing::span::Id) {}
        fn event(&self, e: &tracing::Event<'_>) {
            let mut f = Fields(String::new());
            e.record(&mut f);
            self.0.lock().unwrap().push((*e.metadata().level(), f.0));
        }
        fn enter(&self, _: &tracing::span::Id) {}
        fn exit(&self, _: &tracing::span::Id) {}
    }

    fn report(err: &KvError) -> Vec<(tracing::Level, String)> {
        let capture = Capture::default();
        let scope = KvScope::new("acme", Some("main".to_string()), "waddles.bot.a");
        tracing::subscriber::with_default(capture.clone(), || {
            report_failure("process", "get", &scope, err);
        });
        let events = capture.0.lock().unwrap().clone();
        events
    }

    #[test]
    fn operator_actionable_failures_log_at_error_with_kind_and_message() {
        let cases = [
            (
                KvError::NotGranted("storage.kv is not declared for this app version".into()),
                "not_granted",
                "storage.kv is not declared",
            ),
            (
                KvError::Backend("NOPERM User default has no permissions".into()),
                "backend",
                "NOPERM",
            ),
            (
                KvError::Reconciling("lock held".into()),
                "reconciling",
                "lock held",
            ),
        ];
        for (err, kind, needle) in cases {
            let events = report(&err);
            assert_eq!(events.len(), 1, "{kind}: exactly one event");
            let (level, line) = &events[0];
            assert_eq!(*level, tracing::Level::ERROR, "{kind}");
            assert!(
                line.contains(&format!("error_kind=\"{kind}\"")),
                "{kind}: {line}"
            );
            assert!(line.contains(needle), "{kind}: message missing from {line}");
            assert!(line.contains("stage=\"process\""), "{line}");
            assert!(
                line.contains("waddles.bot.a") && line.contains("acme"),
                "{line}"
            );
        }
    }

    #[test]
    fn guest_caused_rejections_log_at_warn_not_error() {
        for err in [
            KvError::InvalidKey("bad".into()),
            KvError::TooLarge(10),
            KvError::QuotaExceeded("full".into()),
            KvError::RateLimited("slow down".into()),
        ] {
            let events = report(&err);
            assert_eq!(events.len(), 1);
            assert_eq!(events[0].0, tracing::Level::WARN, "{err}");
        }
    }

    #[test]
    fn error_detail_strips_control_characters_and_caps_length() {
        let nasty = KvError::Backend(format!("line1\nline2\r\x1b[31m{}", "x".repeat(2000)));
        let d = error_detail(&nasty);
        assert!(!d.chars().any(char::is_control), "{d:?}");
        assert_eq!(d.chars().count(), MAX_ERROR_DETAIL_CHARS);
    }
}
