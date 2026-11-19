//! Shared decision logic for an immediate, out-of-band "full send" of the
//! active-bundle set to the executor -- the fix for a regression where
//! `core/svc_process` and `core/svc_action`'s `changelog_consumer::run`
//! loops detected a startup or a new executor connection, correctly reset
//! their local `loaded` bookkeeping (the executor wipes its own registry on
//! every disconnect), but never forced a resend: the very next incremental
//! tick's own early return (`safe_seq <= last_seq`, the common case right
//! after a reconnect since nothing in the DB actually changed) meant no
//! `Load` was ever attempted until the next periodic full reconcile --
//! `full_reconcile_interval`, 15 minutes by default. Every Discord message
//! in that window got `UnknownBundle`.
//!
//! regression: loads waited for 15-min full reconcile after
//! startup/reconnect, UnknownBundle (alpha 2026-10-03)
//!
//! Both services' `ConsumerState` shapes are identical in the one way this
//! module cares about (`HashMap<ScopeKey, ActiveSetRead>` for `by_scope`,
//! `HashMap<AppScope, String>` for `loaded`), so the pure decision logic
//! (no I/O, no `sea_orm`) lives exactly once here; each service's own
//! `changelog_consumer::run`/`run_incremental_tick` wires it to the real
//! `BundleSink`/DB read.

use std::collections::HashMap;
use std::time::{Duration, Instant};

use crate::multi_tenant::{scoped_active_rows, AppScope, ScopeKey};
use crate::query::ActiveSetRead;
use crate::session_sync::{SessionId, SessionLoaded};

/// Why a full send was triggered -- the `reason` label on the
/// `bundle_full_sync_total` metric and the structured field on each full
/// send's own INFO log line.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FullSyncReason {
    /// The consumer just started: `initial_state`'s own full read must not
    /// sit unsent waiting for the next watermark advance or full-reconcile
    /// tick once an executor connection becomes available.
    Startup,
    /// A new executor connection was detected (by pointer identity) -- the
    /// executor wipes its ENTIRE bundle registry on every disconnect, so
    /// the freshly-reconnected (empty) executor needs every active bundle
    /// resent immediately, not at the next full reconcile.
    Reconnect,
    /// The periodic full reconcile (`full_reconcile_interval`, default 15
    /// minutes) -- including the retention-exceeded/change-log-gap forced
    /// reconciles, which are themselves already counted by their own
    /// dedicated metrics and are simply a full reconcile triggered early.
    Reconcile,
    /// A poll tick found the active set and the locally-tracked `loaded`
    /// state out of sync even though the change-log watermark has not
    /// advanced -- the exact condition this regression's fix closes.
    Diverged,
    /// A live executor session disappeared while placement is sharded: its
    /// slots now have a new owner set, so surviving owners must pick up the
    /// loads that restore the replica count -- without waiting for the next
    /// change-log advance or full reconcile (new executors already force
    /// [`FullSyncReason::Reconnect`]).
    Rebalance,
}

impl FullSyncReason {
    /// The `reason` label value for `bundle_full_sync_total`.
    pub fn as_str(self) -> &'static str {
        match self {
            FullSyncReason::Startup => "startup",
            FullSyncReason::Reconnect => "reconnect",
            FullSyncReason::Reconcile => "reconcile",
            FullSyncReason::Diverged => "diverged",
            FullSyncReason::Rebalance => "rebalance",
        }
    }
}

