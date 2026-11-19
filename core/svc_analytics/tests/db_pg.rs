//! Real-Postgres tests of the layer A writer (schema mirrors alembic 0129/0131, role
//! grants mirror the migration). Each test gets its own throwaway database.

mod support;

use support::*;
use svc_analytics::aggregate::{Accepted, EntryId};
use svc_analytics::contract::Kind;
use svc_analytics::db::{AnalyticsDb, DbFault, FaultKind, PgDb};

const C: i32 = 10;

fn midnight() -> i64 {
    (NOW_S / DAY + 1) * DAY
}

#[tokio::test]
async fn free_community_gets_basic_counts_only() {
    let fx = Fixture::new().await;
    let s = fx.store().await;
    let u = user(1);
    let batch = vec![
        acc(1, C, u, Kind::Message, NOW_S, false),
        acc(2, C, u, Kind::Message, NOW_S + 10, false),
        acc(3, C, u, Kind::Command, NOW_S + 20, false),
        acc(4, C, user(2), Kind::Join, NOW_S + 30, false),
        acc(5, C, user(2), Kind::Leave, NOW_S + 40, false),
        acc(6, C, u, Kind::PresenceStart, NOW_S + 50, false),
        acc(7, C, u, Kind::PresenceEnd, NOW_S + 650, false),
    ];
    let rep = s.apply(&batch).await.unwrap();
    assert_eq!((rep.applied, rep.duplicates, rep.late_excluded), (7, 0, 0));
    assert_eq!(
        fx.scalar("SELECT value FROM analytics_fp.community_hourly WHERE metric_code = 1")
            .await,
        2
    );
    assert_eq!(
        fx.scalar("SELECT value FROM analytics_fp.community_hourly WHERE metric_code = 2")
            .await,
        1
    );
    assert_eq!(
        fx.scalar("SELECT value FROM analytics_fp.community_hourly WHERE metric_code = 3")
            .await,
        1
    );
    assert_eq!(
        fx.scalar("SELECT value FROM analytics_fp.community_hourly WHERE metric_code = 4")
            .await,
        1
    );
    assert_eq!(
        fx.scalar("SELECT count(*) FROM analytics_fp.community_hourly WHERE metric_code = 5")
            .await,
        0
    );
    assert_eq!(
        fx.scalar("SELECT count(*) FROM analytics_fp.member_sessions")
            .await,
        0,
        "no sessions for Free"
    );
    assert_eq!(
        fx.scalar("SELECT presence_s::bigint FROM analytics_fp.member_daily")
            .await,
        0
    );
    assert_eq!(
        fx.scalar("SELECT msgs::bigint FROM analytics_fp.member_daily")
            .await,
        2
    );
    assert_eq!(
        fx.scalar("SELECT cmds::bigint FROM analytics_fp.member_daily")
            .await,
        1
    );
    assert_eq!(
        fx.scalar("SELECT max(retention_days)::bigint FROM analytics_fp.community_hourly")
            .await,
        90
    );
    assert_eq!(
        fx.scalar("SELECT max(retention_days)::bigint FROM analytics_fp.member_daily")
            .await,
        90
    );
    assert_eq!(
        fx.scalar("SELECT count(*) FROM analytics_ops.applied_entries")
            .await,
        7
    );
}

#[tokio::test]
async fn premium_session_credits_presence_and_400_day_retention() {
    let fx = Fixture::new().await;
    let s = fx.store().await;
    let u = user(1);
    let rep = s
        .apply(&[
            acc(1, C, u, Kind::PresenceStart, NOW_S, true),
            acc(2, C, u, Kind::PresenceEnd, NOW_S + 600, true),
        ])
        .await
        .unwrap();
    assert_eq!((rep.sessions_opened, rep.sessions_closed), (1, 1));
    assert_eq!(
        fx.scalar("SELECT (EXTRACT(EPOCH FROM session_end - session_start))::bigint FROM analytics_fp.member_sessions").await,
        600
    );
    assert_eq!(
        fx.scalar("SELECT presence_s::bigint FROM analytics_fp.member_daily")
            .await,
        600
    );
    assert_eq!(
        fx.scalar("SELECT value FROM analytics_fp.community_hourly WHERE metric_code = 5")
            .await,
        1
    );
    assert_eq!(
        fx.scalar("SELECT retention_days::bigint FROM analytics_fp.member_sessions")
            .await,
        400
    );
    assert_eq!(
        fx.scalar("SELECT retention_days::bigint FROM analytics_fp.member_daily")
            .await,
        400
    );
}

