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

use crate::partition::{parse_executor_id, ExecutorClass, LanguageClass, Placement};
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
    class_of_executor: BTreeMap<String, ExecutorClass>,
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
        let executors: Vec<String> = sessions_by_executor.keys().cloned().collect();
        let class_of_executor = executors
            .iter()
            .map(|e| (e.clone(), parse_executor_id(e).0))
            .collect();
        Self {
            mode,
            replicas: replicas.max(1),
            all,
            executors,
            sessions_by_executor,
            missing,
            class_of_executor,
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

    /// Number of live sessions the router was built over.
    pub fn session_count(&self) -> usize {
        self.all.len()
    }

    /// Distinct live executor ids, sorted.
    pub fn executors(&self) -> &[String] {
        &self.executors
    }

    /// Distinct live executors eligible for `placement`, sorted: every executor for
    /// [`Placement::Unpartitioned`], those whose class accepts the bundle class for
    /// [`Placement::Class`], none for [`Placement::UnknownLanguage`] (fail closed).
    pub fn eligible_executors(&self, placement: Placement) -> Vec<String> {
        self.executors
            .iter()
            .filter(|e| self.executor_accepts(e, placement))
            .cloned()
            .collect()
    }

    fn executor_accepts(&self, executor: &str, placement: Placement) -> bool {
        match placement {
            Placement::Unpartitioned => true,
            Placement::UnknownLanguage => false,
            Placement::Class(c) => self
                .class_of_executor
                .get(executor)
                .copied()
                .unwrap_or_default()
                .accepts(c),
        }
    }

    /// The owner executor ids of `app_id` among the executors eligible for `placement`
    /// (empty when not sharded).
    pub fn owner_executors(&self, app_id: &str, placement: Placement) -> Vec<String> {
        if self.mode != RoutingMode::Sharded {
            return Vec::new();
        }
        let eligible = self.eligible_executors(placement);
        owners(slot(app_id), &eligible, self.replicas)
            .into_iter()
            .map(str::to_string)
            .collect()
    }

    /// Every live session `app_id` must be loaded on / may be dispatched to, sorted: all
    /// sessions of its owner executors (among those eligible for `placement`) when sharded,
    /// every eligible live session otherwise. A session that reported no executor id predates
    /// the handshake (and the partition) and counts as class `any`. Empty when nothing
    /// eligible is live -- callers treat that as unroutable and fail loud, never fall back.
    pub fn target_sessions(&self, app_id: &str, placement: Placement) -> Vec<SessionId> {
        if self.mode != RoutingMode::Sharded {
            let mut out: Vec<SessionId> = self.missing.clone();
            if placement == Placement::UnknownLanguage {
                return Vec::new();
            }
            for (e, sessions) in &self.sessions_by_executor {
                if self.executor_accepts(e, placement) {
                    out.extend(sessions.iter().copied());
                }
            }
            out.sort_unstable();
            return out;
        }
        let mut out: Vec<SessionId> = self
            .owner_executors(app_id, placement)
            .iter()
            .filter_map(|e| self.sessions_by_executor.get(e))
            .flatten()
            .copied()
            .collect();
        out.sort_unstable();
        out
    }

    /// Whether `session` is a target for `app_id` (see [`Self::target_sessions`]).
    pub fn is_target(&self, session: SessionId, app_id: &str, placement: Placement) -> bool {
        self.target_sessions(app_id, placement)
            .binary_search(&session)
            .is_ok()
    }

    /// Whether any live session could host a bundle of `placement` (always true for
    /// [`Placement::Unpartitioned`] when anything is live).
    pub fn has_eligible_session(&self, placement: Placement) -> bool {
        !self.target_sessions("", placement).is_empty()
    }
}

/// Remembers the last routing decision across ticks so a stage logs a
/// routing change (WARN for the missing-executor-id broadcast fallback)
/// once per change, never per tick, and counts slot-owner moves when the
/// executor membership changes.
#[derive(Debug, Clone, Default)]
pub struct RoutingObserver {
    last: Option<(RoutingMode, Vec<SessionId>)>,
    owner_table: Vec<Vec<String>>,
    unroutable_last: BTreeMap<&'static str, u64>,
}

