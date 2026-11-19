//! End to end: real Valkey stream -> consumer -> real Postgres under the writer role grants.

mod support;

use redis::aio::ConnectionManager;
use redis::AsyncCommands;
use support::*;
use svc_analytics::consumer::{now_ms, Consumer, Raw};
use svc_analytics::db::PgStore;
use svc_analytics::flag::FlagGate;
use svc_analytics::health::Ready;
use svc_analytics::metrics::Metrics;
use uuid::Uuid;

async fn conn() -> ConnectionManager {
    ConnectionManager::new(redis::Client::open(valkey_url()).unwrap())
        .await
        .unwrap()
}

fn fields(u: Uuid, kind: u8, community: u32, ts_ms: i64) -> Vec<(String, Vec<u8>)> {
    [
        ("v", "1".to_string()),
        ("tenant_id", "1".into()),
        ("community_id", community.to_string()),
        ("user_uuid", u.to_string()),
        ("platform_code", "2".into()),
        ("kind_code", kind.to_string()),
        ("ts_ms", ts_ms.to_string()),
        ("provenance_code", "2".into()),
    ]
    .into_iter()
    .map(|(k, v)| (k.to_string(), v.into_bytes()))
    .collect()
}

async fn add(c: &mut ConnectionManager, stream: &str, f: &[(String, Vec<u8>)]) {
    let items: Vec<(&str, &[u8])> = f.iter().map(|(k, v)| (k.as_str(), v.as_slice())).collect();
    let _: String = c.xadd(stream, "*", &items).await.unwrap();
}

async fn consumer(fx: &Fixture, stream: &str) -> Consumer<PgStore> {
    Consumer::new(
        test_config(stream),
        fx.store().await,
        conn().await,
        FlagGate::fixed(true),
        Metrics::new(),
        Ready::new(),
    )
}

#[tokio::test]
async fn free_and_premium_communities_are_gated_end_to_end() {
    let fx = Fixture::new().await;
    fx.premium(10).await;
    let stream = unique_stream();
    let c = consumer(&fx, &stream).await;
    let mut r = conn().await;
    let t = now_ms() - 3_600_000;
    for (comm, kind, off) in [
        (10, 5, 0),
        (10, 6, 600_000),
        (10, 1, 1000),
        (11, 5, 0),
        (11, 6, 600_000),
        (11, 1, 1000),
    ] {
        add(&mut r, &stream, &fields(user(1), kind, comm, t + off)).await;
    }
    assert_eq!(c.cycle().await.unwrap(), 6);
    assert_eq!(
        fx.scalar("SELECT count(*) FROM analytics_fp.member_sessions WHERE community_id = 10")
            .await,
        1
    );
    assert_eq!(
        fx.scalar("SELECT count(*) FROM analytics_fp.member_sessions WHERE community_id = 11")
            .await,
        0,
        "Free: no sessions"
    );
    assert_eq!(
        fx.scalar(
            "SELECT presence_s::bigint FROM analytics_fp.member_daily WHERE community_id = 10"
        )
        .await,
        600
    );
    assert_eq!(
        fx.scalar(
            "SELECT presence_s::bigint FROM analytics_fp.member_daily WHERE community_id = 11"
        )
        .await,
        0
    );
    assert_eq!(
        fx.scalar(
            "SELECT retention_days::bigint FROM analytics_fp.member_daily WHERE community_id = 10"
        )
        .await,
        400
    );
    assert_eq!(
        fx.scalar(
            "SELECT retention_days::bigint FROM analytics_fp.member_daily WHERE community_id = 11"
        )
        .await,
        90
    );
    let pend: redis::streams::StreamPendingReply =
        r.xpending(&stream, "svc_analytics").await.unwrap();
    assert_eq!(pend.count(), 0);
}

