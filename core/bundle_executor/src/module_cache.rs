//! Process-wide compiled-module cache bookkeeping: residency, pins, stage
//! references, a byte budget and least-recently-used eviction (executor-scale
//! CONTRACT tasks T1/T7).
//!
//! Pure data structure, generic over the cached value so the policy is unit
//! tested without compiling WASM; `crate::invoke::SharedRuntime` stores its
//! pre-linked components here.
//!
//! Three independent counts per digest:
//! - **resident**: a compiled value is in [`ModuleCache::entries`] and counts
//!   toward the budget;
//! - **pins**: in-flight invokes holding the value -- a pinned entry is never
//!   evicted;
//! - **refs**: how many per-stage registries have the digest `Load`ed. Refs
//!   outlive residency: an evicted-but-referenced digest is simply recompiled
//!   (or deserialized) on its next call (lazy load).
//!
//! Eviction order (only unpinned entries, never the entry just inserted):
//! unreferenced first, then least recently used. With `budget == 0`
//! (unlimited) nothing is evicted for space, and an entry is dropped as soon
//! as it is both unreferenced and unpinned, preserving the pre-cache
//! "unload frees memory" behaviour.

use std::collections::HashMap;

/// One resident module.
struct Entry<T> {
    value: T,
    bytes: u64,
    last_used: u64,
    pins: u32,
}

/// What an insert did to make room.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct InsertOutcome {
    /// Digests evicted to fit the new entry, in eviction order.
    pub evicted: Vec<String>,
    /// `true` when the cache is still above budget after evicting every
    /// evictable entry (everything else is pinned) -- the caller logs it.
    pub over_budget: bool,
}

/// The cache. Not internally synchronized; the owner wraps it in a mutex.
pub struct ModuleCache<T> {
    entries: HashMap<String, Entry<T>>,
    refs: HashMap<String, u32>,
    total_bytes: u64,
    budget_bytes: u64,
    clock: u64,
}

impl<T: Clone> ModuleCache<T> {
    /// An empty cache with `budget_bytes` (`0` = unlimited).
    #[must_use]
    pub fn new(budget_bytes: u64) -> Self {
        Self {
            entries: HashMap::new(),
            refs: HashMap::new(),
            total_bytes: 0,
            budget_bytes,
            clock: 0,
        }
    }

    fn tick(&mut self) -> u64 {
        self.clock += 1;
        self.clock
    }

    /// Resident entry count.
    #[must_use]
    pub fn len(&self) -> usize {
        self.entries.len()
    }

    /// `true` when nothing is resident.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    /// Bytes counted against the budget.
    #[must_use]
    pub fn total_bytes(&self) -> u64 {
        self.total_bytes
    }

    /// `true` when `digest` is resident.
    #[must_use]
    pub fn contains(&self, digest: &str) -> bool {
        self.entries.contains_key(digest)
    }

    /// `true` when a new module may be made resident without exceeding the
    /// budget (always for an unlimited budget). Used to decide eager vs lazy
    /// load.
    #[must_use]
    pub fn has_headroom(&self) -> bool {
        self.budget_bytes == 0 || self.total_bytes < self.budget_bytes
    }

    /// Pins and touches `digest`, returning its value when resident.
    pub fn get_pinned(&mut self, digest: &str) -> Option<T> {
        let now = self.tick();
        let entry = self.entries.get_mut(digest)?;
        entry.pins += 1;
        entry.last_used = now;
        Some(entry.value.clone())
    }

    /// Releases one pin. Drops the entry if it is now unpinned, unreferenced
    /// and the budget is unlimited.
    pub fn unpin(&mut self, digest: &str) {
        let now = self.tick();
        if let Some(entry) = self.entries.get_mut(digest) {
            entry.pins = entry.pins.saturating_sub(1);
            entry.last_used = now;
        }
        self.drop_if_dead(digest);
    }

    /// Records one more stage registry referencing `digest`.
    pub fn add_ref(&mut self, digest: &str) {
        *self.refs.entry(digest.to_string()).or_insert(0) += 1;
    }

    /// Records one stage registry releasing `digest`.
    pub fn release_ref(&mut self, digest: &str) {
        if let Some(n) = self.refs.get_mut(digest) {
            *n = n.saturating_sub(1);
            if *n == 0 {
                self.refs.remove(digest);
            }
        }
        self.drop_if_dead(digest);
    }

    /// Current stage-reference count for `digest`.
    #[must_use]
    pub fn refs(&self, digest: &str) -> u32 {
        self.refs.get(digest).copied().unwrap_or(0)
    }

    /// Current pin count for `digest` (0 when not resident).
    #[must_use]
    pub fn pins(&self, digest: &str) -> u32 {
        self.entries.get(digest).map_or(0, |e| e.pins)
    }

    fn drop_if_dead(&mut self, digest: &str) {
        if self.budget_bytes != 0 || self.refs(digest) > 0 {
            return;
        }
        if self.entries.get(digest).is_some_and(|e| e.pins == 0) {
            self.remove(digest);
        }
    }