impl RoutingObserver {
    /// Fail-loud partition check: counts, per [`Placement::label`], the active bundles that
    /// no live session can host -- a language in no class, or a class with no live eligible
    /// executor -- and logs at ERROR whenever that set changes (not per tick; the caller's
    /// counter metric carries the per-tick rate). Returns the per-label counts of THIS tick.
    /// Nothing is counted while no session is live at all (the stage's existing
    /// no-executor handling owns that outage). `bundles` yields `(app_id, language)`.
    pub fn observe_unroutable<'a>(
        &mut self,
        router: &SessionRouter,
        table: &crate::partition::LanguageTable,
        bundles: impl IntoIterator<Item = (&'a str, &'a str)>,
    ) -> BTreeMap<&'static str, u64> {
        let mut counts: BTreeMap<&'static str, u64> = BTreeMap::new();
        let mut sample: Vec<String> = Vec::new();
        if !router.all.is_empty() {
            for (app, language) in bundles {
                let placement = table.placement(language);
                if !router.has_eligible_session(placement) {
                    *counts.entry(placement.label()).or_default() += 1;
                    if sample.len() < 5 {
                        sample.push(format!("{app}({language})"));
                    }
                }
            }
        }
        if counts != self.unroutable_last {
            if counts.is_empty() {
                tracing::info!("executor partition: every active bundle is routable again");
            } else {
                tracing::error!(
                    unroutable = ?counts,
                    sample = ?sample,
                    "executor partition: active bundles with NO eligible executor -- a language \
                     in no class, or no live executor of the bundle's class; they are NOT loaded \
                     anywhere (never placed on the other class)"
                );
            }
            self.unroutable_last = counts.clone();
        }
        counts
    }

    /// Observes this tick's `router`; returns how many slots changed owner
    /// set since the previous sharded membership (0 on the first sharded
    /// tick and in broadcast modes) for `executor_slot_owner_changes_total`.
    pub fn observe(&mut self, router: &SessionRouter) -> u64 {
        let mode = router.mode();
        let current = Some((mode, router.sessions_missing_executor_id().to_vec()));
        if self.last != current {
            match mode {
                RoutingMode::BroadcastMissingExecutorId => tracing::warn!(
                    sessions_without_executor_id = ?router.sessions_missing_executor_id(),
                    "executor sharding: a live executor sent no executor_id in its handshake \
                     (older executor); falling back to broadcast Load to every session"
                ),
                RoutingMode::BroadcastDisabled => tracing::warn!(
                    "executor sharding disabled by kill switch \
                     waddles.disable-executor-sharding; broadcasting every bundle to every executor"
                ),
                RoutingMode::Sharded => tracing::info!(
                    executors = router.executors().len(),
                    replicas = router.replicas,
                    "executor sharding active: each app loads only on its owner executors"
                ),
            }
            self.last = current;
        }
        if mode != RoutingMode::Sharded {
            return 0;
        }
        // Unpartitioned fleet (every executor `any`): one table. Partitioned: one table per
        // class, so a scripted join registers without disturbing the compiled count.
        let partitioned = router
            .class_of_executor
            .values()
            .any(|c| *c != ExecutorClass::Any);
        let table = if partitioned {
            let mut t = slot_owner_table(
                &router.eligible_executors(Placement::Class(LanguageClass::Compiled)),
                router.replicas,
            );
            t.extend(slot_owner_table(
                &router.eligible_executors(Placement::Class(LanguageClass::Scripted)),
                router.replicas,
            ));
            t
        } else {
            slot_owner_table(router.executors(), router.replicas)
        };
        if table == self.owner_table {
            return 0;
        }
        let changed = if self.owner_table.is_empty() {
            0
        } else {
            count_owner_changes(&self.owner_table, &table)
        };
        if changed > 0 {
            tracing::info!(
                changed_slots = changed,
                executors = router.executors().len(),
                "executor sharding: membership changed, slot owners moved"
            );
        }
        self.owner_table = table;
        changed
    }
}

