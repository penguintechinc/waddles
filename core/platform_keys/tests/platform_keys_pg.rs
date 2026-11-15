//! Real-Postgres tests of [`platform_keys::pg::PgKeyLoader`] and the cache over it.
//!
//! `TEST_DATABASE_URL` (owner credential, e.g. `postgresql://ci:ci@127.0.0.1:5432/ci`; CI starts
//! the container via the `postgres` input of `rust-crate-ci.yml`) is REQUIRED -- an unset variable
//! FAILS, never skips. Each test creates its own throwaway database plus a login role holding
//! exactly the data-plane grants of alembic `0109_platform_vault` (schema USAGE + SELECT on the
//! `public_keys` view, nothing else), then drives the loader as that role.
#![cfg(feature = "postgres")]
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use platform_keys::pg::PgKeyLoader;
use platform_keys::{CacheConfig, KeyCache, KeyError, KeyLoader, KeyState, SetupError};
use sea_orm::{ConnectionTrait, Database, DatabaseConnection};

const PASSWORD: &str = "pk-test-password-0123456789";
const TYPE: i16 = 21;
static SEQ: AtomicU64 = AtomicU64::new(0);

/// The columns of `platform_vault.secrets` the view touches, plus the ones it must hide.
const SCHEMA: &str = "
CREATE SCHEMA platform_vault;
CREATE TABLE platform_vault.secrets (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    secret_type SMALLINT NOT NULL,
    scope_id BIGINT,
    version INTEGER NOT NULL,
    state SMALLINT NOT NULL DEFAULT 1,
    sealed_value BYTEA,
    kid TEXT,
    public_material BYTEA,
    fingerprint BYTEA NOT NULL DEFAULT '\\x00',
    activated_at TIMESTAMPTZ,
    rotated_at TIMESTAMPTZ
);
CREATE VIEW platform_vault.public_keys WITH (security_barrier = true) AS
SELECT secret_type, scope_id, version, kid, state, public_material, activated_at, rotated_at
FROM platform_vault.secrets
WHERE public_material IS NOT NULL AND state IN (2, 3);
";

struct Env {
    owner: DatabaseConnection,
    role_url: String,
}

fn owner_url() -> String {
    std::env::var("TEST_DATABASE_URL")
        .expect("TEST_DATABASE_URL must be set: these tests need a REAL Postgres (no silent skip)")
}

fn url_for(owner: &str, user: &str, password: &str, db: &str) -> String {
    let rest = owner.split("://").nth(1).expect("scheme");
    let host_port = rest.split_once('@').expect("userinfo").1;
    let host_port = host_port.split('/').next().expect("host");
    format!("postgresql://{user}:{password}@{host_port}/{db}")
}

async fn exec(c: &DatabaseConnection, sql: &str) {
    c.execute_unprepared(sql).await.expect(sql);
}

async fn setup() -> Env {
    let owner_dsn = owner_url();
    let n = format!(
        "{}_{}",
        std::process::id(),
        SEQ.fetch_add(1, Ordering::SeqCst)
    );
    let admin = Database::connect(owner_dsn.clone())
        .await
        .expect("owner connect");
    let db = format!("pkdb_{n}");
    let role = format!("pk_ro_{n}");
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
    for stmt in SCHEMA.split(";\n").filter(|s| !s.trim().is_empty()) {
        exec(&owner, stmt).await;
    }
    exec(
        &owner,
        &format!("GRANT USAGE ON SCHEMA platform_vault TO {role}"),
    )
    .await;
    exec(
        &owner,
        &format!("GRANT SELECT ON platform_vault.public_keys TO {role}"),
    )
    .await;
    Env {
        owner,
        role_url: url_for(&owner_dsn, &role, PASSWORD, &db),
    }
}

async fn insert(
    env: &Env,
    ty: i16,
    version: i32,
    state: i16,
    kid: &str,
    material: &str,
    rotated: &str,
) {
    exec(
        &env.owner,
        &format!(
            "INSERT INTO platform_vault.secrets \
             (secret_type, scope_id, version, state, sealed_value, kid, public_material, activated_at, rotated_at) \
             VALUES ({ty}, NULL, {version}, {state}, '\\xdeadbeef', '{kid}', {material}, now(), {rotated})"
        ),
    )
    .await;
}

const MAT: &str =
    "decode('0101010101010101010101010101010101010101010101010101010101010101', 'hex')";

fn loader(env: &Env) -> PgKeyLoader {
    PgKeyLoader::new(env.role_url.clone(), Duration::from_secs(5))
}

