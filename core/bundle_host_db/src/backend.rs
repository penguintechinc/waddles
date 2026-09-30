//! The real `db` backend: one bundle's single table in the shared
//! `waddles` Postgres instance, under the least-privilege
//! `waddles_bundle_runtime` role (design doc SS2/SS7), reached via SeaORM.
//!
//! **Two independent, non-substitutable enforcement layers on every
//! statement (design doc SS6.2), both present here:**
//! 1. RLS via `SET LOCAL` (`waddles.tenant_id`/`waddles.community_id`),
//!    applied by the Postgres role/policy this connection runs under
//!    (provisioned by hub-api's migration, not by this crate).
//! 2. An **explicit `tenant_id = $n AND community_id = $m` predicate**,
//!    bound from the same [`crate::scope::DbScope`] independently of the
//!    `SET LOCAL` call, added by every method below -- so a bug or leak in
//!    one mechanism (e.g. a stale GUC surviving a pooled-connection
//!    checkin) does not silently fall through to the other.
//!
//! Every identifier (schema/table/column) reaching a SQL string here comes
//! only from a [`crate::schema::TableSchema`] that has already passed
//! [`crate::scope::validate_identifier`] -- this module re-validates them
//! anyway immediately before use (defense in depth, not the primary
//! control). Every value (row-id, column value, tenant/community) is bound
//! as a SeaORM [`Value`], never interpolated into SQL text.
//!
//! **Known simplifications in this landing (see PR description "remaining
//! work"):** `tenant_id`/`community_id` platform columns are bound as
//! `text` here; this must be reconciled against whatever concrete type
//! `hub_api/services/bundle_data_ddl.py` (PR #430) actually emits for them
//! once that generator is merged and its DDL is inspectable. `query`
//! (list/filter) is not implemented in this landing -- only
//! `insert`/`get`/`update`/`delete`, per the agreed first-slice scope.
//! Row/byte quota enforcement here is a pre-write `COUNT(*)` under the same
//! transaction, not yet the trigger-based counter the full design (SS9)
//! calls for at scale -- correct today, not the final mechanism.

use std::future::Future;
use std::pin::Pin;

use sea_orm::{
    ConnectionTrait, DatabaseConnection, DbBackend as SeaDbBackend, Statement, TransactionTrait,
    Value,
};
use uuid::Uuid;

use crate::limits::{
    MAX_JSONB_BYTES, MAX_QUERY_LIMIT, MAX_ROWS_PER_APP, MAX_TEXT_BYTES, STATEMENT_TIMEOUT_MS,
};
use crate::schema::{ColumnType, TableSchema, PLATFORM_COLUMNS};
use crate::scope::{quote_ident, validate_identifier, DbScope};

pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// A `db` capability value -- the host-side mirror of the WIT `db.value`
/// variant, independent of whatever wire shape eventually carries it
/// across the host-API boundary.
#[derive(Debug, Clone, PartialEq)]
pub enum DbValue {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Text(String),
    Bytes(Vec<u8>),
}

/// One row, as returned to the guest -- `columns` echoes back exactly the
/// caller-supplied (or previously-stored, for `get`) declared-column
/// values, never a platform column.
#[derive(Debug, Clone, PartialEq)]
pub struct Row {
    pub row_id: String,
    pub version: u64,
    pub columns: Vec<(String, DbValue)>,
}

/// The `db` capability's error surface.
#[derive(Debug, Clone, PartialEq, thiserror::Error)]
pub enum DbError {
    #[error("no table provisioned for this app")]
    NoTable,
    #[error("invalid column: {0}")]
    InvalidColumn(String),
    #[error("invalid value: {0}")]
    InvalidValue(String),
    #[error("row not found")]
    NotFound,
    #[error("version conflict")]
    Conflict,
    #[error("quota exceeded: {0}")]
    QuotaExceeded(String),
    #[error("operation timed out")]
    Timeout,
    #[error("backend error: {0}")]
    Backend(String),
}

impl DbError {
    /// A stable, fine-grained reason string for logs/metrics.
    pub fn code(&self) -> &'static str {
        match self {
            DbError::NoTable => "no_table",
            DbError::InvalidColumn(_) => "invalid_column",
            DbError::InvalidValue(_) => "invalid_value",
            DbError::NotFound => "not_found",
            DbError::Conflict => "conflict",
            DbError::QuotaExceeded(_) => "quota_exceeded",
            DbError::Timeout => "timeout",
            DbError::Backend(_) => "backend",
        }
    }
}

