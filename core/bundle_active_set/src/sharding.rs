//! Executor slot sharding (executor scale-out phase 1, task T6) -- shared by
//! `core/svc_process` and `core/svc_action` so both stages compute the SAME
//! app -> executor placement from the same live membership, with no
//! coordinator.
//!
//! ```text
//!   app_id --xxh64 % 1024--> slot --rendezvous top-R over live executor_ids--> owners
//!   owners --(every live session of an owner executor)--> Load / dispatch targets
//! ```
//!
//! **Why:** before this module every active `(tenant, community, app)` was
//! `Load`ed onto EVERY live executor session (broadcast), so every executor
//! compiled and kept resident every bundle -- the dal2alpha OOM. With
//! sharding each app lives on [`DEFAULT_REPLICAS`] executors only; adding or
//! removing one executor moves only ~R/N of the slots (rendezvous hashing).
//!
//! **Fail-closed to broadcast:** an executor that predates the
//! `HelloBody.executor_id` handshake field cannot be placed, so if ANY live
//! session lacks an executor id the router falls back to the legacy
//! broadcast ([`RoutingMode::BroadcastMissingExecutorId`]) -- callers log
//! that at WARN. The opt-out kill switch `waddles.disable-executor-sharding`
//! (unseen = sharding ON) yields [`RoutingMode::BroadcastDisabled`].

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::time::Duration;

use xxhash_rust::xxh64::xxh64;

use crate::session_sync::SessionId;

/// Fixed number of hash slots (user decision 2026-11-12: 1024, small map,
/// bounded metric label cardinality).
pub const SLOT_COUNT: u16 = 1024;

/// Executors per slot (user decision 2026-11-12: R=2 for HA + read spread).
pub const DEFAULT_REPLICAS: usize = 2;

/// Seed for the slot hash -- fixed forever: changing it reshuffles every
/// app's placement across the whole fleet.
const SLOT_SEED: u64 = 0;

/// Seed for the rendezvous score, distinct from [`SLOT_SEED`] so slot and
/// score are not correlated.
const RENDEZVOUS_SEED: u64 = 0x5741_4444_4c45_5321;

/// The hash slot `app_id` belongs to: `xxh64(app_id) % 1024`. Pure and
/// stable across processes, versions and architectures (xxh64 is defined
/// over bytes, little-endian independent).
pub fn slot(app_id: &str) -> u16 {
    // `% 1024` always fits in u16; the fallback is unreachable.
    u16::try_from(xxh64(app_id.as_bytes(), SLOT_SEED) % u64::from(SLOT_COUNT)).unwrap_or(0)
}

/// Rendezvous (highest-random-weight) score of `executor_id` for `slot`.
fn score(slot: u16, executor_id: &str) -> u64 {
    let mut buf = Vec::with_capacity(2 + executor_id.len());
    buf.extend_from_slice(&slot.to_le_bytes());
    buf.extend_from_slice(executor_id.as_bytes());
    xxh64(&buf, RENDEZVOUS_SEED)
}

/// The top-`replicas` owners of `slot` among `executors`, highest score
/// first (ties broken by id so the order is total and deterministic).
/// Duplicate ids in `executors` are counted once. Fewer than `replicas`
/// executors -> every executor owns the slot.
pub fn owners<'a>(slot: u16, executors: &'a [String], replicas: usize) -> Vec<&'a str> {
    let unique: BTreeSet<&'a str> = executors.iter().map(String::as_str).collect();
    let mut scored: Vec<(u64, &'a str)> = unique.into_iter().map(|e| (score(slot, e), e)).collect();
    scored.sort_unstable_by(|a, b| b.0.cmp(&a.0).then_with(|| a.1.cmp(b.1)));
    scored.into_iter().take(replicas).map(|(_, e)| e).collect()
}

/// The full owner table for every slot -- `table[slot]` is that slot's owner
/// set, sorted by id (so two tables compare by set, not by score order).
pub fn slot_owner_table(executors: &[String], replicas: usize) -> Vec<Vec<String>> {
    (0..SLOT_COUNT)
        .map(|s| {
            let mut o: Vec<String> = owners(s, executors, replicas)
                .into_iter()
                .map(str::to_string)
                .collect();
            o.sort_unstable();
            o
        })
        .collect()
}

/// How many slots changed owner set between two [`slot_owner_table`]s --
/// backs `executor_slot_owner_changes_total`. Tables of different length
/// (never produced by this module) count every slot as changed.
pub fn count_owner_changes(old: &[Vec<String>], new: &[Vec<String>]) -> u64 {
    if old.len() != new.len() {
        return u64::try_from(new.len().max(old.len())).unwrap_or(u64::MAX);
    }
    let changed = old.iter().zip(new).filter(|(a, b)| a != b).count();
    u64::try_from(changed).unwrap_or(u64::MAX)
}

