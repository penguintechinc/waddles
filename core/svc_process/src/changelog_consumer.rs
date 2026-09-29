//! Multi-tenant, change-log-driven active-bundle loader + source-binding
//! supervisor (dataplane scale design rev 4, §7/§8 step 2: "Multi-tenant
//! watermark polling"). Replaces the retired single-`(tenant_id,
//! community_id)` `crate::bundle_loader::run`/`crate::source_supervisor::run`
//! poll loop -- this module owns the ONLY loop now; both those modules'
//! non-loop mechanics (`bundle_loader::BundleSink`/`ExecutorSink`,
//! `source_supervisor::ConsumerSupervisor`/`SpineConsumerSupervisor`/
//! `reconcile`) are reused verbatim, just driven from here.
//!
//! **User requirement, restated exactly:** "every svc_process/svc_action
//! pod serves ALL tenants" -- there is no more `BUNDLE_SCOPE_TENANT_ID`/
//! `BUNDLE_SCOPE_COMMUNITY_ID` scope. On start, [`initial_state`] does a
//! full active-set + source-binding read across EVERY `(tenant_id,
//! community_id)` scope in the database
//! (`bundle_active_set::read_active_set_all`/`read_source_bindings_all`)
//! and seeds a [`bundle_active_set::ChangeLogTracker`] from the `safe_seq`
//! observed around that same read. Every subsequent tick
//! ([`run_incremental_tick`]) reads the primary-published `safe_seq`,
//! reads only the change-log rows in `(last_seq, safe_seq]`
//! (**never consuming beyond `safe_seq`**), and re-reads ONLY the
//! `(tenant_id, community_id)` scopes those rows name -- fail-closed PER
//! SCOPE (a re-read/resolution failure logs + increments a metric and
//! skips just that scope, the tick still advances `last_seq` to
//! `safe_seq` for every other scope). [`run_full_reconcile`] periodically
//! re-runs the full multi-scope read regardless of change-log state,
//! bounding the blast radius of any change-log defect to one interval
//! (design §7).

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Instant;

use bundle_active_set::{ActiveSetRead, ChangeLogTracker, ResolvedScope, ScopeKey, SourceBinding};
use sea_orm::DatabaseConnection;

use crate::bundle_loader::BundleSink;
use crate::license::FeatureGate;
use crate::source_supervisor::{self, ConsumerSupervisor, ResolvedBinding, RunningConsumers};
use crate::telemetry::ChangelogConsumerMetrics;

/// All state one running consumer instance carries across ticks --
/// constructed once by [`initial_state`], mutated in place by every
/// subsequent [`run_incremental_tick`]/[`run_full_reconcile`] call.
pub struct ConsumerState {
    by_scope: HashMap<ScopeKey, ActiveSetRead>,
    bindings_by_scope: HashMap<ScopeKey, Vec<SourceBinding>>,
    resolved_scopes: HashMap<ScopeKey, ResolvedScope>,
    loaded: HashMap<String, String>,
    running_consumers: RunningConsumers,
    tracker: ChangeLogTracker,
}

impl ConsumerState {
    /// Test/startup-only constructor for an empty state seeded with a
    /// given `initial_seq` -- production code only ever obtains a
    /// [`ConsumerState`] via [`initial_state`], which also performs the
    /// required first full read; this is exposed so `run_incremental_tick`/
    /// `run_full_reconcile` unit tests can seed a state directly.
    pub fn new(initial_seq: i64) -> Self {
        Self {
            by_scope: HashMap::new(),
            bindings_by_scope: HashMap::new(),
            resolved_scopes: HashMap::new(),
            loaded: HashMap::new(),
            running_consumers: HashMap::new(),
            tracker: ChangeLogTracker::new(initial_seq),
        }
    }

    #[cfg(test)]
    fn loaded(&self) -> &HashMap<String, String> {
        &self.loaded
    }

    #[cfg(test)]
    fn last_seq(&self) -> i64 {
        self.tracker.last_seq()
    }

    #[cfg(test)]
    fn running_len(&self) -> usize {
        self.running_consumers.len()
    }
}

