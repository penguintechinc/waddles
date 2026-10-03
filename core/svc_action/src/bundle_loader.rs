//! The DB-driven active-bundle loader (spec: hub-api is the sole writer;
//! this stage reads ACTIVE, APPROVED bundle config from a READ-ONLY
//! Postgres and hot-swaps bundles in/out with no pod restart). Enabled by
//! default; opt out via the `waddles.core.disable-db-bundle-config`
//! kill-switch (`crate::flags::DISABLE_DB_BUNDLE_CONFIG_FLAG`, see
//! `crate::flags::db_bundle_config_flag`'s doc). **Mutually exclusive with
//! the legacy `ACTION_BUNDLE_*` env override** (`crate::
//! try_start_env_bundle_loader`) -- `crate::resolve_db_path_active` picks
//! exactly one, once, at startup (`crate::lib`'s top doc); this module's
//! own `run_tick` additionally re-checks the kill-switch every tick so a
//! live flip stops DB-driven `Load`/`Unload` immediately, even though it
//! can't fail the process back over to the legacy path without a restart.
//!
//! Direct port of `core/svc_process/src/bundle_loader.rs` (same query/diff
//! logic from the shared `bundle_active_set` crate) with the wire calls
//! adapted to this crate's own `dispatch::ensure_loaded`/`ensure_unloaded`
//! and `flags::FeatureFlag` -- see that module's doc for the full
//! short-circuit-order rationale, reproduced here only where it differs.

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

use bundle_active_set::{diff, ActiveBundleRow, WatermarkTracker};
use sea_orm::DatabaseConnection;

use crate::dispatch::InvokeError;
use crate::flags::FeatureFlag;
use crate::host_api::Connection;

type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// Abstraction over "load/unload a bundle onto the executor" -- lets
/// [`run_tick`]'s decision logic be unit-tested against a fake sink,
/// without a live mTLS host-API connection. Production wires
/// [`ExecutorSink`].
pub trait BundleSink: Send + Sync {
    /// `tenant_id`/`community_id` identify exactly which scope this `load`
    /// is on behalf of (wire fields added alongside the executor's
    /// digest-keyed, scope-refcounted registry -- `core/bundle_executor/
    /// src/invoke.rs`'s own doc has the full rationale).
    fn load<'a>(
        &'a self,
        tenant_id: i32,
        community_id: i32,
        row: &'a ActiveBundleRow,
    ) -> BoxFuture<'a, Result<(), InvokeError>>;
    /// See [`BundleSink::load`]'s doc.
    fn unload<'a>(
        &'a self,
        tenant_id: i32,
        community_id: i32,
        app_id: &'a str,
        digest: &'a str,
    ) -> BoxFuture<'a, Result<(), InvokeError>>;
}

/// Production [`BundleSink`]: `crate::dispatch::ensure_loaded`/
/// `ensure_unloaded` over one already-active `Connection`. A fresh
/// `ExecutorSink` is built every tick (never cached across ticks) so a
/// mid-poll reconnect is always driven against the *current* connection.
pub struct ExecutorSink {
    pub connection: Arc<Connection>,
    pub call_timeout_ms: u64,
}

impl BundleSink for ExecutorSink {
    fn load<'a>(
        &'a self,
        tenant_id: i32,
        community_id: i32,
        row: &'a ActiveBundleRow,
    ) -> BoxFuture<'a, Result<(), InvokeError>> {
        Box::pin(async move {
            crate::dispatch::ensure_loaded(
                &self.connection,
                tenant_id,
                community_id,
                &row.app_id,
                &row.version,
                &row.digest,
                &row.component_key,
                &row.sidecar_key,
                penguin_bundle_host::wire::LoadLimits {
                    timeout_ms: self.call_timeout_ms,
                    memory_mb: 64,
                },
            )
            .await
            .map(|_| ())
        })
    }

    fn unload<'a>(
        &'a self,
        tenant_id: i32,
        community_id: i32,
        app_id: &'a str,
        digest: &'a str,
    ) -> BoxFuture<'a, Result<(), InvokeError>> {
        Box::pin(async move {
            crate::dispatch::ensure_unloaded(
                &self.connection,
                tenant_id,
                community_id,
                app_id,
                digest,
            )
            .await
            .map(|_| ())
        })
    }
}

/// Per-session counterpart to [`BundleSink`] -- every call names the exact
/// [`bundle_active_set::SessionId`] it targets, rather than being bound to
/// one connection for the sink's whole lifetime. Required by the per-session
/// loading fix (regression: bundles loaded only onto a terminating executor
/// during rollout; live executor got none, alpha 2026-10-03): a full sync
/// must drive `Load`/`Unload` against EVERY live session independently, so
/// the sink itself must be able to address any of them, not just whichever
/// one `ConnectionRegistry::active()` happened to pick for the whole tick.
/// Direct port of `core/svc_process/src/bundle_loader.rs::SessionBundleSink`
/// -- used by `crate::changelog_consumer`'s multi-tenant path only; this
/// module's own [`BundleSink`]/single-tenant [`run_tick`] below are
/// unchanged.
pub trait SessionBundleSink: Send + Sync {
    fn load<'a>(
        &'a self,
        session: bundle_active_set::SessionId,
        tenant_id: i32,
        community_id: i32,
        row: &'a ActiveBundleRow,
    ) -> BoxFuture<'a, Result<(), InvokeError>>;
    fn unload<'a>(
        &'a self,
        session: bundle_active_set::SessionId,
        tenant_id: i32,
        community_id: i32,
        app_id: &'a str,
        digest: &'a str,
    ) -> BoxFuture<'a, Result<(), InvokeError>>;
}

