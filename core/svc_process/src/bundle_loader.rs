//! The DB-driven active-bundle loader (spec: hub-api is the sole writer;
//! this stage reads ACTIVE, APPROVED bundle config from a READ-ONLY
//! Postgres and hot-swaps bundles in/out with no pod restart). Enabled by
//! default; opt out via the `waddles.core.disable-db-bundle-config`
//! kill-switch (`crate::license::DbBundleConfigGate` -- `enabled()` there
//! is already the negated, "is the DB path enabled" answer). While the
//! kill-switch is on, or the DB path is unavailable (missing `DB_READER_*`/
//! `BUNDLE_SCOPE_TENANT_ID`), the existing `PROCESS_APP_ID`/
//! `PROCESS_BUNDLE_*` env selection (`crate::lib::try_start_process_loop`)
//! remains the sole source; this loader never deletes or overrides that
//! path, only supplements it by driving `Load`/`Unload` onto whatever the
//! executor connection already is.
//!
//! Query/diff logic (`bundle_active_set`, a same-repo shared crate) is
//! DB-only and pure; this module owns the actual `Load`/`Unload` wire
//! calls via [`BundleSink`] (production: [`ExecutorSink`], over
//! `crate::host_api::Connection`) plus the poll loop that ties the two
//! together -- [`run_tick`] is the directly-testable unit (mirrors
//! `crate::spine::drain_batch`'s split from `crate::spine::run`), `run` is
//! the live interval/shutdown loop `crate::lib::try_start_db_bundle_loader`
//! spawns.

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

use bundle_active_set::{diff, ActiveBundleRow, WatermarkTracker};
use sea_orm::DatabaseConnection;

use crate::host_api::Connection;
use crate::license::FeatureGate;
use crate::spine::InvokeError;

type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// Abstraction over "load/unload a bundle onto the executor" -- lets
/// [`run_tick`]'s decision logic be unit-tested against a fake sink,
/// without a live mTLS host-API connection. Production wires
/// [`ExecutorSink`].
pub trait BundleSink: Send + Sync {
    /// `tenant_id`/`community_id` identify exactly which scope this `load`
    /// is on behalf of (wire fields added alongside the executor's
    /// digest-keyed, scope-refcounted registry -- `core/bundle_executor/
    /// src/invoke.rs`'s own doc has the full rationale for why this is
    /// required, not optional).
    fn load<'a>(
        &'a self,
        tenant_id: i32,
        community_id: i32,
        row: &'a ActiveBundleRow,
    ) -> BoxFuture<'a, Result<(), InvokeError>>;
    /// See [`BundleSink::load`]'s doc for why `tenant_id`/`community_id` are
    /// required here too.
    fn unload<'a>(
        &'a self,
        tenant_id: i32,
        community_id: i32,
        app_id: &'a str,
        digest: &'a str,
    ) -> BoxFuture<'a, Result<(), InvokeError>>;
}

/// Production [`BundleSink`]: `crate::spine::ensure_loaded`/
/// `ensure_unloaded` over one already-active `Connection`. A fresh
/// `ExecutorSink` is built every tick (never cached across ticks) so a
/// mid-poll reconnect is always driven against the *current* connection --
/// same reconnect-safety rationale as `crate::spine::LoadState`.
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
            crate::spine::ensure_loaded(
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
            crate::spine::ensure_unloaded(&self.connection, tenant_id, community_id, app_id, digest)
                .await
                .map(|_| ())
        })
    }
}

