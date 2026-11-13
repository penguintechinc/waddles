//! Real-Postgres tests for the direct database bundle-config reader (issue #1306).
//!
//! `TEST_DATABASE_URL` (owner credential, e.g. `postgresql://ci:ci@127.0.0.1:5432/ci`; CI starts
//! the container via the `postgres` input of `rust-crate-ci.yml`) is REQUIRED -- an unset variable
//! FAILS, never skips. Each test creates its own throwaway database plus a column-scoped
//! read-only role holding exactly the grants of alembic `0106_bundle_config_db_reads`, then
//! exercises [`DbConfigSource`] / [`BundleConfigCache`] as that role.

use std::sync::atomic::{AtomicI64, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use hub_client::bundle_config::{
    BundleConfigCache, CacheConfig, ConfigKey, ConfigSource, VersionProbe,
};
use hub_client::bundle_config_db::DbConfigSource;
use sea_orm::{ConnectionTrait, Database, DatabaseConnection};
use serde_json::{json, Value};

const APP: &str = "waddles.core.app.calendar";
const PASSWORD: &str = "bcro-test-password-0123456789";
static SEQ: AtomicU64 = AtomicU64::new(0);

/// The schema subset [`hub_client::bundle_config_db::CONFIG_SQL`] touches, with the columns the
/// role must NOT see (`updated_by`, `artifact_digest`, `staging_source_key`, `name`).
const SCHEMA: &str = "
CREATE TABLE tenants (id INTEGER PRIMARY KEY, slug TEXT NOT NULL);
CREATE TABLE communities (id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL REFERENCES tenants(id), name TEXT);
CREATE TABLE app_versions (id BIGSERIAL PRIMARY KEY, app_id TEXT NOT NULL, version TEXT NOT NULL, artifact_digest TEXT);
CREATE TABLE app_version_uploads (id BIGSERIAL PRIMARY KEY, app_id TEXT NOT NULL,
    app_version_id BIGINT REFERENCES app_versions(id), manifest_json JSONB, staging_source_key TEXT);
CREATE TABLE app_active_versions (app_id TEXT NOT NULL, tenant_id INTEGER NOT NULL,
    community_id INTEGER NOT NULL DEFAULT 0, version_id BIGINT NOT NULL REFERENCES app_versions(id),
    activated_by INTEGER, PRIMARY KEY (app_id, tenant_id, community_id));
CREATE TABLE bundle_community_config (id BIGSERIAL PRIMARY KEY, tenant_id INTEGER NOT NULL,
    community_id INTEGER NOT NULL, app_id TEXT NOT NULL, version INTEGER NOT NULL CHECK (version >= 1),
    config JSONB NOT NULL, schema_sha256 TEXT NOT NULL, updated_by INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), UNIQUE (tenant_id, community_id, app_id, version));
INSERT INTO tenants VALUES (1, 'a'), (2, 'b');
";

/// Exactly the grants of the migration (mirrored; the alembic test proves the migration itself).
const GRANTS: &str = "
GRANT SELECT (id, tenant_id) ON communities TO {role};
GRANT SELECT (id) ON app_versions TO {role};
GRANT SELECT (id, app_version_id, manifest_json) ON app_version_uploads TO {role};
GRANT SELECT (app_id, tenant_id, community_id, version_id) ON app_active_versions TO {role};
GRANT SELECT (tenant_id, community_id, app_id, version, config) ON bundle_community_config TO {role};
";

struct Env {
    owner: DatabaseConnection,
    role: String,
    role_url: String,
}

fn owner_url() -> String {
    std::env::var("TEST_DATABASE_URL")
        .expect("TEST_DATABASE_URL must be set: these tests need a REAL Postgres (no silent skip)")
}

/// `postgresql://{user}:{pw}@host:port/{db}` from the owner URL with another login and database.
fn url_for(owner: &str, user: &str, password: &str, db: &str) -> String {
    let rest = owner.split("://").nth(1).expect("scheme");
    let host_port = rest.split_once('@').expect("userinfo").1;
    let host_port = host_port.split('/').next().expect("host");
    format!("postgresql://{user}:{password}@{host_port}/{db}")
}

