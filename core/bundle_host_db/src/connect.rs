//! Production Postgres connection factory for the bundle `db` capability --
//! connects as the least-privilege, read-write `waddles_bundle_runtime`
//! role (`alembic/versions/0030_bundle_app_schemas.py` provisions the role
//! and its `app_core`/`app_community` grants; this crate is DML-only and
//! never issues DDL, see `crate::lib`'s module doc).
//!
//! Mirrors `core/bundle_active_set::reader`'s `ReaderConfig`/`connect`
//! shape byte-for-byte (own small config struct, password passed
//! separately to [`connect`] rather than carried on the struct -- Token &
//! Secret Hygiene), with one deliberate difference: that crate's factory is
//! **read-only** (`options[default_transaction_read_only]=on`, a
//! SELECT-only loader); this one is **read-write**, since `insert`/
//! `update`/`delete` are core `db` capability operations, not just reads.
//!
//! Each of `core/svc_process`/`core/svc_action`'s own `lib.rs` startup
//! wiring calls [`connect`] once at process start (never per-invoke) and
//! wraps the resulting [`sea_orm::DatabaseConnection`] in
//! [`crate::backend::PostgresBackend`] -- see those crates' own
//! `BUNDLE_DB_*` env var docs for the exact Helm-values knobs that make
//! this connection, and therefore the `storage.tables` capability itself,
//! actually usable in a given deployment.

use sea_orm::{ConnectOptions, Database, DatabaseConnection, DbErr};

/// Non-secret connection settings for the bundle-runtime endpoint --
/// `BUNDLE_DB_*` in each service's own `CliConfig`. The password is never
/// carried on this struct; callers pass it directly to [`connect`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConnectConfig {
    pub host: String,
    pub port: u16,
    pub name: String,
    /// Expected to be `waddles_bundle_runtime` in every real deployment
    /// (the role `0030_bundle_app_schemas.py` provisions) -- kept
    /// configurable rather than hardcoded only so a test/alpha deployment
    /// can point this at a differently-named role without a code change.
    pub user: String,
}

fn connection_url(cfg: &ConnectConfig, password: &str) -> String {
    format!(
        "postgres://{user}:{password}@{host}:{port}/{name}",
        user = cfg.user,
        password = password,
        host = cfg.host,
        port = cfg.port,
        name = cfg.name,
    )
}

/// Opens a pooled, read-write connection under `cfg.user` (production:
/// `waddles_bundle_runtime`). `sqlx_logging(false)` -- never echo the
/// password-bearing URL at DEBUG (same rationale as
/// `core/bundle_active_set::reader::connect`). Pool sized for a
/// shared-per-process connection serving every concurrent bundle `db`
/// invocation on this instance (unlike that crate's 4-connection reader
/// pool, which serves at most one query per poll tick).
pub async fn connect(cfg: &ConnectConfig, password: &str) -> Result<DatabaseConnection, DbErr> {
    let mut opts = ConnectOptions::new(connection_url(cfg, password));
    opts.max_connections(10)
        .min_connections(1)
        .sqlx_logging(false);
    Database::connect(opts).await
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> ConnectConfig {
        ConnectConfig {
            host: "waddles-pg.internal".to_string(),
            port: 5432,
            name: "waddlebot".to_string(),
            user: "waddles_bundle_runtime".to_string(),
        }
    }

    #[test]
    fn connection_url_includes_configured_host_and_role() {
        let url = connection_url(&sample(), "s3cret");
        assert_eq!(
            url,
            "postgres://waddles_bundle_runtime:s3cret@waddles-pg.internal:5432/waddlebot"
        );
    }

    /// Unlike `bundle_active_set::reader`'s factory, this one must NOT
    /// carry the read-only startup option -- `insert`/`update`/`delete`
    /// need a genuinely read-write session.
    #[test]
    fn connection_url_never_carries_the_read_only_startup_option() {
        let url = connection_url(&sample(), "s3cret");
        assert!(
            !url.contains("default_transaction_read_only"),
            "bundle db capability needs read-write connections"
        );
    }

    #[test]
    fn connection_url_is_a_well_formed_postgres_url() {
        let parsed = url::Url::parse(&connection_url(&sample(), "s3cret"))
            .expect("connection_url must always produce a valid URL");
        assert_eq!(parsed.scheme(), "postgres");
        assert_eq!(parsed.host_str(), Some("waddles-pg.internal"));
        assert_eq!(parsed.username(), "waddles_bundle_runtime");
    }
}
