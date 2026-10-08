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

use bundle_active_set::{
    ActiveSetRead, AppScope, ChangeLogTracker, FullSyncReason, ResolvedScope, ScopeKey,
};
use sea_orm::DatabaseConnection;

use crate::active_digests::{ActiveDigests, LoadedSessions};
use crate::bundle_loader::SessionBundleSink;
use crate::dispatch_supervisor::{self, ConsumerSupervisor, DispatchTarget, RunningConsumers};
use crate::flags::FeatureFlag;
use crate::telemetry::{ChangelogConsumerMetrics, DispatchSupervisorMetrics};

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

/// Upper bound this consumer waits for a single `Load`/`Unload` wire round
/// trip before treating it as failed. `host_api::Connection::request`
/// (unlike `Connection::ping`) has no timeout of its own -- it just `.await`s
/// the reply oneshot forever. Generous enough for a from-scratch CPython
/// bundle compile (`LoadLimits::timeout_ms` is a *different* number: it
/// tells the EXECUTOR its own compile budget on the wire; this is this
/// client's own ceiling on waiting for any reply to arrive at all).
///
/// regression (alpha 2026-10-03): during a rollout the about-to-terminate
/// session's executor kept the connection open but never answered a `Load`
/// it had already been sent (draining); `apply_active_set`'s old
/// `to_load`/`to_unload` loops awaited each `(session, bundle)` call
/// *sequentially*, so that one un-answered call silently starved every
/// other entry queued behind it in the same full sync -- including the
/// live session's own loads -- and parked this single-threaded consumer's
/// entire `run()` loop (no further ticks, no further logs of ANY kind) for
/// as long as the hang lasted. Fixed by pairing this timeout with running
/// every `(session, bundle)` Load/Unload for a tick concurrently (see
/// `apply_active_set`), so one stuck call can never block another, and by
/// bounding how long "stuck" is allowed to mean "forever".
#[cfg(not(test))]
const SESSION_CALL_TIMEOUT: Duration = Duration::from_secs(120);
#[cfg(test)]
const SESSION_CALL_TIMEOUT: Duration = Duration::from_millis(40);

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

/// Whether `run`'s loop should stop dispatch consumers this tick -- true
/// only on the enabled->disabled transition (Gemini review on PR #396,
/// HIGH), never on every already-disabled tick. Now exercised for a real
/// side effect by `run`'s own kill-switch branch (`crate::
/// dispatch_supervisor::stop_all`) -- regression: svc-action had no
/// multi-tenant dispatch consumers; replies never sent after legacy env
/// removal (alpha 2026-10-03).
fn should_stop_consumers(currently_enabled: bool, were_enabled: bool) -> bool {
    !currently_enabled && were_enabled
}

/// What changed in the live host-API session set between two ticks --
/// `added` (sessions this loop has never synced) and `removed` (sessions
/// that silently dropped out, e.g. the host-API registry pruning a closed
/// connection). Direct port of `core/svc_process/src/changelog_consumer.rs`'s
/// identical type/function -- **replaces the retired `detect_new_connection`'s
/// single pointer-identity slot** (item 4, gh security review on PR #406):
/// that design could only ever track ONE connection at a time, so during a
/// rollout -- when the OLD pod's executor session can briefly outlive the
/// NEW pod's -- it saw only "the connection changed" and reset ALL
/// loaded-state globally, with no way to react when the OLD session
/// specifically disappeared moments later (regression: bundles loaded only
/// onto a terminating executor during rollout; live executor got none,
/// alpha 2026-10-03). A set diff naturally reports BOTH sessions as distinct
/// `added` events and, independently, the old one's later `removed` event.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct SessionDiff {
    pub added: Vec<bundle_active_set::SessionId>,
    pub removed: Vec<bundle_active_set::SessionId>,
}

/// Diffs `known` (every session this loop has already accounted for) against
/// `live` (the host-API `ConnectionRegistry`'s current live set) -- pure (no
/// I/O), so directly unit-testable.
pub(crate) fn diff_sessions(
    known: &std::collections::HashSet<bundle_active_set::SessionId>,
    live: &std::collections::HashSet<bundle_active_set::SessionId>,
) -> SessionDiff {
    SessionDiff {
        added: live.difference(known).copied().collect(),
        removed: known.difference(live).copied().collect(),
    }
}

/// All state one running consumer instance carries across ticks --
/// constructed once by [`initial_state`], mutated in place by every
/// subsequent [`run_incremental_tick`]/[`run_full_reconcile`] call.
pub struct ConsumerState {
    by_scope: HashMap<ScopeKey, ActiveSetRead>,
    /// Per-SESSION record of what this consumer has already told each live
    /// executor session to load, keyed by `AppScope` (`(tenant_id,
    /// community_id, app_id)`) within each session -- **never collapsed onto
    /// `app_id` alone** (see this module's own doc), and **never a single
    /// flat map shared across sessions** (regression: bundles loaded only
    /// onto a terminating executor during rollout; live executor got none,
    /// alpha 2026-10-03): a rolling pod's old executor session can briefly
    /// outrank the new, live one, and a flat "whichever one is active" view
    /// believes a bundle is loaded somewhere even after the session that
    /// actually received it has died. `Arc`-shared (like `active_digests`
    /// below) so `crate::dispatch::handle_delivered` can pick a live session
    /// that actually has the target digest loaded, never just whichever
    /// connection `ConnectionRegistry::active()` calls "newest". See
    /// `bundle_active_set::session_sync`'s own module doc.
    loaded: Arc<LoadedSessions>,
    /// Cached tenant slug/community name per `(tenant_id, community_id)`
    /// scope -- `crate::dispatch_supervisor::DispatchTarget`'s own stream
    /// key needs the resolved slug/name, never the raw numeric ids. See
    /// `resolve_scope_cached`'s own doc for the fail-closed-per-scope
    /// contract.
    resolved_scopes: HashMap<ScopeKey, ResolvedScope>,
    /// The multi-tenant per-app dispatch consumers this instance currently
    /// has running -- `crate::dispatch_supervisor::reconcile`'s own
    /// bookkeeping, held across ticks exactly like `loaded` above.
    /// regression: svc-action had no multi-tenant dispatch consumers;
    /// replies never sent after legacy env removal (alpha 2026-10-03).
    running_consumers: RunningConsumers,
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
    /// The shared, concurrently-readable `(tenant_id, community_id,
    /// app_id)` -> digest map every spawned `dispatch_supervisor::
    /// run_app_consumer` task reads from (`dispatch::DigestSource::
    /// Active`) -- kept in lock-step with `loaded` at the exact same
    /// `apply_active_set` call sites. Defaults to a fresh, empty, private
    /// instance (`ConsumerState::new`/`initial_state`); [`run`] immediately
    /// overwrites it with the externally shared instance `crate::lib` also
    /// threads into `dispatch_supervisor::SupervisorDeps`, before this
    /// state is ever applied against. regression: svc-action had no
    /// multi-tenant dispatch consumers; replies never sent after legacy env
    /// removal (alpha 2026-10-03).
    active_digests: Arc<ActiveDigests>,
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
            loaded: Arc::new(LoadedSessions::new()),
            resolved_scopes: HashMap::new(),
            running_consumers: HashMap::new(),
            tracker: ChangeLogTracker::new(initial_seq),
            scope_last_success: HashMap::new(),
            retention_supported: true,
            // Deliberately `None` -- see `core/svc_process/src/
            // changelog_consumer.rs`'s identical ctor for why.
            pending_full_sync: None,
            last_full_send: None,
            active_digests: Arc::new(ActiveDigests::new()),
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
    fn loaded(&self) -> bundle_active_set::SessionLoaded<AppScope> {
        self.loaded.snapshot()
    }

    #[cfg(test)]
    fn last_seq(&self) -> i64 {
        self.tracker.last_seq()
    }

    #[cfg(test)]
    fn by_scope_len(&self) -> usize {
        self.by_scope.len()
    }

    #[cfg(test)]
    fn running_len(&self) -> usize {
        self.running_consumers.len()
    }

    #[cfg(test)]
    fn active_digests(&self) -> &ActiveDigests {
        &self.active_digests
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
        loaded: Arc::new(LoadedSessions::new()),
        resolved_scopes: HashMap::new(),
        running_consumers: HashMap::new(),
        tracker: ChangeLogTracker::new(safe_seq),
        scope_last_success,
        retention_supported,
        pending_full_sync: Some(FullSyncReason::Startup),
        last_full_send: None,
        active_digests: Arc::new(ActiveDigests::new()),
    })
}