#[tokio::test]
async fn loads_only_active_and_verify_only_rows_with_decoded_fields() {
    let env = setup().await;
    insert(&env, TYPE, 1, 2, "active-1", MAT, "NULL").await;
    insert(
        &env,
        TYPE,
        2,
        3,
        "verify-1",
        MAT,
        "now() - interval '10 minutes'",
    )
    .await;
    insert(&env, TYPE, 3, 1, "pending-1", MAT, "NULL").await; // state 1: hidden by the view
    insert(&env, TYPE, 4, 4, "retired-1", MAT, "NULL").await; // state 4: hidden
    insert(&env, TYPE, 5, 2, "nomaterial", "NULL", "NULL").await; // no public material: hidden
    let rows = loader(&env).load_all().await.unwrap();
    assert_eq!(
        rows.len(),
        2,
        "view hides pending, retired and material-less rows"
    );
    let active = rows.iter().find(|r| r.kid == "active-1").unwrap();
    assert_eq!(
        (active.key_type, active.version, active.state),
        (TYPE, 1, KeyState::Active)
    );
    assert_eq!(active.public_material, vec![1u8; 32]);
    assert!(
        active.scope_id.is_none() && active.activated_at.is_some() && active.rotated_at.is_none()
    );
    let verify = rows.iter().find(|r| r.kid == "verify-1").unwrap();
    assert_eq!(verify.state, KeyState::VerifyOnly);
    let age = std::time::SystemTime::now()
        .duration_since(verify.rotated_at.unwrap())
        .unwrap();
    assert!(
        (590..=700).contains(&age.as_secs()),
        "rotated_at decoded as ~10 min ago, got {age:?}"
    );
}

#[tokio::test]
async fn kid_lookup_is_parameterised() {
    let env = setup().await;
    insert(&env, TYPE, 1, 2, "k1", MAT, "NULL").await;
    insert(&env, TYPE, 2, 2, "k2", MAT, "NULL").await;
    let l = loader(&env);
    assert_eq!(l.load_kid("k1").await.unwrap().len(), 1);
    assert!(l.load_kid("nope").await.unwrap().is_empty());
    assert!(
        l.load_kid("x' OR '1'='1").await.unwrap().is_empty(),
        "an injection string is just an unknown kid"
    );
    assert!(l.load_kid("k1' --").await.unwrap().is_empty());
}

#[tokio::test]
async fn the_data_plane_role_cannot_read_the_table_or_sealed_value() {
    let env = setup().await;
    insert(&env, TYPE, 1, 2, "k1", MAT, "NULL").await;
    let role_conn = Database::connect(env.role_url.clone()).await.unwrap();
    for sql in [
        "SELECT sealed_value FROM platform_vault.secrets",
        "SELECT count(*) FROM platform_vault.secrets",
        "DELETE FROM platform_vault.secrets",
    ] {
        assert!(
            role_conn.execute_unprepared(sql).await.is_err(),
            "must be denied: {sql}"
        );
    }
    assert!(role_conn
        .execute_unprepared("SELECT kid FROM platform_vault.public_keys")
        .await
        .is_ok());
}

#[tokio::test]
async fn cache_over_postgres_seeds_lazy_loads_and_expires_verify_only() {
    let env = setup().await;
    insert(&env, TYPE, 1, 2, "active-1", MAT, "NULL").await;
    // Rotated two hours ago against a one-hour window: already past verify_until.
    insert(
        &env,
        TYPE,
        0,
        3,
        "stale-verify",
        MAT,
        "now() - interval '2 hours'",
    )
    .await;
    let cfg = CacheConfig::new(vec![TYPE], Duration::from_secs(3600));
    let cache = KeyCache::new(Arc::new(loader(&env)), cfg).unwrap();
    cache.seed(Duration::from_secs(10)).await.unwrap();
    assert!(cache.ready());
    assert!(cache.get(TYPE, "active-1").await.is_ok());
    assert_eq!(
        cache.get(TYPE, "stale-verify").await,
        Err(KeyError::Expired)
    );
    // A key published after the seed is found by the lazy load without waiting for the poll.
    insert(&env, TYPE, 2, 2, "late", MAT, "NULL").await;
    assert!(cache.get(TYPE, "late").await.is_ok());
    // Unknown kid stays closed.
    assert_eq!(cache.get(TYPE, "ghost").await, Err(KeyError::UnknownKid));
    // Retiring the active key (state 4) removes it at the next refresh.
    exec(
        &env.owner,
        "UPDATE platform_vault.secrets SET state = 4 WHERE kid = 'active-1'",
    )
    .await;
    cache.refresh().await.unwrap();
    assert_eq!(cache.get(TYPE, "active-1").await, Err(KeyError::UnknownKid));
}

#[tokio::test]
async fn an_empty_view_never_becomes_ready() {
    let env = setup().await;
    let mut cfg = CacheConfig::new(vec![TYPE], Duration::from_secs(60));
    cfg.seed_backoff_initial = Duration::from_millis(20);
    cfg.seed_backoff_max = Duration::from_millis(40);
    let cache = KeyCache::new(Arc::new(loader(&env)), cfg).unwrap();
    let err = cache.seed(Duration::from_millis(300)).await.unwrap_err();
    assert!(matches!(err, SetupError::NotReady { ref missing, .. } if missing == &vec![TYPE]));
}

#[tokio::test]
async fn unreachable_database_is_unavailable_and_errors_never_leak_the_url() {
    let url = "postgresql://nobody:topsecretpw@127.0.0.1:1/none";
    let l = PgKeyLoader::new(url, Duration::from_millis(500));
    let err = l.load_all().await.unwrap_err().to_string();
    assert!(err.contains("unavailable"), "{err}");
    assert!(
        !err.contains("topsecretpw") && !err.contains("nobody"),
        "{err}"
    );
    let cfg = CacheConfig::new(vec![TYPE], Duration::from_secs(60));
    let cache = KeyCache::new(
        Arc::new(PgKeyLoader::new(url, Duration::from_millis(500))),
        cfg,
    )
    .unwrap();
    assert_eq!(cache.get(TYPE, "k").await, Err(KeyError::Unavailable));
}
