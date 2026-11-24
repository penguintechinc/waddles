//! Real-Postgres proof that the `with-time` entity column types decode the `timestamptz`
//! columns of migration 0028 (`bundle_active_set_changes.changed_at`,
//! `bundle_active_set_watermark.computed_at`). Opt-in: needs `TEST_DATABASE_URL` and is
//! `#[ignore]`d so the default `cargo test` stays DB-free.

use bundle_active_set::entities::{bundle_active_set_changes, bundle_active_set_watermark};
use sea_orm::{ConnectionTrait, Database, DatabaseBackend, EntityTrait, Statement};
use time::macros::datetime;

fn stmt(sql: &str) -> Statement {
    Statement::from_string(DatabaseBackend::Postgres, sql.to_string())
}

#[tokio::test]
#[ignore = "needs a real Postgres: set TEST_DATABASE_URL"]
async fn time_entities_decode_timestamptz_columns() {
    let url = std::env::var("TEST_DATABASE_URL").expect("TEST_DATABASE_URL");
    let db = Database::connect(&url).await.expect("connect");
    for sql in [
        "DROP TABLE IF EXISTS bundle_active_set_changes, bundle_active_set_watermark",
        "CREATE TABLE bundle_active_set_changes (seq BIGINT PRIMARY KEY, tenant_id INTEGER, \
         community_id INTEGER, entity TEXT NOT NULL, entity_id TEXT NOT NULL, op TEXT NOT NULL, \
         changed_at TIMESTAMPTZ NOT NULL DEFAULT now(), writer_xid BIGINT)",
        "CREATE TABLE bundle_active_set_watermark (id SMALLINT PRIMARY KEY, safe_seq BIGINT NOT NULL, \
         min_retained_seq BIGINT NOT NULL, computed_at TIMESTAMPTZ NOT NULL DEFAULT now())",
        "INSERT INTO bundle_active_set_changes (seq, entity, entity_id, op, changed_at) \
         VALUES (1, 'e', 'id', 'upsert', '2023-11-14T22:13:20.123456+00')",
        "INSERT INTO bundle_active_set_watermark (id, safe_seq, min_retained_seq, computed_at) \
         VALUES (1, 5, 0, '2023-11-14T14:13:20-08:00')",
    ] {
        db.execute_raw(stmt(sql)).await.expect(sql);
    }

    let change = bundle_active_set_changes::Entity::find()
        .one(&db)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        change.changed_at.to_offset(time::UtcOffset::UTC),
        datetime!(2023-11-14 22:13:20.123456 UTC)
    );
    let wm = bundle_active_set_watermark::Entity::find()
        .one(&db)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(wm.computed_at.unix_timestamp(), 1_700_000_000);

    // The columns are genuinely timestamptz (a plain `timestamp` column would not decode above).
    let rows = db
        .query_all_raw(stmt(
            "SELECT data_type FROM information_schema.columns \
             WHERE column_name IN ('changed_at','computed_at')",
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
    db.execute_raw(stmt(
        "DROP TABLE bundle_active_set_changes, bundle_active_set_watermark",
    ))
    .await
    .unwrap();
}