/// Age of a Valkey stream entry from its id (`<ms>-<seq>`) relative to
/// `now_ms` (unix millis) -- the stage-side queue wait behind
/// `queue_wait_seconds`. `None` for a malformed id; a clock-skewed future
/// id reads as zero wait, never negative.
pub fn stream_entry_age(entry_id: &str, now_ms: u128) -> Option<Duration> {
    let ms: u128 = entry_id.split('-').next()?.parse().ok()?;
    let age = now_ms.saturating_sub(ms);
    Some(Duration::from_millis(
        u64::try_from(age).unwrap_or(u64::MAX),
    ))
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
        let shrunk: Vec<String> = execs(n)
            .into_iter()
            .filter(|e| !e.ends_with("-3"))
            .collect();
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
            let owners = r.owner_executors(app, Placement::Unpartitioned);
            assert_eq!(owners.len(), 2);
            let targets = r.target_sessions(app, Placement::Unpartitioned);
            let want: Vec<SessionId> = ids
                .iter()
                .filter(|(_, e)| owners.iter().any(|o| o == e))
                .map(|(s, _)| *s)
                .collect();
            assert_eq!(targets, want, "{app}");
            assert!(targets
                .iter()
                .all(|s| r.is_target(*s, app, Placement::Unpartitioned)));
        }
    }

    /// Broadcast fallback ONLY when an executor id is absent: one old
    /// executor without an id forces every app onto every live session.
    #[test]
    fn router_broadcasts_when_an_executor_id_is_absent() {
        let r = router(&[1, 2, 3], &[(1, "e0"), (2, "e1")], true);
        assert_eq!(r.mode(), RoutingMode::BroadcastMissingExecutorId);
        assert_eq!(r.sessions_missing_executor_id(), &[3]);
        assert_eq!(
            r.target_sessions("waddles.ping", Placement::Unpartitioned),
            vec![1, 2, 3]
        );
        assert!(r
            .owner_executors("waddles.ping", Placement::Unpartitioned)
            .is_empty());
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
        assert_eq!(
            r.target_sessions("x", Placement::Unpartitioned),
            vec![1, 2, 3]
        );
    }

    #[test]
    fn routing_mode_labels_are_stable() {
        assert_eq!(RoutingMode::Sharded.as_str(), "sharded");
        assert_eq!(
            RoutingMode::BroadcastDisabled.as_str(),
            "broadcast_disabled"
        );
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

    /// The observer counts owner moves only on a sharded membership change.
    #[test]
    fn routing_observer_counts_owner_changes_on_membership_change() {
        let mut obs = RoutingObserver::default();
        let three = [(1, "e0"), (2, "e1"), (3, "e2")];
        assert_eq!(obs.observe(&router(&[1, 2, 3], &three, true)), 0);
        assert_eq!(obs.observe(&router(&[1, 2, 3], &three, true)), 0);
        let four = [(1, "e0"), (2, "e1"), (3, "e2"), (4, "e3")];
        let moved = obs.observe(&router(&[1, 2, 3, 4], &four, true));
        assert!(moved > 0 && moved < u64::from(SLOT_COUNT), "{moved}");
        assert_eq!(obs.observe(&router(&[1, 2], &[(1, "e0")], true)), 0);
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

    fn qualified(class: ExecutorClass, n: usize) -> Vec<String> {
        (0..n)
            .map(|i| crate::partition::qualify_executor_id(class, &format!("exec-{i}")))
            .collect()
    }

    /// Router over compiled executors (sessions 1..) then scripted (sessions 11..).
    fn partitioned(nc: usize, ns: usize, enabled: bool) -> SessionRouter {
        let mut map = HashMap::new();
        let mut live = Vec::new();
        for (i, id) in qualified(ExecutorClass::Compiled, nc)
            .into_iter()
            .enumerate()
        {
            map.insert(1 + i as u64, id);
            live.push(1 + i as u64);
        }
        for (i, id) in qualified(ExecutorClass::Scripted, ns)
            .into_iter()
            .enumerate()
        {
            map.insert(11 + i as u64, id);
            live.push(11 + i as u64);
        }
        SessionRouter::new(&live, &map, enabled, DEFAULT_REPLICAS)
    }

    const COMPILED: Placement = Placement::Class(LanguageClass::Compiled);
    const SCRIPTED: Placement = Placement::Class(LanguageClass::Scripted);

    /// Sharded: a compiled bundle lands ONLY on compiled executors, scripted ONLY on scripted --
    /// for every one of 400 app ids, in both directions.
    #[test]
    fn sharded_placement_never_crosses_classes() {
        let r = partitioned(3, 3, true);
        assert_eq!(r.mode(), RoutingMode::Sharded);
        for i in 0..400 {
            let app = format!("app.{i}");
            let c = r.target_sessions(&app, COMPILED);
            let s = r.target_sessions(&app, SCRIPTED);
            assert_eq!(c.len(), 2, "{app}: R=2 owners");
            assert_eq!(s.len(), 2);
            assert!(c.iter().all(|x| (1..=3).contains(x)), "{c:?}");
            assert!(s.iter().all(|x| (11..=13).contains(x)), "{s:?}");
        }
    }

    /// Kill-switch / broadcast modes still honour the partition: eligible sessions only.
    #[test]
    fn broadcast_modes_still_filter_by_class() {
        let r = partitioned(2, 2, false);
        assert_eq!(r.mode(), RoutingMode::BroadcastDisabled);
        assert_eq!(r.target_sessions("a", COMPILED), vec![1, 2]);
        assert_eq!(r.target_sessions("a", SCRIPTED), vec![11, 12]);
        assert_eq!(
            r.target_sessions("a", Placement::Unpartitioned),
            vec![1, 2, 11, 12]
        );
    }

    /// An unknown language, or a class with no live executor, routes to NOTHING (fail closed,
    /// never to the other class).
    #[test]
    fn unroutable_placements_yield_no_targets() {
        let only_compiled = partitioned(2, 0, true);
        assert!(only_compiled.target_sessions("a", SCRIPTED).is_empty());
        assert!(!only_compiled.has_eligible_session(SCRIPTED));
        assert!(only_compiled.has_eligible_session(COMPILED));
        assert!(only_compiled
            .target_sessions("a", Placement::UnknownLanguage)
            .is_empty());
        let r = partitioned(2, 2, false);
        assert!(r
            .target_sessions("a", Placement::UnknownLanguage)
            .is_empty());
    }

    /// An unqualified (`any` / pre-partition) executor hosts both classes.
    #[test]
    fn any_executors_host_both_classes() {
        let ids = [(1, "exec-0"), (2, "exec-1")];
        let r = router(&[1, 2], &ids, true);
        assert_eq!(r.target_sessions("x", COMPILED).len(), 2);
        assert_eq!(r.target_sessions("x", SCRIPTED).len(), 2);
    }

    /// A scripted executor joining or leaving never moves a compiled bundle's owners.
    #[test]
    fn scripted_membership_change_does_not_move_compiled_owners() {
        let before = partitioned(3, 2, true);
        let after = partitioned(3, 3, true);
        for i in 0..300 {
            let app = format!("app.{i}");
            assert_eq!(
                before.target_sessions(&app, COMPILED),
                after.target_sessions(&app, COMPILED)
            );
        }
    }

    /// The observer counts owner moves per class table, so a scripted join registers.
    #[test]
    fn observer_counts_partitioned_owner_moves() {
        let mut obs = RoutingObserver::default();
        assert_eq!(obs.observe(&partitioned(3, 2, true)), 0);
        assert!(obs.observe(&partitioned(3, 3, true)) > 0);
    }

    /// Fail-loud accounting: an unknown language and a class with no live executor are both
    /// counted per placement label; a fully routable set counts nothing; no live session at
    /// all counts nothing (the no-executor outage is reported elsewhere).
    #[test]
    fn observe_unroutable_counts_by_placement_label() {
        let table = crate::partition::LanguageTable::default();
        let bundles = [("a", "rust"), ("b", "python"), ("c", "cobol"), ("d", "go")];
        let mut obs = RoutingObserver::default();
        let only_compiled = partitioned(2, 0, true);
        let counts = obs.observe_unroutable(&only_compiled, &table, bundles);
        assert_eq!(counts.get("scripted"), Some(&1));
        assert_eq!(counts.get("unknown_language"), Some(&1));
        assert_eq!(counts.get("compiled"), None);
        let both = partitioned(2, 2, true);
        let counts = obs.observe_unroutable(&both, &table, [("a", "rust"), ("b", "python")]);
        assert!(counts.is_empty());
        let none = SessionRouter::new(&[], &HashMap::new(), true, DEFAULT_REPLICAS);
        assert!(obs.observe_unroutable(&none, &table, bundles).is_empty());
    }
}
