//! Real-Postgres tests for the command-latency label replica source (`bundle_config_db::labels`).
//!
//! `TEST_DATABASE_URL` (owner credential) is REQUIRED -- an unset variable FAILS, never skips.
//! Each test builds its own throwaway database plus a login holding exactly the column grants of
//! alembic `0112_latency_label_reads`, and reads through [`PgLabelSource`] as that login.

use std::sync::atomic::{AtomicU64, Ordering};

use bundle_config_db::labels::PgLabelSource;
use command_latency::labels::{LabelSource, Wanted};
use command_latency::registry::{MAX_BUNDLES, MAX_COMMANDS};
use sea_orm::{ConnectionTrait, Database, DatabaseConnection};

const PASSWORD: &str = "lbl-test-password-0123456789";
static SEQ: AtomicU64 = AtomicU64::new(0);

const SCHEMA: &str = "
CREATE TABLE app_commands (id BIGSERIAL PRIMARY KEY, community_id INTEGER NOT NULL, command TEXT NOT NULL, handler TEXT);
CREATE TABLE app_versions (id BIGSERIAL PRIMARY KEY, app_id TEXT NOT NULL, version TEXT NOT NULL,
    language TEXT NOT NULL, artifact_digest TEXT);
";

const GRANTS: &str = "
GRANT SELECT (command) ON app_commands TO {role};
GRANT SELECT (id, app_id, language) ON app_versions TO {role}
";

fn owner_url() -> String {
    std::env::var("TEST_DATABASE_URL")
        .expect("TEST_DATABASE_URL must be set: these tests need a REAL Postgres (no silent skip)")
}

async fn exec(c: &DatabaseConnection, sql: &str) {
    c.execute_unprepared(sql).await.expect(sql);
}

/// A fresh database + column-scoped login; returns (owner handle, login URL).
async fn setup() -> (DatabaseConnection, String) {
    let owner_dsn = owner_url();
    let n = format!(
        "{}_{}",
        std::process::id(),
        SEQ.fetch_add(1, Ordering::SeqCst)
    );
    let admin = Database::connect(owner_dsn.clone())
        .await
        .expect("owner connect");
    let db = format!("lbldb_{n}");
    let role = format!("lbl_ro_{n}");
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
    for stmt in GRANTS.replace("{role}", &role).split(";\n") {
        exec(&owner, stmt).await;
    }
    let rest = owner_dsn.split("://").nth(1).expect("scheme");
    let host_port = rest
        .split_once('@')
        .expect("userinfo")
        .1
        .split('/')
        .next()
        .expect("host");
    (
        owner,
        format!("postgresql://{role}:{PASSWORD}@{host_port}/{db}"),
    )
}

const BOTH: Wanted = Wanted {
    commands: true,
    bundles: true,
};

#[tokio::test]
async fn commands_are_ranked_by_registration_breadth_and_the_wildcard_is_not_a_label() {
    let (owner, url) = setup().await;
    exec(
        &owner,
        "INSERT INTO app_commands (community_id, command) VALUES
         (1,'ping'),(2,'ping'),(3,'ping'),(1,'roll'),(2,'roll'),(1,'alpha'),(1,'*'),(2,'*')",
    )
    .await;
    let src = PgLabelSource::new(url);
    let snap = src
        .load(Wanted {
            commands: true,
            bundles: false,
        })
        .await
        .expect("load");
    assert_eq!(
        snap.commands,
        Some(vec![
            "ping".to_string(),
            "roll".to_string(),
            "alpha".to_string()
        ])
    );
    assert_eq!(snap.bundles, None, "bundles were not requested");
}

#[tokio::test]
async fn bundles_carry_the_language_of_their_newest_version() {
    let (owner, url) = setup().await;
    exec(
        &owner,
        "INSERT INTO app_versions (app_id, version, language) VALUES
         ('app.one','1.0.0','python'),('app.one','2.0.0','rust'),('app.two','1.0.0','python')",
    )
    .await;
    let src = PgLabelSource::new(url);
    let snap = src
        .load(Wanted {
            commands: false,
            bundles: true,
        })
        .await
        .expect("load");
    assert_eq!(snap.commands, None, "commands were not requested");
    let mut got = snap.bundles.expect("bundles");
    got.sort();
    assert_eq!(
        got,
        vec![
            ("app.one".to_string(), "rust".to_string()),
            ("app.two".to_string(), "python".to_string())
        ]
    );
}

#[tokio::test]
async fn empty_tables_load_as_empty_vocabularies_not_errors() {
    let (_owner, url) = setup().await;
    let snap = PgLabelSource::new(url).load(BOTH).await.expect("load");
    assert_eq!(snap.commands, Some(vec![]));
    assert_eq!(snap.bundles, Some(vec![]));
}

#[tokio::test]
async fn more_rows_than_the_cap_returns_cap_plus_one_so_truncation_is_detectable() {
    let (owner, url) = setup().await;
    exec(
        &owner,
        &format!(
            "INSERT INTO app_commands (community_id, command)
             SELECT 1, 'cmd' || g FROM generate_series(1, {n}) g",
            n = MAX_COMMANDS + 10
        ),
    )
    .await;
    exec(
        &owner,
        &format!(
            "INSERT INTO app_versions (app_id, version, language)
             SELECT 'app.' || g, '1.0.0', 'python' FROM generate_series(1, {n}) g",
            n = MAX_BUNDLES + 10
        ),
    )
    .await;
    let snap = PgLabelSource::new(url).load(BOTH).await.expect("load");
    assert_eq!(snap.commands.expect("commands").len(), MAX_COMMANDS + 1);
    assert_eq!(snap.bundles.expect("bundles").len(), MAX_BUNDLES + 1);
}

#[tokio::test]
async fn a_missing_table_is_a_loud_error_that_never_carries_the_credential() {
    let (owner, url) = setup().await;
    exec(&owner, "DROP TABLE app_commands").await;
    let err = PgLabelSource::new(url)
        .load(Wanted {
            commands: true,
            bundles: false,
        })
        .await
        .expect_err("a missing table must fail, not yield an empty vocabulary");
    assert!(err.starts_with("label query:"), "{err}");
    assert!(!err.contains(PASSWORD), "credential leaked: {err}");
}

#[tokio::test]
async fn an_unreachable_replica_is_an_error_not_an_empty_snapshot() {
    let src = PgLabelSource::new(format!("postgresql://nobody:{PASSWORD}@127.0.0.1:1/none"));
    let err = src
        .load(BOTH)
        .await
        .expect_err("unreachable replica must fail");
    assert!(!err.contains(PASSWORD), "credential leaked: {err}");
}
