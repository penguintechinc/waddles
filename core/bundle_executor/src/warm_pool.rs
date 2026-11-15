//! Bounded pool of pre-instantiated, **never-yet-used** guest instances, keyed by bundle digest.
//!
//! **What it buys.** Instantiating a component (Python/C# especially) costs 50-300 ms on the
//! request path (`waddles_executor_instantiate_seconds`, latency suspect #2 in
//! `docs/observability/command-latency.md`). This pool moves that cost off the path: after a
//! digest is first invoked, a background task keeps up to `per_digest` fresh instances ready and
//! an invoke *takes* one instead of instantiating.
//!
//! **Isolation guarantee (why it is safe across tenants).** An entry is **one-shot**: [`take`]
//! removes it and nothing ever returns it to the pool, so a guest instance serves exactly one
//! invocation and is then dropped -- no guest linear memory, global, table or WASI state can
//! carry from one invocation (tenant/community) to the next. This is deliberately stricter than
//! reusing a live store between calls, which cannot be reset without re-instantiating and would
//! leak guest state between the tenants that share a digest. A prewarmed instance is built with
//! NO per-call state (no host bridge, no scope, no invoke id); the caller binds all of it at
//! checkout (see `ExecState::rebind_for_call`).
//!
//! **Bounds.** `per_digest` ready instances per digest, `total_cap` across the process (each
//! warm instance holds guest linear memory, up to the per-instance memory cap), an idle TTL that
//! drops entries and slots nobody invoked recently, eviction on bundle unload / version change
//! (a new version is a new digest, so its pool starts empty and the old one is evicted), and a
//! generation ticket so an in-flight fill that finishes after an eviction is dropped, never
//! inserted. A digest whose prewarm fails (e.g. a start function that needs a host call) is
//! poisoned: it keeps serving cold, loudly, and is never retried in a loop.
//!
//! This module is a plain, wasmtime-free data structure so its policy is unit-testable fast;
//! `crate::invoke` instantiates and stores the real entries.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

/// Proof that the holder reserved a fill for one digest generation; passed back to
/// [`WarmPool::put`] / [`WarmPool::fill_failed`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FillTicket {
    digest: String,
    generation: u64,
}

impl FillTicket {
    /// The digest this fill is for.
    #[must_use]
    pub fn digest(&self) -> &str {
        &self.digest
    }
}

struct Slot<T> {
    generation: u64,
    ready: Vec<(Instant, T)>,
    /// Fills reserved but not yet put / failed.
    filling: usize,
    last_used: Instant,
    poisoned: bool,
}

struct State<T> {
    slots: HashMap<String, Slot<T>>,
    next_generation: u64,
}

/// See the module doc. `T` is one prewarmed instance.
pub struct WarmPool<T> {
    per_digest: usize,
    total_cap: usize,
    idle_ttl: Duration,
    state: Mutex<State<T>>,
}

impl<T> WarmPool<T> {
    /// A pool holding up to `per_digest` ready entries per digest and `total_cap` overall;
    /// entries and slots idle longer than `idle_ttl` are dropped by [`Self::sweep`].
    /// `per_digest == 0` or `total_cap == 0` makes a disabled pool (every call is a no-op).
    #[must_use]
    pub fn new(per_digest: usize, total_cap: usize, idle_ttl: Duration) -> Self {
        Self {
            per_digest,
            total_cap,
            idle_ttl,
            state: Mutex::new(State {
                slots: HashMap::new(),
                next_generation: 1,
            }),
        }
    }

