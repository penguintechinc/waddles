//! Real-Postgres check of `SeaOrmOverlayCodeResolver` against alembic
//! `0056_communities_overlay_code`'s schema -- the only test that proves the
//! SeaORM entity's Rust types agree with the SQL column types (sqlx decodes
//! strictly: an INT4 `id` read into the wrong width, or a `VARCHAR(16)` into
//! a non-string, is a runtime error that `MockDatabase` can never surface) and
//! that the resolver's `WHERE overlay_code = $1` is served by the real UNIQUE
//! index.
//!
//! `#[ignore]`d by default because it needs a live database (CI has no
//! Postgres service for this crate); run it explicitly against a THROWAWAY
//! database whose `communities` table has migration 0056 applied and holds at
//! least one community:
//!
//! ```text
//! OVERLAY_CODE_TEST_DATABASE_URL=postgres://user:pass@127.0.0.1:5432/db \
//!   cargo test --test overlay_code_pg -- --ignored
//! ```
//!
//! It only reads and, for the last check, inserts one row it then deletes.

use sea_orm::{ConnectionTrait, Database, DatabaseBackend, Statement};
use svc_presentation::overlay::code::{OverlayCodeResolver, SeaOrmOverlayCodeResolver};

fn env(name: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| {
        panic!("{name} must be set to run this ignored test (see the module doc)")
    })
}

#[tokio::test]
#[ignore = "needs OVERLAY_CODE_TEST_DATABASE_URL (a throwaway Postgres with alembic 0056 applied)"]
async fn resolver_round_trips_every_community_against_the_real_schema() {
    let db = Database::connect(env("OVERLAY_CODE_TEST_DATABASE_URL"))
        .await
        .expect("connect to the test database");

    let rows = db
        .query_all_raw(Statement::from_string(
            DatabaseBackend::Postgres,
            "SELECT id, overlay_code FROM communities ORDER BY id",
        ))
        .await
        .expect("select communities");
    // A zero denominator would make every assertion below vacuous.
    assert!(
        !rows.is_empty(),
        "the test database has no communities; seed at least one"
    );

    // Zero TTL: every lookup must reach the database, not the cache.
    let resolver = SeaOrmOverlayCodeResolver::with_ttl(db.clone(), std::time::Duration::ZERO);
    let mut seen = std::collections::HashSet::new();
    for row in &rows {
        let id: i32 = row.try_get("", "id").expect("id is INT4");
        let code: String = row
            .try_get("", "overlay_code")
            .expect("overlay_code is a string");
        assert!(
            svc_presentation::overlay::code::is_valid_code(&code),
            "stored code {code:?} is not 16 lowercase hex"
        );
        assert!(seen.insert(code.clone()), "duplicate overlay_code {code:?}");
        assert_eq!(
            resolver.resolve(&code).await.expect("lookup"),
            Some(i64::from(id)),
            "community {id}"
        );
    }
    println!(
        "resolved {} communities, all codes distinct and well-formed",
        rows.len()
    );

    // Absent and near-miss codes resolve to nothing.
    assert_eq!(resolver.resolve("ffffffffffffffff").await.unwrap(), None);
    let first = rows[0]
        .try_get::<String>("", "overlay_code")
        .expect("code")
        .to_uppercase();
    assert_eq!(resolver.resolve(&first).await.unwrap(), None);

    // A community inserted WITHOUT a code gets one from the column DEFAULT, and
    // resolves immediately.
    let inserted = db
        .query_one_raw(Statement::from_string(
            DatabaseBackend::Postgres,
            "INSERT INTO communities (name) VALUES ('overlay-code-pg-test') \
             RETURNING id, overlay_code",
        ))
        .await
        .expect("insert a community")
        .expect("RETURNING row");
    let new_id: i32 = inserted.try_get("", "id").expect("id");
    let new_code: String = inserted.try_get("", "overlay_code").expect("code");
    let resolved = resolver.resolve(&new_code).await;
    db.execute_raw(Statement::from_sql_and_values(
        DatabaseBackend::Postgres,
        "DELETE FROM communities WHERE id = $1",
        [new_id.into()],
    ))
    .await
    .expect("clean up the inserted community");
    assert_eq!(resolved.unwrap(), Some(i64::from(new_id)));
}
