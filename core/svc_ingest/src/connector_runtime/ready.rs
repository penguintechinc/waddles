//! Readiness reporting for the connector runtime (spec section 3).
//!
//! A platform is Ready when it is enabled, the executor is attached, the
//! pinned digest has answered at least one call and the first reconcile has
//! succeeded. `live` below `desired` for more than [`DEGRADE_AFTER`] degrades
//! it to not-ready. Every reason is a fixed label (PII-free).

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex, OnceLock, PoisonError};
use std::time::Duration;

use serde::Serialize;
use tokio::time::Instant;

use crate::connector::ExecutorLink;

/// How long `live < desired` is tolerated before the platform is not-ready.
pub const DEGRADE_AFTER: Duration = Duration::from_secs(300);

/// A platform's readiness as the runtime sees it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Readiness {
    /// The platform's overlay is not enabled; nothing to wait for.
    Disabled,
    /// Enabled but not serving; the reason is a fixed, PII-free label.
    NotReady(&'static str),
    /// Serving: `live` of `desired` connections are up.
    Ready {
        /// Connections currently supervised and live.
        live: u32,
        /// Connections in the last successful desired set.
        desired: u32,
    },
}

/// Read side of the runtime's readiness, shared with `/readyz`.
pub trait RuntimeReady: Send + Sync {
    /// Readiness of one platform.
    fn ready(&self, platform: &str) -> Readiness;
}

/// Whether the executor session is currently attached (a trait so tests and
/// alternative executors can report it without a socket).
pub trait ExecutorStatus: Send + Sync {
    /// True while a live executor session is attached.
    fn attached(&self) -> bool;
}

impl ExecutorStatus for ExecutorLink {
    fn attached(&self) -> bool {
        self.is_attached()
    }
}

impl<T: ExecutorStatus + ?Sized> ExecutorStatus for Arc<T> {
    fn attached(&self) -> bool {
        (**self).attached()
    }
}

/// One platform's `/readyz` fields.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct PlatformSnapshot {
    /// Overlay platform slug.
    pub platform: &'static str,
    /// The platform is enabled (always true for a registered platform).
    pub enabled: bool,
    /// The executor session is attached.
    pub executor_attached: bool,
    /// The pinned digest has answered at least one call.
    pub pinned_ok: bool,
    /// Connections in the last successful desired set.
    pub desired: u32,
    /// Connections currently established.
    pub live: u32,
    /// The latest reconcile's feed call failed.
    pub discovery_failed: bool,
    /// Overall platform readiness.
    pub ready: bool,
    /// Fixed reason when not ready.
    pub reason: Option<&'static str>,
}

#[derive(Default)]
struct PlatformState {
    first_reconcile_ok: bool,
    discovery_failed: bool,
    pinned_ok: bool,
    desired: u32,
    live: u32,
    below_since: Option<Instant>,
}

impl PlatformState {
    /// Tracks since when `live < desired` has held.
    fn refresh_below(&mut self) {
        if self.live < self.desired {
            self.below_since.get_or_insert_with(Instant::now);
        } else {
            self.below_since = None;
        }
    }
}

/// Shared, thread-safe status the supervisor writes and `/readyz` reads.
pub struct RuntimeStatus {
    states: BTreeMap<&'static str, Mutex<PlatformState>>,
    executor: Arc<dyn ExecutorStatus>,
}

