//! Real-Postgres check of `SeaOrmCaptionStore` against migration 102's
//! schema -- the only test that proves the SeaORM entity's Rust types agree
//! with the SQL column types (sqlx decodes strictly: an INT4 `community_id`
//! read into an `i64`, or a NUMERIC `confidence_score` into an `f64`, is a
//! runtime error that `MockDatabase` can never surface).
//!
//! `#[ignore]`d by default because it needs a live database (CI has no
//! Postgres service for this crate); run it explicitly against a THROWAWAY
//! database that has `communities` and migration 102 applied (over either a
//! fresh schema or a 007-shaped `caption_events` -- both paths are valid):
//!
//! ```text
//! CAPTION_TEST_DATABASE_URL=postgres://user:pass@127.0.0.1:5432/db \
//! CAPTION_TEST_COMMUNITY_ID=1 \
//!   cargo test --test caption_store_pg -- --ignored
//! ```
//!
//! It deletes expired rows (`purge_older_than`) -- never point it at data
//! you care about.

use chrono::{Duration, Utc};
use sea_orm::Database;
use svc_presentation::overlay::caption_store::{
    CaptionStore, NewCaptionEvent, SeaOrmCaptionStore, HISTORY_LIMIT,
};
use uuid::Uuid;

fn env(name: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| {
        panic!("{name} must be set to run this ignored test (see the module doc)")
    })
}

#[tokio::test]
#[ignore = "needs CAPTION_TEST_DATABASE_URL + CAPTION_TEST_COMMUNITY_ID (a throwaway Postgres with migration 102)"]
async fn store_round_trips_against_the_real_schema() {
    let db = Database::connect(env("CAPTION_TEST_DATABASE_URL"))
        .await
        .expect("connect to the test database");
    let community_id: i64 = env("CAPTION_TEST_COMMUNITY_ID")
        .parse()
        .expect("numeric community id");
    let store = SeaOrmCaptionStore::new(db);
    let author = Uuid::new_v4();
    let marker = format!("pg-roundtrip-{author}");

    store
        .insert(NewCaptionEvent {
            community_id,
            user_ref: author,
            platform: "twitch".to_string(),
            original: marker.clone(),
            translated: Some("hello".to_string()),
            detected_lang: Some("es".to_string()),
            target_lang: Some("en".to_string()),
            confidence: Some(0.93),
        })
        .await
        .expect("insert");
    store
        .insert(NewCaptionEvent {
            community_id,
            user_ref: author,
            platform: "discord".to_string(),
            original: format!("{marker}-bare"),
            translated: None,
            detected_lang: None,
            target_lang: None,
            confidence: None,
        })
        .await
        .expect("insert with every optional absent");

    let recent = store
        .recent(
            community_id,
            Utc::now() - Duration::minutes(5),
            HISTORY_LIMIT,
        )
        .await
        .expect("recent decodes every column (including any legacy rows)");

    let full = recent
        .iter()
        .find(|c| c.original == marker)
        .expect("the full caption is replayed");
    assert_eq!(full.user_ref, Some(author));
    assert_eq!(full.platform, "twitch");
    assert_eq!(full.translated.as_deref(), Some("hello"));
    assert_eq!(full.detected_lang.as_deref(), Some("es"));
    assert_eq!(full.target_lang.as_deref(), Some("en"));
    assert_eq!(full.confidence, Some(0.93));
    assert!(Utc::now() - full.created_at < Duration::minutes(1));

    let bare = recent
        .iter()
        .find(|c| c.original == format!("{marker}-bare"))
        .expect("the bare caption is replayed");
    assert!(bare.translated.is_none() && bare.confidence.is_none());

    // Oldest-first ordering: the bare caption was inserted second.
    let full_pos = recent.iter().position(|c| c.original == marker).unwrap();
    let bare_pos = recent
        .iter()
        .position(|c| c.original == format!("{marker}-bare"))
        .unwrap();
    assert!(full_pos < bare_pos);

    // Another community sees none of it.
    let other = store
        .recent(
            community_id + 1_000_000,
            Utc::now() - Duration::minutes(5),
            HISTORY_LIMIT,
        )
        .await
        .expect("recent for an unrelated community");
    assert!(other.iter().all(|c| !c.original.starts_with(&marker)));

    // Retention: nothing is older than a week, so a 7-day purge leaves them;
    // a purge with a cutoff in the future removes them.
    store
        .purge_older_than(Utc::now() - Duration::days(7))
        .await
        .expect("purge with a past cutoff");
    assert!(
        store
            .recent(
                community_id,
                Utc::now() - Duration::minutes(5),
                HISTORY_LIMIT
            )
            .await
            .unwrap()
            .iter()
            .any(|c| c.original == marker),
        "a fresh caption survives the 7-day purge"
    );
    let removed = store
        .purge_older_than(Utc::now() + Duration::hours(1))
        .await
        .expect("purge with a future cutoff");
    assert!(removed >= 2, "removed {removed}");
}
