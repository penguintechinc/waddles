//! Bounded per-app concurrency for a stage's `drain_batch` (latency suspect #4 in
//! `docs/observability/command-latency.md`).
//!
//! Before this, each per-app consumer handled its batch strictly serially
//! (`for d in &batch { handle_delivered(d).await? }`), so one slow command (a bundle that waits on
//! a host call) delayed every entry queued behind it -- head-of-line blocking that shows up in
//! p99, not p50.
//!
//! ```text
//!   batch --group by ordering key (arrival order kept inside a group)--> groups
//!   groups --up to `concurrency` at a time--> each group handled strictly in order
//! ```
//!
//! **Ordering (safe by default).** Bundle kv/db/economy have no atomic compare-and-set, so two
//! concurrent messages for the SAME app scope can lose updates (counters, economy, raffles). The
//! default key is therefore the app scope `(tenant, community, app)`: entries of one scope are
//! always handled one after the other, in arrival order; concurrency exists only ACROSS scopes
//! (and across apps, which run as separate consumers). Only a bundle that is provably stateless
//! (its approved manifest declares no capability outside [`STATELESS_SAFE_CAPABILITIES`]) may
//! additionally overlap per actor, and only when `APP_DRAIN_ORDERING=actor` opts in; anything
//! unknown or stateful stays scope-serial ([`drain_key`]).
//!
//! **Failure semantics are unchanged.** The first error in a group stops THAT group (its
//! remaining entries stay un-acked and are redelivered, exactly as the serial loop's early
//! return did) and is returned; other groups already in flight run to completion first, so no
//! handler is ever cancelled mid-ack.

use std::collections::HashMap;
use std::future::Future;
use std::hash::Hash;

use futures_util::stream::{self, StreamExt};
use thiserror::Error;

/// Env var: max groups handled concurrently per drained batch. `1` = the legacy serial loop.
pub const ENV_CONCURRENCY: &str = "APP_DRAIN_CONCURRENCY";
/// Env var: ordering key (`scope` default, or `actor` for provably stateless bundles).
pub const ENV_ORDERING: &str = "APP_DRAIN_ORDERING";
/// Default concurrency when [`ENV_CONCURRENCY`] is unset.
pub const DEFAULT_CONCURRENCY: usize = 4;
/// Hard ceiling: more in-flight handlers than this per app never helps and only risks pool
/// exhaustion at the executor.
pub const MAX_CONCURRENCY: usize = 64;

/// Which entries must stay ordered relative to each other.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Ordering {
    /// Entries of one app scope `(tenant, community, app)` never overlap (default; safe for
    /// stateful bundles).
    #[default]
    Scope,
    /// Like `Scope`, except a provably stateless bundle may also overlap per actor.
    Actor,
}

/// Capabilities a bundle may declare and still count as stateless: none of them gives it durable
/// shared state. Anything else (`kv`, `db`, `economy`, `reputation`, `storage`, platform-table,
/// ...), or an empty/unknown declaration, is treated as stateful.
pub const STATELESS_SAFE_CAPABILITIES: [&str; 6] =
    ["context", "flags", "log", "clock", "http", "relay"];

/// Whether an approved capability set is provably stateless. An EMPTY set is NOT stateless: the
/// manifest summary is absent or unparsed, so the safe answer is "assume state".
#[must_use]
pub fn is_stateless(declared_capabilities: &[String]) -> bool {
    !declared_capabilities.is_empty()
        && declared_capabilities
            .iter()
            .all(|c| STATELESS_SAFE_CAPABILITIES.contains(&c.as_str()))
}

/// The key two entries must share to be handled strictly in arrival order.
pub type DrainKey = (String, Option<String>, String, Option<String>);

/// Builds an entry's [`DrainKey`]: always `(tenant, community, app)`; the actor is added (so
/// different actors may overlap) ONLY when `ordering` is [`Ordering::Actor`] AND the bundle is
/// `stateless`.
#[must_use]
pub fn drain_key(
    tenant: &str,
    community: Option<&str>,
    app_id: &str,
    actor: Option<&str>,
    ordering: Ordering,
    stateless: bool,
) -> DrainKey {
    let actor = (ordering == Ordering::Actor && stateless)
        .then(|| actor.map(str::to_string))
        .flatten();
    (
        tenant.to_string(),
        community.map(str::to_string),
        app_id.to_string(),
        actor,
    )
}

