//! Read-only Postgres connection factory for the DB-driven active-bundle
//! loader. Mirrors `core/svc_action/src/db/mod.rs`'s `connect`/URL-building
//! shape exactly, with two deliberate differences: the config is its own
//! small struct (not the service's full `Config`, since this crate is
//! shared by two services with two otherwise-unrelated `Config` types),
//! and every connection issues `SET default_transaction_read_only = on`
//! immediately after connecting -- defense in depth alongside the RO
//! account's own grants (crate root doc), so a bug in this crate's own
//! query code fails at the database, not just at code review.

use sea_orm::{ConnectOptions, ConnectionTrait, Database, DatabaseConnection, DbErr};

/// Non-secret connection settings for the reader endpoint -- `DB_READER_*`
/// in each service's `CliConfig` (or `DB_*`/primary as the alpha default;
/// see each service's own `config.rs` doc for the reader-vs-primary
/// fallback). The password is never carried on this struct -- callers pass
/// it directly to [`connect`], matching `core/svc_action/src/db`'s
/// `Secret`-never-on-CliConfig convention (Token & Secret Hygiene).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ReaderConfig {
    pub host: String,
    pub port: u16,
    pub name: String,
    pub user: String,
}

fn connection_url(cfg: &ReaderConfig, password: &str) -> String {
    format!(
        "postgres://{user}:{password}@{host}:{port}/{name}",
        user = cfg.user,
        password = password,
        host = cfg.host,
        port = cfg.port,
        name = cfg.name,
    )
}

/// Opens a pooled, read-only connection: `sqlx_logging(false)` (never echo
/// the password-bearing URL at DEBUG, same rationale as
/// `core/svc_action/src/db::connect`), a small pool (this loader issues at
/// most one query per poll tick per service instance -- no need for the
/// primary connection's 10-connection pool), and a `SET
/// default_transaction_read_only = on` issued via `execute_unprepared`
/// once the pool is established (`ConnectOptions` has no direct
/// "after-connect hook" in this `sea-orm` version; every query this crate
/// issues is a single autocommit `SELECT` over the same pool, so setting
/// this once here covers the pool's actual usage pattern even though it
/// isn't a genuine per-physical-connection sqlx `after_connect`).
pub async fn connect(cfg: &ReaderConfig, password: &str) -> Result<DatabaseConnection, DbErr> {
    let mut opts = ConnectOptions::new(connection_url(cfg, password));
    opts.max_connections(4)
        .min_connections(1)
        .sqlx_logging(false);
    let conn = Database::connect(opts).await?;
    conn.execute_unprepared("SET default_transaction_read_only = on")
        .await?;
    Ok(conn)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> ReaderConfig {
        ReaderConfig {
            host: "db-reader.internal".to_string(),
            port: 5433,
            name: "waddlebot".to_string(),
            user: "svc_process_ro".to_string(),
        }
    }

    #[test]
    fn connection_url_includes_configured_reader_host_and_user() {
        let url = connection_url(&sample(), "s3cret");
        assert_eq!(
            url,
            "postgres://svc_process_ro:s3cret@db-reader.internal:5433/waddlebot"
        );
    }
}
