//! State-machine tests of [`platform_keys::KeyCache`] over a fake loader and a manual clock:
//! lazy load, negative cache, fail-closed lookups, last-known on outage, `verify_until`
//! expiry, staleness cap, readiness/seed, polling and the emitted metrics.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime};

use async_trait::async_trait;
use opentelemetry::metrics::MeterProvider;
use opentelemetry_sdk::metrics::data::{AggregatedMetrics, MetricData};
use opentelemetry_sdk::metrics::{InMemoryMetricExporter, PeriodicReader, SdkMeterProvider};
use platform_keys::{
    CacheConfig, Clock, KeyCache, KeyError, KeyLoader, KeyRecord, KeyState, LoaderError, SetupError,
};

const T: i16 = 7;
const EPOCH0: u64 = 1_000_000;

struct ManualClock(AtomicU64);
impl ManualClock {
    fn new() -> Arc<Self> {
        Arc::new(Self(AtomicU64::new(EPOCH0)))
    }
    fn advance(&self, secs: u64) {
        self.0.fetch_add(secs, Ordering::SeqCst);
    }
    fn at(secs: u64) -> SystemTime {
        SystemTime::UNIX_EPOCH + Duration::from_secs(secs)
    }
}
impl Clock for ManualClock {
    fn now(&self) -> SystemTime {
        Self::at(self.0.load(Ordering::SeqCst))
    }
}

#[derive(Default)]
struct Fake {
    rows: Mutex<Vec<KeyRecord>>,
    down: AtomicBool,
    fail_next: AtomicUsize,
    all_calls: AtomicUsize,
    kid_calls: AtomicUsize,
    /// When set, `load_kid` also returns this unrelated row (a misbehaving loader).
    stray: Mutex<Option<KeyRecord>>,
}
impl Fake {
    fn set(&self, rows: Vec<KeyRecord>) {
        *self.rows.lock().unwrap() = rows;
    }
    fn failing(&self) -> bool {
        if self.down.load(Ordering::SeqCst) {
            return true;
        }
        self.fail_next
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |n| n.checked_sub(1))
            .is_ok()
    }
}
#[async_trait]
impl KeyLoader for Fake {
    async fn load_all(&self) -> Result<Vec<KeyRecord>, LoaderError> {
        self.all_calls.fetch_add(1, Ordering::SeqCst);
        if self.failing() {
            return Err(LoaderError::Unavailable("down".into()));
        }
        Ok(self.rows.lock().unwrap().clone())
    }
    async fn load_kid(&self, kid: &str) -> Result<Vec<KeyRecord>, LoaderError> {
        self.kid_calls.fetch_add(1, Ordering::SeqCst);
        if self.failing() {
            return Err(LoaderError::Unavailable("down".into()));
        }
        let mut v: Vec<KeyRecord> = self
            .rows
            .lock()
            .unwrap()
            .iter()
            .filter(|r| r.kid == kid)
            .cloned()
            .collect();
        if let Some(s) = self.stray.lock().unwrap().clone() {
            v.push(s);
        }
        Ok(v)
    }
}

fn rec(key_type: i16, kid: &str, version: i32, state: KeyState, rotated: Option<u64>) -> KeyRecord {
    KeyRecord {
        key_type,
        scope_id: None,
        version,
        kid: kid.to_string(),
        state,
        public_material: vec![9; 32],
        activated_at: Some(ManualClock::at(EPOCH0 - 10)),
        rotated_at: rotated.map(ManualClock::at),
    }
}
fn active(kid: &str) -> KeyRecord {
    rec(T, kid, 1, KeyState::Active, None)
}

struct Harness {
    cache: Arc<KeyCache>,
    fake: Arc<Fake>,
    clock: Arc<ManualClock>,
    provider: SdkMeterProvider,
    exporter: InMemoryMetricExporter,
}

