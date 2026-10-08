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
pub use backend::{BoxFuture, KvBackend, QuotaOutcome, ReconcileOutcome};
pub use limits::{
    MAX_GUEST_KEY_LEN, MAX_KEYS_PER_APP, MAX_OPS_PER_INVOKE, MAX_TTL_SECONDS, MAX_VALUE_BYTES,
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
    /// [`backend::ReconcileOutcome::Locked`]: `count_key` was missing and
    /// another caller already holds the reconciliation lock -- fail
    /// closed rather than guess a count (low-severity fix, PR #425
    /// security review: `count_key` eviction self-heal).
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

    /// Self-heal, low-severity fix (PR #425 security review): if
    /// `count_key` is missing (e.g. evicted under an `allkeys-*`
    /// `maxmemory-policy`, `crate::policy`'s doc), reconciles it against a
    /// bounded `SCAN` before the caller's quota-checked write proceeds.
    /// Fails closed (denies the write) if another caller already holds
    /// the reconciliation lock, rather than racing a second `SCAN` or
    /// guessing a count.
    async fn ensure_count_reconciled(&self, scope: &KvScope) -> Result<(), KvError> {
        match self
            .backend
            .reconcile_count_if_missing(
                &scope.count_key(),
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
                    "bundle kv: count_key was missing (evicted?), reconciled via SCAN"
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
        self.backend
            .get(&scope.data_key(key))
            .await
            .map_err(KvError::Backend)
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
        self.ensure_count_reconciled(scope).await?;
        let ttl = limits::clamp_ttl_seconds(ttl_seconds);
        match self
            .backend
            .set_with_quota(
                &scope.data_key(key),
                &scope.count_key(),
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
        self.backend
            .delete(&scope.data_key(key), &scope.count_key())
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
        self.ensure_count_reconciled(scope).await?;
        let ttl = limits::clamp_ttl_seconds(ttl_seconds);
        match self
            .backend
            .increment_with_quota(
                &scope.data_key(key),
                &scope.count_key(),
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
        backend.seed_count(&scope.count_key(), MAX_KEYS_PER_APP);
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
        // Simulates "this key was created before the app's count was
        // (separately) seeded at its ceiling" -- the key already exists,
        // independent of the counter.
        backend.seed_existing(&scope.data_key("existing"), b"v1");
        backend.seed_count(&scope.count_key(), MAX_KEYS_PER_APP);
        let host = host_for(backend, &["waddles.bot.a"]);

        // Overwriting the already-existing key succeeds despite the quota
        // being full: it never allocates a new slot.
        host.set(&scope, 1, "existing", b"v2", 0).await.unwrap();
        assert_eq!(
            host.get(&scope, 2, "existing").await.unwrap(),
            Some(b"v2".to_vec())
        );

        // A genuinely new key is still rejected under a full quota:
        let err = host.set(&scope, 3, "brand-new", b"x", 0).await.unwrap_err();
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

    // -- Self-heal: count_key eviction reconciliation --

    #[tokio::test]
    async fn set_reconciles_a_missing_count_key_before_admitting_a_new_key() {
        let backend = FakeBackend::new();
        let scope = scope_a();
        // A brand-new app: count_key has never existed at all.
        backend.seed_existing(&scope.data_key("pre-existing"), b"v");
        let host = host_for(backend, &["waddles.bot.a"]);

        // A brand-new key still succeeds -- reconciliation seeds count to
        // the true live count (1), well under quota.
        host.set(&scope, 1, "brand-new", b"x", 0).await.unwrap();
        assert_eq!(
            host.get(&scope, 2, "brand-new").await.unwrap(),
            Some(b"x".to_vec())
        );
    }

    #[tokio::test]
    async fn reconcile_recomputes_a_genuinely_evicted_counter_to_the_true_live_count() {
        // Direct backend-level test (bypassing KvHost/quota, which can't
        // itself distinguish a reconciled count of 0 vs. 2 without
        // artificially hitting a 10,000-key quota) -- proves
        // `evict_count` (simulating a live counter actually lost to
        // Valkey eviction, not merely "never existed") is recomputed to
        // the *true* live-key count, not reset to 0.
        let backend = FakeBackend::new();
        let scope = scope_a();
        backend.seed_existing(&scope.data_key("existing-1"), b"v1");
        backend.seed_existing(&scope.data_key("existing-2"), b"v2");
        backend.seed_count(&scope.count_key(), 2);
        backend.evict_count(&scope.count_key());

        let outcome = backend
            .reconcile_count_if_missing(
                &scope.count_key(),
                &scope.data_scan_pattern(),
                &scope.reconcile_lock_key(),
                limits::RECONCILE_LOCK_TTL_MS,
                limits::RECONCILE_SCAN_LIMIT,
            )
            .await
            .unwrap();
        assert_eq!(outcome, ReconcileOutcome::Reconciled(2));
    }

    #[tokio::test]
    async fn increment_reconciles_an_evicted_count_key_the_same_way() {
        let host = host(&["waddles.bot.a"]);
        let scope = scope_a();
        // No prior seeding at all -- count_key has never existed, the
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
    async fn a_healthy_count_key_never_triggers_reconciliation() {
        // A count_key already at the quota ceiling: if reconciliation ran
        // anyway, it would recompute from a `SCAN` of the (empty) data
        // keyspace and wrongly reset the count to 0, admitting a key that
        // must actually be rejected -- proves `AlreadyPresent` short-
        // circuits before any `SCAN` when the counter is healthy.
        let backend = FakeBackend::new();
        let scope = scope_a();
        backend.seed_count(&scope.count_key(), MAX_KEYS_PER_APP);
        let host = host_for(backend, &["waddles.bot.a"]);
        let err = host
            .set(&scope, 1, "one-too-many", b"x", 0)
            .await
            .unwrap_err();
        assert_eq!(err.code(), "quota_exceeded");
    }
}
