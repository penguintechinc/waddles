//! Multi-tenant, change-log-driven active-bundle loader (dataplane scale
//! design rev 4, §7/§8 step 2: "Multi-tenant watermark polling"). Replaces
//! the retired single-`(tenant_id, community_id)` `crate::bundle_loader::run`
//! poll loop -- this module owns the ONLY loop now; `bundle_loader`'s own
//! non-loop mechanics (`BundleSink`/`ExecutorSink`) are reused verbatim,
//! just driven from here. Direct port of `core/svc_process/src/
//! changelog_consumer.rs` with the source-binding-supervisor half omitted
//! (this stage has no `app_source_bindings` consumer) -- see that module's
//! doc for the full rationale, reproduced here only where it differs.
//!
//! **User requirement, restated exactly:** "every svc_process/svc_action
//! pod serves ALL tenants" -- there is no more `BUNDLE_SCOPE_TENANT_ID`/
//! `BUNDLE_SCOPE_COMMUNITY_ID` scope. On start, [`initial_state`] does a
//! full active-set read across EVERY `(tenant_id, community_id)` scope in
//! the database (`bundle_active_set::read_active_set_all`) and seeds a
//! [`bundle_active_set::ChangeLogTracker`] from the `safe_seq` observed
//! around that same read. Every subsequent tick ([`run_incremental_tick`])
//! reads the primary-published `safe_seq`, reads only the change-log rows
//! in `(last_seq, safe_seq]` (**never consuming beyond `safe_seq`**), and
//! re-reads ONLY the `(tenant_id, community_id)` scopes those rows name --
//! fail-closed PER SCOPE (a re-read failure logs + increments a metric and
//! skips just that scope). [`run_full_reconcile`] periodically re-runs the
//! full multi-scope read regardless of change-log state, bounding the
//! blast radius of any change-log defect to one interval (design §7).
//!
//! **Multi-tenant correctness fix:** bundle load/unload is keyed by
//! `AppScope` (`(tenant_id, community_id, app_id)`), never flattened onto
//! `app_id` alone -- see `bundle_active_set::{scoped_active_rows,
//! plan_scoped}` and `core/bundle_executor/src/invoke.rs`'s digest-keyed,
//! refcounted registry, which is what makes two different scopes safely
//! sharing or independently versioning the same `app_id` correct.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};

use bundle_active_set::{ActiveSetRead, AppScope, ChangeLogTracker, FullSyncReason, ScopeKey};
use sea_orm::DatabaseConnection;

use crate::bundle_loader::BundleSink;
use crate::flags::FeatureFlag;
use crate::telemetry::ChangelogConsumerMetrics;

/// How long a scope may keep failing its active-set re-read before this
/// consumer gives up on its last-known-good rows and evicts them (fail
/// closed) rather than running an unboundedly stale bundle set forever on
/// one persistently-broken scope (Gemini review on PR #396: "never run
/// indefinitely on stale config"). A conservative default pending a
/// configurable knob as a follow-up. Shortened under `#[cfg(test)]` -- see
/// `core/svc_process/src/changelog_consumer.rs`'s identical constant for why
/// (avoids an `Instant` subtraction underflow risk; the eviction test uses a
/// real short `tokio::time::sleep` instead).
#[cfg(not(test))]
const SCOPE_STALE_EVICTION_BOUND: Duration = Duration::from_secs(3600);
#[cfg(test)]
const SCOPE_STALE_EVICTION_BOUND: Duration = Duration::from_millis(30);

/// How long [`run`]'s loop coalesces repeated full-send triggers into a
/// single forced full authoritative active-set read -- see
/// `core/svc_process/src/changelog_consumer.rs`'s identical constant for the
/// full rationale. regression: loads waited for 15-min full reconcile after
/// startup/reconnect, UnknownBundle (alpha 2026-10-03)
#[cfg(not(test))]
const FULL_SEND_DEBOUNCE: Duration = Duration::from_secs(2);
#[cfg(test)]
const FULL_SEND_DEBOUNCE: Duration = Duration::from_millis(20);

/// Whether a scope that just failed its active-set re-read has been failing
/// for longer than `bound` since its last success -- pure, no I/O; see
/// `core/svc_process/src/changelog_consumer.rs`'s identical function for the
/// full rationale.
fn is_scope_stale(last_success: Option<Instant>, now: Instant, bound: Duration) -> bool {
    match last_success {
        Some(t) => now.duration_since(t) > bound,
        None => false,
    }
}

/// Whether `run`'s loop should stop consumers this tick -- true only on the
/// enabled->disabled transition (Gemini review on PR #396, HIGH). This
/// stage has no per-binding consumers of its own to stop (unlike
/// svc_process's `source_supervisor`), so this is currently unused for a
/// live side effect, but kept symmetric with svc_process and available for
/// this stage's own future per-tenant consumers.
#[allow(dead_code)]
fn should_stop_consumers(currently_enabled: bool, were_enabled: bool) -> bool {
    !currently_enabled && were_enabled
}