fn db_value_to_sea_value(v: &DbValue) -> Value {
    match v {
        DbValue::Null => Value::String(None),
        DbValue::Bool(b) => Value::Bool(Some(*b)),
        DbValue::Int(i) => Value::BigInt(Some(*i)),
        DbValue::Float(f) => Value::Double(Some(*f)),
        DbValue::Text(s) => Value::String(Some(s.clone())),
        DbValue::Bytes(b) => Value::Bytes(Some(b.clone())),
    }
}

/// Validates one guest-supplied `(column, value)` pair against `schema`:
/// the column must be declared and not a platform column; a `user_ref`
/// column's value must be `Null` (if nullable) or a syntactically valid
/// UUID string (design doc SS3.2/SS4: "`user_ref` carries the platform
/// user's UUID only"); a `text`/`jsonb` value must respect its size cap
/// (design doc SS3.1).
fn validate_column_value(schema: &TableSchema, name: &str, value: &DbValue) -> Result<(), DbError> {
    if PLATFORM_COLUMNS.contains(&name) {
        return Err(DbError::InvalidColumn(format!(
            "{name:?} is a platform-owned column and cannot be set by a bundle"
        )));
    }
    validate_identifier(name)
        .map_err(|_| DbError::InvalidColumn(format!("{name:?} is not a valid identifier")))?;
    let col = schema
        .column(name)
        .ok_or_else(|| DbError::InvalidColumn(format!("{name:?} is not declared for this app")))?;

    if matches!(value, DbValue::Null) {
        if col.nullable {
            return Ok(());
        }
        return Err(DbError::InvalidValue(format!("{name:?} is not nullable")));
    }

    if col.is_user_ref {
        let DbValue::Text(s) = value else {
            return Err(DbError::InvalidValue(format!(
                "{name:?} is a user_ref column and must be a UUID string"
            )));
        };
        Uuid::parse_str(s)
            .map_err(|_| DbError::InvalidValue(format!("{name:?} is not a valid UUID")))?;
        return Ok(());
    }

    match (col.sql_type, value) {
        (ColumnType::Uuid, DbValue::Text(s)) => {
            Uuid::parse_str(s)
                .map_err(|_| DbError::InvalidValue(format!("{name:?} is not a valid UUID")))?;
        }
        (ColumnType::Int4, DbValue::Int(i)) => {
            if i32::try_from(*i).is_err() {
                return Err(DbError::InvalidValue(format!(
                    "{name:?} exceeds int4 range"
                )));
            }
        }
        (ColumnType::Int8, DbValue::Int(_)) => {}
        (ColumnType::Bool, DbValue::Bool(_)) => {}
        (ColumnType::Timestamptz, DbValue::Text(_)) => {}
        (ColumnType::Text, DbValue::Text(s)) => {
            if s.len() > MAX_TEXT_BYTES {
                return Err(DbError::QuotaExceeded(format!(
                    "{name:?} exceeds max text size ({MAX_TEXT_BYTES} bytes)"
                )));
            }
        }
        (ColumnType::Jsonb, DbValue::Text(s)) => {
            if s.len() > MAX_JSONB_BYTES {
                return Err(DbError::QuotaExceeded(format!(
                    "{name:?} exceeds max jsonb size ({MAX_JSONB_BYTES} bytes)"
                )));
            }
            serde_json::from_str::<serde_json::Value>(s)
                .map_err(|_| DbError::InvalidValue(format!("{name:?} is not valid JSON")))?;
        }
        _ => {
            return Err(DbError::InvalidValue(format!(
                "{name:?} value does not match its declared column type"
            )))
        }
    }
    Ok(())
}

/// Validates every entry in a guest-supplied `column-values` map, in one
/// pass, so a caller gets a single well-formed rejection rather than
/// partially validating and partially writing.
fn validate_column_values(
    schema: &TableSchema,
    column_values: &[(String, DbValue)],
) -> Result<(), DbError> {
    for (name, value) in column_values {
        validate_column_value(schema, name, value)?;
    }
    Ok(())
}

/// Sets the two independent tenant-scope mechanisms for the current
/// transaction (design doc SS6.2/SS7): the `SET LOCAL`-backed RLS GUCs
/// (via `set_config(..., true)`, parameterized -- never interpolated, the
/// exact pattern `alembic/versions/0030_bundle_app_schemas.py` already
/// uses for a role password) and the statement timeout. The explicit
/// `tenant_id = $n` predicate itself is added separately, per statement,
/// by each op below -- this function only establishes the RLS half.
async fn set_local_scope(txn: &impl ConnectionTrait, scope: &DbScope) -> Result<(), DbError> {
    txn.execute_raw(Statement::from_sql_and_values(
        SeaDbBackend::Postgres,
        format!("SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"),
        [],
    ))
    .await
    .map_err(|e| DbError::Backend(e.to_string()))?;

    txn.execute_raw(Statement::from_sql_and_values(
        SeaDbBackend::Postgres,
        "SELECT set_config('waddles.tenant_id', $1, true), \
                set_config('waddles.community_id', $2, true), \
                set_config('waddles.app_id', $3, true)"
            .to_string(),
        [
            Value::String(Some(scope.tenant.clone())),
            Value::String(scope.community.clone()),
            Value::String(Some(scope.app_id.clone())),
        ],
    ))
    .await
    .map_err(|e| DbError::Backend(e.to_string()))?;
    Ok(())
}