/// Drain tuning resolved once at startup.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DrainTuning {
    /// Max groups handled at once (`1` = serial).
    pub concurrency: usize,
    /// Ordering key mode.
    pub ordering: Ordering,
}

impl Default for DrainTuning {
    fn default() -> Self {
        Self {
            concurrency: DEFAULT_CONCURRENCY,
            ordering: Ordering::Scope,
        }
    }
}

/// Invalid drain tuning (a typo must refuse to start, never silently fall back).
#[derive(Debug, Error, PartialEq, Eq)]
pub enum TuningError {
    /// `APP_DRAIN_CONCURRENCY` was not an integer in `1..=MAX_CONCURRENCY`.
    #[error("{ENV_CONCURRENCY} {0:?} is not an integer in 1..={MAX_CONCURRENCY}")]
    Concurrency(String),
    /// `APP_DRAIN_ORDERING` was not `scope` or `actor`.
    #[error("{ENV_ORDERING} {0:?} is not `scope` or `actor`")]
    Ordering(String),
}

impl DrainTuning {
    /// Parses the two env values (`None` = unset = default).
    pub fn parse(concurrency: Option<&str>, ordering: Option<&str>) -> Result<Self, TuningError> {
        let mut t = Self::default();
        if let Some(raw) = concurrency.map(str::trim).filter(|v| !v.is_empty()) {
            let n: usize = raw
                .parse()
                .map_err(|_| TuningError::Concurrency(raw.to_string()))?;
            if n == 0 || n > MAX_CONCURRENCY {
                return Err(TuningError::Concurrency(raw.to_string()));
            }
            t.concurrency = n;
        }
        if let Some(raw) = ordering.map(str::trim).filter(|v| !v.is_empty()) {
            t.ordering = match raw.to_ascii_lowercase().as_str() {
                "scope" => Ordering::Scope,
                "actor" => Ordering::Actor,
                other => return Err(TuningError::Ordering(other.to_string())),
            };
        }
        Ok(t)
    }

    /// Reads [`ENV_CONCURRENCY`] / [`ENV_ORDERING`].
    pub fn from_env() -> Result<Self, TuningError> {
        Self::parse(
            std::env::var(ENV_CONCURRENCY).ok().as_deref(),
            std::env::var(ENV_ORDERING).ok().as_deref(),
        )
    }
}

static TUNING: std::sync::OnceLock<DrainTuning> = std::sync::OnceLock::new();

/// Installs the process-wide tuning from the environment (first install wins). Stages call this
/// at startup and refuse to start on `Err`.
pub fn init_tuning_from_env() -> Result<DrainTuning, TuningError> {
    let t = DrainTuning::from_env()?;
    Ok(*TUNING.get_or_init(|| t))
}

/// The process-wide tuning (the default until [`init_tuning_from_env`] ran).
#[must_use]
pub fn tuning() -> DrainTuning {
    *TUNING.get_or_init(DrainTuning::default)
}