/// How a [`SessionRouter`] decided to place bundles this tick.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RoutingMode {
    /// Each app goes only to the sessions of its owner executors.
    Sharded,
    /// Kill switch `waddles.disable-executor-sharding` is ON: legacy
    /// broadcast to every live session.
    BroadcastDisabled,
    /// At least one live session did not report an executor id (an executor
    /// older than the handshake field): legacy broadcast, logged at WARN.
    BroadcastMissingExecutorId,
}

impl RoutingMode {
    /// Stable label for logs/metrics.
    pub fn as_str(self) -> &'static str {
        match self {
            RoutingMode::Sharded => "sharded",
            RoutingMode::BroadcastDisabled => "broadcast_disabled",
            RoutingMode::BroadcastMissingExecutorId => "broadcast_missing_executor_id",
        }
    }
}

/// One tick's placement decision over the live session set: which sessions
/// each app must be loaded on / dispatched to.
#[derive(Debug, Clone)]
pub struct SessionRouter {
    mode: RoutingMode,
    replicas: usize,
    all: Vec<SessionId>,
    executors: Vec<String>,
    sessions_by_executor: BTreeMap<String, Vec<SessionId>>,
    missing: Vec<SessionId>,
}

impl SessionRouter {
    /// Builds the router for `live` sessions. `executor_of` maps a session to
    /// the executor id it reported in its handshake (absent = old executor).
    /// `sharding_enabled` is the already-negated kill switch (true = shard).
    pub fn new(
        live: &[SessionId],
        executor_of: &HashMap<SessionId, String>,
        sharding_enabled: bool,
        replicas: usize,
    ) -> Self {
        let mut all: Vec<SessionId> = live.to_vec();
        all.sort_unstable();
        all.dedup();
        let mut sessions_by_executor: BTreeMap<String, Vec<SessionId>> = BTreeMap::new();
        let mut missing = Vec::new();
        for &s in &all {
            match executor_of.get(&s).filter(|id| !id.is_empty()) {
                Some(id) => sessions_by_executor.entry(id.clone()).or_default().push(s),
                None => missing.push(s),
            }
        }
        let mode = if !sharding_enabled {
            RoutingMode::BroadcastDisabled
        } else if !missing.is_empty() {
            RoutingMode::BroadcastMissingExecutorId
        } else {
            RoutingMode::Sharded
        };
        let executors = sessions_by_executor.keys().cloned().collect();
        Self {
            mode,
            replicas: replicas.max(1),
            all,
            executors,
            sessions_by_executor,
            missing,
        }
    }

    /// The decision this router made.
    pub fn mode(&self) -> RoutingMode {
        self.mode
    }

    /// Live sessions that reported no executor id (empty unless the mode is
    /// [`RoutingMode::BroadcastMissingExecutorId`] or the switch is off).
    pub fn sessions_missing_executor_id(&self) -> &[SessionId] {
        &self.missing
    }

    /// Distinct live executor ids, sorted.
    pub fn executors(&self) -> &[String] {
        &self.executors
    }

    /// The owner executor ids of `app_id` (empty when not sharded).
    pub fn owner_executors(&self, app_id: &str) -> Vec<String> {
        if self.mode != RoutingMode::Sharded {
            return Vec::new();
        }
        owners(slot(app_id), &self.executors, self.replicas)
            .into_iter()
            .map(str::to_string)
            .collect()
    }

    /// Every live session `app_id` must be loaded on / may be dispatched to,
    /// sorted: all sessions of its owner executors when sharded, every live
    /// session otherwise.
    pub fn target_sessions(&self, app_id: &str) -> Vec<SessionId> {
        if self.mode != RoutingMode::Sharded {
            return self.all.clone();
        }
        let mut out: Vec<SessionId> = self
            .owner_executors(app_id)
            .iter()
            .filter_map(|e| self.sessions_by_executor.get(e))
            .flatten()
            .copied()
            .collect();
        out.sort_unstable();
        out
    }

    /// Whether `session` is a target for `app_id` (see [`Self::target_sessions`]).
    pub fn is_target(&self, session: SessionId, app_id: &str) -> bool {
        self.target_sessions(app_id).binary_search(&session).is_ok()
    }
}

/// The optional `executor_id` an executor reports in its handshake
/// (`penguin_bundle_host::wire::HelloBody.executor_id`, a serde-defaulted
/// back-compat field). Read through the body's own serde form so stages
/// compile and behave identically against penguin-libs revisions with and
/// without the field: absent, null or empty -> `None` (broadcast fallback).
pub fn hello_executor_id<H: serde::Serialize>(hello: &H) -> Option<String> {
    serde_json::to_value(hello)
        .ok()?
        .get("executor_id")?
        .as_str()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
}

