//! Database connection setup shared by the DB-backed commands.
//!
//! Mirrors hub-api's `HubAPIConfig.from_env()` contract: `DATABASE_URL` wins, otherwise
//! `DB_TYPE` (must be `postgresql`) + `DB_HOST/PORT/NAME/USER/PASS`. The password is only ever
//! handed to the driver -- never logged, never an argv value.

use std::str::FromStr;
use std::time::Duration;

use sqlx::postgres::{PgConnectOptions, PgPoolOptions};
use sqlx::PgPool;

/// Configuration or connection failure (messages are secret-free).
#[derive(Debug, thiserror::Error)]
pub enum DbError {
    #[error("{0}")]
    Config(String),
    #[error("database error: {0}")]
    Sql(String),
}

/// Value-free description of a driver error (a driver message can embed bound values).
pub fn describe_sqlx_error(err: &sqlx::Error) -> String {
    match err {
        sqlx::Error::Database(e) => format!(
            "database error (sqlstate={}, constraint={})",
            e.code().as_deref().unwrap_or("?"),
            e.constraint().unwrap_or("-")
        ),
        sqlx::Error::Io(_) => "io error talking to the database".into(),
        sqlx::Error::Tls(_) => "tls error talking to the database".into(),
        sqlx::Error::PoolTimedOut => "timed out acquiring a database connection".into(),
        sqlx::Error::PoolClosed => "database pool closed".into(),
        sqlx::Error::RowNotFound => "row not found".into(),
        sqlx::Error::ColumnDecode { index, .. } => format!("column {index} decode error"),
        sqlx::Error::ColumnNotFound(c) => format!("column {c} not found"),
        sqlx::Error::Configuration(_) => "invalid database configuration".into(),
        _ => "database driver error".into(),
    }
}

impl From<sqlx::Error> for DbError {
    fn from(e: sqlx::Error) -> Self {
        Self::Sql(describe_sqlx_error(&e))
    }
}

/// Resolves connection options from the environment (fail-closed on bad config).
pub fn connect_options_from(
    env: &dyn Fn(&str) -> Option<String>,
) -> Result<PgConnectOptions, DbError> {
    if let Some(url) = env("DATABASE_URL").filter(|v| !v.is_empty()) {
        // The parse error from sqlx can quote the URL; replace it with a fixed message.
        return PgConnectOptions::from_str(&url)
            .map_err(|_| DbError::Config("DATABASE_URL is not a valid postgres URL".into()));
    }
    let db_type = env("DB_TYPE")
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| "postgresql".into());
    if db_type != "postgresql" {
        return Err(DbError::Config(format!(
            "unsupported DB_TYPE {db_type:?} -- waddles-ops supports postgresql only"
        )));
    }
    let get = |k: &str, d: &str| {
        env(k)
            .filter(|v| !v.is_empty())
            .unwrap_or_else(|| d.to_string())
    };
    let port: u16 = get("DB_PORT", "5432")
        .parse()
        .map_err(|_| DbError::Config("DB_PORT is not a valid port".into()))?;
    let mut opts = PgConnectOptions::new()
        .host(&get("DB_HOST", "infra-postgres"))
        .port(port)
        .database(&get("DB_NAME", "waddlebot"))
        .username(&get("DB_USER", "hub-api-rw"));
    if let Some(pw) = env("DB_PASS").filter(|v| !v.is_empty()) {
        opts = opts.password(&pw);
    }
    Ok(opts)
}

/// Connect/acquire timeout: `DB_CONNECT_TIMEOUT_SECS` (1..=300), default 10.
fn connect_timeout_secs() -> u64 {
    std::env::var("DB_CONNECT_TIMEOUT_SECS")
        .ok()
        .and_then(|v| v.parse::<u64>().ok())
        .filter(|v| (1..=300).contains(v))
        .unwrap_or(10)
}

/// Opens a small pool (these are one-shot operator jobs) from the process environment.
pub async fn connect_from_env() -> Result<PgPool, DbError> {
    let opts = connect_options_from(&|k| std::env::var(k).ok())?;
    let pool = PgPoolOptions::new()
        .max_connections(2)
        .acquire_timeout(Duration::from_secs(connect_timeout_secs()))
        .connect_with(opts)
        .await?;
    Ok(pool)
}
