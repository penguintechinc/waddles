//! Fixed-platform receiver supervisors: each submodule owns one platform's
//! persistent connection lifecycle (connect, receive, reconnect-with-
//! backoff on any transient failure) and, on every inbound chat message,
//! normalizes it (`crate::normalize`) and publishes it onto the spine
//! (`crate::publish::publish_event`) exactly once.
//!
//! `penguin-connector-{slack,youtube,kick}` and the generic webhook/JWT
//! intake surfaces are `// TODO(M5)` -- not wired here, see
//! `crate::lib`'s module doc.
//!
//! [`Backoff`] and [`STABILITY_WINDOW`] are the shared reconnect/retry
//! discipline every retry-on-failure loop in this crate uses --
//! `ingest::discord`, `ingest::twitch`, and `crate::outbound`'s queue-read
//! retry alike (see [`Backoff`]'s own doc comment for the incident that
//! drove this: a Discord reconnect storm caused by resetting the backoff
//! on a bare successful connect, before the session had proven itself).

pub mod discord;
pub mod twitch;

use std::time::Duration;

/// How long a freshly (re)established connection must stay open -- or
/// otherwise prove itself (e.g. deliver one message), whichever comes
/// first -- before it is trusted enough to reset the reconnect backoff.
/// Shared by every platform reconnect loop in this crate; see
/// [`Backoff`]'s doc comment for why this exists.
pub(crate) const STABILITY_WINDOW: Duration = Duration::from_secs(60);

/// Exponential reconnect/retry backoff with full jitter (a uniform draw in
/// `0..=ceiling`, `ceiling = min(cap, 1s * 2^attempts)`, matching AWS's
/// "full jitter" guidance and this repo's `core/svc_action/src/
/// retry.rs::Jitter`/`backoff_delay_ms` precedent). A deliberately tiny
/// local primitive, not a re-implementation of a shared connector
/// mechanic: neither `penguin-connector-twitch` nor `-discord` ships a
/// shared "core"/reconnect-backoff crate at the pinned rev (confirmed by
/// reading their real `Cargo.toml`/`lib.rs` -- there is no
/// `penguin-connector-core`), so there is nothing to consume instead. The
/// xorshift64* PRNG step is duplicated from `svc_action::retry::Jitter`
/// rather than imported (separate binary crates, no shared dependency) --
/// see that module's own doc comment for why neither crate pulls in a
/// `rand` dependency for this. Exclusively owned by one caller at a time
/// (a single `run_loop`/`drain_loop`), hence a plain `u64` PRNG state
/// behind `&mut self`, not `svc_action::retry::Jitter`'s deliberately
/// shared `AtomicU64`/`&self` shape.
///
/// **`reset()` is never called by [`Backoff::delay`] or by a bare
/// successful connect/poll.** That is the actual bug behind the incident
/// this type exists to fix (a live Discord gateway reconnect storm):
/// the old code reset the backoff to ~1s the moment the low-level
/// WebSocket handshake (`HELLO`/`IDENTIFY`) succeeded, before the Gateway
/// session had done anything to prove itself. A connection Discord
/// accepts and then immediately drops (bad/disabled bot token, rate
/// limiting, protocol churn) looks "successful" at that layer on every
/// single cycle, so the backoff never escalated -- hundreds of
/// connect/disconnect cycles ~100-150ms apart hammering Discord's real
/// API, to the point the bot token had to be scaled to zero to protect
/// it. Every caller of this type must only call `reset()` once the
/// connection/poll is *confirmed* healthy: a message received, or
/// [`STABILITY_WINDOW`] elapsed connected (`ingest::discord`/
/// `ingest::twitch`'s `run_loop`s), or a successful queue poll
/// (`crate::outbound::drain_loop`) -- and must apply `delay()` (awaited,
/// racing shutdown) on *every* failure path, not just some of them.
pub(crate) struct Backoff {
    attempts: u32,
    base: Duration,
    cap: Duration,
    prng_state: u64,
}

impl Backoff {
    /// Real-entropy constructor for production use.
    pub(crate) fn new(cap: Duration) -> Self {
        Self::seeded(cap, Self::entropy_seed())
    }