impl std::fmt::Display for FullSyncReason {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Whether enough time has passed since the last full send (`last_sent`) to
/// perform another one now, at `now` -- coalesces a reconnect storm (many
/// `detect_new_connection` events in quick succession, e.g. a flapping TCP
/// connection) into a single full send per `debounce` window instead of one
/// per detected reconnect. `None` (no full send has happened yet this
/// process lifetime) always proceeds -- there is nothing to coalesce
/// against. `saturating_duration_since` so a synthetic/test `now` that is
/// (incorrectly) earlier than `last_sent` can never underflow-panic; it is
/// simply treated as "no time has passed" (debounced).
pub fn should_send_full_sync(last_sent: Option<Instant>, now: Instant, debounce: Duration) -> bool {
    match last_sent {
        None => true,
        Some(t) => now.saturating_duration_since(t) >= debounce,
    }
}

/// Whether the active set (`by_scope`) and the locally-tracked `loaded`
/// state have diverged -- true when at least one scoped, currently-active
/// bundle is either missing from `loaded` entirely or loaded at a different
/// digest. Pure (no I/O): a poll tick calls this even when the change-log
/// watermark hasn't advanced at all, to catch "active set non-empty but
/// loaded-state empty or diverged" (e.g. a reconnect cleared `loaded` but
/// the watermark genuinely has not moved since).
pub fn loaded_state_diverged(
    by_scope: &HashMap<ScopeKey, ActiveSetRead>,
    loaded: &HashMap<AppScope, String>,
) -> bool {
    let active = scoped_active_rows(by_scope);
    active
        .iter()
        .any(|(scope, row)| loaded.get(scope) != Some(&row.digest))
}

/// Per-session counterpart to [`loaded_state_diverged`]: true when `session`
/// specifically is missing, or holds the wrong digest for, at least one
/// currently-active scoped bundle. Used by [`any_session_diverged`] (the
/// actual per-tick check -- #547's divergence check compares per session,
/// not against one flattened view that could paper over a single
/// out-of-sync session).
pub fn session_loaded_state_diverged(
    by_scope: &HashMap<ScopeKey, ActiveSetRead>,
    loaded: &SessionLoaded<AppScope>,
    session: SessionId,
) -> bool {
    let active = scoped_active_rows(by_scope);
    active
        .iter()
        .any(|(scope, row)| loaded.digest_for(session, scope) != Some(row.digest.as_str()))
}

/// True if ANY of `live_sessions` has diverged from the active set --
/// `live_sessions` empty (no executor connected at all) is never
/// "diverged" (there is nothing to resync onto).
pub fn any_session_diverged(
    by_scope: &HashMap<ScopeKey, ActiveSetRead>,
    loaded: &SessionLoaded<AppScope>,
    live_sessions: &[SessionId],
) -> bool {
    live_sessions
        .iter()
        .any(|&session| session_loaded_state_diverged(by_scope, loaded, session))
}

/// Routing-aware [`any_session_diverged`]: true when the placement planner
/// still has work for any live session -- a TARGET session missing (or at the
/// wrong digest for) an active bundle, or a NON-target session holding a
/// bundle whose targets are all ready (make-before-break unload due).
///
/// Under executor sharding the plain per-session check is wrong: a session
/// that is not an owner of an app legitimately never holds it, so it would
/// read as "diverged" on every poll tick forever and force a spurious
/// full-sync pass (INFO line + `bundle_full_sync_total{reason="diverged"}`)
/// each time. `targets_of(scope, row)` is the router's target sessions for the
/// scope's app. `live_sessions` empty is never diverged.
pub fn any_session_diverged_routed(
    by_scope: &HashMap<ScopeKey, ActiveSetRead>,
    loaded: &SessionLoaded<AppScope>,
    live_sessions: &[SessionId],
    targets_of: impl Fn(&AppScope, &crate::query::ActiveBundleRow) -> Vec<SessionId>,
) -> bool {
    if live_sessions.is_empty() {
        return false;
    }
    let active = scoped_active_rows(by_scope);
    let plan = crate::session_sync::plan_sessions_routed(
        loaded,
        &active,
        live_sessions,
        targets_of,
        |row| row.digest.as_str(),
    );
    // Stale (no-longer-active) keys are the diff path's business, exactly as
    // with `any_session_diverged`; only ACTIVE keys count here.
    !plan.to_load.is_empty()
        || plan
            .to_unload
            .iter()
            .any(|(_, scope, _)| active.contains_key(scope))
}

/// A short, stable-length prefix of a bundle digest for log lines -- never
/// the full digest (noisy, and the full value is already available via
/// `grep` against the per-bundle structured field when needed). Safe on any
/// ASCII digest string shorter than the prefix length (returns the whole
/// string rather than panicking on a byte-index that isn't a char
/// boundary -- digests are always ASCII hex, so byte index == char index,
/// but `get` is used defensively regardless).
pub fn digest_prefix(digest: &str) -> &str {
    digest.get(..19).unwrap_or(digest)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::query::ActiveBundleRow;

    fn row(app_id: &str, digest: &str) -> ActiveBundleRow {
        ActiveBundleRow {
            app_id: app_id.to_string(),
            version: "1".to_string(),
            version_id: 1,
            digest: digest.to_string(),
            component_key: format!("bundles/{digest}/component.wasm"),
            sidecar_key: format!("bundles/{digest}/sidecar.json"),
            artifact_signature: None,
            artifact_signature_key_id: None,
            artifact_signed_approval_id: None,
            language: "rust".to_string(),
            declared_capabilities: Vec::new(),
            egress: Vec::new(),
            egress_rps: None,
            tools: Default::default(),
        }
    }

    fn active_set(rows: Vec<ActiveBundleRow>) -> ActiveSetRead {
        ActiveSetRead {
            rows,
            excluded: Vec::new(),
            degraded: Vec::new(),
        }
    }

    #[test]
    fn full_sync_reason_as_str_matches_the_documented_label_set() {
        assert_eq!(FullSyncReason::Startup.as_str(), "startup");
        assert_eq!(FullSyncReason::Reconnect.as_str(), "reconnect");
        assert_eq!(FullSyncReason::Reconcile.as_str(), "reconcile");
        assert_eq!(FullSyncReason::Diverged.as_str(), "diverged");
        assert_eq!(FullSyncReason::Rebalance.as_str(), "rebalance");
    }

    #[test]
    fn should_send_full_sync_always_proceeds_with_no_prior_send() {
        assert!(should_send_full_sync(
            None,
            Instant::now(),
            Duration::from_secs(2)
        ));
    }

    #[test]
    fn should_send_full_sync_debounces_within_the_window() {
        let t0 = Instant::now();
        let debounce = Duration::from_secs(2);
        assert!(
            !should_send_full_sync(Some(t0), t0 + Duration::from_millis(500), debounce),
            "within the debounce window must be coalesced (skipped)"
        );
    }

    #[test]
    fn should_send_full_sync_proceeds_once_the_window_elapses() {
        let t0 = Instant::now();
        let debounce = Duration::from_secs(2);
        assert!(should_send_full_sync(
            Some(t0),
            t0 + Duration::from_secs(3),
            debounce
        ));
    }

    #[test]
    fn should_send_full_sync_never_underflows_on_a_now_before_last_sent() {
        let t0 = Instant::now();
        // A synthetic `now` earlier than `last_sent` must never panic --
        // `saturating_duration_since` floors at zero, which is < any
        // positive debounce, so this is simply treated as debounced.
        assert!(!should_send_full_sync(
            Some(t0 + Duration::from_secs(5)),
            t0,
            Duration::from_secs(2)
        ));
    }

    #[test]
    fn loaded_state_diverged_is_false_when_empty_and_unloaded() {
        let by_scope = HashMap::new();
        let loaded = HashMap::new();
        assert!(!loaded_state_diverged(&by_scope, &loaded));
    }

    #[test]
    fn loaded_state_diverged_is_true_when_active_but_nothing_loaded() {
        let mut by_scope = HashMap::new();
        by_scope.insert((1, 0), active_set(vec![row("waddles.a", "d1")]));
        let loaded = HashMap::new();
        assert!(loaded_state_diverged(&by_scope, &loaded));
    }

    #[test]
    fn loaded_state_diverged_is_true_on_a_digest_mismatch() {
        let mut by_scope = HashMap::new();
        by_scope.insert((1, 0), active_set(vec![row("waddles.a", "d2")]));
        let mut loaded = HashMap::new();
        loaded.insert((1, 0, "waddles.a".to_string()), "d1".to_string());
        assert!(loaded_state_diverged(&by_scope, &loaded));
    }

    #[test]
    fn loaded_state_diverged_is_false_when_fully_in_sync() {
        let mut by_scope = HashMap::new();
        by_scope.insert((1, 0), active_set(vec![row("waddles.a", "d1")]));
        let mut loaded = HashMap::new();
        loaded.insert((1, 0, "waddles.a".to_string()), "d1".to_string());
        assert!(!loaded_state_diverged(&by_scope, &loaded));
    }

    #[test]
    fn any_session_diverged_is_false_with_no_live_sessions() {
        let mut by_scope = HashMap::new();
        by_scope.insert((1, 0), active_set(vec![row("waddles.a", "d1")]));
        let loaded = SessionLoaded::new();
        assert!(!any_session_diverged(&by_scope, &loaded, &[]));
    }

    #[test]
    fn any_session_diverged_is_true_when_one_of_several_live_sessions_lacks_the_bundle() {
        let mut by_scope = HashMap::new();
        by_scope.insert((1, 0), active_set(vec![row("waddles.a", "d1")]));
        let mut loaded = SessionLoaded::new();
        loaded.mark_loaded(1, (1, 0, "waddles.a".to_string()), "d1".to_string());
        // Session 2 has nothing loaded -- diverged even though session 1 is fine.
        assert!(any_session_diverged(&by_scope, &loaded, &[1, 2]));
    }

    #[test]
    fn any_session_diverged_is_false_when_every_live_session_matches() {
        let mut by_scope = HashMap::new();
        by_scope.insert((1, 0), active_set(vec![row("waddles.a", "d1")]));
        let mut loaded = SessionLoaded::new();
        loaded.mark_loaded(1, (1, 0, "waddles.a".to_string()), "d1".to_string());
        assert!(!any_session_diverged(&by_scope, &loaded, &[1]));
    }

    #[test]
    fn digest_prefix_truncates_a_long_digest() {
        let digest = format!("sha256:{}", "a".repeat(64));
        assert_eq!(digest_prefix(&digest), "sha256:aaaaaaaaaaaa");
        assert_eq!(digest_prefix(&digest).len(), 19);
    }

    #[test]
    fn digest_prefix_returns_the_whole_string_when_shorter_than_the_prefix() {
        assert_eq!(digest_prefix("short"), "short");
    }

    fn one_scope(app: &str, digest: &str) -> HashMap<ScopeKey, ActiveSetRead> {
        let mut m = HashMap::new();
        m.insert((1, 0), active_set(vec![row(app, digest)]));
        m
    }

    /// Regression (sharding): a NON-owner session never holds the app, which
    /// the plain check reads as diverged forever (a forced full-sync pass on
    /// every poll tick); the routed check does not.
    #[test]
    fn routed_divergence_ignores_a_session_that_is_not_a_target() {
        let by_scope = one_scope("waddles.a", "sha256:a");
        let mut loaded = SessionLoaded::new();
        loaded.mark_loaded(1, (1, 0, "waddles.a".to_string()), "sha256:a".into());
        assert!(
            any_session_diverged(&by_scope, &loaded, &[1, 2]),
            "the unrouted check wrongly flags the non-owner"
        );
        assert!(!any_session_diverged_routed(
            &by_scope,
            &loaded,
            &[1, 2],
            |_, _| vec![1]
        ));
    }

    /// A TARGET session that lacks the digest (or has a stale one) IS diverged.
    #[test]
    fn routed_divergence_flags_a_target_missing_the_digest() {
        let by_scope = one_scope("waddles.a", "sha256:new");
        let mut loaded = SessionLoaded::new();
        let live = [1, 2];
        assert!(any_session_diverged_routed(
            &by_scope,
            &loaded,
            &live,
            |_, _| vec![1]
        ));
        loaded.mark_loaded(1, (1, 0, "waddles.a".to_string()), "sha256:old".into());
        assert!(any_session_diverged_routed(
            &by_scope,
            &loaded,
            &live,
            |_, _| vec![1]
        ));
        loaded.mark_loaded(1, (1, 0, "waddles.a".to_string()), "sha256:new".into());
        assert!(!any_session_diverged_routed(
            &by_scope,
            &loaded,
            &live,
            |_, _| vec![1]
        ));
    }

    /// Make-before-break: a leftover non-target holding an active bundle is due
    /// an unload once every target is ready (diverged); the missing target's
    /// load is what counts first while it is not.
    #[test]
    fn routed_divergence_flags_a_leftover_non_target() {
        let by_scope = one_scope("waddles.a", "sha256:a");
        let scope = (1, 0, "waddles.a".to_string());
        let mut loaded = SessionLoaded::new();
        loaded.mark_loaded(2, scope.clone(), "sha256:a".into());
        let live = [1, 2];
        assert!(any_session_diverged_routed(
            &by_scope,
            &loaded,
            &live,
            |_, _| vec![1]
        ));
        loaded.mark_loaded(1, scope, "sha256:a".into());
        assert!(any_session_diverged_routed(
            &by_scope,
            &loaded,
            &live,
            |_, _| vec![1]
        ));
    }

    /// Stale (deactivated) keys stay the change-diff path's business, and no
    /// live session at all is never diverged.
    #[test]
    fn routed_divergence_ignores_stale_keys_and_empty_live_set() {
        let by_scope = one_scope("waddles.a", "sha256:a");
        let mut loaded = SessionLoaded::new();
        loaded.mark_loaded(1, (1, 0, "waddles.a".to_string()), "sha256:a".into());
        loaded.mark_loaded(1, (1, 0, "waddles.gone".to_string()), "sha256:g".into());
        assert!(!any_session_diverged_routed(
            &by_scope,
            &loaded,
            &[1],
            |_, _| vec![1]
        ));
        assert!(!any_session_diverged_routed(
            &by_scope,
            &loaded,
            &[],
            |_, _| vec![]
        ));
    }
}
