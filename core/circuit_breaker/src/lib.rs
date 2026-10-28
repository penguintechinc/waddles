//! Shared, generic per-key circuit breaker for guest faults (connector spec
//! `docs/superpowers/specs/2026-09-28-connector-bundles.md` SS0 condition 5):
//! a bundle-executor trap, epoch/fuel timeout, or OOM must disable only the
//! offending key, never crash the host process, and never affect any other
//! key's traffic.
//!
//! A path dependency within this repo (see `Cargo.toml`'s doc), used by
//! every stage service that invokes bundle-executor per some unit of
//! traffic:
//!
//! - `svc_process`: keyed by `{app_id}|{stream}` -- the stream is the
//!   closest thing it has to a source/connection identity (the
//!   dataplane-scale design keys streams by `hash(source_id)`), and the
//!   `app_id` prefix keeps one app's faults from disabling every other
//!   app's consumer on the same stream
//! - `svc_action`: keyed by a composite of `(app_id, scope, destination)` --
//!   see `svc_action::dispatch`'s own doc for why all three dimensions are
//!   folded into one key rather than three independent breakers
//! - `svc_ingest`'s connector host (once connector bundles land, same spec
//!   SS2.6): keyed by `source_id`/connection, the same shape this module was
//!   originally written against
//!
//! Each caller picks its own key granularity and composition -- this module
//! only owns the failure-counting/open/backoff/reset state machine.
//!
//! Deliberately process-local, in-memory state (not shared via Valkey/DB):
//! each pod already only owns a fixed shard of its own keys (fixed
//! 2048-partition hashing for `svc_process`'s sources, `dataplane-scale-
//! design`; a fixed app/scope/destination set for `svc_action`), so a
//! breaker tripped on one pod for a key that pod owns is the complete
//! blast-radius containment the spec asks for -- no cross-pod coordination
//! needed.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use tracing::{error, info};

/// Guest faults within this window trip the breaker open.
pub const FAILURE_THRESHOLD: u32 = 5;
/// The window failures are counted over -- older failures age out.
pub const FAILURE_WINDOW: Duration = Duration::from_secs(60);
/// How long a tripped source stays disabled before the breaker resets it
/// (task instruction 4: "the circuit breaker resets after backoff"). A timed
/// reset rather than a half-open probe: simplest policy that satisfies the
/// spec's "with backoff" requirement without risking a probe call itself
/// tripping the same fault again in a tight loop.
pub const OPEN_BACKOFF: Duration = Duration::from_secs(30);

/// Soft cap on tracked keys before [`CircuitBreaker::record_failure`] sweeps
/// out entries that carry no live state (closed with every failure aged out
/// of [`FAILURE_WINDOW`], or open with an elapsed backoff). Keeps the map
/// from growing without bound on a key space with high churn (every distinct
/// app/stream/destination that ever faulted once).
pub const SWEEP_THRESHOLD: usize = 1024;

/// Optional counters a caller can wire to Prometheus
/// (`crate::telemetry::CircuitBreakerMetrics`); `()` is a no-op impl so unit
/// tests and any call site that doesn't care about metrics can skip it.
///
/// Deliberately takes NO per-key argument: the key is an unbounded
/// identifier space (stream / app|scope|destination), and a metric label
/// drawn from it is a cardinality bomb. Per-key attribution lives in the
/// `error!` log emitted on the OPEN transition, never in a label.
pub trait CircuitBreakerMetrics: Send + Sync {
    /// `action` is exactly `"opened"` or `"closed"`.
    fn transition(&self, action: &str);
}

impl CircuitBreakerMetrics for () {
    fn transition(&self, _action: &str) {}
}

#[derive(Debug, Clone, Copy)]
enum SourceState {
    Closed,
    /// Open until this instant; a call to [`CircuitBreaker::allow`] after
    /// this elapses transitions the source back to `Closed` (task
    /// instruction 4's "resets after backoff").
    Open {
        until: Instant,
    },
}