    /// Seeds from a mix of wall-clock nanos and this process's pid, so
    /// concurrent instances don't share a seed -- mirrors
    /// `core/svc_action/src/retry.rs::Jitter::from_entropy`.
    fn entropy_seed() -> u64 {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos() as u64)
            .unwrap_or(0);
        let seed = nanos ^ (u64::from(std::process::id()) << 32) ^ 0x9E37_79B9_7F4A_7C15;
        if seed == 0 {
            0xDEAD_BEEF_CAFE_F00D
        } else {
            seed
        }
    }

    /// Deterministic constructor -- [`Backoff::new`] uses a real-entropy
    /// seed; tests pass a fixed one for a reproducible delay sequence.
    pub(crate) fn seeded(cap: Duration, seed: u64) -> Self {
        Self {
            attempts: 0,
            base: Duration::from_secs(1),
            cap,
            prng_state: if seed == 0 { 1 } else { seed },
        }
    }

    /// One xorshift64* step.
    fn next_u64(&mut self) -> u64 {
        let mut x = self.prng_state;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.prng_state = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }

    /// The exponential ceiling for the current attempt count -- pure, no
    /// randomness: `min(cap, base * 2^attempts)`. `checked_shl` guards the
    /// (practically unreachable, but not UB-free) case of `attempts >= 32`.
    pub(crate) fn ceiling(&self) -> Duration {
        match 1u32.checked_shl(self.attempts) {
            Some(mult) => self.base.saturating_mul(mult).min(self.cap),
            None => self.cap,
        }
    }

    /// Returns a full-jitter delay (uniform draw in `0..=ceiling`) for the
    /// current attempt count, then advances the attempt count.
    pub(crate) fn delay(&mut self) -> Duration {
        let ceiling = self.ceiling();
        let ceiling_ms = u64::try_from(ceiling.as_millis()).unwrap_or(u64::MAX);
        let draw_ms = if ceiling_ms == 0 {
            0
        } else {
            self.next_u64() % (ceiling_ms + 1)
        };
        self.attempts = self.attempts.saturating_add(1);
        Duration::from_millis(draw_ms)
    }

    /// Resets the attempt count back to zero. Callers must only invoke
    /// this once the connection/poll has been confirmed healthy -- see
    /// this type's own doc comment.
    pub(crate) fn reset(&mut self) {
        self.attempts = 0;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backoff_ceiling_doubles_up_to_the_cap_and_never_shrinks() {
        let mut b = Backoff::seeded(Duration::from_secs(30), 42);
        let ceilings: Vec<Duration> = (0..8)
            .map(|_| {
                let c = b.ceiling();
                b.delay();
                c
            })
            .collect();
        assert_eq!(
            ceilings,
            vec![
                Duration::from_secs(1),
                Duration::from_secs(2),
                Duration::from_secs(4),
                Duration::from_secs(8),
                Duration::from_secs(16),
                Duration::from_secs(30),
                Duration::from_secs(30),
                Duration::from_secs(30),
            ]
        );
    }

    #[test]
    fn backoff_reset_returns_the_ceiling_to_the_base() {
        let mut b = Backoff::seeded(Duration::from_secs(30), 42);
        b.delay();
        b.delay();
        b.delay();
        assert_eq!(b.ceiling(), Duration::from_secs(8));
        b.reset();
        assert_eq!(b.ceiling(), Duration::from_secs(1));
    }

    #[test]
    fn backoff_delay_never_exceeds_its_ceiling() {
        let mut b = Backoff::seeded(Duration::from_secs(30), 777);
        for _ in 0..50 {
            let ceiling = b.ceiling();
            let delay = b.delay();
            assert!(
                delay <= ceiling,
                "full-jitter delay {delay:?} must never exceed its ceiling {ceiling:?}"
            );
        }
    }

    #[test]
    fn backoff_zero_cap_always_returns_zero_delay() {
        let mut b = Backoff::seeded(Duration::ZERO, 1);
        for _ in 0..5 {
            assert_eq!(b.delay(), Duration::ZERO);
        }
    }
}
