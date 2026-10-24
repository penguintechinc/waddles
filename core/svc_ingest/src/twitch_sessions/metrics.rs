//! Metrics seam for the Twitch session manager.
//!
//! Only the trait and its int-coded enums live here; the Prometheus
//! implementation is wired separately. Every method takes counts or int codes
//! -- never a channel, login, token or any other PII.

use std::time::Duration;

use crate::twitch_sessions::manager::{ReconcileReport, SessionEnd};

/// Outcome of one lease interaction (int-coded for stable label values).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum LeaseOutcome {
    /// A new lease was taken.
    Acquired = 1,
    /// Another replica holds the lease.
    Denied = 2,
    /// Valkey could not give a trustworthy answer (fail-closed).
    Error = 3,
    /// An owned lease was extended.
    Renewed = 4,
    /// An owned lease was found lost.
    Lost = 5,
}

impl LeaseOutcome {
    /// Stable label value for this outcome.
    #[must_use]
    pub fn label(self) -> &'static str {
        match self {
            Self::Acquired => "acquired",
            Self::Denied => "denied",
            Self::Error => "error",
            Self::Renewed => "renewed",
            Self::Lost => "lost",
        }
    }
}

/// Outcome of one binding discovery.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum DiscoveryOutcome {
    /// The complete desired set was obtained.
    Ok = 1,
    /// Discovery failed; no start/stop decisions were made.
    Failed = 2,
}

impl DiscoveryOutcome {
    /// Stable label value for this outcome.
    #[must_use]
    pub fn label(self) -> &'static str {
        match self {
            Self::Ok => "ok",
            Self::Failed => "failed",
        }
    }
}

impl SessionEnd {
    /// Stable label value for this end reason.
    #[must_use]
    pub fn label(self) -> &'static str {
        match self {
            Self::Shutdown => "shutdown",
            Self::NonRetryable => "non_retryable",
            Self::LeaseLost => "lease_lost",
        }
    }
}

/// Sink for session-manager telemetry. Implementations must never block or
/// panic: a dead exporter is never a reconcile failure.
pub trait SessionMetrics: Send + Sync {
    /// A session task was spawned.
    fn session_started(&self);
    /// A session ended for `reason`.
    fn session_ended(&self, reason: SessionEnd);
    /// A lease interaction happened.
    fn lease_event(&self, outcome: LeaseOutcome);
    /// A discovery attempt finished.
    fn discovery(&self, outcome: DiscoveryOutcome);
    /// One reconcile pass finished in `elapsed`; `live` sessions are running.
    fn reconcile_done(&self, report: &ReconcileReport, elapsed: Duration, live: usize);
}