async fn exec(c: &DatabaseConnection, sql: &str) {
    c.execute_unprepared(sql).await.expect(sql);
}

/// Creates a fresh database + role, applies the schema and grants, returns the owner handle.
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
    let db = format!("bcdb_{n}");
    let role = format!("bc_ro_{n}");
    exec(&admin, &format!("CREATE DATABASE {db}")).await;
    exec(
        &admin,
        &format!("CREATE ROLE {role} LOGIN PASSWORD '{PASSWORD}'"),
    )
    .await;
    exec(&admin, &format!("GRANT CONNECT ON DATABASE {db} TO {role}")).await;
    // The owner login itself against the new database.
    let owner_db_dsn = {
        let (base, _) = owner_dsn.rsplit_once('/').expect("db path");
        format!("{base}/{db}")
    };
    let owner = Database::connect(owner_db_dsn)
        .await
        .expect("owner connect to test db");
    for stmt in SCHEMA.split(";\n").filter(|s| !s.trim().is_empty()) {
        exec(&owner, stmt).await;
    }
    for stmt in GRANTS
        .replace("{role}", &role)
        .split(";\n")
        .filter(|s| !s.trim().is_empty())
    {
        exec(&owner, stmt).await;
    }
    let role_url = url_for(&owner_dsn, &role, PASSWORD, &db);
    Env {
        owner,
        role,
        role_url,
    }
}

/// Seeds `communities` 10..10+n in tenant 1 (+ 99 in tenant 2), the calendar app active
/// tenant-wide with schema defaults, community 12 with its own activation, and saved configs.
async fn seed(env: &Env, n: i64) {
    let o = &env.owner;
    for c in 10..10 + n {
        exec(
            o,
            &format!("INSERT INTO communities VALUES ({c}, 1, 'c{c}')"),
        )
        .await;
    }
    exec(o, "INSERT INTO communities VALUES (99, 2, 'other')").await;
    let manifest = |dur: i64| {
        json!({
            "kind": "app",
            "community_config_schema": {"type": "object", "properties": {
                "default_duration_minutes": {"type": "integer", "default": dur},
                "flag": {"default": null},
                "label": {"type": "string"}
            }}
        })
        .to_string()
    };
    exec(o, &format!("INSERT INTO app_versions (id, app_id, version, artifact_digest) VALUES (1, '{APP}', '1.0.0', 'sha256:x'), (2, '{APP}', '2.0.0', 'sha256:y')")).await;
    exec(o, &format!("INSERT INTO app_version_uploads (app_id, app_version_id, manifest_json, staging_source_key) VALUES ('{APP}', 1, '{}'::jsonb, 'k1'), ('{APP}', 2, '{}'::jsonb, 'k2')", manifest(60), manifest(120))).await;
    exec(o, &format!("INSERT INTO app_active_versions (app_id, tenant_id, community_id, version_id) VALUES ('{APP}', 1, 0, 1), ('{APP}', 1, 12, 2)")).await;
    exec(o, &format!("INSERT INTO bundle_community_config (tenant_id, community_id, app_id, version, config, schema_sha256, updated_by) VALUES (1, 10, '{APP}', 1, '{{\"default_duration_minutes\":45}}', 'h', 7), (1, 10, '{APP}', 2, '{{\"default_duration_minutes\":30,\"extra\":true}}', 'h', 7)")).await;
}

fn key(community: i64) -> ConfigKey {
    ConfigKey::new(1, community, APP)
}

fn parsed(json: &str) -> Value {
    serde_json::from_str(json).expect("config_json is JSON")
}

struct AtomicProbe(AtomicI64);

#[async_trait]
impl VersionProbe for AtomicProbe {
    async fn latest(&self, _key: &ConfigKey) -> Option<i64> {
        Some(self.0.load(Ordering::SeqCst))
    }
}