/// Runs `handle` over `items`, up to `concurrency` ordering-groups at a time (see the module
/// doc). `key_of` assigns each item its ordering key. Returns the first error after every group
/// already running has finished; `Ok(())` when all succeeded. `concurrency <= 1` (or a batch of
/// at most one) is exactly the legacy serial loop with its early return.
pub async fn run_keyed<'a, I, K, E, F, Fut>(
    items: &'a [I],
    concurrency: usize,
    key_of: impl Fn(&I) -> K,
    handle: F,
) -> Result<(), E>
where
    K: Hash + Eq,
    F: Fn(&'a I) -> Fut,
    Fut: Future<Output = Result<(), E>>,
{
    if concurrency <= 1 || items.len() <= 1 {
        for item in items {
            handle(item).await?;
        }
        return Ok(());
    }
    let mut group_of: HashMap<K, usize> = HashMap::new();
    let mut groups: Vec<Vec<usize>> = Vec::new();
    for (i, item) in items.iter().enumerate() {
        let next = groups.len();
        let g = *group_of.entry(key_of(item)).or_insert(next);
        if g == next {
            groups.push(Vec::new());
        }
        groups[g].push(i);
    }
    let handle = &handle;
    let results: Vec<Result<(), E>> = stream::iter(groups)
        .map(|group| async move {
            for i in group {
                handle(&items[i]).await?;
            }
            Ok(())
        })
        .buffer_unordered(concurrency)
        .collect()
        .await;
    results.into_iter().collect()
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering as AO};
    use std::sync::{Arc, Mutex};
    use std::time::Duration;

    type Item = (&'static str, u32);

    #[tokio::test]
    async fn same_key_entries_run_in_arrival_order_and_never_overlap() {
        let items: Vec<Item> = vec![("a", 1), ("a", 2), ("a", 3), ("b", 1), ("b", 2)];
        let log: Arc<Mutex<Vec<String>>> = Arc::default();
        let active_a = Arc::new(AtomicUsize::new(0));
        let max_a = Arc::new(AtomicUsize::new(0));
        run_keyed(
            &items,
            4,
            |i| i.0,
            |i| {
                let log = Arc::clone(&log);
                let (active_a, max_a) = (Arc::clone(&active_a), Arc::clone(&max_a));
                let it = *i;
                async move {
                    if it.0 == "a" {
                        let now = active_a.fetch_add(1, AO::SeqCst) + 1;
                        max_a.fetch_max(now, AO::SeqCst);
                    }
                    tokio::time::sleep(Duration::from_millis(5)).await;
                    log.lock().unwrap().push(format!("{}{}", it.0, it.1));
                    if it.0 == "a" {
                        active_a.fetch_sub(1, AO::SeqCst);
                    }
                    Ok::<(), String>(())
                }
            },
        )
        .await
        .unwrap();
        let log = log.lock().unwrap().clone();
        assert_eq!(log.len(), 5, "every item handled exactly once");
        let pos = |s: &str| log.iter().position(|x| x == s).unwrap();
        assert!(pos("a1") < pos("a2") && pos("a2") < pos("a3"), "{log:?}");
        assert!(pos("b1") < pos("b2"), "{log:?}");
        assert_eq!(max_a.load(AO::SeqCst), 1, "one key never overlaps itself");
    }

    /// The point of the change: a slow entry no longer blocks entries of OTHER keys behind it.
    #[tokio::test]
    async fn a_slow_entry_does_not_block_other_keys() {
        let items: Vec<Item> = vec![("slow", 1), ("fast", 1), ("fast2", 1)];
        let started = std::time::Instant::now();
        let done: Arc<Mutex<Vec<&'static str>>> = Arc::default();
        run_keyed(
            &items,
            4,
            |i| i.0,
            |i| {
                let done = Arc::clone(&done);
                let name = i.0;
                async move {
                    let ms = if name == "slow" { 150 } else { 5 };
                    tokio::time::sleep(Duration::from_millis(ms)).await;
                    done.lock().unwrap().push(name);
                    Ok::<(), String>(())
                }
            },
        )
        .await
        .unwrap();
        assert_eq!(
            done.lock().unwrap().last(),
            Some(&"slow"),
            "fast keys finished first"
        );
        assert!(
            started.elapsed() < Duration::from_millis(250),
            "overlapped, not serial"
        );
    }

    #[tokio::test]
    async fn concurrency_is_bounded() {
        let items: Vec<(u32, u32)> = (0..12).map(|i| (i, 0)).collect();
        let active = Arc::new(AtomicUsize::new(0));
        let peak = Arc::new(AtomicUsize::new(0));
        run_keyed(
            &items,
            3,
            |i| i.0,
            |_| {
                let (active, peak) = (Arc::clone(&active), Arc::clone(&peak));
                async move {
                    let now = active.fetch_add(1, AO::SeqCst) + 1;
                    peak.fetch_max(now, AO::SeqCst);
                    tokio::time::sleep(Duration::from_millis(10)).await;
                    active.fetch_sub(1, AO::SeqCst);
                    Ok::<(), String>(())
                }
            },
        )
        .await
        .unwrap();
        assert_eq!(
            peak.load(AO::SeqCst),
            3,
            "exactly the configured bound, never above"
        );
    }

    /// Failure semantics: the first error is returned, the failing group stops (its tail is not
    /// handled -> redelivered), other groups already running finish.
    #[tokio::test]
    async fn an_error_stops_its_group_returns_first_error_and_lets_others_finish() {
        let items: Vec<Item> = vec![("a", 1), ("a", 2), ("b", 1), ("b", 2)];
        let handled: Arc<Mutex<Vec<String>>> = Arc::default();
        let r = run_keyed(
            &items,
            4,
            |i| i.0,
            |i| {
                let handled = Arc::clone(&handled);
                let it = *i;
                async move {
                    tokio::time::sleep(Duration::from_millis(5)).await;
                    handled.lock().unwrap().push(format!("{}{}", it.0, it.1));
                    if it == ("a", 1) {
                        return Err("boom".to_string());
                    }
                    Ok(())
                }
            },
        )
        .await;
        assert_eq!(r, Err("boom".to_string()));
        let h = handled.lock().unwrap().clone();
        assert!(
            !h.contains(&"a2".to_string()),
            "failed group's tail not handled: {h:?}"
        );
        assert!(
            h.contains(&"b1".to_string()) && h.contains(&"b2".to_string()),
            "{h:?}"
        );
    }

    #[tokio::test]
    async fn concurrency_one_is_the_legacy_serial_loop_with_early_return() {
        let items: Vec<Item> = vec![("a", 1), ("b", 1), ("c", 1)];
        let handled: Arc<Mutex<Vec<&'static str>>> = Arc::default();
        let r = run_keyed(
            &items,
            1,
            |i| i.0,
            |i| {
                let handled = Arc::clone(&handled);
                let name = i.0;
                async move {
                    handled.lock().unwrap().push(name);
                    if name == "b" {
                        Err("stop")
                    } else {
                        Ok(())
                    }
                }
            },
        )
        .await;
        assert_eq!(r, Err("stop"));
        assert_eq!(*handled.lock().unwrap(), vec!["a", "b"], "c never reached");
    }

    #[tokio::test]
    async fn empty_and_single_batches_are_trivial() {
        let none: Vec<Item> = vec![];
        run_keyed(&none, 4, |i| i.0, |_| async { Ok::<(), String>(()) })
            .await
            .unwrap();
        let one: Vec<Item> = vec![("a", 1)];
        run_keyed(&one, 4, |i| i.0, |_| async { Ok::<(), String>(()) })
            .await
            .unwrap();
    }

    #[test]
    fn tuning_parse_is_strict() {
        assert_eq!(
            DrainTuning::parse(None, None).unwrap(),
            DrainTuning::default()
        );
        let t = DrainTuning::parse(Some(" 8 "), Some("Actor")).unwrap();
        assert_eq!((t.concurrency, t.ordering), (8, Ordering::Actor));
        assert_eq!(
            DrainTuning::default().ordering,
            Ordering::Scope,
            "safe default"
        );
        for bad in ["0", "65", "-1", "four", "1.5"] {
            assert_eq!(
                DrainTuning::parse(Some(bad), None),
                Err(TuningError::Concurrency(bad.to_string())),
                "{bad}"
            );
        }
        assert_eq!(
            DrainTuning::parse(None, Some("stream")),
            Err(TuningError::Ordering("stream".to_string()))
        );
        assert_eq!(
            DrainTuning::parse(Some(""), Some("")).unwrap(),
            DrainTuning::default()
        );
    }

    #[test]
    fn process_tuning_defaults_without_env() {
        assert!(tuning().concurrency >= 1);
    }

    fn caps(v: &[&str]) -> Vec<String> {
        v.iter().map(|s| (*s).to_string()).collect()
    }

    #[test]
    fn only_provably_stateless_capability_sets_count_as_stateless() {
        assert!(is_stateless(&caps(&[
            "context", "log", "clock", "flags", "http", "relay"
        ])));
        assert!(!is_stateless(&caps(&["context", "kv"])), "kv is state");
        for c in [
            "db",
            "economy",
            "reputation",
            "storage",
            "platform-table",
            "mystery",
        ] {
            assert!(
                !is_stateless(&caps(&["context", c])),
                "{c} must be stateful"
            );
        }
        assert!(
            !is_stateless(&[]),
            "an empty/unknown declaration is assumed stateful"
        );
    }

    /// Two messages for the same stateful app scope share a key under EVERY mode, so they never
    /// overlap; a different community (scope) or a different app has a different key.
    #[test]
    fn stateful_app_scope_is_always_one_key_and_other_scopes_are_not() {
        for mode in [Ordering::Scope, Ordering::Actor] {
            let a = drain_key("t1", Some("c1"), "app", Some("alice"), mode, false);
            let b = drain_key("t1", Some("c1"), "app", Some("bob"), mode, false);
            assert_eq!(
                a, b,
                "{mode:?}: stateful bundle serial per scope across actors"
            );
            assert_ne!(
                a,
                drain_key("t1", Some("c2"), "app", Some("alice"), mode, false)
            );
            assert_ne!(
                a,
                drain_key("t1", Some("c1"), "other", Some("alice"), mode, false)
            );
            assert_ne!(
                a,
                drain_key("t2", Some("c1"), "app", Some("alice"), mode, false)
            );
        }
        // Default mode never splits by actor even for a stateless bundle.
        assert_eq!(
            drain_key("t", None, "app", Some("a"), Ordering::Scope, true),
            drain_key("t", None, "app", Some("b"), Ordering::Scope, true)
        );
    }

    #[test]
    fn stateless_opt_in_overlaps_per_actor_only() {
        let a = drain_key("t", Some("c"), "app", Some("alice"), Ordering::Actor, true);
        let b = drain_key("t", Some("c"), "app", Some("bob"), Ordering::Actor, true);
        assert_ne!(
            a, b,
            "different actors of a stateless opted-in bundle overlap"
        );
        assert_eq!(
            a,
            drain_key("t", Some("c"), "app", Some("alice"), Ordering::Actor, true),
            "one actor stays ordered"
        );
    }

    /// End to end through `run_keyed`: same stateful scope never overlaps; different apps and
    /// scopes do; stateless opt-in overlaps per actor.
    #[tokio::test]
    async fn run_keyed_with_drain_keys_serialises_stateful_scopes_and_overlaps_the_rest() {
        type Msg = (&'static str, &'static str, &'static str, &'static str); // community, app, actor, id
        async fn peak(items: Vec<Msg>, ordering: Ordering, stateless: bool) -> usize {
            let active = Arc::new(AtomicUsize::new(0));
            let peak = Arc::new(AtomicUsize::new(0));
            run_keyed(
                &items,
                8,
                |m: &Msg| drain_key("t", Some(m.0), m.1, Some(m.2), ordering, stateless),
                |_| {
                    let (active, peak) = (Arc::clone(&active), Arc::clone(&peak));
                    async move {
                        let now = active.fetch_add(1, AO::SeqCst) + 1;
                        peak.fetch_max(now, AO::SeqCst);
                        tokio::time::sleep(Duration::from_millis(15)).await;
                        active.fetch_sub(1, AO::SeqCst);
                        Ok::<(), String>(())
                    }
                },
            )
            .await
            .unwrap();
            peak.load(AO::SeqCst)
        }
        let same_scope = vec![
            ("c", "app", "alice", "1"),
            ("c", "app", "bob", "2"),
            ("c", "app", "carol", "3"),
        ];
        assert_eq!(peak(same_scope.clone(), Ordering::Scope, false).await, 1);
        assert_eq!(
            peak(same_scope.clone(), Ordering::Actor, false).await,
            1,
            "stateful: opt-in ignored"
        );
        assert_eq!(
            peak(same_scope, Ordering::Actor, true).await,
            3,
            "stateless opt-in overlaps actors"
        );
        let other_scopes = vec![
            ("c1", "app", "a", "1"),
            ("c2", "app", "a", "2"),
            ("c1", "other", "a", "3"),
        ];
        assert_eq!(
            peak(other_scopes, Ordering::Scope, false).await,
            3,
            "different scopes/apps overlap"
        );
    }
}