/// Resolves and caches the tenant slug/community name for `scope`, reusing
/// an already-cached entry when present. A resolution failure (missing row,
/// cross-tenant community id, or a query error) is reported to the caller
/// as `None` -- **fail-closed, per-scope**: that scope's dispatch target is
/// simply left out of this tick's `dispatch_supervisor::reconcile` target
/// list (never a hardcoded/guessed scope, never an aborted tick). Direct
/// port of `core/svc_process/src/changelog_consumer.rs`'s identical
/// function.
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
                     row, or cross-tenant community id); skipping this scope's dispatch target \
                     this tick"
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
                     dispatch target this tick"
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

/// Builds the flat [`DispatchTarget`] list `dispatch_supervisor::reconcile`
/// consumes: one target per currently-active `(tenant_id, community_id,
/// app_id)` scope (`bundle_active_set::scoped_active_rows`'s own keys) that
/// also has a resolved tenant slug/community name cached. A scope with no
/// resolved slug/name (resolution failed or was never attempted) or that is
/// no longer active at all contributes NO target, which `dispatch_
/// supervisor::reconcile` correctly reads as "stop its running consumer"
/// (fail-closed) -- unlike `core/svc_process`'s `app_source_bindings`-driven
/// equivalent, this stage has no separate bindings table: the target set IS
/// the active bundle set itself.
fn build_dispatch_targets(
    active: &HashMap<AppScope, bundle_active_set::ActiveBundleRow>,
    resolved_scopes: &HashMap<ScopeKey, ResolvedScope>,
) -> Vec<DispatchTarget> {
    let mut targets = Vec::new();
    for (tenant_id, community_id, app_id) in active.keys() {
        let scope_key: ScopeKey = (*tenant_id, *community_id);
        let Some(resolved) = resolved_scopes.get(&scope_key) else {
            continue;
        };
        targets.push(DispatchTarget {
            tenant_id: *tenant_id,
            community_id: *community_id,
            tenant_slug: resolved.tenant_slug.clone(),
            community_name: resolved.community_name.clone(),
            app_id: app_id.clone(),
        });
    }
    targets
}

