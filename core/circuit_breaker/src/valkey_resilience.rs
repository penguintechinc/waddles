//! Process-wide Valkey connection resilience shared by the data-plane stage
//! services (`svc_process`, `svc_action`): capped exponential backoff with
//! full jitter, a keyed log throttle, and a single-flight shared connection
//! slot. Incident 2026-11 (alpha): ~150 per-bundle consumers each opened
//! their own TLS connection and retried on a tight fixed loop, starving
//! Valkey of CPU until its liveness probe failed -- these primitives make
//! every consumer share ONE connection and back off together.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

/// Default first retry ceiling.
pub const BACKOFF_BASE: Duration = Duration::from_millis(250);
/// Default retry ceiling cap.
pub const BACKOFF_CAP: Duration = Duration::from_secs(30);

/// Tiny xorshift64* generator; jitter needs spread, not cryptographic
/// strength, so no `rand` dependency is added to this crate.
fn next_rand(state: &mut u64) -> u64 {
    let mut x = *state;
    x ^= x << 13;
    x ^= x >> 7;
    x ^= x << 17;
    *state = x;
    x.wrapping_mul(0x2545_F491_4F6C_DD1D)
}

/// Seed mixing wall-clock nanos with a per-process counter so concurrent
/// consumers never share a jitter sequence.
fn entropy_seed() -> u64 {
    static COUNTER: AtomicU64 = AtomicU64::new(1);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0);
    let seed = nanos
        ^ COUNTER
            .fetch_add(1, Ordering::Relaxed)
            .wrapping_mul(0x9E37_79B9_7F4A_7C15);
    if seed == 0 {
        0xDEAD_BEEF_CAFE_F00D
    } else {
        seed
    }
}

/// Capped exponential backoff with full jitter: attempt `n` sleeps a
/// uniformly random duration in `[0, min(cap, base * 2^n)]`.
pub struct Backoff {
    base: Duration,
    cap: Duration,
    attempt: u32,
    rng: u64,
}

impl Backoff {
    /// Builds a backoff with an entropy-derived jitter seed.
    pub fn new(base: Duration, cap: Duration) -> Self {
        Self::seeded(base, cap, entropy_seed())
    }

    /// Deterministic constructor for tests.
    pub fn seeded(base: Duration, cap: Duration, seed: u64) -> Self {
        Self {
            base,
            cap,
            attempt: 0,
            rng: if seed == 0 { 1 } else { seed },
        }
    }

    /// The un-jittered ceiling for the current attempt (never above `cap`).
    pub fn ceiling(&self) -> Duration {
        let factor = 1u128 << self.attempt.min(32);
        let raw = self.base.as_millis().saturating_mul(factor);
        Duration::from_millis(raw.min(self.cap.as_millis()) as u64)
    }

    /// Returns the next jittered delay and advances the attempt counter.
    pub fn next_delay(&mut self) -> Duration {
        let ceil_ms = self.ceiling().as_millis() as u64;
        self.attempt = self.attempt.saturating_add(1);
        Duration::from_millis(next_rand(&mut self.rng) % (ceil_ms + 1))
    }

    /// Resets to the first attempt after a success.
    pub fn reset(&mut self) {
        self.attempt = 0;
    }
}

impl Default for Backoff {
    fn default() -> Self {
        Self::new(BACKOFF_BASE, BACKOFF_CAP)
    }
}

/// Rate-limits identical log lines: the first occurrence of a key logs, then
/// at most once per `interval`, reporting how many were suppressed between.
pub struct LogThrottle {
    interval: Duration,
    state: Mutex<HashMap<String, (Instant, u64)>>,
}

impl LogThrottle {
    /// Creates a throttle emitting each key at most once per `interval`.
    pub fn new(interval: Duration) -> Self {
        Self {
            interval,
            state: Mutex::new(HashMap::new()),
        }
    }

    /// Returns `Some(suppressed_since_last_log)` when the caller should log
    /// now, `None` when this occurrence must be suppressed.
    pub fn check(&self, key: &str) -> Option<u64> {
        self.check_at(key, Instant::now())
    }

