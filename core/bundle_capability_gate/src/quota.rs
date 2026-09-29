//! `QuotaLedger`: the gate-level numeric quota checks `authorize()` runs for
//! the [`crate::permission::Quota`] shapes this crate itself enforces
//! (rate-limited call counts and `reputation.*.write`'s daily aggregate
//! caps, spec SS1/SS7.3) -- byte/row/object-count ceilings remain the
//! capability-specific implementation's job (spec SS5.3 "post-authorize").
//!
//! **This ledger deliberately stops at per-call/per-user/per-scope
//! (community or tenant) caps.** The *global* per-bundle and per-publisher
//! caps plus the distribution/entropy anomaly auto-suspend threshold (spec
//! SS7.3, Gemini condition 4) aggregate a single app's -- or a single
//! publisher's multiple apps' -- reputation writes across *every*
//! community/tenant it's activated in platform-wide, which no single
//! `authorize()` call's `InvokeScope` can see or bound. Those controls are a
//! hub-api background job (spec SS12 Phase 10), reading the same
//! `bundle_reputation_adjustments` audit ledger this crate's callers write
//! to -- not a responsibility of [`InMemoryQuotaLedger`] or any future
//! `QuotaLedger` implementation in this crate.

use std::collections::HashMap;
use std::sync::{Mutex, RwLock};
use std::time::{Duration, Instant};

use crate::permission::Quota;
use crate::scope::GrantScopeKey;

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum QuotaDenial {
    RateLimited,
    QuotaExceeded,
}

/// A rolling window of timestamped amounts, trimmed to `window` on every
/// check -- backs both the simple rate-limit shape (`amount == 1` per call)
/// and the cumulative-magnitude shape (`amount == |delta|`).
struct Window {
    window: Duration,
    entries: Vec<(Instant, i64)>,
}

impl Window {
    fn new(window: Duration) -> Self {
        Self {
            window,
            entries: Vec::new(),
        }
    }

    fn trim(&mut self, now: Instant) {
        let window = self.window;
        self.entries
            .retain(|(t, _)| now.duration_since(*t) < window);
    }

    fn sum(&self) -> i64 {
        self.entries.iter().map(|(_, amount)| amount.abs()).sum()
    }

    fn record(&mut self, now: Instant, amount: i64) {
        self.entries.push((now, amount));
    }
}

/// Checks and, on success, consumes one unit of a permission's quota for a
/// given scope. Sync (spec SS5.5's performance budget applies to this path
/// too -- no I/O, in-memory only).
pub trait QuotaLedger: Send + Sync {
    /// `amount` is `1` for a plain rate-limited call
    /// ([`Quota::CallsPerWindow`]) or the requested `|delta|` for a
    /// [`Quota::ReputationDelta`] aggregate check. Returns `Ok(())` and
    /// records the consumption, or `Err` and records nothing (a denied call
    /// never counts against the quota it was denied by).
    fn check_and_consume(
        &self,
        key: &GrantScopeKey,
        canonical_permission_id: &str,
        quota: &Quota,
        amount: i64,
    ) -> Result<(), QuotaDenial>;

    /// The per-target-user aggregate half of [`Quota::ReputationDelta`]
    /// (spec SS7.3's "per-user cap... plus a per-app-per-user-per-day
    /// aggregate cap") -- keyed additionally by the target user, since the
    /// scope-level check above only bounds the community/tenant aggregate.
    fn check_and_consume_per_user(
        &self,
        key: &GrantScopeKey,
        canonical_permission_id: &str,
        target_user: uuid::Uuid,
        max_abs_total: i64,
        window: Duration,
        amount: i64,
    ) -> Result<(), QuotaDenial>;
}

/// A single-process, in-memory [`QuotaLedger`] -- sufficient for gate-level
/// tests and as a reference implementation; a production deployment may
/// replace this with the same `UsageBatcher` primitive `relay`/`moderation`
/// already use (spec SS7.2 step 3), which this trait boundary allows without
/// changing `authorize()`'s callers.
#[derive(Default)]
pub struct InMemoryQuotaLedger {
    scope_windows: Mutex<HashMap<(GrantScopeKey, String), Window>>,
    user_windows: RwLock<HashMap<(GrantScopeKey, String, uuid::Uuid), Mutex<Window>>>,
}

impl InMemoryQuotaLedger {
    pub fn new() -> Self {
        Self::default()
    }
}

impl QuotaLedger for InMemoryQuotaLedger {
    fn check_and_consume(
        &self,
        key: &GrantScopeKey,
        canonical_permission_id: &str,
        quota: &Quota,
        amount: i64,
    ) -> Result<(), QuotaDenial> {
        let (window_duration, max_total, denial) = match quota {
            Quota::Unlimited | Quota::Descriptive(_) => return Ok(()),
            Quota::CallsPerWindow { max_calls, window } => {
                (*window, i64::from(*max_calls), QuotaDenial::RateLimited)
            }
            Quota::ReputationDelta {
                per_scope_daily_abs_max,
                ..
            } => (
                Duration::from_secs(24 * 60 * 60),
                *per_scope_daily_abs_max,
                QuotaDenial::QuotaExceeded,
            ),
        };

        let mut windows = self.scope_windows.lock().expect("lock poisoned");
        let entry = windows
            .entry((key.clone(), canonical_permission_id.to_string()))
            .or_insert_with(|| Window::new(window_duration));
        let now = Instant::now();
        entry.trim(now);
        if entry.sum() + amount.abs() > max_total {
            return Err(denial);
        }
        entry.record(now, amount);
        Ok(())
    }