/// Detects a NEW executor connection becoming active, by pointer identity --
/// see `core/svc_process/src/changelog_consumer.rs`'s identical function for
/// the full rationale (item 4, gh security review on PR #406).
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
    /// `AppScope` (`(tenant_id, community_id, app_id)`) -> the digest this
    /// consumer has already told the executor to load for that exact scope
    /// -- never collapsed onto `app_id` alone (see this module's own doc).
    loaded: HashMap<AppScope, String>,
    tracker: ChangeLogTracker,
    /// Last time each scope's active-set re-read succeeded -- the basis for
    /// [`SCOPE_STALE_EVICTION_BOUND`]'s fail-closed eviction.
    scope_last_success: HashMap<ScopeKey, Instant>,
    /// Whether `bundle_active_set_watermark.min_retained_seq` exists in this
    /// environment's schema (hub-api migration `0026`/PR #397) -- probed
    /// once by [`initial_state`]. `false` means the primary retention check
    /// in [`run_incremental_tick`] never fires; the heuristic gap fallback
    /// remains the sole detector.
    retention_supported: bool,
    /// A forced full authoritative active-set send this consumer still owes
    /// -- see `core/svc_process/src/changelog_consumer.rs`'s identical
    /// field for the full rationale. regression: loads waited for 15-min
    /// full reconcile after startup/reconnect, UnknownBundle (alpha
    /// 2026-10-03).
    pending_full_sync: Option<FullSyncReason>,
    /// When the last forced full send actually ran -- the basis for
    /// [`FULL_SEND_DEBOUNCE`]'s reconnect-storm coalescing.
    last_full_send: Option<Instant>,
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
            loaded: HashMap::new(),
            tracker: ChangeLogTracker::new(initial_seq),
            scope_last_success: HashMap::new(),
            retention_supported: true,
            // Deliberately `None` -- see `core/svc_process/src/
            // changelog_consumer.rs`'s identical ctor for why.
            pending_full_sync: None,
            last_full_send: None,
        }
    }

    #[cfg(test)]
    fn with_pending_full_sync(mut self, reason: FullSyncReason) -> Self {
        self.pending_full_sync = Some(reason);
        self
    }

    #[cfg(test)]
    fn pending_full_sync(&self) -> Option<FullSyncReason> {
        self.pending_full_sync
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
    fn by_scope_len(&self) -> usize {
        self.by_scope.len()
    }
}

/// The "on start, full active-set read for ALL tenants/communities"
/// requirement -- reads `safe_seq` FIRST, then performs the full
/// multi-scope active-set read, and seeds the [`ChangeLogTracker`] from
/// that pre-read `safe_seq` value. A tiny race (a change committing
/// between the `safe_seq` read and the full read finishing) is bounded by
/// the periodic full reconcile, never a correctness gap (see this module's
/// own doc).
pub async fn initial_state(
    db: &DatabaseConnection,
) -> Result<ConsumerState, bundle_active_set::ActiveSetError> {
    // Probed ONCE, cached for this consumer's lifetime -- see
    // `core/svc_process/src/changelog_consumer.rs`'s identical call for the
    // full rationale (an older hub-api schema must never prevent startup).
    let retention_supported = bundle_active_set::probe_min_retained_seq_supported(db).await?;
    let safe_seq = bundle_active_set::read_safe_seq_watermark(db, retention_supported)
        .await?
        .safe_seq;
    let by_scope = bundle_active_set::read_active_set_all(db).await?;
    let scope_last_success = by_scope.keys().map(|s| (*s, Instant::now())).collect();
    Ok(ConsumerState {
        by_scope,
        loaded: HashMap::new(),
        tracker: ChangeLogTracker::new(safe_seq),
        scope_last_success,
        retention_supported,
        pending_full_sync: Some(FullSyncReason::Startup),
        last_full_send: None,
    })
}