/// Serializes the quota check-then-insert critical section for one
/// `(tenant, community, app_id)` scope without any new schema/counter
/// table (this crate is DML-only -- see module doc). A
/// `pg_advisory_xact_lock` keyed by a hash of the scope is a purely
/// application-level (DML-reachable) primitive: two concurrent
/// transactions racing the same scope's `COUNT(*)` + `INSERT` serialize on
/// this lock, so the second transaction's `COUNT(*)` always observes the
/// first's committed-or-not-yet-visible row only after the first has
/// released the lock (commit or rollback) -- closing the classic
/// check-then-act TOCTOU window a bare `COUNT(*)` guard has under
/// READ COMMITTED. Different scopes hash to (almost certainly) different
/// lock keys and never contend with each other. The lock is
/// transaction-scoped (`_xact_`) -- released automatically at COMMIT/
/// ROLLBACK, never leaked across a pooled connection's next checkout.
async fn lock_quota_scope(txn: &impl ConnectionTrait, scope: &DbScope) -> Result<(), DbError> {
    let key = format!(
        "{}:{}:{}",
        scope.tenant,
        scope.community.as_deref().unwrap_or(""),
        scope.app_id
    );
    txn.execute_raw(Statement::from_sql_and_values(
        SeaDbBackend::Postgres,
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))".to_string(),
        [Value::String(Some(key))],
    ))
    .await
    .map_err(|e| DbError::Backend(e.to_string()))?;
    Ok(())
}

/// The explicit, independent `tenant_id`/`community_id` predicate every
/// generated statement below carries in addition to RLS (design doc
/// SS6.2's "defense in depth" requirement) -- returns the SQL fragment and
/// the two bound values, in bind order.
fn tenant_predicate(scope: &DbScope, next_param: usize) -> (String, Vec<Value>) {
    match &scope.community {
        Some(community) => (
            format!(
                "tenant_id = ${} AND community_id = ${}",
                next_param,
                next_param + 1
            ),
            vec![
                Value::String(Some(scope.tenant.clone())),
                Value::String(Some(community.clone())),
            ],
        ),
        None => (
            format!("tenant_id = ${} AND community_id IS NULL", next_param),
            vec![Value::String(Some(scope.tenant.clone()))],
        ),
    }
}

/// Object-safe `db` backend trait -- mirrors `bundle_host_kv::backend::KvBackend`'s
/// shape so both capability crates follow the same call-site pattern in
/// `core/svc_process`/`core/svc_action`.
pub trait DbBackend: Send + Sync {
    fn insert<'a>(
        &'a self,
        schema: &'a TableSchema,
        scope: &'a DbScope,
        column_values: Vec<(String, DbValue)>,
    ) -> BoxFuture<'a, Result<Row, DbError>>;

    fn get<'a>(
        &'a self,
        schema: &'a TableSchema,
        scope: &'a DbScope,
        row_id: &'a str,
    ) -> BoxFuture<'a, Result<Row, DbError>>;

    fn update<'a>(
        &'a self,
        schema: &'a TableSchema,
        scope: &'a DbScope,
        row_id: &'a str,
        expected_version: u64,
        column_values: Vec<(String, DbValue)>,
    ) -> BoxFuture<'a, Result<Row, DbError>>;

    fn delete<'a>(
        &'a self,
        schema: &'a TableSchema,
        scope: &'a DbScope,
        row_id: &'a str,
        expected_version: u64,
    ) -> BoxFuture<'a, Result<(), DbError>>;

    /// Bounded list of a scope's rows, ordered by `row_id` for a stable
    /// keyset-style page boundary. Host-enforced page size (never
    /// guest-controlled beyond the [`MAX_QUERY_LIMIT`] ceiling) -- see
    /// module doc "known simplifications": this is the host-side op ready
    /// for the proposed `query` WIT shape (PR description), not yet wired
    /// to a guest-reachable op in this landing's `stage.wit`.
    fn query<'a>(
        &'a self,
        schema: &'a TableSchema,
        scope: &'a DbScope,
        limit: u32,
        offset: u32,
    ) -> BoxFuture<'a, Result<Vec<Row>, DbError>>;
}