fn harness_with(cfg: impl FnOnce(&mut CacheConfig)) -> Harness {
    let fake = Arc::new(Fake::default());
    let clock = ManualClock::new();
    let exporter = InMemoryMetricExporter::default();
    let provider = SdkMeterProvider::builder()
        .with_reader(PeriodicReader::builder(exporter.clone()).build())
        .build();
    let mut c = CacheConfig::new(vec![T], Duration::from_secs(3600));
    cfg(&mut c);
    let cache =
        KeyCache::with_parts(fake.clone(), c, clock.clone(), &provider.meter("test")).unwrap();
    Harness {
        cache: Arc::new(cache),
        fake,
        clock,
        provider,
        exporter,
    }
}
fn harness() -> Harness {
    harness_with(|_| {})
}

impl Harness {
    /// Highest cumulative counter sum for `name` whose attributes include every `filter` pair.
    fn counter(&self, name: &str, filter: &[(&str, &str)]) -> u64 {
        self.provider.force_flush().unwrap();
        let mut best = 0;
        for rm in self.exporter.get_finished_metrics().unwrap() {
            for sm in rm.scope_metrics() {
                for m in sm.metrics() {
                    if m.name() != name {
                        continue;
                    }
                    if let AggregatedMetrics::U64(MetricData::Sum(s)) = m.data() {
                        for dp in s.data_points() {
                            let ok = filter.iter().all(|(k, v)| {
                                dp.attributes()
                                    .any(|kv| kv.key.as_str() == *k && kv.value.to_string() == *v)
                            });
                            if ok {
                                best = best.max(dp.value());
                            }
                        }
                    }
                }
            }
        }
        best
    }

    fn histogram_count(&self, name: &str) -> u64 {
        self.provider.force_flush().unwrap();
        let mut best = 0;
        for rm in self.exporter.get_finished_metrics().unwrap() {
            for sm in rm.scope_metrics() {
                for m in sm.metrics() {
                    if m.name() == name {
                        if let AggregatedMetrics::F64(MetricData::Histogram(h)) = m.data() {
                            for dp in h.data_points() {
                                best = best.max(dp.count());
                            }
                        }
                    }
                }
            }
        }
        best
    }
}

#[tokio::test]
async fn lazy_load_then_served_from_cache() {
    let h = harness();
    h.fake.set(vec![active("k1")]);
    let got = h.cache.get(T, "k1").await.unwrap();
    assert_eq!(got.kid, "k1");
    assert_eq!(h.fake.kid_calls.load(Ordering::SeqCst), 1);
    h.cache.get(T, "k1").await.unwrap();
    assert_eq!(
        h.fake.kid_calls.load(Ordering::SeqCst),
        1,
        "second get is a cache hit"
    );
    assert_eq!(
        h.counter("platform_keys_cache_hits_total", &[("key_type", "7")]),
        1
    );
    assert_eq!(
        h.counter("platform_keys_cache_misses_total", &[("key_type", "7")]),
        1
    );
}

#[tokio::test]
async fn unknown_kid_fails_closed_with_30s_negative_cache() {
    let h = harness();
    assert_eq!(h.cache.get(T, "nope").await, Err(KeyError::UnknownKid));
    assert_eq!(h.cache.get(T, "nope").await, Err(KeyError::UnknownKid));
    assert_eq!(
        h.fake.kid_calls.load(Ordering::SeqCst),
        1,
        "negative cache absorbed the retry"
    );
    h.clock.advance(29);
    assert_eq!(h.cache.get(T, "nope").await, Err(KeyError::UnknownKid));
    assert_eq!(h.fake.kid_calls.load(Ordering::SeqCst), 1);
    h.clock.advance(2);
    assert_eq!(h.cache.get(T, "nope").await, Err(KeyError::UnknownKid));
    assert_eq!(
        h.fake.kid_calls.load(Ordering::SeqCst),
        2,
        "entry expired after 30s"
    );
    assert!(h.counter("platform_keys_unknown_kid_total", &[]) >= 3);
    assert!(h.counter("platform_keys_rejected_total", &[("reason", "unknown_kid")]) >= 3);
}

#[tokio::test]
async fn refresh_clears_a_negative_entry_for_a_new_key() {
    let h = harness();
    assert!(h.cache.get(T, "late").await.is_err());
    h.fake.set(vec![active("late")]);
    h.cache.refresh().await.unwrap();
    assert_eq!(h.cache.get(T, "late").await.unwrap().kid, "late");
}

