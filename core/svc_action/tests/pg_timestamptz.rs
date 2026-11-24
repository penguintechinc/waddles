//! Real-Postgres proof that the `with-time` entity (`action_dispatch_log`) writes and reads the
//! `TIMESTAMPTZ` columns of migration 074 with the instant intact. Opt-in: needs
//! `TEST_DATABASE_URL`; `#[ignore]`d so the default `cargo test` stays DB-free.

use sea_orm::{ConnectionTrait, Database, DatabaseBackend, EntityTrait, Statement};
use svc_action::db::entities::action_dispatch_log::{self, insert};
use time::OffsetDateTime;

fn stmt(sql: &str) -> Statement {
    Statement::from_string(DatabaseBackend::Postgres, sql.to_string())
}

#[tokio::test]
#[ignore = "needs a real Postgres: set TEST_DATABASE_URL"]
async fn action_dispatch_log_roundtrips_timestamptz() {
    let url = std::env::var("TEST_DATABASE_URL").expect("TEST_DATABASE_URL");
    let db = Database::connect(&url).await.expect("connect");
    // Migration 074's columns, minus the FKs to tables this test does not need.
    for sql in [
        "DROP TABLE IF EXISTS action_dispatch_log",
        "CREATE TABLE action_dispatch_log (id BIGSERIAL PRIMARY KEY, tenant_id INTEGER NOT NULL, \
         community_id INTEGER, app_id TEXT NOT NULL, target_type TEXT NOT NULL, status TEXT NOT NULL, \
         attempt INTEGER NOT NULL DEFAULT 1, http_status INTEGER, detail TEXT NOT NULL DEFAULT '', \
         envelope_ts TIMESTAMPTZ, dispatched_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
        "SET TIME ZONE 'America/Los_Angeles'",
    ] {
        db.execute_raw(stmt(sql)).await.expect(sql);
    }

    // 2023-11-14T22:13:20.123456Z, supplied with a +05:30 offset: the instant must survive.
    let envelope = OffsetDateTime::from_unix_timestamp_nanos(1_700_000_000_123_456_000)
        .unwrap()
        .to_offset(time::UtcOffset::from_hms(5, 30, 0).unwrap());
    let before = OffsetDateTime::now_utc();
    insert(
        &db,
        7,
        Some(3),
        "penguintechinc.waddles.test-app".to_string(),
        "webhook".to_string(),
        "success".to_string(),
        1,
        Some(200),
        "ok".to_string(),
        Some(envelope),
    )
    .await
    .unwrap();

    let row = action_dispatch_log::Entity::find()
        .one(&db)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        row.envelope_ts.unwrap().unix_timestamp_nanos(),
        1_700_000_000_123_456_000
    );
    // `dispatched_at` is written by `insert` from `now_utc()`; it must land within the window.
    let after = OffsetDateTime::now_utc();
    assert!(row.dispatched_at >= before - time::Duration::seconds(1));
    assert!(row.dispatched_at <= after + time::Duration::seconds(1));

    let rows = db
        .query_all_raw(stmt(
            "SELECT data_type FROM information_schema.columns WHERE table_name = 'action_dispatch_log' \
             AND column_name IN ('envelope_ts','dispatched_at')",
        ))
        .await
        .unwrap();
    assert_eq!(rows.len(), 2);
    for r in rows {
        assert_eq!(
            r.try_get_by_index::<String>(0).unwrap(),
            "timestamp with time zone"
        );
    }
    db.execute_raw(stmt("DROP TABLE action_dispatch_log"))
        .await
        .unwrap();
}
