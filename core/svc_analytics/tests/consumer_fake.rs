//! Consumer behaviour against a REAL Valkey stream with a scriptable fake database:
//! validation/DLQ, exclusion drop + fail-closed, retry/poison handling, flag-off idle,
//! idle-entry claiming and maintenance scheduling.

mod support;

use std::sync::Arc;
use std::time::Duration;

use redis::aio::ConnectionManager;
use redis::AsyncCommands;
use support::*;
use svc_analytics::consumer::{Consumer, CycleError};
use svc_analytics::flag::FlagGate;
use svc_analytics::health::Ready;
use svc_analytics::jobs::{run_cohorts, run_purge, run_sweep, Jobs};
use svc_analytics::metrics::Metrics;
use tokio::sync::watch;
use uuid::Uuid;

async fn conn() -> ConnectionManager {
    ConnectionManager::new(redis::Client::open(valkey_url()).unwrap())
        .await
        .unwrap()
}

fn now_ms() -> i64 {
    svc_analytics::consumer::now_ms()
}

async fn add(c: &mut ConnectionManager, stream: &str, fields: &[(&str, String)]) {
    let _: String = c.xadd(stream, "*", fields).await.unwrap();
}

fn ev(u: Uuid, kind: u8, community: u32) -> Vec<(&'static str, String)> {
    vec![
        ("v", "1".into()),
        ("tenant_id", "1".into()),
        ("community_id", community.to_string()),
        ("user_uuid", u.to_string()),
        ("platform_code", "2".into()),
        ("kind_code", kind.to_string()),
        ("ts_ms", now_ms().to_string()),
        ("provenance_code", "2".into()),
    ]
}

async fn mk(
    stream: &str,
    db: Arc<FakeDb>,
    flag: FlagGate,
) -> (Consumer<Shared>, Arc<FakeDb>, Ready) {
    let ready = Ready::new();
    let c = Consumer::new(
        test_config(stream),
        Shared(db.clone()),
        conn().await,
        flag,
        Metrics::new(),
        ready.clone(),
    );
    (c, db, ready)
}

async fn pending(c: &mut ConnectionManager, stream: &str) -> usize {
    let r: redis::streams::StreamPendingReply = c.xpending(stream, "svc_analytics").await.unwrap();
    r.count()
}

async fn dlq(
    c: &mut ConnectionManager,
    stream: &str,
) -> Vec<std::collections::HashMap<String, String>> {
    let r: redis::streams::StreamRangeReply = c.xrange_all(format!("{stream}:dlq")).await.unwrap();
    r.ids
        .into_iter()
        .map(|i| {
            i.map
                .into_iter()
                .map(|(k, v)| (k, redis::from_redis_value::<String>(v).unwrap()))
                .collect()
        })
        .collect()
}

#[tokio::test]
async fn valid_events_are_written_then_acked() {
    let stream = unique_stream();
    let (c, db, _) = mk(&stream, Arc::new(FakeDb::default()), FlagGate::fixed(true)).await;
    let mut r = conn().await;
    db.premium.lock().unwrap().insert(10);
    for k in 1..=3 {
        add(&mut r, &stream, &ev(user(1), k, 10)).await;
    }
    assert_eq!(c.cycle().await.unwrap(), 3);
    let applied = db.applied.lock().unwrap().clone();
    assert_eq!(applied.len(), 3);
    assert!(applied.iter().all(|a| a.premium), "tier resolved Premium");
    assert_eq!(pending(&mut r, &stream).await, 0, "acked after write");
    assert_eq!(c.premium_seen(), vec![10]);
    assert_eq!(c.cycle().await.unwrap(), 0, "nothing left");
}