/// The real [`DbBackend`]: SeaORM over the shared `waddles` Postgres
/// instance, connected as `waddles_bundle_runtime` (grants provisioned by
/// `alembic/versions/0030_bundle_app_schemas.py`).
pub struct PostgresBackend {
    conn: DatabaseConnection,
    /// Per-app row cap applied by [`Self::insert_impl`]'s quota check.
    /// Defaults to [`MAX_ROWS_PER_APP`]; overridable only under
    /// `#[cfg(any(test, feature = "test-util"))]` so an integration test
    /// can prove the quota boundary (and the race-safety of
    /// [`lock_quota_scope`] around it) without actually inserting
    /// [`MAX_ROWS_PER_APP`] rows.
    row_cap: i64,
}

impl PostgresBackend {
    pub fn new(conn: DatabaseConnection) -> Self {
        Self {
            conn,
            row_cap: MAX_ROWS_PER_APP,
        }
    }

    /// Test-only: overrides the per-app row cap so a quota/concurrency test
    /// can exercise the boundary with a handful of rows instead of
    /// [`MAX_ROWS_PER_APP`].
    #[cfg(any(test, feature = "test-util"))]
    pub fn with_row_cap_for_test(mut self, cap: i64) -> Self {
        self.row_cap = cap;
        self
    }

    async fn insert_impl(
        &self,
        schema: &TableSchema,
        scope: &DbScope,
        column_values: Vec<(String, DbValue)>,
    ) -> Result<Row, DbError> {
        validate_column_values(schema, &column_values)?;

        let txn = self
            .conn
            .begin()
            .await
            .map_err(|e| DbError::Backend(e.to_string()))?;
        set_local_scope(&txn, scope).await?;

        // Race-safe quota: serialize this scope's check-then-insert
        // against every other concurrent transaction touching the same
        // (tenant, community, app_id) before reading the count (see
        // `lock_quota_scope`'s doc) -- a bare `COUNT(*)` guard alone is a
        // TOCTOU race under concurrency.
        lock_quota_scope(&txn, scope).await?;

        let (pred_sql, pred_values) = tenant_predicate(scope, 1);
        let count_sql = format!(
            "SELECT COUNT(*) AS n FROM {} WHERE {}",
            schema.qualified_name(),
            pred_sql
        );
        let count_row = txn
            .query_one_raw(Statement::from_sql_and_values(
                SeaDbBackend::Postgres,
                count_sql,
                pred_values.clone(),
            ))
            .await
            .map_err(|e| DbError::Backend(e.to_string()))?
            .ok_or_else(|| DbError::Backend("COUNT(*) returned no row".to_string()))?;
        let row_count: i64 = count_row
            .try_get("", "n")
            .map_err(|e| DbError::Backend(e.to_string()))?;
        if row_count >= self.row_cap {
            return Err(DbError::QuotaExceeded(format!(
                "row count would exceed the per-app cap ({})",
                self.row_cap
            )));
        }

        let mut col_names: Vec<String> = column_values.iter().map(|(n, _)| n.clone()).collect();
        col_names.push("tenant_id".to_string());
        col_names.push("community_id".to_string());
        let quoted_cols: Vec<String> = col_names.iter().map(|c| quote_ident(c)).collect();

        let mut bind_values: Vec<Value> = column_values
            .iter()
            .map(|(_, v)| db_value_to_sea_value(v))
            .collect();
        bind_values.push(Value::String(Some(scope.tenant.clone())));
        bind_values.push(Value::String(scope.community.clone()));

        let placeholders: Vec<String> = (1..=bind_values.len()).map(|i| format!("${i}")).collect();

        let sql = format!(
            "INSERT INTO {} ({}) VALUES ({}) RETURNING row_id, version",
            schema.qualified_name(),
            quoted_cols.join(", "),
            placeholders.join(", ")
        );

        let result_row = txn
            .query_one_raw(Statement::from_sql_and_values(
                SeaDbBackend::Postgres,
                sql,
                bind_values,
            ))
            .await
            .map_err(|e| DbError::Backend(e.to_string()))?
            .ok_or_else(|| DbError::Backend("INSERT ... RETURNING produced no row".to_string()))?;

        let row_id: Uuid = result_row
            .try_get("", "row_id")
            .map_err(|e| DbError::Backend(e.to_string()))?;
        let version: i64 = result_row
            .try_get("", "version")
            .map_err(|e| DbError::Backend(e.to_string()))?;

        txn.commit()
            .await
            .map_err(|e| DbError::Backend(e.to_string()))?;

        Ok(Row {
            row_id: row_id.to_string(),
            version: version as u64,
            columns: column_values,
        })
    }