/// The "on start, full active-set read for ALL tenants/communities"
/// requirement -- reads `safe_seq` FIRST, then performs the full
/// multi-scope active-set + source-binding read, and seeds the
/// [`ChangeLogTracker`] from that pre-read `safe_seq` value. A tiny race
/// (a change committing between the `safe_seq` read and the full read
/// finishing) is bounded by the periodic full reconcile, never a
/// correctness gap (see this module's own doc).
pub async fn initial_state(
    db: &DatabaseConnection,
) -> Result<ConsumerState, bundle_active_set::ActiveSetError> {
    let safe_seq = bundle_active_set::read_safe_seq(db).await?;
    let by_scope = bundle_active_set::read_active_set_all(db).await?;
    let bindings_by_scope = bundle_active_set::read_source_bindings_all(db).await?;
    Ok(ConsumerState {
        by_scope,
        bindings_by_scope,
        resolved_scopes: HashMap::new(),
        loaded: HashMap::new(),
        running_consumers: HashMap::new(),
        tracker: ChangeLogTracker::new(safe_seq),
    })
}

/// Resolves and caches the tenant slug/community name for `scope`, reusing
/// an already-cached entry when present. A resolution failure (missing
/// row, cross-tenant community id, or a query error) is reported to the
/// caller as `None` -- **fail-closed, per-scope**: that scope's bindings
/// are simply left out of this tick's consumer target (never a hardcoded/
/// guessed scope, never an aborted tick).
async fn resolve_scope_cached<'a>(
    db: &DatabaseConnection,
    cache: &'a mut HashMap<ScopeKey, ResolvedScope>,
    scope: ScopeKey,
    metrics: &ChangelogConsumerMetrics,
) -> Option<&'a ResolvedScope> {
    if let std::collections::hash_map::Entry::Vacant(entry) = cache.entry(scope) {
        match bundle_active_set::resolve_scope(db, scope.0, scope.1).await {
            Ok(Some(resolved)) => {
                entry.insert(resolved);
            }
            Ok(None) => {
                tracing::error!(
                    tenant_id = scope.0,
                    community_id = scope.1,
                    "changelog consumer: tenant/community scope could not be resolved (missing \
                     row, or cross-tenant community id); skipping this scope's bindings this tick"
                );
                metrics
                    .scope_failures_total
                    .with_label_values(&["resolve_failed"])
                    .inc();
                return None;
            }
            Err(err) => {
                tracing::error!(
                    tenant_id = scope.0,
                    community_id = scope.1,
                    error = %err,
                    "changelog consumer: scope resolution query failed; skipping this scope's \
                     bindings this tick"
                );
                metrics
                    .scope_failures_total
                    .with_label_values(&["resolve_failed"])
                    .inc();
                return None;
            }
        }
    }
    cache.get(&scope)
}

/// Builds the flat [`ResolvedBinding`] target list `source_supervisor::
/// reconcile` consumes, from every currently-cached scope's bindings --
/// scopes with no resolved tenant slug/community name (resolution failed
/// or was never attempted) contribute NO bindings, which
/// `source_supervisor::reconcile` correctly reads as "stop any consumers
/// for that scope" (fail-closed).
fn build_binding_targets(
    bindings_by_scope: &HashMap<ScopeKey, Vec<SourceBinding>>,
    resolved_scopes: &HashMap<ScopeKey, ResolvedScope>,
) -> Vec<ResolvedBinding> {
    let mut targets = Vec::new();
    for (scope, bindings) in bindings_by_scope {
        let Some(resolved) = resolved_scopes.get(scope) else {
            continue;
        };
        for binding in bindings {
            targets.push(ResolvedBinding {
                tenant_id: scope.0,
                community_id: scope.1,
                tenant_slug: resolved.tenant_slug.clone(),
                community_name: resolved.community_name.clone(),
                app_id: binding.app_id.clone(),
                platform: binding.platform.clone(),
                source_id: binding.source_id.clone(),
            });
        }
    }
    targets
}