    fn remove(&mut self, digest: &str) {
        if let Some(e) = self.entries.remove(digest) {
            self.total_bytes = self.total_bytes.saturating_sub(e.bytes);
        }
    }

    /// Inserts (or replaces nothing if already resident) `digest` with
    /// `bytes` of estimated resident cost, pinned once for the caller, then
    /// evicts unpinned entries until the budget holds.
    pub fn insert_pinned(&mut self, digest: &str, value: T, bytes: u64) -> (T, InsertOutcome) {
        if let Some(existing) = self.get_pinned(digest) {
            return (existing, InsertOutcome::default());
        }
        let now = self.tick();
        self.entries.insert(
            digest.to_string(),
            Entry {
                value: value.clone(),
                bytes,
                last_used: now,
                pins: 1,
            },
        );
        self.total_bytes = self.total_bytes.saturating_add(bytes);
        let outcome = self.enforce_budget(digest);
        (value, outcome)
    }

    /// Evicts unpinned entries (unreferenced first, then LRU), never `keep`,
    /// until `total_bytes <= budget_bytes`.
    fn enforce_budget(&mut self, keep: &str) -> InsertOutcome {
        let mut outcome = InsertOutcome::default();
        if self.budget_bytes == 0 {
            return outcome;
        }
        while self.total_bytes > self.budget_bytes {
            let victim = self
                .entries
                .iter()
                .filter(|(d, e)| e.pins == 0 && d.as_str() != keep)
                .min_by_key(|(d, e)| (self.refs.get(d.as_str()).copied().unwrap_or(0) > 0, e.last_used))
                .map(|(d, _)| d.clone());
            match victim {
                Some(d) => {
                    self.remove(&d);
                    outcome.evicted.push(d);
                }
                None => {
                    outcome.over_budget = true;
                    break;
                }
            }
        }
        outcome
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lru_evicts_the_least_recently_used_unpinned_entry() {
        let mut c = ModuleCache::new(100);
        for d in ["a", "b", "c"] {
            c.add_ref(d);
            c.insert_pinned(d, d.to_string(), 30);
            c.unpin(d);
        }
        // touch "a" so "b" is now the LRU entry
        c.get_pinned("a");
        c.unpin("a");
        c.add_ref("d");
        let (_, out) = c.insert_pinned("d", "d".to_string(), 30);
        assert_eq!(out.evicted, vec!["b".to_string()]);
        assert!(!out.over_budget);
        assert_eq!(c.total_bytes(), 90);
        assert!(c.contains("a") && c.contains("c") && c.contains("d"));
        // "b" stays referenced: it will be lazily reloaded on next call
        assert_eq!(c.refs("b"), 1);
    }

    #[test]
    fn pinned_entries_are_never_evicted() {
        let mut c = ModuleCache::new(50);
        c.insert_pinned("a", 1, 40); // stays pinned (in-flight invoke)
        let (_, out) = c.insert_pinned("b", 2, 40);
        assert!(out.evicted.is_empty());
        assert!(out.over_budget);
        assert!(c.contains("a") && c.contains("b"));
        assert_eq!(c.pins("a"), 1);
    }

    #[test]
    fn unreferenced_entries_go_before_referenced_ones() {
        let mut c = ModuleCache::new(60);
        c.add_ref("old_but_used");
        c.insert_pinned("old_but_used", 1, 30);
        c.unpin("old_but_used");
        c.insert_pinned("newer_unused", 2, 30);
        c.unpin("newer_unused");
        let (_, out) = c.insert_pinned("x", 3, 30);
        assert_eq!(out.evicted, vec!["newer_unused".to_string()]);
    }

    #[test]
    fn unlimited_budget_drops_dead_entries_immediately() {
        let mut c = ModuleCache::new(0);
        c.add_ref("a");
        c.insert_pinned("a", 1, 1 << 40);
        assert!(c.has_headroom());
        c.release_ref("a");
        assert!(c.contains("a"), "still pinned by the in-flight call");
        c.unpin("a");
        assert!(c.is_empty());
        assert_eq!(c.total_bytes(), 0);
    }

    #[test]
    fn bounded_budget_keeps_idle_unreferenced_entries_warm() {
        let mut c = ModuleCache::new(100);
        c.add_ref("a");
        c.insert_pinned("a", 1, 10);
        c.unpin("a");
        c.release_ref("a");
        assert!(c.contains("a"), "kept warm for a reconnecting stage");
        assert!(c.has_headroom());
    }

    #[test]
    fn double_insert_pins_the_existing_value() {
        let mut c = ModuleCache::new(0);
        c.insert_pinned("a", 1, 10);
        let (v, _) = c.insert_pinned("a", 2, 10);
        assert_eq!(v, 1);
        assert_eq!(c.pins("a"), 2);
        assert_eq!(c.total_bytes(), 10);
        assert_eq!(c.len(), 1);
    }

    #[test]
    fn headroom_reflects_the_budget() {
        let mut c = ModuleCache::new(10);
        assert!(c.has_headroom());
        c.insert_pinned("a", 1, 10);
        assert!(!c.has_headroom());
    }
}
