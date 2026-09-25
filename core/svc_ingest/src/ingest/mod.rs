//! Fixed-platform receiver supervisors: each submodule owns one platform's
//! persistent connection lifecycle (connect, receive, reconnect-with-
//! backoff on any transient failure) and, on every inbound chat message,
//! normalizes it (`crate::normalize`) and publishes it onto the spine
//! (`crate::publish::publish_event`) exactly once.
//!
//! `penguin-connector-{slack,youtube,kick}` and the generic webhook/JWT
//! intake surfaces are `// TODO(M5)` -- not wired here, see
//! `crate::lib`'s module doc.

pub mod discord;
pub mod twitch;

use std::time::Duration;

/// Exponential reconnect backoff, 1s doubling to a 30s ceiling. A
/// deliberately tiny local primitive, not a re-implementation of a shared
/// connector mechanic: neither `penguin-connector-twitch` nor
/// `-discord` ships a shared "core"/reconnect-backoff crate at the pinned
/// rev (confirmed by reading their real `Cargo.toml`/`lib.rs` -- there is
/// no `penguin-connector-core`), so there is nothing to consume instead.
pub(crate) struct Backoff {
    next: Duration,
    ceiling: Duration,
}

impl Backoff {
    /// Starts at 1 second, doubling up to `ceiling` on each call to
    /// [`Self::delay`].
    pub(crate) fn new(ceiling: Duration) -> Self {
        Self {
            next: Duration::from_secs(1),
            ceiling,
        }
    }

    /// Returns the delay to wait before the next reconnect attempt, then
    /// doubles it (capped at `ceiling`) for the call after that.
    pub(crate) fn delay(&mut self) -> Duration {
        let d = self.next;
        self.next = (self.next * 2).min(self.ceiling);
        d
    }

    /// Resets to the initial 1-second delay -- called after a successful
    /// connect, so a receiver that has been up for a while doesn't carry a
    /// stale long backoff into its *next* disconnect.
    pub(crate) fn reset(&mut self) {
        self.next = Duration::from_secs(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backoff_doubles_up_to_the_ceiling() {
        let mut b = Backoff::new(Duration::from_secs(30));
        assert_eq!(b.delay(), Duration::from_secs(1));
        assert_eq!(b.delay(), Duration::from_secs(2));
        assert_eq!(b.delay(), Duration::from_secs(4));
        assert_eq!(b.delay(), Duration::from_secs(8));
        assert_eq!(b.delay(), Duration::from_secs(16));
        assert_eq!(b.delay(), Duration::from_secs(30), "capped at ceiling");
        assert_eq!(b.delay(), Duration::from_secs(30), "stays at ceiling");
    }

    #[test]
    fn backoff_reset_returns_to_the_initial_delay() {
        let mut b = Backoff::new(Duration::from_secs(30));
        b.delay();
        b.delay();
        b.reset();
        assert_eq!(b.delay(), Duration::from_secs(1));
    }
}