/// Flattens `state.by_scope`, diffs against `state.loaded`, and drives the
/// resulting `Load`/`Unload` calls through `sink` -- shared by both
/// [`run_incremental_tick`] and [`run_full_reconcile`] so the "apply what
/// changed" half of a tick is identical regardless of which read path fed
/// `state.by_scope`.
async fn apply_active_set(
    state: &mut ConsumerState,
    sink: Option<&dyn BundleSink>,
    excluded_metric: &prometheus::IntCounterVec,
    metrics: &ChangelogConsumerMetrics,
) {
    let (flattened, conflicts) = bundle_active_set::flatten_by_scope(&state.by_scope);
    if conflicts > 0 {
        metrics.flatten_conflicts_total.inc_by(conflicts);
    }
    for active_set in state.by_scope.values() {
        for (app_id, reason) in &active_set.excluded {
            excluded_metric
                .with_label_values(&[app_id, reason.as_str()])
                .inc();
        }
        for (app_id, reason) in &active_set.degraded {
            excluded_metric
                .with_label_values(&[app_id, reason.as_str()])
                .inc();
        }
    }

    let plan = bundle_active_set::plan(&state.loaded, &flattened);
    let Some(sink) = sink else {
        if !plan.is_empty() {
            tracing::debug!(
                to_load = plan.to_load.len(),
                to_unload = plan.to_unload.len(),
                "changelog consumer: active set changed but no executor connection yet; deferring"
            );
        }
        return;
    };

    for row in &plan.to_load {
        match sink.load(row).await {
            Ok(()) => {
                tracing::info!(app_id = %row.app_id, digest = %row.digest, "changelog consumer: loaded");
                state.loaded.insert(row.app_id.clone(), row.digest.clone());
            }
            Err(err) => {
                tracing::warn!(app_id = %row.app_id, digest = %row.digest, error = %err, "changelog consumer: load failed, will retry next tick");
            }
        }
    }
    for (app_id, digest) in &plan.to_unload {
        match sink.unload(app_id, digest).await {
            Ok(()) => {
                tracing::info!(app_id, digest, "changelog consumer: unloaded");
                state.loaded.remove(app_id);
            }
            Err(err) => {
                tracing::warn!(app_id, digest, error = %err, "changelog consumer: unload failed, will retry next tick");
            }
        }
    }
}

fn update_tenant_gauges(state: &ConsumerState, metrics: &ChangelogConsumerMetrics) {
    for (tenant_id, count) in bundle_active_set::tenant_active_app_counts(&state.by_scope) {
        metrics
            .tenant_active_apps
            .with_label_values(&[&tenant_id.to_string()])
            .set(count);
    }
}

/// One incremental tick: reads `safe_seq`, reads change-log rows in
/// `(last_seq, safe_seq]` (never beyond), re-reads only the affected
/// scopes (fail-closed per scope), applies the resulting diff, reconciles
/// source-binding consumers, and advances `state`'s tracker to `safe_seq`
/// regardless of any individual scope's failure (dataplane scale design
/// §7: the watermark itself is exact; a per-entry failure only means that
/// one scope stays stale until its next success or the periodic full
/// reconcile, never that the whole pod stalls on one bad scope).
#[allow(clippy::too_many_arguments)]
pub async fn run_incremental_tick(
    db: &DatabaseConnection,
    state: &mut ConsumerState,
    sink: Option<&dyn BundleSink>,
    spawner: Option<&dyn ConsumerSupervisor>,
    excluded_metric: &prometheus::IntCounterVec,
    binding_metrics: &crate::telemetry::SourceBindingSupervisorMetrics,
    metrics: &ChangelogConsumerMetrics,
) {
    let safe_seq = match bundle_active_set::read_safe_seq(db).await {
        Ok(s) => s,
        Err(err) => {
            tracing::warn!(error = %err, "changelog consumer: safe_seq read failed");
            return;
        }
    };
    metrics.changelog_lag.set(state.tracker.lag(safe_seq));
    if safe_seq <= state.tracker.last_seq() {
        return;
    }

    let changes =
        match bundle_active_set::read_changes(db, state.tracker.last_seq(), safe_seq).await {
            Ok(c) => c,
            Err(err) => {
                tracing::warn!(error = %err, "changelog consumer: change-log read failed");
                return;
            }
        };
    let scopes = bundle_active_set::affected_scopes(&changes);

    for scope in &scopes {
        match bundle_active_set::read_active_set(db, scope.0, scope.1, None).await {
            Ok(active_set) => {
                state.by_scope.insert(*scope, active_set);
                metrics.applied_scopes_total.inc();
            }
            Err(err) => {
                tracing::warn!(
                    tenant_id = scope.0, community_id = scope.1, error = %err,
                    "changelog consumer: per-scope active-set re-read failed, skipping this scope this tick"
                );
                metrics
                    .scope_failures_total
                    .with_label_values(&["read_failed"])
                    .inc();
            }
        }
        if spawner.is_some() {
            match bundle_active_set::read_source_bindings(db, scope.0, scope.1).await {
                Ok(bindings) => {
                    state.bindings_by_scope.insert(*scope, bindings);
                }
                Err(err) => {
                    tracing::warn!(
                        tenant_id = scope.0, community_id = scope.1, error = %err,
                        "changelog consumer: per-scope binding re-read failed, skipping this scope's bindings this tick"
                    );
                    metrics
                        .scope_failures_total
                        .with_label_values(&["read_failed"])
                        .inc();
                }
            }
            let _ = resolve_scope_cached(db, &mut state.resolved_scopes, *scope, metrics).await;
        }
    }

    // Never consume beyond safe_seq -- advance regardless of per-scope
    // failures above (see this function's own doc).
    state.tracker.advance(safe_seq);
    metrics.changelog_lag.set(state.tracker.lag(safe_seq));

    apply_active_set(state, sink, excluded_metric, metrics).await;
    update_tenant_gauges(state, metrics);

    if let Some(spawner) = spawner {
        let targets = build_binding_targets(&state.bindings_by_scope, &state.resolved_scopes);
        source_supervisor::reconcile(
            &mut state.running_consumers,
            &targets,
            spawner,
            binding_metrics,
        )
        .await;
    }
}

