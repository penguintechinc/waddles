//! Postgres implementation of [`PlatformTableStore`] (SeaORM, parameterized).

use std::time::Instant;

use sea_orm::{
    ConnectOptions, ConnectionTrait, Database, DatabaseConnection, DbBackend, DbErr, Statement,
    TransactionTrait, Value,
};
use uuid::Uuid;

use crate::{
    metrics, validate_read, validate_write, BoxFuture, Change, PlatformTableError,
    PlatformTableScope, PlatformTableStore, PROTECTED_ROLES,
};

/// Per-transaction statement timeout so a stuck row lock can never hold a
/// bundle invocation past the host call deadline.
const STATEMENT_TIMEOUT_MS: u32 = 5_000;

/// Target-row predicate shared by every statement: exactly this tenant's
/// community (BOTH ids host-injected) and an active, non-removed member.
/// `is_active IS TRUE`: a NULL is not an active member (fail closed).
/// Bind order is always `$1 = tenant_id`, `$2 = community_id`, `$3 = user`.
pub(crate) const TARGET_PREDICATE: &str = "cm.community_id = $2 AND c.tenant_id = $1 \
     AND cm.user_uuid = $3 AND cm.is_active IS TRUE AND cm.removed_at IS NULL \
     AND cm.left_at IS NULL";

/// Non-secret connection settings.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConnectConfig {
    pub host: String,
    pub port: u16,
    pub name: String,
    /// `waddles_platform_table` in every real deployment.
    pub user: String,
}

/// Opens the pooled connection. `sqlx_logging(false)` -- never echo the URL.
pub async fn connect(cfg: &ConnectConfig, password: &str) -> Result<DatabaseConnection, DbErr> {
    let url = format!(
        "postgres://{user}:{password}@{host}:{port}/{name}",
        user = cfg.user,
        host = cfg.host,
        port = cfg.port,
        name = cfg.name,
    );
    let mut opts = ConnectOptions::new(url);
    opts.max_connections(4)
        .min_connections(1)
        .sqlx_logging(false);
    Database::connect(opts).await
}

fn backend_err(e: impl std::fmt::Display) -> PlatformTableError {
    PlatformTableError::Backend(e.to_string())
}

fn stmt(sql: &str, values: Vec<Value>) -> Statement {
    Statement::from_sql_and_values(DbBackend::Postgres, sql, values)
}

fn scope_values(scope: &PlatformTableScope, user: Uuid) -> Vec<Value> {
    vec![
        Value::Int(Some(scope.tenant_id)),
        Value::Int(Some(scope.community_id)),
        Value::Uuid(Some(user)),
    ]
}

/// SQL select expression + JSON kind for one allowlisted `community_members` column.
/// Matching on the canonical `&'static str` keeps identifiers constant.
fn select_expr(column: &'static str) -> Result<(&'static str, bool), PlatformTableError> {
    match column {
        "role" => Ok(("cm.role", false)),
        "is_active" => Ok(("(cm.is_active IS TRUE)", true)),
        "joined_at" => Ok((
            "to_char(cm.joined_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')",
            false,
        )),
        other => Err(PlatformTableError::NotAllowlisted(other.to_string())),
    }
}