#[tokio::test]
async fn session_crossing_midnight_splits_presence_per_day() {
    let fx = Fixture::new().await;
    let s = fx.store().await;
    let m = midnight();
    s.apply(&[
        acc(1, C, user(1), Kind::PresenceStart, m - 120, true),
        acc(2, C, user(1), Kind::PresenceEnd, m + 180, true),
    ])
    .await
    .unwrap();
    let rows = fx
        .q("SELECT presence_s::bigint FROM analytics_fp.member_daily ORDER BY day")
        .await;
    let got: Vec<i64> = rows
        .iter()
        .map(|r| r.try_get_by_index(0).unwrap())
        .collect();
    assert_eq!(got, vec![120, 180]);
}

#[tokio::test]
async fn duplicate_start_orphan_end_and_duplicate_end_are_counted_not_written() {
    let fx = Fixture::new().await;
    let s = fx.store().await;
    let u = user(1);
    let r = s
        .apply(&[acc(1, C, u, Kind::PresenceEnd, NOW_S, true)])
        .await
        .unwrap();
    assert_eq!(r.orphan_ends, 1);
    assert_eq!(
        fx.scalar("SELECT count(*) FROM analytics_fp.member_sessions")
            .await,
        0
    );
    let r = s
        .apply(&[
            acc(2, C, u, Kind::PresenceStart, NOW_S + 100, true),
            acc(3, C, u, Kind::PresenceStart, NOW_S + 110, true),
        ])
        .await
        .unwrap();
    assert_eq!((r.sessions_opened, r.duplicate_starts), (1, 1));
    let r = s
        .apply(&[acc(4, C, u, Kind::PresenceEnd, NOW_S + 200, true)])
        .await
        .unwrap();
    assert_eq!(r.sessions_closed, 1);
    // a repeated end inside the closed session is a duplicate, not an orphan
    let r = s
        .apply(&[acc(5, C, u, Kind::PresenceEnd, NOW_S + 150, true)])
        .await
        .unwrap();
    assert_eq!((r.orphan_ends, r.duplicate_starts), (0, 1));
    assert_eq!(
        fx.scalar("SELECT count(*) FROM analytics_fp.member_sessions")
            .await,
        1
    );
    assert_eq!(
        fx.scalar("SELECT presence_s::bigint FROM analytics_fp.member_daily")
            .await,
        100
    );
}

#[tokio::test]
async fn out_of_order_within_a_batch_is_sorted_by_event_time() {
    let fx = Fixture::new().await;
    let s = fx.store().await;
    let r = s
        .apply(&[
            acc(1, C, user(1), Kind::PresenceEnd, NOW_S + 300, true),
            acc(2, C, user(1), Kind::PresenceStart, NOW_S, true),
        ])
        .await
        .unwrap();
    assert_eq!(
        (r.sessions_opened, r.sessions_closed, r.orphan_ends),
        (1, 1, 0)
    );
    assert_eq!(
        fx.scalar("SELECT presence_s::bigint FROM analytics_fp.member_daily")
            .await,
        300
    );
}

