//! Per-SESSION loaded-bundle tracking + sync-plan computation -- shared by
//! `core/svc_process` and `core/svc_action`'s loader modules.
//!
//! regression: bundles loaded only onto a terminating executor during
//! rollout; live executor got none (alpha 2026-10-03)
//!
//! **The bug this module replaces:** both services tracked "what's loaded"
//! as one flat `HashMap<scope, digest>`, driven through whichever single
//! executor connection `ConnectionRegistry::active()` (#545) happened to
//! pick -- "newest session wins". During a rollout, the OLD pod's executor
//! can reconnect microseconds AFTER the new pod's executor (a terminating
//! pod's process does not stop accepting/dialing instantly), briefly
//! becoming the "newest" session. Every `Load` that tick went to that
//! about-to-die session; the one flat `loaded` map then believed those
//! bundles were loaded *somewhere*, so nothing re-sent them once the old
//! session closed -- the live executor never received a single `Load` and
//! sat in `UnknownBundle` until a manual restart.
//!
//! The fix: loaded-state is keyed by `(session_id, scope)`, not `scope`
//! alone ([`SessionLoaded`]), and [`plan_sessions`] computes a sync plan
//! against *every* live session independently -- a full sync sends `Load`
//! for every active bundle to every live session that lacks it, never just
//! the newest.

use std::collections::{HashMap, HashSet};
use std::hash::Hash;

/// The #545 host-API session registry's per-connection id
/// (`host_api::ConnectionRegistry::register`'s return value) -- re-exported
/// here under a descriptive alias rather than a bare `u64` at every call
/// site in this module's own API.
pub type SessionId = u64;

/// Tracks, per live executor session, which `K`-keyed scope is loaded at
/// which digest. See the module doc for why this replaced a single flat
/// `HashMap<K, String>`.
#[derive(Debug, Clone, Default)]
pub struct SessionLoaded<K: Eq + Hash + Clone> {
    by_session: HashMap<SessionId, HashMap<K, String>>,
}

impl<K: Eq + Hash + Clone> SessionLoaded<K> {
    pub fn new() -> Self {
        Self {
            by_session: HashMap::new(),
        }
    }

    /// Records `key` as loaded at `digest` for `session` -- called after a
    /// successful `Load` wire call to that specific session.
    pub fn mark_loaded(&mut self, session: SessionId, key: K, digest: String) {
        self.by_session
            .entry(session)
            .or_default()
            .insert(key, digest);
    }

    /// Clears `key`'s entry for `session` -- called after a successful
    /// `Unload` wire call to that specific session.
    pub fn mark_unloaded(&mut self, session: SessionId, key: &K) {
        if let Some(scopes) = self.by_session.get_mut(&session) {
            scopes.remove(key);
        }
    }

    /// A session has been REMOVED from the host-API registry (closed,
    /// reconnect, or termination) -- drops ALL of its loaded-state at once
    /// and returns what it had loaded, for the caller's own ERROR/metric
    /// bookkeeping (e.g. "did this leave any active bundle on zero
    /// sessions"). This is the step the alpha incident's old design never
    /// performed: nothing reacted to a session disappearing, so its
    /// (now-meaningless) entries in the single flat map lingered and
    /// suppressed a resend forever.
    pub fn on_session_removed(&mut self, session: SessionId) -> HashMap<K, String> {
        self.by_session.remove(&session).unwrap_or_default()
    }

    /// The digest `session` currently has loaded for `key`, or `None`.
    pub fn digest_for(&self, session: SessionId, key: &K) -> Option<&str> {
        self.by_session.get(&session)?.get(key).map(String::as_str)
    }

    /// Every `(session, key, digest)` triple currently tracked, in no
    /// particular order.
    pub fn iter(&self) -> impl Iterator<Item = (SessionId, &K, &str)> {
        self.by_session
            .iter()
            .flat_map(|(&sid, scopes)| scopes.iter().map(move |(k, d)| (sid, k, d.as_str())))
    }

    /// Live sessions (any digest) that currently have `key` loaded --
    /// callers use this to pick a dispatch target (see
    /// [`pick_session_with_digest`] for the digest-exact variant invoke
    /// actually needs) or to detect "loaded on zero sessions".
    pub fn sessions_with(&self, key: &K) -> Vec<SessionId> {
        self.by_session
            .iter()
            .filter(|(_, scopes)| scopes.contains_key(key))
            .map(|(&sid, _)| sid)
            .collect()
    }

    /// Number of sessions that currently have `key` loaded (any digest) --
    /// `0` is the fail-closed "no live executor can serve this bundle"
    /// state callers must log as ERROR and keep retrying, never invoke
    /// into.
    pub fn loaded_count(&self, key: &K) -> usize {
        self.sessions_with(key).len()
    }

