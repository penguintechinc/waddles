//! Retry/backoff decisions (spec §4.3), matching `core/svc_action/
//! runner.py`'s `retry_with_backoff` behavior for parity: the runner owns
//! all backoff timing, a bundle never sleeps; `transport-error.retryable =
//! true` retries, `false` records a terminal failure.
//!
//! Outcome vocabulary mirrors the Python runner's exactly (`_record`'s
//! `status` values) so `action_dispatch_log` rows read identically
//! regardless of which stage-runner wrote them: `"success"`,
//! `"non_retryable_failure"`, `"retryable_failure"` (retries exhausted).

use std::sync::atomic::{AtomicU64, Ordering};

/// One dispatch attempt's outcome, before retry/backoff policy is applied.
#[derive(Debug, Clone, PartialEq)]
pub enum AttemptOutcome {
    /// The sender succeeded. `target_type` mirrors the Python runner's
    /// `result.transport` (e.g. `"irc_relay"`); `detail` is a short,
    /// human-readable status string (never PII/secrets, spec §6.10's own
    /// migration comment on `action_dispatch_log.detail`).
    Success {
        target_type: String,
        http_status: Option<i32>,
        detail: String,
    },
    /// A transient failure the caller may retry.
    Retryable {
        http_status: Option<i32>,
        detail: String,
        /// A sender-supplied override (e.g. an HTTP `Retry-After` header,
        /// or a bundle-returned `retry-after-ms`) that takes precedence
        /// over the computed backoff when larger (spec §4.3), capped at
        /// `ACTION_MAX_BACKOFF_MS`.
        retry_after_ms: Option<u64>,
    },
    /// A terminal failure -- never retried regardless of remaining budget.
    NonRetryable {
        http_status: Option<i32>,
        detail: String,
    },
}

/// The final, terminal record after retry policy has run out (or a
/// non-retryable failure occurred immediately) -- what
/// `crate::dispatch`/`crate::db::entities::action_dispatch_log` writes.
#[derive(Debug, Clone, PartialEq)]
pub struct DispatchRecord {
    pub target_type: String,
    /// One of `"success"`, `"non_retryable_failure"`, `"retryable_failure"`
    /// -- exact string parity with the Python runner's `_record` calls.
    pub status: &'static str,
    pub attempt: u32,
    pub http_status: Option<i32>,
    pub detail: String,
}

/// A tiny, dependency-free xorshift64* PRNG for full-jitter backoff (spec
/// §4.3: "full jitter"). Cryptographic randomness is not required here --
/// jitter only needs to avoid thundering-herd retries, not resist an
/// adversary -- so this avoids adding a `rand` dependency to a crate that
/// otherwise pins tightly. Seeded from the monotonic clock by default;
/// injectable for deterministic tests.
pub struct Jitter {
    state: AtomicU64,
}

impl Jitter {
    /// Seeds from a mix of wall-clock nanos and this process's `std::process::id()`,
    /// so concurrent instances (multiple dispatch-loop tasks) don't share a
    /// seed. Never zero (xorshift's one fixed point) -- falls back to a
    /// fixed odd constant if the clock read is exactly zero.
    pub fn from_entropy() -> Self {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos() as u64)
            .unwrap_or(0);
        let seed = nanos ^ (u64::from(std::process::id()) << 32) ^ 0x9E37_79B9_7F4A_7C15;
        Self::seeded(if seed == 0 {
            0xDEAD_BEEF_CAFE_F00D
        } else {
            seed
        })
    }

    /// Deterministic constructor for tests.
    pub fn seeded(seed: u64) -> Self {
        Self {
            state: AtomicU64::new(if seed == 0 { 1 } else { seed }),
        }
    }

    /// Returns the next pseudo-random `u64` and advances the internal
    /// state (xorshift64*).
    pub fn next_u64(&self) -> u64 {
        let mut x = self.state.load(Ordering::Relaxed);
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.state.store(x, Ordering::Relaxed);
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }

    /// A pseudo-random value in `0..=max` inclusive (`max = 0` always
    /// returns `0`).
    pub fn uniform(&self, max: u64) -> u64 {
        if max == 0 {
            0
        } else {
            self.next_u64() % (max + 1)
        }
    }
}