/// One poll tick's worth of work, split out from [`run`] so it is directly
/// testable against a `MockDatabase`-backed `DatabaseConnection`, a fake
/// [`FeatureGate`], and a fake [`BundleSink`] -- no live Postgres, no live
/// executor connection.
///
/// Order of short-circuits (cheapest first, matching the spec's "poll a
/// cheap CHANGE-WATERMARK first" refinement):
/// 1. Flag off -- no DB call at all.
/// 2. Watermark read fails -- logged, retried next tick.
/// 3. Watermark unchanged -- no full active-set read.
/// 4. Full read fails -- logged, retried next tick (the watermark tracker
///    already advanced, so a persistently failing full read after a real
///    change would otherwise poll forever without a full read; accepted
///    as a documented tradeoff -- the next *different* watermark still
///    triggers another attempt, and a transient failure self-heals on the
///    next legitimate change).
/// 5. Diff is empty (watermark moved but nothing this scope's set changed
///    -- e.g. another tenant's rows) -- no executor call.
/// 6. No active executor connection -- deferred to the next tick; `loaded`
///    is left untouched so the next tick's diff is still computed against
///    reality, not a falsely-advanced view.
#[allow(clippy::too_many_arguments)]
pub async fn run_tick(
    db: &DatabaseConnection,
    tenant_id: i32,
    community_id: i32,
    gate: &dyn FeatureGate,
    tracker: &mut WatermarkTracker,
    loaded: &mut HashMap<String, String>,
    sink: Option<&dyn BundleSink>,
    excluded_metric: &prometheus::IntCounterVec,
) {
    if !gate.enabled().await {
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
    gate: Arc<dyn FeatureGate>,
    connections: Arc<crate::host_api::ConnectionRegistry>,
    excluded_metric: prometheus::IntCounterVec,
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
                    gate.as_ref(),
                    &mut tracker,
                    &mut loaded,
                    sink.as_ref().map(|s| s as &dyn BundleSink),
                    &excluded_metric,
                )
                .await;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::license::test_support::{FixedGate, ToggleGate};
    use sea_orm::{DatabaseBackend, MockDatabase};
    use std::sync::Mutex as StdMutex;

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
    /// `app_active_versions` -- see that function's own doc) after every
    /// watermark-only `app_active_versions` read; every `run_tick` test
    /// below that exercises the watermark path queues this empty result
    /// right after it, since none of these tests care about source
    /// bindings themselves (`crate::source_supervisor`'s own tests do).
    fn empty_bindings() -> Vec<bundle_active_set::entities::app_source_bindings::Model> {
        Vec::new()
    }

    #[tokio::test]
    async fn run_tick_skips_all_db_work_when_the_flag_is_off() {
        // No `append_query_results` at all -- if `run_tick` issued even
        // the watermark read despite the flag being off, `MockDatabase`
        // would return a "no more results" `DbErr` and the test's own
        // assertions on `loaded`/`tracker` would still pass, so this test
        // additionally checks the transaction log is empty (proves zero
        // queries were sent, not just that errors were swallowed).
        let db = MockDatabase::new(DatabaseBackend::Postgres).into_connection();
        let mut tracker = WatermarkTracker::new();
        let mut loaded = HashMap::new();
        run_tick(
            &db,
            1,
            0,
            &FixedGate(false),
            &mut tracker,
            &mut loaded,
            None,
            &test_metric(),
        )
        .await;
        assert!(loaded.is_empty());
    }

    #[tokio::test]
    async fn run_tick_skips_the_full_read_when_the_watermark_is_unchanged() {
        // A "trap" test: tick 1 legitimately loads `waddles.a` (its
        // watermark is newly observed, so the full read is expected).
        // Tick 2's watermark is IDENTICAL to tick 1's -- `run_tick` must
        // skip the full read entirely, so the trap active-set rows queued
        // for tick 2 (which would load a second bundle, `waddles.trap`,
        // if consumed) are left unread. Asserting `sink.calls()` has
        // exactly tick 1's load -- not two -- is only possible if the
        // trap rows were genuinely never queried, unlike a bare "no panic"
        // check (a wrongly-attempted extra read against an exhausted mock
        // queue would silently log+return either way).
        let digest = format!("sha256:{}", "d".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([empty_bindings()])
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([vec![version_row("waddles.a", 10, "1", &digest)]])
            .append_query_results([vec![approval_row("waddles.a", "1")]])
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([empty_bindings()])
            // Trap: only consumed if tick 2 incorrectly performs a full
            // read despite the unchanged watermark above.
            .append_query_results([vec![active_row("waddles.trap")]])
            .into_connection();
        let mut tracker = WatermarkTracker::new();
        let mut loaded = HashMap::new();
        let gate = FixedGate(true);
        let sink = FakeSink::default();
        let metric = test_metric();

        run_tick(
            &db,
            1,
            0,
            &gate,
            &mut tracker,
            &mut loaded,
            Some(&sink as &dyn BundleSink),
            &metric,
        )
        .await;
        run_tick(
            &db,
            1,
            0,
            &gate,
            &mut tracker,
            &mut loaded,
            Some(&sink as &dyn BundleSink),
            &metric,
        )
        .await;

        assert_eq!(
            sink.calls(),
            vec![format!("load:waddles.a:{digest}")],
            "tick 2's unchanged watermark must skip the full read -- the trap row must never load"
        );
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
            &FixedGate(true),
            &mut tracker,
            &mut loaded,
            None,
            &test_metric(),
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
            &FixedGate(true),
            &mut tracker,
            &mut loaded,
            Some(&sink as &dyn BundleSink),
            &test_metric(),
        )
        .await;
        assert_eq!(loaded.get("waddles.a"), Some(&digest));
        assert_eq!(sink.calls(), vec![format!("load:waddles.a:{digest}")]);
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
            &FixedGate(true),
            &mut tracker,
            &mut loaded,
            Some(&sink as &dyn BundleSink),
            &test_metric(),
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
            &FixedGate(true),
            &mut tracker,
            &mut loaded,
            Some(&sink as &dyn BundleSink),
            &test_metric(),
        )
        .await;
        assert!(
            loaded.is_empty(),
            "a failed load must not be recorded as loaded -- retried next tick"
        );
    }

    #[tokio::test]
    async fn run_tick_respects_a_gate_flipped_off_mid_run() {
        let toggle = ToggleGate::new(true);
        let db = MockDatabase::new(DatabaseBackend::Postgres).into_connection();
        let mut tracker = WatermarkTracker::new();
        let mut loaded = HashMap::new();
        toggle.set(false);
        run_tick(
            &db,
            1,
            0,
            &toggle,
            &mut tracker,
            &mut loaded,
            None,
            &test_metric(),
        )
        .await;
        assert!(loaded.is_empty());
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
        bundle_active_set::entities::app_install_approvals::Model {
            id: 1,
            tenant_id: 1,
            community_id: None,
            app_id: app_id.to_string(),
            version: version.to_string(),
            superseded_by: None,
        }
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
            &FixedGate(true),
            &mut tracker,
            &mut loaded,
            Some(&sink as &dyn BundleSink),
            &metric,
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
            &FixedGate(true),
            &mut tracker,
            &mut loaded,
            Some(&sink as &dyn BundleSink),
            &metric,
        )
        .await;
        assert_eq!(
            loaded.get("waddles.a"),
            Some(&digest),
            "an un-backfilled row must still load via the fallback, never be dropped"
        );
        let (expected_component, _) = bundle_active_set::derive_component_keys(&digest);
        assert_eq!(
            sink.calls(),
            vec![format!("load:waddles.a:{digest}")],
            "sink sees app_id/digest only; the fallback component_key ({expected_component}) is \
             asserted directly against bundle_active_set::read_active_set's own tests"
        );
        assert_eq!(
            metric
                .with_label_values(&["waddles.a", "missing_component_key"])
                .get(),
            1,
            "a NULL component_key must increment the shared metric with the degraded reason"
        );
    }
}