/// Warm-up: 60 scopes -> exactly ONE statement; merge semantics, tenant gate and per-community
/// activation all match hub-api's `get_runtime_config`.
#[tokio::test]
async fn warm_up_is_one_batched_query_with_hub_api_merge_semantics() {
    let env = setup().await;
    seed(&env, 60).await;
    let src = Arc::new(DbConfigSource::new(
        env.role_url.clone(),
        Duration::from_secs(5),
    ));
    let cache = BundleConfigCache::new(src.clone(), None, CacheConfig::default());
    let mut keys: Vec<ConfigKey> = (10..70).map(key).collect();
    keys.push(ConfigKey::new(1, 99, APP)); // community of ANOTHER tenant: must not resolve
    keys.push(ConfigKey::new(1, 10, "waddles.core.app.inactive")); // not active: absent
    keys.push(key(10)); // duplicate scope: deduplicated
    let loaded = cache.seed(&keys).await;
    assert_eq!(src.query_count(), 1, "N scopes = ONE query");
    assert_eq!(loaded, 60, "cross-tenant + inactive scopes are absent");
    assert_eq!(cache.len(), 60);
    // Community 10: latest saved version (2) over the schema defaults; null default kept.
    let c10 = src.get(&key(10)).await.expect("c10");
    assert_eq!(c10.version, 2);
    assert_eq!(
        parsed(&c10.config_json),
        json!({"default_duration_minutes": 30, "extra": true, "flag": null})
    );
    // Community 11: never saved -> version 0, tenant-wide activation's defaults.
    let c11 = src.get(&key(11)).await.expect("c11");
    assert_eq!(c11.version, 0);
    assert_eq!(
        parsed(&c11.config_json),
        json!({"default_duration_minutes": 60, "flag": null})
    );
    // Community 12: its own activation (version 2 manifest) beats the tenant-wide one.
    let c12 = src.get(&key(12)).await.expect("c12");
    assert_eq!(parsed(&c12.config_json)["default_duration_minutes"], 120);
    // Cross-tenant and inactive lazy-load as NOT_FOUND.
    assert!(src.get(&ConfigKey::new(1, 99, APP)).await.is_err());
    assert!(src
        .get(&ConfigKey::new(1, 10, "waddles.core.app.inactive"))
        .await
        .is_err());
}

/// An unseeded scope lazy-loads on first lookup (one `get` query), then hits the cache.
#[tokio::test]
async fn lazy_load_on_cache_miss_then_hit() {
    let env = setup().await;
    seed(&env, 5).await;
    let src = Arc::new(DbConfigSource::new(
        env.role_url.clone(),
        Duration::from_secs(5),
    ));
    let cache = BundleConfigCache::new(src.clone(), None, CacheConfig::default());
    let v = cache.lookup(&key(10)).await;
    assert_eq!(parsed(&v)["extra"], true);
    assert_eq!(src.query_count(), 1);
    cache.lookup(&key(10)).await;
    assert_eq!(src.query_count(), 1, "second lookup is a cache hit");
}

/// A newer announced version (the Valkey invalidation) reloads from the database.
#[tokio::test]
async fn invalidation_refreshes_from_the_database() {
    let env = setup().await;
    seed(&env, 5).await;
    let src = Arc::new(DbConfigSource::new(
        env.role_url.clone(),
        Duration::from_secs(5),
    ));
    let probe = Arc::new(AtomicProbe(AtomicI64::new(2)));
    let cfg = CacheConfig {
        ttl: Duration::from_secs(60),
        recheck: Duration::from_millis(0),
    };
    let cache = BundleConfigCache::new(src.clone(), Some(probe.clone()), cfg);
    assert_eq!(
        parsed(&cache.lookup(&key(10)).await)["default_duration_minutes"],
        30
    );
    let before = src.query_count();
    // hub-api (the writer) saves version 3, then bumps the version key.
    exec(
        &env.owner,
        &format!("INSERT INTO bundle_community_config (tenant_id, community_id, app_id, version, config, schema_sha256) VALUES (1, 10, '{APP}', 3, '{{\"default_duration_minutes\":15}}', 'h')"),
    )
    .await;
    assert_eq!(
        parsed(&cache.lookup(&key(10)).await)["default_duration_minutes"],
        30,
        "same announced version: still cached, no reload"
    );
    assert_eq!(src.query_count(), before);
    probe.0.store(3, Ordering::SeqCst);
    assert_eq!(
        parsed(&cache.lookup(&key(10)).await)["default_duration_minutes"],
        15
    );
    assert_eq!(src.query_count(), before + 1);
}

