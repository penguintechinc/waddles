//! Per-source circuit breaker for guest faults (connector spec
//! `docs/superpowers/specs/2026-09-28-connector-bundles.md` SS0 condition 5):
//! a bundle-executor trap, epoch/fuel timeout, or OOM must disable only the
//! offending source (`Delivered.stream`, the closest thing svc_process has
//! to a source/connection identity -- spec's dataplane-scale design keys
//! streams by `hash(source_id)`), never crash the host process, and never
//! affect any other source or tenant's traffic.
//!
//! Deliberately process-local, in-memory state (not shared via Valkey/DB):
//! each svc_process pod already only owns a fixed shard of sources (fixed
//! 2048-partition hashing, `dataplane-scale-design`), so a breaker tripped
//! on one pod for a source that pod owns is the complete blast-radius
//! containment the spec asks for -- no cross-pod coordination needed.

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

/// Optional counters a caller can wire to Prometheus
/// (`crate::telemetry::CircuitBreakerMetrics`); `()` is a no-op impl so unit
/// tests and any call site that doesn't care about metrics can skip it.
pub trait CircuitBreakerMetrics: Send + Sync {
    fn transition(&self, source: &str, action: &str);
}

impl CircuitBreakerMetrics for () {
    fn transition(&self, _source: &str, _action: &str) {}
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
}

impl CircuitBreaker {
    pub fn new(metrics: Arc<dyn CircuitBreakerMetrics>) -> Self {
        Self {
            sources: Mutex::new(HashMap::new()),
            metrics,
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
        let mut sources = self.sources.lock().unwrap_or_else(|e| e.into_inner());
        match sources.get_mut(source) {
            None => true,
            Some(entry) => match entry.state {
                SourceState::Closed => true,
                SourceState::Open { until } if Instant::now() >= until => {
                    entry.state = SourceState::Closed;
                    entry.failures.clear();
                    info!(source, "circuit breaker backoff elapsed, source re-enabled");
                    self.metrics.transition(source, "closed");
                    true
                }
                SourceState::Open { .. } => false,
            },
        }
    }

    /// Records a successful invocation, clearing this source's failure
    /// history -- a source that is working again should not carry stale
    /// failure count toward a future trip.
    pub fn record_success(&self, source: &str) {
        let mut sources = self.sources.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(entry) = sources.get_mut(source) {
            entry.failures.clear();
        }
    }

    /// Records a guest fault (trap/timeout/fuel exhaustion/OOM) for
    /// `source`. Returns `true` if this call is what tripped the breaker
    /// open (so the caller can alert exactly once per trip, not once per
    /// failure). Failures outside [`FAILURE_WINDOW`] are pruned before
    /// counting, per task instruction 4 "N failures in a window".
    pub fn record_failure(&self, source: &str) -> bool {
        let mut sources = self.sources.lock().unwrap_or_else(|e| e.into_inner());
        let entry = sources
            .entry(source.to_string())
            .or_insert_with(SourceEntry::new);
        self.metrics.transition(source, "failure");

        let now = Instant::now();
        entry
            .failures
            .retain(|t| now.duration_since(*t) < FAILURE_WINDOW);
        entry.failures.push(now);

        if matches!(entry.state, SourceState::Open { .. }) {
            // Already open; a fault while disabled (e.g. a caller that
            // ignored `allow`, or a race) never re-alerts.
            return false;
        }

        if entry.failures.len() as u32 >= FAILURE_THRESHOLD {
            entry.state = SourceState::Open {
                until: now + OPEN_BACKOFF,
            };
            error!(
                source,
                failures = entry.failures.len(),
                window_s = FAILURE_WINDOW.as_secs(),
                backoff_s = OPEN_BACKOFF.as_secs(),
                alert = true,
                "circuit breaker OPEN: source disabled after repeated guest faults \
                 (trap/timeout/fuel exhaustion) -- other sources and tenants unaffected"
            );
            self.metrics.transition(source, "opened");
            return true;
        }
        false
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
        transitions: Mutex<Vec<(String, String)>>,
    }

    impl CircuitBreakerMetrics for RecordingMetrics {
        fn transition(&self, source: &str, action: &str) {
            self.transitions
                .lock()
                .unwrap()
                .push((source.to_string(), action.to_string()));
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
        assert!(transitions
            .iter()
            .any(|(s, a)| s == "source-a" && a == "opened"));
    }
}