    async fn get_impl(
        &self,
        schema: &TableSchema,
        scope: &DbScope,
        row_id: &str,
    ) -> Result<Row, DbError> {
        let row_uuid = Uuid::parse_str(row_id)
            .map_err(|_| DbError::InvalidValue("row_id is not a valid UUID".to_string()))?;

        let txn = self
            .conn
            .begin()
            .await
            .map_err(|e| DbError::Backend(e.to_string()))?;
        set_local_scope(&txn, scope).await?;

        let declared: Vec<String> = schema.columns.iter().map(|c| c.name.clone()).collect();
        let mut select_cols = vec!["row_id".to_string(), "version".to_string()];
        select_cols.extend(declared.iter().cloned());
        let quoted_select: Vec<String> = select_cols.iter().map(|c| quote_ident(c)).collect();

        let (pred_sql, mut pred_values) = tenant_predicate(scope, 2);
        pred_values.insert(0, Value::Uuid(Some(row_uuid)));

        let sql = format!(
            "SELECT {} FROM {} WHERE row_id = $1 AND {}",
            quoted_select.join(", "),
            schema.qualified_name(),
            pred_sql
        );

        let result_row = txn
            .query_one_raw(Statement::from_sql_and_values(
                SeaDbBackend::Postgres,
                sql,
                pred_values,
            ))
            .await
            .map_err(|e| DbError::Backend(e.to_string()))?
            .ok_or(DbError::NotFound)?;

        txn.commit()
            .await
            .map_err(|e| DbError::Backend(e.to_string()))?;

        let version: i64 = result_row
            .try_get("", "version")
            .map_err(|e| DbError::Backend(e.to_string()))?;

        let mut columns = Vec::with_capacity(declared.len());
        for col in &schema.columns {
            let value = extract_value(&result_row, col)?;
            columns.push((col.name.clone(), value));
        }

        Ok(Row {
            row_id: row_id.to_string(),
            version: version as u64,
            columns,
        })
    }

    async fn update_impl(
        &self,
        schema: &TableSchema,
        scope: &DbScope,
        row_id: &str,
        expected_version: u64,
        column_values: Vec<(String, DbValue)>,
    ) -> Result<Row, DbError> {
        validate_column_values(schema, &column_values)?;
        let row_uuid = Uuid::parse_str(row_id)
            .map_err(|_| DbError::InvalidValue("row_id is not a valid UUID".to_string()))?;
        let expected_version_i64 = i64::try_from(expected_version)
            .map_err(|_| DbError::InvalidValue("expected_version out of range".to_string()))?;

        let txn = self
            .conn
            .begin()
            .await
            .map_err(|e| DbError::Backend(e.to_string()))?;
        set_local_scope(&txn, scope).await?;

        let set_clauses: Vec<String> = column_values
            .iter()
            .enumerate()
            .map(|(i, (name, _))| format!("{} = ${}", quote_ident(name), i + 1))
            .collect();
        let mut bind_values: Vec<Value> = column_values
            .iter()
            .map(|(_, v)| db_value_to_sea_value(v))
            .collect();

        let row_id_param = bind_values.len() + 1;
        let version_param = row_id_param + 1;
        bind_values.push(Value::Uuid(Some(row_uuid)));
        bind_values.push(Value::BigInt(Some(expected_version_i64)));

        let (pred_sql, pred_values) = tenant_predicate(scope, version_param + 1);
        bind_values.extend(pred_values);

        let sql = format!(
            "UPDATE {} SET {}, version = version + 1, updated_at = now() \
             WHERE row_id = ${row_id_param} AND version = ${version_param} AND {pred_sql} \
             RETURNING version",
            schema.qualified_name(),
            set_clauses.join(", "),
        );

        let updated = txn
            .query_one_raw(Statement::from_sql_and_values(
                SeaDbBackend::Postgres,
                sql,
                bind_values,
            ))
            .await
            .map_err(|e| DbError::Backend(e.to_string()))?;

        let Some(updated_row) = updated else {
            // 0 rows affected: distinguish not-found from a version
            // conflict with a follow-up existence check, still inside the
            // same transaction/RLS scope.
            let (exists_pred_sql, mut exists_values) = tenant_predicate(scope, 2);
            exists_values.insert(0, Value::Uuid(Some(row_uuid)));
            let exists_sql = format!(
                "SELECT 1 AS present FROM {} WHERE row_id = $1 AND {}",
                schema.qualified_name(),
                exists_pred_sql
            );
            let exists = txn
                .query_one_raw(Statement::from_sql_and_values(
                    SeaDbBackend::Postgres,
                    exists_sql,
                    exists_values,
                ))
                .await
                .map_err(|e| DbError::Backend(e.to_string()))?;
            txn.commit()
                .await
                .map_err(|e| DbError::Backend(e.to_string()))?;
            return Err(if exists.is_some() {
                DbError::Conflict
            } else {
                DbError::NotFound
            });
        };

        let version: i64 = updated_row
            .try_get("", "version")
            .map_err(|e| DbError::Backend(e.to_string()))?;
        txn.commit()
            .await
            .map_err(|e| DbError::Backend(e.to_string()))?;

        Ok(Row {
            row_id: row_id.to_string(),
            version: version as u64,
            columns: column_values,
        })
    }