/// Flattens `state.by_scope` scope-preservingly (`bundle_active_set::
/// scoped_active_rows` -- never collapsing two scopes' independently-active
/// digests for the same `app_id` onto one slot, the retired multi-tenant
/// correctness bug), diffs against `state.loaded`, and drives the resulting
/// `Load`/`Unload` calls through `sink` -- shared by both
/// [`run_incremental_tick`] and [`run_full_reconcile`].
#[allow(clippy::too_many_arguments)]
async fn apply_active_set(
    state: &mut ConsumerState,
    sink: Option<&dyn BundleSink>,
    excluded_metric: &prometheus::IntCounterVec,
    kv_capabilities: &bundle_host_kv::CapabilitySnapshot,
    metrics: &ChangelogConsumerMetrics,
    full_sync_reason: Option<FullSyncReason>,
) {
    let active = bundle_active_set::scoped_active_rows(&state.by_scope);
    // Mirrors `crate::bundle_loader::run_tick`'s identical refresh (PR #425
    // coordinator fix): every active app's declared-capability snapshot is
    // refreshed every tick, not just the diffed to_load set -- see that
    // function's doc for the full rationale (manifest re-approval without a
    // digest bump, staleness window on eviction).
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

    if let Some(reason) = full_sync_reason {
        // Over-log by design (user rule: never silent). regression: loads
        // waited for 15-min full reconcile after startup/reconnect,
        // UnknownBundle (alpha 2026-10-03).
        tracing::info!(
            reason = %reason,
            active_count = active.len(),
            to_load = plan.to_load.len(),
            to_unload = plan.to_unload.len(),
            "changelog consumer: forcing a full authoritative active-set send"
        );
        metrics
            .bundle_full_sync_total
            .with_label_values(&[reason.as_str()])
            .inc();
    }

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
        let load_start = Instant::now();
        match sink.load(scope.0, scope.1, row).await {
            Ok(()) => {
                tracing::info!(
                    tenant_id = scope.0, community_id = scope.1,
                    app_id = %row.app_id, version = %row.version,
                    digest_prefix = %bundle_active_set::digest_prefix(&row.digest),
                    duration_ms = load_start.elapsed().as_millis() as u64,
                    "changelog consumer: loaded"
                );
                metrics
                    .bundle_loads_total
                    .with_label_values(&["success"])
                    .inc();
                state.loaded.insert(scope.clone(), row.digest.clone());
            }
            Err(err) => {
                tracing::error!(
                    tenant_id = scope.0, community_id = scope.1,
                    app_id = %row.app_id, version = %row.version,
                    digest_prefix = %bundle_active_set::digest_prefix(&row.digest),
                    duration_ms = load_start.elapsed().as_millis() as u64,
                    error = %err,
                    "changelog consumer: load failed, will retry next tick"
                );
                metrics
                    .bundle_loads_total
                    .with_label_values(&["failure"])
                    .inc();
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
    metrics.bundles_loaded.set(state.loaded.len() as i64);
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
/// scopes (fail-closed per scope), applies the resulting diff, and
/// advances `state`'s tracker -- to `safe_seq` when every affected scope's
/// re-read succeeded, or only as far as is SAFE when one or more failed.
///
/// **Partial advance on a per-scope failure (Gemini review on PR #396,
/// HIGH):** the tracker must never advance past a `seq` whose scope re-read
/// failed -- doing so would permanently skip that row. On any active-set
/// re-read failure this tick advances only to `(lowest failed scope's first
/// affecting seq) - 1`, floored at the tracker's current `last_seq`; the
/// failed scope (and anything after it) is retried from scratch next tick.
///
/// **Retention-gap fail-safe (hub-api migration `0026`/PR #397's
/// `min_retained_seq`, 48h retention):** when this crate's schema has the
/// `min_retained_seq` column (`state.retention_supported`, probed once at
/// startup), the PRIMARY check below is authoritative -- it forces a full
/// reconcile the moment this consumer has fallen behind the retention floor,
/// before ever attempting a partial apply. On an older hub-api schema
/// without the column, `min_retained_seq` reads as `0` and the primary
/// check becomes a structural no-op; the FALLBACK heuristic further down --
/// a returned `changes` set whose LOWEST `seq` is strictly greater than
/// `last_seq + 1` -- remains the sole detector until upgraded. Either path
/// short-circuits to a full multi-tenant reconcile instead of a partial
/// apply, resetting the tracker to `safe_seq`.
pub async fn run_incremental_tick(
    db: &DatabaseConnection,
    state: &mut ConsumerState,
    sink: Option<&dyn BundleSink>,
    excluded_metric: &prometheus::IntCounterVec,
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
    let active_count = bundle_active_set::scoped_active_rows(&state.by_scope).len();
    tracing::debug!(
        safe_seq,
        last_seq = state.tracker.last_seq(),
        active_count,
        loaded_count = state.loaded.len(),
        "changelog consumer: poll tick"
    );
    if safe_seq <= state.tracker.last_seq() {
        // The watermark hasn't moved, but a reconnect (or any other
        // out-of-band event) may have cleared `state.loaded` without
        // anything in the DB actually changing -- see
        // `core/svc_process/src/changelog_consumer.rs`'s identical check
        // for the full rationale. regression: loads waited for 15-min full
        // reconcile after startup/reconnect, UnknownBundle (alpha
        // 2026-10-03).
        if bundle_active_set::loaded_state_diverged(&state.by_scope, &state.loaded) {
            apply_active_set(
                state,
                sink,
                excluded_metric,
                kv_capabilities,
                metrics,
                Some(FullSyncReason::Diverged),
            )
            .await;
            update_tenant_gauges(state, metrics);
        }
        return;
    }

    // PRIMARY retention check (hub-api migration `0026`/PR #397's
    // `min_retained_seq`, authoritative once present) -- see
    // `core/svc_process/src/changelog_consumer.rs`'s identical check for the
    // full rationale.
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
            excluded_metric,
            metrics,
            kv_capabilities,
            FullSyncReason::Reconcile,
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

    // FALLBACK gap heuristic (older hub-api schema, or an unexplained gap
    // the primary check above didn't catch).
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
                excluded_metric,
                metrics,
                kv_capabilities,
                FullSyncReason::Reconcile,
            )
            .await;
            state.tracker.advance(safe_seq);
            metrics.changelog_lag.set(state.tracker.lag(safe_seq));
            return;
        }
    }

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
    }

    let new_last_seq = match min_failure_seq {
        Some(seq) => (seq - 1).max(state.tracker.last_seq()),
        None => safe_seq,
    };
    state.tracker.advance(new_last_seq);
    metrics.changelog_lag.set(state.tracker.lag(safe_seq));

    apply_active_set(state, sink, excluded_metric, kv_capabilities, metrics, None).await;
    update_tenant_gauges(state, metrics);
    metrics
        .reconcile_duration_seconds
        .observe(start.elapsed().as_secs_f64());
}