#[tokio::test]
async fn excluded_user_is_never_written_and_late_erasure_is_caught_in_the_transaction() {
    let fx = Fixture::new().await;
    fx.exclude(user(9)).await;
    let stream = unique_stream();
    let c = consumer(&fx, &stream).await;
    let mut r = conn().await;
    let t = now_ms() - 1000;
    add(&mut r, &stream, &fields(user(9), 1, 10, t)).await;
    add(&mut r, &stream, &fields(user(1), 1, 10, t)).await;
    c.cycle().await.unwrap();
    assert_eq!(
        fx.scalar("SELECT count(*) FROM analytics_fp.member_daily")
            .await,
        1
    );
    assert_eq!(
        fx.scalar(&format!(
            "SELECT count(*) FROM analytics_fp.member_daily WHERE user_uuid = '{}'",
            user(9)
        ))
        .await,
        0
    );

    // user(1) is cached as NOT excluded; an erasure then lands (rows deleted + ERASED row) and a
    // late event arrives inside the cache TTL: the authoritative in-transaction check drops it.
    fx.exec(&format!(
        "DELETE FROM analytics_fp.member_daily WHERE user_uuid = '{}'",
        user(1)
    ))
    .await;
    fx.exclude(user(1)).await;
    add(&mut r, &stream, &fields(user(1), 1, 10, t + 5)).await;
    c.cycle().await.unwrap();
    assert_eq!(
        fx.scalar(&format!(
            "SELECT count(*) FROM analytics_fp.member_daily WHERE user_uuid = '{}'",
            user(1)
        ))
        .await,
        0,
        "late event for an erased subject must not re-create rows"
    );
}

#[tokio::test]
async fn lost_ack_replay_is_idempotent() {
    let fx = Fixture::new().await;
    let stream = unique_stream();
    let c = consumer(&fx, &stream).await;
    let t = now_ms() - 5000;
    let raws: Vec<Raw> = (0..4)
        .map(|i| Raw {
            id: format!("{}-{i}", t + i),
            fields: fields(user(1), if i == 3 { 2 } else { 1 }, 10, t + i),
        })
        .collect();
    c.process(raws.clone(), now_ms()).await.unwrap();
    // the XACK was "lost": the same entries are delivered again, twice
    c.process(raws.clone(), now_ms()).await.unwrap();
    c.process(raws, now_ms()).await.unwrap();
    assert_eq!(
        fx.scalar("SELECT msgs::bigint FROM analytics_fp.member_daily")
            .await,
        3
    );
    assert_eq!(
        fx.scalar("SELECT cmds::bigint FROM analytics_fp.member_daily")
            .await,
        1
    );
    assert_eq!(
        fx.scalar("SELECT sum(value)::bigint FROM analytics_fp.community_hourly")
            .await,
        4
    );
    assert_eq!(
        fx.scalar("SELECT count(*) FROM analytics_ops.applied_entries")
            .await,
        4
    );
}

#[tokio::test]
async fn bad_entry_id_and_data_fault_rows_are_dead_lettered() {
    let fx = Fixture::new().await;
    let stream = unique_stream();
    let c = consumer(&fx, &stream).await;
    let mut r = conn().await;
    c.ensure_group().await.unwrap();
    let t = now_ms() - 1000;
    // an entry id that is not <ms>-<seq> (cannot happen from a real stream; defensive)
    let bad = vec![Raw {
        id: "garbage".into(),
        fields: fields(user(1), 1, 10, t),
    }];
    c.process(bad, now_ms()).await.unwrap();
    let d: redis::streams::StreamRangeReply = r.xrange_all(format!("{stream}:dlq")).await.unwrap();
    assert_eq!(d.ids.len(), 1);
    // a DB-rejected row: tenant id fits the contract but community FK-style CHECK is violated via
    // an out-of-range retention (premium forced by raw SQL tier row is not needed): use platform
    // code that passes the contract but the table rejects -> impossible, so exercise the isolation
    // path with a CHECK we can trip from SQL: drop the constraint's allowance.
    fx.exec("ALTER TABLE analytics_fp.community_hourly DROP CONSTRAINT community_hourly_value_check; \
             ALTER TABLE analytics_fp.community_hourly ADD CONSTRAINT community_hourly_value_check CHECK (value < 0)").await;
    let raw = vec![
        Raw {
            id: format!("{t}-0"),
            fields: fields(user(1), 1, 10, t),
        },
        Raw {
            id: format!("{t}-1"),
            fields: fields(user(2), 1, 10, t),
        },
    ];
    c.process(raw, now_ms()).await.unwrap();
    let d: redis::streams::StreamRangeReply = r.xrange_all(format!("{stream}:dlq")).await.unwrap();
    assert_eq!(
        d.ids.len(),
        3,
        "both rejected rows dead-lettered after isolation"
    );
    assert_eq!(
        fx.scalar("SELECT count(*) FROM analytics_ops.applied_entries")
            .await,
        0,
        "nothing half-applied"
    );
}
