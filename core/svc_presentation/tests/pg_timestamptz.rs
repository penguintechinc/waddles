//! Real-Postgres proof that the `with-time` entities (`caption_events`,
//! `overlay_view_credentials`) read the `TIMESTAMPTZ` columns with the instant intact, even when
//! the session time zone is not UTC. Opt-in: needs `TEST_DATABASE_URL`; `#[ignore]`d so the
//! default `cargo test` stays DB-free.

use sea_orm::{ConnectionTrait, Database, DatabaseBackend, EntityTrait, Statement};
use svc_presentation::db::entities::{caption_event, overlay_view_credential};

fn stmt(sql: &str) -> Statement {
    Statement::from_string(DatabaseBackend::Postgres, sql.to_string())
}

#[tokio::test]
#[ignore = "needs a real Postgres: set TEST_DATABASE_URL"]
async fn time_entities_roundtrip_timestamptz() {
    let url = std::env::var("TEST_DATABASE_URL").expect("TEST_DATABASE_URL");
    let db = Database::connect(&url).await.expect("connect");
    // Column shapes of migrations 102 / 100, minus the FKs to tables this test does not need.
    for sql in [
        "DROP TABLE IF EXISTS caption_events",
        "DROP TABLE IF EXISTS overlay_view_credentials",
        "CREATE TABLE caption_events (id BIGSERIAL PRIMARY KEY, community_id BIGINT NOT NULL, \
         user_ref UUID, platform TEXT NOT NULL, original_message TEXT NOT NULL, \
         translated_message TEXT, detected_language TEXT, target_language TEXT, \
         confidence_score DOUBLE PRECISION, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
        "CREATE TABLE overlay_view_credentials (id BIGSERIAL PRIMARY KEY, community_id BIGINT NOT NULL, \
         key_hash TEXT NOT NULL, previous_key_hash TEXT, is_active BOOLEAN NOT NULL, \
         rotated_at TIMESTAMPTZ)",
        "SET TIME ZONE 'America/Los_Angeles'",
        // 2023-11-14T22:13:20.123456Z written with a +05:30 literal offset.
        "INSERT INTO caption_events (community_id, platform, original_message, created_at) \
         VALUES (1, 'twitch', 'hi', '2023-11-15 03:43:20.123456+05:30')",
        "INSERT INTO overlay_view_credentials (community_id, key_hash, is_active, rotated_at) \
         VALUES (1, 'h', true, '2023-11-15 03:43:20.123456+05:30'), (2, 'h', true, NULL)",
    ] {
        db.execute_raw(stmt(sql)).await.expect(sql);
    }

    let caption = caption_event::Entity::find()
        .one(&db)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        caption.created_at.unix_timestamp_nanos(),
        1_700_000_000_123_456_000
    );

    let mut creds = overlay_view_credential::Entity::find()
        .all(&db)
        .await
        .unwrap();
    creds.sort_by_key(|c| c.community_id);
    assert_eq!(
        creds[0].rotated_at.unwrap().unix_timestamp_nanos(),
        1_700_000_000_123_456_000
    );
    assert!(creds[1].rotated_at.is_none());

    let rows = db
        .query_all_raw(stmt(
            "SELECT data_type FROM information_schema.columns WHERE \
             (table_name = 'caption_events' AND column_name = 'created_at') OR \
             (table_name = 'overlay_view_credentials' AND column_name = 'rotated_at')",
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
    for sql in [
        "DROP TABLE caption_events",
        "DROP TABLE overlay_view_credentials",
    ] {
        db.execute_raw(stmt(sql)).await.unwrap();
    }
}