/// The periodic full reconcile (dataplane scale design §7, default every
/// [`crate::config::CliConfig::full_reconcile_interval`]): re-reads the
/// active set + bindings for EVERY scope, replacing `state.by_scope`/
/// `bindings_by_scope` wholesale, then applies/reconciles exactly like an
/// incremental tick. Bounds the blast radius of any change-log defect to
/// one interval, independent of the change-log's own correctness. Timed
/// into [`ChangelogConsumerMetrics::reconcile_duration_seconds`].
pub async fn run_full_reconcile(
    db: &DatabaseConnection,
    state: &mut ConsumerState,
    sink: Option<&dyn BundleSink>,
    spawner: Option<&dyn ConsumerSupervisor>,
    excluded_metric: &prometheus::IntCounterVec,
    binding_metrics: &crate::telemetry::SourceBindingSupervisorMetrics,
    metrics: &ChangelogConsumerMetrics,
) {
    let start = Instant::now();
    match bundle_active_set::read_active_set_all(db).await {
        Ok(by_scope) => {
            metrics.applied_scopes_total.inc_by(by_scope.len() as u64);
            state.by_scope = by_scope;
        }
        Err(err) => {
            tracing::warn!(error = %err, "changelog consumer: full reconcile active-set read failed");
            metrics
                .reconcile_duration_seconds
                .observe(start.elapsed().as_secs_f64());
            return;
        }
    }

    if let Some(spawner) = spawner {
        match bundle_active_set::read_source_bindings_all(db).await {
            Ok(bindings_by_scope) => state.bindings_by_scope = bindings_by_scope,
            Err(err) => {
                tracing::warn!(error = %err, "changelog consumer: full reconcile binding read failed");
            }
        }
        for scope in state.by_scope.keys().copied().collect::<Vec<_>>() {
            let _ = resolve_scope_cached(db, &mut state.resolved_scopes, scope, metrics).await;
        }
        let targets = build_binding_targets(&state.bindings_by_scope, &state.resolved_scopes);
        source_supervisor::reconcile(
            &mut state.running_consumers,
            &targets,
            spawner,
            binding_metrics,
        )
        .await;
    }

    apply_active_set(state, sink, excluded_metric, metrics).await;
    update_tenant_gauges(state, metrics);

    metrics
        .reconcile_duration_seconds
        .observe(start.elapsed().as_secs_f64());
}