    fn check_and_consume_per_user(
        &self,
        key: &GrantScopeKey,
        canonical_permission_id: &str,
        target_user: uuid::Uuid,
        max_abs_total: i64,
        window: Duration,
        amount: i64,
    ) -> Result<(), QuotaDenial> {
        let map_key = (
            key.clone(),
            canonical_permission_id.to_string(),
            target_user,
        );
        // Two-step: take a read lock to find/clone the per-user mutex handle
        // out of the map, then lock only that entry -- avoids holding the
        // outer map lock across the whole check (a different target_user
        // under the same permission never contends).
        {
            let existing = self.user_windows.read().expect("lock poisoned");
            if let Some(entry) = existing.get(&map_key) {
                return Self::check_window(entry, window, max_abs_total, amount);
            }
        }
        let mut writable = self.user_windows.write().expect("lock poisoned");
        let entry = writable
            .entry(map_key)
            .or_insert_with(|| Mutex::new(Window::new(window)));
        Self::check_window(entry, window, max_abs_total, amount)
    }
}

impl InMemoryQuotaLedger {
    fn check_window(
        entry: &Mutex<Window>,
        window: Duration,
        max_abs_total: i64,
        amount: i64,
    ) -> Result<(), QuotaDenial> {
        let mut window_state = entry.lock().expect("lock poisoned");
        window_state.window = window;
        let now = Instant::now();
        window_state.trim(now);
        if window_state.sum() + amount.abs() > max_abs_total {
            return Err(QuotaDenial::QuotaExceeded);
        }
        window_state.record(now, amount);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key() -> GrantScopeKey {
        GrantScopeKey {
            tenant_id: 7,
            community_id: 3,
            app_id: "waddles.core.example_echo".to_string(),
            app_version: 1,
        }
    }

    #[test]
    fn unlimited_quota_never_denies() {
        let ledger = InMemoryQuotaLedger::new();
        for _ in 0..1000 {
            assert!(ledger
                .check_and_consume(&key(), "flags.read", &Quota::Unlimited, 1)
                .is_ok());
        }
    }

    #[test]
    fn calls_per_window_allows_up_to_the_limit_then_rate_limits() {
        let ledger = InMemoryQuotaLedger::new();
        let quota = Quota::CallsPerWindow {
            max_calls: 3,
            window: Duration::from_secs(60),
        };
        for _ in 0..3 {
            assert!(ledger
                .check_and_consume(&key(), "overlay.media", &quota, 1)
                .is_ok());
        }
        assert_eq!(
            ledger.check_and_consume(&key(), "overlay.media", &quota, 1),
            Err(QuotaDenial::RateLimited)
        );
    }

    /// Quota-exhaustion regression: a scope-level aggregate cap (spec SS7.3's
    /// "±50/community/day aggregate") is exceeded by several calls whose
    /// individual magnitudes are each within bounds.
    #[test]
    fn reputation_delta_scope_aggregate_exhausts_across_multiple_calls() {
        let ledger = InMemoryQuotaLedger::new();
        let quota = Quota::ReputationDelta {
            per_call_abs_max: 5,
            per_user_daily_abs_max: 5,
            per_scope_daily_abs_max: 12,
        };
        assert!(ledger
            .check_and_consume(&key(), "reputation.community.write", &quota, 5)
            .is_ok());
        assert!(ledger
            .check_and_consume(&key(), "reputation.community.write", &quota, 5)
            .is_ok());
        // 5 + 5 + 5 = 15 > 12 -- the third call must be denied.
        assert_eq!(
            ledger.check_and_consume(&key(), "reputation.community.write", &quota, 5),
            Err(QuotaDenial::QuotaExceeded)
        );
    }

    #[test]
    fn a_denied_call_does_not_consume_the_quota() {
        let ledger = InMemoryQuotaLedger::new();
        let quota = Quota::CallsPerWindow {
            max_calls: 1,
            window: Duration::from_secs(60),
        };
        assert!(ledger
            .check_and_consume(&key(), "overlay.media", &quota, 1)
            .is_ok());
        assert!(ledger
            .check_and_consume(&key(), "overlay.media", &quota, 1)
            .is_err());
        // Retrying still fails -- the failed attempt above did not itself
        // get recorded as additional consumption beyond the single allowed
        // call (no double-penalty drift).
        assert!(ledger
            .check_and_consume(&key(), "overlay.media", &quota, 1)
            .is_err());
    }

    #[test]
    fn per_user_aggregate_is_independent_per_target_user() {
        let ledger = InMemoryQuotaLedger::new();
        let user_a = uuid::Uuid::new_v4();
        let user_b = uuid::Uuid::new_v4();
        let window = Duration::from_secs(60);
        assert!(ledger
            .check_and_consume_per_user(&key(), "reputation.community.write", user_a, 5, window, 5)
            .is_ok());
        // A different target_user's own cap is untouched by user_a's usage.
        assert!(ledger
            .check_and_consume_per_user(&key(), "reputation.community.write", user_b, 5, window, 5)
            .is_ok());
        assert_eq!(
            ledger.check_and_consume_per_user(
                &key(),
                "reputation.community.write",
                user_a,
                5,
                window,
                1
            ),
            Err(QuotaDenial::QuotaExceeded)
        );
    }
}