    async fn delete_impl(
        &self,
        schema: &TableSchema,
        scope: &DbScope,
        row_id: &str,
        expected_version: u64,
    ) -> Result<(), DbError> {
        let row_uuid = Uuid::parse_str(row_id)
            .map_err(|_| DbError::InvalidValue("row_id is not a valid UUID".to_string()))?;
        let expected_version_i64 = i64::try_from(expected_version)
            .map_err(|_| DbError::InvalidValue("expected_version out of range".to_string()))?;

        let txn = self
            .conn
            .begin()
            .await
            .map_err(|e| DbError::Backend(e.to_string()))?;
        set_local_scope(&txn, scope).await?;

        let (pred_sql, mut pred_values) = tenant_predicate(scope, 3);
        let mut bind_values = vec![
            Value::Uuid(Some(row_uuid)),
            Value::BigInt(Some(expected_version_i64)),
        ];
        bind_values.append(&mut pred_values);

        let sql = format!(
            "DELETE FROM {} WHERE row_id = $1 AND version = $2 AND {pred_sql}",
            schema.qualified_name(),
        );

        let result = txn
            .execute_raw(Statement::from_sql_and_values(
                SeaDbBackend::Postgres,
                sql,
                bind_values,
            ))
            .await
            .map_err(|e| DbError::Backend(e.to_string()))?;

        if result.rows_affected() == 0 {
            let (exists_pred_sql, mut exists_values) = tenant_predicate(scope, 2);
            exists_values.insert(0, Value::Uuid(Some(row_uuid)));
            let exists_sql = format!(
                "SELECT 1 AS present FROM {} WHERE row_id = $1 AND {}",
                schema.qualified_name(),
                exists_pred_sql
            );
            let exists = txn
                .query_one_raw(Statement::from_sql_and_values(
                    SeaDbBackend::Postgres,
                    exists_sql,
                    exists_values,
                ))
                .await
                .map_err(|e| DbError::Backend(e.to_string()))?;
            txn.commit()
                .await
                .map_err(|e| DbError::Backend(e.to_string()))?;
            return Err(if exists.is_some() {
                DbError::Conflict
            } else {
                DbError::NotFound
            });
        }

        txn.commit()
            .await
            .map_err(|e| DbError::Backend(e.to_string()))?;
        Ok(())
    }

    /// Bounded list, ordered by `row_id`, both scoping mechanisms applied
    /// exactly like every other op (RLS `SET LOCAL` + explicit predicate).
    /// `limit` is clamped to [`MAX_QUERY_LIMIT`] host-side -- never trusts a
    /// guest-requested page size past the ceiling.
    async fn query_impl(
        &self,
        schema: &TableSchema,
        scope: &DbScope,
        limit: u32,
        offset: u32,
    ) -> Result<Vec<Row>, DbError> {
        let bounded_limit = limit.min(MAX_QUERY_LIMIT);

        let txn = self
            .conn
            .begin()
            .await
            .map_err(|e| DbError::Backend(e.to_string()))?;
        set_local_scope(&txn, scope).await?;

        let declared: Vec<String> = schema.columns.iter().map(|c| c.name.clone()).collect();
        let mut select_cols = vec!["row_id".to_string(), "version".to_string()];
        select_cols.extend(declared.iter().cloned());
        let quoted_select: Vec<String> = select_cols.iter().map(|c| quote_ident(c)).collect();

        let (pred_sql, pred_values) = tenant_predicate(scope, 1);
        let limit_param = pred_values.len() + 1;
        let offset_param = limit_param + 1;
        let mut bind_values = pred_values;
        bind_values.push(Value::BigInt(Some(i64::from(bounded_limit))));
        bind_values.push(Value::BigInt(Some(i64::from(offset))));

        let sql = format!(
            "SELECT {} FROM {} WHERE {} ORDER BY row_id LIMIT ${limit_param} OFFSET ${offset_param}",
            quoted_select.join(", "),
            schema.qualified_name(),
            pred_sql,
        );

        let rows = txn
            .query_all_raw(Statement::from_sql_and_values(
                SeaDbBackend::Postgres,
                sql,
                bind_values,
            ))
            .await
            .map_err(|e| DbError::Backend(e.to_string()))?;

        txn.commit()
            .await
            .map_err(|e| DbError::Backend(e.to_string()))?;

        let mut out = Vec::with_capacity(rows.len());
        for result_row in &rows {
            let row_uuid: Uuid = result_row
                .try_get("", "row_id")
                .map_err(|e| DbError::Backend(e.to_string()))?;
            let version: i64 = result_row
                .try_get("", "version")
                .map_err(|e| DbError::Backend(e.to_string()))?;
            let mut columns = Vec::with_capacity(declared.len());
            for col in &schema.columns {
                columns.push((col.name.clone(), extract_value(result_row, col)?));
            }
            out.push(Row {
                row_id: row_uuid.to_string(),
                version: version as u64,
                columns,
            });
        }
        Ok(out)
    }
}