/// Builds the scoped SELECT for `columns` (canonical static names). Pure, so the
/// scope predicate is unit-testable without a database.
pub(crate) fn build_read_sql(columns: &[&'static str]) -> Result<String, PlatformTableError> {
    let mut exprs = Vec::with_capacity(columns.len());
    for column in columns {
        let (expr, _) = select_expr(column)?;
        exprs.push(format!("{expr} AS c_{column}"));
    }
    Ok(format!(
        "SELECT {} FROM community_members cm JOIN communities c ON c.id = cm.community_id \
         WHERE {TARGET_PREDICATE}",
        exprs.join(", ")
    ))
}

/// The row-lock SELECT of a write (scoped identically to a read).
pub(crate) fn build_lock_sql() -> String {
    format!(
        "SELECT cm.id AS id, cm.role AS role FROM community_members cm \
         JOIN communities c ON c.id = cm.community_id WHERE {TARGET_PREDICATE} FOR UPDATE OF cm"
    )
}

/// The scoped UPDATE: re-asserts the community on top of the locked row id.
pub(crate) const UPDATE_ROLE_SQL: &str = "UPDATE community_members SET role = $1, \
     updated_at = NOW() WHERE id = $2 AND community_id = $3";

const AUDIT_SQL: &str = "INSERT INTO bundle_platform_table_audit \
     (tenant_id, community_id, app_id, app_version, user_uuid, table_name, column_name, \
      op, old_value, new_value) VALUES ($1, $2, $3, $4, $5, $6, $7, 'write', $8, $9)";

/// The production [`PlatformTableStore`].
pub struct PostgresPlatformTableStore {
    conn: DatabaseConnection,
}

impl PostgresPlatformTableStore {
    pub fn new(conn: DatabaseConnection) -> Self {
        Self { conn }
    }

    async fn read_impl(
        &self,
        scope: &PlatformTableScope,
        table: &str,
        user: Uuid,
        columns: &[String],
    ) -> Result<serde_json::Value, PlatformTableError> {
        let cols = validate_read(table, columns)?;
        let sql = build_read_sql(&cols)?;
        let row = self
            .conn
            .query_one_raw(stmt(&sql, scope_values(scope, user)))
            .await
            .map_err(backend_err)?
            .ok_or(PlatformTableError::NotAMember)?;
        let mut out = serde_json::Map::new();
        for column in cols {
            let (_, is_bool) = select_expr(column)?;
            let alias = format!("c_{column}");
            let value = if is_bool {
                serde_json::Value::Bool(row.try_get::<bool>("", &alias).map_err(backend_err)?)
            } else {
                match row
                    .try_get::<Option<String>>("", &alias)
                    .map_err(backend_err)?
                {
                    Some(s) => serde_json::Value::String(s),
                    None => serde_json::Value::Null,
                }
            };
            out.insert(column.to_string(), value);
        }
        Ok(serde_json::Value::Object(out))
    }

    async fn write_impl(
        &self,
        scope: &PlatformTableScope,
        table: &str,
        user: Uuid,
        changes: &[Change],
    ) -> Result<(), PlatformTableError> {
        let validated = validate_write(table, changes)?;
        let txn = self.conn.begin().await.map_err(backend_err)?;
        txn.execute_raw(stmt(
            &format!("SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"),
            vec![],
        ))
        .await
        .map_err(backend_err)?;

        let row = txn
            .query_one_raw(stmt(&build_lock_sql(), scope_values(scope, user)))
            .await
            .map_err(backend_err)?
            .ok_or(PlatformTableError::NotAMember)?;
        let row_id = row.try_get::<i32>("", "id").map_err(backend_err)?;
        let old_role = row
            .try_get::<Option<String>>("", "role")
            .map_err(backend_err)?;
        if old_role
            .as_deref()
            .is_some_and(|r| PROTECTED_ROLES.contains(&r))
        {
            metrics::record_denied("protected_role");
            return Err(PlatformTableError::NotAMember);
        }

        for (column, value) in &validated {
            // `validate_write` only ever yields `role` today (the sole write
            // column with a validator); a new write column needs its own UPDATE.
            if *column != "role" {
                return Err(PlatformTableError::NotAllowlisted((*column).to_string()));
            }
            txn.execute_raw(stmt(
                UPDATE_ROLE_SQL,
                vec![
                    Value::String(Some(value.clone())),
                    Value::Int(Some(row_id)),
                    Value::Int(Some(scope.community_id)),
                ],
            ))
            .await
            .map_err(backend_err)?;
            txn.execute_raw(stmt(
                AUDIT_SQL,
                vec![
                    Value::Int(Some(scope.tenant_id)),
                    Value::Int(Some(scope.community_id)),
                    Value::String(Some(scope.app_id.clone())),
                    Value::BigInt(Some(scope.app_version)),
                    Value::Uuid(Some(user)),
                    Value::String(Some("community_members".to_string())),
                    Value::String(Some((*column).to_string())),
                    Value::String(old_role.clone()),
                    Value::String(Some(value.clone())),
                ],
            ))
            .await
            .map_err(backend_err)?;
        }
        txn.commit().await.map_err(backend_err)?;
        Ok(())
    }
}

fn outcome_of<T>(r: &Result<T, PlatformTableError>) -> &'static str {
    match r {
        Ok(_) => "ok",
        Err(e) => e.wire_code(),
    }
}

impl PlatformTableStore for PostgresPlatformTableStore {
    fn read<'a>(
        &'a self,
        scope: &'a PlatformTableScope,
        table: &'a str,
        user: Uuid,
        columns: &'a [String],
    ) -> BoxFuture<'a, Result<serde_json::Value, PlatformTableError>> {
        Box::pin(async move {
            let start = Instant::now();
            let r = self.read_impl(scope, table, user, columns).await;
            metrics::record_call("read", outcome_of(&r), start.elapsed().as_secs_f64());
            r
        })
    }

    fn write<'a>(
        &'a self,
        scope: &'a PlatformTableScope,
        table: &'a str,
        user: Uuid,
        changes: &'a [Change],
    ) -> BoxFuture<'a, Result<(), PlatformTableError>> {
        Box::pin(async move {
            let start = Instant::now();
            let r = self.write_impl(scope, table, user, changes).await;
            metrics::record_call("write", outcome_of(&r), start.elapsed().as_secs_f64());
            r
        })
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn every_statement_is_scoped_by_community_and_tenant() {
        let read = build_read_sql(&["role", "is_active", "joined_at"]).unwrap();
        let lock = build_lock_sql();
        for sql in [read.as_str(), lock.as_str()] {
            assert!(sql.contains("cm.community_id = $2"), "{sql}");
            assert!(sql.contains("c.tenant_id = $1"), "{sql}");
            assert!(sql.contains("cm.user_uuid = $3"), "{sql}");
        }
        assert!(UPDATE_ROLE_SQL.contains("community_id = $3"));
        assert!(lock.contains("FOR UPDATE OF cm"));
    }

    #[test]
    fn unknown_column_never_reaches_sql() {
        assert!(build_read_sql(&["display_name"]).is_err());
        assert!(build_read_sql(&["role; DROP TABLE community_members"]).is_err());
    }
}