impl RuntimeStatus {
    /// Tracks `platforms` (the enabled overlay set) against `executor`.
    pub fn new(platforms: &[&'static str], executor: Arc<dyn ExecutorStatus>) -> Self {
        Self {
            states: platforms
                .iter()
                .map(|p| (*p, Mutex::new(PlatformState::default())))
                .collect(),
            executor,
        }
    }

    fn with<T>(&self, platform: &str, f: impl FnOnce(&mut PlatformState) -> T) -> Option<T> {
        let m = self.states.get(platform)?;
        let mut g = m.lock().unwrap_or_else(PoisonError::into_inner);
        Some(f(&mut g))
    }

    /// A reconcile succeeded with `desired` connections wanted.
    pub(crate) fn reconcile_ok(&self, platform: &str, desired: u32) {
        self.with(platform, |s| {
            s.first_reconcile_ok = true;
            s.discovery_failed = false;
            s.desired = desired;
            s.refresh_below();
        });
    }

    /// A reconcile's feed call failed; the current set is kept.
    pub(crate) fn reconcile_failed(&self, platform: &str) {
        self.with(platform, |s| s.discovery_failed = true);
    }

    /// The pinned digest answered a call.
    pub(crate) fn mark_pinned(&self, platform: &str) {
        self.with(platform, |s| s.pinned_ok = true);
    }

    /// One more connection established; returns the new live count.
    pub(crate) fn live_inc(&self, platform: &str) -> u32 {
        self.with(platform, |s| {
            s.live = s.live.saturating_add(1);
            s.refresh_below();
            s.live
        })
        .unwrap_or(0)
    }

    /// One connection lost; returns the new live count.
    pub(crate) fn live_dec(&self, platform: &str) -> u32 {
        self.with(platform, |s| {
            s.live = s.live.saturating_sub(1);
            s.refresh_below();
            s.live
        })
        .unwrap_or(0)
    }

    fn evaluate(&self, platform: &'static str, s: &PlatformState) -> Readiness {
        let _ = platform;
        if !self.executor.attached() {
            return Readiness::NotReady("executor_detached");
        }
        if !s.pinned_ok {
            return Readiness::NotReady("digest_unverified");
        }
        if !s.first_reconcile_ok {
            return Readiness::NotReady("awaiting_first_reconcile");
        }
        if let Some(since) = s.below_since {
            if since.elapsed() > DEGRADE_AFTER {
                return Readiness::NotReady("live_below_desired");
            }
        }
        Readiness::Ready {
            live: s.live,
            desired: s.desired,
        }
    }

    /// The `/readyz` fields for one platform; `None` when not enabled.
    #[must_use]
    pub fn snapshot(&self, platform: &str) -> Option<PlatformSnapshot> {
        let (name, m) = self.states.get_key_value(platform)?;
        let g = m.lock().unwrap_or_else(PoisonError::into_inner);
        let r = self.evaluate(name, &g);
        let (ready, reason) = match r {
            Readiness::NotReady(why) => (false, Some(why)),
            _ => (true, None),
        };
        Some(PlatformSnapshot {
            platform: name,
            enabled: true,
            executor_attached: self.executor.attached(),
            pinned_ok: g.pinned_ok,
            desired: g.desired,
            live: g.live,
            discovery_failed: g.discovery_failed,
            ready,
            reason,
        })
    }

    /// Snapshots of every enabled platform, in platform order.
    #[must_use]
    pub fn snapshots(&self) -> Vec<PlatformSnapshot> {
        self.states
            .keys()
            .filter_map(|p| self.snapshot(p))
            .collect()
    }
}

impl RuntimeReady for RuntimeStatus {
    fn ready(&self, platform: &str) -> Readiness {
        match self.states.get_key_value(platform) {
            None => Readiness::Disabled,
            Some((name, m)) => {
                let g = m.lock().unwrap_or_else(PoisonError::into_inner);
                self.evaluate(name, &g)
            }
        }
    }
}

/// Installing the process-wide status failed because one is already set.
#[derive(Debug, thiserror::Error)]
#[error("a connector runtime status is already installed")]
pub struct AlreadyInstalled;

static INSTALLED: OnceLock<Arc<RuntimeStatus>> = OnceLock::new();

/// Publishes the runtime's status for `/readyz` (called once at boot).
pub fn install(status: Arc<RuntimeStatus>) -> Result<(), AlreadyInstalled> {
    INSTALLED.set(status).map_err(|_| AlreadyInstalled)
}

/// The installed status, if the runtime is enabled in this process.
#[must_use]
pub fn installed() -> Option<&'static Arc<RuntimeStatus>> {
    INSTALLED.get()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, Ordering};

    struct Flag(AtomicBool);
    impl ExecutorStatus for Flag {
        fn attached(&self) -> bool {
            self.0.load(Ordering::SeqCst)
        }
    }

    fn status(attached: bool) -> (RuntimeStatus, Arc<Flag>) {
        let f = Arc::new(Flag(AtomicBool::new(attached)));
        (RuntimeStatus::new(&["twitch"], f.clone()), f)
    }

    #[test]
    fn unknown_platform_is_disabled_and_has_no_snapshot() {
        let (s, _) = status(true);
        assert_eq!(s.ready("discord"), Readiness::Disabled);
        assert!(s.snapshot("discord").is_none());
    }

    #[test]
    fn readiness_requires_attach_pin_and_first_reconcile_in_order() {
        let (s, f) = status(false);
        assert_eq!(s.ready("twitch"), Readiness::NotReady("executor_detached"));
        f.0.store(true, Ordering::SeqCst);
        assert_eq!(s.ready("twitch"), Readiness::NotReady("digest_unverified"));
        s.mark_pinned("twitch");
        assert_eq!(
            s.ready("twitch"),
            Readiness::NotReady("awaiting_first_reconcile")
        );
        s.reconcile_ok("twitch", 0);
        assert_eq!(
            s.ready("twitch"),
            Readiness::Ready {
                live: 0,
                desired: 0
            }
        );
    }

    #[test]
    fn discovery_failure_is_reported_but_does_not_unready_a_ready_platform() {
        let (s, _) = status(true);
        s.mark_pinned("twitch");
        s.reconcile_ok("twitch", 1);
        s.reconcile_failed("twitch");
        let snap = s.snapshot("twitch").expect("snap");
        assert!(snap.discovery_failed && snap.ready, "{snap:?}");
        s.reconcile_ok("twitch", 1);
        assert!(!s.snapshot("twitch").expect("snap").discovery_failed);
    }

    #[tokio::test(start_paused = true)]
    async fn live_below_desired_for_five_minutes_degrades() {
        let (s, _) = status(true);
        s.mark_pinned("twitch");
        s.reconcile_ok("twitch", 2);
        s.live_inc("twitch");
        tokio::time::advance(DEGRADE_AFTER - Duration::from_secs(1)).await;
        assert!(matches!(s.ready("twitch"), Readiness::Ready { .. }));
        tokio::time::advance(Duration::from_secs(2)).await;
        assert_eq!(s.ready("twitch"), Readiness::NotReady("live_below_desired"));
        // Catching up clears it.
        s.live_inc("twitch");
        assert_eq!(
            s.ready("twitch"),
            Readiness::Ready {
                live: 2,
                desired: 2
            }
        );
    }

    #[tokio::test(start_paused = true)]
    async fn dropping_below_restarts_the_degrade_clock() {
        let (s, _) = status(true);
        s.mark_pinned("twitch");
        s.reconcile_ok("twitch", 1);
        s.live_inc("twitch");
        tokio::time::advance(DEGRADE_AFTER * 2).await;
        assert!(matches!(s.ready("twitch"), Readiness::Ready { .. }));
        assert_eq!(s.live_dec("twitch"), 0);
        tokio::time::advance(Duration::from_secs(10)).await;
        assert!(matches!(s.ready("twitch"), Readiness::Ready { .. }));
        assert_eq!(s.live_dec("twitch"), 0, "saturates at zero");
        assert_eq!(s.live_inc("unknown"), 0);
        assert_eq!(s.live_dec("unknown"), 0);
    }

    #[test]
    fn snapshot_serializes_the_documented_fields() {
        let (s, _) = status(true);
        let v = serde_json::to_value(s.snapshot("twitch").expect("snap")).expect("json");
        for k in [
            "enabled",
            "executor_attached",
            "pinned_ok",
            "desired",
            "live",
            "discovery_failed",
        ] {
            assert!(v.get(k).is_some(), "missing {k}");
        }
        assert_eq!(v["ready"], false);
        assert_eq!(s.snapshots().len(), 1);
    }

    #[test]
    fn arc_executor_status_delegates() {
        let f: Arc<dyn ExecutorStatus> = Arc::new(Flag(AtomicBool::new(true)));
        assert!(ExecutorStatus::attached(&f));
        assert!(ExecutorStatus::attached(&ExecutorLink::new()) == false);
    }
}
