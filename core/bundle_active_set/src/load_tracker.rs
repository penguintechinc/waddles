//! In-flight `Load` tracking + exponential backoff with full jitter
//! (executor scale-out phase 1, task T2) -- shared by `core/svc_process`
//! and `core/svc_action`'s changelog consumers.
//!
//! **The bug this replaces (dal2alpha 2026-11-11):** a `Load` that did not
//! answer within the stage's 120 s call timeout was re-sent on the very next
//! tick, while the executor was still compiling the first one. With JIT
//! compiles taking up to 26 min this became a retry storm (2.8k timeouts in
//! 2 h, 1.4k duplicate scope registrations for ONE scope).
//!
//! Now every `(session, key)` Load is in exactly one state:
//!
//! ```text
//!   (none) --admit--> InFlight --ok--> (none)
//!                        |
//!                        +--err/timeout--> Backoff{until} --until passes--> admit again
//! ```
//!
//! A Load is never re-sent while `InFlight`, and a failed/timed-out one waits
//! `uniform(0, min(cap, base * 2^attempt))` (AWS "full jitter") before the
//! next attempt -- base 5 s, cap 300 s. A different target digest for the
//! same key (a new version) is admitted immediately with a fresh attempt
//! count: backoff applies to retrying the SAME load, never to an upgrade.

use std::collections::HashMap;
use std::hash::Hash;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use xxhash_rust::xxh64::xxh64;

use crate::session_sync::SessionId;

/// Backoff parameters (contract: base 5 s, cap 300 s).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LoadBackoff {
    pub base: Duration,
    pub cap: Duration,
}

impl Default for LoadBackoff {
    fn default() -> Self {
        Self {
            base: Duration::from_secs(5),
            cap: Duration::from_secs(300),
        }
    }
}

/// Full-jitter delay for the `attempt`-th consecutive failure (1-based):
/// `unit * min(cap, base * 2^(attempt-1))`, `unit` in `[0, 1)`. Out-of-range
/// `unit` is clamped so the result is always within `[0, cap]`.
pub fn full_jitter_delay(attempt: u32, policy: LoadBackoff, unit: f64) -> Duration {
    let exp = attempt.saturating_sub(1).min(32);
    let ceiling = policy
        .base
        .checked_mul(1u32 << exp.min(31))
        .unwrap_or(policy.cap)
        .min(policy.cap);
    let unit = if unit.is_finite() {
        unit.clamp(0.0, 1.0)
    } else {
        0.0
    };
    ceiling.mul_f64(unit)
}

/// A uniform-ish value in `[0, 1)` for jitter -- NOT cryptographic (it only
/// de-synchronizes retries across replicas): xxh64 over the wall clock's
/// nanos and a process-wide counter, so two calls never repeat even within
/// one clock tick.
pub fn jitter_unit() -> f64 {
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let n = COUNTER.fetch_add(1, Ordering::Relaxed);
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let mut buf = [0u8; 24];
    buf[..16].copy_from_slice(&nanos.to_le_bytes());
    buf[16..].copy_from_slice(&n.to_le_bytes());
    // Top 53 bits -> exact f64 in [0, 1).
    (xxh64(&buf, 0) >> 11) as f64 / (1u64 << 53) as f64
}

#[derive(Debug, Clone)]
enum Entry {
    InFlight {
        digest: String,
        attempts: u32,
    },
    Backoff {
        digest: String,
        attempts: u32,
        until: Instant,
    },
}

/// Why [`LoadTracker::admit`] said no -- logged at DEBUG by callers.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LoadAdmission {
    /// Send the Load now (already marked in flight).
    Send,
    /// The same Load is still in flight; do not resend.
    InFlight,
    /// The last attempt failed; retry after this much longer.
    BackingOff(Duration),
}

/// Per-`(session, key)` Load state. See the module doc.
#[derive(Debug, Clone)]
pub struct LoadTracker<K: Eq + Hash + Clone> {
    entries: HashMap<(SessionId, K), Entry>,
    policy: LoadBackoff,
}