/// The role is read-only and column-scoped: writes and unlisted columns/tables are refused.
#[tokio::test]
async fn read_only_role_cannot_write_or_read_other_columns() {
    let env = setup().await;
    seed(&env, 3).await;
    let ro = Database::connect(env.role_url.clone())
        .await
        .expect("role connect");
    for forbidden in [
        format!("INSERT INTO bundle_community_config (tenant_id, community_id, app_id, version, config, schema_sha256) VALUES (1, 10, '{APP}', 9, '{{}}', 'h')"),
        "UPDATE bundle_community_config SET config = '{}'::jsonb".to_string(),
        "DELETE FROM bundle_community_config".to_string(),
        "INSERT INTO communities VALUES (500, 1, 'x')".to_string(),
        "SELECT updated_by FROM bundle_community_config".to_string(),
        "SELECT schema_sha256 FROM bundle_community_config".to_string(),
        "SELECT artifact_digest FROM app_versions".to_string(),
        "SELECT staging_source_key FROM app_version_uploads".to_string(),
        "SELECT name FROM communities".to_string(),
        "SELECT slug FROM tenants".to_string(),
        format!("CREATE TABLE {}_t (x int)", env.role),
    ] {
        let err = ro.execute_unprepared(&forbidden).await.expect_err(&forbidden);
        assert!(
            err.to_string().to_lowercase().contains("permission denied"),
            "{forbidden}: {err}"
        );
    }
    // ...while the reader's own statement works as that role.
    let src = DbConfigSource::new(env.role_url.clone(), Duration::from_secs(5));
    assert!(src.get(&key(10)).await.is_ok());
}

/// Database down: the cache serves the last-known value; a never-seen scope errors loudly (the
/// registered manifest defaults, else `{}` with an ERROR) and nothing panics. Reads fail fast
/// inside the backoff window (an outage costs one attempt per window, not one per invocation).
#[tokio::test]
async fn database_down_serves_stale_and_never_panics() {
    let env = setup().await;
    seed(&env, 3).await;
    let src = Arc::new(
        DbConfigSource::new(env.role_url.clone(), Duration::from_secs(2))
            .with_failure_backoff(Duration::from_millis(600)),
    );
    let cfg = CacheConfig {
        ttl: Duration::from_millis(30),
        recheck: Duration::from_secs(60),
    };
    let cache = BundleConfigCache::new(src.clone(), None, cfg);
    let warm = cache.lookup(&key(10)).await;
    assert_eq!(parsed(&warm)["extra"], true);
    // Take the role's database away: no new logins, kill the pooled sessions.
    exec(&env.owner, &format!("ALTER ROLE {} NOLOGIN", env.role)).await;
    exec(
        &env.owner,
        &format!(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '{}'",
            env.role
        ),
    )
    .await;
    tokio::time::sleep(Duration::from_millis(60)).await; // ttl expired -> a reload is attempted
    let served = cache.lookup(&key(10)).await;
    assert_eq!(
        parsed(&served),
        parsed(&warm),
        "stale value served while the DB is down"
    );
    // Inside the backoff window further reads fail fast: no new statement reaches Postgres.
    let sent = src.query_count();
    assert_eq!(
        &*cache.lookup(&key(11)).await,
        "{}",
        "never-seen + down: loud empty"
    );
    cache.set_defaults(APP, r#"{"default_duration_minutes":60}"#);
    assert_eq!(
        &*cache.lookup(&key(11)).await,
        r#"{"default_duration_minutes":60}"#,
        "never-seen + down: registered manifest defaults"
    );
    assert_eq!(
        src.query_count(),
        sent,
        "fast-fail inside the backoff window"
    );
    // Recovery: after the window the next lookup reconnects and loads (lazy pool, no restart).
    exec(&env.owner, &format!("ALTER ROLE {} LOGIN", env.role)).await;
    tokio::time::sleep(Duration::from_millis(700)).await;
    assert_eq!(
        parsed(&cache.lookup(&key(11)).await)["default_duration_minutes"],
        60
    );
    assert!(src.query_count() > sent);
}