fn extract_value(
    row: &sea_orm::QueryResult,
    col: &crate::schema::ColumnDef,
) -> Result<DbValue, DbError> {
    macro_rules! try_nullable {
        ($ty:ty, $wrap:expr) => {{
            let v: Option<$ty> = row
                .try_get("", &col.name)
                .map_err(|e| DbError::Backend(e.to_string()))?;
            Ok(match v {
                Some(v) => $wrap(v),
                None => DbValue::Null,
            })
        }};
    }
    match col.sql_type {
        ColumnType::Bool => try_nullable!(bool, DbValue::Bool),
        ColumnType::Int4 => try_nullable!(i32, |v: i32| DbValue::Int(v as i64)),
        ColumnType::Int8 => try_nullable!(i64, DbValue::Int),
        ColumnType::Text | ColumnType::Jsonb | ColumnType::Timestamptz => {
            try_nullable!(String, DbValue::Text)
        }
        ColumnType::Uuid => {
            let v: Option<Uuid> = row
                .try_get("", &col.name)
                .map_err(|e| DbError::Backend(e.to_string()))?;
            Ok(match v {
                Some(v) => DbValue::Text(v.to_string()),
                None => DbValue::Null,
            })
        }
    }
}

impl DbBackend for PostgresBackend {
    fn insert<'a>(
        &'a self,
        schema: &'a TableSchema,
        scope: &'a DbScope,
        column_values: Vec<(String, DbValue)>,
    ) -> BoxFuture<'a, Result<Row, DbError>> {
        Box::pin(self.insert_impl(schema, scope, column_values))
    }

    fn get<'a>(
        &'a self,
        schema: &'a TableSchema,
        scope: &'a DbScope,
        row_id: &'a str,
    ) -> BoxFuture<'a, Result<Row, DbError>> {
        Box::pin(self.get_impl(schema, scope, row_id))
    }

    fn update<'a>(
        &'a self,
        schema: &'a TableSchema,
        scope: &'a DbScope,
        row_id: &'a str,
        expected_version: u64,
        column_values: Vec<(String, DbValue)>,
    ) -> BoxFuture<'a, Result<Row, DbError>> {
        Box::pin(self.update_impl(schema, scope, row_id, expected_version, column_values))
    }

    fn delete<'a>(
        &'a self,
        schema: &'a TableSchema,
        scope: &'a DbScope,
        row_id: &'a str,
        expected_version: u64,
    ) -> BoxFuture<'a, Result<(), DbError>> {
        Box::pin(self.delete_impl(schema, scope, row_id, expected_version))
    }

    fn query<'a>(
        &'a self,
        schema: &'a TableSchema,
        scope: &'a DbScope,
        limit: u32,
        offset: u32,
    ) -> BoxFuture<'a, Result<Vec<Row>, DbError>> {
        Box::pin(self.query_impl(schema, scope, limit, offset))
    }
}