#[tokio::test]
async fn empty_and_overlong_kids_never_reach_the_store() {
    let h = harness();
    assert_eq!(h.cache.get(T, "").await, Err(KeyError::UnknownKid));
    let long = "a".repeat(129);
    assert_eq!(h.cache.get(T, &long).await, Err(KeyError::UnknownKid));
    assert_eq!(h.fake.kid_calls.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn type_is_part_of_the_key() {
    let h = harness();
    h.fake.set(vec![active("k1")]);
    h.cache.refresh().await.unwrap();
    assert_eq!(h.cache.get(T + 1, "k1").await, Err(KeyError::UnknownKid));
}

#[tokio::test]
async fn store_down_and_not_cached_is_unavailable_and_not_negative_cached() {
    let h = harness();
    h.fake.down.store(true, Ordering::SeqCst);
    assert_eq!(h.cache.get(T, "k1").await, Err(KeyError::Unavailable));
    assert_eq!(h.cache.get(T, "k1").await, Err(KeyError::Unavailable));
    assert_eq!(
        h.fake.kid_calls.load(Ordering::SeqCst),
        2,
        "transient failure is retried"
    );
    assert!(h.counter("platform_keys_refresh_failures_total", &[]) >= 2);
    h.fake.down.store(false, Ordering::SeqCst);
    h.fake.set(vec![active("k1")]);
    assert!(
        h.cache.get(T, "k1").await.is_ok(),
        "recovers once the store is back"
    );
}

#[tokio::test]
async fn keeps_last_known_keys_when_the_store_goes_down() {
    let h = harness();
    h.fake.set(vec![active("k1")]);
    assert_eq!(h.cache.refresh().await.unwrap(), 1);
    h.fake.down.store(true, Ordering::SeqCst);
    assert_eq!(h.cache.refresh().await, Err(KeyError::Unavailable));
    assert_eq!(h.cache.get(T, "k1").await.unwrap().kid, "k1");
    assert!(h.cache.ready());
    assert!(h.counter("platform_keys_refresh_failures_total", &[]) >= 1);
    assert!(h.histogram_count("platform_keys_refresh_duration_seconds") >= 1);
    assert_eq!(h.counter("platform_keys_refreshes_total", &[]), 1);
}

#[tokio::test]
async fn verify_only_key_is_honoured_until_verify_until_then_refused() {
    let h = harness_with(|c| c.verify_window = Duration::from_secs(100));
    let rotated = EPOCH0 + 10;
    h.fake
        .set(vec![rec(T, "old", 1, KeyState::VerifyOnly, Some(rotated))]);
    h.cache.refresh().await.unwrap();
    h.clock.advance(10 + 100); // now == rotated + window exactly
    assert!(
        h.cache.get(T, "old").await.is_ok(),
        "boundary instant still verifies"
    );
    h.clock.advance(1);
    assert_eq!(h.cache.get(T, "old").await, Err(KeyError::Expired));
    assert!(h.counter("platform_keys_rejected_total", &[("reason", "expired")]) >= 1);
}

#[tokio::test]
async fn verify_only_key_without_rotated_at_fails_closed() {
    let h = harness();
    h.fake
        .set(vec![rec(T, "old", 1, KeyState::VerifyOnly, None)]);
    h.cache.refresh().await.unwrap();
    assert_eq!(h.cache.get(T, "old").await, Err(KeyError::Expired));
    assert!(
        !h.cache.ready(),
        "an unusable key does not satisfy readiness"
    );
}

#[tokio::test]
async fn stale_snapshot_refuses_verify_only_but_not_active_keys() {
    let h = harness(); // window 1h, staleness cap 15 min
    h.fake.set(vec![
        active("cur"),
        rec(T, "old", 1, KeyState::VerifyOnly, Some(EPOCH0)),
    ]);
    h.cache.refresh().await.unwrap();
    h.fake.down.store(true, Ordering::SeqCst);
    h.clock.advance(15 * 60);
    assert!(
        h.cache.get(T, "old").await.is_ok(),
        "exactly at the cap is still fresh"
    );
    h.clock.advance(1);
    assert_eq!(h.cache.get(T, "old").await, Err(KeyError::Stale));
    assert!(h.cache.get(T, "cur").await.is_ok());
    assert!(h.counter("platform_keys_rejected_total", &[("reason", "stale")]) >= 1);
}

#[tokio::test]
async fn a_retired_key_disappears_at_the_next_refresh() {
    let h = harness();
    h.fake.set(vec![active("a"), active("b")]);
    h.cache.refresh().await.unwrap();
    h.fake.set(vec![active("b")]);
    h.cache.refresh().await.unwrap();
    assert!(h.cache.get(T, "b").await.is_ok());
    assert_eq!(h.cache.get(T, "a").await, Err(KeyError::UnknownKid));
}

#[tokio::test]
async fn unusable_rows_are_skipped_and_highest_version_wins() {
    let h = harness();
    let mut empty = active("empty");
    empty.public_material.clear();
    let mut v2 = active("dup");
    v2.version = 2;
    v2.public_material = vec![2; 32];
    let v1 = active("dup");
    let mut nokid = active("");
    nokid.version = 3;
    let mut big = active(&"z".repeat(200));
    big.version = 3;
    h.fake.set(vec![empty, v2.clone(), v1, nokid, big]);
    assert_eq!(h.cache.refresh().await.unwrap(), 1);
    assert_eq!(
        h.cache.get(T, "dup").await.unwrap().public_material,
        v2.public_material
    );
    assert!(h.cache.get(T, "empty").await.is_err());
}

#[tokio::test]
async fn lazy_load_ignores_rows_for_other_kids() {
    let h = harness();
    *h.fake.stray.lock().unwrap() = Some(active("intruder"));
    assert_eq!(h.cache.get(T, "wanted").await, Err(KeyError::UnknownKid));
    h.fake.set(vec![active("wanted")]);
    assert!(
        h.cache.get(T, "wanted").await.is_err(),
        "still negatively cached"
    );
    h.clock.advance(31);
    assert!(h.cache.get(T, "wanted").await.is_ok());
    // The stray row was never cached under its own kid.
    h.fake.set(vec![]);
    *h.fake.stray.lock().unwrap() = None;
    assert_eq!(h.cache.get(T, "intruder").await, Err(KeyError::UnknownKid));
}

#[tokio::test]
async fn negative_cache_is_bounded() {
    let h = harness_with(|c| c.negative_cap = 2);
    for kid in ["a", "b", "c"] {
        assert!(h.cache.get(T, kid).await.is_err());
    }
    assert_eq!(h.fake.kid_calls.load(Ordering::SeqCst), 3);
    // Inserting "c" hit the cap and cleared the map, so "a" is forgotten; "c" is remembered.
    assert!(h.cache.get(T, "c").await.is_err());
    assert_eq!(h.fake.kid_calls.load(Ordering::SeqCst), 3);
    assert!(h.cache.get(T, "a").await.is_err());
    assert_eq!(h.fake.kid_calls.load(Ordering::SeqCst), 4);
    // Expired entries are pruned before the map is cleared.
    h.clock.advance(31);
    for kid in ["d", "e", "f"] {
        assert!(h.cache.get(T, kid).await.is_err());
    }
}

#[tokio::test]
async fn readiness_requires_every_required_type() {
    let h = harness_with(|c| c.required_types = vec![T, T + 1]);
    assert!(!h.cache.ready());
    assert_eq!(h.cache.missing_types(), vec![T, T + 1]);
    h.fake.set(vec![active("a")]);
    h.cache.refresh().await.unwrap();
    assert_eq!(h.cache.missing_types(), vec![T + 1]);
    h.fake.set(vec![
        active("a"),
        rec(T + 1, "b", 1, KeyState::Active, None),
    ]);
    h.cache.refresh().await.unwrap();
    assert!(h.cache.ready());
}

#[tokio::test(start_paused = true)]
async fn seed_retries_with_bounded_backoff_until_ready() {
    let h = harness();
    h.fake.set(vec![active("k")]);
    h.fake.fail_next.store(3, Ordering::SeqCst);
    h.cache.seed(Duration::from_secs(60)).await.unwrap();
    assert_eq!(h.fake.all_calls.load(Ordering::SeqCst), 4);
    assert!(h.cache.ready());
}

#[tokio::test(start_paused = true)]
async fn seed_waits_for_a_missing_type_then_gives_up_loudly() {
    let h = harness();
    // The store is reachable but has no key of the required type.
    h.fake
        .set(vec![rec(T + 5, "other", 1, KeyState::Active, None)]);
    let err = h.cache.seed(Duration::from_secs(10)).await.unwrap_err();
    match err {
        SetupError::NotReady { missing, attempts } => {
            assert_eq!(missing, vec![T]);
            assert!(attempts >= 3, "attempts={attempts}");
        }
        other => panic!("unexpected {other:?}"),
    }
    assert!(!h.cache.ready(), "no fallback source ever fills the cache");
    // Backoff is capped: ~10s at <=5s steps cannot exceed a handful of attempts.
    assert!(h.fake.all_calls.load(Ordering::SeqCst) <= 8);
}

#[tokio::test(start_paused = true)]
async fn seed_with_the_store_down_fails_not_ready() {
    let h = harness();
    h.fake.down.store(true, Ordering::SeqCst);
    let err = h.cache.seed(Duration::from_secs(3)).await.unwrap_err();
    assert!(matches!(err, SetupError::NotReady { .. }));
    assert!(h.counter("platform_keys_refresh_failures_total", &[]) >= 2);
}

#[test]
fn empty_required_types_is_rejected() {
    let fake = Arc::new(Fake::default());
    let provider = SdkMeterProvider::builder().build();
    let res = KeyCache::with_parts(
        fake,
        CacheConfig::new(vec![], Duration::from_secs(1)),
        ManualClock::new(),
        &provider.meter("t"),
    );
    assert!(matches!(res, Err(SetupError::Config(_))));
}

#[tokio::test]
async fn new_uses_the_global_meter_and_system_clock() {
    let fake = Arc::new(Fake::default());
    fake.set(vec![KeyRecord {
        activated_at: None,
        ..active("sys")
    }]);
    let cache = KeyCache::new(fake, CacheConfig::new(vec![T], Duration::from_secs(60))).unwrap();
    assert!(cache.get(T, "sys").await.is_ok());
}

#[tokio::test(start_paused = true)]
async fn poller_refreshes_periodically_and_stops_on_signal() {
    let h = harness();
    h.fake.set(vec![active("k")]);
    let (stop, handle) = h.cache.spawn_poller();
    tokio::time::sleep(Duration::from_secs(61)).await;
    assert_eq!(h.fake.all_calls.load(Ordering::SeqCst), 1);
    assert!(h.cache.ready());
    tokio::time::sleep(Duration::from_secs(60)).await;
    assert_eq!(h.fake.all_calls.load(Ordering::SeqCst), 2);
    stop.send(true).unwrap();
    handle.await.unwrap();
    tokio::time::sleep(Duration::from_secs(300)).await;
    assert_eq!(
        h.fake.all_calls.load(Ordering::SeqCst),
        2,
        "no refresh after stop"
    );
}

#[tokio::test(start_paused = true)]
async fn poller_stops_when_the_sender_is_dropped() {
    let h = harness();
    let (stop, handle) = h.cache.spawn_poller();
    drop(stop);
    handle.await.unwrap();
}

#[tokio::test]
async fn concurrent_lookups_of_one_unknown_kid_cost_one_query() {
    let h = harness();
    h.fake.set(vec![active("burst")]);
    let mut tasks = Vec::new();
    for _ in 0..16 {
        let c = Arc::clone(&h.cache);
        tasks.push(tokio::spawn(async move { c.get(T, "burst").await.is_ok() }));
    }
    for t in tasks {
        assert!(t.await.unwrap());
    }
    assert_eq!(h.fake.kid_calls.load(Ordering::SeqCst), 1);
}

#[test]
fn hostile_kid_in_logs_is_sanitised_by_construction() {
    // The kid reaches the cache only through `get`; a newline-bearing kid is just an unknown kid.
    let rt = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    let h = harness();
    let res = rt.block_on(h.cache.get(T, "evil\nforged=1"));
    assert_eq!(res, Err(KeyError::UnknownKid));
}