#[tokio::test]
async fn contract_violations_are_dead_lettered_pii_free_and_acked() {
    let stream = unique_stream();
    let (c, db, _) = mk(&stream, Arc::new(FakeDb::default()), FlagGate::fixed(true)).await;
    let mut r = conn().await;
    let who = user(7);
    let mut bad_v = ev(who, 1, 10);
    bad_v[0].1 = "2".into();
    let mut extra = ev(who, 1, 10);
    extra.push(("text", "super secret message".into()));
    let mut not_uuid = ev(who, 1, 10);
    not_uuid[3].1 = "alice".into();
    let missing: Vec<_> = ev(who, 1, 10)
        .into_iter()
        .filter(|(k, _)| *k != "kind_code")
        .collect();
    for f in [&bad_v, &extra, &not_uuid, &missing] {
        add(&mut r, &stream, f).await;
    }
    add(&mut r, &stream, &ev(who, 1, 10)).await; // one good entry rides along
    assert_eq!(c.cycle().await.unwrap(), 5);
    assert_eq!(db.applied.lock().unwrap().len(), 1);
    let d = dlq(&mut r, &stream).await;
    let mut reasons: Vec<_> = d.iter().map(|m| m["reason"].clone()).collect();
    reasons.sort();
    assert_eq!(
        reasons,
        [
            "bad_user_uuid",
            "missing_field",
            "unexpected_field",
            "unknown_version"
        ]
    );
    for m in &d {
        let mut keys: Vec<_> = m.keys().cloned().collect();
        keys.sort();
        assert_eq!(keys, ["at_ms", "consumer", "orig_id", "reason"]);
        let blob = format!("{m:?}");
        assert!(
            !blob.contains(&who.to_string()) && !blob.contains("secret"),
            "DLQ must be PII-free"
        );
    }
    assert_eq!(pending(&mut r, &stream).await, 0);
}

#[tokio::test]
async fn excluded_subjects_are_dropped_seeded_and_lazy_loaded() {
    let stream = unique_stream();
    let db = Arc::new(FakeDb::default());
    db.exclusions.lock().unwrap().insert(user(1));
    let (c, db, _) = mk(&stream, db, FlagGate::fixed(true)).await;
    let mut r = conn().await;
    add(&mut r, &stream, &ev(user(1), 1, 10)).await; // excluded at seed time
    add(&mut r, &stream, &ev(user(2), 1, 10)).await;
    c.cycle().await.unwrap();
    assert!(c.exclusions().len() >= 2);
    let got: Vec<_> = db
        .applied
        .lock()
        .unwrap()
        .iter()
        .map(|a| a.event.user_uuid)
        .collect();
    assert_eq!(got, vec![user(2)]);
    // user(3) becomes excluded AFTER the seed: never cached -> lazy-loaded -> dropped
    db.exclusions.lock().unwrap().insert(user(3));
    add(&mut r, &stream, &ev(user(3), 1, 10)).await;
    add(&mut r, &stream, &ev(user(2), 1, 10)).await; // cached negative: served without a lookup
    let lookups_before = *db.exclusion_lookups.lock().unwrap();
    c.cycle().await.unwrap();
    assert_eq!(
        *db.exclusion_lookups.lock().unwrap(),
        lookups_before + 1,
        "one lookup, only for the miss"
    );
    let got: Vec<_> = db
        .applied
        .lock()
        .unwrap()
        .iter()
        .map(|a| a.event.user_uuid)
        .collect();
    assert_eq!(got, vec![user(2), user(2)]);
    assert_eq!(
        pending(&mut r, &stream).await,
        0,
        "dropped entries are acked"
    );
}

#[tokio::test]
async fn exclusion_lookup_outage_fails_closed_holds_entries_then_recovers() {
    let stream = unique_stream();
    let (c, db, _) = mk(&stream, Arc::new(FakeDb::default()), FlagGate::fixed(true)).await;
    let mut r = conn().await;
    c.cycle().await.unwrap(); // group + seed while healthy
    *db.fail_exclusions.lock().unwrap() = true;
    add(&mut r, &stream, &ev(user(5), 1, 10)).await;
    let e = c.cycle().await.unwrap_err();
    assert!(
        matches!(e, CycleError::Pending("exclusion_unavailable")),
        "{e}"
    );
    assert!(
        db.applied.lock().unwrap().is_empty(),
        "fail closed: nothing written"
    );
    assert_eq!(pending(&mut r, &stream).await, 1);
    // recovery before the attempt budget is spent: written exactly once
    *db.fail_exclusions.lock().unwrap() = false;
    assert_eq!(c.cycle().await.unwrap(), 1);
    assert_eq!(db.applied.lock().unwrap().len(), 1);
    assert_eq!(pending(&mut r, &stream).await, 0);
}

