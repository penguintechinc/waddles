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
use std::time::{Duration, Instant};

use bundle_active_set::{
    ActiveSetRead, AppScope, ChangeLogTracker, ResolvedScope, ScopeKey, SourceBinding,
};
use sea_orm::DatabaseConnection;

use crate::bundle_loader::BundleSink;
use crate::license::FeatureGate;
use crate::source_supervisor::{self, ConsumerSupervisor, ResolvedBinding, RunningConsumers};
use crate::telemetry::ChangelogConsumerMetrics;

/// How long a scope may keep failing its active-set re-read before this
/// consumer gives up on its last-known-good rows and evicts them (fail
/// closed) rather than running an unboundedly stale bundle set forever on
/// one persistently-broken scope (Gemini review on PR #396: "never run
/// indefinitely on stale config"). A conservative default pending a
/// configurable knob as a follow-up -- generous enough that a transient DB
/// blip or a few unlucky ticks never evicts a healthy scope, short enough
/// that a genuinely broken scope (e.g. a dangling cross-tenant community id)
/// doesn't serve a wrong/outdated bundle set indefinitely.
#[cfg(not(test))]
const SCOPE_STALE_EVICTION_BOUND: Duration = Duration::from_secs(3600);
#[cfg(test)]
const SCOPE_STALE_EVICTION_BOUND: Duration = Duration::from_millis(30);

/// Whether a scope that just failed its active-set re-read has been failing
/// for longer than `bound` since its last success -- pure, no I/O, so
/// `SCOPE_STALE_EVICTION_BOUND`'s eviction trigger is directly unit-testable
/// with synthetic `Instant`s (constructed via addition from a single base
/// point, never subtraction -- avoids an underflow panic risk on a
/// freshly-booted CI container whose monotonic clock has not yet
/// accumulated an hour). `None` (never succeeded yet) is never stale --
/// there is nothing established yet to evict.
fn is_scope_stale(last_success: Option<Instant>, now: Instant, bound: Duration) -> bool {
    match last_success {
        Some(t) => now.duration_since(t) > bound,
        None => false,
    }
}

/// Whether `run`'s loop should call `source_supervisor::stop_all` this tick
/// -- true only on the enabled->disabled transition, never on every
/// already-disabled tick (Gemini review on PR #396, HIGH: the pre-fix
/// version called `stop_all` unconditionally every tick the kill-switch was
/// on, which is wasteful busywork after the first tick since
/// `running_consumers` is already empty by then). Pure so this is directly
/// unit-testable without a live interval loop.
fn should_stop_consumers(currently_enabled: bool, were_enabled: bool) -> bool {
    !currently_enabled && were_enabled
}

/// Detects a NEW executor connection becoming active, by pointer identity,
/// compared to the last one this consumer observed -- updates
/// `last_connection_id` in place and reports whether a change occurred.
/// Pure (no I/O), so directly unit-testable.
///
/// **Why this matters (item 4, gh security review on PR #406):** the
/// executor wipes its ENTIRE bundle registry on every disconnect
/// (`core/bundle_executor/src/invoke.rs`'s `on_disconnect`) -- "on
/// reconnect, the consumer sends its full authoritative scope set, which
/// REPLACES that peer's scopes" is only true if this consumer's OWN
/// `state.loaded` bookkeeping is reset on a detected reconnect too;
/// otherwise it would believe everything it previously loaded is still
/// resident and never resend the `Load`s the freshly-reconnected (empty)
/// executor actually needs, leaving a real (if self-healing on the next
/// full reconcile) availability gap. `crate::host_api::ConnectionRegistry::
/// set_active` constructs a brand-new `Connection` for every accepted TCP
/// connection (see that type's own doc), so pointer identity is a reliable,
/// zero-cost way to detect this without any wire-level signal.
fn detect_new_connection<T>(
    current: Option<&Arc<T>>,
    last_connection_id: &mut Option<usize>,
) -> bool {
    let current_id = current.map(|c| Arc::as_ptr(c) as usize);
    let changed = match (current_id, *last_connection_id) {
        (Some(cur), Some(last)) => cur != last,
        (Some(_), None) => true,
        (None, _) => false,
    };
    if let Some(id) = current_id {
        *last_connection_id = Some(id);
    }
    changed
}