    fn check_at(&self, key: &str, now: Instant) -> Option<u64> {
        let mut map = self.state.lock().unwrap_or_else(|e| e.into_inner());
        match map.get_mut(key) {
            None => {
                map.insert(key.to_string(), (now, 0));
                Some(0)
            }
            Some((last, suppressed)) => {
                if now.duration_since(*last) >= self.interval {
                    let n = *suppressed;
                    *last = now;
                    *suppressed = 0;
                    Some(n)
                } else {
                    *suppressed += 1;
                    None
                }
            }
        }
    }
}

impl Default for LogThrottle {
    fn default() -> Self {
        Self::new(Duration::from_secs(30))
    }
}

struct Slot<T> {
    value: Option<(T, u64)>,
    generation: u64,
    backoff: Backoff,
    retry_not_before: Option<Instant>,
    last_error: String,
}

/// One lazily-built, process-shared connection handle (`T` is a cheap
/// `Clone`, e.g. a multiplexed Valkey client). Concurrent callers are
/// single-flighted so only one connection attempt is ever in progress, and
/// after a failed attempt every caller fails fast (no network) until a shared
/// jittered backoff elapses -- so N consumers cost one probe, not N.
pub struct SharedConn<T: Clone> {
    slot: tokio::sync::Mutex<Slot<T>>,
    constructions: AtomicU64,
}

impl<T: Clone> SharedConn<T> {
    /// Creates an empty slot with the default shared backoff.
    pub fn new() -> Self {
        Self::with_backoff(Backoff::default())
    }

    /// Creates an empty slot with a caller-supplied backoff (tests).
    pub fn with_backoff(backoff: Backoff) -> Self {
        Self {
            slot: tokio::sync::Mutex::new(Slot {
                value: None,
                generation: 0,
                backoff,
                retry_not_before: None,
                last_error: String::new(),
            }),
            constructions: AtomicU64::new(0),
        }
    }

    /// Number of times the factory has actually been invoked.
    pub fn constructions(&self) -> u64 {
        self.constructions.load(Ordering::Relaxed)
    }

    /// Returns a clone of the shared connection plus its generation, building
    /// it with `connect` on first use. During a post-failure cooldown returns
    /// the cached error without calling `connect`.
    pub async fn get_or_connect<F, Fut, E>(&self, connect: F) -> Result<(T, u64), String>
    where
        F: FnOnce() -> Fut,
        Fut: std::future::Future<Output = Result<T, E>>,
        E: std::fmt::Display,
    {
        let mut slot = self.slot.lock().await;
        if let Some((v, g)) = &slot.value {
            return Ok((v.clone(), *g));
        }
        if let Some(until) = slot.retry_not_before {
            if Instant::now() < until {
                return Err(format!("{} (shared connect cooling down)", slot.last_error));
            }
        }
        self.constructions.fetch_add(1, Ordering::Relaxed);
        match connect().await {
            Ok(v) => {
                slot.generation += 1;
                let g = slot.generation;
                slot.value = Some((v.clone(), g));
                slot.retry_not_before = None;
                slot.backoff.reset();
                Ok((v, g))
            }
            Err(e) => {
                let delay = slot.backoff.next_delay();
                slot.retry_not_before = Some(Instant::now() + delay);
                slot.last_error = e.to_string();
                Err(slot.last_error.clone())
            }
        }
    }

    /// Drops the cached connection if it is still generation `generation`, so
    /// the next caller rebuilds it (a stale handle after a Valkey restart).
    /// A newer generation already replaced by another consumer is kept.
    pub async fn invalidate(&self, generation: u64) {
        let mut slot = self.slot.lock().await;
        if matches!(&slot.value, Some((_, g)) if *g == generation) {
            slot.value = None;
        }
    }
}

impl<T: Clone> Default for SharedConn<T> {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;

    #[test]
    fn backoff_delay_is_bounded_by_ceiling_and_cap() {
        let mut b = Backoff::seeded(Duration::from_millis(250), Duration::from_secs(30), 42);
        for _ in 0..200 {
            let ceil = b.ceiling();
            let d = b.next_delay();
            assert!(d <= ceil, "{d:?} > {ceil:?}");
            assert!(d <= Duration::from_secs(30));
        }
        assert_eq!(b.ceiling(), Duration::from_secs(30));
    }