#[tokio::test]
async fn persistent_exclusion_outage_dead_letters_after_max_attempts_without_writing() {
    let stream = unique_stream();
    let (c, db, _) = mk(&stream, Arc::new(FakeDb::default()), FlagGate::fixed(true)).await;
    let mut r = conn().await;
    c.cycle().await.unwrap();
    *db.fail_exclusions.lock().unwrap() = true;
    add(&mut r, &stream, &ev(user(5), 1, 10)).await;
    for _ in 0..3 {
        assert!(c.cycle().await.is_err());
    }
    assert_eq!(
        pending(&mut r, &stream).await,
        0,
        "third failed attempt dead-letters and acks"
    );
    // attempt 3 hit max_attempts=3
    let d = dlq(&mut r, &stream).await;
    assert_eq!(d.len(), 1);
    assert_eq!(d[0]["reason"], "exclusion_unavailable");
    assert!(db.applied.lock().unwrap().is_empty());
}

#[tokio::test]
async fn transient_write_failure_keeps_entries_pending_then_succeeds() {
    let stream = unique_stream();
    let (c, db, _) = mk(&stream, Arc::new(FakeDb::default()), FlagGate::fixed(true)).await;
    let mut r = conn().await;
    *db.transient_apply.lock().unwrap() = true;
    add(&mut r, &stream, &ev(user(1), 1, 10)).await;
    assert!(matches!(
        c.cycle().await.unwrap_err(),
        CycleError::Pending("write_transient")
    ));
    assert_eq!(pending(&mut r, &stream).await, 1);
    *db.transient_apply.lock().unwrap() = false;
    assert_eq!(
        c.cycle().await.unwrap(),
        1,
        "redelivered from the consumer's own pending list"
    );
    assert_eq!(db.applied.lock().unwrap().len(), 1);
    assert_eq!(pending(&mut r, &stream).await, 0);
}

#[tokio::test]
async fn poison_row_is_isolated_to_the_dlq_while_the_rest_of_the_batch_lands() {
    let stream = unique_stream();
    let db = Arc::new(FakeDb::default());
    db.poison_users.lock().unwrap().insert(user(1));
    let (c, db, _) = mk(&stream, db, FlagGate::fixed(true)).await;
    let mut r = conn().await;
    add(&mut r, &stream, &ev(user(1), 1, 10)).await;
    add(&mut r, &stream, &ev(user(2), 1, 10)).await;
    c.cycle().await.unwrap();
    let got: Vec<_> = db
        .applied
        .lock()
        .unwrap()
        .iter()
        .map(|a| a.event.user_uuid)
        .collect();
    assert_eq!(got, vec![user(2)]);
    let d = dlq(&mut r, &stream).await;
    assert_eq!((d.len(), d[0]["reason"].as_str()), (1, "write_rejected"));
    assert_eq!(pending(&mut r, &stream).await, 0);
}

#[tokio::test]
async fn tier_lookup_failure_falls_back_to_free_never_premium() {
    let stream = unique_stream();
    let db = Arc::new(FakeDb::default());
    db.premium.lock().unwrap().insert(10);
    *db.fail_tier.lock().unwrap() = true;
    let (c, db, _) = mk(&stream, db, FlagGate::fixed(true)).await;
    let mut r = conn().await;
    add(&mut r, &stream, &ev(user(1), 1, 10)).await;
    c.cycle().await.unwrap();
    assert!(!db.applied.lock().unwrap()[0].premium);
}

#[tokio::test]
async fn dead_letter_outage_leaves_the_entry_pending() {
    let stream = unique_stream();
    let (c, _db, _) = mk(&stream, Arc::new(FakeDb::default()), FlagGate::fixed(true)).await;
    let mut r = conn().await;
    let _: () = r
        .set(format!("{stream}:dlq"), "not a stream")
        .await
        .unwrap();
    let mut bad = ev(user(1), 1, 10);
    bad[0].1 = "9".into();
    add(&mut r, &stream, &bad).await;
    assert!(matches!(
        c.cycle().await.unwrap_err(),
        CycleError::Pending("dlq_unavailable")
    ));
    assert_eq!(pending(&mut r, &stream).await, 1);
    let _: () = r.del(format!("{stream}:dlq")).await.unwrap();
}