#[tokio::test]
async fn missing_end_is_closed_by_the_bounded_sweep_and_end_is_clamped() {
    let fx = Fixture::new().await;
    let s = fx.store_with(&fx.writer_url, 600).await;
    // start with no end
    s.apply(&[acc(1, C, user(1), Kind::PresenceStart, NOW_S, true)])
        .await
        .unwrap();
    assert_eq!(
        s.close_stale_sessions(NOW_S + 500, 600, 100).await.unwrap(),
        0,
        "not idle long enough"
    );
    assert_eq!(
        s.close_stale_sessions(NOW_S + 700, 600, 100).await.unwrap(),
        1
    );
    assert_eq!(
        fx.scalar("SELECT presence_s::bigint FROM analytics_fp.member_daily")
            .await,
        600
    );
    assert_eq!(
        s.close_stale_sessions(NOW_S + 7000, 600, 100)
            .await
            .unwrap(),
        0,
        "already closed"
    );
    // an end far past the bound is clamped to start + bound
    s.apply(&[
        acc(2, C, user(2), Kind::PresenceStart, NOW_S, true),
        acc(3, C, user(2), Kind::PresenceEnd, NOW_S + 5000, true),
    ])
    .await
    .unwrap();
    assert_eq!(
        fx.scalar(&format!(
            "SELECT (EXTRACT(EPOCH FROM session_end - session_start))::bigint FROM analytics_fp.member_sessions WHERE user_uuid = '{}'",
            user(2)
        ))
        .await,
        600
    );
}

#[tokio::test]
async fn a_new_start_closes_a_stale_open_session_first() {
    let fx = Fixture::new().await;
    let s = fx.store_with(&fx.writer_url, 600).await;
    let u = user(1);
    s.apply(&[acc(1, C, u, Kind::PresenceStart, NOW_S, true)])
        .await
        .unwrap();
    let r = s
        .apply(&[acc(2, C, u, Kind::PresenceStart, NOW_S + 1000, true)])
        .await
        .unwrap();
    assert_eq!(r.sessions_opened, 1);
    assert_eq!(
        fx.scalar("SELECT count(*) FROM analytics_fp.member_sessions")
            .await,
        2
    );
    assert_eq!(
        fx.scalar("SELECT count(*) FROM analytics_fp.member_sessions WHERE session_end IS NULL")
            .await,
        1
    );
    assert_eq!(
        fx.scalar("SELECT presence_s::bigint FROM analytics_fp.member_daily")
            .await,
        600
    );
}

#[tokio::test]
async fn replay_of_the_same_entries_does_not_double_count() {
    let fx = Fixture::new().await;
    let s = fx.store().await;
    let batch = vec![
        acc(1, C, user(1), Kind::Message, NOW_S, true),
        acc(2, C, user(1), Kind::PresenceStart, NOW_S + 5, true),
        acc(3, C, user(1), Kind::PresenceEnd, NOW_S + 65, true),
    ];
    let first = s.apply(&batch).await.unwrap();
    assert_eq!(first.applied, 3);
    let second = s.apply(&batch).await.unwrap();
    assert_eq!((second.applied, second.duplicates), (0, 3));
    // partial overlap: only the new entry applies
    let mut more = batch.clone();
    more.push(acc(4, C, user(1), Kind::Message, NOW_S + 90, true));
    let third = s.apply(&more).await.unwrap();
    assert_eq!((third.applied, third.duplicates), (1, 3));
    assert_eq!(
        fx.scalar("SELECT msgs::bigint FROM analytics_fp.member_daily")
            .await,
        2
    );
    assert_eq!(
        fx.scalar("SELECT presence_s::bigint FROM analytics_fp.member_daily")
            .await,
        60
    );
    assert_eq!(
        fx.scalar("SELECT value FROM analytics_fp.community_hourly WHERE metric_code = 1")
            .await,
        2
    );
    assert_eq!(
        fx.scalar("SELECT count(*) FROM analytics_fp.member_sessions")
            .await,
        1
    );
}

