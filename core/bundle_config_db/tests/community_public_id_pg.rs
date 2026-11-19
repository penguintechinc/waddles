//! Real-Postgres tests for the community public-id replica source
//! (`bundle_config_db::community_public_id`).
//!
//! `TEST_DATABASE_URL` (owner credential) is REQUIRED -- an unset variable FAILS, never skips.
//! Each test builds its own throwaway database plus a login holding exactly the column grants of
//! alembic `0132_community_public_id_read` (`communities (id, tenant_id, public_id)`) and reads
//! through [`PgPublicIdSource`] / [`CommunityPublicIds`] as that login.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use bundle_config_db::community_public_id::{
    install_global_from_value, CacheConfig, CommunityPublicIds, PgPublicIdSource, PublicIdSource,
};
use sea_orm::{ConnectionTrait, Database, DatabaseConnection};

const PASSWORD: &str = "pid-test-password-0123456789";
const PUB: &str = "0b6b0a52-5f0e-4f6a-9a39-1b6a3d6d9f01";
const PUB_OTHER: &str = "7f3c2d10-aaaa-4bbb-8ccc-0123456789ab";
static SEQ: AtomicU64 = AtomicU64::new(0);

fn owner_url() -> String {
    std::env::var("TEST_DATABASE_URL")
        .expect("TEST_DATABASE_URL must be set: these tests need a REAL Postgres (no silent skip)")
}

async fn exec(c: &DatabaseConnection, sql: &str) {
    c.execute_unprepared(sql).await.expect(sql);
}

/// A fresh database + column-scoped login with two seeded communities; returns the login URL.
async fn setup() -> String {
    let owner_dsn = owner_url();
    let n = format!(
        "{}_{}",
        std::process::id(),
        SEQ.fetch_add(1, Ordering::SeqCst)
    );
    let admin = Database::connect(owner_dsn.clone())
        .await
        .expect("owner connect");
    let db = format!("piddb_{n}");
    let role = format!("pid_ro_{n}");
    exec(&admin, &format!("CREATE DATABASE {db}")).await;
    exec(
        &admin,
        &format!("CREATE ROLE {role} LOGIN PASSWORD '{PASSWORD}'"),
    )
    .await;
    exec(&admin, &format!("GRANT CONNECT ON DATABASE {db} TO {role}")).await;
    let (base, _) = owner_dsn.rsplit_once('/').expect("db path");
    let owner = Database::connect(format!("{base}/{db}"))
        .await
        .expect("owner connect to test db");
    exec(
        &owner,
        "CREATE TABLE communities (id INTEGER PRIMARY KEY, name TEXT NOT NULL,
            tenant_id INTEGER NOT NULL, public_id VARCHAR(36))",
    )
    .await;
    exec(
        &owner,
        &format!(
            "INSERT INTO communities (id, name, tenant_id, public_id) VALUES
             (7, 'c7', 10, '{PUB}'), (8, 'c8', 11, '{PUB_OTHER}'), (9, 'c9', 10, NULL),
             (12, 'c12', 10, 'not-a-uuid')"
        ),
    )
    .await;
    exec(
        &owner,
        &format!("GRANT SELECT (id, tenant_id, public_id) ON communities TO {role}"),
    )
    .await;
    let rest = owner_dsn.split("://").nth(1).expect("scheme");
    let host_port = rest
        .split_once('@')
        .expect("userinfo")
        .1
        .split('/')
        .next()
        .expect("host");
    format!("postgresql://{role}:{PASSWORD}@{host_port}/{db}")
}

#[tokio::test]
async fn the_verified_pair_resolves_and_a_mismatched_tenant_resolves_to_nothing() {
    let src = PgPublicIdSource::new(setup().await);
    assert_eq!(src.fetch(10, 7).await.expect("fetch").as_deref(), Some(PUB));
    assert_eq!(src.fetch(11, 7).await.expect("fetch"), None, "wrong tenant");
    assert_eq!(
        src.fetch(10, 424_242).await.expect("fetch"),
        None,
        "unknown"
    );
    assert_eq!(
        src.fetch(10, 9).await.expect("fetch"),
        None,
        "NULL public_id"
    );
}

#[tokio::test]
async fn the_login_can_read_nothing_else_on_communities() {
    let url = setup().await;
    let conn = Database::connect(url).await.expect("login connect");
    let denied = conn
        .execute_unprepared("SELECT name FROM communities")
        .await
        .expect_err("name is not granted");
    assert!(denied.to_string().to_lowercase().contains("permission"));
}

#[tokio::test]
async fn the_cache_over_postgres_serves_a_uuid_and_rejects_a_malformed_value() {
    let cache = CommunityPublicIds::new(
        Arc::new(PgPublicIdSource::new(setup().await)),
        CacheConfig::default(),
    );
    assert_eq!(cache.lookup(10, 7).await.as_deref(), Some(PUB));
    assert_eq!(cache.lookup(10, 7).await.as_deref(), Some(PUB), "cached");
    assert_eq!(cache.lookup(11, 8).await.as_deref(), Some(PUB_OTHER));
    assert_eq!(
        cache.lookup(10, 12).await,
        None,
        "malformed value is absent"
    );
    assert_eq!(cache.lookup(11, 7).await, None, "tenant mismatch");
}

#[tokio::test]
async fn an_unreachable_replica_is_an_error_that_never_carries_the_credential() {
    let url = "postgresql://nobody:s3cret-pw@127.0.0.1:1/none".to_string();
    let src = PgPublicIdSource::new(url);
    let err = src.fetch(10, 7).await.expect_err("unreachable");
    assert!(!err.contains("s3cret-pw"), "{err}");
    // The cache degrades to "absent" (never an error, never fabricated).
    let cache = CommunityPublicIds::new(
        Arc::new(PgPublicIdSource::new(
            "postgresql://nobody:s3cret-pw@127.0.0.1:1/none".to_string(),
        )),
        CacheConfig::default(),
    );
    assert_eq!(cache.lookup(10, 7).await, None);
}

#[tokio::test]
async fn install_global_from_a_replica_url_installs_the_process_wide_cache() {
    let url = setup().await;
    let cache = install_global_from_value(Some(url)).expect("installed");
    assert_eq!(cache.lookup(10, 7).await.as_deref(), Some(PUB));
    assert!(bundle_config_db::community_public_id::global().is_some());
}