    /// Every session id this tracker currently holds any state for.
    pub fn known_sessions(&self) -> Vec<SessionId> {
        self.by_session.keys().copied().collect()
    }

    /// Total `(session, key)` pairs currently tracked, across every session
    /// -- a bundle loaded on two live sessions counts twice. Backs the
    /// `bundles_loaded` gauge (fan-out visibility, not just presence).
    pub fn total_entries(&self) -> usize {
        self.by_session.values().map(HashMap::len).sum()
    }

    /// True if no session has anything loaded at all.
    pub fn is_empty(&self) -> bool {
        self.by_session.values().all(|m| m.is_empty())
    }
}

/// The live session (if any) that has `key` loaded at exactly `digest` --
/// **prefer newest** (highest session id) among qualifying sessions, same
/// "newest wins" tie-break `host_api::ConnectionRegistry::active` already
/// used, now scoped to "and actually has this digest" rather than blindly
/// picking the newest live connection regardless of its loaded-state.
/// Dispatch/invoke MUST use this (or an equivalent digest-exact check)
/// instead of `ConnectionRegistry::active()` alone -- picking a live
/// session that simply happens to be newest, without checking it actually
/// has the target digest loaded, is exactly the alpha 2026-10-03 failure
/// mode.
pub fn pick_session_with_digest<K: Eq + Hash + Clone>(
    loaded: &SessionLoaded<K>,
    key: &K,
    digest: &str,
) -> Option<SessionId> {
    loaded
        .by_session
        .iter()
        .filter(|(_, scopes)| scopes.get(key).is_some_and(|d| d == digest))
        .map(|(&sid, _)| sid)
        .max()
}

/// What [`plan_sessions`] decided to do this tick: bundles to `Load` (a
/// live session lacking the target digest for an active scope) and bundles
/// to `Unload` (a live session holding a scope no longer in the active
/// set). Generic over the row type `Row` each service's own
/// `ActiveBundleRow`-equivalent supplies.
#[derive(Debug, Clone)]
pub struct SessionSyncPlan<K, Row> {
    pub to_load: Vec<(SessionId, K, Row)>,
    pub to_unload: Vec<(SessionId, K, String)>,
}

impl<K, Row> Default for SessionSyncPlan<K, Row> {
    fn default() -> Self {
        Self {
            to_load: Vec::new(),
            to_unload: Vec::new(),
        }
    }
}

impl<K, Row> SessionSyncPlan<K, Row> {
    pub fn is_empty(&self) -> bool {
        self.to_load.is_empty() && self.to_unload.is_empty()
    }
}

/// Computes [`SessionSyncPlan`] for `active` (this tick's DB-driven active
/// set, keyed by `K`) against `loaded` (per-session loaded-state) and
/// `live_sessions` (every session the host-API registry currently reports
/// live) -- **a full sync sends `Load` for every active bundle to EVERY
/// live session that lacks it, never just the newest.**
///
/// `digest_of` extracts the target digest from a `Row` -- kept as a
/// closure rather than a trait bound so this stays usable with each
/// service's own row type without a shared trait neither otherwise needs.
///
/// Sessions no longer in `live_sessions` are never targeted for `Unload`
/// (their connection is already gone) -- the caller is expected to have
/// already called [`SessionLoaded::on_session_removed`] for any session
/// that dropped out of the live set, in the same tick, before calling this
/// function.
pub fn plan_sessions<K, Row>(
    loaded: &SessionLoaded<K>,
    active: &HashMap<K, Row>,
    live_sessions: &[SessionId],
    digest_of: impl Fn(&Row) -> &str,
) -> SessionSyncPlan<K, Row>
where
    K: Eq + Hash + Clone,
    Row: Clone,
{
    let mut plan = SessionSyncPlan::default();

    for (key, row) in active {
        let want_digest = digest_of(row);
        for &session in live_sessions {
            match loaded.digest_for(session, key) {
                Some(have) if have == want_digest => {}
                _ => plan.to_load.push((session, key.clone(), row.clone())),
            }
        }
    }

    let live: HashSet<SessionId> = live_sessions.iter().copied().collect();
    for (session, key, digest) in loaded.iter() {
        if live.contains(&session) && !active.contains_key(key) {
            plan.to_unload
                .push((session, key.clone(), digest.to_string()));
        }
    }

    plan
}

#[cfg(test)]
mod tests {
    use super::*;

    type Scope = (i32, i32, String);

    fn scope(app_id: &str) -> Scope {
        (1, 0, app_id.to_string())
    }

    #[derive(Debug, Clone, PartialEq, Eq)]
    struct Row {
        digest: String,
    }

    fn row(digest: &str) -> Row {
        Row {
            digest: digest.to_string(),
        }
    }

    fn digest_of(r: &Row) -> &str {
        &r.digest
    }