/// Production [`SessionBundleSink`]: resolves `session` against the live
/// [`crate::host_api::ConnectionRegistry`] on every call (never cached), so
/// a session that closes between two calls in the same tick simply fails
/// that one call (`InvokeError::NoExecutor`) -- the caller leaves it
/// unloaded for that session and retries next tick, it never panics or
/// silently targets a different session.
pub struct RegistrySink {
    pub registry: Arc<crate::host_api::ConnectionRegistry>,
    pub call_timeout_ms: u64,
}

impl SessionBundleSink for RegistrySink {
    fn load<'a>(
        &'a self,
        session: bundle_active_set::SessionId,
        tenant_id: i32,
        community_id: i32,
        row: &'a ActiveBundleRow,
    ) -> BoxFuture<'a, Result<(), InvokeError>> {
        Box::pin(async move {
            let Some(connection) = self.registry.get(session) else {
                return Err(InvokeError::NoExecutor);
            };
            crate::dispatch::ensure_loaded(
                &connection,
                tenant_id,
                community_id,
                &row.app_id,
                &row.version,
                &row.digest,
                &row.component_key,
                &row.sidecar_key,
                penguin_bundle_host::wire::LoadLimits {
                    timeout_ms: self.call_timeout_ms,
                    memory_mb: 64,
                },
            )
            .await
            .map(|_| ())
        })
    }

    fn unload<'a>(
        &'a self,
        session: bundle_active_set::SessionId,
        tenant_id: i32,
        community_id: i32,
        app_id: &'a str,
        digest: &'a str,
    ) -> BoxFuture<'a, Result<(), InvokeError>> {
        Box::pin(async move {
            let Some(connection) = self.registry.get(session) else {
                return Err(InvokeError::NoExecutor);
            };
            crate::dispatch::ensure_unloaded(&connection, tenant_id, community_id, app_id, digest)
                .await
                .map(|_| ())
        })
    }
}

/// One poll tick's worth of work, split out from [`run`] so it is directly
/// testable against a `MockDatabase`-backed `DatabaseConnection`, a fake
/// [`FeatureFlag`], and a fake [`BundleSink`]. See
/// `core/svc_process/src/bundle_loader.rs::run_tick`'s doc for the full
/// short-circuit-order rationale (flag off -> watermark read -> unchanged
/// skip -> full read -> empty-diff skip -> no-connection defer -> apply).
#[allow(clippy::too_many_arguments)]
pub async fn run_tick(
    db: &DatabaseConnection,
    tenant_id: i32,
    community_id: i32,
    flag: &dyn FeatureFlag,
    tracker: &mut WatermarkTracker,
    loaded: &mut HashMap<String, String>,
    sink: Option<&dyn BundleSink>,
    excluded_metric: &prometheus::IntCounterVec,
    kv_capabilities: &bundle_host_kv::CapabilitySnapshot,
) {
    if !flag.enabled().await {
        tracing::debug!(
            "db-bundle-config disabled (waddles.core.disable-db-bundle-config kill-switch on); skipping tick"
        );
        return;
    }

    let watermark = match bundle_active_set::read_watermark(db, tenant_id, community_id).await {
        Ok(w) => w,
        Err(err) => {
            tracing::warn!(error = %err, "db bundle-config: watermark read failed");
            return;
        }
    };
    if !tracker.observe(watermark) {
        return;
    }

    let bundle_active_set::ActiveSetRead {
        rows: active,
        excluded,
        degraded,
    } = match bundle_active_set::read_active_set(db, tenant_id, community_id, None).await {
        Ok(result) => result,
        Err(err) => {
            tracing::warn!(error = %err, "db bundle-config: active-set read failed");
            return;
        }
    };
    // Ops-visibility fix (security review): `read_active_set` already logs
    // a structured `warn!` per excluded row (`bundle_active_set::query`) --
    // this additionally drives a Prometheus counter so a feature silently
    // going dark (e.g. an approval expiring with nothing re-approving it)
    // is visible on a dashboard/alert, not just in a log stream.
    for (app_id, reason) in &excluded {
        excluded_metric
            .with_label_values(&[app_id, reason.as_str()])
            .inc();
    }
    // component_key contract: a row that loaded via `derive_component_keys`'s
    // fallback (NULL `component_key` column, un-backfilled row) is still
    // active -- shares the same counter/label space as `excluded` (both are
    // `(app_id, &'static str)` reasons) so an un-backfilled row is visible
    // during rollout without a second metric.
    for (app_id, reason) in &degraded {
        excluded_metric
            .with_label_values(&[app_id, reason.as_str()])
            .inc();
    }

    // Coordinator fix on PR #425: refresh every active app's declared-
    // capability snapshot on every tick, not just the diffed to_load set
    // -- a manifest re-approval that changes `summary_json.capabilities`
    // without a digest bump (unusual, but not impossible) must still be
    // observed, and this is a cheap in-memory hashmap write, not a Valkey
    // round trip. `bundle_host_kv::authorize`'s own doc: an app this
    // snapshot has never seen denies `kv` by default, so a bundle that
    // drops out of the active set (excluded above) simply stops being
    // refreshed here -- its last-known grant lingers until process
    // restart, an accepted staleness window matching every other
    // in-memory snapshot this crate keeps (`crate::distribution::
    // BundleCatalog`'s identical shape).
    for row in &active {
        kv_capabilities.update(row.app_id.clone(), row.declared_capabilities.clone());
    }

    let plan = diff::plan(loaded, &active);
    if plan.is_empty() {
        return;
    }

    let Some(sink) = sink else {
        tracing::debug!(
            to_load = plan.to_load.len(),
            to_unload = plan.to_unload.len(),
            "db bundle-config: active set changed but no executor connection yet; deferring"
        );
        return;
    };

    for row in &plan.to_load {
        match sink.load(tenant_id, community_id, row).await {
            Ok(()) => {
                tracing::info!(app_id = %row.app_id, digest = %row.digest, "db bundle-config: loaded");
                loaded.insert(row.app_id.clone(), row.digest.clone());
            }
            Err(err) => {
                tracing::warn!(app_id = %row.app_id, digest = %row.digest, error = %err, "db bundle-config: load failed, will retry next tick");
            }
        }
    }
    for (app_id, digest) in &plan.to_unload {
        match sink.unload(tenant_id, community_id, app_id, digest).await {
            Ok(()) => {
                tracing::info!(app_id, digest, "db bundle-config: unloaded");
                loaded.remove(app_id);
            }
            Err(err) => {
                tracing::warn!(app_id, digest, error = %err, "db bundle-config: unload failed, will retry next tick");
            }
        }
    }
    tracing::info!(
        active_count = active.len(),
        loaded_count = loaded.len(),
        "db bundle-config: tick applied"
    );
}