#[tokio::test]
async fn entries_stuck_on_a_dead_consumer_are_claimed() {
    let stream = unique_stream();
    let (c, db, _) = mk(&stream, Arc::new(FakeDb::default()), FlagGate::fixed(true)).await;
    let mut r = conn().await;
    c.ensure_group().await.unwrap();
    c.ensure_group().await.unwrap(); // BUSYGROUP is fine
    add(&mut r, &stream, &ev(user(1), 1, 10)).await;
    let _: redis::Value = redis::cmd("XREADGROUP")
        .arg("GROUP")
        .arg("svc_analytics")
        .arg("dead-consumer")
        .arg("COUNT")
        .arg(10)
        .arg("STREAMS")
        .arg(&stream)
        .arg(">")
        .query_async(&mut r)
        .await
        .unwrap();
    assert_eq!(pending(&mut r, &stream).await, 1);
    tokio::time::sleep(Duration::from_millis(1200)).await; // claim_idle = 1s
    assert_eq!(c.cycle().await.unwrap(), 1);
    assert_eq!(db.applied.lock().unwrap().len(), 1);
    assert_eq!(pending(&mut r, &stream).await, 0);
}

#[tokio::test]
async fn valkey_failure_surfaces_as_a_cycle_error() {
    let stream = unique_stream();
    let (c, _db, _) = mk(&stream, Arc::new(FakeDb::default()), FlagGate::fixed(true)).await;
    let mut r = conn().await;
    let _: () = r.set(&stream, "wrong type").await.unwrap();
    let e = c.cycle().await.unwrap_err();
    assert!(matches!(e, CycleError::Valkey(_)), "{e}");
    assert!(e.to_string().starts_with("valkey:"));
    let _: () = r.del(&stream).await.unwrap();
    let from_db: CycleError = svc_analytics::db::DbFault::transient("x").into();
    assert!(from_db.to_string().contains("database fault"));
}

