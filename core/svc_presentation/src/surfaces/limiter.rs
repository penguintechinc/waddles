//! Per-(community, surface) token-bucket rate limiter for surface publishes.
//!
//! One bucket per key, refilled continuously at `per_minute / 60` tokens per
//! second with a burst of a quarter-minute's worth (at least one). The key map
//! is bounded: when full, buckets idle long enough to be fully refilled are
//! dropped first (they carry no state), and a still-full map refuses new keys
//! loudly rather than growing without bound.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

/// Upper bound on tracked (community, surface) buckets.
pub const MAX_BUCKETS: usize = 16_384;

struct Bucket {
    tokens: f64,
    last: Instant,
}

/// Why a publish was not admitted.
#[derive(Debug, PartialEq, Eq)]
pub enum Denied {
    /// The bucket is empty; retry after the given delay.
    RateLimited(Duration),
    /// The limiter's key table is saturated with live buckets.
    Saturated,
}

/// The limiter. Cheap to share behind an `Arc`.
#[derive(Default)]
pub struct RateLimiter {
    buckets: Mutex<HashMap<(i64, String), Bucket>>,
}

impl RateLimiter {
    /// An empty limiter.
    pub fn new() -> Self {
        Self::default()
    }

    /// Takes one token for `(community_id, surface)` under `per_minute`.
    pub fn check(
        &self,
        community_id: i64,
        surface: &str,
        per_minute: u32,
        now: Instant,
    ) -> Result<(), Denied> {
        let rate = f64::from(per_minute) / 60.0;
        let burst = f64::from((per_minute / 4).max(1));
        let mut buckets = match self.buckets.lock() {
            Ok(guard) => guard,
            // The map holds only counters; recover but say so.
            Err(poisoned) => {
                tracing::warn!("surface rate limiter lock was poisoned; recovering");
                poisoned.into_inner()
            }
        };
        let key = (community_id, surface.to_string());
        if !buckets.contains_key(&key) && buckets.len() >= MAX_BUCKETS {
            buckets.retain(|_, b| {
                let refilled =
                    b.tokens + now.saturating_duration_since(b.last).as_secs_f64() * rate;
                refilled < burst
            });
            if buckets.len() >= MAX_BUCKETS {
                return Err(Denied::Saturated);
            }
        }
        let bucket = buckets.entry(key).or_insert(Bucket {
            tokens: burst,
            last: now,
        });
        let elapsed = now.saturating_duration_since(bucket.last).as_secs_f64();
        bucket.tokens = (bucket.tokens + elapsed * rate).min(burst);
        bucket.last = now;
        if bucket.tokens >= 1.0 {
            bucket.tokens -= 1.0;
            Ok(())
        } else {
            let wait = (1.0 - bucket.tokens) / rate;
            Err(Denied::RateLimited(Duration::from_secs_f64(wait)))
        }
    }

    /// Number of tracked buckets (diagnostics/tests).
    pub fn len(&self) -> usize {
        self.buckets.lock().map(|b| b.len()).unwrap_or(0)
    }

    /// `true` when no bucket is tracked.
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn burst_then_denied_then_refills() {
        let l = RateLimiter::new();
        let t0 = Instant::now();
        // 60/min -> burst 15, 1 token/s.
        for _ in 0..15 {
            assert!(l.check(1, "a", 60, t0).is_ok());
        }
        match l.check(1, "a", 60, t0) {
            Err(Denied::RateLimited(d)) => assert!(d <= Duration::from_secs(1)),
            other => panic!("expected rate limited, got {other:?}"),
        }
        assert!(l.check(1, "a", 60, t0 + Duration::from_secs(1)).is_ok());
    }

    #[test]
    fn communities_and_surfaces_have_independent_buckets() {
        let l = RateLimiter::new();
        let t0 = Instant::now();
        for _ in 0..5 {
            assert!(l.check(1, "clip.alert", 20, t0).is_ok());
        }
        assert!(l.check(1, "clip.alert", 20, t0).is_err());
        assert!(l.check(2, "clip.alert", 20, t0).is_ok());
        assert!(l.check(1, "alert", 20, t0).is_ok());
    }

    #[test]
    fn tiny_rate_still_admits_one() {
        let l = RateLimiter::new();
        assert!(l.check(1, "x", 1, Instant::now()).is_ok());
    }
}