impl<K: Eq + Hash + Clone> Default for LoadTracker<K> {
    fn default() -> Self {
        Self::new(LoadBackoff::default())
    }
}

impl<K: Eq + Hash + Clone> LoadTracker<K> {
    pub fn new(policy: LoadBackoff) -> Self {
        Self {
            entries: HashMap::new(),
            policy,
        }
    }

    /// Decides whether to send `digest` for `(session, key)` now; on
    /// [`LoadAdmission::Send`] the entry is marked in flight.
    pub fn admit(
        &mut self,
        session: SessionId,
        key: &K,
        digest: &str,
        now: Instant,
    ) -> LoadAdmission {
        let slot = (session, key.clone());
        let attempts = match self.entries.get(&slot) {
            Some(Entry::InFlight { digest: d, .. }) if d == digest => {
                return LoadAdmission::InFlight;
            }
            Some(Entry::Backoff {
                digest: d,
                attempts,
                until,
            }) if d == digest => {
                if *until > now {
                    return LoadAdmission::BackingOff(until.saturating_duration_since(now));
                }
                *attempts
            }
            // Never seen, or a different digest (new version): fresh start.
            _ => 0,
        };
        self.entries.insert(
            slot,
            Entry::InFlight {
                digest: digest.to_string(),
                attempts,
            },
        );
        LoadAdmission::Send
    }

    /// The Load for `(session, key)` was confirmed -- forget it.
    pub fn succeeded(&mut self, session: SessionId, key: &K) {
        self.entries.remove(&(session, key.clone()));
    }

    /// The Load for `(session, key)` failed or timed out: back off with full
    /// jitter (`unit` in `[0,1)`, production passes [`jitter_unit`]).
    /// Returns the chosen delay for the caller's log line.
    pub fn failed(&mut self, session: SessionId, key: &K, now: Instant, unit: f64) -> Duration {
        let slot = (session, key.clone());
        let (digest, attempts) = match self.entries.remove(&slot) {
            Some(Entry::InFlight { digest, attempts })
            | Some(Entry::Backoff {
                digest, attempts, ..
            }) => (digest, attempts.saturating_add(1)),
            None => (String::new(), 1),
        };
        let delay = full_jitter_delay(attempts, self.policy, unit);
        self.entries.insert(
            slot,
            Entry::Backoff {
                digest,
                attempts,
                until: now + delay,
            },
        );
        delay
    }

    /// Number of Loads currently in flight (backs `stage_load_inflight`).
    pub fn in_flight_count(&self) -> usize {
        self.entries
            .values()
            .filter(|e| matches!(e, Entry::InFlight { .. }))
            .count()
    }

    /// Consecutive failures recorded for `(session, key)` (0 if none).
    pub fn attempts(&self, session: SessionId, key: &K) -> u32 {
        match self.entries.get(&(session, key.clone())) {
            Some(Entry::InFlight { attempts, .. }) | Some(Entry::Backoff { attempts, .. }) => {
                *attempts
            }
            None => 0,
        }
    }

    /// A session left the registry: its in-flight Loads can never answer.
    pub fn on_session_removed(&mut self, session: SessionId) {
        self.entries.retain(|(s, _), _| *s != session);
    }