/// Flattens `state.by_scope` scope-preservingly (`bundle_active_set::
/// scoped_active_rows` -- never collapsing two scopes' independently-active
/// digests for the same `app_id` onto one slot, the retired multi-tenant
/// correctness bug), diffs against `state.loaded` across EVERY
/// `live_sessions` entry, and drives the resulting `Load`/`Unload` calls
/// through `sink` -- shared by both [`run_incremental_tick`] and
/// [`run_full_reconcile`].
///
/// **Per-session, not "the active connection"** (regression: bundles loaded
/// only onto a terminating executor during rollout; live executor got none,
/// alpha 2026-10-03): a full sync sends `Load` for every active bundle to
/// EVERY live session that lacks it, via `bundle_active_set::plan_sessions`
/// -- never just whichever single session `ConnectionRegistry::active()`
/// would have picked.
#[allow(clippy::too_many_arguments)]
async fn apply_active_set(
    state: &mut ConsumerState,
    sink: Option<&dyn SessionBundleSink>,
    live_sessions: &[bundle_active_set::SessionId],
    excluded_metric: &prometheus::IntCounterVec,
    kv_capabilities: &bundle_host_kv::CapabilitySnapshot,
    metrics: &ChangelogConsumerMetrics,
    full_sync_reason: Option<FullSyncReason>,
    app_version_snapshot: &bundle_active_set::ActiveVersionSnapshot,
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
    // Feeds `crate::dispatch::DispatchDeps::app_version_snapshot` (bundle
    // capability-gate wiring, spec SS12 Phase 4) from the SAME DB truth
    // this consumer just applied -- never a value captured once at
    // startup. Flattened onto `app_id` alone (dropping the `(tenant,
    // community)` scope `active` itself still preserves) is safe ONLY
    // because `crate::dispatch`'s own dispatch loop is itself still
    // single-`app_id`-per-pod, hardcoded to the `global` tenant scope
    // (`crate::lib::try_start_dispatch`'s own documented M3+ milestone seam,
    // tracked gh-598) -- this does NOT
    // reintroduce the retired multi-tenant collapse bug this module's own
    // doc warns about, since that bug was about the executor load/unload
    // signaling above, which stays scope-preserving via `plan_scoped`.
    app_version_snapshot.update(&active.values().cloned().collect::<Vec<_>>());
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

    let loaded_snapshot = state.loaded.snapshot();
    // Over-log by design, per-session (user rule: never silent) -- the
    // single aggregate `loaded_count` DEBUG line in `run_incremental_tick`
    // can look "healthy" overall while one specific live session is
    // actually missing everything (alpha 2026-10-03's exact failure mode).
    for &session in live_sessions {
        let session_loaded = active
            .keys()
            .filter(|scope| {
                loaded_snapshot.digest_for(session, scope)
                    == active.get(scope).map(|row| row.digest.as_str())
            })
            .count();
        tracing::debug!(
            session,
            session_loaded,
            active_count = active.len(),
            "changelog consumer: per-session load status this tick"
        );
    }
    let plan = bundle_active_set::plan_sessions(&loaded_snapshot, &active, live_sessions, |row| {
        row.digest.as_str()
    });

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

    // Every (session, bundle) Load this tick is independent -- run them
    // CONCURRENTLY (never sequentially) so one session's stuck/slow call can
    // never starve another, and wrap each in `SESSION_CALL_TIMEOUT` so a
    // session that never answers at all (alpha 2026-10-03: mid-termination,
    // connection open, no reply) counts as failed instead of hanging this
    // whole tick forever. The `(session, scope, row)` triples are only
    // resolved against `state`/metrics AFTER every call has settled --
    // `SessionBundleSink::load` only needs `&self`/shared refs, so no
    // mutable borrow of `state` is live during the concurrent phase.
    let load_outcomes =
        futures_util::future::join_all(plan.to_load.iter().map(|(session, scope, row)| {
            let load_start = Instant::now();
            async move {
                let outcome = tokio::time::timeout(
                    SESSION_CALL_TIMEOUT,
                    sink.load(*session, scope.0, scope.1, row),
                )
                .await;
                (session, scope, row, outcome, load_start.elapsed())
            }
        }))
        .await;

    for (session, scope, row, outcome, elapsed) in load_outcomes {
        match outcome {
            Ok(Ok(())) => {
                tracing::info!(
                    session = *session,
                    tenant_id = scope.0, community_id = scope.1,
                    app_id = %row.app_id, version = %row.version,
                    digest_prefix = %bundle_active_set::digest_prefix(&row.digest),
                    duration_ms = elapsed.as_millis() as u64,
                    "changelog consumer: loaded"
                );
                metrics
                    .bundle_loads_total
                    .with_label_values(&["success"])
                    .inc();
                // Mark loaded ONLY on this confirmed `Ok` reply -- never
                // optimistically ahead of it.
                state
                    .loaded
                    .mark_loaded(*session, scope.clone(), row.digest.clone());
                // Lock-step with `state.loaded` above -- see
                // `ActiveDigests`'s own doc. regression: svc-action had no
                // multi-tenant dispatch consumers; replies never sent after
                // legacy env removal (alpha 2026-10-03).
                state.active_digests.set(scope.clone(), row.digest.clone());
            }
            Ok(Err(err)) => {
                // Per-session failure (e.g. a connection reset mid-load,
                // alpha 2026-10-03's exact failure mode) leaves this
                // specific session "not loaded" for this scope -- it is
                // retried next tick, never silently assumed loaded just
                // because another session succeeded this same tick.
                tracing::error!(
                    session = *session,
                    tenant_id = scope.0, community_id = scope.1,
                    app_id = %row.app_id, version = %row.version,
                    digest_prefix = %bundle_active_set::digest_prefix(&row.digest),
                    duration_ms = elapsed.as_millis() as u64,
                    error = %err,
                    "changelog consumer: load failed for this session, will retry next tick"
                );
                metrics
                    .bundle_loads_total
                    .with_label_values(&["failure"])
                    .inc();
            }
            Err(_timed_out) => {
                // regression: svc-action full sync silently dropped 2/3
                // loads on live executor when old session died mid-sync
                // (alpha 2026-10-03) -- a Load that never gets a reply at
                // all must time out and count as failed, never hang this
                // tick (or this consumer) forever.
                let timeout_secs = SESSION_CALL_TIMEOUT.as_secs_f64();
                tracing::error!(
                    session = *session,
                    tenant_id = scope.0, community_id = scope.1,
                    app_id = %row.app_id, version = %row.version,
                    digest_prefix = %bundle_active_set::digest_prefix(&row.digest),
                    duration_ms = elapsed.as_millis() as u64,
                    timeout_secs,
                    "changelog consumer: load timed out after {timeout_secs}s for this \
                     session with no reply, will retry next tick"
                );
                metrics
                    .bundle_loads_total
                    .with_label_values(&["timeout"])
                    .inc();
            }
        }
    }

    let unload_outcomes = futures_util::future::join_all(plan.to_unload.iter().map(
        |(session, scope, digest)| async move {
            let outcome = tokio::time::timeout(
                SESSION_CALL_TIMEOUT,
                sink.unload(*session, scope.0, scope.1, &scope.2, digest),
            )
            .await;
            (session, scope, digest, outcome)
        },
    ))
    .await;

    for (session, scope, digest, outcome) in unload_outcomes {
        match outcome {
            Ok(Ok(())) => {
                tracing::info!(
                    session = *session,
                    tenant_id = scope.0, community_id = scope.1,
                    app_id = %scope.2, digest,
                    "changelog consumer: unloaded"
                );
                state.loaded.mark_unloaded(*session, scope);
                // Only clear the canonical active digest once NO live
                // session holds this scope loaded anymore -- see
                // `ActiveDigests`'s own doc.
                if state.loaded.loaded_count(scope) == 0 {
                    state.active_digests.remove(scope);
                }
            }
            Ok(Err(err)) => {
                tracing::warn!(
                    session = *session,
                    tenant_id = scope.0, community_id = scope.1,
                    app_id = %scope.2, digest, error = %err,
                    "changelog consumer: unload failed for this session, will retry next tick"
                );
            }
            Err(_timed_out) => {
                let timeout_secs = SESSION_CALL_TIMEOUT.as_secs_f64();
                tracing::warn!(
                    session = *session,
                    tenant_id = scope.0, community_id = scope.1,
                    app_id = %scope.2, digest, timeout_secs,
                    "changelog consumer: unload timed out after {timeout_secs}s for this \
                     session with no reply, will retry next tick"
                );
            }
        }
    }

    // Fail-closed, never silent (regression: bundles loaded only onto a
    // terminating executor during rollout; live executor got none, alpha
    // 2026-10-03): an active bundle loaded on ZERO live sessions after this
    // sync is a real outage for every request targeting it -- log loudly and
    // keep retrying next tick, rather than letting it go unnoticed until a
    // user report surfaces it.
    for scope in active.keys() {
        if state.loaded.loaded_count(scope) == 0 && !live_sessions.is_empty() {
            tracing::error!(
                tenant_id = scope.0, community_id = scope.1, app_id = %scope.2,
                live_sessions = live_sessions.len(),
                "changelog consumer: bundle is active but loaded on ZERO live executor \
                 sessions; dispatch will dead-letter this scope until the next successful sync"
            );
            metrics.bundle_zero_session_total.inc();
        }
    }
    metrics
        .bundles_loaded
        .set(state.loaded.total_entries() as i64);
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
#[allow(clippy::too_many_arguments)]
pub async fn run_incremental_tick(
    db: &DatabaseConnection,
    state: &mut ConsumerState,
    sink: Option<&dyn SessionBundleSink>,
    live_sessions: &[bundle_active_set::SessionId],
    spawner: Option<&dyn ConsumerSupervisor>,
    excluded_metric: &prometheus::IntCounterVec,
    dispatch_metrics: &DispatchSupervisorMetrics,
    metrics: &ChangelogConsumerMetrics,
    kv_capabilities: &bundle_host_kv::CapabilitySnapshot,
    app_version_snapshot: &bundle_active_set::ActiveVersionSnapshot,
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
        loaded_count = state.loaded.total_entries(),
        "changelog consumer: poll tick"
    );
    if safe_seq <= state.tracker.last_seq() {
        // The watermark hasn't moved, but a reconnect (or any other
        // out-of-band event) may have cleared `state.loaded` without
        // anything in the DB actually changing -- see
        // `core/svc_process/src/changelog_consumer.rs`'s identical check
        // for the full rationale. regression: loads waited for 15-min full
        // reconcile after startup/reconnect, UnknownBundle (alpha
        // 2026-10-03). Per-session: a single out-of-sync live session is
        // enough to force a resync, even if another live session is already
        // correct.
        let loaded_snapshot = state.loaded.snapshot();
        if bundle_active_set::any_session_diverged(&state.by_scope, &loaded_snapshot, live_sessions)
        {
            apply_active_set(
                state,
                sink,
                live_sessions,
                excluded_metric,
                kv_capabilities,
                metrics,
                Some(FullSyncReason::Diverged),
                app_version_snapshot,
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
            live_sessions,
            spawner,
            excluded_metric,
            dispatch_metrics,
            metrics,
            kv_capabilities,
            FullSyncReason::Reconcile,
            app_version_snapshot,
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
                live_sessions,
                spawner,
                excluded_metric,
                dispatch_metrics,
                metrics,
                kv_capabilities,
                FullSyncReason::Reconcile,
                app_version_snapshot,
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
        if spawner.is_some() {
            let _ = resolve_scope_cached(db, &mut state.resolved_scopes, *scope, metrics).await;
        }
    }

    let new_last_seq = match min_failure_seq {
        Some(seq) => (seq - 1).max(state.tracker.last_seq()),
        None => safe_seq,
    };
    state.tracker.advance(new_last_seq);
    metrics.changelog_lag.set(state.tracker.lag(safe_seq));

    apply_active_set(
        state,
        sink,
        live_sessions,
        excluded_metric,
        kv_capabilities,
        metrics,
        None,
        app_version_snapshot,
    )
    .await;
    update_tenant_gauges(state, metrics);

    if let Some(spawner) = spawner {
        let active = bundle_active_set::scoped_active_rows(&state.by_scope);
        let targets = build_dispatch_targets(&active, &state.resolved_scopes);
        dispatch_supervisor::reconcile(
            &mut state.running_consumers,
            &targets,
            spawner,
            dispatch_metrics,
        )
        .await;
    }
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
    sink: Option<&dyn SessionBundleSink>,
    live_sessions: &[bundle_active_set::SessionId],
    spawner: Option<&dyn ConsumerSupervisor>,
    excluded_metric: &prometheus::IntCounterVec,
    dispatch_metrics: &DispatchSupervisorMetrics,
    metrics: &ChangelogConsumerMetrics,
    kv_capabilities: &bundle_host_kv::CapabilitySnapshot,
    reason: FullSyncReason,
    app_version_snapshot: &bundle_active_set::ActiveVersionSnapshot,
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

    if let Some(spawner) = spawner {
        for scope in state.by_scope.keys().copied().collect::<Vec<_>>() {
            let _ = resolve_scope_cached(db, &mut state.resolved_scopes, scope, metrics).await;
        }
        let active = bundle_active_set::scoped_active_rows(&state.by_scope);
        let targets = build_dispatch_targets(&active, &state.resolved_scopes);
        dispatch_supervisor::reconcile(
            &mut state.running_consumers,
            &targets,
            spawner,
            dispatch_metrics,
        )
        .await;
    }

    apply_active_set(
        state,
        sink,
        live_sessions,
        excluded_metric,
        kv_capabilities,
        metrics,
        Some(reason),
        app_version_snapshot,
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
    sink: Option<&dyn SessionBundleSink>,
    live_sessions: &[bundle_active_set::SessionId],
    spawner: Option<&dyn ConsumerSupervisor>,
    excluded_metric: &prometheus::IntCounterVec,
    dispatch_metrics: &DispatchSupervisorMetrics,
    metrics: &ChangelogConsumerMetrics,
    kv_capabilities: &bundle_host_kv::CapabilitySnapshot,
    now: Instant,
    debounce: Duration,
    app_version_snapshot: &bundle_active_set::ActiveVersionSnapshot,
) {
    let Some(reason) = state.pending_full_sync else {
        return;
    };
    if sink.is_none() || live_sessions.is_empty() {
        return;
    }
    if !bundle_active_set::should_send_full_sync(state.last_full_send, now, debounce) {
        return;
    }
    run_full_reconcile(
        db,
        state,
        sink,
        live_sessions,
        spawner,
        excluded_metric,
        dispatch_metrics,
        metrics,
        kv_capabilities,
        reason,
        app_version_snapshot,
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
    spawner: Option<Arc<dyn ConsumerSupervisor>>,
    excluded_metric: prometheus::IntCounterVec,
    dispatch_metrics: DispatchSupervisorMetrics,
    metrics: ChangelogConsumerMetrics,
    kv_capabilities: Arc<bundle_host_kv::CapabilitySnapshot>,
    changelog_consumer_ready: Arc<std::sync::atomic::AtomicBool>,
    // The SAME instance `crate::lib::try_start_changelog_consumer` also
    // threads into `dispatch_supervisor::SupervisorDeps` -- overwrites
    // `initial_state`'s own fresh, private default the moment `state` is
    // constructed, before the very first `apply_active_set` call.
    // regression: svc-action had no multi-tenant dispatch consumers;
    // replies never sent after legacy env removal (alpha 2026-10-03).
    active_digests: Arc<ActiveDigests>,
    // The SAME instance `crate::lib::try_start_changelog_consumer` also
    // threads into `dispatch_supervisor::SupervisorDeps`/`DigestSource::
    // Active` -- overwrites `initial_state`'s own fresh, private default,
    // exactly like `active_digests` above. regression: bundles loaded only
    // onto a terminating executor during rollout; live executor got none
    // (alpha 2026-10-03).
    loaded_sessions: Arc<LoadedSessions>,
    // Bundle-permissions-and-capability-gate wiring (spec SS12 Phase 4):
    // the SAME instance `crate::lib::try_start_changelog_consumer` hands to
    // `try_start_dispatch`/`dispatch::handle_delivered` -- kept current here
    // every tick via `apply_active_set` so grants are never resolved
    // against a stale `app_versions.id`.
    app_version_snapshot: bundle_active_set::ActiveVersionSnapshot,
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
    state.active_digests = active_digests;
    state.loaded = loaded_sessions;
    changelog_consumer_ready.store(true, std::sync::atomic::Ordering::Relaxed);
    // Seed `app_version_snapshot` from the same startup full read, before
    // the first tick -- see `apply_active_set`'s doc for why flattening
    // onto `app_id` here is safe.
    app_version_snapshot.update(
        &bundle_active_set::scoped_active_rows(&state.by_scope)
            .values()
            .cloned()
            .collect::<Vec<_>>(),
    );

    let mut poll_tick = tokio::time::interval(poll_interval);
    poll_tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let mut reconcile_tick = tokio::time::interval(full_reconcile_interval);
    reconcile_tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    // The startup `initial_state` read above already IS the first full
    // reconcile -- skip `reconcile_tick`'s own immediate first fire.
    reconcile_tick.tick().await;

    // Every session this loop has already accounted for (added or removed)
    // as of the last iteration -- the per-session replacement for the
    // retired `detect_new_connection`'s single pointer-identity slot. See
    // `diff_sessions`'s own doc for why a SET diff against the host-API
    // session registry, not a single "most recent connection" pointer, is
    // required.
    let mut known_sessions: std::collections::HashSet<bundle_active_set::SessionId>;
    // Mirrors `core/svc_process/src/changelog_consumer.rs`'s identical
    // tracking: `dispatch_supervisor::stop_all` must run exactly once on the
    // kill-switch ON->OFF transition, never on every already-disabled tick.
    // Starts `true`: `initial_state` above always performs its full read
    // regardless of the gate, so the very first disabled tick is a genuine
    // transition worth acting on.
    let mut consumers_were_enabled = true;

    // `initial_state` already set `state.pending_full_sync` to
    // `FullSyncReason::Startup` -- see `core/svc_process/src/
    // changelog_consumer.rs`'s identical prelude for the full rationale.
    // A no-op (flag stays pending) if no connection exists yet -- the loop's
    // own per-iteration call below retries it as soon as one appears.
    {
        let live_sessions = connections.live_session_ids();
        known_sessions = live_sessions.iter().copied().collect();
        let sink = (!live_sessions.is_empty()).then(|| crate::bundle_loader::RegistrySink {
            registry: Arc::clone(&connections),
            call_timeout_ms,
        });
        drain_pending_full_sync(
            &db,
            &mut state,
            sink.as_ref().map(|s| s as &dyn SessionBundleSink),
            &live_sessions,
            spawner.as_deref(),
            &excluded_metric,
            &dispatch_metrics,
            &metrics,
            &kv_capabilities,
            Instant::now(),
            FULL_SEND_DEBOUNCE,
            &app_version_snapshot,
        )
        .await;
    }

    loop {
        let live_sessions = connections.live_session_ids();
        let live_set: std::collections::HashSet<bundle_active_set::SessionId> =
            live_sessions.iter().copied().collect();
        let diff = diff_sessions(&known_sessions, &live_set);

        // A session REMOVED from the registry (closed, reconnect, or
        // termination) -- drop ITS loaded-state only, never another
        // session's (regression: bundles loaded only onto a terminating
        // executor during rollout; live executor got none, alpha
        // 2026-10-03: nothing used to react to a session disappearing at
        // all, so a dead session's now-meaningless entries suppressed a
        // resend onto the survivor forever).
        for session in &diff.removed {
            let dropped = state.loaded.on_session_removed(*session);
            if !dropped.is_empty() {
                tracing::warn!(
                    session = *session,
                    dropped_count = dropped.len(),
                    "changelog consumer: executor session removed; dropped its loaded-state, \
                     the next sync will resync any surviving session that still needs these bundles"
                );
            }
        }
        // A session ADDED -- forces a full sync (never only resetting state
        // and waiting for the watermark/reconcile timer, same "don't wait"
        // requirement `FullSyncReason::Reconnect` already covers). Unlike
        // the retired single-pointer design, this is additive: an existing,
        // already-synced live session is left completely alone.
        if !diff.added.is_empty() {
            tracing::info!(
                added = ?diff.added,
                "changelog consumer: new executor session(s) detected; forcing a full sync onto \
                 every live session that needs it"
            );
            state.pending_full_sync = Some(FullSyncReason::Reconnect);
            metrics.executor_reconnect_detected_total.inc();
        }
        known_sessions = live_set;

        let sink = (!live_sessions.is_empty()).then(|| crate::bundle_loader::RegistrySink {
            registry: Arc::clone(&connections),
            call_timeout_ms,
        });
        let sink_ref = sink.as_ref().map(|s| s as &dyn SessionBundleSink);

        // "Don't wait for the watermark or the full-reconcile timer" --
        // regression: loads waited for 15-min full reconcile after
        // startup/reconnect, UnknownBundle (alpha 2026-10-03).
        drain_pending_full_sync(
            &db,
            &mut state,
            sink_ref,
            &live_sessions,
            spawner.as_deref(),
            &excluded_metric,
            &dispatch_metrics,
            &metrics,
            &kv_capabilities,
            Instant::now(),
            FULL_SEND_DEBOUNCE,
            &app_version_snapshot,
        )
        .await;

        tokio::select! {
            _ = &mut shutdown => {
                dispatch_supervisor::stop_all(&mut state.running_consumers, &dispatch_metrics).await;
                return;
            }
            _ = poll_tick.tick() => {
                let enabled = flag.enabled().await;
                if should_stop_consumers(enabled, consumers_were_enabled) {
                    tracing::debug!(
                        "multi-tenant changelog consumer disabled (kill-switch on); stopping all dispatch consumers"
                    );
                    dispatch_supervisor::stop_all(&mut state.running_consumers, &dispatch_metrics).await;
                }
                consumers_were_enabled = enabled;
                if !enabled {
                    tracing::debug!(
                        "multi-tenant changelog consumer disabled (kill-switch on); skipping tick"
                    );
                    continue;
                }
                run_incremental_tick(
                    &db, &mut state, sink_ref, &live_sessions, spawner.as_deref(), &excluded_metric,
                    &dispatch_metrics, &metrics, &kv_capabilities, &app_version_snapshot,
                ).await;
            }
            _ = reconcile_tick.tick() => {
                if !flag.enabled().await {
                    continue;
                }
                run_full_reconcile(
                    &db, &mut state, sink_ref, &live_sessions, spawner.as_deref(), &excluded_metric,
                    &dispatch_metrics, &metrics, &kv_capabilities,
                    FullSyncReason::Reconcile, &app_version_snapshot,
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
            artifact_signature: None,
            artifact_signature_key_id: None,
            artifact_signed_approval_id: None,
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
    impl SessionBundleSink for FakeSink {
        fn load<'a>(
            &'a self,
            session: bundle_active_set::SessionId,
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
                    .push(format!("load:{session}:{}:{}", row.app_id, row.digest));
                Ok(())
            })
        }
        fn unload<'a>(
            &'a self,
            session: bundle_active_set::SessionId,
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
            let call = format!("unload:{session}:{app_id}:{digest}");
            Box::pin(async move {
                self.calls.lock().unwrap().push(call);
                Ok(())
            })
        }
    }

    fn test_changelog_metrics() -> ChangelogConsumerMetrics {
        crate::telemetry::register_changelog_consumer_metrics(&prometheus::Registry::new())
    }

    fn test_dispatch_supervisor_metrics() -> DispatchSupervisorMetrics {
        crate::telemetry::register_dispatch_supervisor_metrics(&prometheus::Registry::new())
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
            Some(&sink as &dyn SessionBundleSink),
            &[1],
            None,
            &test_excluded_metric(),
            &test_dispatch_supervisor_metrics(),
            &metrics,
            &bundle_host_kv::CapabilitySnapshot::new(),
            &bundle_active_set::ActiveVersionSnapshot::new(),
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
            Some(&sink as &dyn SessionBundleSink),
            &[1],
            None,
            &test_excluded_metric(),
            &test_dispatch_supervisor_metrics(),
            &test_changelog_metrics(),
            &bundle_host_kv::CapabilitySnapshot::new(),
            &bundle_active_set::ActiveVersionSnapshot::new(),
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
            Some(&sink as &dyn SessionBundleSink),
            &[1],
            None,
            &test_excluded_metric(),
            &test_dispatch_supervisor_metrics(),
            &test_changelog_metrics(),
            &bundle_host_kv::CapabilitySnapshot::new(),
            &bundle_active_set::ActiveVersionSnapshot::new(),
        )
        .await;
        assert_eq!(
            state.last_seq(),
            150,
            "must advance to safe_seq, not max(seq)"
        );
        assert_eq!(
            state
                .loaded()
                .digest_for(1, &(1, 0, "waddles.a".to_string())),
            Some(digest.as_str())
        );
        assert_eq!(sink.calls(), vec![format!("load:1:waddles.a:{digest}")]);
        // Lock-step with `state.loaded` -- regression: svc-action had no
        // multi-tenant dispatch consumers; replies never sent after legacy
        // env removal (alpha 2026-10-03).
        assert_eq!(
            state.active_digests().get(&(1, 0, "waddles.a".to_string())),
            Some(digest)
        );
    }

    /// Records every `spawn`/`stop` call it receives -- used to prove
    /// `run_incremental_tick`/`run_full_reconcile` actually wire
    /// `dispatch_supervisor::reconcile` against the current active set,
    /// without any live Valkey/host-API/DB dependency for the spawned
    /// consumer task itself. regression: svc-action had no multi-tenant
    /// dispatch consumers; replies never sent after legacy env removal
    /// (alpha 2026-10-03).
    #[derive(Default)]
    struct RecordingDispatchSupervisor {
        calls: StdMutex<Vec<String>>,
    }

    impl RecordingDispatchSupervisor {
        fn calls(&self) -> Vec<String> {
            self.calls.lock().unwrap().clone()
        }
    }

    impl ConsumerSupervisor for RecordingDispatchSupervisor {
        fn spawn(&self, target: &DispatchTarget) -> dispatch_supervisor::RunningConsumer {
            self.calls.lock().unwrap().push(format!(
                "spawn:{}:{}:{}",
                target.tenant_id, target.community_id, target.app_id
            ));
            let (tx, rx) = tokio::sync::oneshot::channel();
            let handle = tokio::spawn(async move {
                let _ = rx.await;
            });
            dispatch_supervisor::RunningConsumer {
                shutdown: tx,
                handle,
            }
        }
    }

    fn tenant_row(id: i32, slug: &str) -> bundle_active_set::entities::tenants::Model {
        bundle_active_set::entities::tenants::Model {
            id,
            slug: slug.to_string(),
        }
    }

    #[tokio::test]
    async fn run_incremental_tick_reconciles_a_dispatch_consumer_for_a_newly_active_scope() {
        let digest = format!("sha256:{}", "d".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![watermark_row(150)]])
            .append_query_results([vec![change_row(101, 1, 0)]])
            .append_query_results([vec![active_row("waddles.a", 1, 0, 10)]])
            .append_query_results([vec![version_row(10, "waddles.a", &digest)]])
            .append_query_results([vec![approval_row(1, "waddles.a")]])
            .append_query_results([vec![tenant_row(1, "acme")]])
            .into_connection();
        let mut state = ConsumerState::new(100);
        let sink = FakeSink::default();
        let spawner = RecordingDispatchSupervisor::default();
        run_incremental_tick(
            &db,
            &mut state,
            Some(&sink as &dyn SessionBundleSink),
            &[1],
            Some(&spawner),
            &test_excluded_metric(),
            &test_dispatch_supervisor_metrics(),
            &test_changelog_metrics(),
            &bundle_host_kv::CapabilitySnapshot::new(),
            &bundle_active_set::ActiveVersionSnapshot::new(),
        )
        .await;

        assert_eq!(spawner.calls(), vec!["spawn:1:0:waddles.a".to_string()]);
        assert_eq!(state.running_len(), 1);
    }

    #[tokio::test]
    async fn run_incremental_tick_skips_the_dispatch_target_when_scope_resolution_fails() {
        let digest = format!("sha256:{}", "e".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![watermark_row(150)]])
            .append_query_results([vec![change_row(101, 1, 0)]])
            .append_query_results([vec![active_row("waddles.a", 1, 0, 10)]])
            .append_query_results([vec![version_row(10, "waddles.a", &digest)]])
            .append_query_results([vec![approval_row(1, "waddles.a")]])
            // No tenant row -- `resolve_scope` returns `Ok(None)`, fail-closed.
            .append_query_results([Vec::<bundle_active_set::entities::tenants::Model>::new()])
            .into_connection();
        let mut state = ConsumerState::new(100);
        let sink = FakeSink::default();
        let spawner = RecordingDispatchSupervisor::default();
        run_incremental_tick(
            &db,
            &mut state,
            Some(&sink as &dyn SessionBundleSink),
            &[1],
            Some(&spawner),
            &test_excluded_metric(),
            &test_dispatch_supervisor_metrics(),
            &test_changelog_metrics(),
            &bundle_host_kv::CapabilitySnapshot::new(),
            &bundle_active_set::ActiveVersionSnapshot::new(),
        )
        .await;

        assert!(
            spawner.calls().is_empty(),
            "fail-closed: no resolved scope, no dispatch target"
        );
        assert_eq!(state.running_len(), 0);
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
            Some(&sink as &dyn SessionBundleSink),
            &[1],
            None,
            &test_excluded_metric(),
            &test_dispatch_supervisor_metrics(),
            &metrics,
            &bundle_host_kv::CapabilitySnapshot::new(),
            &bundle_active_set::ActiveVersionSnapshot::new(),
        )
        .await;
        assert_eq!(
            state.last_seq(),
            150,
            "exactly-at-floor must still advance to safe_seq via the normal incremental path"
        );
        assert_eq!(sink.calls(), vec![format!("load:1:waddles.a:{digest}")]);
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
            Some(&sink as &dyn SessionBundleSink),
            &[1],
            None,
            &test_excluded_metric(),
            &test_dispatch_supervisor_metrics(),
            &test_changelog_metrics(),
            &bundle_host_kv::CapabilitySnapshot::new(),
            &bundle_active_set::ActiveVersionSnapshot::new(),
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
            Some(&sink as &dyn SessionBundleSink),
            &[1],
            None,
            &test_excluded_metric(),
            &test_dispatch_supervisor_metrics(),
            &test_changelog_metrics(),
            &bundle_host_kv::CapabilitySnapshot::new(),
            &bundle_active_set::ActiveVersionSnapshot::new(),
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
            Some(&sink as &dyn SessionBundleSink),
            &[1],
            None,
            &test_excluded_metric(),
            &test_dispatch_supervisor_metrics(),
            &test_changelog_metrics(),
            &bundle_host_kv::CapabilitySnapshot::new(),
            &bundle_active_set::ActiveVersionSnapshot::new(),
        )
        .await;
        assert_eq!(state.last_seq(), 101);
        assert_eq!(
            state
                .loaded()
                .digest_for(1, &(1, 0, "waddles.a".to_string())),
            Some(digest.as_str())
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
                    version_id: 0,
                    digest: "sha256:00".to_string(),
                    component_key: "k".to_string(),
                    sidecar_key: "s".to_string(),
                    artifact_signature: None,
                    artifact_signature_key_id: None,
                    artifact_signed_approval_id: None,
                    declared_capabilities: Vec::new(),
                }],
                excluded: Vec::new(),
                degraded: Vec::new(),
            },
        );
        state
            .loaded
            .mark_loaded(1, (1, 0, "waddles.a".to_string()), "sha256:00".to_string());
        state.scope_last_success.insert((1, 0), Instant::now());
        tokio::time::sleep(Duration::from_millis(60)).await;

        let sink = FakeSink::default();
        run_incremental_tick(
            &db,
            &mut state,
            Some(&sink as &dyn SessionBundleSink),
            &[1],
            None,
            &test_excluded_metric(),
            &test_dispatch_supervisor_metrics(),
            &test_changelog_metrics(),
            &bundle_host_kv::CapabilitySnapshot::new(),
            &bundle_active_set::ActiveVersionSnapshot::new(),
        )
        .await;

        assert_eq!(state.by_scope_len(), 0);
        assert_eq!(
            sink.calls(),
            vec!["unload:1:waddles.a:sha256:00".to_string()]
        );
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
            Some(&sink as &dyn SessionBundleSink),
            &[1],
            None,
            &test_excluded_metric(),
            &test_dispatch_supervisor_metrics(),
            &metrics,
            &bundle_host_kv::CapabilitySnapshot::new(),
            &bundle_active_set::ActiveVersionSnapshot::new(),
        )
        .await;

        assert_eq!(state.last_seq(), 200);
        assert_eq!(metrics.changelog_retention_exceeded_total.get(), 1);
        assert_eq!(metrics.changelog_gap_detected_total.get(), 0);
        assert_eq!(sink.calls(), vec![format!("load:1:waddles.a:{digest}")]);
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
            Some(&sink as &dyn SessionBundleSink),
            &[1],
            None,
            &test_excluded_metric(),
            &test_dispatch_supervisor_metrics(),
            &metrics,
            &bundle_host_kv::CapabilitySnapshot::new(),
            &bundle_active_set::ActiveVersionSnapshot::new(),
        )
        .await;

        assert_eq!(state.last_seq(), 150);
        assert_eq!(metrics.changelog_gap_detected_total.get(), 1);
        assert_eq!(sink.calls(), vec![format!("load:1:waddles.a:{digest}")]);
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
            Some(&sink as &dyn SessionBundleSink),
            &[1],
            None,
            &test_excluded_metric(),
            &test_dispatch_supervisor_metrics(),
            &test_changelog_metrics(),
            &bundle_host_kv::CapabilitySnapshot::new(),
            FullSyncReason::Reconcile,
            &bundle_active_set::ActiveVersionSnapshot::new(),
        )
        .await;
        assert_eq!(
            state
                .loaded()
                .digest_for(1, &(1, 0, "waddles.a".to_string())),
            Some(digest.as_str())
        );
    }

    /// **Fix requirement 1 (shared helper, `bundle_active_set::session_sync`
    /// wired end to end):** two live sessions, neither has anything loaded --
    /// a full reconcile must `Load` the one active bundle onto BOTH, never
    /// just the newest. regression: bundles loaded only onto a terminating
    /// executor during rollout; live executor got none (alpha 2026-10-03)
    #[tokio::test]
    async fn run_full_reconcile_loads_every_active_bundle_onto_every_live_session() {
        let digest = format!("sha256:{}", "7".repeat(64));
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
            Some(&sink as &dyn SessionBundleSink),
            &[1, 2],
            None,
            &test_excluded_metric(),
            &test_dispatch_supervisor_metrics(),
            &test_changelog_metrics(),
            &bundle_host_kv::CapabilitySnapshot::new(),
            FullSyncReason::Reconcile,
            &bundle_active_set::ActiveVersionSnapshot::new(),
        )
        .await;
        let mut calls = sink.calls();
        calls.sort_unstable();
        assert_eq!(
            calls,
            vec![
                format!("load:1:waddles.a:{digest}"),
                format!("load:2:waddles.a:{digest}"),
            ],
            "both live sessions must receive Load, not just the newest"
        );
        assert_eq!(
            state
                .loaded()
                .digest_for(1, &(1, 0, "waddles.a".to_string())),
            Some(digest.as_str())
        );
        assert_eq!(
            state
                .loaded()
                .digest_for(2, &(1, 0, "waddles.a".to_string())),
            Some(digest.as_str())
        );
    }

    /// A sink that fails every `load`/`unload` call targeting one specific
    /// session id -- models a session dying mid-tick (connection reset),
    /// while every other session succeeds normally.
    #[derive(Default)]
    struct DiesForSessionSink {
        calls: StdMutex<Vec<String>>,
        dies_for: bundle_active_set::SessionId,
    }
    impl DiesForSessionSink {
        fn calls(&self) -> Vec<String> {
            self.calls.lock().unwrap().clone()
        }
    }
    impl SessionBundleSink for DiesForSessionSink {
        fn load<'a>(
            &'a self,
            session: bundle_active_set::SessionId,
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
            self.calls
                .lock()
                .unwrap()
                .push(format!("load:{session}:{}:{}", row.app_id, row.digest));
            let fail = session == self.dies_for;
            Box::pin(async move {
                if fail {
                    Err(crate::dispatch::InvokeError::NoExecutor)
                } else {
                    Ok(())
                }
            })
        }
        fn unload<'a>(
            &'a self,
            _session: bundle_active_set::SessionId,
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
            Box::pin(async move { Ok(()) })
        }
    }

    /// **Regression (alpha 2026-10-03): the OLD (about-to-terminate) session
    /// dying mid-load must never cost the live session anything.** Session 2
    /// (newer, dying) fails its load; session 1 (the live one) must still
    /// end up holding the bundle after this tick.
    #[tokio::test]
    async fn run_full_reconcile_leaves_the_survivor_fully_loaded_when_the_other_session_dies_mid_load(
    ) {
        let digest = format!("sha256:{}", "6".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![active_row("waddles.a", 1, 0, 10)]])
            .append_query_results([vec![version_row(10, "waddles.a", &digest)]])
            .append_query_results([vec![approval_row(1, "waddles.a")]])
            .into_connection();
        let mut state = ConsumerState::new(0);
        let sink = DiesForSessionSink {
            dies_for: 2,
            ..Default::default()
        };
        run_full_reconcile(
            &db,
            &mut state,
            Some(&sink as &dyn SessionBundleSink),
            &[1, 2],
            None,
            &test_excluded_metric(),
            &test_dispatch_supervisor_metrics(),
            &test_changelog_metrics(),
            &bundle_host_kv::CapabilitySnapshot::new(),
            FullSyncReason::Reconcile,
            &bundle_active_set::ActiveVersionSnapshot::new(),
        )
        .await;
        assert_eq!(
            state
                .loaded()
                .digest_for(1, &(1, 0, "waddles.a".to_string())),
            Some(digest.as_str()),
            "the live session must still hold the bundle regardless of the other session's failure"
        );
        assert_eq!(
            state
                .loaded()
                .digest_for(2, &(1, 0, "waddles.a".to_string())),
            None,
            "the failed session must never be recorded as loaded"
        );
        assert_eq!(
            sink.calls().len(),
            2,
            "both sessions must have been attempted, got {:?}",
            sink.calls()
        );
    }

    /// A sink whose `load`/`unload` NEVER resolves for one specific session
    /// id (models the alpha 2026-10-03 failure mode exactly: the connection
    /// stays open and simply never answers, e.g. a draining executor that
    /// accepted the frame but will not reply before the rollout kills it) --
    /// every other session succeeds immediately. Distinct from
    /// `DiesForSessionSink` above, which fails FAST (`Err`); this sink never
    /// completes at all without `SESSION_CALL_TIMEOUT` stepping in.
    #[derive(Default)]
    struct HangsForSessionSink {
        calls: StdMutex<Vec<String>>,
        hangs_for: bundle_active_set::SessionId,
    }
    impl HangsForSessionSink {
        fn calls(&self) -> Vec<String> {
            self.calls.lock().unwrap().clone()
        }
    }
    impl SessionBundleSink for HangsForSessionSink {
        fn load<'a>(
            &'a self,
            session: bundle_active_set::SessionId,
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
            self.calls
                .lock()
                .unwrap()
                .push(format!("load:{session}:{}:{}", row.app_id, row.digest));
            let hangs = session == self.hangs_for;
            Box::pin(async move {
                if hangs {
                    // Never resolves on its own -- only `tokio::time::
                    // timeout` in `apply_active_set` can end this await.
                    std::future::pending::<Result<(), crate::dispatch::InvokeError>>().await
                } else {
                    Ok(())
                }
            })
        }
        fn unload<'a>(
            &'a self,
            _session: bundle_active_set::SessionId,
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
            Box::pin(async move { Ok(()) })
        }
    }

    /// **Regression (alpha 2026-10-03, the exact live failure): a Load that
    /// never gets ANY reply (session mid-disconnect, connection still open)
    /// must never block another session's Loads, and must never hang this
    /// tick forever.** Three active bundles (mirrors the real `ping`/
    /// `csping`/`pyping` incident) -- the terminating session (2) hangs on
    /// every single one; the live session (1) must still end up holding all
    /// three, and the whole call must finish quickly (proving the loads ran
    /// CONCURRENTLY, not sequentially behind the hang) rather than only
    /// after `3 * SESSION_CALL_TIMEOUT`.
    #[tokio::test]
    async fn run_full_reconcile_concurrently_loads_the_live_session_while_another_session_hangs() {
        let apps = [
            ("waddles.ping", format!("sha256:{}", "1".repeat(64))),
            ("waddles.csping", format!("sha256:{}", "2".repeat(64))),
            ("waddles.pyping", format!("sha256:{}", "3".repeat(64))),
        ];
        let active_rows: Vec<_> = apps
            .iter()
            .enumerate()
            .map(|(i, (app_id, _))| active_row(app_id, 1, 0, 10 + i as i64))
            .collect();
        let version_rows: Vec<_> = apps
            .iter()
            .enumerate()
            .map(|(i, (app_id, digest))| version_row(10 + i as i64, app_id, digest))
            .collect();
        let approval_rows: Vec<_> = apps
            .iter()
            .map(|(app_id, _)| approval_row(1, app_id))
            .collect();
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([active_rows])
            .append_query_results([version_rows])
            .append_query_results([approval_rows])
            .into_connection();
        let mut state = ConsumerState::new(0);
        let sink = HangsForSessionSink {
            hangs_for: 2,
            ..Default::default()
        };
        let metrics = test_changelog_metrics();

        let start = Instant::now();
        run_full_reconcile(
            &db,
            &mut state,
            Some(&sink as &dyn SessionBundleSink),
            &[1, 2],
            None,
            &test_excluded_metric(),
            &test_dispatch_supervisor_metrics(),
            &metrics,
            &bundle_host_kv::CapabilitySnapshot::new(),
            FullSyncReason::Reconcile,
            &bundle_active_set::ActiveVersionSnapshot::new(),
        )
        .await;
        let elapsed = start.elapsed();

        assert!(
            elapsed < SESSION_CALL_TIMEOUT * 2,
            "loads must run concurrently (one timeout's worth of wall time for \
             ALL three hung session-2 calls together), took {elapsed:?}"
        );
        for (app_id, digest) in &apps {
            assert_eq!(
                state.loaded().digest_for(1, &(1, 0, app_id.to_string())),
                Some(digest.as_str()),
                "live session 1 must hold {app_id} despite session 2 hanging on every load"
            );
            assert_eq!(
                state.loaded().digest_for(2, &(1, 0, app_id.to_string())),
                None,
                "a session whose Load never replied must NEVER be marked loaded \
                 (no optimistic marking) for {app_id}"
            );
        }
        assert_eq!(
            sink.calls().len(),
            6,
            "both sessions must have been attempted for all 3 bundles, got {:?}",
            sink.calls()
        );
        assert_eq!(
            metrics
                .bundle_loads_total
                .with_label_values(&["timeout"])
                .get(),
            3,
            "all 3 hung loads on session 2 must be counted as timeouts"
        );
        assert_eq!(
            metrics
                .bundle_loads_total
                .with_label_values(&["success"])
                .get(),
            3,
            "all 3 loads on the live session must still succeed"
        );
    }

    /// **Regression: a timed-out Load is retried on the next tick, never
    /// given up on.** First reconcile hangs (times out, not loaded); once
    /// the session stops hanging, the very next reconcile loads it
    /// successfully -- proving the timeout path leaves the bundle eligible
    /// for retry rather than treating it as a permanent failure.
    #[tokio::test]
    async fn a_timed_out_load_is_retried_and_succeeds_on_the_next_reconcile() {
        let digest = format!("sha256:{}", "4".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![active_row("waddles.a", 1, 0, 10)]])
            .append_query_results([vec![version_row(10, "waddles.a", &digest)]])
            .append_query_results([vec![approval_row(1, "waddles.a")]])
            .append_query_results([vec![active_row("waddles.a", 1, 0, 10)]])
            .append_query_results([vec![version_row(10, "waddles.a", &digest)]])
            .append_query_results([vec![approval_row(1, "waddles.a")]])
            .into_connection();
        let mut state = ConsumerState::new(0);
        // `hangs_for: 1` on the first call; flipped to a harmless id before
        // the second so the exact same session succeeds on retry.
        let sink = HangsForSessionSink {
            hangs_for: 1,
            ..Default::default()
        };
        let metrics = test_changelog_metrics();

        run_full_reconcile(
            &db,
            &mut state,
            Some(&sink as &dyn SessionBundleSink),
            &[1],
            None,
            &test_excluded_metric(),
            &test_dispatch_supervisor_metrics(),
            &metrics,
            &bundle_host_kv::CapabilitySnapshot::new(),
            FullSyncReason::Reconcile,
            &bundle_active_set::ActiveVersionSnapshot::new(),
        )
        .await;
        assert_eq!(
            state
                .loaded()
                .digest_for(1, &(1, 0, "waddles.a".to_string())),
            None,
            "a timed-out load must not be marked loaded"
        );
        assert_eq!(
            metrics
                .bundle_loads_total
                .with_label_values(&["timeout"])
                .get(),
            1
        );

        // Same scope is still active and still not loaded on session 1 --
        // the next reconcile must retry it, this time against a sink that
        // answers immediately.
        let retry_sink = DiesForSessionSink {
            dies_for: 999, // no session dies; every live session succeeds
            ..Default::default()
        };
        run_full_reconcile(
            &db,
            &mut state,
            Some(&retry_sink as &dyn SessionBundleSink),
            &[1],
            None,
            &test_excluded_metric(),
            &test_dispatch_supervisor_metrics(),
            &metrics,
            &bundle_host_kv::CapabilitySnapshot::new(),
            FullSyncReason::Reconcile,
            &bundle_active_set::ActiveVersionSnapshot::new(),
        )
        .await;
        assert_eq!(
            state
                .loaded()
                .digest_for(1, &(1, 0, "waddles.a".to_string())),
            Some(digest.as_str()),
            "the retried load must succeed once the session actually answers"
        );
    }

    /// Fail-closed requirement 2: an active bundle loaded on ZERO live
    /// sessions after a sync increments `bundle_zero_session_total` and is
    /// logged -- never silently left for a user report to surface.
    #[tokio::test]
    async fn run_full_reconcile_increments_the_zero_session_metric_when_every_session_fails() {
        let digest = format!("sha256:{}", "5".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![active_row("waddles.a", 1, 0, 10)]])
            .append_query_results([vec![version_row(10, "waddles.a", &digest)]])
            .append_query_results([vec![approval_row(1, "waddles.a")]])
            .into_connection();
        let mut state = ConsumerState::new(0);
        let sink = FailingSink;
        let metrics = test_changelog_metrics();
        run_full_reconcile(
            &db,
            &mut state,
            Some(&sink as &dyn SessionBundleSink),
            &[1],
            None,
            &test_excluded_metric(),
            &test_dispatch_supervisor_metrics(),
            &metrics,
            &bundle_host_kv::CapabilitySnapshot::new(),
            FullSyncReason::Reconcile,
            &bundle_active_set::ActiveVersionSnapshot::new(),
        )
        .await;
        assert_eq!(
            metrics.bundle_zero_session_total.get(),
            1,
            "an active bundle loaded nowhere must increment the fail-closed metric"
        );
    }

    /// **The exact alpha 2026-10-03 sequence, end to end through
    /// `run_full_reconcile`:** the old (terminating) pod's session (2) joins
    /// alongside the new live session (1); both receive every active bundle.
    /// Session 2 then disappears from the live set (closed) -- `run`'s own
    /// session-removal handling (`LoadedSessions::on_session_removed`) drops
    /// its state, and the live session (1) is proven to already hold
    /// everything with zero further intervention.
    ///
    /// regression: bundles loaded only onto a terminating executor during
    /// rollout; live executor got none (alpha 2026-10-03)
    #[tokio::test]
    async fn alpha_2026_10_03_sequence_through_run_full_reconcile() {
        let digest_ping = format!("sha256:{}", "1".repeat(64));
        let digest_csping = format!("sha256:{}", "2".repeat(64));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![
                active_row("ping", 1, 0, 10),
                active_row("csping", 1, 0, 11),
            ]])
            .append_query_results([vec![
                version_row(10, "ping", &digest_ping),
                version_row(11, "csping", &digest_csping),
            ]])
            .append_query_results([vec![approval_row(1, "ping"), approval_row(1, "csping")]])
            .into_connection();
        let mut state = ConsumerState::new(0);
        let sink = FakeSink::default();
        run_full_reconcile(
            &db,
            &mut state,
            Some(&sink as &dyn SessionBundleSink),
            &[1, 2],
            None,
            &test_excluded_metric(),
            &test_dispatch_supervisor_metrics(),
            &test_changelog_metrics(),
            &bundle_host_kv::CapabilitySnapshot::new(),
            FullSyncReason::Reconcile,
            &bundle_active_set::ActiveVersionSnapshot::new(),
        )
        .await;
        for app in ["ping", "csping"] {
            for session in [1u64, 2u64] {
                assert!(
                    state
                        .loaded()
                        .digest_for(session, &(1, 0, app.to_string()))
                        .is_some(),
                    "session {session} must hold {app} after the initial sync"
                );
            }
        }

        // Session 2 (the old, terminating pod) is removed from the registry.
        let dropped = state.loaded.on_session_removed(2);
        assert_eq!(dropped.len(), 2, "session 2 had both bundles loaded");

        // The live session (1) still holds everything -- zero manual
        // intervention required, proving the fix for the alpha incident
        // where the live executor ended up holding nothing at all.
        for app in ["ping", "csping"] {
            assert_eq!(
                state.loaded().digest_for(1, &(1, 0, app.to_string())),
                Some(if app == "ping" {
                    digest_ping.as_str()
                } else {
                    digest_csping.as_str()
                })
            );
            assert!(state
                .loaded()
                .digest_for(2, &(1, 0, app.to_string()))
                .is_none());
        }
    }

    #[test]
    fn should_stop_consumers_only_on_the_enabled_to_disabled_transition() {
        assert!(should_stop_consumers(false, true));
        assert!(!should_stop_consumers(false, false));
        assert!(!should_stop_consumers(true, true));
        assert!(!should_stop_consumers(true, false));
    }

    /// Item 4 (gh security review on PR #406), per-session fix (regression:
    /// bundles loaded only onto a terminating executor during rollout; live
    /// executor got none, alpha 2026-10-03): a session present in `live` but
    /// not `known` is `added`; one in `known` but not `live` is `removed`; a
    /// session in both is neither. See `core/svc_process/src/
    /// changelog_consumer.rs`'s identical test suite.
    #[test]
    fn diff_sessions_reports_additions_and_removals_independently() {
        let known: std::collections::HashSet<bundle_active_set::SessionId> =
            [1, 2].into_iter().collect();
        let live: std::collections::HashSet<bundle_active_set::SessionId> =
            [2, 3].into_iter().collect();
        let mut diff = diff_sessions(&known, &live);
        diff.added.sort_unstable();
        diff.removed.sort_unstable();
        assert_eq!(diff.added, vec![3]);
        assert_eq!(diff.removed, vec![1]);
    }

    /// **The exact alpha 2026-10-03 shape:** the old pod's session (2)
    /// connects in the SAME tick the new pod's session (1) is already known
    /// -- both are reported, and 2's later disappearance is a separate,
    /// independent `removed` event on a later diff, never confused with
    /// session 1 (which was never touched).
    #[test]
    fn diff_sessions_handles_a_rollout_overlap_then_the_old_sessions_removal() {
        let known: std::collections::HashSet<bundle_active_set::SessionId> =
            [1].into_iter().collect();
        let live_both: std::collections::HashSet<bundle_active_set::SessionId> =
            [1, 2].into_iter().collect();
        let diff = diff_sessions(&known, &live_both);
        assert_eq!(diff.added, vec![2]);
        assert!(diff.removed.is_empty());

        let live_after_old_dies: std::collections::HashSet<bundle_active_set::SessionId> =
            [1].into_iter().collect();
        let diff2 = diff_sessions(&live_both, &live_after_old_dies);
        assert!(diff2.added.is_empty());
        assert_eq!(diff2.removed, vec![2]);
    }

    #[test]
    fn diff_sessions_is_empty_when_nothing_changed() {
        let s: std::collections::HashSet<bundle_active_set::SessionId> =
            [1, 2].into_iter().collect();
        assert_eq!(diff_sessions(&s, &s), SessionDiff::default());
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
                None,
                excluded_metric,
                test_dispatch_supervisor_metrics(),
                metrics,
                Arc::new(bundle_host_kv::CapabilitySnapshot::new()),
                Arc::clone(&changelog_consumer_ready),
                Arc::new(ActiveDigests::new()),
                Arc::new(LoadedSessions::new()),
                bundle_active_set::ActiveVersionSnapshot::new(),
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
    impl SessionBundleSink for FailingSink {
        fn load<'a>(
            &'a self,
            _session: bundle_active_set::SessionId,
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
            _session: bundle_active_set::SessionId,
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
                    version_id: 1,
                    digest: digest.to_string(),
                    component_key: "k".to_string(),
                    sidecar_key: "s".to_string(),
                    artifact_signature: None,
                    artifact_signature_key_id: None,
                    artifact_signed_approval_id: None,
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
            Some(&sink as &dyn SessionBundleSink),
            &[1],
            None,
            &test_excluded_metric(),
            &test_dispatch_supervisor_metrics(),
            &metrics,
            &bundle_host_kv::CapabilitySnapshot::new(),
            Instant::now(),
            Duration::from_millis(20),
            &bundle_active_set::ActiveVersionSnapshot::new(),
        )
        .await;
        assert_eq!(sink.calls(), vec![format!("load:1:waddles.a:{digest}")]);
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
            &[],
            None,
            &test_excluded_metric(),
            &test_dispatch_supervisor_metrics(),
            &test_changelog_metrics(),
            &bundle_host_kv::CapabilitySnapshot::new(),
            Instant::now(),
            Duration::from_millis(20),
            &bundle_active_set::ActiveVersionSnapshot::new(),
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
            Some(&sink as &dyn SessionBundleSink),
            &[1],
            None,
            &test_excluded_metric(),
            &test_dispatch_supervisor_metrics(),
            &metrics,
            &bundle_host_kv::CapabilitySnapshot::new(),
            t0,
            debounce,
            &bundle_active_set::ActiveVersionSnapshot::new(),
        )
        .await;
        assert_eq!(sink.calls(), vec![format!("load:1:waddles.a:{digest_a}")]);

        state.pending_full_sync = Some(FullSyncReason::Reconnect);
        drain_pending_full_sync(
            &db,
            &mut state,
            Some(&sink as &dyn SessionBundleSink),
            &[1],
            None,
            &test_excluded_metric(),
            &test_dispatch_supervisor_metrics(),
            &metrics,
            &bundle_host_kv::CapabilitySnapshot::new(),
            t0 + Duration::from_millis(5),
            debounce,
            &bundle_active_set::ActiveVersionSnapshot::new(),
        )
        .await;
        assert_eq!(
            sink.calls(),
            vec![format!("load:1:waddles.a:{digest_a}")],
            "a reconnect within the debounce window must be coalesced, not re-sent"
        );
        assert_eq!(state.pending_full_sync(), Some(FullSyncReason::Reconnect));

        drain_pending_full_sync(
            &db,
            &mut state,
            Some(&sink as &dyn SessionBundleSink),
            &[1],
            None,
            &test_excluded_metric(),
            &test_dispatch_supervisor_metrics(),
            &metrics,
            &bundle_host_kv::CapabilitySnapshot::new(),
            t0 + Duration::from_millis(25),
            debounce,
            &bundle_active_set::ActiveVersionSnapshot::new(),
        )
        .await;
        assert_eq!(
            sink.calls(),
            vec![
                format!("load:1:waddles.a:{digest_a}"),
                format!("load:1:waddles.b:{digest_b}"),
                format!("unload:1:waddles.a:{digest_a}"),
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
            Some(&sink as &dyn SessionBundleSink),
            &[1],
            None,
            &test_excluded_metric(),
            &test_dispatch_supervisor_metrics(),
            &metrics,
            &bundle_host_kv::CapabilitySnapshot::new(),
            &bundle_active_set::ActiveVersionSnapshot::new(),
        )
        .await;
        assert_eq!(state.last_seq(), 100);
        assert_eq!(sink.calls(), vec![format!("load:1:waddles.a:{digest}")]);
        assert_eq!(
            state
                .loaded()
                .digest_for(1, &(1, 0, "waddles.a".to_string())),
            Some(digest.as_str())
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
            Some(&failing_sink as &dyn SessionBundleSink),
            &[1],
            None,
            &test_excluded_metric(),
            &test_dispatch_supervisor_metrics(),
            &metrics,
            &bundle_host_kv::CapabilitySnapshot::new(),
            &bundle_active_set::ActiveVersionSnapshot::new(),
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
            Some(&sink as &dyn SessionBundleSink),
            &[1],
            None,
            &test_excluded_metric(),
            &test_dispatch_supervisor_metrics(),
            &metrics,
            &bundle_host_kv::CapabilitySnapshot::new(),
            &bundle_active_set::ActiveVersionSnapshot::new(),
        )
        .await;
        assert_eq!(sink.calls(), vec![format!("load:1:waddles.a:{digest}")]);
        assert_eq!(
            metrics
                .bundle_loads_total
                .with_label_values(&["success"])
                .get(),
            1
        );
    }
}