/// All state one running consumer instance carries across ticks --
/// constructed once by [`initial_state`], mutated in place by every
/// subsequent [`run_incremental_tick`]/[`run_full_reconcile`] call.
pub struct ConsumerState {
    by_scope: HashMap<ScopeKey, ActiveSetRead>,
    bindings_by_scope: HashMap<ScopeKey, Vec<SourceBinding>>,
    resolved_scopes: HashMap<ScopeKey, ResolvedScope>,
    /// `AppScope` (`(tenant_id, community_id, app_id)`) -> the digest this
    /// consumer has already told the executor to load for that exact scope
    /// -- **never collapsed onto `app_id` alone** (the retired multi-tenant
    /// correctness bug this module was fixed for): two different scopes can
    /// independently reference two different digests of the same `app_id`,
    /// each tracked as its own entry.
    loaded: HashMap<AppScope, String>,
    running_consumers: RunningConsumers,
    tracker: ChangeLogTracker,
    /// Last time each scope's active-set re-read succeeded -- the basis for
    /// [`SCOPE_STALE_EVICTION_BOUND`]'s fail-closed eviction. A scope with
    /// no entry here has either never succeeded yet (nothing to evict) or
    /// was already evicted (fresh baseline on its next success).
    scope_last_success: HashMap<ScopeKey, Instant>,
    /// Whether `bundle_active_set_watermark.min_retained_seq` exists in this
    /// environment's schema (hub-api migration `0026`/PR #397) -- probed
    /// ONCE by [`initial_state`] and cached here for the process's lifetime;
    /// `false` on an older hub-api means the primary retention check in
    /// [`run_incremental_tick`] never fires, and the heuristic gap-detection
    /// fallback remains the sole detector.
    retention_supported: bool,
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
            scope_last_success: HashMap::new(),
            // Tests assume the column is present by default (matches every
            // existing `watermark_row(...)` fixture); tests exercising the
            // "older hub-api" fallback set this explicitly via
            // `#[cfg(test)] fn with_retention_supported`.
            retention_supported: true,
        }
    }

    #[cfg(test)]
    fn with_retention_supported(mut self, supported: bool) -> Self {
        self.retention_supported = supported;
        self
    }

    #[cfg(test)]
    fn loaded(&self) -> &HashMap<AppScope, String> {
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

    #[cfg(test)]
    fn by_scope_len(&self) -> usize {
        self.by_scope.len()
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
    // Probed ONCE (never per-tick) and cached for this consumer's lifetime
    // (`ConsumerState::retention_supported`) -- an older hub-api schema
    // (migration `0026`/PR #397 not yet applied) must never prevent this
    // consumer from starting at all; it only means the primary retention
    // check never fires, and the heuristic gap-detection fallback
    // (`run_incremental_tick`'s own doc) remains the sole detector.
    let retention_supported = bundle_active_set::probe_min_retained_seq_supported(db).await?;
    let safe_seq = bundle_active_set::read_safe_seq_watermark(db, retention_supported)
        .await?
        .safe_seq;
    let by_scope = bundle_active_set::read_active_set_all(db).await?;
    let bindings_by_scope = bundle_active_set::read_source_bindings_all(db).await?;
    let scope_last_success = by_scope.keys().map(|s| (*s, Instant::now())).collect();
    Ok(ConsumerState {
        by_scope,
        bindings_by_scope,
        resolved_scopes: HashMap::new(),
        loaded: HashMap::new(),
        running_consumers: HashMap::new(),
        tracker: ChangeLogTracker::new(safe_seq),
        scope_last_success,
        retention_supported,
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
    kv_capabilities: &bundle_host_kv::CapabilitySnapshot,
) {
    // Scope-preserving: NEVER collapses two different `(tenant, community)`
    // scopes' independently-active digests for the same `app_id` onto one
    // flat `app_id`-keyed slot (the retired `flatten_by_scope` did, which
    // was this multi-tenant rewrite's correctness bug -- see this crate's
    // own `scoped_active_rows`/`plan_scoped` docs and `core/bundle_executor/
    // src/invoke.rs`'s digest-keyed, refcounted registry that makes this
    // safe).
    let active = bundle_active_set::scoped_active_rows(&state.by_scope);
    // Mirrors `core/svc_action/src/changelog_consumer.rs`'s identical
    // refresh (PR #425 coordinator fix): every active app's declared-
    // capability snapshot is refreshed every tick, not just the diffed
    // to_load set.
    for row in active.values() {
        kv_capabilities.update(row.app_id.clone(), row.declared_capabilities.clone());
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

    let plan = bundle_active_set::plan_scoped(&state.loaded, &active);
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

    for (scope, row) in &plan.to_load {
        match sink.load(scope.0, scope.1, row).await {
            Ok(()) => {
                tracing::info!(
                    tenant_id = scope.0, community_id = scope.1,
                    app_id = %row.app_id, digest = %row.digest,
                    "changelog consumer: loaded"
                );
                state.loaded.insert(scope.clone(), row.digest.clone());
            }
            Err(err) => {
                tracing::warn!(
                    tenant_id = scope.0, community_id = scope.1,
                    app_id = %row.app_id, digest = %row.digest, error = %err,
                    "changelog consumer: load failed, will retry next tick"
                );
            }
        }
    }
    for (scope, digest) in &plan.to_unload {
        match sink.unload(scope.0, scope.1, &scope.2, digest).await {
            Ok(()) => {
                tracing::info!(
                    tenant_id = scope.0, community_id = scope.1,
                    app_id = %scope.2, digest,
                    "changelog consumer: unloaded"
                );
                state.loaded.remove(scope);
            }
            Err(err) => {
                tracing::warn!(
                    tenant_id = scope.0, community_id = scope.1,
                    app_id = %scope.2, digest, error = %err,
                    "changelog consumer: unload failed, will retry next tick"
                );
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
/// source-binding consumers, and advances `state`'s tracker -- to `safe_seq`
/// when every affected scope's re-read succeeded, or only as far as is
/// SAFE when one or more failed (see the "partial advance" note below;
/// Gemini review on PR #396, HIGH).
///
/// **Partial advance on a per-scope failure:** the tracker must never
/// advance past a `seq` whose scope re-read failed -- doing so would
/// permanently skip that row (the next tick's `read_changes` starts strictly
/// after `last_seq`, so a failed scope's own change would never be looked
/// at again once `last_seq` passes it). Instead, on any active-set re-read
/// failure this tick advances only to `(lowest failed scope's first
/// affecting seq) - 1`, floored at the tracker's current `last_seq` (never
/// regresses) -- every scope at or before that point is either successfully
/// applied or was never affected this tick, so it's safe to consider
/// consumed; the failed scope (and anything after it) is retried from
/// scratch next tick. `changes` is already sorted ascending by `seq`
/// (`bundle_active_set::read_changes`'s own contract), so the first change
/// row naming a given scope is that scope's earliest-affecting seq.
///
/// **Retention-gap fail-safe (hub-api migration `0026`/PR #397's
/// `min_retained_seq`, 48h change-log retention):** when this crate's schema
/// has the `min_retained_seq` column (`state.retention_supported`, probed
/// once at startup via `probe_min_retained_seq_supported`), the PRIMARY
/// check below compares `last_seq + 1` against `min_retained_seq` on every
/// tick and is authoritative -- it forces a full reconcile the moment this
/// consumer has fallen behind the retention floor, before ever attempting a
/// partial apply. On an older hub-api schema without the column yet,
/// `min_retained_seq` reads as `0` (see `read_safe_seq_watermark`'s own
/// doc) and the primary check becomes a structural no-op; the FALLBACK
/// heuristic further below -- a returned `changes` set whose LOWEST `seq`
/// is strictly greater than `last_seq + 1` -- remains the sole detector
/// until that hub-api is upgraded. Either path short-circuits straight to a
/// full multi-tenant reconcile instead of applying a partial/misleading
/// incremental diff, resetting the tracker to `safe_seq` (a full reconcile
/// re-reads every scope fresh, so there is nothing left to apply
/// incrementally).
#[allow(clippy::too_many_arguments)]
pub async fn run_incremental_tick(
    db: &DatabaseConnection,
    state: &mut ConsumerState,
    sink: Option<&dyn BundleSink>,
    spawner: Option<&dyn ConsumerSupervisor>,
    excluded_metric: &prometheus::IntCounterVec,
    binding_metrics: &crate::telemetry::SourceBindingSupervisorMetrics,
    metrics: &ChangelogConsumerMetrics,
    kv_capabilities: &bundle_host_kv::CapabilitySnapshot,
) {
    let start = Instant::now();
    let watermark =
        match bundle_active_set::read_safe_seq_watermark(db, state.retention_supported).await {
            Ok(w) => w,
            Err(err) => {
                tracing::warn!(error = %err, "changelog consumer: safe_seq read failed");
                return;
            }
        };
    let safe_seq = watermark.safe_seq;
    metrics.changelog_lag.set(state.tracker.lag(safe_seq));
    if safe_seq <= state.tracker.last_seq() {
        return;
    }

    // PRIMARY retention check (hub-api migration `0026`/PR #397's
    // `min_retained_seq`, authoritative once present): `last_seq + 1` is a
    // row this consumer would need to see next, but retention has already
    // pruned everything below `min_retained_seq` -- that row is gone for
    // good. Never attempt a partial apply against a horizon retention has
    // already invalidated; force a full multi-tenant reconcile instead.
    // `min_retained_seq` reads as `0` on an older hub-api schema without
    // this column yet (`read_safe_seq_watermark`'s own doc), which makes
    // this check a structural no-op there -- the "lowest returned change
    // row's seq" heuristic below remains the sole detector until upgraded.
    if state.tracker.last_seq() + 1 < watermark.min_retained_seq {
        tracing::warn!(
            last_seq = state.tracker.last_seq(),
            min_retained_seq = watermark.min_retained_seq,
            safe_seq,
            "changelog consumer: last_seq has fallen behind change-log retention \
             (min_retained_seq); forcing a full reconcile instead of a partial apply"
        );
        metrics.changelog_retention_exceeded_total.inc();
        run_full_reconcile(
            db,
            state,
            sink,
            spawner,
            excluded_metric,
            binding_metrics,
            metrics,
            kv_capabilities,
        )
        .await;
        state.tracker.advance(safe_seq);
        metrics.changelog_lag.set(state.tracker.lag(safe_seq));
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

    // FALLBACK gap heuristic (older hub-api schema without `min_retained_
    // seq` yet, or a genuine unexplained gap the primary check above didn't
    // catch): a returned change set whose lowest `seq` exceeds `last_seq +
    // 1` means at least one row in that range is unaccounted for.
    if let Some(first) = changes.first() {
        if first.seq > state.tracker.last_seq() + 1 {
            tracing::error!(
                last_seq = state.tracker.last_seq(),
                first_returned_seq = first.seq,
                safe_seq,
                "changelog consumer: detected a change-log gap (likely retention truncation \
                 after falling behind); forcing a full reconcile instead of a partial apply"
            );
            metrics.changelog_gap_detected_total.inc();
            run_full_reconcile(
                db,
                state,
                sink,
                spawner,
                excluded_metric,
                binding_metrics,
                metrics,
                kv_capabilities,
            )
            .await;
            state.tracker.advance(safe_seq);
            metrics.changelog_lag.set(state.tracker.lag(safe_seq));
            return;
        }
    }

    // First-affecting seq per scope, in ascending order (see this
    // function's own "partial advance" doc) -- used both to bound the
    // tracker advance on a failure and to attribute a staleness check to
    // the right scope.
    let mut first_seq_for_scope: HashMap<ScopeKey, i64> = HashMap::new();
    // regression: watermark id INT2 vs i32 decode killed active-set consumer (alpha 2026-10-02)
    // `ChangeRow::scope_key()` skips non-tenant-scoped rows (`tenant_id IS
    // NULL`, e.g. an `app_versions` change) -- there is no scope to bound a
    // partial advance against for those; the periodic full reconcile picks
    // them up instead (see `bundle_active_set::changelog`'s module doc).
    for c in &changes {
        if let Some(key) = c.scope_key() {
            first_seq_for_scope.entry(key).or_insert(c.seq);
        }
    }
    let scopes = bundle_active_set::affected_scopes(&changes);

    let mut min_failure_seq: Option<i64> = None;
    let now = Instant::now();
    for scope in &scopes {
        match bundle_active_set::read_active_set(db, scope.0, scope.1, None).await {
            Ok(active_set) => {
                state.by_scope.insert(*scope, active_set);
                state.scope_last_success.insert(*scope, now);
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
                let seq = first_seq_for_scope.get(scope).copied().unwrap_or(safe_seq);
                min_failure_seq = Some(min_failure_seq.map_or(seq, |m: i64| m.min(seq)));

                // Fail-closed staleness bound (Gemini review, LOW): a scope
                // that has been failing for longer than
                // `SCOPE_STALE_EVICTION_BOUND` since its last success has
                // its last-known-good rows evicted rather than served
                // forever -- `apply_active_set`'s diff naturally unloads
                // whatever was loaded for this scope once `state.by_scope`
                // no longer has an entry for it.
                let stale = is_scope_stale(
                    state.scope_last_success.get(scope).copied(),
                    now,
                    SCOPE_STALE_EVICTION_BOUND,
                );
                if stale {
                    tracing::error!(
                        tenant_id = scope.0,
                        community_id = scope.1,
                        "changelog consumer: scope has failed to re-read for longer than the \
                         staleness bound; evicting its last-known-good bundle set (fail closed)"
                    );
                    state.by_scope.remove(scope);
                    state.scope_last_success.remove(scope);
                    metrics.scope_stale_evicted_total.inc();
                }
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

    // Never consume beyond safe_seq, and never past a failed scope's own
    // first-affecting seq either (see this function's own "partial advance"
    // doc) -- `.max(state.tracker.last_seq())` is a defensive floor so a
    // pathological `min_failure_seq` before the current `last_seq` can never
    // regress the tracker.
    let new_last_seq = match min_failure_seq {
        Some(seq) => (seq - 1).max(state.tracker.last_seq()),
        None => safe_seq,
    };
    state.tracker.advance(new_last_seq);
    metrics.changelog_lag.set(state.tracker.lag(safe_seq));

    apply_active_set(state, sink, excluded_metric, kv_capabilities).await;
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
    metrics
        .reconcile_duration_seconds
        .observe(start.elapsed().as_secs_f64());
}

/// The periodic full reconcile (dataplane scale design §7, default every
/// [`crate::config::CliConfig::full_reconcile_interval`]): re-reads the
/// active set + bindings for EVERY scope, replacing `state.by_scope`/
/// `bindings_by_scope` wholesale, then applies/reconciles exactly like an
/// incremental tick. Bounds the blast radius of any change-log defect to
/// one interval, independent of the change-log's own correctness. Timed
/// into [`ChangelogConsumerMetrics::reconcile_duration_seconds`].
#[allow(clippy::too_many_arguments)]
pub async fn run_full_reconcile(
    db: &DatabaseConnection,
    state: &mut ConsumerState,
    sink: Option<&dyn BundleSink>,
    spawner: Option<&dyn ConsumerSupervisor>,
    excluded_metric: &prometheus::IntCounterVec,
    binding_metrics: &crate::telemetry::SourceBindingSupervisorMetrics,
    metrics: &ChangelogConsumerMetrics,
    kv_capabilities: &bundle_host_kv::CapabilitySnapshot,
) {
    let start = Instant::now();
    match bundle_active_set::read_active_set_all(db).await {
        Ok(by_scope) => {
            metrics.applied_scopes_total.inc_by(by_scope.len() as u64);
            let now = Instant::now();
            // Every scope in a fresh full reconcile is, by definition, just
            // successfully re-read -- reset the staleness baseline for all
            // of them (and drop any scope no longer present at all, which
            // otherwise would leave a stale timestamp for a scope this pod
            // will never see fail again).
            state.scope_last_success = by_scope.keys().map(|s| (*s, now)).collect();
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

    apply_active_set(state, sink, excluded_metric, kv_capabilities).await;
    update_tenant_gauges(state, metrics);

    metrics
        .reconcile_duration_seconds
        .observe(start.elapsed().as_secs_f64());
}

/// How long [`run`] keeps retrying [`initial_state`] with capped backoff
/// before giving up and exiting the process (regression: a startup decode
/// failure used to log one ERROR and silently return, leaving the pod
/// `Ready` with no consumer at all and no crashloop signal -- alpha
/// 2026-10-02, see this module's own `run` doc).
#[cfg(not(test))]
const INITIAL_STATE_RETRY_GRACE: Duration = Duration::from_secs(120);
#[cfg(test)]
const INITIAL_STATE_RETRY_GRACE: Duration = Duration::from_millis(50);

/// Capped backoff ceiling between `initial_state` retry attempts -- same
/// cap `crate::lib::try_start_process_loop`'s own connect retry uses.
const INITIAL_STATE_RETRY_BACKOFF_MAX: Duration = Duration::from_secs(30);

/// What [`run`]'s retry loop should do after one more failed
/// [`initial_state`] attempt -- pure, no I/O, so the grace-period/backoff
/// decision is unit-testable without ever exercising the real
/// `std::process::exit` [`run`] calls on [`RetryDecision::GiveUp`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum RetryDecision {
    /// Sleep this long (or until shutdown) before the next attempt.
    Retry(Duration),
    /// `elapsed >= grace` -- the caller must exit, never loop again.
    GiveUp,
}

/// Decides [`RetryDecision`] for attempt number `attempt` (1-based) after
/// `elapsed` time has passed since the first attempt.
fn decide_retry(
    attempt: u32,
    elapsed: Duration,
    grace: Duration,
    backoff_max: Duration,
) -> RetryDecision {
    if elapsed >= grace {
        RetryDecision::GiveUp
    } else {
        RetryDecision::Retry(crate::backoff_for_attempt(attempt, backoff_max))
    }
}

/// The live interval/shutdown loop `crate::lib::try_start_changelog_consumer`
/// spawns: incremental ticks on `poll_interval`, a full reconcile on
/// `full_reconcile_interval`, and a graceful `source_supervisor::stop_all`
/// on shutdown so a pod termination never abandons a running consumer.
///
/// `consumer_loop_ready` (regression: watermark id INT2 vs i32 decode killed
/// active-set consumer, alpha 2026-10-02): held `false` for as long as the
/// initial full active-set read keeps failing, so `/healthz` reports
/// `degraded` instead of silently staying `Ready` with no consumer -- fail
/// loud, never silent (user requirement). Retries with capped exponential
/// backoff, logging an ERROR with the rendered error per attempt; if
/// [`INITIAL_STATE_RETRY_GRACE`] elapses without success, this process
/// exits non-zero so Kubernetes restarts it visibly rather than leaving an
/// unrecoverable pod running indefinitely.
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
    kv_capabilities: Arc<bundle_host_kv::CapabilitySnapshot>,
    binding_metrics: crate::telemetry::SourceBindingSupervisorMetrics,
    metrics: ChangelogConsumerMetrics,
    consumer_loop_ready: Arc<std::sync::atomic::AtomicBool>,
    mut shutdown: tokio::sync::oneshot::Receiver<()>,
) {
    consumer_loop_ready.store(false, std::sync::atomic::Ordering::Relaxed);
    let retry_started_at = Instant::now();
    let mut attempt: u32 = 0;
    let mut state = loop {
        attempt += 1;
        match initial_state(&db).await {
            Ok(s) => break s,
            Err(err) => {
                tracing::error!(
                    error = %err,
                    attempt,
                    elapsed_secs = retry_started_at.elapsed().as_secs(),
                    "changelog consumer: initial full active-set read failed; retrying"
                );
                let decision = decide_retry(
                    attempt,
                    retry_started_at.elapsed(),
                    INITIAL_STATE_RETRY_GRACE,
                    INITIAL_STATE_RETRY_BACKOFF_MAX,
                );
                let backoff = match decision {
                    RetryDecision::GiveUp => {
                        tracing::error!(
                            attempts = attempt,
                            grace_secs = INITIAL_STATE_RETRY_GRACE.as_secs(),
                            "changelog consumer: initial full active-set read still failing after \
                             the retry grace period; exiting so Kubernetes restarts this pod"
                        );
                        std::process::exit(1);
                    }
                    RetryDecision::Retry(backoff) => backoff,
                };
                if crate::wait_or_shutdown(&mut shutdown, backoff).await {
                    tracing::info!(
                        "changelog consumer: shutdown received while retrying initial \
                         active-set read; exiting without starting"
                    );
                    return;
                }
            }
        }
    };
    consumer_loop_ready.store(true, std::sync::atomic::Ordering::Relaxed);

    let mut poll_tick = tokio::time::interval(poll_interval);
    poll_tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let mut reconcile_tick = tokio::time::interval(full_reconcile_interval);
    reconcile_tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    // The startup `initial_state` read above already IS the first full
    // reconcile -- skip `reconcile_tick`'s own immediate first fire so a
    // fresh pod doesn't redundantly re-read everything twice in a row.
    reconcile_tick.tick().await;

    // Tracks whether the gate was enabled as of the last poll tick, so the
    // kill-switch-disabled branch below calls `stop_all` exactly once on
    // the ON->OFF transition, never on every single disabled tick (Gemini
    // review on PR #396, HIGH: `stop_all` used to run unconditionally every
    // tick the gate was off, which is wasteful and -- since `stop_all`
    // drains `state.running_consumers`, already empty after the first call
    // -- a no-op busywork loop after the first tick anyway). Starts `true`:
    // `initial_state` above always performs its full read regardless of the
    // gate, so the very first disabled tick is a genuine transition worth
    // acting on.
    let mut consumers_were_enabled = true;
    // See `detect_new_connection`'s own doc (item 4, gh security review on
    // PR #406). `None`: no connection observed yet.
    let mut last_connection_id: Option<usize> = None;

    loop {
        let active_connection = connections.active();
        if detect_new_connection(active_connection.as_ref(), &mut last_connection_id) {
            tracing::info!(
                "changelog consumer: detected a new executor connection; resetting loaded-state \
                 so the full authoritative active set is resent (the executor wipes its own \
                 registry on every disconnect)"
            );
            state.loaded.clear();
            metrics.executor_reconnect_detected_total.inc();
        }
        let sink = active_connection.map(|connection| crate::bundle_loader::ExecutorSink {
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
                let enabled = gate.enabled().await;
                if should_stop_consumers(enabled, consumers_were_enabled) {
                    tracing::debug!(
                        "multi-tenant changelog consumer disabled (kill-switch on); stopping all consumers"
                    );
                    source_supervisor::stop_all(&mut state.running_consumers, &binding_metrics).await;
                }
                consumers_were_enabled = enabled;
                if !enabled {
                    continue;
                }
                run_incremental_tick(
                    &db, &mut state, sink_ref, spawner.as_deref(), &excluded_metric,
                    &binding_metrics, &metrics, &kv_capabilities,
                ).await;
            }
            _ = reconcile_tick.tick() => {
                if !gate.enabled().await {
                    continue;
                }
                run_full_reconcile(
                    &db, &mut state, sink_ref, spawner.as_deref(), &excluded_metric,
                    &binding_metrics, &metrics, &kv_capabilities,
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

    /// regression: watermark id INT2 vs i32 decode killed active-set consumer (alpha 2026-10-02)
    /// Below the grace period, every attempt gets a `Retry` with the same
    /// backoff `crate::backoff_for_attempt` would return directly -- never
    /// `GiveUp` early.
    #[test]
    fn decide_retry_retries_with_capped_backoff_before_the_grace_period_elapses() {
        let grace = Duration::from_secs(120);
        let backoff_max = Duration::from_secs(30);
        assert_eq!(
            decide_retry(1, Duration::from_secs(0), grace, backoff_max),
            RetryDecision::Retry(Duration::from_secs(1))
        );
        assert_eq!(
            decide_retry(3, Duration::from_secs(10), grace, backoff_max),
            RetryDecision::Retry(Duration::from_secs(4))
        );
        assert_eq!(
            decide_retry(10, Duration::from_secs(100), grace, backoff_max),
            RetryDecision::Retry(backoff_max),
            "backoff is capped at backoff_max, never grows unbounded"
        );
    }

    /// regression: watermark id INT2 vs i32 decode killed active-set consumer (alpha 2026-10-02)
    /// At or past the grace period, the decision is `GiveUp` -- `run`'s
    /// caller then exits non-zero instead of retrying forever.
    #[test]
    fn decide_retry_gives_up_once_the_grace_period_has_elapsed() {
        let grace = Duration::from_secs(120);
        let backoff_max = Duration::from_secs(30);
        assert_eq!(
            decide_retry(50, grace, grace, backoff_max),
            RetryDecision::GiveUp,
            "exactly at the grace boundary must give up, not retry one more time"
        );
        assert_eq!(
            decide_retry(50, Duration::from_secs(121), grace, backoff_max),
            RetryDecision::GiveUp
        );
    }

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
            summary_json: sea_orm::JsonValue::Null,
        }
    }

    /// Mocked row for `probe_min_retained_seq_supported`'s `information_
    /// schema.columns` query when the `min_retained_seq` column exists.
    fn retention_probe_row_supported() -> std::collections::BTreeMap<String, sea_orm::Value> {
        std::collections::BTreeMap::from([("?column?".to_string(), sea_orm::Value::Int(Some(1)))])
    }

    fn watermark_row(
        safe_seq: i64,
    ) -> bundle_active_set::entities::bundle_active_set_watermark::Model {
        watermark_row_with_retention(safe_seq, 0)
    }

    fn watermark_row_with_retention(
        safe_seq: i64,
        min_retained_seq: i64,
    ) -> bundle_active_set::entities::bundle_active_set_watermark::Model {
        bundle_active_set::entities::bundle_active_set_watermark::Model {
            id: 1,
            safe_seq,
            min_retained_seq,
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
            tenant_id: Some(tenant_id),
            community_id: Some(community_id),
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
            _tenant_id: i32,
            _community_id: i32,
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
            _tenant_id: i32,
            _community_id: i32,
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
            .append_query_results([vec![retention_probe_row_supported()]])
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
        assert!(
            state.retention_supported,
            "the probe query returned a row -- the column is present"
        );
        Ok(())
    }

    /// Item 5 (Gemini re-review of PR #406): an older hub-api whose schema
    /// predates migration `0026`/PR #397 (no `min_retained_seq` column at
    /// all) must NOT prevent this consumer from starting -- `initial_state`
    /// probes first, finds it absent, and falls back to a `safe_seq`-only
    /// read to seed the tracker.
    #[tokio::test]
    async fn initial_state_starts_successfully_when_min_retained_seq_column_is_absent(
    ) -> Result<(), bundle_active_set::ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            // The probe query: an EMPTY result means the column is absent.
            .append_query_results([
                Vec::<std::collections::BTreeMap<String, sea_orm::Value>>::new(),
            ])
            // The safe_seq-only fallback read (never references the missing
            // column).
            .append_query_results([vec![std::collections::BTreeMap::from([(
                "safe_seq".to_string(),
                sea_orm::Value::BigInt(Some(500)),
            )])]])
            .append_query_results([
                Vec::<bundle_active_set::entities::app_active_versions::Model>::new(),
            ])
            .append_query_results([
                Vec::<bundle_active_set::entities::app_active_versions::Model>::new(),
            ])
            .into_connection();

        let state = initial_state(&db).await?;
        assert!(
            !state.retention_supported,
            "an empty probe result must be read as \"column absent\""
        );
        assert_eq!(
            state.last_seq(),
            500,
            "the safe_seq-only fallback must still seed the tracker correctly"
        );
        Ok(())
    }

    /// The counterpart at tick level: with `retention_supported: false`, the
    /// primary retention check must never fire (it always sees
    /// `min_retained_seq: 0`) -- a genuine gap is still caught by the
    /// existing heuristic fallback instead, proving the two checks are
    /// independent and the older-schema case degrades gracefully rather
    /// than losing gap detection entirely.
    #[tokio::test]
    async fn run_incremental_tick_relies_on_the_heuristic_fallback_when_retention_unsupported() {
        let digest = format!("sha256:{}", "9".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            // safe_seq-only read (retention_supported: false).
            .append_query_results([vec![std::collections::BTreeMap::from([(
                "safe_seq".to_string(),
                sea_orm::Value::BigInt(Some(150)),
            )])]])
            // The lowest returned seq (120) is past last_seq+1 (101) --
            // caught by the heuristic fallback, never the (unavailable)
            // primary retention check.
            .append_query_results([vec![change_row(120, 1, 0)]])
            .append_query_results([vec![active_row("waddles.a", 1, 0, 10)]])
            .append_query_results([vec![version_row(10, "waddles.a", &digest)]])
            .append_query_results([vec![approval_row(1, "waddles.a")]])
            .into_connection();
        let mut state = ConsumerState::new(100).with_retention_supported(false);
        let sink = FakeSink::default();
        let metrics = test_changelog_metrics();
        run_incremental_tick(
            &db,
            &mut state,
            Some(&sink as &dyn BundleSink),
            None,
            &test_excluded_metric(),
            &test_binding_metrics(),
            &metrics,
            &bundle_host_kv::CapabilitySnapshot::new(),
        )
        .await;

        assert_eq!(state.last_seq(), 150);
        assert_eq!(
            metrics.changelog_gap_detected_total.get(),
            1,
            "the heuristic fallback must still catch the gap"
        );
        assert_eq!(
            metrics.changelog_retention_exceeded_total.get(),
            0,
            "the primary check must never fire when retention is unsupported"
        );
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
            &bundle_host_kv::CapabilitySnapshot::new(),
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
            &bundle_host_kv::CapabilitySnapshot::new(),
        )
        .await;
        assert_eq!(
            state.last_seq(),
            150,
            "must advance to safe_seq, not max(seq)"
        );
        assert_eq!(
            state.loaded().get(&(1, 0, "waddles.a".to_string())),
            Some(&digest)
        );
        assert_eq!(sink.calls(), vec![format!("load:waddles.a:{digest}")]);
    }

    /// Boundary proof for the PRIMARY retention check: `last_seq + 1 ==
    /// min_retained_seq` is still WITHIN retention (the check is strictly
    /// `<`, not `<=`), so this must take the ordinary incremental path --
    /// `read_changes` is queried and applied, never short-circuited to a
    /// full reconcile. Complements `run_incremental_tick_forces_a_full_
    /// reconcile_when_behind_min_retained_seq` (one seq further behind),
    /// pinning the exact edge of the enforced range.
    #[tokio::test]
    async fn run_incremental_tick_applies_incrementally_when_exactly_at_the_retention_floor() {
        let digest = format!("sha256:{}", "c".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![watermark_row_with_retention(150, 101)]])
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
        let metrics = test_changelog_metrics();
        run_incremental_tick(
            &db,
            &mut state,
            Some(&sink as &dyn BundleSink),
            Some(&spawner),
            &test_excluded_metric(),
            &test_binding_metrics(),
            &metrics,
            &bundle_host_kv::CapabilitySnapshot::new(),
        )
        .await;
        assert_eq!(
            state.last_seq(),
            150,
            "exactly-at-floor must still advance to safe_seq via the normal incremental path"
        );
        assert_eq!(sink.calls(), vec![format!("load:waddles.a:{digest}")]);
        assert_eq!(
            metrics.changelog_retention_exceeded_total.get(),
            0,
            "the primary check must not fire when last_seq + 1 == min_retained_seq"
        );
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
            &bundle_host_kv::CapabilitySnapshot::new(),
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
            &bundle_host_kv::CapabilitySnapshot::new(),
        )
        .await;
        assert_eq!(state.last_seq(), 120);
    }

    /// Per-entry fail-closed regression, updated for the partial-advance fix
    /// (Gemini review on PR #396, HIGH): one scope's re-read failing must
    /// not abort the tick, but it also must NOT let `last_seq` advance past
    /// that failed scope's own affecting seq -- doing so would permanently
    /// skip the change (the next tick's `read_changes` starts strictly
    /// after `last_seq`). With only one change in range and its scope
    /// failing, nothing is safe to consider consumed yet, so `last_seq`
    /// stays exactly where it was; the failed scope is retried next tick.
    #[tokio::test]
    async fn run_incremental_tick_does_not_advance_past_a_failed_scopes_own_seq() {
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
            &bundle_host_kv::CapabilitySnapshot::new(),
        )
        .await;
        assert_eq!(
            state.last_seq(),
            100,
            "the only change's scope failed -- nothing is safe to advance past yet"
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
            &bundle_host_kv::CapabilitySnapshot::new(),
        )
        .await;
        assert_eq!(
            state.loaded().get(&(1, 0, "waddles.a".to_string())),
            Some(&digest)
        );
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

    // --- Gemini review fixes (PR #396/#397) -----------------------------

    #[test]
    fn is_scope_stale_is_false_before_the_bound_and_true_after() {
        let t0 = Instant::now();
        let bound = Duration::from_secs(60);
        assert!(!is_scope_stale(
            Some(t0),
            t0 + Duration::from_secs(30),
            bound
        ));
        assert!(is_scope_stale(
            Some(t0),
            t0 + Duration::from_secs(61),
            bound
        ));
    }

    #[test]
    fn is_scope_stale_is_false_when_never_succeeded() {
        assert!(!is_scope_stale(
            None,
            Instant::now(),
            Duration::from_secs(1)
        ));
    }

    #[test]
    fn should_stop_consumers_only_on_the_enabled_to_disabled_transition() {
        assert!(
            should_stop_consumers(false, true),
            "must stop exactly on the transition"
        );
        assert!(
            !should_stop_consumers(false, false),
            "must not repeat on every already-disabled tick"
        );
        assert!(!should_stop_consumers(true, true));
        assert!(!should_stop_consumers(true, false));
    }

    /// Item 4 (gh security review on PR #406): a genuinely new connection
    /// (a different `Arc` allocation -- exactly what `ConnectionRegistry::
    /// set_active` produces for every accepted TCP connection) must be
    /// detected; the SAME connection observed again, or no connection at
    /// all, must not be.
    #[test]
    fn detect_new_connection_fires_only_on_a_genuine_identity_change() {
        let mut last = None;
        let a = Arc::new(());
        let b = Arc::new(());

        assert!(
            detect_new_connection(Some(&a), &mut last),
            "the very first observed connection is always a change"
        );
        assert!(
            !detect_new_connection(Some(&a), &mut last),
            "the same connection observed again must not re-trigger"
        );
        assert!(
            detect_new_connection(Some(&b), &mut last),
            "a different Arc allocation must be detected as a new connection"
        );
        assert!(
            !detect_new_connection(Some(&b), &mut last),
            "b observed again must not re-trigger"
        );
        assert!(
            !detect_new_connection(Option::<&Arc<()>>::None, &mut last),
            "no connection currently active is never itself a \"new connection\" event"
        );
        assert!(
            detect_new_connection(Some(&a), &mut last),
            "a reappearing after a None gap is still a change from b (the last real connection)"
        );
    }

    /// Partial-advance regression (Gemini review, HIGH): scope (1,0) at seq
    /// 101 succeeds, scope (2,0) at seq 102 fails -- the tracker must
    /// advance only to 101 (never past the failed scope's own seq), so the
    /// failed scope is retried from scratch next tick instead of being
    /// permanently skipped.
    #[tokio::test]
    async fn run_incremental_tick_advances_only_up_to_the_scope_before_a_failure() {
        let digest = format!("sha256:{}", "5".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![watermark_row(150)]])
            .append_query_results([vec![change_row(101, 1, 0), change_row(102, 2, 0)]])
            // scope (1, 0)'s active-set re-read succeeds.
            .append_query_results([vec![active_row("waddles.a", 1, 0, 10)]])
            .append_query_results([vec![version_row(10, "waddles.a", &digest)]])
            .append_query_results([vec![approval_row(1, "waddles.a")]])
            // scope (2, 0)'s active-set re-read: queue exhausted -> DbErr.
            .into_connection();
        let mut state = ConsumerState::new(100);
        let sink = FakeSink::default();
        run_incremental_tick(
            &db,
            &mut state,
            Some(&sink as &dyn BundleSink),
            None,
            &test_excluded_metric(),
            &test_binding_metrics(),
            &test_changelog_metrics(),
            &bundle_host_kv::CapabilitySnapshot::new(),
        )
        .await;
        assert_eq!(
            state.last_seq(),
            101,
            "must not advance past the failed scope (2,0)'s own affecting seq (102)"
        );
        assert_eq!(
            state.loaded().get(&(1, 0, "waddles.a".to_string())),
            Some(&digest),
            "the succeeding scope must still apply this tick"
        );
    }

    /// Stale-eviction regression (Gemini review, LOW): a scope that keeps
    /// failing longer than `SCOPE_STALE_EVICTION_BOUND` (shortened to 30ms
    /// under `#[cfg(test)]`) since its last success must have its
    /// last-known-good rows evicted, which drives an `unload` for whatever
    /// it had previously loaded -- never served indefinitely stale.
    #[tokio::test]
    async fn run_incremental_tick_evicts_a_scope_stale_beyond_the_bound() {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![watermark_row(150)]])
            .append_query_results([vec![change_row(101, 1, 0)]])
            // scope (1, 0)'s active-set re-read: queue exhausted -> DbErr.
            .into_connection();
        let mut state = ConsumerState::new(100);
        state.by_scope.insert(
            (1, 0),
            bundle_active_set::ActiveSetRead {
                rows: vec![bundle_active_set::ActiveBundleRow {
                    app_id: "waddles.a".to_string(),
                    version: "1".to_string(),
                    digest: "sha256:00".to_string(),
                    component_key: "k".to_string(),
                    sidecar_key: "s".to_string(),
                    declared_capabilities: Vec::new(),
                }],
                excluded: Vec::new(),
                degraded: Vec::new(),
            },
        );
        state
            .loaded
            .insert((1, 0, "waddles.a".to_string()), "sha256:00".to_string());
        state.scope_last_success.insert((1, 0), Instant::now());
        // Guarantee the 30ms test-only staleness bound has elapsed --
        // deterministic via a real (short) sleep, never clock subtraction.
        tokio::time::sleep(Duration::from_millis(60)).await;

        let sink = FakeSink::default();
        run_incremental_tick(
            &db,
            &mut state,
            Some(&sink as &dyn BundleSink),
            None,
            &test_excluded_metric(),
            &test_binding_metrics(),
            &test_changelog_metrics(),
            &bundle_host_kv::CapabilitySnapshot::new(),
        )
        .await;

        assert_eq!(
            state.by_scope_len(),
            0,
            "the stale scope's last-known-good rows must be evicted"
        );
        assert_eq!(
            sink.calls(),
            vec!["unload:waddles.a:sha256:00".to_string()],
            "eviction must drive an unload through apply_active_set's diff"
        );
    }

    /// Primary retention regression (hub-api migration `0026`/PR #397's
    /// `min_retained_seq`): `last_seq + 1` (101) is already below
    /// `min_retained_seq` (150) -- retention has pruned that row for good --
    /// so this must force a full reconcile WITHOUT ever reading `changes` at
    /// all (no query queued for it; the mock would error if one were
    /// attempted, proving the primary check short-circuits before the
    /// heuristic fallback gets a chance to run).
    #[tokio::test]
    async fn run_incremental_tick_forces_a_full_reconcile_when_behind_min_retained_seq() {
        let digest = format!("sha256:{}", "8".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![watermark_row_with_retention(200, 150)]])
            // The forced full reconcile's own reads (no `read_changes` query
            // queued -- the primary retention check must short-circuit
            // before ever reading the change log):
            .append_query_results([vec![active_row("waddles.a", 1, 0, 10)]])
            .append_query_results([vec![version_row(10, "waddles.a", &digest)]])
            .append_query_results([vec![approval_row(1, "waddles.a")]])
            .into_connection();
        let mut state = ConsumerState::new(100);
        let sink = FakeSink::default();
        let metrics = test_changelog_metrics();
        run_incremental_tick(
            &db,
            &mut state,
            Some(&sink as &dyn BundleSink),
            None,
            &test_excluded_metric(),
            &test_binding_metrics(),
            &metrics,
            &bundle_host_kv::CapabilitySnapshot::new(),
        )
        .await;

        assert_eq!(
            state.last_seq(),
            200,
            "falling behind retention resets the tracker straight to safe_seq"
        );
        assert_eq!(
            metrics.changelog_retention_exceeded_total.get(),
            1,
            "the primary-confirmed retention breach must be counted, never silent"
        );
        assert_eq!(
            metrics.changelog_gap_detected_total.get(),
            0,
            "the primary check firing must not also count as the heuristic fallback"
        );
        assert_eq!(
            sink.calls(),
            vec![format!("load:waddles.a:{digest}")],
            "the full reconcile's own active set must still apply"
        );
    }

    /// Retention-gap regression (Gemini review on PR #397): a returned
    /// change set whose lowest `seq` exceeds `last_seq + 1` must never be
    /// partially applied -- it forces a full reconcile and resets the
    /// tracker straight to `safe_seq`.
    #[tokio::test]
    async fn run_incremental_tick_forces_a_full_reconcile_on_a_detected_changelog_gap() {
        let digest = format!("sha256:{}", "7".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![watermark_row(150)]])
            // The lowest returned seq (120) is far past last_seq+1 (101) --
            // rows in between are unexplained (retention truncation).
            .append_query_results([vec![change_row(120, 1, 0)]])
            // The forced full reconcile's own reads:
            // `read_active_set_all`'s one transaction (active/version/approval).
            .append_query_results([vec![active_row("waddles.a", 1, 0, 10)]])
            .append_query_results([vec![version_row(10, "waddles.a", &digest)]])
            .append_query_results([vec![approval_row(1, "waddles.a")]])
            .into_connection();
        let mut state = ConsumerState::new(100);
        let sink = FakeSink::default();
        let metrics = test_changelog_metrics();
        run_incremental_tick(
            &db,
            &mut state,
            Some(&sink as &dyn BundleSink),
            None,
            &test_excluded_metric(),
            &test_binding_metrics(),
            &metrics,
            &bundle_host_kv::CapabilitySnapshot::new(),
        )
        .await;

        assert_eq!(
            state.last_seq(),
            150,
            "a detected gap resets the tracker straight to safe_seq"
        );
        assert_eq!(
            metrics.changelog_gap_detected_total.get(),
            1,
            "the gap must be counted, never silent"
        );
        assert_eq!(
            sink.calls(),
            vec![format!("load:waddles.a:{digest}")],
            "the full reconcile's own active set must still apply"
        );
    }
}