/// The periodic full reconcile (dataplane scale design §7, default every
/// [`crate::config::CliConfig::full_reconcile_interval`]): re-reads the
/// active set for EVERY scope, replacing `state.by_scope` wholesale, then
/// applies exactly like an incremental tick. Bounds the blast radius of
/// any change-log defect to one interval, independent of the change-log's
/// own correctness. Timed into
/// [`ChangelogConsumerMetrics::reconcile_duration_seconds`].
#[allow(clippy::too_many_arguments)]
pub async fn run_full_reconcile(
    db: &DatabaseConnection,
    state: &mut ConsumerState,
    sink: Option<&dyn BundleSink>,
    excluded_metric: &prometheus::IntCounterVec,
    metrics: &ChangelogConsumerMetrics,
    kv_capabilities: &bundle_host_kv::CapabilitySnapshot,
    reason: FullSyncReason,
) {
    let start = Instant::now();
    match bundle_active_set::read_active_set_all(db).await {
        Ok(by_scope) => {
            metrics.applied_scopes_total.inc_by(by_scope.len() as u64);
            let now = Instant::now();
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

    apply_active_set(
        state,
        sink,
        excluded_metric,
        kv_capabilities,
        metrics,
        Some(reason),
    )
    .await;
    update_tenant_gauges(state, metrics);

    metrics
        .reconcile_duration_seconds
        .observe(start.elapsed().as_secs_f64());
}

/// Attempts the forced full send `state.pending_full_sync` names (if any),
/// coalesced against `state.last_full_send` by `debounce` -- see
/// `core/svc_process/src/changelog_consumer.rs`'s identical function for the
/// full rationale. regression: loads waited for 15-min full reconcile after
/// startup/reconnect, UnknownBundle (alpha 2026-10-03).
#[allow(clippy::too_many_arguments)]
pub async fn drain_pending_full_sync(
    db: &DatabaseConnection,
    state: &mut ConsumerState,
    sink: Option<&dyn BundleSink>,
    excluded_metric: &prometheus::IntCounterVec,
    metrics: &ChangelogConsumerMetrics,
    kv_capabilities: &bundle_host_kv::CapabilitySnapshot,
    now: Instant,
    debounce: Duration,
) {
    let Some(reason) = state.pending_full_sync else {
        return;
    };
    if sink.is_none() {
        return;
    }
    if !bundle_active_set::should_send_full_sync(state.last_full_send, now, debounce) {
        return;
    }
    run_full_reconcile(
        db,
        state,
        sink,
        excluded_metric,
        metrics,
        kv_capabilities,
        reason,
    )
    .await;
    state.last_full_send = Some(now);
    state.pending_full_sync = None;
}

/// How long [`run`] keeps retrying [`initial_state`] with capped backoff
/// before giving up and exiting the process -- see
/// `core/svc_process/src/changelog_consumer.rs`'s identical constant for the
/// full rationale (regression: watermark id INT2 vs i32 decode killed
/// active-set consumer, alpha 2026-10-02).
#[cfg(not(test))]
const INITIAL_STATE_RETRY_GRACE: Duration = Duration::from_secs(120);
#[cfg(test)]
const INITIAL_STATE_RETRY_GRACE: Duration = Duration::from_millis(50);

/// Capped backoff ceiling between `initial_state` retry attempts -- same
/// cap `crate::lib::try_start_dispatch`'s own connect retry uses.
const INITIAL_STATE_RETRY_BACKOFF_MAX: Duration = Duration::from_secs(30);

/// What [`run`]'s retry loop should do after one more failed
/// [`initial_state`] attempt -- see `core/svc_process/src/changelog_consumer
/// .rs`'s identical type for the full rationale (pure, no I/O, so the
/// grace-period/backoff decision is unit-testable without ever exercising
/// the real `std::process::exit` [`run`] calls on [`RetryDecision::GiveUp`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum RetryDecision {
    Retry(Duration),
    GiveUp,
}

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
/// `full_reconcile_interval`.
///
/// `changelog_consumer_ready` (regression: watermark id INT2 vs i32 decode
/// killed active-set consumer, alpha 2026-10-02): held `false` for as long
/// as the initial full active-set read keeps failing, so `/readyz` reports
/// `degraded` instead of silently staying `Ready` with no consumer -- fail
/// loud, never silent (user requirement). Retries with capped exponential
/// backoff, logging an ERROR with the rendered error per attempt; if
/// [`INITIAL_STATE_RETRY_GRACE`] elapses without success, this process exits
/// non-zero so Kubernetes restarts it visibly.
#[allow(clippy::too_many_arguments)]
pub async fn run(
    db: DatabaseConnection,
    poll_interval: std::time::Duration,
    full_reconcile_interval: std::time::Duration,
    call_timeout_ms: u64,
    flag: Arc<dyn FeatureFlag>,
    connections: Arc<crate::host_api::ConnectionRegistry>,
    excluded_metric: prometheus::IntCounterVec,
    metrics: ChangelogConsumerMetrics,
    kv_capabilities: Arc<bundle_host_kv::CapabilitySnapshot>,
    changelog_consumer_ready: Arc<std::sync::atomic::AtomicBool>,
    mut shutdown: tokio::sync::oneshot::Receiver<()>,
) {
    changelog_consumer_ready.store(false, std::sync::atomic::Ordering::Relaxed);
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
                            "changelog consumer: initial full active-set read still failing \
                             after the retry grace period; exiting so Kubernetes restarts this pod"
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
    changelog_consumer_ready.store(true, std::sync::atomic::Ordering::Relaxed);

    let mut poll_tick = tokio::time::interval(poll_interval);
    poll_tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let mut reconcile_tick = tokio::time::interval(full_reconcile_interval);
    reconcile_tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    // The startup `initial_state` read above already IS the first full
    // reconcile -- skip `reconcile_tick`'s own immediate first fire.
    reconcile_tick.tick().await;

    // See `detect_new_connection`'s own doc (item 4, gh security review on
    // PR #406). `None`: no connection observed yet.
    let mut last_connection_id: Option<usize> = None;

    // `initial_state` already set `state.pending_full_sync` to
    // `FullSyncReason::Startup` -- see `core/svc_process/src/
    // changelog_consumer.rs`'s identical prelude for the full rationale.
    {
        let active_connection = connections.active();
        let sink = active_connection.map(|connection| crate::bundle_loader::ExecutorSink {
            connection,
            call_timeout_ms,
        });
        drain_pending_full_sync(
            &db,
            &mut state,
            sink.as_ref().map(|s| s as &dyn BundleSink),
            &excluded_metric,
            &metrics,
            &kv_capabilities,
            Instant::now(),
            FULL_SEND_DEBOUNCE,
        )
        .await;
    }

    loop {
        let active_connection = connections.active();
        if detect_new_connection(active_connection.as_ref(), &mut last_connection_id) {
            tracing::info!(
                "changelog consumer: detected a new executor connection; resetting loaded-state \
                 so the full authoritative active set is resent (the executor wipes its own \
                 registry on every disconnect)"
            );
            state.loaded.clear();
            state.pending_full_sync = Some(FullSyncReason::Reconnect);
            metrics.executor_reconnect_detected_total.inc();
        }
        let sink = active_connection.map(|connection| crate::bundle_loader::ExecutorSink {
            connection,
            call_timeout_ms,
        });
        let sink_ref = sink.as_ref().map(|s| s as &dyn BundleSink);

        // "Don't wait for the watermark or the full-reconcile timer" --
        // regression: loads waited for 15-min full reconcile after
        // startup/reconnect, UnknownBundle (alpha 2026-10-03).
        drain_pending_full_sync(
            &db,
            &mut state,
            sink_ref,
            &excluded_metric,
            &metrics,
            &kv_capabilities,
            Instant::now(),
            FULL_SEND_DEBOUNCE,
        )
        .await;

        tokio::select! {
            _ = &mut shutdown => return,
            _ = poll_tick.tick() => {
                if !flag.enabled().await {
                    tracing::debug!(
                        "multi-tenant changelog consumer disabled (kill-switch on); skipping tick"
                    );
                    continue;
                }
                run_incremental_tick(&db, &mut state, sink_ref, &excluded_metric, &metrics, &kv_capabilities).await;
            }
            _ = reconcile_tick.tick() => {
                if !flag.enabled().await {
                    continue;
                }
                run_full_reconcile(
                    &db, &mut state, sink_ref, &excluded_metric, &metrics, &kv_capabilities,
                    FullSyncReason::Reconcile,
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
            summary_json: sea_orm::JsonValue::Null,
        }
    }

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

    /// regression: watermark id INT2 vs i32 decode killed active-set consumer (alpha 2026-10-02)
    #[test]
    fn decide_retry_retries_with_capped_backoff_before_the_grace_period_elapses() {
        let grace = Duration::from_secs(120);
        let backoff_max = Duration::from_secs(30);
        assert_eq!(
            decide_retry(1, Duration::from_secs(0), grace, backoff_max),
            RetryDecision::Retry(Duration::from_secs(1))
        );
        assert_eq!(
            decide_retry(10, Duration::from_secs(100), grace, backoff_max),
            RetryDecision::Retry(backoff_max),
            "backoff is capped at backoff_max, never grows unbounded"
        );
    }

    /// regression: watermark id INT2 vs i32 decode killed active-set consumer (alpha 2026-10-02)
    #[test]
    fn decide_retry_gives_up_once_the_grace_period_has_elapsed() {
        let grace = Duration::from_secs(120);
        let backoff_max = Duration::from_secs(30);
        assert_eq!(
            decide_retry(50, grace, grace, backoff_max),
            RetryDecision::GiveUp,
            "exactly at the grace boundary must give up, not retry one more time"
        );
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
                dyn std::future::Future<Output = Result<(), crate::dispatch::InvokeError>>
                    + Send
                    + 'a,
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
                dyn std::future::Future<Output = Result<(), crate::dispatch::InvokeError>>
                    + Send
                    + 'a,
            >,
        > {
            let call = format!("unload:{app_id}:{digest}");
            Box::pin(async move {
                self.calls.lock().unwrap().push(call);
                Ok(())
            })
        }
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
            .into_connection();

        let state = initial_state(&db).await?;
        assert_eq!(state.last_seq(), 500);
        assert_eq!(state.by_scope.len(), 2);
        assert!(state.retention_supported);
        Ok(())
    }

    /// Item 5 (Gemini re-review of PR #406): see
    /// `core/svc_process/src/changelog_consumer.rs`'s identical test.
    #[tokio::test]
    async fn initial_state_starts_successfully_when_min_retained_seq_column_is_absent(
    ) -> Result<(), bundle_active_set::ActiveSetError> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([
                Vec::<std::collections::BTreeMap<String, sea_orm::Value>>::new(),
            ])
            .append_query_results([vec![std::collections::BTreeMap::from([(
                "safe_seq".to_string(),
                sea_orm::Value::BigInt(Some(500)),
            )])]])
            .append_query_results([
                Vec::<bundle_active_set::entities::app_active_versions::Model>::new(),
            ])
            .into_connection();

        let state = initial_state(&db).await?;
        assert!(!state.retention_supported);
        assert_eq!(state.last_seq(), 500);
        Ok(())
    }

    /// See `core/svc_process/src/changelog_consumer.rs`'s identical test.
    #[tokio::test]
    async fn run_incremental_tick_relies_on_the_heuristic_fallback_when_retention_unsupported() {
        let digest = format!("sha256:{}", "9".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![std::collections::BTreeMap::from([(
                "safe_seq".to_string(),
                sea_orm::Value::BigInt(Some(150)),
            )])]])
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
            &test_excluded_metric(),
            &metrics,
            &bundle_host_kv::CapabilitySnapshot::new(),
        )
        .await;

        assert_eq!(state.last_seq(), 150);
        assert_eq!(metrics.changelog_gap_detected_total.get(), 1);
        assert_eq!(metrics.changelog_retention_exceeded_total.get(), 0);
    }

    #[tokio::test]
    async fn run_incremental_tick_does_nothing_when_safe_seq_has_not_advanced() {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![watermark_row(100)]])
            .into_connection();
        let mut state = ConsumerState::new(100);
        let sink = FakeSink::default();
        run_incremental_tick(
            &db,
            &mut state,
            Some(&sink as &dyn BundleSink),
            &test_excluded_metric(),
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
            .into_connection();
        let mut state = ConsumerState::new(100);
        let sink = FakeSink::default();
        run_incremental_tick(
            &db,
            &mut state,
            Some(&sink as &dyn BundleSink),
            &test_excluded_metric(),
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
            .into_connection();
        let mut state = ConsumerState::new(100);
        let sink = FakeSink::default();
        let metrics = test_changelog_metrics();
        run_incremental_tick(
            &db,
            &mut state,
            Some(&sink as &dyn BundleSink),
            &test_excluded_metric(),
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

    #[tokio::test]
    async fn run_incremental_tick_never_reads_past_safe_seq() {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![watermark_row(120)]])
            .append_query_results([vec![change_row(101, 1, 0)]])
            .append_query_results([
                Vec::<bundle_active_set::entities::app_active_versions::Model>::new(),
            ])
            .into_connection();
        let mut state = ConsumerState::new(100);
        let sink = FakeSink::default();
        run_incremental_tick(
            &db,
            &mut state,
            Some(&sink as &dyn BundleSink),
            &test_excluded_metric(),
            &test_changelog_metrics(),
            &bundle_host_kv::CapabilitySnapshot::new(),
        )
        .await;
        assert_eq!(state.last_seq(), 120);
    }

    /// Partial-advance regression, updated for the fix (Gemini review on PR
    /// #396, HIGH): the only change's scope failed, so nothing is safe to
    /// advance past yet -- `last_seq` must stay put, not jump to `safe_seq`
    /// (which would permanently skip this change).
    #[tokio::test]
    async fn run_incremental_tick_does_not_advance_past_a_failed_scopes_own_seq() {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![watermark_row(150)]])
            .append_query_results([vec![change_row(101, 1, 0)]])
            .into_connection();
        let mut state = ConsumerState::new(100);
        let sink = FakeSink::default();
        run_incremental_tick(
            &db,
            &mut state,
            Some(&sink as &dyn BundleSink),
            &test_excluded_metric(),
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

    /// The counterpart regression: two scopes change, one succeeds and one
    /// fails -- the tracker advances only up to the failed scope's own
    /// affecting seq, and the succeeding scope's load still applies.
    #[tokio::test]
    async fn run_incremental_tick_advances_only_up_to_the_scope_before_a_failure() {
        let digest = format!("sha256:{}", "5".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![watermark_row(150)]])
            .append_query_results([vec![change_row(101, 1, 0), change_row(102, 2, 0)]])
            .append_query_results([vec![active_row("waddles.a", 1, 0, 10)]])
            .append_query_results([vec![version_row(10, "waddles.a", &digest)]])
            .append_query_results([vec![approval_row(1, "waddles.a")]])
            .into_connection();
        let mut state = ConsumerState::new(100);
        let sink = FakeSink::default();
        run_incremental_tick(
            &db,
            &mut state,
            Some(&sink as &dyn BundleSink),
            &test_excluded_metric(),
            &test_changelog_metrics(),
            &bundle_host_kv::CapabilitySnapshot::new(),
        )
        .await;
        assert_eq!(state.last_seq(), 101);
        assert_eq!(
            state.loaded().get(&(1, 0, "waddles.a".to_string())),
            Some(&digest)
        );
    }

    /// Stale-eviction regression (Gemini review, LOW): see
    /// `core/svc_process/src/changelog_consumer.rs`'s identical test.
    #[tokio::test]
    async fn run_incremental_tick_evicts_a_scope_stale_beyond_the_bound() {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![watermark_row(150)]])
            .append_query_results([vec![change_row(101, 1, 0)]])
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
        tokio::time::sleep(Duration::from_millis(60)).await;

        let sink = FakeSink::default();
        run_incremental_tick(
            &db,
            &mut state,
            Some(&sink as &dyn BundleSink),
            &test_excluded_metric(),
            &test_changelog_metrics(),
            &bundle_host_kv::CapabilitySnapshot::new(),
        )
        .await;

        assert_eq!(state.by_scope_len(), 0);
        assert_eq!(sink.calls(), vec!["unload:waddles.a:sha256:00".to_string()]);
    }

    /// Primary retention regression (hub-api migration `0026`/PR #397):
    /// see `core/svc_process/src/changelog_consumer.rs`'s identical test.
    #[tokio::test]
    async fn run_incremental_tick_forces_a_full_reconcile_when_behind_min_retained_seq() {
        let digest = format!("sha256:{}", "8".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![watermark_row_with_retention(200, 150)]])
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
            &test_excluded_metric(),
            &metrics,
            &bundle_host_kv::CapabilitySnapshot::new(),
        )
        .await;

        assert_eq!(state.last_seq(), 200);
        assert_eq!(metrics.changelog_retention_exceeded_total.get(), 1);
        assert_eq!(metrics.changelog_gap_detected_total.get(), 0);
        assert_eq!(sink.calls(), vec![format!("load:waddles.a:{digest}")]);
    }

    /// Retention-gap regression (Gemini review on PR #397): see
    /// `core/svc_process/src/changelog_consumer.rs`'s identical test.
    #[tokio::test]
    async fn run_incremental_tick_forces_a_full_reconcile_on_a_detected_changelog_gap() {
        let digest = format!("sha256:{}", "7".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![watermark_row(150)]])
            .append_query_results([vec![change_row(120, 1, 0)]])
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
            &test_excluded_metric(),
            &metrics,
            &bundle_host_kv::CapabilitySnapshot::new(),
        )
        .await;

        assert_eq!(state.last_seq(), 150);
        assert_eq!(metrics.changelog_gap_detected_total.get(), 1);
        assert_eq!(sink.calls(), vec![format!("load:waddles.a:{digest}")]);
    }

    #[tokio::test]
    async fn run_full_reconcile_replaces_the_whole_by_scope_map() {
        let digest = format!("sha256:{}", "c".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![active_row("waddles.a", 1, 0, 10)]])
            .append_query_results([vec![version_row(10, "waddles.a", &digest)]])
            .append_query_results([vec![approval_row(1, "waddles.a")]])
            .into_connection();
        let mut state = ConsumerState::new(0);
        let sink = FakeSink::default();
        run_full_reconcile(
            &db,
            &mut state,
            Some(&sink as &dyn BundleSink),
            &test_excluded_metric(),
            &test_changelog_metrics(),
            &bundle_host_kv::CapabilitySnapshot::new(),
            FullSyncReason::Reconcile,
        )
        .await;
        assert_eq!(
            state.loaded().get(&(1, 0, "waddles.a".to_string())),
            Some(&digest)
        );
    }

    #[test]
    fn should_stop_consumers_only_on_the_enabled_to_disabled_transition() {
        assert!(should_stop_consumers(false, true));
        assert!(!should_stop_consumers(false, false));
        assert!(!should_stop_consumers(true, true));
        assert!(!should_stop_consumers(true, false));
    }

    /// See `core/svc_process/src/changelog_consumer.rs`'s identical test.
    #[test]
    fn detect_new_connection_fires_only_on_a_genuine_identity_change() {
        let mut last = None;
        let a = Arc::new(());
        let b = Arc::new(());

        assert!(detect_new_connection(Some(&a), &mut last));
        assert!(!detect_new_connection(Some(&a), &mut last));
        assert!(detect_new_connection(Some(&b), &mut last));
        assert!(!detect_new_connection(Some(&b), &mut last));
        assert!(!detect_new_connection(Option::<&Arc<()>>::None, &mut last));
        assert!(detect_new_connection(Some(&a), &mut last));
    }

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

    /// The live `run()` loop, end to end against a `MockDatabase`: a
    /// never-previously-exercised function. `initial_state` loads an empty
    /// active set (no scopes at all -- the DB mock has nothing else queued,
    /// so any further query would panic on an empty queue), the kill-switch
    /// flag is permanently OFF so both the `poll_tick`/`reconcile_tick`
    /// branches take their own `continue` arm without ever touching
    /// `run_incremental_tick`/`run_full_reconcile`, and a short shutdown
    /// delay exercises the `_ = &mut shutdown => return` arm -- proving the
    /// loop actually ticks (both intervals are 1ms, far shorter than the
    /// shutdown delay) and still returns promptly instead of hanging.
    #[tokio::test]
    async fn run_ticks_and_returns_promptly_on_shutdown() {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([
                Vec::<std::collections::BTreeMap<String, sea_orm::Value>>::new(),
            ])
            .append_query_results([vec![std::collections::BTreeMap::from([(
                "safe_seq".to_string(),
                sea_orm::Value::BigInt(Some(0)),
            )])]])
            .append_query_results([
                Vec::<bundle_active_set::entities::app_active_versions::Model>::new(),
            ])
            .into_connection();

        let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel();
        tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(50)).await;
            let _ = shutdown_tx.send(());
        });

        let flag: Arc<dyn FeatureFlag> = Arc::new(crate::flags::StaticFlag(false));
        let excluded_metric = test_excluded_metric();
        let metrics = test_changelog_metrics();
        let changelog_consumer_ready = Arc::new(std::sync::atomic::AtomicBool::new(false));

        let result = tokio::time::timeout(
            Duration::from_secs(5),
            run(
                db,
                Duration::from_millis(1),
                Duration::from_millis(1),
                2000,
                flag,
                Arc::new(crate::host_api::ConnectionRegistry::new()),
                excluded_metric,
                metrics,
                Arc::new(bundle_host_kv::CapabilitySnapshot::new()),
                Arc::clone(&changelog_consumer_ready),
                shutdown_rx,
            ),
        )
        .await;
        assert!(
            result.is_ok(),
            "run() must return promptly once shutdown resolves, not hang"
        );
        assert!(
            changelog_consumer_ready.load(std::sync::atomic::Ordering::Relaxed),
            "regression: watermark id INT2 vs i32 decode killed active-set consumer (alpha 2026-10-02) -- \
             readiness must flip true once the initial active-set read succeeds"
        );
    }

    // --- Immediate full-sync regression (alpha 2026-10-03) -------------
    // regression: loads waited for 15-min full reconcile after
    // startup/reconnect, UnknownBundle (alpha 2026-10-03)
    // See `core/svc_process/src/changelog_consumer.rs`'s identical test
    // suite for the full rationale behind each test below.

    #[derive(Default)]
    struct FailingSink;
    impl BundleSink for FailingSink {
        fn load<'a>(
            &'a self,
            _tenant_id: i32,
            _community_id: i32,
            _row: &'a bundle_active_set::ActiveBundleRow,
        ) -> std::pin::Pin<
            Box<
                dyn std::future::Future<Output = Result<(), crate::dispatch::InvokeError>>
                    + Send
                    + 'a,
            >,
        > {
            Box::pin(async move { Err(crate::dispatch::InvokeError::NoExecutor) })
        }
        fn unload<'a>(
            &'a self,
            _tenant_id: i32,
            _community_id: i32,
            _app_id: &'a str,
            _digest: &'a str,
        ) -> std::pin::Pin<
            Box<
                dyn std::future::Future<Output = Result<(), crate::dispatch::InvokeError>>
                    + Send
                    + 'a,
            >,
        > {
            Box::pin(async move { Err(crate::dispatch::InvokeError::NoExecutor) })
        }
    }

    fn by_scope_with_one_active_app(digest: &str) -> HashMap<ScopeKey, ActiveSetRead> {
        let mut by_scope = HashMap::new();
        by_scope.insert(
            (1, 0),
            ActiveSetRead {
                rows: vec![bundle_active_set::ActiveBundleRow {
                    app_id: "waddles.a".to_string(),
                    version: "1".to_string(),
                    digest: digest.to_string(),
                    component_key: "k".to_string(),
                    sidecar_key: "s".to_string(),
                    declared_capabilities: Vec::new(),
                }],
                excluded: Vec::new(),
                degraded: Vec::new(),
            },
        );
        by_scope
    }

    #[tokio::test]
    async fn initial_state_seeds_a_pending_startup_full_sync() {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![retention_probe_row_supported()]])
            .append_query_results([vec![watermark_row(0)]])
            .append_query_results([
                Vec::<bundle_active_set::entities::app_active_versions::Model>::new(),
            ])
            .into_connection();
        let state = initial_state(&db)
            .await
            .expect("initial_state must succeed");
        assert_eq!(
            state.pending_full_sync(),
            Some(FullSyncReason::Startup),
            "a freshly-started consumer must owe an immediate full send"
        );
    }

    #[tokio::test]
    async fn drain_pending_full_sync_sends_immediately_on_startup() {
        let digest = format!("sha256:{}", "1".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![active_row("waddles.a", 1, 0, 10)]])
            .append_query_results([vec![version_row(10, "waddles.a", &digest)]])
            .append_query_results([vec![approval_row(1, "waddles.a")]])
            .into_connection();
        let mut state = ConsumerState::new(0).with_pending_full_sync(FullSyncReason::Startup);
        let sink = FakeSink::default();
        let metrics = test_changelog_metrics();
        drain_pending_full_sync(
            &db,
            &mut state,
            Some(&sink as &dyn BundleSink),
            &test_excluded_metric(),
            &metrics,
            &bundle_host_kv::CapabilitySnapshot::new(),
            Instant::now(),
            Duration::from_millis(20),
        )
        .await;
        assert_eq!(sink.calls(), vec![format!("load:waddles.a:{digest}")]);
        assert_eq!(state.pending_full_sync(), None);
        assert_eq!(
            metrics
                .bundle_full_sync_total
                .with_label_values(&["startup"])
                .get(),
            1
        );
    }

    #[tokio::test]
    async fn drain_pending_full_sync_leaves_the_flag_pending_with_no_sink_yet() {
        let db = MockDatabase::new(DatabaseBackend::Postgres).into_connection();
        let mut state = ConsumerState::new(0).with_pending_full_sync(FullSyncReason::Startup);
        drain_pending_full_sync(
            &db,
            &mut state,
            None,
            &test_excluded_metric(),
            &test_changelog_metrics(),
            &bundle_host_kv::CapabilitySnapshot::new(),
            Instant::now(),
            Duration::from_millis(20),
        )
        .await;
        assert_eq!(
            state.pending_full_sync(),
            Some(FullSyncReason::Startup),
            "no connection yet -- must retry on a later iteration, never drop silently"
        );
    }

    #[tokio::test]
    async fn drain_pending_full_sync_coalesces_a_reconnect_storm_then_sends_again_after_debounce() {
        let digest_a = format!("sha256:{}", "2".repeat(64));
        let digest_b = format!("sha256:{}", "3".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![active_row("waddles.a", 1, 0, 10)]])
            .append_query_results([vec![version_row(10, "waddles.a", &digest_a)]])
            .append_query_results([vec![approval_row(1, "waddles.a")]])
            .append_query_results([vec![active_row("waddles.b", 1, 0, 11)]])
            .append_query_results([vec![version_row(11, "waddles.b", &digest_b)]])
            .append_query_results([vec![approval_row(1, "waddles.b")]])
            .into_connection();
        let mut state = ConsumerState::new(0).with_pending_full_sync(FullSyncReason::Reconnect);
        let sink = FakeSink::default();
        let metrics = test_changelog_metrics();
        let debounce = Duration::from_millis(20);
        let t0 = Instant::now();

        drain_pending_full_sync(
            &db,
            &mut state,
            Some(&sink as &dyn BundleSink),
            &test_excluded_metric(),
            &metrics,
            &bundle_host_kv::CapabilitySnapshot::new(),
            t0,
            debounce,
        )
        .await;
        assert_eq!(sink.calls(), vec![format!("load:waddles.a:{digest_a}")]);

        state.pending_full_sync = Some(FullSyncReason::Reconnect);
        drain_pending_full_sync(
            &db,
            &mut state,
            Some(&sink as &dyn BundleSink),
            &test_excluded_metric(),
            &metrics,
            &bundle_host_kv::CapabilitySnapshot::new(),
            t0 + Duration::from_millis(5),
            debounce,
        )
        .await;
        assert_eq!(
            sink.calls(),
            vec![format!("load:waddles.a:{digest_a}")],
            "a reconnect within the debounce window must be coalesced, not re-sent"
        );
        assert_eq!(state.pending_full_sync(), Some(FullSyncReason::Reconnect));

        drain_pending_full_sync(
            &db,
            &mut state,
            Some(&sink as &dyn BundleSink),
            &test_excluded_metric(),
            &metrics,
            &bundle_host_kv::CapabilitySnapshot::new(),
            t0 + Duration::from_millis(25),
            debounce,
        )
        .await;
        assert_eq!(
            sink.calls(),
            vec![
                format!("load:waddles.a:{digest_a}"),
                format!("load:waddles.b:{digest_b}"),
                format!("unload:waddles.a:{digest_a}"),
            ],
            "once the debounce window elapses the coalesced reconnect must still be honored"
        );
        assert_eq!(state.pending_full_sync(), None);
        assert_eq!(
            metrics
                .bundle_full_sync_total
                .with_label_values(&["reconnect"])
                .get(),
            2,
            "exactly two full sends must have happened, never three"
        );
    }

    #[tokio::test]
    async fn run_incremental_tick_sends_full_sync_when_loaded_state_diverged_but_watermark_unchanged(
    ) {
        let digest = format!("sha256:{}", "4".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![watermark_row(100)]])
            .into_connection();
        let mut state = ConsumerState::new(100);
        state.by_scope = by_scope_with_one_active_app(&digest);
        let sink = FakeSink::default();
        let metrics = test_changelog_metrics();
        run_incremental_tick(
            &db,
            &mut state,
            Some(&sink as &dyn BundleSink),
            &test_excluded_metric(),
            &metrics,
            &bundle_host_kv::CapabilitySnapshot::new(),
        )
        .await;
        assert_eq!(state.last_seq(), 100);
        assert_eq!(sink.calls(), vec![format!("load:waddles.a:{digest}")]);
        assert_eq!(
            state.loaded().get(&(1, 0, "waddles.a".to_string())),
            Some(&digest)
        );
        assert_eq!(
            metrics
                .bundle_full_sync_total
                .with_label_values(&["diverged"])
                .get(),
            1
        );
    }

    #[tokio::test]
    async fn run_incremental_tick_retries_a_failed_load_on_the_next_tick() {
        let digest = format!("sha256:{}", "6".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![watermark_row(100)]])
            .append_query_results([vec![watermark_row(100)]])
            .into_connection();
        let mut state = ConsumerState::new(100);
        state.by_scope = by_scope_with_one_active_app(&digest);
        let metrics = test_changelog_metrics();

        let failing_sink = FailingSink;
        run_incremental_tick(
            &db,
            &mut state,
            Some(&failing_sink as &dyn BundleSink),
            &test_excluded_metric(),
            &metrics,
            &bundle_host_kv::CapabilitySnapshot::new(),
        )
        .await;
        assert!(state.loaded().is_empty());
        assert_eq!(
            metrics
                .bundle_loads_total
                .with_label_values(&["failure"])
                .get(),
            1
        );

        let sink = FakeSink::default();
        run_incremental_tick(
            &db,
            &mut state,
            Some(&sink as &dyn BundleSink),
            &test_excluded_metric(),
            &metrics,
            &bundle_host_kv::CapabilitySnapshot::new(),
        )
        .await;
        assert_eq!(sink.calls(), vec![format!("load:waddles.a:{digest}")]);
        assert_eq!(
            metrics
                .bundle_loads_total
                .with_label_values(&["success"])
                .get(),
            1
        );
    }
}