    #[test]
    fn backoff_ceiling_doubles_then_caps() {
        let mut b = Backoff::seeded(Duration::from_millis(250), Duration::from_secs(30), 7);
        let mut seen = vec![];
        for _ in 0..9 {
            seen.push(b.ceiling().as_millis());
            b.next_delay();
        }
        assert_eq!(
            seen,
            vec![250, 500, 1000, 2000, 4000, 8000, 16000, 30000, 30000]
        );
    }

    #[test]
    fn backoff_is_jittered_and_reset_restarts() {
        let mut b = Backoff::seeded(Duration::from_millis(250), Duration::from_secs(30), 99);
        for _ in 0..10 {
            b.next_delay();
        }
        let delays: std::collections::HashSet<u128> =
            (0..50).map(|_| b.next_delay().as_millis()).collect();
        assert!(delays.len() > 10, "expected spread, got {}", delays.len());
        b.reset();
        assert_eq!(b.ceiling(), Duration::from_millis(250));
    }

    #[test]
    fn backoff_huge_attempt_does_not_overflow() {
        let mut b = Backoff::seeded(Duration::from_secs(1), Duration::from_secs(30), 3);
        b.attempt = u32::MAX;
        assert_eq!(b.ceiling(), Duration::from_secs(30));
        assert!(b.next_delay() <= Duration::from_secs(30));
    }

    #[test]
    fn throttle_logs_first_then_suppresses_with_count() {
        let t = LogThrottle::new(Duration::from_secs(10));
        let t0 = Instant::now();
        assert_eq!(t.check_at("k", t0), Some(0));
        assert_eq!(t.check_at("k", t0 + Duration::from_secs(1)), None);
        assert_eq!(t.check_at("k", t0 + Duration::from_secs(2)), None);
        assert_eq!(t.check_at("other", t0 + Duration::from_secs(2)), Some(0));
        assert_eq!(t.check_at("k", t0 + Duration::from_secs(11)), Some(2));
        assert_eq!(t.check_at("k", t0 + Duration::from_secs(12)), None);
    }

    #[tokio::test]
    async fn n_concurrent_consumers_share_one_connection() {
        let shared: Arc<SharedConn<u32>> = Arc::new(SharedConn::new());
        let mut tasks = vec![];
        for _ in 0..150 {
            let s = Arc::clone(&shared);
            tasks.push(tokio::spawn(async move {
                s.get_or_connect(|| async { Ok::<_, String>(7u32) }).await
            }));
        }
        for t in tasks {
            let (v, g) = t.await.unwrap().unwrap();
            assert_eq!((v, g), (7, 1));
        }
        assert_eq!(shared.constructions(), 1);
    }

    #[tokio::test]
    async fn failed_connect_cools_down_without_network_then_retries() {
        let shared: SharedConn<u32> = SharedConn::with_backoff(Backoff::seeded(
            Duration::from_secs(60),
            Duration::from_secs(60),
            5,
        ));
        // A 60s ceiling with jitter could still be ~0; force a cooldown deterministically
        // by failing until one lands with a non-trivial window.
        let mut cooled = false;
        for _ in 0..20 {
            let _ = shared
                .get_or_connect(|| async { Err::<u32, _>("boom".to_string()) })
                .await;
            let before = shared.constructions();
            let r = shared
                .get_or_connect(|| async { Err::<u32, _>("boom".to_string()) })
                .await;
            if shared.constructions() == before {
                assert!(r.unwrap_err().contains("cooling down"));
                cooled = true;
                break;
            }
        }
        assert!(cooled, "a failed connect must put callers into cooldown");
    }

    #[tokio::test]
    async fn invalidate_rebuilds_only_current_generation() {
        let shared: SharedConn<u32> = SharedConn::new();
        let (_, g1) = shared
            .get_or_connect(|| async { Ok::<_, String>(1u32) })
            .await
            .unwrap();
        shared.invalidate(g1).await;
        let (v, g2) = shared
            .get_or_connect(|| async { Ok::<_, String>(2u32) })
            .await
            .unwrap();
        assert_eq!((v, g2), (2, 2));
        shared.invalidate(g1).await; // stale generation: no-op
        let (v, _) = shared
            .get_or_connect(|| async { Ok::<_, String>(3u32) })
            .await
            .unwrap();
        assert_eq!(v, 2);
        assert_eq!(shared.constructions(), 2);
    }
}
