//! Consecutive-failure circuit breaker with a single half-open probe.

use std::sync::Mutex;
use std::time::Duration;

use tokio::time::Instant;

use crate::error::ResolveError;

#[derive(Default)]
struct State {
    failures: u32,
    opened_at: Option<Instant>,
    half_open: bool,
}

/// Opens after `threshold` consecutive transient failures; after `cooldown` lets one
/// probe through (re-arming the cooldown so concurrent callers stay shed).
pub(crate) struct Breaker {
    threshold: u32,
    cooldown: Duration,
    state: Mutex<State>,
}

/// How a finished call counts toward the breaker.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(crate) enum Verdict {
    Success,
    Failure,
    /// Hub answered deterministically (auth/not-found/...): proves liveness, not health.
    Neutral,
}

impl Verdict {
    /// Classify an error: only transient failures trip the breaker.
    pub(crate) fn of_error(e: &ResolveError) -> Self {
        match e {
            ResolveError::Transient(_) => Self::Failure,
            _ => Self::Neutral,
        }
    }
}

impl Breaker {
    pub(crate) fn new(threshold: u32, cooldown: Duration) -> Self {
        Self {
            threshold: threshold.max(1),
            cooldown,
            state: Mutex::new(State::default()),
        }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Admit a call, or `CircuitOpen` while shedding.
    pub(crate) fn allow(&self) -> Result<(), ResolveError> {
        let mut s = self.lock();
        match s.opened_at {
            None => Ok(()),
            Some(at) if at.elapsed() >= self.cooldown => {
                s.half_open = true;
                s.opened_at = Some(Instant::now());
                Ok(())
            }
            Some(_) => Err(ResolveError::CircuitOpen),
        }
    }

    /// Record the outcome of an admitted call.
    pub(crate) fn record(&self, v: Verdict) {
        let mut s = self.lock();
        match v {
            Verdict::Success => *s = State::default(),
            Verdict::Neutral => {
                if s.half_open {
                    *s = State::default();
                }
            }
            Verdict::Failure => {
                if s.half_open {
                    s.opened_at = Some(Instant::now());
                    s.half_open = false;
                    return;
                }
                s.failures += 1;
                if s.failures >= self.threshold {
                    s.opened_at = Some(Instant::now());
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test(start_paused = true)]
    async fn breaker_opens_after_threshold_then_half_opens() {
        let b = Breaker::new(3, Duration::from_secs(10));
        for _ in 0..2 {
            b.allow().unwrap();
            b.record(Verdict::Failure);
        }
        b.allow().unwrap(); // still closed at 2 failures
        b.record(Verdict::Failure); // 3rd -> open
        assert_eq!(b.allow(), Err(ResolveError::CircuitOpen));
        tokio::time::advance(Duration::from_secs(9)).await;
        assert_eq!(b.allow(), Err(ResolveError::CircuitOpen));
        tokio::time::advance(Duration::from_secs(1)).await;
        b.allow().unwrap(); // half-open probe
        assert_eq!(b.allow(), Err(ResolveError::CircuitOpen)); // others shed during the probe
        b.record(Verdict::Failure); // probe fails -> reopen, full cooldown again
        assert_eq!(b.allow(), Err(ResolveError::CircuitOpen));
        tokio::time::advance(Duration::from_secs(10)).await;
        b.allow().unwrap();
        b.record(Verdict::Success); // probe ok -> closed
        b.allow().unwrap();
        b.allow().unwrap();
    }

    #[tokio::test(start_paused = true)]
    async fn neutral_never_trips_and_closes_a_half_open_probe() {
        let b = Breaker::new(2, Duration::from_secs(5));
        for _ in 0..10 {
            b.allow().unwrap();
            b.record(Verdict::Neutral);
        }
        b.record(Verdict::Failure);
        b.record(Verdict::Failure);
        assert_eq!(b.allow(), Err(ResolveError::CircuitOpen));
        tokio::time::advance(Duration::from_secs(5)).await;
        b.allow().unwrap();
        b.record(Verdict::Neutral);
        b.allow().unwrap();
    }

    #[tokio::test(start_paused = true)]
    async fn success_resets_the_failure_streak() {
        let b = Breaker::new(2, Duration::from_secs(5));
        b.record(Verdict::Failure);
        b.record(Verdict::Success);
        b.record(Verdict::Failure);
        b.allow().unwrap();
    }

    #[test]
    fn verdict_of_error_only_transient_fails() {
        assert_eq!(
            Verdict::of_error(&ResolveError::Transient("x")),
            Verdict::Failure
        );
        assert_eq!(Verdict::of_error(&ResolveError::Auth), Verdict::Neutral);
        assert_eq!(Verdict::of_error(&ResolveError::NotFound), Verdict::Neutral);
    }
}