#[tokio::test]
async fn flag_off_consumer_idles_but_stays_ready_then_consumes_when_enabled() {
    let stream = unique_stream();
    let flag = FlagGate::fixed(false);
    let (c, db, ready) = mk(&stream, Arc::new(FakeDb::default()), flag.clone()).await;
    let c = Arc::new(c);
    let mut r = conn().await;
    add(&mut r, &stream, &ev(user(1), 1, 10)).await;
    let (tx, rx) = watch::channel(false);
    let task = tokio::spawn({
        let c = c.clone();
        async move { c.run(rx).await }
    });
    tokio::time::sleep(Duration::from_millis(400)).await;
    assert!(
        db.applied.lock().unwrap().is_empty(),
        "flag OFF: nothing consumed"
    );
    assert!(ready.get(), "idle but healthy");
    let groups: redis::RedisResult<redis::Value> = redis::cmd("XINFO")
        .arg("GROUPS")
        .arg(&stream)
        .query_async(&mut r)
        .await;
    let consumed = match groups {
        Ok(redis::Value::Array(a)) => !a.is_empty(),
        _ => false,
    };
    assert!(!consumed, "no consumer group is created while OFF");
    flag.set_fixed(true);
    for _ in 0..40 {
        if !db.applied.lock().unwrap().is_empty() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert_eq!(db.applied.lock().unwrap().len(), 1, "flag ON: consumed");
    tx.send(true).unwrap();
    tokio::time::timeout(Duration::from_secs(5), task)
        .await
        .unwrap()
        .unwrap();
}

#[tokio::test]
async fn flag_off_on_a_license_bypass_domain_keeps_the_consumer_idle_but_healthy() {
    svc_analytics::crypto::ensure_installed();
    let cfg = penguin_licensing::LicenseConfig::new("waddles")
        .unwrap()
        .with_bypass_domain("waddles.app")
        .with_deployment_domain("svc-analytics.waddles.app");
    let client = penguin_licensing::LicenseClient::new(cfg).unwrap();
    assert!(client.bypass_active());
    let flag = FlagGate::Licensed {
        client: Some(client),
        env_on: false,
    };
    let stream = unique_stream();
    let (c, db, ready) = mk(&stream, Arc::new(FakeDb::default()), flag).await;
    let c = Arc::new(c);
    let mut r = conn().await;
    add(&mut r, &stream, &ev(user(1), 1, 10)).await;
    let (tx, rx) = watch::channel(false);
    let task = tokio::spawn({
        let c = c.clone();
        async move { c.run(rx).await }
    });
    tokio::time::sleep(Duration::from_millis(500)).await;
    assert!(
        db.applied.lock().unwrap().is_empty(),
        "bypass domain must not enable collection"
    );
    assert!(ready.get(), "idle but healthy");
    let len: usize = r.xlen(&stream).await.unwrap();
    assert_eq!(len, 1, "entry untouched");
    tx.send(true).unwrap();
    tokio::time::timeout(Duration::from_secs(5), task)
        .await
        .unwrap()
        .unwrap();
}

#[tokio::test]
async fn run_loop_backs_off_and_goes_not_ready_after_repeated_failures() {
    let stream = unique_stream();
    let (c, _db, ready) = mk(&stream, Arc::new(FakeDb::default()), FlagGate::fixed(true)).await;
    let c = Arc::new(c);
    let mut r = conn().await;
    let _: () = r.set(&stream, "wrong type").await.unwrap();
    let (tx, rx) = watch::channel(false);
    let task = tokio::spawn({
        let c = c.clone();
        async move { c.run(rx).await }
    });
    for _ in 0..60 {
        if !ready.get() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert!(!ready.get(), "sustained failures flip readiness");
    tx.send(true).unwrap();
    tokio::time::timeout(Duration::from_secs(5), task)
        .await
        .unwrap()
        .unwrap();
    let _: () = r.del(&stream).await.unwrap();
}

#[tokio::test]
async fn maintenance_jobs_report_and_schedule_once_per_day_in_the_window() {
    let db = FakeDb::default();
    let cfg = test_config("unused");
    let m = Metrics::new();
    assert_eq!(run_sweep(&db, &cfg, &m, NOW_S).await, 0);
    assert_eq!(run_cohorts(&db, &m, &[], NOW_S).await, 0);
    assert_eq!(run_cohorts(&db, &m, &[10], NOW_S).await, 3);
    assert_eq!(run_purge(&db, &cfg, &m, NOW_S).await, 4);
    *db.fail_jobs.lock().unwrap() = true;
    assert_eq!(run_sweep(&db, &cfg, &m, NOW_S).await, 0);
    assert_eq!(run_cohorts(&db, &m, &[10], NOW_S).await, 0);
    assert_eq!(run_purge(&db, &cfg, &m, NOW_S).await, 0);
    *db.fail_jobs.lock().unwrap() = false;

    // purge only inside 03:00-05:00 UTC and only once per UTC day
    let in_window = (NOW_S / DAY) * DAY + 3 * 3600 + 5;
    let out_of_window = (NOW_S / DAY) * DAY + 12 * 3600;
    let before = *db.purged.lock().unwrap();
    let mut jobs = Jobs::default();
    jobs.tick(&db, &cfg, &m, &[], out_of_window).await;
    assert_eq!(
        *db.purged.lock().unwrap(),
        before,
        "outside the off-peak window"
    );
    let mut cfg2 = cfg.clone();
    cfg2.maintenance_interval = Duration::from_secs(1);
    let mut jobs = Jobs::default();
    jobs.tick(&db, &cfg2, &m, &[], in_window).await;
    assert_eq!(*db.purged.lock().unwrap(), before + 1);
    jobs.tick(&db, &cfg2, &m, &[], in_window).await; // not due yet (interval)
    tokio::time::sleep(Duration::from_millis(1100)).await;
    jobs.tick(&db, &cfg2, &m, &[], in_window + 60).await; // due, same UTC day: no second purge
    assert_eq!(*db.purged.lock().unwrap(), before + 1);
    tokio::time::sleep(Duration::from_millis(1100)).await;
    jobs.tick(&db, &cfg2, &m, &[], in_window + DAY).await; // next day
    assert_eq!(*db.purged.lock().unwrap(), before + 2);
}