struct SourceEntry {
    state: SourceState,
    /// Timestamps of guest faults within the current window, oldest first.
    failures: Vec<Instant>,
}

impl SourceEntry {
    fn new() -> Self {
        Self {
            state: SourceState::Closed,
            failures: Vec::new(),
        }
    }
}

/// Tracks guest-fault history per source and gates whether a source may be
/// invoked right now. One instance shared (behind an `Arc`) across every
/// `handle_delivered` call in a `svc_process` pod -- see module doc for why
/// this being process-local (not cross-pod) is the correct scope.
pub struct CircuitBreaker {
    sources: Mutex<HashMap<String, SourceEntry>>,
    metrics: Arc<dyn CircuitBreakerMetrics>,
    threshold: u32,
    window: Duration,
    backoff: Duration,
}

impl CircuitBreaker {
    pub fn new(metrics: Arc<dyn CircuitBreakerMetrics>) -> Self {
        Self::with_config(metrics, FAILURE_THRESHOLD, FAILURE_WINDOW, OPEN_BACKOFF)
    }

    /// Like [`Self::new`] but with a caller-chosen trip threshold, failure
    /// window and open backoff (the defaults suit guest-fault isolation; an
    /// external-API breaker such as svc_ingest's Discord resolver tunes
    /// them, and tests use short durations).
    pub fn with_config(
        metrics: Arc<dyn CircuitBreakerMetrics>,
        threshold: u32,
        window: Duration,
        backoff: Duration,
    ) -> Self {
        Self {
            sources: Mutex::new(HashMap::new()),
            metrics,
            threshold,
            window,
            backoff,
        }
    }

