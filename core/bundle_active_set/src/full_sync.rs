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
}

impl FullSyncReason {
    /// The `reason` label value for `bundle_full_sync_total`.
    pub fn as_str(self) -> &'static str {
        match self {
            FullSyncReason::Startup => "startup",
            FullSyncReason::Reconnect => "reconnect",
            FullSyncReason::Reconcile => "reconcile",
            FullSyncReason::Diverged => "diverged",
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
            digest: digest.to_string(),
            component_key: format!("bundles/{digest}/component.wasm"),
            sidecar_key: format!("bundles/{digest}/sidecar.json"),
            declared_capabilities: Vec::new(),
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
    fn digest_prefix_truncates_a_long_digest() {
        let digest = format!("sha256:{}", "a".repeat(64));
        assert_eq!(digest_prefix(&digest), "sha256:aaaaaaaaaaaa");
        assert_eq!(digest_prefix(&digest).len(), 19);
    }

    #[test]
    fn digest_prefix_returns_the_whole_string_when_shorter_than_the_prefix() {
        assert_eq!(digest_prefix("short"), "short");
    }
}
