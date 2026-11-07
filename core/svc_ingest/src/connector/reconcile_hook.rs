//! Generic bundle-exported periodic reconcile hook.
//!
//! A platform connector bundle may export a `reconcile` entry point (EventSub renewal, slash
//! command registration, ...). The bundle declares its interval in its manifest; the host owns
//! the schedule: [`ReconcileScheduler`] calls the bundle through a [`ReconcileInvoke`] at the
//! declared interval plus a 0-20% jitter, **single-flight per `(platform, community)`** (an
//! overrunning pass is skipped, never stacked), and emits a tracing span plus Prometheus
//! counter/histogram for every pass. The scheduler knows nothing about any platform.

use std::collections::HashSet;
use std::sync::{Arc, LazyLock, Mutex};
use std::time::{Duration, Instant};

use tracing::Instrument;

/// Shortest interval a manifest may declare.
pub const MIN_RECONCILE_INTERVAL: Duration = Duration::from_secs(60);
/// Longest interval a manifest may declare.
pub const MAX_RECONCILE_INTERVAL: Duration = Duration::from_secs(24 * 60 * 60);

/// A manifest-declared reconcile cadence, validated against the host bounds.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ReconcileSpec {
    interval: Duration,
}

/// The declared interval is outside [`MIN_RECONCILE_INTERVAL`]..=[`MAX_RECONCILE_INTERVAL`].
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
#[error("reconcile interval must be within 60s..=24h")]
pub struct IntervalOutOfRange;

impl ReconcileSpec {
    /// A validated cadence.
    ///
    /// # Errors
    /// [`IntervalOutOfRange`] when `interval` is outside the host bounds.
    pub fn new(interval: Duration) -> Result<Self, IntervalOutOfRange> {
        if (MIN_RECONCILE_INTERVAL..=MAX_RECONCILE_INTERVAL).contains(&interval) {
            Ok(Self { interval })
        } else {
            Err(IntervalOutOfRange)
        }
    }

    /// The un-jittered interval.
    #[must_use]
    pub fn interval(&self) -> Duration {
        self.interval
    }

    /// The interval plus a deterministic `jitter_frac` in `0.0..=0.2` of itself (clamped).
    #[must_use]
    pub fn jittered(&self, jitter_frac: f64) -> Duration {
        let f = if jitter_frac.is_finite() {
            jitter_frac.clamp(0.0, 0.2)
        } else {
            0.0
        };
        self.interval.saturating_add(self.interval.mul_f64(f))
    }
}

/// Why one reconcile pass failed. The reason is a fixed code, never content.
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
#[error("reconcile failed ({0})")]
pub struct ReconcileError(pub &'static str);

/// Calls the bundle's exported `reconcile` for one community.
#[allow(async_fn_in_trait)] // service-binary-internal, see outbound.rs::IrcOutbound
pub trait ReconcileInvoke: Send + Sync {
    /// Runs one pass for `community_id` on `platform`'s pinned connector.
    ///
    /// # Errors
    /// [`ReconcileError`] when the bundle (or the call) failed.
    async fn reconcile(&self, platform: &str, community_id: i64) -> Result<(), ReconcileError>;
}

/// What one scheduled attempt did.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReconcileRun {
    /// The pass ran and succeeded.
    Ok,
    /// The pass ran and failed.
    Failed(&'static str),
    /// Another pass for the same `(platform, community)` was still running.
    SkippedInFlight,
}

static RECONCILE_TOTAL: LazyLock<prometheus::IntCounterVec> = LazyLock::new(|| {
    // The definition is a compile-time constant; construction cannot fail.
    #[allow(clippy::expect_used)]
    prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_ingest_connector_reconcile_total",
            "Bundle reconcile passes by platform and outcome (ok, failed, skipped_in_flight)",
        ),
        &["platform", "outcome"],
    )
    .expect("valid metric definition")
});