    /// Two live sessions, neither has anything loaded -- both get `Load`
    /// for every active bundle, not just the newest.
    #[test]
    fn plan_sessions_loads_every_live_session_lacking_an_active_bundle() {
        let loaded = SessionLoaded::new();
        let mut active = HashMap::new();
        active.insert(scope("ping"), row("sha256:p"));
        active.insert(scope("csping"), row("sha256:c"));

        let plan = plan_sessions(&loaded, &active, &[1, 2], digest_of);
        assert_eq!(
            plan.to_load.len(),
            4,
            "2 bundles x 2 sessions, got {plan:?}"
        );
        for session in [1u64, 2u64] {
            for app in ["ping", "csping"] {
                assert!(
                    plan.to_load
                        .iter()
                        .any(|(s, k, _)| *s == session && k == &scope(app)),
                    "session {session} missing load for {app}"
                );
            }
        }
        assert!(plan.to_unload.is_empty());
    }

    /// A session that already has the exact active digest loaded is left
    /// alone -- no redundant `Load`.
    #[test]
    fn plan_sessions_skips_a_session_already_holding_the_active_digest() {
        let mut loaded = SessionLoaded::new();
        loaded.mark_loaded(1, scope("ping"), "sha256:p".to_string());
        let mut active = HashMap::new();
        active.insert(scope("ping"), row("sha256:p"));

        let plan = plan_sessions(&loaded, &active, &[1], digest_of);
        assert!(plan.is_empty());
    }

    /// A session holding a STALE digest for an active scope gets a `Load`
    /// for the new digest (never a separate `Unload` first -- the
    /// executor's own `on_load` overwrites, same contract as the
    /// single-session `diff::plan`).
    #[test]
    fn plan_sessions_reloads_a_session_with_a_stale_digest() {
        let mut loaded = SessionLoaded::new();
        loaded.mark_loaded(1, scope("ping"), "sha256:old".to_string());
        let mut active = HashMap::new();
        active.insert(scope("ping"), row("sha256:new"));

        let plan = plan_sessions(&loaded, &active, &[1], digest_of);
        assert_eq!(plan.to_load, vec![(1, scope("ping"), row("sha256:new"))]);
        assert!(plan.to_unload.is_empty());
    }

    /// A live session holding a scope no longer in the active set gets an
    /// `Unload` for exactly that session.
    #[test]
    fn plan_sessions_unloads_a_live_session_for_a_scope_no_longer_active() {
        let mut loaded = SessionLoaded::new();
        loaded.mark_loaded(1, scope("gone"), "sha256:g".to_string());
        let active: HashMap<Scope, Row> = HashMap::new();

        let plan = plan_sessions(&loaded, &active, &[1], digest_of);
        assert_eq!(
            plan.to_unload,
            vec![(1, scope("gone"), "sha256:g".to_string())]
        );
        assert!(plan.to_load.is_empty());
    }

    /// A session that dropped out of `live_sessions` is never targeted for
    /// `Unload` -- its connection is already gone; the caller is expected
    /// to have dropped its state via `on_session_removed` already.
    #[test]
    fn plan_sessions_never_targets_a_non_live_session_for_unload() {
        let mut loaded = SessionLoaded::new();
        loaded.mark_loaded(1, scope("gone"), "sha256:g".to_string());
        let active: HashMap<Scope, Row> = HashMap::new();

        // Session 1 still has tracked state but is no longer live.
        let plan = plan_sessions(&loaded, &active, &[], digest_of);
        assert!(
            plan.to_unload.is_empty(),
            "a dead session must never be sent an Unload, got {plan:?}"
        );
    }

    /// `on_session_removed` drops exactly that session's state and returns
    /// what it had loaded, leaving every other session untouched.
    #[test]
    fn on_session_removed_drops_only_that_session() {
        let mut loaded = SessionLoaded::new();
        loaded.mark_loaded(1, scope("ping"), "sha256:p".to_string());
        loaded.mark_loaded(2, scope("ping"), "sha256:p".to_string());

        let removed = loaded.on_session_removed(1);
        assert_eq!(removed.get(&scope("ping")), Some(&"sha256:p".to_string()));
        assert_eq!(loaded.sessions_with(&scope("ping")), vec![2]);
    }

    /// `loaded_count` is the fail-closed "is this bundle loaded on zero
    /// live sessions" detector callers key their ERROR log/retry on.
    #[test]
    fn loaded_count_reflects_how_many_sessions_hold_a_scope() {
        let mut loaded = SessionLoaded::new();
        assert_eq!(loaded.loaded_count(&scope("ping")), 0);
        loaded.mark_loaded(1, scope("ping"), "sha256:p".to_string());
        assert_eq!(loaded.loaded_count(&scope("ping")), 1);
        loaded.mark_loaded(2, scope("ping"), "sha256:p".to_string());
        assert_eq!(loaded.loaded_count(&scope("ping")), 2);
        loaded.on_session_removed(1);
        assert_eq!(loaded.loaded_count(&scope("ping")), 1);
    }