/// The live interval/shutdown loop `crate::lib::try_start_db_bundle_loader`
/// spawns: builds a fresh [`ExecutorSink`] every tick from whatever
/// connection is currently active (`None` when the executor hasn't
/// connected yet) and delegates the actual decision to [`run_tick`].
#[allow(clippy::too_many_arguments)]
pub async fn run(
    db: DatabaseConnection,
    tenant_id: i32,
    community_id: i32,
    poll_interval: std::time::Duration,
    call_timeout_ms: u64,
    flag: Arc<dyn FeatureFlag>,
    connections: Arc<crate::host_api::ConnectionRegistry>,
    excluded_metric: prometheus::IntCounterVec,
    kv_capabilities: Arc<bundle_host_kv::CapabilitySnapshot>,
    mut shutdown: tokio::sync::oneshot::Receiver<()>,
) {
    let mut tracker = WatermarkTracker::new();
    let mut loaded: HashMap<String, String> = HashMap::new();
    let mut interval = tokio::time::interval(poll_interval);
    interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    loop {
        tokio::select! {
            _ = &mut shutdown => return,
            _ = interval.tick() => {
                let sink = connections.active().map(|connection| ExecutorSink {
                    connection,
                    call_timeout_ms,
                });
                run_tick(
                    &db,
                    tenant_id,
                    community_id,
                    flag.as_ref(),
                    &mut tracker,
                    &mut loaded,
                    sink.as_ref().map(|s| s as &dyn BundleSink),
                    &excluded_metric,
                    &kv_capabilities,
                )
                .await;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::flags::StaticFlag;
    use sea_orm::{DatabaseBackend, MockDatabase};
    use std::sync::Mutex as StdMutex;

    /// `RegistrySink` resolves `session` against the live registry on every
    /// call -- a session with no live connection (never registered, already
    /// removed, or closed) fails that one call with
    /// `InvokeError::NoExecutor`, never panics and never silently targets a
    /// different session. regression: bundles loaded only onto a
    /// terminating executor during rollout; live executor got none (alpha
    /// 2026-10-03)
    mod registry_sink {
        use super::*;
        use crate::host_api::ConnectionRegistry;

        fn test_row(digest: &str) -> ActiveBundleRow {
            ActiveBundleRow {
                app_id: "waddles.a".to_string(),
                version: "1".to_string(),
                digest: digest.to_string(),
                component_key: "k".to_string(),
                sidecar_key: "s".to_string(),
                declared_capabilities: Vec::new(),
            }
        }

        #[tokio::test]
        async fn load_fails_closed_for_an_unknown_session() {
            let registry = Arc::new(ConnectionRegistry::new());
            let sink = RegistrySink {
                registry,
                call_timeout_ms: 1000,
            };
            let result = sink.load(99, 1, 0, &test_row("sha256:aa")).await;
            assert!(matches!(result, Err(InvokeError::NoExecutor)));
        }

        #[tokio::test]
        async fn unload_fails_closed_for_an_unknown_session() {
            let registry = Arc::new(ConnectionRegistry::new());
            let sink = RegistrySink {
                registry,
                call_timeout_ms: 1000,
            };
            let result = sink.unload(99, 1, 0, "waddles.a", "sha256:aa").await;
            assert!(matches!(result, Err(InvokeError::NoExecutor)));
        }
    }

    /// Records every `load`/`unload` call it receives and answers each
    /// with a fixed, caller-chosen result -- no network, no wasmtime, no
    /// live executor.
    #[derive(Default)]
    struct FakeSink {
        calls: StdMutex<Vec<String>>,
        fail_loads: StdMutex<std::collections::HashSet<String>>,
    }

    impl FakeSink {
        fn calls(&self) -> Vec<String> {
            self.calls.lock().unwrap().clone()
        }

        fn fail_load(&self, app_id: &str) {
            self.fail_loads.lock().unwrap().insert(app_id.to_string());
        }
    }

    impl BundleSink for FakeSink {
        fn load<'a>(
            &'a self,
            _tenant_id: i32,
            _community_id: i32,
            row: &'a ActiveBundleRow,
        ) -> BoxFuture<'a, Result<(), InvokeError>> {
            Box::pin(async move {
                self.calls
                    .lock()
                    .unwrap()
                    .push(format!("load:{}:{}", row.app_id, row.digest));
                if self.fail_loads.lock().unwrap().contains(&row.app_id) {
                    return Err(InvokeError::NoExecutor);
                }
                Ok(())
            })
        }

        fn unload<'a>(
            &'a self,
            _tenant_id: i32,
            _community_id: i32,
            app_id: &'a str,
            digest: &'a str,
        ) -> BoxFuture<'a, Result<(), InvokeError>> {
            let call = format!("unload:{app_id}:{digest}");
            Box::pin(async move {
                self.calls.lock().unwrap().push(call);
                Ok(())
            })
        }
    }

    fn active_row(app_id: &str) -> bundle_active_set::entities::app_active_versions::Model {
        bundle_active_set::entities::app_active_versions::Model {
            app_id: app_id.to_string(),
            tenant_id: 1,
            community_id: 0,
            version_id: 10,
        }
    }

    /// `bundle_active_set::read_watermark` now issues a second query
    /// (`app_source_bindings`, folded into the same fingerprint as
    /// `app_active_versions`) after every watermark-only `app_active_versions`
    /// read; every `run_tick` test below that exercises the watermark path
    /// queues this empty result right after it. `svc_action` has no
    /// source-binding supervisor of its own -- this is purely a mock-queue
    /// bookkeeping consequence of the shared `bundle_active_set` crate.
    fn empty_bindings() -> Vec<bundle_active_set::entities::app_source_bindings::Model> {
        Vec::new()
    }

    /// A published, fully-backfilled `app_versions` row -- real
    /// `component_key`/`sidecar_key` columns set, exactly
    /// `bundles/{app_id}/{version}/{sha256}.wasm` per the contract, so
    /// existing tests exercise the primary (non-fallback) path by default.
    /// `version_row_without_component_key` below is the dedicated
    /// fallback-path fixture.
    fn version_row(
        app_id: &str,
        id: i64,
        version: &str,
        digest: &str,
    ) -> bundle_active_set::entities::app_versions::Model {
        let hex = digest.strip_prefix("sha256:").unwrap_or(digest);
        bundle_active_set::entities::app_versions::Model {
            id,
            app_id: app_id.to_string(),
            version: version.to_string(),
            artifact_digest: Some(digest.to_string()),
            scan_status: "scanned".to_string(),
            component_key: Some(format!("bundles/{app_id}/{version}/{hex}.wasm")),
            sidecar_key: Some(format!("bundles/{app_id}/{version}/{hex}.json")),
        }
    }

    /// A published `app_versions` row from before the `component_key`
    /// migration/backfill landed -- `component_key`/`sidecar_key` both
    /// `NULL`, exercising `bundle_active_set::derive_component_keys`'s
    /// fallback path.
    fn version_row_without_component_key(
        app_id: &str,
        id: i64,
        version: &str,
        digest: &str,
    ) -> bundle_active_set::entities::app_versions::Model {
        bundle_active_set::entities::app_versions::Model {
            id,
            app_id: app_id.to_string(),
            version: version.to_string(),
            artifact_digest: Some(digest.to_string()),
            scan_status: "scanned".to_string(),
            component_key: None,
            sidecar_key: None,
        }
    }

    fn approval_row(
        app_id: &str,
        version: &str,
    ) -> bundle_active_set::entities::app_install_approvals::Model {
        // `summary_json` declares `storage.kv` by default -- every test in
        // this module other than the `kv_capabilities_*` ones below is
        // testing load/unload/diff behavior, not the capability snapshot,
        // so they should not incidentally start failing a `kv` grant check
        // elsewhere in this crate's test suite.
        approval_row_with_capabilities(app_id, version, &["storage.kv"])
    }

    fn approval_row_with_capabilities(
        app_id: &str,
        version: &str,
        capabilities: &[&str],
    ) -> bundle_active_set::entities::app_install_approvals::Model {
        bundle_active_set::entities::app_install_approvals::Model {
            id: 1,
            tenant_id: 1,
            community_id: None,
            app_id: app_id.to_string(),
            version: version.to_string(),
            superseded_by: None,
            summary_json: serde_json::json!({ "capabilities": capabilities }),
        }
    }

    /// A standalone, unregistered `IntCounterVec` -- valid to `.inc()`
    /// against without a `prometheus::Registry` (registration only matters
    /// for `/metrics` exposition, not internal correctness), so tests don't
    /// need to thread `telemetry::register_bundle_loader_excluded_metrics`
    /// through just to satisfy `run_tick`'s signature.
    fn test_metric() -> prometheus::IntCounterVec {
        prometheus::IntCounterVec::new(
            prometheus::Opts::new("test_bundle_active_set_excluded_total", "test"),
            &["app_id", "reason"],
        )
        .expect("valid metric definition")
    }

    #[tokio::test]
    async fn run_tick_skips_all_db_work_when_the_flag_is_off() {
        let db = MockDatabase::new(DatabaseBackend::Postgres).into_connection();
        let mut tracker = WatermarkTracker::new();
        let mut loaded = HashMap::new();
        run_tick(
            &db,
            1,
            0,
            &StaticFlag(false),
            &mut tracker,
            &mut loaded,
            None,
            &test_metric(),
            &bundle_host_kv::CapabilitySnapshot::new(),
        )
        .await;
        assert!(loaded.is_empty());
    }

    #[tokio::test]
    async fn run_tick_skips_the_full_read_when_the_watermark_is_unchanged() {
        // Trap technique (see `core/svc_process`'s identical test): tick 1
        // legitimately loads `waddles.a`; tick 2's watermark is IDENTICAL,
        // so the trap rows queued for it (which would load `waddles.trap`
        // if read) must never be consumed.
        let digest = format!("sha256:{}", "d".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([empty_bindings()])
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([vec![version_row("waddles.a", 10, "1", &digest)]])
            .append_query_results([vec![approval_row("waddles.a", "1")]])
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([empty_bindings()])
            .append_query_results([vec![active_row("waddles.trap")]])
            .into_connection();
        let mut tracker = WatermarkTracker::new();
        let mut loaded = HashMap::new();
        let flag = StaticFlag(true);
        let sink = FakeSink::default();
        let metric = test_metric();

        run_tick(
            &db,
            1,
            0,
            &flag,
            &mut tracker,
            &mut loaded,
            Some(&sink as &dyn BundleSink),
            &metric,
            &bundle_host_kv::CapabilitySnapshot::new(),
        )
        .await;
        run_tick(
            &db,
            1,
            0,
            &flag,
            &mut tracker,
            &mut loaded,
            Some(&sink as &dyn BundleSink),
            &metric,
            &bundle_host_kv::CapabilitySnapshot::new(),
        )
        .await;

        assert_eq!(
            sink.calls(),
            vec![format!("load:waddles.a:{digest}")],
            "tick 2's unchanged watermark must skip the full read -- the trap row must never load"
        );
    }

    /// A watermark-read DB error must be logged and swallowed -- `run_tick`
    /// returns early, leaving `loaded` untouched, never panicking or
    /// propagating the error to the caller (the poll loop must keep
    /// ticking on the next interval regardless).
    #[tokio::test]
    async fn run_tick_returns_when_watermark_read_fails() {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_errors([sea_orm::DbErr::Custom("simulated".to_string())])
            .into_connection();
        let mut tracker = WatermarkTracker::new();
        let mut loaded = HashMap::new();
        run_tick(
            &db,
            1,
            0,
            &StaticFlag(true),
            &mut tracker,
            &mut loaded,
            None,
            &test_metric(),
            &bundle_host_kv::CapabilitySnapshot::new(),
        )
        .await;
        assert!(loaded.is_empty());
    }

    /// Same early-return/swallow contract as the watermark-read failure
    /// above, but for the full active-set read that follows a changed
    /// watermark.
    #[tokio::test]
    async fn run_tick_returns_when_active_set_read_fails() {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([empty_bindings()])
            .append_query_errors([sea_orm::DbErr::Custom("simulated".to_string())])
            .into_connection();
        let mut tracker = WatermarkTracker::new();
        let mut loaded = HashMap::new();
        run_tick(
            &db,
            1,
            0,
            &StaticFlag(true),
            &mut tracker,
            &mut loaded,
            None,
            &test_metric(),
            &bundle_host_kv::CapabilitySnapshot::new(),
        )
        .await;
        assert!(loaded.is_empty());
    }

    /// An unchanged diff (nothing to load or unload) returns before ever
    /// consulting `sink` -- distinct from
    /// `run_tick_defers_when_no_executor_connection_is_active` below, which
    /// covers a *non-empty* diff with `sink: None`.
    #[tokio::test]
    async fn run_tick_returns_early_when_the_diff_is_empty() {
        let digest = format!("sha256:{}", "a".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([empty_bindings()])
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([vec![version_row("waddles.a", 10, "1", &digest)]])
            .append_query_results([vec![approval_row("waddles.a", "1")]])
            .into_connection();
        let mut tracker = WatermarkTracker::new();
        // Pre-seed `loaded` to already match the active set -- `diff::plan`
        // reports empty, so the sink (`None` here) must never be consulted.
        let mut loaded = HashMap::new();
        loaded.insert("waddles.a".to_string(), digest.clone());
        run_tick(
            &db,
            1,
            0,
            &StaticFlag(true),
            &mut tracker,
            &mut loaded,
            None,
            &test_metric(),
            &bundle_host_kv::CapabilitySnapshot::new(),
        )
        .await;
        assert_eq!(loaded.get("waddles.a"), Some(&digest));
    }

    #[tokio::test]
    async fn run_tick_defers_when_no_executor_connection_is_active() {
        let digest = format!("sha256:{}", "a".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([empty_bindings()])
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([vec![version_row("waddles.a", 10, "1", &digest)]])
            .append_query_results([vec![approval_row("waddles.a", "1")]])
            .into_connection();
        let mut tracker = WatermarkTracker::new();
        let mut loaded = HashMap::new();
        run_tick(
            &db,
            1,
            0,
            &StaticFlag(true),
            &mut tracker,
            &mut loaded,
            None,
            &test_metric(),
            &bundle_host_kv::CapabilitySnapshot::new(),
        )
        .await;
        assert!(
            loaded.is_empty(),
            "no connection -> nothing recorded as loaded yet"
        );
    }

    #[tokio::test]
    async fn run_tick_loads_a_newly_active_bundle_through_the_sink() {
        let digest = format!("sha256:{}", "b".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([empty_bindings()])
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([vec![version_row("waddles.a", 10, "1", &digest)]])
            .append_query_results([vec![approval_row("waddles.a", "1")]])
            .into_connection();
        let mut tracker = WatermarkTracker::new();
        let mut loaded = HashMap::new();
        let sink = FakeSink::default();
        run_tick(
            &db,
            1,
            0,
            &StaticFlag(true),
            &mut tracker,
            &mut loaded,
            Some(&sink as &dyn BundleSink),
            &test_metric(),
            &bundle_host_kv::CapabilitySnapshot::new(),
        )
        .await;
        assert_eq!(loaded.get("waddles.a"), Some(&digest));
        assert_eq!(sink.calls(), vec![format!("load:waddles.a:{digest}")]);
    }

    /// Contract test (regression: DB bare-hex digest rejected by executor
    /// Load (malformed digest), UnknownBundle, alpha 2026-10-03): direct
    /// port of `core/svc_process/src/bundle_loader.rs`'s identically-named
    /// test -- `app_versions.artifact_digest` is stored bare-hex by the
    /// control plane; `bundle_active_set::canonical_digest` (the sole
    /// normalization boundary) must turn that into the `sha256:`-prefixed
    /// form before it ever reaches [`BundleSink::load`], which is what the
    /// live executor's wire protocol requires
    /// (`core/bundle_executor/src/invoke.rs::verify_digest`).
    #[tokio::test]
    async fn run_tick_sends_a_canonical_digest_to_the_sink_for_a_bare_hex_db_row() {
        let hex = "d".repeat(64);
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([empty_bindings()])
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([vec![version_row("waddles.a", 10, "1", &hex)]])
            .append_query_results([vec![approval_row("waddles.a", "1")]])
            .into_connection();
        let mut tracker = WatermarkTracker::new();
        let mut loaded = HashMap::new();
        let sink = FakeSink::default();
        run_tick(
            &db,
            1,
            0,
            &StaticFlag(true),
            &mut tracker,
            &mut loaded,
            Some(&sink as &dyn BundleSink),
            &test_metric(),
            &bundle_host_kv::CapabilitySnapshot::new(),
        )
        .await;
        let canonical = format!("sha256:{hex}");
        assert_eq!(
            loaded.get("waddles.a"),
            Some(&canonical),
            "loaded-state map must record the canonical (prefixed) form, never the bare DB value"
        );
        assert_eq!(
            sink.calls(),
            vec![format!("load:waddles.a:{canonical}")],
            "the sink (and therefore the executor Load wire request) must see the sha256:-prefixed form"
        );
    }

    /// Loaded-state comparisons match across forms end to end -- direct port
    /// of `core/svc_process/src/bundle_loader.rs`'s identically-named test.
    /// A bundle loaded from a bare-hex DB row (canonicalized on the way in)
    /// is correctly recognized as "still active, unchanged" on a later tick
    /// whose active set moved for an unrelated reason (a second app
    /// activating) -- `diff::plan`'s `current_digest == &row.digest` check
    /// never spuriously reloads `waddles.a` because one side was prefixed
    /// and the other wasn't.
    #[tokio::test]
    async fn run_tick_does_not_reload_an_unchanged_bare_hex_digest_on_the_next_tick() {
        let hex_a = "e".repeat(64);
        let hex_b = "f".repeat(64);
        let active_b = bundle_active_set::entities::app_active_versions::Model {
            app_id: "waddles.b".to_string(),
            tenant_id: 1,
            community_id: 0,
            version_id: 20,
        };
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            // Tick 1: only `waddles.a` active.
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([empty_bindings()])
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([vec![version_row("waddles.a", 10, "1", &hex_a)]])
            .append_query_results([vec![approval_row("waddles.a", "1")]])
            // Tick 2: `waddles.b` newly activates (moves the watermark);
            // `waddles.a`'s row is byte-for-byte identical to tick 1.
            .append_query_results([vec![active_row("waddles.a"), active_b.clone()]])
            .append_query_results([empty_bindings()])
            .append_query_results([vec![active_row("waddles.a"), active_b]])
            .append_query_results([vec![
                version_row("waddles.a", 10, "1", &hex_a),
                version_row("waddles.b", 20, "1", &hex_b),
            ]])
            .append_query_results([vec![
                approval_row("waddles.a", "1"),
                approval_row("waddles.b", "1"),
            ]])
            .into_connection();
        let mut tracker = WatermarkTracker::new();
        let mut loaded = HashMap::new();
        let sink = FakeSink::default();
        for _ in 0..2 {
            run_tick(
                &db,
                1,
                0,
                &StaticFlag(true),
                &mut tracker,
                &mut loaded,
                Some(&sink as &dyn BundleSink),
                &test_metric(),
                &bundle_host_kv::CapabilitySnapshot::new(),
            )
            .await;
        }
        assert_eq!(
            sink.calls(),
            vec![
                format!("load:waddles.a:sha256:{hex_a}"),
                format!("load:waddles.b:sha256:{hex_b}"),
            ],
            "waddles.a's unchanged bare-hex digest must compare equal to the canonical form \
             already recorded and never trigger a redundant reload, even though the watermark \
             moved for waddles.b's sake"
        );
    }

    #[tokio::test]
    async fn run_tick_unloads_a_bundle_removed_from_the_active_set() {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([
                Vec::<bundle_active_set::entities::app_active_versions::Model>::new(),
            ])
            .append_query_results([empty_bindings()])
            .append_query_results([
                Vec::<bundle_active_set::entities::app_active_versions::Model>::new(),
            ])
            .into_connection();
        let mut tracker = WatermarkTracker::new();
        let mut loaded = HashMap::new();
        loaded.insert("waddles.gone".to_string(), "sha256:old".to_string());
        let sink = FakeSink::default();
        run_tick(
            &db,
            1,
            0,
            &StaticFlag(true),
            &mut tracker,
            &mut loaded,
            Some(&sink as &dyn BundleSink),
            &test_metric(),
            &bundle_host_kv::CapabilitySnapshot::new(),
        )
        .await;
        assert!(loaded.is_empty());
        assert_eq!(
            sink.calls(),
            vec!["unload:waddles.gone:sha256:old".to_string()]
        );
    }

    #[tokio::test]
    async fn run_tick_leaves_loaded_untouched_when_the_sink_load_fails() {
        let digest = format!("sha256:{}", "c".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([empty_bindings()])
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([vec![version_row("waddles.a", 10, "1", &digest)]])
            .append_query_results([vec![approval_row("waddles.a", "1")]])
            .into_connection();
        let mut tracker = WatermarkTracker::new();
        let mut loaded = HashMap::new();
        let sink = FakeSink::default();
        sink.fail_load("waddles.a");
        run_tick(
            &db,
            1,
            0,
            &StaticFlag(true),
            &mut tracker,
            &mut loaded,
            Some(&sink as &dyn BundleSink),
            &test_metric(),
            &bundle_host_kv::CapabilitySnapshot::new(),
        )
        .await;
        assert!(
            loaded.is_empty(),
            "a failed load must not be recorded as loaded -- retried next tick"
        );
    }

    /// Ops-visibility fix (security review): an excluded active row must
    /// increment the caller-supplied Prometheus counter, not just log --
    /// this is the regression test for that behavior. `waddles.excluded`
    /// is active but has no matching approval row queued, so it's excluded
    /// while `waddles.a` (which does have one) loads normally.
    #[tokio::test]
    async fn run_tick_increments_the_excluded_metric_for_an_unapproved_row() {
        let digest = format!("sha256:{}", "e".repeat(64));
        // Distinct `version_id`s (11/12, not `active_row`'s hardcoded 10)
        // -- `versions_by_id` is keyed by `id`, so two active rows sharing
        // one `version_id` would collapse to a single `app_versions` row
        // and defeat this test's two-distinct-apps setup.
        let excluded_active = bundle_active_set::entities::app_active_versions::Model {
            app_id: "waddles.excluded".to_string(),
            tenant_id: 1,
            community_id: 0,
            version_id: 12,
        };
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![active_row("waddles.a"), excluded_active.clone()]])
            .append_query_results([empty_bindings()])
            .append_query_results([vec![active_row("waddles.a"), excluded_active]])
            .append_query_results([vec![
                version_row("waddles.a", 10, "1", &digest),
                version_row("waddles.excluded", 12, "1", &digest),
            ]])
            .append_query_results([vec![approval_row("waddles.a", "1")]])
            .into_connection();
        let mut tracker = WatermarkTracker::new();
        let mut loaded = HashMap::new();
        let sink = FakeSink::default();
        let metric = test_metric();
        run_tick(
            &db,
            1,
            0,
            &StaticFlag(true),
            &mut tracker,
            &mut loaded,
            Some(&sink as &dyn BundleSink),
            &metric,
            &bundle_host_kv::CapabilitySnapshot::new(),
        )
        .await;
        assert_eq!(loaded.get("waddles.a"), Some(&digest));
        assert_eq!(
            metric
                .with_label_values(&["waddles.excluded", "no_approval"])
                .get(),
            1,
            "the excluded row must increment the metric labeled with its own app_id/reason"
        );
        assert_eq!(
            metric
                .with_label_values(&["waddles.a", "no_approval"])
                .get(),
            0,
            "only the actually-excluded app_id must be incremented"
        );
    }

    /// component_key contract: a row with a `NULL` `component_key` column
    /// still loads (never excluded), goes through the sink with the
    /// `derive_component_keys`-derived component key, and increments the
    /// shared metric labeled `missing_component_key` -- proving `run_tick`
    /// actually surfaces `ActiveSetRead::degraded`, not just
    /// `ActiveSetRead::excluded`.
    #[tokio::test]
    async fn run_tick_loads_via_the_fallback_and_increments_the_degraded_metric_when_component_key_is_null(
    ) {
        let digest = format!("sha256:{}", "f".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([empty_bindings()])
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([vec![version_row_without_component_key(
                "waddles.a",
                10,
                "1",
                &digest,
            )]])
            .append_query_results([vec![approval_row("waddles.a", "1")]])
            .into_connection();
        let mut tracker = WatermarkTracker::new();
        let mut loaded = HashMap::new();
        let sink = FakeSink::default();
        let metric = test_metric();
        run_tick(
            &db,
            1,
            0,
            &StaticFlag(true),
            &mut tracker,
            &mut loaded,
            Some(&sink as &dyn BundleSink),
            &metric,
            &bundle_host_kv::CapabilitySnapshot::new(),
        )
        .await;
        assert_eq!(
            loaded.get("waddles.a"),
            Some(&digest),
            "an un-backfilled row must still load via the fallback, never be dropped"
        );
        assert_eq!(
            sink.calls(),
            vec![format!("load:waddles.a:{digest}")],
            "sink sees app_id/digest only; the fallback component_key is asserted directly \
             against bundle_active_set::read_active_set's own tests"
        );
        assert_eq!(
            metric
                .with_label_values(&["waddles.a", "missing_component_key"])
                .get(),
            1,
            "a NULL component_key must increment the shared metric with the degraded reason"
        );
    }

    /// Coordinator fix on PR #425: `run_tick` must populate the shared
    /// `CapabilitySnapshot` from every active row's `summary_json`-derived
    /// `declared_capabilities`, so `storage.kv` becomes checkable by
    /// `bundle_host_kv::authorize::authorize_kv` at the host-call layer.
    #[tokio::test]
    async fn run_tick_populates_the_capability_snapshot_from_declared_capabilities() {
        let digest = format!("sha256:{}", "9".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([empty_bindings()])
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([vec![version_row("waddles.a", 10, "1", &digest)]])
            .append_query_results([vec![approval_row_with_capabilities(
                "waddles.a",
                "1",
                &["context", "kv", "flags", "log", "clock"],
            )]])
            .into_connection();
        let mut tracker = WatermarkTracker::new();
        let mut loaded = HashMap::new();
        let sink = FakeSink::default();
        let kv_capabilities = bundle_host_kv::CapabilitySnapshot::new();
        run_tick(
            &db,
            1,
            0,
            &StaticFlag(true),
            &mut tracker,
            &mut loaded,
            Some(&sink as &dyn BundleSink),
            &test_metric(),
            &kv_capabilities,
        )
        .await;
        assert!(kv_capabilities.declares("waddles.a", "kv"));
        assert!(!kv_capabilities.declares("waddles.a", "storage.kv"));
    }

    /// A bundle whose approved manifest never declares `kv` at all must
    /// leave the snapshot without that grant -- proves this isn't a
    /// blanket "every active row grants everything" bug.
    #[tokio::test]
    async fn run_tick_never_grants_kv_for_a_bundle_that_did_not_declare_it() {
        let digest = format!("sha256:{}", "8".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([empty_bindings()])
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([vec![version_row("waddles.a", 10, "1", &digest)]])
            .append_query_results([vec![approval_row_with_capabilities(
                "waddles.a",
                "1",
                &["context", "flags", "log", "clock", "http"],
            )]])
            .into_connection();
        let mut tracker = WatermarkTracker::new();
        let mut loaded = HashMap::new();
        let sink = FakeSink::default();
        let kv_capabilities = bundle_host_kv::CapabilitySnapshot::new();
        run_tick(
            &db,
            1,
            0,
            &StaticFlag(true),
            &mut tracker,
            &mut loaded,
            Some(&sink as &dyn BundleSink),
            &test_metric(),
            &kv_capabilities,
        )
        .await;
        assert!(!kv_capabilities.declares("waddles.a", "kv"));
    }

    /// The live `run()` loop, never previously exercised: with the
    /// kill-switch flag permanently OFF, `run_tick` returns before ever
    /// touching the DB (its own first check), so an empty `MockDatabase`
    /// (no queued results at all -- any query would panic) is sufficient to
    /// prove the interval/shutdown `tokio::select!` itself works -- ticks
    /// repeatedly (1ms interval, far shorter than the 50ms shutdown delay)
    /// and returns promptly once `shutdown` resolves instead of hanging.
    #[tokio::test]
    async fn run_ticks_and_returns_promptly_on_shutdown() {
        let db = MockDatabase::new(DatabaseBackend::Postgres).into_connection();
        let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel();
        tokio::spawn(async move {
            tokio::time::sleep(std::time::Duration::from_millis(50)).await;
            let _ = shutdown_tx.send(());
        });

        let flag: Arc<dyn FeatureFlag> = Arc::new(StaticFlag(false));
        let result = tokio::time::timeout(
            std::time::Duration::from_secs(5),
            run(
                db,
                1,
                0,
                std::time::Duration::from_millis(1),
                2000,
                flag,
                Arc::new(crate::host_api::ConnectionRegistry::new()),
                test_metric(),
                Arc::new(bundle_host_kv::CapabilitySnapshot::new()),
                shutdown_rx,
            ),
        )
        .await;
        assert!(
            result.is_ok(),
            "run() must return promptly once shutdown resolves, not hang"
        );
    }
}