    /// Whether the pool can ever hold anything.
    #[must_use]
    pub fn enabled(&self) -> bool {
        self.per_digest > 0 && self.total_cap > 0
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, State<T>> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Takes one ready entry for `digest` (consumed: it is never returned to the pool), or
    /// `None` on a miss. Also marks the digest recently used so its slot survives the sweep.
    pub fn take(&self, digest: &str, now: Instant) -> Option<T> {
        if !self.enabled() {
            return None;
        }
        let mut st = self.lock();
        let slot = st.slots.get_mut(digest)?;
        slot.last_used = now;
        slot.ready.pop().map(|(_, t)| t)
    }

    /// Notes that `digest` was invoked (creates its slot on first use) and reserves as many
    /// fills as the bounds allow, returning one ticket per instance the caller should now
    /// instantiate and [`Self::put`]. Empty when disabled, poisoned, full, or at the global cap.
    pub fn reserve_fills(&self, digest: &str, now: Instant) -> Vec<FillTicket> {
        if !self.enabled() {
            return Vec::new();
        }
        let mut st = self.lock();
        let total: usize = st.slots.values().map(|s| s.ready.len() + s.filling).sum();
        if !st.slots.contains_key(digest) {
            let generation = st.next_generation;
            st.next_generation += 1;
            st.slots.insert(
                digest.to_string(),
                Slot {
                    generation,
                    ready: Vec::new(),
                    filling: 0,
                    last_used: now,
                    poisoned: false,
                },
            );
        }
        let Some(slot) = st.slots.get_mut(digest) else {
            return Vec::new();
        };
        slot.last_used = now;
        if slot.poisoned {
            return Vec::new();
        }
        let want = self
            .per_digest
            .saturating_sub(slot.ready.len() + slot.filling);
        let room = self.total_cap.saturating_sub(total);
        let n = want.min(room);
        slot.filling += n;
        let generation = slot.generation;
        (0..n)
            .map(|_| FillTicket {
                digest: digest.to_string(),
                generation,
            })
            .collect()
    }

    /// Stores a freshly instantiated entry for `ticket`. Returns it back (`Err`) when the slot
    /// was evicted/poisoned since the reservation or is already full, so the caller drops it.
    pub fn put(&self, ticket: &FillTicket, value: T, now: Instant) -> Result<(), T> {
        let mut st = self.lock();
        let Some(slot) = st.slots.get_mut(&ticket.digest) else {
            return Err(value);
        };
        if slot.generation != ticket.generation {
            return Err(value);
        }
        slot.filling = slot.filling.saturating_sub(1);
        if slot.poisoned || slot.ready.len() >= self.per_digest {
            return Err(value);
        }
        slot.ready.push((now, value));
        Ok(())
    }

    /// Releases a reservation whose instantiate failed. `poison` stops all further fills for
    /// this digest generation (it keeps serving cold) -- used for deterministic failures such as
    /// a start function that needs a host call, so a broken digest never spins.
    pub fn fill_failed(&self, ticket: &FillTicket, poison: bool) {
        let mut st = self.lock();
        if let Some(slot) = st.slots.get_mut(&ticket.digest) {
            if slot.generation == ticket.generation {
                slot.filling = slot.filling.saturating_sub(1);
                slot.poisoned |= poison;
            }
        }
    }

    /// Drops the whole slot for `digest` (bundle unloaded or superseded) and returns the ready
    /// entries so the caller drops them outside the lock. In-flight fills are invalidated.
    pub fn evict(&self, digest: &str) -> Vec<T> {
        let mut st = self.lock();
        st.slots
            .remove(digest)
            .map(|s| s.ready.into_iter().map(|(_, t)| t).collect())
            .unwrap_or_default()
    }

    /// Drops every slot not used within the idle TTL; returns the dropped entries.
    pub fn sweep(&self, now: Instant) -> Vec<T> {
        let mut st = self.lock();
        let ttl = self.idle_ttl;
        let stale: Vec<String> = st
            .slots
            .iter()
            .filter(|(_, s)| now.saturating_duration_since(s.last_used) >= ttl)
            .map(|(d, _)| d.clone())
            .collect();
        let mut out = Vec::new();
        for d in stale {
            if let Some(s) = st.slots.remove(&d) {
                out.extend(s.ready.into_iter().map(|(_, t)| t));
            }
        }
        out
    }

    /// Ready entries for `digest`.
    #[must_use]
    pub fn ready_len(&self, digest: &str) -> usize {
        self.lock().slots.get(digest).map_or(0, |s| s.ready.len())
    }

    /// Ready entries across every digest (backs the pool-size gauge).
    #[must_use]
    pub fn total_ready(&self) -> usize {
        self.lock().slots.values().map(|s| s.ready.len()).sum()
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    fn pool(per: usize, cap: usize) -> WarmPool<u32> {
        WarmPool::new(per, cap, Duration::from_secs(60))
    }

    fn fill(p: &WarmPool<u32>, digest: &str, now: Instant, first: u32) -> usize {
        let tickets = p.reserve_fills(digest, now);
        let n = tickets.len();
        for (i, t) in tickets.iter().enumerate() {
            p.put(t, first + u32::try_from(i).unwrap(), now).unwrap();
        }
        n
    }

    #[test]
    fn disabled_pool_is_inert() {
        let now = Instant::now();
        for p in [pool(0, 8), pool(2, 0)] {
            assert!(!p.enabled());
            assert!(p.reserve_fills("d", now).is_empty());
            assert!(p.take("d", now).is_none());
            assert_eq!(p.total_ready(), 0);
        }
    }

    /// The isolation property: every entry is handed out exactly once -- a taken entry can never
    /// be taken again, by the same or another tenant's invocation.
    #[test]
    fn entries_are_one_shot_never_returned_to_the_pool() {
        let now = Instant::now();
        let p = pool(3, 8);
        assert_eq!(fill(&p, "d", now, 10), 3);
        let mut seen = Vec::new();
        while let Some(v) = p.take("d", now) {
            seen.push(v);
        }
        seen.sort_unstable();
        assert_eq!(
            seen,
            vec![10, 11, 12],
            "each instance served exactly one take"
        );
        assert!(
            p.take("d", now).is_none(),
            "an exhausted pool misses; nothing is reused"
        );
        assert_eq!(p.ready_len("d"), 0);
    }

    #[test]
    fn per_digest_bound_and_refill_after_take() {
        let now = Instant::now();
        let p = pool(2, 100);
        assert_eq!(fill(&p, "d", now, 0), 2);
        assert!(
            p.reserve_fills("d", now).is_empty(),
            "already at the per-digest bound"
        );
        p.take("d", now).unwrap();
        assert_eq!(
            p.reserve_fills("d", now).len(),
            1,
            "refills exactly the taken slot"
        );
    }

    #[test]
    fn in_flight_fills_count_against_the_bound() {
        let now = Instant::now();
        let p = pool(2, 100);
        let first = p.reserve_fills("d", now);
        assert_eq!(first.len(), 2);
        assert!(
            p.reserve_fills("d", now).is_empty(),
            "no double reservation while filling"
        );
        for t in &first {
            p.put(t, 1, now).unwrap();
        }
        assert_eq!(p.ready_len("d"), 2);
    }

    #[test]
    fn global_cap_bounds_the_total_across_digests() {
        let now = Instant::now();
        let p = pool(4, 5);
        assert_eq!(fill(&p, "a", now, 0), 4);
        assert_eq!(
            fill(&p, "b", now, 100),
            1,
            "only one slot of global room left"
        );
        assert!(p.reserve_fills("c", now).is_empty());
        assert_eq!(p.total_ready(), 5);
    }

    /// Version change / unload: the digest's entries are dropped and a fill that was in flight
    /// during the eviction is refused when it lands (the old version never repopulates).
    #[test]
    fn eviction_drops_entries_and_invalidates_in_flight_fills() {
        let now = Instant::now();
        let p = pool(2, 8);
        assert_eq!(fill(&p, "old", now, 1), 2);
        let in_flight = p.reserve_fills("new", now);
        assert_eq!(in_flight.len(), 2);
        let dropped = p.evict("old");
        assert_eq!(dropped.len(), 2);
        assert!(p.take("old", now).is_none());
        let _ = p.evict("new");
        assert_eq!(
            p.put(&in_flight[0], 99, now),
            Err(99),
            "stale generation is refused"
        );
        // A re-created slot for the same digest gets a NEW generation: the old ticket stays dead.
        let again = p.reserve_fills("new", now);
        assert_eq!(again.len(), 2);
        assert_eq!(p.put(&in_flight[1], 98, now), Err(98));
        assert!(p.put(&again[0], 7, now).is_ok());
    }

    #[test]
    fn poisoned_digest_stops_filling_but_other_digests_are_unaffected() {
        let now = Instant::now();
        let p = pool(2, 8);
        let t = p.reserve_fills("bad", now);
        assert_eq!(t.len(), 2);
        p.fill_failed(&t[0], true);
        p.fill_failed(&t[1], false);
        assert!(
            p.reserve_fills("bad", now).is_empty(),
            "a poisoned digest is never retried"
        );
        assert_eq!(fill(&p, "ok", now, 0), 2);
    }

    #[test]
    fn non_poisoning_failure_releases_the_reservation() {
        let now = Instant::now();
        let p = pool(1, 8);
        let t = p.reserve_fills("d", now);
        p.fill_failed(&t[0], false);
        assert_eq!(
            p.reserve_fills("d", now).len(),
            1,
            "transient failure may retry"
        );
    }

    #[test]
    fn idle_sweep_drops_stale_slots_only() {
        let t0 = Instant::now();
        let p = WarmPool::<u32>::new(2, 8, Duration::from_secs(30));
        assert_eq!(fill(&p, "stale", t0, 1), 2);
        assert_eq!(fill(&p, "busy", t0, 10), 2);
        let later = t0 + Duration::from_secs(40);
        p.take("busy", later).unwrap(); // marks busy as recently used
        let dropped = p.sweep(later);
        assert_eq!(
            dropped.len(),
            2,
            "only the idle digest's entries are dropped"
        );
        assert_eq!(p.ready_len("stale"), 0);
        assert_eq!(p.ready_len("busy"), 1);
    }

    #[test]
    fn put_beyond_the_bound_is_refused() {
        let now = Instant::now();
        let p = pool(1, 8);
        let t = p.reserve_fills("d", now);
        p.put(&t[0], 1, now).unwrap();
        assert_eq!(
            p.put(&t[0], 2, now),
            Err(2),
            "a duplicate put cannot exceed the bound"
        );
    }
}
