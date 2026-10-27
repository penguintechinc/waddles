//! A tiny sampling limiter for per-frame / per-event WARN logs. A hostile or
//! broken upstream can make the host reject thousands of frames a second
//! (oversize, platform-spoofed events, publish failures); every one is COUNTED
//! (metrics are bounded-cardinality and cheap), but only a bounded sample is
//! LOGGED, with a count of what was suppressed, so a flood cannot flood the log
//! pipeline. One limiter per category per connection: the pump is a single
//! task, so it needs no lock.

use std::time::{Duration, Instant};

/// Warnings admitted per window per category.
pub(crate) const WARN_BURST: u32 = 5;
/// The window a burst is measured over.
pub(crate) const WARN_WINDOW: Duration = Duration::from_secs(30);

/// What the limiter decided about one would-be log line.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Admit {
    /// Log it; `suppressed_before` lines were dropped since the last emitted
    /// one and should be reported on this line.
    Emit { suppressed_before: u64 },
    /// Do not log it (already counted by the caller's metric).
    Suppress,
}

/// Fixed-window burst limiter.
#[derive(Debug)]
pub(crate) struct WarnLimiter {
    window: Duration,
    burst: u32,
    window_start: Option<Instant>,
    emitted: u32,
    suppressed: u64,
}

impl WarnLimiter {
    /// A limiter admitting `burst` lines per `window`.
    pub(crate) fn new(burst: u32, window: Duration) -> Self {
        Self {
            window,
            burst,
            window_start: None,
            emitted: 0,
            suppressed: 0,
        }
    }

    /// The production default: [`WARN_BURST`] per [`WARN_WINDOW`].
    pub(crate) fn standard() -> Self {
        Self::new(WARN_BURST, WARN_WINDOW)
    }

    /// Decides at wall time.
    pub(crate) fn admit(&mut self) -> Admit {
        self.admit_at(Instant::now())
    }

    /// Decides at an explicit instant (deterministic in tests).
    pub(crate) fn admit_at(&mut self, now: Instant) -> Admit {
        let expired = match self.window_start {
            None => true,
            Some(start) => now.saturating_duration_since(start) >= self.window,
        };
        let mut carried = 0;
        if expired {
            carried = self.suppressed;
            self.window_start = Some(now);
            self.emitted = 0;
            self.suppressed = 0;
        }
        if self.emitted < self.burst {
            self.emitted += 1;
            Admit::Emit {
                suppressed_before: carried,
            }
        } else {
            self.suppressed += 1;
            Admit::Suppress
        }
    }
}

/// The per-connection limiters for each flood-prone WARN category.
#[derive(Debug)]
pub(crate) struct PumpLogs {
    pub(crate) oversize: WarnLimiter,
    pub(crate) truncated: WarnLimiter,
    pub(crate) mismatch: WarnLimiter,
    pub(crate) rejected: WarnLimiter,
    pub(crate) publish: WarnLimiter,
}

impl PumpLogs {
    /// Fresh limiters, one per category.
    pub(crate) fn new() -> Self {
        Self {
            oversize: WarnLimiter::standard(),
            truncated: WarnLimiter::standard(),
            mismatch: WarnLimiter::standard(),
            rejected: WarnLimiter::standard(),
            publish: WarnLimiter::standard(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn admits_the_burst_then_suppresses_and_reports_the_count_next_window() {
        let t0 = Instant::now();
        let mut l = WarnLimiter::new(3, Duration::from_secs(10));
        for _ in 0..3 {
            assert_eq!(
                l.admit_at(t0),
                Admit::Emit {
                    suppressed_before: 0
                }
            );
        }
        for _ in 0..100 {
            assert_eq!(l.admit_at(t0 + Duration::from_secs(1)), Admit::Suppress);
        }
        assert_eq!(
            l.admit_at(t0 + Duration::from_secs(10)),
            Admit::Emit {
                suppressed_before: 100
            }
        );
        assert_eq!(
            l.admit_at(t0 + Duration::from_secs(10)),
            Admit::Emit {
                suppressed_before: 0
            }
        );
    }
}