#[tokio::test]
async fn excluded_subject_is_dropped_inside_the_write_transaction() {
    let fx = Fixture::new().await;
    let s = fx.store().await;
    fx.exclude(user(9)).await;
    let r = s
        .apply(&[
            acc(1, C, user(9), Kind::Message, NOW_S, true),
            acc(2, C, user(9), Kind::PresenceStart, NOW_S, true),
            acc(3, C, user(1), Kind::Message, NOW_S, true),
        ])
        .await
        .unwrap();
    assert_eq!((r.applied, r.late_excluded), (1, 2));
    assert_eq!(
        fx.scalar(&format!(
            "SELECT count(*) FROM analytics_fp.member_daily WHERE user_uuid = '{}'",
            user(9)
        ))
        .await,
        0
    );
    assert_eq!(
        fx.scalar("SELECT count(*) FROM analytics_fp.member_sessions")
            .await,
        0
    );
    assert_eq!(
        fx.scalar("SELECT msgs::bigint FROM analytics_fp.member_daily")
            .await,
        1
    );
}

#[tokio::test]
async fn exclusion_and_tier_lookups_use_the_writer_grants() {
    let fx = Fixture::new().await;
    let s = fx.store().await;
    assert!(s.load_exclusions(10).await.unwrap().is_empty());
    assert!(s.lookup_exclusions(&[]).await.unwrap().is_empty());
    fx.exclude(user(3)).await;
    fx.exclude(user(4)).await;
    fx.exec(&format!(
        "INSERT INTO public.data_use_exclusions (user_uuid, reason_code) VALUES ('{}', 1)",
        user(3)
    ))
    .await;
    let mut all = s.load_exclusions(10).await.unwrap();
    all.sort();
    assert_eq!(all, vec![user(3), user(4)], "DISTINCT across reasons");
    assert_eq!(
        s.load_exclusions(1).await.unwrap().len(),
        1,
        "seed is capped"
    );
    assert_eq!(
        s.lookup_exclusions(&[user(3), user(5)]).await.unwrap(),
        vec![user(3)]
    );

    fx.premium(10).await;
    fx.exec("INSERT INTO public.community_tier_allocations (tenant_id, community_id, tier_code, released_at) VALUES (1, 11, 2, now())").await;
    assert_eq!(s.premium_communities(&[]).await.unwrap(), Vec::<i32>::new());
    assert_eq!(
        s.premium_communities(&[10, 11, 12]).await.unwrap(),
        vec![10],
        "released/absent are Free"
    );

    // WITHOUT the proposed column grant the lookup is a transient (operator-fixable) fault.
    let ungranted = fx.store_with(&fx.ungranted_url, 600).await;
    let e = ungranted.premium_communities(&[10]).await.unwrap_err();
    assert_eq!(e, DbFault::transient("42501"));
}

#[tokio::test]
async fn member_login_role_inherits_only_the_three_tier_columns() {
    use sea_orm::{ConnectionTrait, Database, DbBackend, Statement};
    let fx = Fixture::new().await;
    fx.premium(10).await;
    // the service connects as a LOGIN role that is only a MEMBER of the writer group role
    let conn = Database::connect(fx.writer_url.clone()).await.unwrap();
    let q = |sql: &'static str| {
        let conn = conn.clone();
        async move {
            conn.query_all_raw(Statement::from_string(DbBackend::Postgres, sql))
                .await
        }
    };
    let ok =
        q("SELECT community_id, tier_code, released_at FROM public.community_tier_allocations")
            .await;
    assert_eq!(
        ok.unwrap().len(),
        1,
        "granted columns are readable through membership"
    );
    for sql in [
        "SELECT id FROM public.community_tier_allocations",
        "SELECT tenant_id FROM public.community_tier_allocations",
        "SELECT * FROM public.community_tier_allocations",
    ] {
        let e = q(sql).await.unwrap_err();
        assert!(e.to_string().contains("permission denied"), "{sql}: {e}");
    }
    // no write access to the tier table either
    let e = conn
        .execute_unprepared("UPDATE public.community_tier_allocations SET released_at = now()")
        .await
        .unwrap_err();
    assert!(e.to_string().contains("permission denied"), "{e}");
}