    /// Drops state for keys no longer wanted (deactivated apps), so the map
    /// stays bounded by the active set.
    pub fn retain_keys(&mut self, keep: impl Fn(&K) -> bool) {
        self.entries.retain(|(_, k), _| keep(k));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    type Key = (i32, i32, String);

    fn key(a: &str) -> Key {
        (1, 0, a.to_string())
    }

    /// No resend while in flight: a second admit for the same Load is
    /// refused until the first completes.
    #[test]
    fn no_resend_while_in_flight() {
        let mut t = LoadTracker::default();
        let now = Instant::now();
        assert_eq!(t.admit(1, &key("a"), "d1", now), LoadAdmission::Send);
        assert_eq!(t.in_flight_count(), 1);
        for _ in 0..5 {
            assert_eq!(t.admit(1, &key("a"), "d1", now), LoadAdmission::InFlight);
        }
        // Another session / key is independent.
        assert_eq!(t.admit(2, &key("a"), "d1", now), LoadAdmission::Send);
        assert_eq!(t.admit(1, &key("b"), "d1", now), LoadAdmission::Send);
        t.succeeded(1, &key("a"));
        assert_eq!(t.admit(1, &key("a"), "d1", now), LoadAdmission::Send);
    }

    /// A timed-out Load backs off; it is re-admitted only after the jittered
    /// delay, and consecutive failures grow the ceiling.
    #[test]
    fn timeout_backs_off_then_readmits() {
        let mut t = LoadTracker::default();
        let now = Instant::now();
        t.admit(1, &key("a"), "d1", now);
        let d = t.failed(1, &key("a"), now, 0.999);
        assert!(d <= Duration::from_secs(5) && d > Duration::from_secs(4));
        assert!(matches!(
            t.admit(1, &key("a"), "d1", now + Duration::from_secs(1)),
            LoadAdmission::BackingOff(_)
        ));
        assert_eq!(t.in_flight_count(), 0);
        let later = now + d;
        assert_eq!(t.admit(1, &key("a"), "d1", later), LoadAdmission::Send);
        let d2 = t.failed(1, &key("a"), later, 0.999);
        assert!(d2 > Duration::from_secs(9) && d2 <= Duration::from_secs(10));
        assert_eq!(t.attempts(1, &key("a")), 2);
    }

    /// A new digest (upgrade) is never held back by the old digest's backoff.
    #[test]
    fn new_digest_bypasses_backoff() {
        let mut t = LoadTracker::default();
        let now = Instant::now();
        t.admit(1, &key("a"), "d1", now);
        t.failed(1, &key("a"), now, 0.9);
        assert_eq!(t.admit(1, &key("a"), "d2", now), LoadAdmission::Send);
        assert_eq!(t.attempts(1, &key("a")), 0);
    }

    /// Full-jitter bounds: always within [0, min(cap, base*2^(n-1))].
    #[test]
    fn full_jitter_bounds() {
        let p = LoadBackoff::default();
        for attempt in 1..=40u32 {
            let ceiling = Duration::from_secs(5)
                .saturating_mul(1u32 << (attempt - 1).min(31))
                .min(Duration::from_secs(300));
            for unit in [0.0, 0.25, 0.5, 0.999_999, 1.0, -3.0, 7.0, f64::NAN] {
                let d = full_jitter_delay(attempt, p, unit);
                assert!(d <= ceiling, "attempt {attempt} unit {unit}: {d:?} > {ceiling:?}");
            }
        }
        assert_eq!(full_jitter_delay(1, p, 0.0), Duration::ZERO);
        assert_eq!(full_jitter_delay(1, p, 1.0), Duration::from_secs(5));
        assert_eq!(full_jitter_delay(7, p, 1.0), Duration::from_secs(300));
        assert_eq!(full_jitter_delay(1000, p, 1.0), Duration::from_secs(300));
    }

    /// jitter_unit is in [0, 1) and not constant.
    #[test]
    fn jitter_unit_is_in_range_and_varies() {
        let v: Vec<f64> = (0..1000).map(|_| jitter_unit()).collect();
        assert!(v.iter().all(|u| (0.0..1.0).contains(u)));
        let mean = v.iter().sum::<f64>() / v.len() as f64;
        assert!((0.4..0.6).contains(&mean), "mean {mean}");
    }

    #[test]
    fn session_removal_and_retain_drop_state() {
        let mut t = LoadTracker::default();
        let now = Instant::now();
        t.admit(1, &key("a"), "d", now);
        t.admit(2, &key("b"), "d", now);
        t.on_session_removed(1);
        assert_eq!(t.in_flight_count(), 1);
        t.retain_keys(|k| k != &key("b"));
        assert_eq!(t.in_flight_count(), 0);
        // failed() on an unknown entry still records a backoff.
        assert!(t.failed(3, &key("c"), now, 0.5) <= Duration::from_secs(5));
        assert_eq!(t.attempts(3, &key("c")), 1);
    }
}