static RECONCILE_SECONDS: LazyLock<prometheus::HistogramVec> = LazyLock::new(|| {
    // The definition is a compile-time constant; construction cannot fail.
    #[allow(clippy::expect_used)]
    prometheus::HistogramVec::new(
        prometheus::HistogramOpts::new(
            "svc_ingest_connector_reconcile_seconds",
            "Wall time of one bundle reconcile pass, by platform",
        ),
        &["platform"],
    )
    .expect("valid metric definition")
});

/// Registers the reconcile series on the service registry.
///
/// # Errors
/// Propagates a duplicate-registration error from `prometheus`.
pub fn register_metrics(registry: &prometheus::Registry) -> Result<(), prometheus::Error> {
    registry.register(Box::new(RECONCILE_TOTAL.clone()))?;
    registry.register(Box::new(RECONCILE_SECONDS.clone()))
}

/// Count of one label set (test/observability helper).
#[must_use]
pub fn reconcile_count(platform: &str, outcome: &str) -> u64 {
    RECONCILE_TOTAL
        .with_label_values(&[platform_label(platform), outcome])
        .get()
}

/// Bounded platform label (a connector slug outside the known set is `other`).
fn platform_label(p: &str) -> &'static str {
    match p {
        "twitch" => "twitch",
        "discord" => "discord",
        "youtube" => "youtube",
        "kick" => "kick",
        _ => "other",
    }
}

type InFlight = Arc<Mutex<HashSet<(String, i64)>>>;

/// Removes its key from the in-flight set on drop, so a panic or cancel still frees the slot.
struct FlightGuard {
    set: InFlight,
    key: (String, i64),
}

impl Drop for FlightGuard {
    fn drop(&mut self) {
        // Lock poisoning only means another pass panicked; the set itself is still valid.
        let mut g = self.set.lock().unwrap_or_else(|p| p.into_inner());
        g.remove(&self.key);
    }
}

/// Schedules and single-flights bundle reconcile passes.
pub struct ReconcileScheduler<I> {
    invoke: I,
    in_flight: InFlight,
}

impl<I: ReconcileInvoke> ReconcileScheduler<I> {
    /// A scheduler over `invoke`.
    #[must_use]
    pub fn new(invoke: I) -> Self {
        Self {
            invoke,
            in_flight: Arc::new(Mutex::new(HashSet::new())),
        }
    }