#[tokio::test]
async fn database_rejection_is_a_data_fault_and_rolls_back_the_ledger() {
    let fx = Fixture::new().await;
    let s = fx.store().await;
    let mut bad = acc(1, C, user(1), Kind::Message, NOW_S, false);
    bad.event.platform_code = 9; // violates the CHECK (bypassing contract validation)
    let e = s.apply(&[bad.clone()]).await.unwrap_err();
    assert_eq!(e.kind, FaultKind::Data);
    assert_eq!(
        fx.scalar("SELECT count(*) FROM analytics_ops.applied_entries")
            .await,
        0,
        "tx rolled back"
    );
    // the entry is not poisoned in the ledger: a corrected replay applies
    bad.event.platform_code = 2;
    assert_eq!(s.apply(&[bad]).await.unwrap().applied, 1);
}

#[tokio::test]
async fn connect_failure_and_ping() {
    let fx = Fixture::new().await;
    let s = fx.store().await;
    s.db.ping().await.unwrap();
    let e = PgDb::connect("postgres://nobody:x@127.0.0.1:1/none")
        .await
        .err()
        .expect("must fail");
    assert_eq!(e.kind, FaultKind::Transient);
}

#[tokio::test]
async fn cohorts_are_recomputed_idempotently() {
    let fx = Fixture::new().await;
    let s = fx.store().await;
    // a Monday at least 3 weeks before NOW
    let mut d = NOW_S / DAY - 30;
    while d % 7 != 4 {
        d += 1;
    }
    let w0 = d * DAY;
    let w1 = (d + 7) * DAY;
    let mut seq = 0;
    let mut e = |u, t| {
        seq += 1;
        acc(seq, C, user(u), Kind::Message, t, true)
    };
    s.apply(&[e(1, w0 + 100), e(2, w0 + DAY), e(1, w1 + 100), e(3, w1 + 5)])
        .await
        .unwrap();
    assert_eq!(s.refresh_cohorts(&[], NOW_S).await.unwrap(), 0);
    let n = s.refresh_cohorts(&[C], NOW_S).await.unwrap();
    assert_eq!(n, 3);
    let rows = fx
        .q("SELECT (cohort_week - DATE '1970-01-01')::bigint, week_offset::bigint, active_members::bigint, cohort_size::bigint, retention_days::bigint FROM analytics_fp.cohort_weekly ORDER BY 1, 2")
        .await;
    let got: Vec<(i64, i64, i64, i64, i64)> = rows
        .iter()
        .map(|r| {
            (
                r.try_get_by_index(0).unwrap(),
                r.try_get_by_index(1).unwrap(),
                r.try_get_by_index(2).unwrap(),
                r.try_get_by_index(3).unwrap(),
                r.try_get_by_index(4).unwrap(),
            )
        })
        .collect();
    assert_eq!(
        got,
        vec![(d, 0, 2, 2, 400), (d, 1, 1, 2, 400), (d + 7, 0, 1, 1, 400)]
    );
    assert_eq!(
        s.refresh_cohorts(&[C], NOW_S).await.unwrap(),
        3,
        "recompute, not accumulate"
    );
    assert_eq!(
        fx.scalar("SELECT count(*) FROM analytics_fp.cohort_weekly")
            .await,
        3
    );
}