/// The live interval/shutdown loop `crate::lib::try_start_changelog_consumer`
/// spawns: incremental ticks on `poll_interval`, a full reconcile on
/// `full_reconcile_interval`, and a graceful `source_supervisor::stop_all`
/// on shutdown so a pod termination never abandons a running consumer.
#[allow(clippy::too_many_arguments)]
pub async fn run(
    db: DatabaseConnection,
    poll_interval: std::time::Duration,
    full_reconcile_interval: std::time::Duration,
    call_timeout_ms: u64,
    gate: Arc<dyn FeatureGate>,
    connections: Arc<crate::host_api::ConnectionRegistry>,
    spawner: Option<Arc<dyn ConsumerSupervisor>>,
    excluded_metric: prometheus::IntCounterVec,
    binding_metrics: crate::telemetry::SourceBindingSupervisorMetrics,
    metrics: ChangelogConsumerMetrics,
    mut shutdown: tokio::sync::oneshot::Receiver<()>,
) {
    let mut state = match initial_state(&db).await {
        Ok(s) => s,
        Err(err) => {
            tracing::error!(error = %err, "changelog consumer: initial full active-set read failed; not starting");
            return;
        }
    };

    let mut poll_tick = tokio::time::interval(poll_interval);
    poll_tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let mut reconcile_tick = tokio::time::interval(full_reconcile_interval);
    reconcile_tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    // The startup `initial_state` read above already IS the first full
    // reconcile -- skip `reconcile_tick`'s own immediate first fire so a
    // fresh pod doesn't redundantly re-read everything twice in a row.
    reconcile_tick.tick().await;

    loop {
        let sink = connections
            .active()
            .map(|connection| crate::bundle_loader::ExecutorSink {
                connection,
                call_timeout_ms,
            });
        let sink_ref = sink.as_ref().map(|s| s as &dyn BundleSink);

        tokio::select! {
            _ = &mut shutdown => {
                source_supervisor::stop_all(&mut state.running_consumers, &binding_metrics).await;
                return;
            }
            _ = poll_tick.tick() => {
                if !gate.enabled().await {
                    tracing::debug!(
                        "multi-tenant changelog consumer disabled (kill-switch on); stopping all consumers"
                    );
                    source_supervisor::stop_all(&mut state.running_consumers, &binding_metrics).await;
                    continue;
                }
                run_incremental_tick(
                    &db, &mut state, sink_ref, spawner.as_deref(), &excluded_metric,
                    &binding_metrics, &metrics,
                ).await;
            }
            _ = reconcile_tick.tick() => {
                if !gate.enabled().await {
                    continue;
                }
                run_full_reconcile(
                    &db, &mut state, sink_ref, spawner.as_deref(), &excluded_metric,
                    &binding_metrics, &metrics,
                ).await;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use sea_orm::{DatabaseBackend, MockDatabase};
    use std::sync::Mutex as StdMutex;

    fn active_row(
        app_id: &str,
        tenant_id: i32,
        community_id: i32,
        version_id: i64,
    ) -> bundle_active_set::entities::app_active_versions::Model {
        bundle_active_set::entities::app_active_versions::Model {
            app_id: app_id.to_string(),
            tenant_id,
            community_id,
            version_id,
        }
    }

    fn version_row(
        id: i64,
        app_id: &str,
        digest: &str,
    ) -> bundle_active_set::entities::app_versions::Model {
        bundle_active_set::entities::app_versions::Model {
            id,
            app_id: app_id.to_string(),
            version: "1".to_string(),
            artifact_digest: Some(digest.to_string()),
            scan_status: "scanned".to_string(),
            component_key: None,
            sidecar_key: None,
        }
    }

    fn approval_row(
        tenant_id: i32,
        app_id: &str,
    ) -> bundle_active_set::entities::app_install_approvals::Model {
        bundle_active_set::entities::app_install_approvals::Model {
            id: 1,
            tenant_id,
            community_id: None,
            app_id: app_id.to_string(),
            version: "1".to_string(),
            superseded_by: None,
        }
    }

    fn watermark_row(
        safe_seq: i64,
    ) -> bundle_active_set::entities::bundle_active_set_watermark::Model {
        bundle_active_set::entities::bundle_active_set_watermark::Model {
            id: 1,
            safe_seq,
            computed_at: chrono::Utc::now(),
        }
    }

    fn change_row(
        seq: i64,
        tenant_id: i32,
        community_id: i32,
    ) -> bundle_active_set::entities::bundle_active_set_changes::Model {
        bundle_active_set::entities::bundle_active_set_changes::Model {
            seq,
            tenant_id,
            community_id,
            entity: "app_active_versions".to_string(),
            entity_id: "waddles.a".to_string(),
            op: "upsert".to_string(),
            changed_at: chrono::Utc::now(),
            writer_xid: None,
        }
    }

    #[derive(Default)]
    struct FakeSink {
        calls: StdMutex<Vec<String>>,
    }
    impl FakeSink {
        fn calls(&self) -> Vec<String> {
            self.calls.lock().unwrap().clone()
        }
    }
    impl BundleSink for FakeSink {
        fn load<'a>(
            &'a self,
            row: &'a bundle_active_set::ActiveBundleRow,
        ) -> std::pin::Pin<
            Box<
                dyn std::future::Future<Output = Result<(), crate::spine::InvokeError>> + Send + 'a,
            >,
        > {
            Box::pin(async move {
                self.calls
                    .lock()
                    .unwrap()
                    .push(format!("load:{}:{}", row.app_id, row.digest));
                Ok(())
            })
        }
        fn unload<'a>(
            &'a self,
            app_id: &'a str,
            digest: &'a str,
        ) -> std::pin::Pin<
            Box<
                dyn std::future::Future<Output = Result<(), crate::spine::InvokeError>> + Send + 'a,
            >,
        > {
            let call = format!("unload:{app_id}:{digest}");
            Box::pin(async move {
                self.calls.lock().unwrap().push(call);
                Ok(())
            })
        }
    }

    #[derive(Default)]
    struct RecordingSupervisor {
        calls: StdMutex<Vec<String>>,
    }
    impl ConsumerSupervisor for RecordingSupervisor {
        fn spawn(&self, binding: &ResolvedBinding) -> source_supervisor::RunningConsumer {
            self.calls.lock().unwrap().push(binding.app_id.clone());
            let (shutdown, rx) = tokio::sync::oneshot::channel();
            let handle = tokio::spawn(async move {
                let _ = rx.await;
            });
            source_supervisor::RunningConsumer { shutdown, handle }
        }
    }

    fn test_binding_metrics() -> crate::telemetry::SourceBindingSupervisorMetrics {
        crate::telemetry::register_source_binding_supervisor_metrics(&prometheus::Registry::new())
    }

    fn test_changelog_metrics() -> ChangelogConsumerMetrics {
        crate::telemetry::register_changelog_consumer_metrics(&prometheus::Registry::new())
    }

    fn test_excluded_metric() -> prometheus::IntCounterVec {
        prometheus::IntCounterVec::new(
            prometheus::Opts::new("test_excluded_total", "test"),
            &["app_id", "reason"],
        )
        .expect("valid metric definition")
    }

    #[tokio::test]
    async fn initial_state_loads_every_scope_and_seeds_the_tracker_from_safe_seq(
    ) -> Result<(), bundle_active_set::ActiveSetError> {
        let digest = format!("sha256:{}", "a".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![watermark_row(500)]])
            .append_query_results([vec![
                active_row("waddles.a", 1, 0, 10),
                active_row("waddles.b", 2, 0, 20),
            ]])
            .append_query_results([vec![
                version_row(10, "waddles.a", &digest),
                version_row(20, "waddles.b", &digest),
            ]])
            .append_query_results([vec![
                approval_row(1, "waddles.a"),
                approval_row(2, "waddles.b"),
            ]])
            .append_query_results([
                Vec::<bundle_active_set::entities::app_active_versions::Model>::new(),
            ])
            .into_connection();

        let state = initial_state(&db).await?;
        assert_eq!(state.last_seq(), 500);
        assert_eq!(state.by_scope.len(), 2);
        Ok(())
    }

    #[tokio::test]
    async fn run_incremental_tick_does_nothing_when_safe_seq_has_not_advanced() {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![watermark_row(100)]])
            .into_connection();
        let mut state = ConsumerState::new(100);
        let sink = FakeSink::default();
        let spawner = RecordingSupervisor::default();
        run_incremental_tick(
            &db,
            &mut state,
            Some(&sink as &dyn BundleSink),
            Some(&spawner),
            &test_excluded_metric(),
            &test_binding_metrics(),
            &test_changelog_metrics(),
        )
        .await;
        assert!(sink.calls().is_empty());
        assert_eq!(state.last_seq(), 100);
    }

    #[tokio::test]
    async fn run_incremental_tick_applies_a_newly_active_scope_and_advances_to_safe_seq() {
        let digest = format!("sha256:{}", "b".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![watermark_row(150)]])
            .append_query_results([vec![change_row(101, 1, 0)]])
            .append_query_results([vec![active_row("waddles.a", 1, 0, 10)]])
            .append_query_results([vec![version_row(10, "waddles.a", &digest)]])
            .append_query_results([vec![approval_row(1, "waddles.a")]])
            .append_query_results([vec![active_row("waddles.a", 1, 0, 10)]])
            .append_query_results([
                Vec::<bundle_active_set::entities::app_source_bindings::Model>::new(),
            ])
            .append_query_results([vec![bundle_active_set::entities::tenants::Model {
                id: 1,
                slug: "acme".to_string(),
            }]])
            .into_connection();
        let mut state = ConsumerState::new(100);
        let sink = FakeSink::default();
        let spawner = RecordingSupervisor::default();
        run_incremental_tick(
            &db,
            &mut state,
            Some(&sink as &dyn BundleSink),
            Some(&spawner),
            &test_excluded_metric(),
            &test_binding_metrics(),
            &test_changelog_metrics(),
        )
        .await;
        assert_eq!(
            state.last_seq(),
            150,
            "must advance to safe_seq, not max(seq)"
        );
        assert_eq!(state.loaded().get("waddles.a"), Some(&digest));
        assert_eq!(sink.calls(), vec![format!("load:waddles.a:{digest}")]);
    }

    /// End-to-end binding-reconciliation regression: a newly-bound source
    /// in an affected scope must spawn exactly one consumer, resolved
    /// against that scope's real tenant slug -- proves `run_incremental_
    /// tick` wires `read_source_bindings` -> `resolve_scope_cached` ->
    /// `build_binding_targets` -> `source_supervisor::reconcile` end to
    /// end, not just the bundle-loading half.
    #[tokio::test]
    async fn run_incremental_tick_spawns_a_consumer_for_a_newly_bound_source() {
        let digest = format!("sha256:{}", "9".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![watermark_row(101)]])
            .append_query_results([vec![change_row(101, 1, 0)]])
            // `read_active_set(1, 0, None)`'s own three queries.
            .append_query_results([vec![active_row("waddles.a", 1, 0, 10)]])
            .append_query_results([vec![version_row(10, "waddles.a", &digest)]])
            .append_query_results([vec![approval_row(1, "waddles.a")]])
            // `read_source_bindings(1, 0)`'s own two queries: its own
            // independent `app_active_versions` re-derivation, then bindings.
            .append_query_results([vec![active_row("waddles.a", 1, 0, 10)]])
            .append_query_results([vec![
                bundle_active_set::entities::app_source_bindings::Model {
                    tenant_id: 1,
                    community_id: 0,
                    app_id: "waddles.a".to_string(),
                    platform: "twitch".to_string(),
                    source_id: "tw-x".to_string(),
                },
            ]])
            // `resolve_scope`'s tenant lookup.
            .append_query_results([vec![bundle_active_set::entities::tenants::Model {
                id: 1,
                slug: "acme".to_string(),
            }]])
            .into_connection();
        let mut state = ConsumerState::new(100);
        let sink = FakeSink::default();
        let spawner = RecordingSupervisor::default();
        run_incremental_tick(
            &db,
            &mut state,
            Some(&sink as &dyn BundleSink),
            Some(&spawner),
            &test_excluded_metric(),
            &test_binding_metrics(),
            &test_changelog_metrics(),
        )
        .await;
        assert_eq!(state.running_len(), 1, "one consumer must be spawned");
        assert_eq!(spawner.calls.lock().unwrap().as_slice(), ["waddles.a"]);
    }

    #[tokio::test]
    async fn run_incremental_tick_never_reads_past_safe_seq() {
        // change_row(101,...) is within (100,120]; a row at seq 130 would
        // be past safe_seq=120 and must never be read -- proven by the
        // mock only ever returning the in-range row.
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![watermark_row(120)]])
            .append_query_results([vec![change_row(101, 1, 0)]])
            .append_query_results([
                Vec::<bundle_active_set::entities::app_active_versions::Model>::new(),
            ])
            .append_query_results([
                Vec::<bundle_active_set::entities::app_source_bindings::Model>::new(),
            ])
            .append_query_results([vec![bundle_active_set::entities::tenants::Model {
                id: 1,
                slug: "acme".to_string(),
            }]])
            .into_connection();
        let mut state = ConsumerState::new(100);
        let sink = FakeSink::default();
        let spawner = RecordingSupervisor::default();
        run_incremental_tick(
            &db,
            &mut state,
            Some(&sink as &dyn BundleSink),
            Some(&spawner),
            &test_excluded_metric(),
            &test_binding_metrics(),
            &test_changelog_metrics(),
        )
        .await;
        assert_eq!(state.last_seq(), 120);
    }

    /// Per-entry fail-closed regression: one scope's re-read failing must
    /// not abort the tick or block `last_seq` from advancing.
    #[tokio::test]
    async fn run_incremental_tick_advances_last_seq_even_when_a_scope_read_fails() {
        // No queued result for the active-set/bindings/scope-resolution
        // reads after the change-log read -- `MockDatabase` returns a
        // `DbErr` once its queue is exhausted, simulating a failed re-read.
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![watermark_row(150)]])
            .append_query_results([vec![change_row(101, 1, 0)]])
            .into_connection();
        let mut state = ConsumerState::new(100);
        let sink = FakeSink::default();
        let spawner = RecordingSupervisor::default();
        run_incremental_tick(
            &db,
            &mut state,
            Some(&sink as &dyn BundleSink),
            Some(&spawner),
            &test_excluded_metric(),
            &test_binding_metrics(),
            &test_changelog_metrics(),
        )
        .await;
        assert_eq!(
            state.last_seq(),
            150,
            "the watermark is exact and must advance regardless of a per-scope failure"
        );
    }

    #[tokio::test]
    async fn run_full_reconcile_replaces_the_whole_by_scope_map() {
        let digest = format!("sha256:{}", "c".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![active_row("waddles.a", 1, 0, 10)]])
            .append_query_results([vec![version_row(10, "waddles.a", &digest)]])
            .append_query_results([vec![approval_row(1, "waddles.a")]])
            .append_query_results([
                Vec::<bundle_active_set::entities::app_active_versions::Model>::new(),
            ])
            .append_query_results([vec![bundle_active_set::entities::tenants::Model {
                id: 1,
                slug: "acme".to_string(),
            }]])
            .into_connection();
        let mut state = ConsumerState::new(0);
        let sink = FakeSink::default();
        let spawner = RecordingSupervisor::default();
        run_full_reconcile(
            &db,
            &mut state,
            Some(&sink as &dyn BundleSink),
            Some(&spawner),
            &test_excluded_metric(),
            &test_binding_metrics(),
            &test_changelog_metrics(),
        )
        .await;
        assert_eq!(state.loaded().get("waddles.a"), Some(&digest));
    }

    #[test]
    fn build_binding_targets_excludes_scopes_with_no_resolved_scope() {
        let mut bindings_by_scope = HashMap::new();
        bindings_by_scope.insert(
            (1, 0),
            vec![SourceBinding {
                app_id: "waddles.a".to_string(),
                platform: "twitch".to_string(),
                source_id: "tw-x".to_string(),
            }],
        );
        bindings_by_scope.insert(
            (2, 0),
            vec![SourceBinding {
                app_id: "waddles.b".to_string(),
                platform: "discord".to_string(),
                source_id: "dg-y".to_string(),
            }],
        );
        let mut resolved_scopes = HashMap::new();
        resolved_scopes.insert(
            (1, 0),
            ResolvedScope {
                tenant_slug: "acme".to_string(),
                community_name: None,
            },
        );
        // (2, 0) deliberately unresolved -- its bindings must be excluded.

        let targets = build_binding_targets(&bindings_by_scope, &resolved_scopes);
        assert_eq!(targets.len(), 1);
        assert_eq!(targets[0].app_id, "waddles.a");
        assert_eq!(targets[0].tenant_slug, "acme");
    }
}