    /// Locks the per-source state map. A poisoned mutex means a prior holder
    /// panicked mid-update; the map is plain counters/timestamps with no
    /// cross-entry invariant, so we recover the guard but log loudly (once
    /// per acquisition) rather than silently swallowing the poison.
    fn lock_sources(&self) -> std::sync::MutexGuard<'_, HashMap<String, SourceEntry>> {
        match self.sources.lock() {
            Ok(guard) => guard,
            Err(poisoned) => {
                error!("circuit breaker state mutex poisoned; recovering state map");
                poisoned.into_inner()
            }
        }
    }

    #[cfg(test)]
    fn new_for_test() -> Self {
        Self::new(Arc::new(()))
    }

    /// Whether `source` may be invoked right now. An open breaker whose
    /// backoff has elapsed transitions to `Closed` here (lazily, on next
    /// check) and is allowed through; a source never seen before starts
    /// `Closed`/allowed -- other tenants' and other sources' traffic is
    /// never affected by this call (each source's state lives under its own
    /// map key, task instruction 2's isolation requirement).
    pub fn allow(&self, source: &str) -> bool {
        let mut sources = self.lock_sources();
        let elapsed = match sources.get(source) {
            None => return true,
            Some(entry) => match entry.state {
                SourceState::Closed => return true,
                SourceState::Open { until } => Instant::now() >= until,
            },
        };
        if !elapsed {
            return false;
        }
        // Backoff elapsed: a re-enabled key carries no state worth keeping
        // (its failure history is cleared on reset), so evict it -- the map
        // only ever holds keys with live state.
        sources.remove(source);
        info!(source, "circuit breaker backoff elapsed, source re-enabled");
        self.metrics.transition("closed");
        true
    }

    /// Records a successful invocation, clearing this source's failure
    /// history -- a source that is working again should not carry stale
    /// failure count toward a future trip. A closed entry with no failures
    /// is indistinguishable from an absent one, so it is evicted; an open
    /// entry is left alone (a success racing an open breaker must not
    /// silently re-enable it before its backoff elapses).
    pub fn record_success(&self, source: &str) {
        let mut sources = self.lock_sources();
        let evict = match sources.get_mut(source) {
            Some(entry) if matches!(entry.state, SourceState::Closed) => {
                entry.failures.clear();
                true
            }
            _ => false,
        };
        if evict {
            sources.remove(source);
        }
    }

    /// Drops every entry that carries no live state: closed with all
    /// failures aged out of [`FAILURE_WINDOW`], or open with an elapsed
    /// backoff. Called under the map lock from [`Self::record_failure`] once
    /// the map passes [`SWEEP_THRESHOLD`].
    fn sweep(sources: &mut HashMap<String, SourceEntry>, now: Instant, window: Duration) {
        sources.retain(|_, entry| match entry.state {
            SourceState::Closed => entry
                .failures
                .iter()
                .any(|t| now.duration_since(*t) < window),
            SourceState::Open { until } => now < until,
        });
    }

    /// Records a guest fault (trap/timeout/fuel exhaustion/OOM) for
    /// `source`. Returns `true` if this call is what tripped the breaker
    /// open (so the caller can alert exactly once per trip, not once per
    /// failure). Failures outside [`FAILURE_WINDOW`] are pruned before
    /// counting, per task instruction 4 "N failures in a window".
    ///
    /// Callers must NOT call this for back-pressure signals (e.g. the
    /// executor's `pool_exhausted`): those say nothing about the guest.
    pub fn record_failure(&self, source: &str) -> bool {
        let mut sources = self.lock_sources();
        let now = Instant::now();
        if sources.len() >= SWEEP_THRESHOLD && !sources.contains_key(source) {
            Self::sweep(&mut sources, now, self.window);
        }
        let entry = sources
            .entry(source.to_string())
            .or_insert_with(SourceEntry::new);

        entry
            .failures
            .retain(|t| now.duration_since(*t) < self.window);
        entry.failures.push(now);

        if matches!(entry.state, SourceState::Open { .. }) {
            // Already open; a fault while disabled (e.g. a caller that
            // ignored `allow`, or a race) never re-alerts.
            return false;
        }

        if entry.failures.len() as u32 >= self.threshold {
            entry.state = SourceState::Open {
                until: now + self.backoff,
            };
            error!(
                source,
                failures = entry.failures.len(),
                window_s = self.window.as_secs(),
                backoff_s = self.backoff.as_secs(),
                alert = true,
                "circuit breaker OPEN: source disabled after repeated guest faults \
                 (trap/timeout/fuel exhaustion) -- other sources and tenants unaffected"
            );
            self.metrics.transition("opened");
            return true;
        }
        false
    }

    /// Number of keys currently tracked (test/diagnostic visibility into
    /// the eviction behavior).
    pub fn tracked_keys(&self) -> usize {
        self.lock_sources().len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_source_never_seen_before_is_allowed() {
        let breaker = CircuitBreaker::new_for_test();
        assert!(breaker.allow("source-a"));
    }

    #[test]
    fn fewer_than_threshold_failures_keep_the_source_allowed() {
        let breaker = CircuitBreaker::new_for_test();
        for _ in 0..FAILURE_THRESHOLD - 1 {
            assert!(!breaker.record_failure("source-a"));
        }
        assert!(breaker.allow("source-a"));
    }

    #[test]
    fn threshold_failures_trip_the_breaker_open_exactly_once() {
        let breaker = CircuitBreaker::new_for_test();
        let mut opened_count = 0;
        for _ in 0..FAILURE_THRESHOLD {
            if breaker.record_failure("source-a") {
                opened_count += 1;
            }
        }
        assert_eq!(opened_count, 1, "must alert exactly once per trip");
        assert!(!breaker.allow("source-a"));
    }

    #[test]
    fn a_success_clears_the_failure_history() {
        let breaker = CircuitBreaker::new_for_test();
        for _ in 0..FAILURE_THRESHOLD - 1 {
            breaker.record_failure("source-a");
        }
        breaker.record_success("source-a");
        // The window was cleared, so the next failure alone must not trip it.
        assert!(!breaker.record_failure("source-a"));
        assert!(breaker.allow("source-a"));
    }

    #[test]
    fn other_sources_are_unaffected_by_one_sources_trip() {
        let breaker = CircuitBreaker::new_for_test();
        for _ in 0..FAILURE_THRESHOLD {
            breaker.record_failure("source-a");
        }
        assert!(!breaker.allow("source-a"));
        assert!(
            breaker.allow("source-b"),
            "an unrelated source must never be disabled by another source's trip"
        );
    }

    #[test]
    fn the_breaker_resets_after_the_backoff_elapses() {
        let breaker = CircuitBreaker::new_for_test();
        for _ in 0..FAILURE_THRESHOLD {
            breaker.record_failure("source-a");
        }
        assert!(!breaker.allow("source-a"));

        // Force the backoff to have already elapsed rather than sleeping
        // the real 30s in a unit test.
        {
            let mut sources = breaker.sources.lock().unwrap();
            sources.get_mut("source-a").unwrap().state = SourceState::Open {
                until: Instant::now() - Duration::from_millis(1),
            };
        }
        assert!(
            breaker.allow("source-a"),
            "the source must be allowed again once its backoff window has elapsed"
        );
    }

    #[derive(Default)]
    struct RecordingMetrics {
        transitions: Mutex<Vec<String>>,
    }

    impl CircuitBreakerMetrics for RecordingMetrics {
        fn transition(&self, action: &str) {
            self.transitions.lock().unwrap().push(action.to_string());
        }
    }

    #[test]
    fn metrics_record_the_opened_transition() {
        let metrics = Arc::new(RecordingMetrics::default());
        let breaker = CircuitBreaker::new(metrics.clone() as Arc<dyn CircuitBreakerMetrics>);
        for _ in 0..FAILURE_THRESHOLD {
            breaker.record_failure("source-a");
        }
        let transitions = metrics.transitions.lock().unwrap();
        assert_eq!(
            *transitions,
            vec!["opened".to_string()],
            "exactly one low-cardinality `opened` transition, no per-fault `failure` noise"
        );
    }

    #[test]
    fn metrics_record_closed_on_reset_and_evict_the_entry() {
        let metrics = Arc::new(RecordingMetrics::default());
        let breaker = CircuitBreaker::new(metrics.clone() as Arc<dyn CircuitBreakerMetrics>);
        for _ in 0..FAILURE_THRESHOLD {
            breaker.record_failure("source-a");
        }
        {
            let mut sources = breaker.sources.lock().unwrap();
            sources.get_mut("source-a").unwrap().state = SourceState::Open {
                until: Instant::now() - Duration::from_millis(1),
            };
        }
        assert!(breaker.allow("source-a"));
        assert_eq!(breaker.tracked_keys(), 0, "a re-enabled key is evicted");
        assert_eq!(
            *metrics.transitions.lock().unwrap(),
            vec!["opened".to_string(), "closed".to_string()]
        );
    }

    #[test]
    fn a_success_evicts_a_closed_entry_but_never_an_open_one() {
        let breaker = CircuitBreaker::new_for_test();
        breaker.record_failure("source-a");
        assert_eq!(breaker.tracked_keys(), 1);
        breaker.record_success("source-a");
        assert_eq!(breaker.tracked_keys(), 0);

        for _ in 0..FAILURE_THRESHOLD {
            breaker.record_failure("source-b");
        }
        breaker.record_success("source-b");
        assert!(
            !breaker.allow("source-b"),
            "a success must not re-enable an open breaker before its backoff"
        );
    }

    #[test]
    fn stale_closed_entries_are_swept_so_the_map_cannot_grow_unbounded() {
        let breaker = CircuitBreaker::new_for_test();
        let long_ago = Instant::now() - FAILURE_WINDOW - Duration::from_secs(1);
        {
            let mut sources = breaker.sources.lock().unwrap();
            for i in 0..SWEEP_THRESHOLD {
                let mut entry = SourceEntry::new();
                entry.failures.push(long_ago);
                sources.insert(format!("stale-{i}"), entry);
            }
        }
        assert_eq!(breaker.tracked_keys(), SWEEP_THRESHOLD);
        breaker.record_failure("fresh");
        assert_eq!(
            breaker.tracked_keys(),
            1,
            "stale entries are evicted once the map passes the sweep threshold"
        );
    }
}