/// Computes the full-jitter exponential backoff delay for `attempt`
/// (0-indexed: the delay before the *second* attempt uses `attempt = 0`),
/// per spec §4.3: `min(max_ms, base_ms * 2^attempt)`, then a uniform
/// random draw in `0..=that`. A `retry_after_ms` override (from a
/// `Retry-After` header or a bundle's `retry-after-ms`) replaces the
/// computed ceiling when larger, capped at `max_ms`.
pub fn backoff_delay_ms(
    jitter: &Jitter,
    attempt: u32,
    base_ms: u64,
    max_ms: u64,
    retry_after_ms: Option<u64>,
) -> u64 {
    let exponential = base_ms.saturating_mul(1u64 << attempt.min(32));
    let mut ceiling = exponential.min(max_ms);
    if let Some(requested) = retry_after_ms {
        ceiling = ceiling.max(requested.min(max_ms));
    }
    jitter.uniform(ceiling)
}

/// Runs one logical dispatch (a caller-supplied `attempt` closure) with
/// retry-with-backoff, up to `max_retries` additional attempts after the
/// first (so `max_retries = 3` means up to 4 total attempts, matching
/// `flask_core.circuit_breaker.retry_with_backoff`'s own semantics of
/// "retries" being attempts beyond the first). `sleep` is injected so tests
/// run instantly under `tokio::time::pause`/`advance` rather than a real
/// wall-clock wait.
pub async fn dispatch_with_retry<F, Fut, S, SFut>(
    mut attempt_fn: F,
    max_retries: u32,
    base_backoff_ms: u64,
    max_backoff_ms: u64,
    jitter: &Jitter,
    mut sleep: S,
) -> (DispatchRecord, u32)
where
    F: FnMut(u32) -> Fut,
    Fut: std::future::Future<Output = AttemptOutcome>,
    S: FnMut(std::time::Duration) -> SFut,
    SFut: std::future::Future<Output = ()>,
{
    let mut attempt_count: u32 = 0;
    loop {
        attempt_count += 1;
        match attempt_fn(attempt_count).await {
            AttemptOutcome::Success {
                target_type,
                http_status,
                detail,
            } => {
                return (
                    DispatchRecord {
                        target_type,
                        status: "success",
                        attempt: attempt_count,
                        http_status,
                        detail,
                    },
                    attempt_count,
                )
            }
            AttemptOutcome::NonRetryable {
                http_status,
                detail,
            } => {
                return (
                    DispatchRecord {
                        target_type: "bundle".to_string(),
                        status: "non_retryable_failure",
                        attempt: attempt_count,
                        http_status,
                        detail,
                    },
                    attempt_count,
                )
            }
            AttemptOutcome::Retryable {
                http_status,
                detail,
                retry_after_ms,
            } => {
                let retries_so_far = attempt_count - 1;
                if retries_so_far >= max_retries {
                    return (
                        DispatchRecord {
                            target_type: "bundle".to_string(),
                            status: "retryable_failure",
                            attempt: attempt_count,
                            http_status,
                            detail,
                        },
                        attempt_count,
                    );
                }
                let delay_ms = backoff_delay_ms(
                    jitter,
                    retries_so_far,
                    base_backoff_ms,
                    max_backoff_ms,
                    retry_after_ms,
                );
                sleep(std::time::Duration::from_millis(delay_ms)).await;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn jitter_uniform_zero_max_is_always_zero() {
        let j = Jitter::seeded(42);
        for _ in 0..10 {
            assert_eq!(j.uniform(0), 0);
        }
    }

    #[test]
    fn jitter_uniform_stays_within_bounds() {
        let j = Jitter::seeded(1234);
        for _ in 0..1000 {
            assert!(j.uniform(100) <= 100);
        }
    }

    #[test]
    fn jitter_from_entropy_never_panics_and_produces_values() {
        let j = Jitter::from_entropy();
        let _ = j.next_u64();
        assert!(j.uniform(50) <= 50);
    }

    #[test]
    fn backoff_delay_never_exceeds_max_ms() {
        let j = Jitter::seeded(7);
        for attempt in 0..10 {
            let delay = backoff_delay_ms(&j, attempt, 250, 8000, None);
            assert!(delay <= 8000);
        }
    }

    #[test]
    fn backoff_delay_grows_with_attempt_ceiling() {
        // Deterministic seed -- assert the *ceiling* grows (exponential),
        // not the exact jittered value, by drawing the max over many
        // samples per attempt and comparing.
        let j = Jitter::seeded(99);
        let max_at = |attempt: u32| -> u64 {
            (0..200)
                .map(|_| backoff_delay_ms(&j, attempt, 250, 8000, None))
                .max()
                .unwrap()
        };
        assert!(max_at(0) <= max_at(3));
        assert!(max_at(3) <= 8000);
    }

    #[test]
    fn retry_after_ms_override_raises_the_ceiling_when_larger() {
        let j = Jitter::seeded(5);
        // base*2^0 = 250, but retry_after_ms of 5000 should raise the
        // ceiling -- draw many samples and confirm we see values above 250.
        let saw_above_base = (0..200)
            .map(|_| backoff_delay_ms(&j, 0, 250, 8000, Some(5000)))
            .any(|d| d > 250);
        assert!(saw_above_base);
    }

    #[test]
    fn retry_after_ms_is_capped_at_max_backoff_ms() {
        let j = Jitter::seeded(5);
        for _ in 0..200 {
            let delay = backoff_delay_ms(&j, 0, 250, 8000, Some(1_000_000));
            assert!(delay <= 8000);
        }
    }

    async fn no_sleep(_d: std::time::Duration) {}

    #[tokio::test]
    async fn success_on_first_attempt_records_success_with_attempt_1() {
        let jitter = Jitter::seeded(1);
        let (record, attempts) = dispatch_with_retry(
            |_attempt| async {
                AttemptOutcome::Success {
                    target_type: "irc_relay".to_string(),
                    http_status: None,
                    detail: "ok".to_string(),
                }
            },
            3,
            250,
            8000,
            &jitter,
            no_sleep,
        )
        .await;
        assert_eq!(attempts, 1);
        assert_eq!(record.status, "success");
        assert_eq!(record.attempt, 1);
    }

    #[tokio::test]
    async fn non_retryable_failure_stops_immediately() {
        let jitter = Jitter::seeded(1);
        let (record, attempts) = dispatch_with_retry(
            |_attempt| async {
                AttemptOutcome::NonRetryable {
                    http_status: Some(400),
                    detail: "bad request".to_string(),
                }
            },
            3,
            250,
            8000,
            &jitter,
            no_sleep,
        )
        .await;
        assert_eq!(attempts, 1);
        assert_eq!(record.status, "non_retryable_failure");
        assert_eq!(record.http_status, Some(400));
    }

    #[tokio::test]
    async fn retryable_failure_retries_up_to_max_then_records_retryable_failure() {
        let jitter = Jitter::seeded(1);
        let call_count = std::sync::atomic::AtomicU32::new(0);
        let (record, attempts) = dispatch_with_retry(
            |_attempt| {
                call_count.fetch_add(1, Ordering::Relaxed);
                async {
                    AttemptOutcome::Retryable {
                        http_status: Some(503),
                        detail: "unavailable".to_string(),
                        retry_after_ms: None,
                    }
                }
            },
            3,
            1,
            2,
            &jitter,
            no_sleep,
        )
        .await;
        // max_retries=3 -> 1 initial + 3 retries = 4 total attempts.
        assert_eq!(attempts, 4);
        assert_eq!(call_count.load(Ordering::Relaxed), 4);
        assert_eq!(record.status, "retryable_failure");
        assert_eq!(record.attempt, 4);
    }

    #[tokio::test]
    async fn succeeds_after_two_retryable_failures() {
        let jitter = Jitter::seeded(1);
        let call_count = std::sync::atomic::AtomicU32::new(0);
        let (record, attempts) = dispatch_with_retry(
            |_attempt| {
                let n = call_count.fetch_add(1, Ordering::Relaxed);
                async move {
                    if n < 2 {
                        AttemptOutcome::Retryable {
                            http_status: None,
                            detail: "transient".to_string(),
                            retry_after_ms: None,
                        }
                    } else {
                        AttemptOutcome::Success {
                            target_type: "irc_relay".to_string(),
                            http_status: None,
                            detail: "ok".to_string(),
                        }
                    }
                }
            },
            5,
            1,
            2,
            &jitter,
            no_sleep,
        )
        .await;
        assert_eq!(attempts, 3);
        assert_eq!(record.status, "success");
    }
}
