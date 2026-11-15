//! Postgres [`KeyLoader`] over the replica view `platform_vault.public_keys` (feature `postgres`).
//!
//! Both statements are fixed literals; the only caller-controlled value (`kid`) is a bound
//! parameter. The connecting role needs `SELECT` on the view and `USAGE` on the schema -- exactly
//! the grants of alembic `0109_platform_vault` for the `svc_*` roles; nothing is granted or
//! assumed beyond that. Errors carry a kind string only, never the connection URL.

use std::time::{Duration, SystemTime};

use sea_orm::{
    ConnectOptions, ConnectionTrait, Database, DatabaseConnection, DbBackend, Statement,
};
use tokio::sync::Mutex;

use crate::{KeyLoader, KeyRecord, KeyState, LoaderError};

/// Column list shared by both statements. Timestamps are read as epoch seconds so no datetime
/// crate feature is needed.
macro_rules! cols {
    () => {
        "secret_type, scope_id, version, kid, state, public_material, \
         EXTRACT(EPOCH FROM activated_at)::float8, EXTRACT(EPOCH FROM rotated_at)::float8"
    };
}

/// Every verifiable key (seed / poll).
pub const ALL_SQL: &str = concat!(
    "SELECT ",
    cols!(),
    " FROM platform_vault.public_keys WHERE kid IS NOT NULL"
);

/// The keys carrying one kid (lazy load); `$1` is the kid.
pub const KID_SQL: &str = concat!(
    "SELECT ",
    cols!(),
    " FROM platform_vault.public_keys WHERE kid = $1"
);

/// Loads keys from the read-only replica with a small lazily-created pool.
pub struct PgKeyLoader {
    url: String,
    timeout: Duration,
    conn: Mutex<Option<DatabaseConnection>>,
}

/// Coarse error classification safe to log (no URL, no driver message).
fn error_kind(e: &sea_orm::DbErr) -> &'static str {
    match e {
        sea_orm::DbErr::ConnectionAcquire(_) | sea_orm::DbErr::Conn(_) => "connection",
        sea_orm::DbErr::Query(_) | sea_orm::DbErr::Exec(_) => "query",
        sea_orm::DbErr::Type(_) | sea_orm::DbErr::TryIntoErr { .. } => "decode",
        _ => "other",
    }
}

fn epoch(secs: Option<f64>) -> Result<Option<SystemTime>, LoaderError> {
    match secs {
        None => Ok(None),
        Some(s) if s.is_finite() && s >= 0.0 => {
            Ok(Some(SystemTime::UNIX_EPOCH + Duration::from_secs_f64(s)))
        }
        Some(_) => Err(LoaderError::Malformed("timestamp out of range".to_string())),
    }
}

impl PgKeyLoader {
    /// `url` is the data-plane read-only role's connection string (from env, never logged).
    #[must_use]
    pub fn new(url: impl Into<String>, timeout: Duration) -> Self {
        Self {
            url: url.into(),
            timeout,
            conn: Mutex::new(None),
        }
    }

    async fn connection(&self) -> Result<DatabaseConnection, LoaderError> {
        let mut slot = self.conn.lock().await;
        if let Some(c) = slot.as_ref() {
            return Ok(c.clone());
        }
        let mut opts = ConnectOptions::new(self.url.clone());
        opts.max_connections(2)
            .min_connections(0)
            .connect_timeout(self.timeout)
            .acquire_timeout(self.timeout)
            .sqlx_logging(false);
        let conn = Database::connect(opts)
            .await
            .map_err(|e| LoaderError::Unavailable(format!("connect: {}", error_kind(&e))))?;
        *slot = Some(conn.clone());
        Ok(conn)
    }

    async fn run(&self, stmt: Statement) -> Result<Vec<KeyRecord>, LoaderError> {
        let conn = self.connection().await?;
        let rows = tokio::time::timeout(self.timeout, conn.query_all_raw(stmt))
            .await
            .map_err(|_| LoaderError::Unavailable("query timed out".to_string()))?
            .map_err(|e| LoaderError::Unavailable(format!("query: {}", error_kind(&e))))?;
        let dec = |e: sea_orm::DbErr| LoaderError::Malformed(format!("decode: {}", error_kind(&e)));
        let mut out = Vec::with_capacity(rows.len());
        for row in rows {
            let key_type: i16 = row.try_get_by_index(0).map_err(dec)?;
            let scope_id: Option<i64> = row.try_get_by_index(1).map_err(dec)?;
            let version: i32 = row.try_get_by_index(2).map_err(dec)?;
            let kid: Option<String> = row.try_get_by_index(3).map_err(dec)?;
            let state: i16 = row.try_get_by_index(4).map_err(dec)?;
            let material: Option<Vec<u8>> = row.try_get_by_index(5).map_err(dec)?;
            let activated: Option<f64> = row.try_get_by_index(6).map_err(dec)?;
            let rotated: Option<f64> = row.try_get_by_index(7).map_err(dec)?;
            let (Some(kid), Some(public_material)) = (kid, material) else {
                return Err(LoaderError::Malformed(
                    "view returned a row without kid or material".to_string(),
                ));
            };
            out.push(KeyRecord {
                key_type,
                scope_id,
                version,
                kid,
                state: KeyState::from_code(state)?,
                public_material,
                activated_at: epoch(activated)?,
                rotated_at: epoch(rotated)?,
            });
        }
        Ok(out)
    }
}

#[async_trait::async_trait]
impl KeyLoader for PgKeyLoader {
    async fn load_all(&self) -> Result<Vec<KeyRecord>, LoaderError> {
        self.run(Statement::from_string(DbBackend::Postgres, ALL_SQL))
            .await
    }

    async fn load_kid(&self, kid: &str) -> Result<Vec<KeyRecord>, LoaderError> {
        self.run(Statement::from_sql_and_values(
            DbBackend::Postgres,
            KID_SQL,
            [kid.to_owned().into()],
        ))
        .await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn statements_are_fixed_view_reads() {
        for sql in [ALL_SQL, KID_SQL] {
            assert!(sql.contains("FROM platform_vault.public_keys"));
            assert!(!sql.to_lowercase().contains("sealed_value"));
        }
        assert!(KID_SQL.ends_with("kid = $1"));
    }

    #[test]
    fn epoch_bounds() {
        assert_eq!(epoch(None).ok(), Some(None));
        assert!(epoch(Some(1.5)).is_ok());
        assert!(epoch(Some(-1.0)).is_err());
        assert!(epoch(Some(f64::NAN)).is_err());
    }
}