#[tokio::test]
async fn purge_honours_per_row_retention_in_bounded_batches() {
    let fx = Fixture::new().await;
    let s = fx.store().await;
    let ago = |days: i64| NOW_S - days * DAY;
    // (hours): Free expired (100d > 90), Free kept (10d), Premium kept (200d <= 400), Premium expired (401d)
    for (h, ret) in [
        (ago(100), 90),
        (ago(10), 90),
        (ago(200), 400),
        (ago(401), 400),
    ] {
        fx.exec(&format!(
            "INSERT INTO analytics_fp.community_hourly (tenant_id, community_id, hour_ts, metric_code, platform_code, value, retention_days) \
             VALUES (1, 10, to_timestamp({h}), 1, 2, 1, {ret})"
        ))
        .await;
    }
    for (d, ret) in [(ago(100), 90), (ago(5), 90), (ago(401), 400)] {
        fx.exec(&format!(
            "INSERT INTO analytics_fp.member_daily (community_id, user_uuid, day, retention_days) \
             VALUES (10, '{}', (to_timestamp({d}) AT TIME ZONE 'UTC')::date, {ret})",
            user(d as u128 % 1000)
        ))
        .await;
        fx.exec(&format!(
            "INSERT INTO analytics_fp.member_sessions (community_id, user_uuid, session_start, platform_code, retention_days) \
             VALUES (10, '{}', to_timestamp({d}), 2, {ret})",
            user(d as u128 % 1000)
        ))
        .await;
        fx.exec(&format!(
            "INSERT INTO analytics_fp.cohort_weekly (community_id, cohort_week, week_offset, active_members, cohort_size, retention_days) \
             VALUES (10, (to_timestamp({d}) AT TIME ZONE 'UTC')::date, 0, 1, 1, {ret})"
        ))
        .await;
        fx.exec(&format!(
            "INSERT INTO analytics_fp.member_signals_daily (community_id, user_uuid, day, signal_code, score, retention_days) \
             VALUES (10, '{}', (to_timestamp({d}) AT TIME ZONE 'UTC')::date, 1, 5, {ret})",
            user(d as u128 % 1000)
        ))
        .await;
    }
    fx.exec(&format!("INSERT INTO analytics_ops.applied_entries (entry_ms, entry_seq, applied_at) VALUES (1, 1, to_timestamp({})), (2, 2, to_timestamp({}))", ago(30), ago(1))).await;

    // bounded: 1 row per batch, 1 batch => only one expired hourly row goes
    let rep = s.purge(NOW_S, 1, 1).await.unwrap();
    let hourly = rep
        .tables
        .iter()
        .find(|t| t.0 == "analytics_fp.community_hourly")
        .unwrap()
        .1;
    assert_eq!(hourly, 1);
    assert_eq!(
        fx.scalar("SELECT count(*) FROM analytics_fp.community_hourly")
            .await,
        3
    );

    let rep = s.purge(NOW_S, 100, 5).await.unwrap();
    assert_eq!(rep.tables.len(), 6);
    assert_eq!(
        fx.scalar("SELECT count(*) FROM analytics_fp.community_hourly")
            .await,
        2
    );
    assert_eq!(
        fx.scalar("SELECT count(*) FROM analytics_fp.community_hourly WHERE retention_days = 400")
            .await,
        1
    );
    assert_eq!(
        fx.scalar("SELECT count(*) FROM analytics_fp.member_daily")
            .await,
        1
    );
    assert_eq!(
        fx.scalar("SELECT count(*) FROM analytics_fp.member_sessions")
            .await,
        1
    );
    assert_eq!(
        fx.scalar("SELECT count(*) FROM analytics_fp.cohort_weekly")
            .await,
        1
    );
    assert_eq!(
        fx.scalar("SELECT count(*) FROM analytics_fp.member_signals_daily")
            .await,
        1
    );
    // ledger rows older than 7 days are purged, fresh ones kept
    assert_eq!(
        fx.scalar("SELECT count(*) FROM analytics_ops.applied_entries")
            .await,
        1
    );
    // a second run finds nothing
    let rep = s.purge(NOW_S, 100, 5).await.unwrap();
    assert!(rep.tables.iter().all(|t| t.1 == 0));
}

#[tokio::test]
async fn entry_id_helper_matches_stream_format() {
    assert_eq!(
        EntryId::parse("1700000000000-5"),
        Some(EntryId {
            ms: 1_700_000_000_000,
            seq: 5
        })
    );
    let _ = Accepted {
        entry: EntryId { ms: 0, seq: 0 },
        event: acc(0, 1, user(0), Kind::Join, NOW_S, false).event,
        premium: false,
    };
}