    /// Runs one pass unless one for the same `(platform, community_id)` is already running.
    /// Emits the span and the counter/histogram either way.
    pub async fn run_once(&self, platform: &str, community_id: i64) -> ReconcileRun {
        let label = platform_label(platform);
        let key = (platform.to_string(), community_id);
        let inserted = {
            let mut g = self.in_flight.lock().unwrap_or_else(|p| p.into_inner());
            g.insert(key.clone())
        };
        if !inserted {
            RECONCILE_TOTAL
                .with_label_values(&[label, "skipped_in_flight"])
                .inc();
            tracing::debug!(platform = label, "reconcile pass skipped: one is in flight");
            return ReconcileRun::SkippedInFlight;
        }
        let _guard = FlightGuard {
            set: Arc::clone(&self.in_flight),
            key,
        };
        let start = Instant::now();
        let span = tracing::info_span!("connector_reconcile", platform = label);
        let res = self
            .invoke
            .reconcile(platform, community_id)
            .instrument(span)
            .await;
        RECONCILE_SECONDS
            .with_label_values(&[label])
            .observe(start.elapsed().as_secs_f64());
        match res {
            Ok(()) => {
                RECONCILE_TOTAL.with_label_values(&[label, "ok"]).inc();
                ReconcileRun::Ok
            }
            Err(ReconcileError(code)) => {
                RECONCILE_TOTAL.with_label_values(&[label, "failed"]).inc();
                tracing::warn!(platform = label, code, "bundle reconcile pass failed");
                ReconcileRun::Failed(code)
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use tokio::sync::Semaphore;

    struct Fake {
        calls: Arc<AtomicUsize>,
        gate: Option<Arc<Semaphore>>,
        fail: bool,
    }

    impl ReconcileInvoke for Fake {
        async fn reconcile(&self, _p: &str, _c: i64) -> Result<(), ReconcileError> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            if let Some(g) = &self.gate {
                if let Ok(p) = g.acquire().await {
                    p.forget();
                }
            }
            if self.fail {
                Err(ReconcileError("bundle_error"))
            } else {
                Ok(())
            }
        }
    }

    fn fake(gate: Option<Arc<Semaphore>>, fail: bool) -> (Fake, Arc<AtomicUsize>) {
        let calls = Arc::new(AtomicUsize::new(0));
        (
            Fake {
                calls: Arc::clone(&calls),
                gate,
                fail,
            },
            calls,
        )
    }

    #[test]
    fn spec_bounds_and_jitter_are_enforced() {
        assert!(ReconcileSpec::new(Duration::from_secs(59)).is_err());
        assert!(ReconcileSpec::new(Duration::from_secs(24 * 3600 + 1)).is_err());
        let s = ReconcileSpec::new(Duration::from_secs(600)).unwrap();
        assert_eq!(s.interval(), Duration::from_secs(600));
        assert_eq!(s.jittered(0.0), Duration::from_secs(600));
        assert_eq!(s.jittered(0.2), Duration::from_secs(720));
        // Out-of-range or non-finite jitter is clamped, never amplified.
        assert_eq!(s.jittered(5.0), Duration::from_secs(720));
        assert_eq!(s.jittered(-1.0), Duration::from_secs(600));
        assert_eq!(s.jittered(f64::NAN), Duration::from_secs(600));
    }

    #[tokio::test]
    async fn a_pass_records_ok_and_failed_outcomes() {
        let (ok, calls) = fake(None, false);
        let s = ReconcileScheduler::new(ok);
        let before = reconcile_count("twitch", "ok");
        assert_eq!(s.run_once("twitch", 1).await, ReconcileRun::Ok);
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        assert_eq!(reconcile_count("twitch", "ok"), before + 1);

        let (bad, _) = fake(None, true);
        let s = ReconcileScheduler::new(bad);
        let before = reconcile_count("discord", "failed");
        assert_eq!(
            s.run_once("discord", 1).await,
            ReconcileRun::Failed("bundle_error")
        );
        assert_eq!(reconcile_count("discord", "failed"), before + 1);
    }

    #[tokio::test]
    async fn the_same_community_is_single_flight_but_others_run() {
        let gate = Arc::new(Semaphore::new(0));
        let (f, calls) = fake(Some(Arc::clone(&gate)), false);
        let s = Arc::new(ReconcileScheduler::new(f));
        let first = {
            let s = Arc::clone(&s);
            tokio::spawn(async move { s.run_once("twitch", 7).await })
        };
        // Wait until the first pass is inside the bundle call.
        while calls.load(Ordering::SeqCst) == 0 {
            tokio::task::yield_now().await;
        }
        let skipped_before = reconcile_count("twitch", "skipped_in_flight");
        assert_eq!(s.run_once("twitch", 7).await, ReconcileRun::SkippedInFlight);
        assert_eq!(
            reconcile_count("twitch", "skipped_in_flight"),
            skipped_before + 1
        );
        // A different community is not blocked by the first.
        let other = {
            let s = Arc::clone(&s);
            tokio::spawn(async move { s.run_once("twitch", 8).await })
        };
        while calls.load(Ordering::SeqCst) < 2 {
            tokio::task::yield_now().await;
        }
        gate.add_permits(2);
        assert_eq!(first.await.unwrap(), ReconcileRun::Ok);
        assert_eq!(other.await.unwrap(), ReconcileRun::Ok);
        // The slot is free again after the pass finished.
        let (f2, _) = fake(None, false);
        let s2 = ReconcileScheduler::new(f2);
        assert_eq!(s2.run_once("twitch", 7).await, ReconcileRun::Ok);
    }
}