/// Runs `fut` under [`crate::limits::CALL_DEADLINE`] -- the per-bundle-call
/// deadline wrapping connection acquisition and the whole transaction
/// (design doc: "a connection pool with a per-bundle-call deadline").
pub async fn with_call_deadline<T>(
    fut: impl Future<Output = Result<T, DbError>>,
) -> Result<T, DbError> {
    match tokio::time::timeout(crate::limits::CALL_DEADLINE, fut).await {
        Ok(result) => result,
        Err(_) => Err(DbError::Timeout),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::ColumnDef;
    use crate::scope::AppSchema;

    fn schema_with_user_ref() -> TableSchema {
        TableSchema::validated(
            AppSchema::Core,
            "fishing_core",
            vec![
                ColumnDef {
                    name: "user_ref".to_string(),
                    sql_type: ColumnType::Uuid,
                    nullable: true,
                    is_user_ref: true,
                },
                ColumnDef {
                    name: "score".to_string(),
                    sql_type: ColumnType::Int8,
                    nullable: true,
                    is_user_ref: false,
                },
                ColumnDef {
                    name: "note".to_string(),
                    sql_type: ColumnType::Text,
                    nullable: true,
                    is_user_ref: false,
                },
            ],
        )
        .unwrap()
    }

    #[test]
    fn validate_column_value_rejects_platform_columns() {
        let schema = schema_with_user_ref();
        let err =
            validate_column_value(&schema, "tenant_id", &DbValue::Text("x".into())).unwrap_err();
        assert_eq!(err.code(), "invalid_column");
    }

    #[test]
    fn validate_column_value_rejects_undeclared_columns() {
        let schema = schema_with_user_ref();
        let err =
            validate_column_value(&schema, "not_declared", &DbValue::Text("x".into())).unwrap_err();
        assert_eq!(err.code(), "invalid_column");
    }

    #[test]
    fn validate_column_value_rejects_sql_injection_shaped_column_name() {
        let schema = schema_with_user_ref();
        let err = validate_column_value(
            &schema,
            "score; DROP TABLE fishing_core; --",
            &DbValue::Int(1),
        )
        .unwrap_err();
        assert_eq!(err.code(), "invalid_column");
    }

    #[test]
    fn validate_column_value_enforces_user_ref_is_a_uuid() {
        let schema = schema_with_user_ref();
        assert!(
            validate_column_value(&schema, "user_ref", &DbValue::Text("not-a-uuid".into()))
                .is_err()
        );
        assert!(validate_column_value(
            &schema,
            "user_ref",
            &DbValue::Text(Uuid::new_v4().to_string())
        )
        .is_ok());
        assert!(validate_column_value(&schema, "user_ref", &DbValue::Null).is_ok());
    }

    #[test]
    fn validate_column_value_enforces_user_ref_type_is_text() {
        let schema = schema_with_user_ref();
        let err = validate_column_value(&schema, "user_ref", &DbValue::Int(5)).unwrap_err();
        assert_eq!(err.code(), "invalid_value");
    }

    #[test]
    fn validate_column_value_enforces_text_size_cap() {
        let schema = schema_with_user_ref();
        let too_long = "a".repeat(MAX_TEXT_BYTES + 1);
        let err = validate_column_value(&schema, "note", &DbValue::Text(too_long)).unwrap_err();
        assert_eq!(err.code(), "quota_exceeded");
    }

    #[test]
    fn validate_column_value_rejects_not_nullable_null() {
        let schema = TableSchema::validated(
            AppSchema::Core,
            "t",
            vec![ColumnDef {
                name: "required".to_string(),
                sql_type: ColumnType::Text,
                nullable: false,
                is_user_ref: false,
            }],
        )
        .unwrap();
        let err = validate_column_value(&schema, "required", &DbValue::Null).unwrap_err();
        assert_eq!(err.code(), "invalid_value");
    }

    #[test]
    fn tenant_predicate_binds_community_when_present() {
        let scope = DbScope::new("acme", Some("main".to_string()), "app");
        let (sql, values) = tenant_predicate(&scope, 1);
        assert_eq!(sql, "tenant_id = $1 AND community_id = $2");
        assert_eq!(values.len(), 2);
    }

    #[test]
    fn tenant_predicate_uses_is_null_when_community_absent() {
        let scope = DbScope::new("acme", None, "app");
        let (sql, values) = tenant_predicate(&scope, 1);
        assert_eq!(sql, "tenant_id = $1 AND community_id IS NULL");
        assert_eq!(values.len(), 1);
    }

    #[test]
    fn tenant_predicate_never_lets_guest_input_influence_the_predicate_shape() {
        // scope.tenant/community come only from the authenticated
        // invocation (crate::scope::DbScope's own doc) -- this test just
        // pins the SQL shape so a future edit can't accidentally splice
        // scope values into the SQL string instead of the bind-value list.
        let scope = DbScope::new(
            "acme'; DROP TABLE x; --".to_string(),
            None,
            "app".to_string(),
        );
        let (sql, _values) = tenant_predicate(&scope, 1);
        assert!(
            !sql.contains("DROP"),
            "tenant value must never appear in SQL text"
        );
    }
}