    /// `pick_session_with_digest` prefers the NEWEST (highest id) session
    /// among those actually holding the exact target digest -- never a
    /// session that is merely "newest overall" but lacks the digest, which
    /// is the alpha 2026-10-03 bug (invoke routed to a session with zero
    /// bundles loaded because it was the only connection known at all).
    #[test]
    fn pick_session_with_digest_prefers_newest_among_sessions_holding_the_digest() {
        let mut loaded = SessionLoaded::new();
        loaded.mark_loaded(1, scope("ping"), "sha256:p".to_string());
        loaded.mark_loaded(3, scope("ping"), "sha256:p".to_string());
        // Session 5 is newer overall but holds a DIFFERENT digest -- must
        // never be picked for a dispatch targeting "sha256:p".
        loaded.mark_loaded(5, scope("ping"), "sha256:other".to_string());

        assert_eq!(
            pick_session_with_digest(&loaded, &scope("ping"), "sha256:p"),
            Some(3)
        );
    }

    /// No live session holds the target digest at all -- `None`, never a
    /// fallback guess. Callers dead-letter + ERROR `NO_LOADED_EXECUTOR` on
    /// this, they never invoke.
    #[test]
    fn pick_session_with_digest_returns_none_when_nothing_holds_it() {
        let loaded = SessionLoaded::<Scope>::new();
        assert_eq!(
            pick_session_with_digest(&loaded, &scope("ping"), "sha256:p"),
            None
        );
    }

    /// **The exact alpha 2026-10-03 sequence:** the OLD (terminating) pod's
    /// executor session (id 2) connects AFTER the new live pod's session
    /// (id 1) and is briefly "newest"; a tick's full sync must still load
    /// every active bundle onto BOTH live sessions (never only the
    /// newest). Session 2 then dies mid-tick (simulated: its own loads are
    /// recorded as having happened, then it's removed) -- the survivor
    /// (session 1) must end up holding every active bundle on the very
    /// next sync, with zero manual intervention.
    #[test]
    fn alpha_2026_10_03_sequence_ends_with_the_live_executor_holding_everything() {
        let active_apps = ["ping", "csping", "pyping"];
        let mut active = HashMap::new();
        for app in active_apps {
            active.insert(scope(app), row(&format!("sha256:{app}")));
        }

        let mut loaded = SessionLoaded::new();
        // Session 1 (new, live pod) connects first.
        let live_after_session1: Vec<SessionId> = vec![1];
        let plan1 = plan_sessions(&loaded, &active, &live_after_session1, digest_of);
        for app in active_apps {
            loaded.mark_loaded(
                1,
                scope(app),
                plan1
                    .to_load
                    .iter()
                    .find(|(_, k, _)| k == &scope(app))
                    .unwrap()
                    .2
                    .digest
                    .clone(),
            );
        }

        // Session 2 (OLD, terminating pod) connects moments later -- both
        // sessions are now live; a full sync must target session 2 too
        // (session 1 is already satisfied and skipped).
        let live_both: Vec<SessionId> = vec![1, 2];
        let plan2 = plan_sessions(&loaded, &active, &live_both, digest_of);
        assert_eq!(
            plan2.to_load.len(),
            active_apps.len(),
            "only session 2 needs loading, session 1 already satisfied: {plan2:?}"
        );
        assert!(plan2.to_load.iter().all(|(s, _, _)| *s == 2));

        // Session 2 only finishes ping + csping before it dies mid-pyping
        // compile (connection reset) -- pyping's load for session 2 never
        // completes, so it is never marked loaded there.
        loaded.mark_loaded(2, scope("ping"), "sha256:ping".to_string());
        loaded.mark_loaded(2, scope("csping"), "sha256:csping".to_string());

        // Session 2 is removed (closed) -- drop its state entirely.
        loaded.on_session_removed(2);
        assert!(loaded.sessions_with(&scope("ping")).iter().all(|s| *s == 1));

        // The next tick's live set is just session 1 -- plan_sessions must
        // show session 1 already fully satisfied (it got everything on the
        // very first sync), proving the live executor never actually lost
        // anything despite session 2's churn.
        let plan3 = plan_sessions(&loaded, &active, &[1], digest_of);
        assert!(
            plan3.is_empty(),
            "the live session already holds every active bundle: {plan3:?}"
        );
        for app in active_apps {
            assert_eq!(
                loaded.loaded_count(&scope(app)),
                1,
                "{app} must be loaded on exactly the live session"
            );
            assert_eq!(
                pick_session_with_digest(&loaded, &scope(app), &format!("sha256:{app}")),
                Some(1)
            );
        }
    }
}