/// Age of a Valkey stream entry from its id (`<ms>-<seq>`) relative to
/// `now_ms` (unix millis) -- the stage-side queue wait behind
/// `queue_wait_seconds`. `None` for a malformed id; a clock-skewed future
/// id reads as zero wait, never negative.
pub fn stream_entry_age(entry_id: &str, now_ms: u128) -> Option<Duration> {
    let ms: u128 = entry_id.split('-').next()?.parse().ok()?;
    let age = now_ms.saturating_sub(ms);
    Some(Duration::from_millis(u64::try_from(age).unwrap_or(u64::MAX)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn execs(n: usize) -> Vec<String> {
        (0..n)
            .map(|i| format!("waddlebot-svc-bundles-executor-{i}"))
            .collect()
    }

    /// slot() is a pure function of app_id: stable values (pinned so a hash
    /// or seed change that would reshuffle the fleet fails this test) and
    /// always < 1024.
    #[test]
    fn slot_is_stable_and_in_range() {
        assert_eq!(slot("waddles.ping"), slot("waddles.ping"));
        let pinned = [slot("waddles.ping"), slot("waddles.csping"), slot("")];
        assert_eq!(
            pinned,
            [
                (xxh64(b"waddles.ping", 0) % 1024) as u16,
                (xxh64(b"waddles.csping", 0) % 1024) as u16,
                (xxh64(b"", 0) % 1024) as u16,
            ]
        );
        // xxh64("") with seed 0 is a published test vector.
        assert_eq!(xxh64(b"", 0), 0xEF46_DB37_51D8_E999);
        for i in 0..5000 {
            assert!(slot(&format!("app.{i}")) < SLOT_COUNT);
        }
    }

    /// Owners are deterministic regardless of the input order and the two
    /// replicas are distinct executors.
    #[test]
    fn owners_are_deterministic_and_two_replicas_are_distinct() {
        let a = execs(5);
        let mut b = a.clone();
        b.reverse();
        for s in 0..SLOT_COUNT {
            let oa = owners(s, &a, 2);
            assert_eq!(oa, owners(s, &b, 2), "slot {s}");
            assert_eq!(oa.len(), 2);
            assert_ne!(oa[0], oa[1], "slot {s} replicas must differ");
        }
    }

    /// Fewer executors than replicas: every executor owns every slot.
    #[test]
    fn owners_with_one_executor_is_that_executor() {
        let one = execs(1);
        assert_eq!(owners(7, &one, 2), vec![one[0].as_str()]);
        assert!(owners(7, &[], 2).is_empty());
    }

    /// Rendezvous property: growing N -> N+1 moves only the slots the new
    /// executor wins (~R/(N+1)), and every moved slot now includes it.
    #[test]
    fn rendezvous_moves_about_one_nth_of_slots_on_resize() {
        let n = 10;
        let before = slot_owner_table(&execs(n), 1);
        let grown = execs(n + 1);
        let after = slot_owner_table(&grown, 1);
        let moved = count_owner_changes(&before, &after);
        let expected = f64::from(SLOT_COUNT) / (n as f64 + 1.0); // ~93
        assert!(
            (moved as f64) > expected * 0.5 && (moved as f64) < expected * 1.6,
            "moved {moved}, expected ~{expected}"
        );
        let newcomer = &grown[n];
        for (s, (b, a)) in before.iter().zip(&after).enumerate() {
            if b != a {
                assert_eq!(&a[0], newcomer, "slot {s} moved to a non-new executor");
            }
        }
        // R=2: removal of one executor only touches slots it owned.
        let r2_before = slot_owner_table(&execs(n), 2);
        let shrunk: Vec<String> = execs(n).into_iter().filter(|e| !e.ends_with("-3")).collect();
        let r2_after = slot_owner_table(&shrunk, 2);
        for (b, a) in r2_before.iter().zip(&r2_after) {
            if b != a {
                assert!(b.iter().any(|e| e.ends_with("-3")));
            }
        }
        let moved2 = count_owner_changes(&r2_before, &r2_after) as f64;
        let exp2 = 2.0 * f64::from(SLOT_COUNT) / n as f64; // ~205
        assert!(moved2 > exp2 * 0.6 && moved2 < exp2 * 1.4, "moved {moved2}");
    }

    /// Load is spread: with 4 executors and R=2 every executor owns roughly
    /// half the slots.
    #[test]
    fn slots_are_spread_evenly() {
        let e = execs(4);
        let table = slot_owner_table(&e, 2);
        for id in &e {
            let owned = table.iter().filter(|o| o.contains(id)).count();
            assert!((400..=624).contains(&owned), "{id} owns {owned}");
        }
    }

    fn router(live: &[SessionId], ids: &[(SessionId, &str)], enabled: bool) -> SessionRouter {
        let map = ids.iter().map(|(s, e)| (*s, e.to_string())).collect();
        SessionRouter::new(live, &map, enabled, DEFAULT_REPLICAS)
    }

    /// Sharded: an app targets exactly the sessions of its two owners
    /// (every session of each owner, since one executor dials one session
    /// per stage connection).
    #[test]
    fn router_targets_only_owner_executor_sessions() {
        let ids = [(1, "e0"), (2, "e0"), (3, "e1"), (4, "e2"), (5, "e3")];
        let r = router(&[1, 2, 3, 4, 5], &ids, true);
        assert_eq!(r.mode(), RoutingMode::Sharded);
        for app in ["waddles.ping", "a", "b", "c", "d", "e"] {
            let owners = r.owner_executors(app);
            assert_eq!(owners.len(), 2);
            let targets = r.target_sessions(app);
            let want: Vec<SessionId> = ids
                .iter()
                .filter(|(_, e)| owners.iter().any(|o| o == e))
                .map(|(s, _)| *s)
                .collect();
            assert_eq!(targets, want, "{app}");
            assert!(targets.iter().all(|s| r.is_target(*s, app)));
        }
    }

    /// Broadcast fallback ONLY when an executor id is absent: one old
    /// executor without an id forces every app onto every live session.
    #[test]
    fn router_broadcasts_when_an_executor_id_is_absent() {
        let r = router(&[1, 2, 3], &[(1, "e0"), (2, "e1")], true);
        assert_eq!(r.mode(), RoutingMode::BroadcastMissingExecutorId);
        assert_eq!(r.sessions_missing_executor_id(), &[3]);
        assert_eq!(r.target_sessions("waddles.ping"), vec![1, 2, 3]);
        assert!(r.owner_executors("waddles.ping").is_empty());
        // An empty id counts as absent too.
        let r = router(&[1], &[(1, "")], true);
        assert_eq!(r.mode(), RoutingMode::BroadcastMissingExecutorId);
    }

    /// Kill switch ON (sharding disabled) -> legacy broadcast even when every
    /// executor reported an id.
    #[test]
    fn router_kill_switch_forces_broadcast() {
        let r = router(&[1, 2, 3], &[(1, "e0"), (2, "e1"), (3, "e2")], false);
        assert_eq!(r.mode(), RoutingMode::BroadcastDisabled);
        assert_eq!(r.target_sessions("x"), vec![1, 2, 3]);
    }

    #[test]
    fn routing_mode_labels_are_stable() {
        assert_eq!(RoutingMode::Sharded.as_str(), "sharded");
        assert_eq!(RoutingMode::BroadcastDisabled.as_str(), "broadcast_disabled");
        assert_eq!(
            RoutingMode::BroadcastMissingExecutorId.as_str(),
            "broadcast_missing_executor_id"
        );
    }

    #[test]
    fn count_owner_changes_handles_length_mismatch() {
        assert_eq!(count_owner_changes(&[], &[vec!["a".into()]]), 1);
        assert_eq!(count_owner_changes(&[vec![]], &[vec![]]), 0);
    }

    #[derive(serde::Serialize)]
    struct NewHello {
        protocol_version: u32,
        executor_id: Option<String>,
    }

    #[derive(serde::Serialize)]
    struct OldHello {
        protocol_version: u32,
    }

    /// executor_id is read when present and None for old executors.
    #[test]
    fn hello_executor_id_is_optional() {
        let new = NewHello {
            protocol_version: 1,
            executor_id: Some("waddlebot-svc-bundles-executor-0".into()),
        };
        assert_eq!(
            hello_executor_id(&new).as_deref(),
            Some("waddlebot-svc-bundles-executor-0")
        );
        let none = NewHello {
            protocol_version: 1,
            executor_id: None,
        };
        assert_eq!(hello_executor_id(&none), None);
        let blank = NewHello {
            protocol_version: 1,
            executor_id: Some("  ".into()),
        };
        assert_eq!(hello_executor_id(&blank), None);
        assert_eq!(hello_executor_id(&OldHello { protocol_version: 1 }), None);
    }

    #[test]
    fn stream_entry_age_parses_the_ms_prefix() {
        assert_eq!(
            stream_entry_age("1000-0", 1500),
            Some(Duration::from_millis(500))
        );
        assert_eq!(stream_entry_age("2000-3", 1500), Some(Duration::ZERO));
        assert_eq!(stream_entry_age("garbage", 1500), None);
    }
}
