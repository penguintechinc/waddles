//! `sea_orm` connection factory. Reads `DB_*` config (per-service account,
//! never a shared credential -- see `rules/security.md`) plus the
//! env-only `DB_PASSWORD` secret and returns a pooled
//! `DatabaseConnection`. Mirrors `core/svc_streaming/src/db/mod.rs` (the
//! reference template) exactly.
//!
//! No queries are issued here; that is a later chunk's job (TODO(M3):
//! executor integration -- blocked on M2 -- writes `action_dispatch_log`
//! rows through [`entities::action_dispatch_log`] once the dispatch loop
//! lands). This module only owns connecting and declares the entity for
//! this service's own table.

use sea_orm::{ConnectOptions, Database, DatabaseConnection, DbErr};
use tokio::sync::OnceCell;

use crate::config::Config;

pub mod entities;

/// Builds a connection URL from `Config`'s `DB_*` fields plus the
/// `DB_PASSWORD` secret. Never logged in full -- callers must not
/// `tracing::debug!` this value. `DB_TYPE` (`postgres` default, or
/// `sqlite`) is read directly from the environment rather than threaded
/// through `CliConfig`, matching `core/svc_streaming`'s documented
/// `DB_TYPE` behavior.
fn connection_url(config: &Config) -> String {
    let cli = &config.cli;
    match std::env::var("DB_TYPE").as_deref() {
        Ok("sqlite") => format!("sqlite://{name}?mode=rwc", name = cli.db_name),
        _ => format!(
            "postgres://{user}:{password}@{host}:{port}/{name}",
            user = cli.db_user,
            password = config.db_password.expose(),
            host = cli.db_host,
            port = cli.db_port,
            name = cli.db_name,
        ),
    }
}

/// Opens a pooled connection to the service's database.
/// `sqlx_logging` is disabled: the default sqlx query logger would echo
/// the connection URL (including the password) into logs at DEBUG level,
/// which `rules/critical-rules.md` Observability forbids.
pub async fn connect(config: &Config) -> Result<DatabaseConnection, DbErr> {
    let mut opts = ConnectOptions::new(connection_url(config));
    opts.max_connections(10)
        .min_connections(1)
        .sqlx_logging(false);
    Database::connect(opts).await
}

/// Process-wide connection pool, established on first use.
/// `DatabaseConnection` is a cheap `Clone` (wraps an `Arc`-backed pool), so
/// every caller gets a handle to the same underlying pool.
static CONNECTION: OnceCell<DatabaseConnection> = OnceCell::const_new();

pub async fn get_or_connect(config: &Config) -> Result<DatabaseConnection, DbErr> {
    let conn = CONNECTION.get_or_try_init(|| connect(config)).await?;
    Ok(conn.clone())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{CliConfig, Secret};
    use clap::Parser;
    use tokio::sync::Mutex;

    // DB_TYPE is process-global; serialize the tests that touch it so
    // parallel `cargo test` threads don't race on the same variable.
    // `tokio::sync::Mutex` (not `std::sync::Mutex`) -- its guard is `Send`
    // and safe to hold across an `.await`, which
    // `get_or_connect_establishes_and_caches_a_connection` below needs to
    // do; `std::sync::MutexGuard` held across an await point is a clippy
    // `await_holding_lock` deny.
    static ENV_LOCK: Mutex<()> = Mutex::const_new(());

    fn sample_config() -> Config {
        let cli = CliConfig::parse_from([
            "svc-action",
            "--db-host",
            "db.internal",
            "--db-port",
            "5433",
            "--db-name",
            "waddlebot_action",
            "--db-user",
            "svc_action",
        ]);
        Config {
            cli,
            db_password: Secret::new("test-password"),
            envelope_binding_keys: None,
            secret_key: Secret::new("test-jwt-signing-secret"),
            discord_bot_token: None,
        }
    }

    #[tokio::test]
    async fn connection_url_includes_configured_host_and_db_name() {
        let _guard = ENV_LOCK.lock().await;
        // SAFETY: serialized by ENV_LOCK.
        unsafe { std::env::remove_var("DB_TYPE") };
        let url = connection_url(&sample_config());
        assert_eq!(
            url,
            "postgres://svc_action:test-password@db.internal:5433/waddlebot_action"
        );
    }

    #[tokio::test]
    async fn db_type_sqlite_selects_a_sqlite_url() {
        let _guard = ENV_LOCK.lock().await;
        // SAFETY: serialized by ENV_LOCK.
        unsafe { std::env::set_var("DB_TYPE", "sqlite") };
        let url = connection_url(&sample_config());
        assert_eq!(url, "sqlite://waddlebot_action?mode=rwc");
        // Never leak "sqlite" into `postgres`-assuming DB_TYPE-unset tests.
        unsafe { std::env::remove_var("DB_TYPE") };
    }

    #[tokio::test]
    async fn get_or_connect_establishes_and_caches_a_connection() {
        let _guard = ENV_LOCK.lock().await;
        // SAFETY: serialized by ENV_LOCK.
        unsafe { std::env::set_var("DB_TYPE", "sqlite") };
        let path = std::env::temp_dir().join(format!(
            "svc-action-get-or-connect-test-{}.sqlite",
            std::process::id()
        ));
        let mut cli = CliConfig::parse_from(["svc-action"]);
        cli.db_name = path.to_string_lossy().to_string();
        let config = Config {
            cli,
            db_password: Secret::new("unused"),
            envelope_binding_keys: None,
            secret_key: Secret::new("unused"),
            discord_bot_token: None,
        };
        let first = get_or_connect(&config).await.expect("connects");
        // Second call returns the cached connection rather than
        // reconnecting -- both are cheap clones of the same pool.
        let second = get_or_connect(&config).await.expect("cached connection");
        drop(first);
        drop(second);
        unsafe { std::env::remove_var("DB_TYPE") };
        std::fs::remove_file(&path).ok();
    }
}
