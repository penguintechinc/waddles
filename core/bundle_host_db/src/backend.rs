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
//! **Typed columns** (`uuid` incl. `user_ref`, `timestamptz`, `jsonb`, and
//! NULL in any type): writes bind by the *declared* column type and emit an
//! explicit `$n::uuid`/`$n::timestamptz`/`$n::jsonb` cast, and reads project
//! `timestamptz`/`jsonb` as text in SQL -- see `crate::typed` for why a
//! plain `text` bind fails against real Postgres.
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
use crate::typed;

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

/// Row ordering for `query` (coordinator follow-up: the structured client
/// needs `ORDER BY`/sort, not just `LIMIT`/`offset`, so bundles with a
/// genuine sort/"pick one at random" need -- a quote/8-ball-style bundle,
/// "most recent N" lists -- never have to drop to a raw-SQL escape hatch).
/// `Column` validates `name` against the schema's declared columns (or the
/// fixed platform columns `row_id`/`version`/`created_at`/`updated_at`) --
/// never an arbitrary guest-supplied SQL fragment, same defense-in-depth
/// requirement every other identifier in this crate has
/// ([`crate::scope::validate_identifier`]). `Random` is `ORDER BY
/// random()`, the "sample" helper for a uniformly-random single row
/// (typically paired with `limit: 1`). `query`'s own `order_by:
/// Option<OrderBy>` defaults to the pre-existing stable `row_id ASC`
/// keyset order when `None` -- fully backward compatible.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OrderBy {
    Column { name: String, descending: bool },
    Random,
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

/// Rejects every write op (`insert`/`update`/`delete`) against a table that
/// opted into the cross-community read exception (`crate::schema::
/// TableSchema::cross_community_read`) -- the exception is read-only by
/// design (design note: reputation/user-details are read, never written,
/// through this capability), so a write attempt here is always a
/// configuration error (a schema that should never have been marked
/// cross-community-read for a bundle-writable table), not a race or a
/// legitimate denial -- logged loudly (ERROR) alongside the returned error.
fn reject_write_on_cross_community_table(schema: &TableSchema) -> Result<(), DbError> {
    if schema.cross_community_read {
        tracing::error!(
            table = %schema.table,
            "db capability: refusing a write against a cross-community-read table -- \
             this exception is read-only by design"
        );
        return Err(DbError::InvalidColumn(
            "this table is cross-community-read-only; writes are not permitted".to_string(),
        ));
    }
    Ok(())
}

/// Plain (column-type-agnostic) conversion of a [`DbValue`] to the SeaORM
/// value bound for it -- [`typed::bind_value`] layers the declared-column
/// typing (native `uuid`, typed NULLs) on top of this for every write.
pub(crate) fn db_value_to_sea_value(v: &DbValue) -> Value {
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
        (ColumnType::Timestamptz, DbValue::Text(s)) => {
            typed::validate_timestamptz(s)
                .map_err(|reason| DbError::InvalidValue(format!("{name:?} {reason}")))?;
        }
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

/// One guest-supplied column of an `insert`/`update`, resolved against the
/// schema into exactly what the statement needs: the quoted identifier, the
/// explicit type cast for its placeholder, and the typed bind value.
struct WriteColumn {
    quoted: String,
    cast: &'static str,
    value: Value,
}

impl WriteColumn {
    /// The placeholder for bind position `n` (1-based): `$n` plus the
    /// declared column's explicit cast, if it is a typed column.
    fn placeholder(&self, n: usize) -> String {
        format!("${n}{}", self.cast)
    }
}

/// Resolves every (already [`validate_column_values`]-checked) guest column
/// against `schema` -- the declared type decides the bind type and cast
/// (see [`crate::typed`]), never the guest value. Run **before** a
/// transaction is opened so a failure here costs no round trip.
fn plan_writes(
    schema: &TableSchema,
    column_values: &[(String, DbValue)],
) -> Result<Vec<WriteColumn>, DbError> {
    column_values
        .iter()
        .map(|(name, value)| {
            let col = schema.column(name).ok_or_else(|| {
                DbError::InvalidColumn(format!("{name:?} is not declared for this app"))
            })?;
            Ok(WriteColumn {
                quoted: quote_ident(name),
                cast: typed::param_cast(col),
                value: typed::bind_value(col, value)?,
            })
        })
        .collect()
}

/// The `SELECT` list `get`/`query` share: the platform `row_id`/`version`
/// followed by each declared column in its read form
/// ([`typed::select_expr`]).
fn select_list(schema: &TableSchema) -> String {
    let mut exprs = vec![quote_ident("row_id"), quote_ident("version")];
    exprs.extend(schema.columns.iter().map(typed::select_expr));
    exprs.join(", ")
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

/// Resolves the row predicate `get`/`query` actually use: [`tenant_predicate`]
/// for every ordinary table, or an unconditional `TRUE` (no tenant/community
/// filtering at all) for a table that opted into the cross-community read
/// exception (`schema.cross_community_read`, `crate::schema::TableSchema`'s
/// own doc -- reputation/user-details, never a bundle-controlled choice).
/// `app_id` scoping is untouched either way: `schema` itself was already
/// resolved for exactly one `app_id` before this is ever called, so a
/// cross-community-read table still only ever reads its own app's table.
fn scope_predicate(
    schema: &TableSchema,
    scope: &DbScope,
    next_param: usize,
) -> (String, Vec<Value>) {
    if schema.cross_community_read {
        ("TRUE".to_string(), Vec::new())
    } else {
        tenant_predicate(scope, next_param)
    }
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

    /// Bounded list of a scope's rows, ordered by `order_by` (default
    /// `row_id ASC`, a stable keyset-style page boundary, when `None`).
    /// Host-enforced page size (never guest-controlled beyond the
    /// [`MAX_QUERY_LIMIT`] ceiling).
    fn query<'a>(
        &'a self,
        schema: &'a TableSchema,
        scope: &'a DbScope,
        limit: u32,
        offset: u32,
        order_by: Option<OrderBy>,
    ) -> BoxFuture<'a, Result<Vec<Row>, DbError>>;
}

/// Validates an `order_by` column name against `schema`: must be either a
/// declared bundle column or one of the fixed platform columns safe to
/// sort by (`row_id`, `version`, `created_at`, `updated_at` -- `tenant_id`/
/// `community_id` excluded: sorting by them would leak cross-scope
/// ordering information with no legitimate bundle use case). Re-validates
/// the identifier shape too, same defense-in-depth posture as every other
/// identifier this crate puts into SQL text.
fn validate_order_column(schema: &TableSchema, name: &str) -> Result<(), DbError> {
    validate_identifier(name)
        .map_err(|_| DbError::InvalidColumn(format!("{name:?} is not a valid identifier")))?;
    const SORTABLE_PLATFORM_COLUMNS: &[&str] = &["row_id", "version", "created_at", "updated_at"];
    if SORTABLE_PLATFORM_COLUMNS.contains(&name) || schema.column(name).is_some() {
        Ok(())
    } else {
        Err(DbError::InvalidColumn(format!(
            "{name:?} is not a valid order-by column for this app"
        )))
    }
}

/// Renders `order_by` into an `ORDER BY` SQL fragment (never guest text --
/// `Column`'s `name` is validated by [`validate_order_column`] first).
fn order_by_sql(schema: &TableSchema, order_by: &Option<OrderBy>) -> Result<String, DbError> {
    match order_by {
        None => Ok("row_id ASC".to_string()),
        Some(OrderBy::Random) => Ok("random()".to_string()),
        Some(OrderBy::Column { name, descending }) => {
            validate_order_column(schema, name)?;
            let dir = if *descending { "DESC" } else { "ASC" };
            // Table-qualified on purpose: `get`/`query` project
            // `timestamptz`/`jsonb` columns under their own name (as text,
            // see `typed::select_expr`), and Postgres resolves a bare
            // `ORDER BY name` to that *output* column -- sorting the text
            // projection instead of the stored value. A qualified reference
            // always means the real column.
            Ok(format!(
                "{}.{} {dir}",
                quote_ident(&schema.table),
                quote_ident(name)
            ))
        }
    }
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
        reject_write_on_cross_community_table(schema)?;
        validate_column_values(schema, &column_values)?;
        let writes = plan_writes(schema, &column_values)?;

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

        let mut quoted_cols: Vec<String> = writes.iter().map(|w| w.quoted.clone()).collect();
        quoted_cols.push(quote_ident("tenant_id"));
        quoted_cols.push(quote_ident("community_id"));

        // Declared columns first (typed bind + explicit cast per column),
        // then the two platform scope columns, bound as text.
        let mut placeholders: Vec<String> = writes
            .iter()
            .enumerate()
            .map(|(i, w)| w.placeholder(i + 1))
            .collect();
        let mut bind_values: Vec<Value> = writes.into_iter().map(|w| w.value).collect();
        bind_values.push(Value::String(Some(scope.tenant.clone())));
        bind_values.push(Value::String(scope.community.clone()));
        placeholders.push(format!("${}", bind_values.len() - 1));
        placeholders.push(format!("${}", bind_values.len()));

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
            .map_err(typed::map_write_error)?
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

        let (pred_sql, mut pred_values) = scope_predicate(schema, scope, 2);
        pred_values.insert(0, Value::Uuid(Some(row_uuid)));
        if schema.cross_community_read {
            tracing::info!(
                app_id = %scope.app_id,
                table = %schema.table,
                "db capability: cross-community read (reputation/user-details exception)"
            );
        }

        let sql = format!(
            "SELECT {} FROM {} WHERE row_id = $1 AND {}",
            select_list(schema),
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

        let mut columns = Vec::with_capacity(schema.columns.len());
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
        reject_write_on_cross_community_table(schema)?;
        validate_column_values(schema, &column_values)?;
        // An empty map would render `SET , version = ...` -- a Postgres
        // syntax error that surfaced as a `backend` failure (ERROR log + a
        // "db unavailable or misconfigured" signal) for what is plain bad
        // guest input. Reject it up front as the validation error it is.
        if column_values.is_empty() {
            return Err(DbError::InvalidValue(
                "update requires at least one column value".to_string(),
            ));
        }
        let row_uuid = Uuid::parse_str(row_id)
            .map_err(|_| DbError::InvalidValue("row_id is not a valid UUID".to_string()))?;
        let expected_version_i64 = i64::try_from(expected_version)
            .map_err(|_| DbError::InvalidValue("expected_version out of range".to_string()))?;
        let writes = plan_writes(schema, &column_values)?;

        let txn = self
            .conn
            .begin()
            .await
            .map_err(|e| DbError::Backend(e.to_string()))?;
        set_local_scope(&txn, scope).await?;

        let set_clauses: Vec<String> = writes
            .iter()
            .enumerate()
            .map(|(i, w)| format!("{} = {}", w.quoted, w.placeholder(i + 1)))
            .collect();
        let mut bind_values: Vec<Value> = writes.into_iter().map(|w| w.value).collect();

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
            .map_err(typed::map_write_error)?;

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
        reject_write_on_cross_community_table(schema)?;
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
        order_by: Option<OrderBy>,
    ) -> Result<Vec<Row>, DbError> {
        let bounded_limit = limit.min(MAX_QUERY_LIMIT);
        let order_sql = order_by_sql(schema, &order_by)?;

        let txn = self
            .conn
            .begin()
            .await
            .map_err(|e| DbError::Backend(e.to_string()))?;
        set_local_scope(&txn, scope).await?;

        let (pred_sql, pred_values) = scope_predicate(schema, scope, 1);
        if schema.cross_community_read {
            tracing::info!(
                app_id = %scope.app_id,
                table = %schema.table,
                "db capability: cross-community read (reputation/user-details exception)"
            );
        }
        let limit_param = pred_values.len() + 1;
        let offset_param = limit_param + 1;
        let mut bind_values = pred_values;
        bind_values.push(Value::BigInt(Some(i64::from(bounded_limit))));
        bind_values.push(Value::BigInt(Some(i64::from(offset))));

        let sql = format!(
            "SELECT {} FROM {} WHERE {} ORDER BY {order_sql} LIMIT ${limit_param} OFFSET ${offset_param}",
            select_list(schema),
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
            let mut columns = Vec::with_capacity(schema.columns.len());
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

/// Decodes one declared column of a `get`/`query` result row. Reads the
/// form [`typed::select_expr`] projected: `timestamptz` (RFC 3339 UTC) and
/// `jsonb` (JSON text) arrive as plain text, `uuid` decodes natively -- so
/// the decode type here must stay in lockstep with that projection.
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
    match typed::effective_type(col) {
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
        order_by: Option<OrderBy>,
    ) -> BoxFuture<'a, Result<Vec<Row>, DbError>> {
        Box::pin(self.query_impl(schema, scope, limit, offset, order_by))
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
    fn scope_predicate_uses_tenant_predicate_for_an_ordinary_table() {
        let schema = schema_with_user_ref();
        let scope = DbScope::new("acme", Some("main".to_string()), "app");
        let (sql, values) = scope_predicate(&schema, &scope, 1);
        assert_eq!(sql, "tenant_id = $1 AND community_id = $2");
        assert_eq!(values.len(), 2);
    }

    #[test]
    fn scope_predicate_is_unconditional_for_a_cross_community_read_table() {
        let schema = schema_with_user_ref().with_cross_community_read();
        let scope = DbScope::new("acme", Some("main".to_string()), "app");
        let (sql, values) = scope_predicate(&schema, &scope, 1);
        assert_eq!(sql, "TRUE");
        assert!(values.is_empty(), "no tenant/community value must be bound");
    }

    #[test]
    fn reject_write_on_cross_community_table_allows_an_ordinary_table() {
        let schema = schema_with_user_ref();
        assert!(reject_write_on_cross_community_table(&schema).is_ok());
    }

    #[test]
    fn reject_write_on_cross_community_table_denies_a_cross_community_read_table() {
        let schema = schema_with_user_ref().with_cross_community_read();
        let err = reject_write_on_cross_community_table(&schema).unwrap_err();
        assert_eq!(err.code(), "invalid_column");
    }

    #[test]
    fn order_by_sql_defaults_to_row_id_ascending() {
        let schema = schema_with_user_ref();
        assert_eq!(order_by_sql(&schema, &None).unwrap(), "row_id ASC");
    }

    #[test]
    fn order_by_sql_renders_random() {
        let schema = schema_with_user_ref();
        assert_eq!(
            order_by_sql(&schema, &Some(OrderBy::Random)).unwrap(),
            "random()"
        );
    }

    #[test]
    fn order_by_sql_renders_a_declared_column_both_directions() {
        let schema = schema_with_user_ref();
        assert_eq!(
            order_by_sql(
                &schema,
                &Some(OrderBy::Column {
                    name: "score".to_string(),
                    descending: false
                })
            )
            .unwrap(),
            "\"fishing_core\".\"score\" ASC"
        );
        assert_eq!(
            order_by_sql(
                &schema,
                &Some(OrderBy::Column {
                    name: "score".to_string(),
                    descending: true
                })
            )
            .unwrap(),
            "\"fishing_core\".\"score\" DESC"
        );
    }

    #[test]
    fn order_by_sql_allows_sortable_platform_columns() {
        let schema = schema_with_user_ref();
        for col in ["row_id", "version", "created_at", "updated_at"] {
            assert!(order_by_sql(
                &schema,
                &Some(OrderBy::Column {
                    name: col.to_string(),
                    descending: false
                })
            )
            .is_ok());
        }
    }

    #[test]
    fn order_by_sql_rejects_tenant_and_community_id() {
        let schema = schema_with_user_ref();
        for col in ["tenant_id", "community_id"] {
            let err = order_by_sql(
                &schema,
                &Some(OrderBy::Column {
                    name: col.to_string(),
                    descending: false,
                }),
            )
            .unwrap_err();
            assert_eq!(err.code(), "invalid_column");
        }
    }

    #[test]
    fn order_by_sql_rejects_an_undeclared_column() {
        let schema = schema_with_user_ref();
        let err = order_by_sql(
            &schema,
            &Some(OrderBy::Column {
                name: "not_declared".to_string(),
                descending: false,
            }),
        )
        .unwrap_err();
        assert_eq!(err.code(), "invalid_column");
    }

    #[test]
    fn order_by_sql_rejects_sql_injection_shaped_column_name() {
        let schema = schema_with_user_ref();
        let err = order_by_sql(
            &schema,
            &Some(OrderBy::Column {
                name: "score; DROP TABLE fishing_core; --".to_string(),
                descending: false,
            }),
        )
        .unwrap_err();
        assert_eq!(err.code(), "invalid_column");
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

    // The tests below drive `PostgresBackend`'s real statement flow through
    // SeaORM's `MockDatabase` (scripted rows + a recorded statement log), so
    // the scope-enforcement, quota, and error-mapping branches are covered
    // with no Docker. `tests/postgres_integration.rs` still proves the same
    // flow against a real server (roles, RLS policy, advisory lock).

    use std::collections::BTreeMap;

    use crate::limits::STATEMENT_TIMEOUT_MS;
    use sea_orm::{DbErr, MockDatabase, MockExecResult};

    type MockRow = BTreeMap<String, Value>;

    const ROW_ID: &str = "11111111-1111-4111-8111-111111111111";
    const SCOPE_PROLOGUE_SQL: &str = "SELECT set_config('waddles.tenant_id', $1, true), \
         set_config('waddles.community_id', $2, true), \
         set_config('waddles.app_id', $3, true)";
    const LOCK_SQL: &str = "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))";

    /// One scripted result row from `(column, value)` pairs.
    fn mock_row(cells: Vec<(&str, Value)>) -> MockRow {
        cells
            .into_iter()
            .map(|(name, value)| (name.to_string(), value))
            .collect()
    }

    fn no_rows() -> Vec<MockRow> {
        Vec::new()
    }

    fn uuid_cell() -> Value {
        Value::Uuid(Some(
            Uuid::parse_str(ROW_ID).expect("ROW_ID is a valid UUID"),
        ))
    }

    fn text(v: &str) -> Value {
        Value::String(Some(v.to_string()))
    }

    fn big(v: i64) -> Value {
        Value::BigInt(Some(v))
    }

    fn exec_result(rows_affected: u64) -> MockExecResult {
        MockExecResult {
            last_insert_id: 0,
            rows_affected,
        }
    }

    fn exec_ok(n: usize) -> Vec<MockExecResult> {
        vec![exec_result(0); n]
    }

    fn boom() -> DbErr {
        DbErr::Custom("boom".to_string())
    }

    fn mock_db() -> MockDatabase {
        MockDatabase::new(SeaDbBackend::Postgres)
    }

    fn scoped() -> DbScope {
        DbScope::new("acme", Some("main".to_string()), "waddles.bot.a")
    }

    fn tenant_wide() -> DbScope {
        DbScope::new("acme", None, "waddles.bot.a")
    }

    /// A backend over a scripted mock connection, plus a handle to read back
    /// every statement it sent.
    struct Harness {
        backend: PostgresBackend,
        conn: DatabaseConnection,
    }

    impl Harness {
        fn new(db: MockDatabase) -> Self {
            let conn = db.into_connection();
            Self {
                backend: PostgresBackend::new(conn.clone()),
                conn,
            }
        }

        fn with_row_cap(db: MockDatabase, cap: i64) -> Self {
            let conn = db.into_connection();
            Self {
                backend: PostgresBackend::new(conn.clone()).with_row_cap_for_test(cap),
                conn,
            }
        }

        /// Every statement sent, in order (`BEGIN` ... `COMMIT`/`ROLLBACK`).
        fn statements(self) -> Vec<Statement> {
            self.conn
                .into_transaction_log()
                .into_iter()
                .flat_map(|txn| txn.statements().to_vec())
                .collect()
        }
    }

    fn bound(stmt: &Statement) -> Vec<Value> {
        stmt.values
            .as_ref()
            .map_or_else(Vec::new, |values| values.0.clone())
    }

    /// Asserts the transaction opened with both scope mechanisms: the
    /// statement timeout and the three `set_config` GUCs bound from `scope`.
    fn assert_prologue(stmts: &[Statement], scope: &DbScope) {
        assert_eq!(stmts[0].sql, "BEGIN");
        assert_eq!(
            stmts[1].sql,
            format!("SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
        );
        assert_eq!(stmts[2].sql, SCOPE_PROLOGUE_SQL);
        assert_eq!(
            bound(&stmts[2]),
            vec![
                text(&scope.tenant),
                Value::String(scope.community.clone()),
                text(&scope.app_id)
            ]
        );
    }

    fn assert_rolled_back(stmts: &[Statement]) {
        assert_eq!(stmts.last().map(|s| s.sql.as_str()), Some("ROLLBACK"));
        assert!(stmts.iter().all(|s| s.sql != "COMMIT"));
    }

    fn wide_schema() -> TableSchema {
        let col = |name: &str, sql_type: ColumnType| ColumnDef {
            name: name.to_string(),
            sql_type,
            nullable: true,
            is_user_ref: false,
        };
        TableSchema::validated(
            AppSchema::Community,
            "wide_tbl",
            vec![
                col("flag", ColumnType::Bool),
                col("small", ColumnType::Int4),
                col("big", ColumnType::Int8),
                col("note", ColumnType::Text),
                col("seen_at", ColumnType::Timestamptz),
                col("doc", ColumnType::Jsonb),
                col("other_id", ColumnType::Uuid),
            ],
        )
        .expect("wide_schema is valid")
    }

    fn wide_row(cells: Vec<(&str, Value)>) -> MockRow {
        let mut all = vec![("row_id", uuid_cell()), ("version", big(3))];
        all.extend(cells);
        mock_row(all)
    }

    fn count_row(n: i64) -> Vec<MockRow> {
        vec![mock_row(vec![("n", big(n))])]
    }

    fn insert_returning_row() -> Vec<MockRow> {
        vec![mock_row(vec![("row_id", uuid_cell()), ("version", big(1))])]
    }

    #[tokio::test]
    async fn insert_sends_the_full_scoped_transaction_and_binds_scope_from_the_invocation() {
        let h = Harness::new(
            mock_db()
                .append_exec_results(exec_ok(3))
                .append_query_results([count_row(0), insert_returning_row()]),
        );
        let scope = scoped();
        let cols = vec![
            ("score".to_string(), DbValue::Int(7)),
            ("note".to_string(), DbValue::Text("hi".to_string())),
        ];

        let row = h
            .backend
            .insert(&schema_with_user_ref(), &scope, cols.clone())
            .await
            .unwrap();
        assert_eq!(
            row,
            Row {
                row_id: ROW_ID.to_string(),
                version: 1,
                columns: cols
            }
        );

        let stmts = h.statements();
        assert_eq!(
            stmts.len(),
            7,
            "BEGIN, 2 scope stmts, lock, count, insert, COMMIT"
        );
        assert_prologue(&stmts, &scope);
        assert_eq!(stmts[3].sql, LOCK_SQL);
        assert_eq!(bound(&stmts[3]), vec![text("acme:main:waddles.bot.a")]);
        assert_eq!(
            stmts[4].sql,
            "SELECT COUNT(*) AS n FROM \"app_core\".\"fishing_core\" \
             WHERE tenant_id = $1 AND community_id = $2"
        );
        assert_eq!(bound(&stmts[4]), vec![text("acme"), text("main")]);
        assert_eq!(
            stmts[5].sql,
            "INSERT INTO \"app_core\".\"fishing_core\" \
             (\"score\", \"note\", \"tenant_id\", \"community_id\") \
             VALUES ($1, $2, $3, $4) RETURNING row_id, version"
        );
        assert_eq!(
            bound(&stmts[5]),
            vec![big(7), text("hi"), text("acme"), text("main")]
        );
        assert_eq!(stmts[6].sql, "COMMIT");
    }

    #[tokio::test]
    async fn insert_without_a_community_scopes_to_tenant_wide_rows_only() {
        let h = Harness::new(
            mock_db()
                .append_exec_results(exec_ok(3))
                .append_query_results([count_row(0), insert_returning_row()]),
        );
        let scope = tenant_wide();

        h.backend
            .insert(
                &schema_with_user_ref(),
                &scope,
                vec![("score".to_string(), DbValue::Int(1))],
            )
            .await
            .unwrap();

        let stmts = h.statements();
        assert_prologue(&stmts, &scope);
        assert_eq!(bound(&stmts[3]), vec![text("acme::waddles.bot.a")]);
        assert_eq!(
            stmts[4].sql,
            "SELECT COUNT(*) AS n FROM \"app_core\".\"fishing_core\" \
             WHERE tenant_id = $1 AND community_id IS NULL"
        );
        assert_eq!(bound(&stmts[4]), vec![text("acme")]);
        assert_eq!(
            bound(&stmts[5]),
            vec![big(1), text("acme"), Value::String(None)],
            "a tenant-wide row is written with a NULL community"
        );
    }

    #[tokio::test]
    async fn insert_keeps_a_sql_shaped_guest_value_out_of_the_sql_text() {
        let evil = "x'); DROP TABLE app_core.fishing_core; --";
        let h = Harness::new(
            mock_db()
                .append_exec_results(exec_ok(3))
                .append_query_results([count_row(0), insert_returning_row()]),
        );

        h.backend
            .insert(
                &schema_with_user_ref(),
                &scoped(),
                vec![("note".to_string(), DbValue::Text(evil.to_string()))],
            )
            .await
            .unwrap();

        let stmts = h.statements();
        assert!(stmts.iter().all(|s| !s.sql.contains("DROP")));
        assert!(bound(&stmts[5]).contains(&text(evil)));
    }

    #[tokio::test]
    async fn insert_refuses_a_guest_supplied_scope_or_platform_column_before_any_io() {
        for name in [
            "tenant_id",
            "community_id",
            "row_id",
            "version",
            "created_at",
        ] {
            let h = Harness::new(mock_db());
            let err = h
                .backend
                .insert(
                    &schema_with_user_ref(),
                    &scoped(),
                    vec![(name.to_string(), DbValue::Text("evil".to_string()))],
                )
                .await
                .unwrap_err();
            assert_eq!(err.code(), "invalid_column", "column {name}");
            assert!(h.statements().is_empty(), "{name} reached the database");
        }
    }

    #[tokio::test]
    async fn every_write_refuses_a_cross_community_read_table_before_any_io() {
        let schema = schema_with_user_ref().with_cross_community_read();
        let scope = scoped();
        let cols = vec![("score".to_string(), DbValue::Int(1))];

        let h = Harness::new(mock_db());
        let err = h
            .backend
            .insert(&schema, &scope, cols.clone())
            .await
            .unwrap_err();
        assert_eq!(err.code(), "invalid_column");
        assert!(h.statements().is_empty());

        let h = Harness::new(mock_db());
        let err = h
            .backend
            .update(&schema, &scope, ROW_ID, 1, cols)
            .await
            .unwrap_err();
        assert_eq!(err.code(), "invalid_column");
        assert!(h.statements().is_empty());

        let h = Harness::new(mock_db());
        let err = h
            .backend
            .delete(&schema, &scope, ROW_ID, 1)
            .await
            .unwrap_err();
        assert_eq!(err.code(), "invalid_column");
        assert!(h.statements().is_empty());
    }

    #[tokio::test]
    async fn insert_rejects_an_invalid_value_before_any_io() {
        let h = Harness::new(mock_db());
        let err = h
            .backend
            .insert(
                &schema_with_user_ref(),
                &scoped(),
                vec![("score".to_string(), DbValue::Text("not an int".to_string()))],
            )
            .await
            .unwrap_err();
        assert_eq!(err.code(), "invalid_value");
        assert!(h.statements().is_empty());
    }

    #[tokio::test]
    async fn insert_at_the_row_cap_is_rejected_and_rolled_back_without_inserting() {
        let h = Harness::with_row_cap(
            mock_db()
                .append_exec_results(exec_ok(3))
                .append_query_results([count_row(2)]),
            2,
        );

        let err = h
            .backend
            .insert(&schema_with_user_ref(), &scoped(), vec![])
            .await
            .unwrap_err();
        assert_eq!(
            err,
            DbError::QuotaExceeded("row count would exceed the per-app cap (2)".to_string())
        );

        let stmts = h.statements();
        assert!(stmts.iter().all(|s| !s.sql.starts_with("INSERT")));
        assert_rolled_back(&stmts);
    }

    #[tokio::test]
    async fn insert_one_row_below_the_cap_is_admitted() {
        let h = Harness::with_row_cap(
            mock_db()
                .append_exec_results(exec_ok(3))
                .append_query_results([count_row(1), insert_returning_row()]),
            2,
        );

        let row = h
            .backend
            .insert(&schema_with_user_ref(), &scoped(), vec![])
            .await
            .unwrap();
        assert_eq!(row.version, 1);
    }

    #[tokio::test]
    async fn the_default_row_cap_is_the_documented_per_app_ceiling() {
        let conn = mock_db().into_connection();
        assert_eq!(PostgresBackend::new(conn).row_cap, MAX_ROWS_PER_APP);

        let h = Harness::new(
            mock_db()
                .append_exec_results(exec_ok(3))
                .append_query_results([count_row(MAX_ROWS_PER_APP)]),
        );
        let err = h
            .backend
            .insert(&schema_with_user_ref(), &scoped(), vec![])
            .await
            .unwrap_err();
        assert_eq!(err.code(), "quota_exceeded");
    }

    #[tokio::test]
    async fn insert_maps_every_backend_failure_to_a_rolled_back_backend_error() {
        let scenarios: Vec<(&str, MockDatabase)> = vec![
            (
                "SET LOCAL statement_timeout fails",
                mock_db().append_exec_errors([boom()]),
            ),
            (
                "set_config fails",
                mock_db()
                    .append_exec_results(exec_ok(1))
                    .append_exec_errors([boom()]),
            ),
            (
                "advisory lock fails",
                mock_db()
                    .append_exec_results(exec_ok(2))
                    .append_exec_errors([boom()]),
            ),
            (
                "COUNT query fails",
                mock_db()
                    .append_exec_results(exec_ok(3))
                    .append_query_errors([boom()]),
            ),
            (
                "COUNT returns no row",
                mock_db()
                    .append_exec_results(exec_ok(3))
                    .append_query_results([no_rows()]),
            ),
            (
                "COUNT row has no n column",
                mock_db()
                    .append_exec_results(exec_ok(3))
                    .append_query_results([vec![mock_row(vec![("other", big(0))])]]),
            ),
            (
                "INSERT fails",
                mock_db()
                    .append_exec_results(exec_ok(3))
                    .append_query_results([count_row(0)])
                    .append_query_errors([boom()]),
            ),
            (
                "INSERT returns no row",
                mock_db()
                    .append_exec_results(exec_ok(3))
                    .append_query_results([count_row(0), no_rows()]),
            ),
            (
                "INSERT row lacks row_id",
                mock_db()
                    .append_exec_results(exec_ok(3))
                    .append_query_results([
                        count_row(0),
                        vec![mock_row(vec![("version", big(1))])],
                    ]),
            ),
            (
                "INSERT row lacks version",
                mock_db()
                    .append_exec_results(exec_ok(3))
                    .append_query_results([
                        count_row(0),
                        vec![mock_row(vec![("row_id", uuid_cell())])],
                    ]),
            ),
        ];

        for (label, db) in scenarios {
            let h = Harness::new(db);
            let err = h
                .backend
                .insert(&schema_with_user_ref(), &scoped(), vec![])
                .await
                .unwrap_err();
            assert_eq!(err.code(), "backend", "{label}: {err:?}");
            assert_rolled_back(&h.statements());
        }
    }

    #[tokio::test]
    async fn get_reads_back_every_declared_column_type() {
        let h = Harness::new(
            mock_db()
                .append_exec_results(exec_ok(2))
                .append_query_results([vec![wide_row(vec![
                    ("flag", Value::Bool(Some(true))),
                    ("small", Value::Int(Some(5))),
                    ("big", big(9_000_000_000)),
                    ("note", text("hi")),
                    ("seen_at", text("2026-01-01T00:00:00Z")),
                    ("doc", text("{\"a\":1}")),
                    ("other_id", uuid_cell()),
                ])]]),
        );

        let row = h
            .backend
            .get(&wide_schema(), &scoped(), ROW_ID)
            .await
            .unwrap();

        assert_eq!(row.row_id, ROW_ID);
        assert_eq!(row.version, 3);
        assert_eq!(
            row.columns,
            vec![
                ("flag".to_string(), DbValue::Bool(true)),
                ("small".to_string(), DbValue::Int(5)),
                ("big".to_string(), DbValue::Int(9_000_000_000)),
                ("note".to_string(), DbValue::Text("hi".to_string())),
                (
                    "seen_at".to_string(),
                    DbValue::Text("2026-01-01T00:00:00Z".to_string())
                ),
                ("doc".to_string(), DbValue::Text("{\"a\":1}".to_string())),
                ("other_id".to_string(), DbValue::Text(ROW_ID.to_string())),
            ]
        );
    }

    #[tokio::test]
    async fn get_maps_sql_null_in_every_column_type_to_db_null() {
        let h = Harness::new(
            mock_db()
                .append_exec_results(exec_ok(2))
                .append_query_results([vec![wide_row(vec![
                    ("flag", Value::Bool(None)),
                    ("small", Value::Int(None)),
                    ("big", Value::BigInt(None)),
                    ("note", Value::String(None)),
                    ("seen_at", Value::String(None)),
                    ("doc", Value::String(None)),
                    ("other_id", Value::Uuid(None)),
                ])]]),
        );

        let row = h
            .backend
            .get(&wide_schema(), &scoped(), ROW_ID)
            .await
            .unwrap();
        assert_eq!(row.columns.len(), 7);
        assert!(row.columns.iter().all(|(_, v)| *v == DbValue::Null));
    }

    #[tokio::test]
    async fn get_filters_by_row_id_and_the_invocation_scope() {
        let h = Harness::new(
            mock_db()
                .append_exec_results(exec_ok(2))
                .append_query_results([vec![mock_row(vec![
                    ("row_id", uuid_cell()),
                    ("version", big(4)),
                    ("user_ref", Value::Uuid(None)),
                    ("score", big(1)),
                    ("note", Value::String(None)),
                ])]]),
        );
        let scope = scoped();

        h.backend
            .get(&schema_with_user_ref(), &scope, ROW_ID)
            .await
            .unwrap();

        let stmts = h.statements();
        assert_prologue(&stmts, &scope);
        assert_eq!(
            stmts[3].sql,
            "SELECT \"row_id\", \"version\", \"user_ref\", \"score\", \"note\" \
             FROM \"app_core\".\"fishing_core\" \
             WHERE row_id = $1 AND tenant_id = $2 AND community_id = $3"
        );
        assert_eq!(
            bound(&stmts[3]),
            vec![uuid_cell(), text("acme"), text("main")]
        );
        assert_eq!(stmts[4].sql, "COMMIT");
    }

    #[tokio::test]
    async fn get_on_a_cross_community_table_drops_only_the_tenant_predicate() {
        let h = Harness::new(
            mock_db()
                .append_exec_results(exec_ok(2))
                .append_query_results([vec![mock_row(vec![
                    ("row_id", uuid_cell()),
                    ("version", big(1)),
                    ("user_ref", Value::Uuid(None)),
                    ("score", big(1)),
                    ("note", Value::String(None)),
                ])]]),
        );

        h.backend
            .get(
                &schema_with_user_ref().with_cross_community_read(),
                &scoped(),
                ROW_ID,
            )
            .await
            .unwrap();

        let stmts = h.statements();
        assert!(stmts[3].sql.ends_with("WHERE row_id = $1 AND TRUE"));
        assert_eq!(bound(&stmts[3]), vec![uuid_cell()]);
    }

    #[tokio::test]
    async fn get_rejects_a_non_uuid_row_id_before_any_io() {
        let h = Harness::new(mock_db());
        let err = h
            .backend
            .get(&schema_with_user_ref(), &scoped(), "1 OR 1=1")
            .await
            .unwrap_err();
        assert_eq!(err.code(), "invalid_value");
        assert!(h.statements().is_empty());
    }

    #[tokio::test]
    async fn get_of_a_row_outside_the_scope_is_not_found_and_never_commits() {
        let h = Harness::new(
            mock_db()
                .append_exec_results(exec_ok(2))
                .append_query_results([no_rows()]),
        );
        let err = h
            .backend
            .get(&schema_with_user_ref(), &scoped(), ROW_ID)
            .await
            .unwrap_err();
        assert_eq!(err, DbError::NotFound);
        assert_rolled_back(&h.statements());
    }

    /// The mock driver reports a missing/mistyped *nullable* declared column
    /// as SQL NULL (unlike sqlx), so only the always-required `version`
    /// column is exercised for a decode failure here.
    #[tokio::test]
    async fn get_maps_backend_and_decode_failures_to_backend_errors() {
        let scenarios: Vec<(&str, MockDatabase)> = vec![
            ("SET LOCAL fails", mock_db().append_exec_errors([boom()])),
            (
                "set_config fails",
                mock_db()
                    .append_exec_results(exec_ok(1))
                    .append_exec_errors([boom()]),
            ),
            (
                "SELECT fails",
                mock_db()
                    .append_exec_results(exec_ok(2))
                    .append_query_errors([boom()]),
            ),
            (
                "row lacks version",
                mock_db()
                    .append_exec_results(exec_ok(2))
                    .append_query_results([vec![mock_row(vec![
                        ("row_id", uuid_cell()),
                        ("user_ref", Value::Uuid(None)),
                        ("score", big(1)),
                        ("note", Value::String(None)),
                    ])]]),
            ),
        ];

        for (label, db) in scenarios {
            let h = Harness::new(db);
            let err = h
                .backend
                .get(&schema_with_user_ref(), &scoped(), ROW_ID)
                .await
                .unwrap_err();
            assert_eq!(err.code(), "backend", "{label}: {err:?}");
        }
    }

    #[tokio::test]
    async fn update_is_version_checked_and_scoped_in_one_statement() {
        let h = Harness::new(
            mock_db()
                .append_exec_results(exec_ok(2))
                .append_query_results([vec![mock_row(vec![("version", big(5))])]]),
        );
        let scope = scoped();
        let cols = vec![
            ("score".to_string(), DbValue::Int(9)),
            ("note".to_string(), DbValue::Text("n".to_string())),
        ];

        let row = h
            .backend
            .update(&schema_with_user_ref(), &scope, ROW_ID, 4, cols.clone())
            .await
            .unwrap();
        assert_eq!(
            row,
            Row {
                row_id: ROW_ID.to_string(),
                version: 5,
                columns: cols
            }
        );

        let stmts = h.statements();
        assert_eq!(stmts.len(), 5);
        assert_prologue(&stmts, &scope);
        assert_eq!(
            stmts[3].sql,
            "UPDATE \"app_core\".\"fishing_core\" SET \"score\" = $1, \"note\" = $2, \
             version = version + 1, updated_at = now() \
             WHERE row_id = $3 AND version = $4 AND tenant_id = $5 AND community_id = $6 \
             RETURNING version"
        );
        assert_eq!(
            bound(&stmts[3]),
            vec![
                big(9),
                text("n"),
                uuid_cell(),
                big(4),
                text("acme"),
                text("main")
            ]
        );
        assert_eq!(stmts[4].sql, "COMMIT");
    }

    #[tokio::test]
    async fn update_without_a_community_matches_only_tenant_wide_rows() {
        let h = Harness::new(
            mock_db()
                .append_exec_results(exec_ok(2))
                .append_query_results([vec![mock_row(vec![("version", big(2))])]]),
        );

        h.backend
            .update(
                &schema_with_user_ref(),
                &tenant_wide(),
                ROW_ID,
                1,
                vec![("score".to_string(), DbValue::Int(1))],
            )
            .await
            .unwrap();

        let stmts = h.statements();
        assert!(stmts[3]
            .sql
            .ends_with("AND tenant_id = $4 AND community_id IS NULL RETURNING version"));
        assert_eq!(
            bound(&stmts[3]),
            vec![big(1), uuid_cell(), big(1), text("acme")]
        );
    }

    #[tokio::test]
    async fn update_of_a_stale_version_is_a_conflict_but_a_missing_row_is_not_found() {
        let probe = |exists: Vec<MockRow>| {
            Harness::new(
                mock_db()
                    .append_exec_results(exec_ok(2))
                    .append_query_results([no_rows(), exists]),
            )
        };
        let cols = || vec![("score".to_string(), DbValue::Int(1))];

        let h = probe(vec![mock_row(vec![("present", Value::Int(Some(1)))])]);
        let err = h
            .backend
            .update(&schema_with_user_ref(), &scoped(), ROW_ID, 1, cols())
            .await
            .unwrap_err();
        assert_eq!(err, DbError::Conflict);
        let stmts = h.statements();
        assert_eq!(
            stmts[4].sql,
            "SELECT 1 AS present FROM \"app_core\".\"fishing_core\" \
             WHERE row_id = $1 AND tenant_id = $2 AND community_id = $3"
        );
        assert_eq!(
            bound(&stmts[4]),
            vec![uuid_cell(), text("acme"), text("main")],
            "the existence probe is scoped too"
        );
        assert_eq!(stmts[5].sql, "COMMIT");

        let h = probe(no_rows());
        let err = h
            .backend
            .update(&schema_with_user_ref(), &scoped(), ROW_ID, 1, cols())
            .await
            .unwrap_err();
        assert_eq!(err, DbError::NotFound);
    }

    #[tokio::test]
    async fn update_rejects_malformed_input_before_any_io() {
        let schema = schema_with_user_ref();
        let scope = scoped();
        let one_col = || vec![("score".to_string(), DbValue::Int(1))];

        let cases: Vec<(&str, Result<Row, DbError>)> = vec![
            (
                "no columns",
                Harness::new(mock_db())
                    .backend
                    .update(&schema, &scope, ROW_ID, 1, vec![])
                    .await,
            ),
            (
                "row_id not a uuid",
                Harness::new(mock_db())
                    .backend
                    .update(&schema, &scope, "nope", 1, one_col())
                    .await,
            ),
            (
                "expected_version beyond i64",
                Harness::new(mock_db())
                    .backend
                    .update(&schema, &scope, ROW_ID, u64::MAX, one_col())
                    .await,
            ),
            (
                "platform column",
                Harness::new(mock_db())
                    .backend
                    .update(
                        &schema,
                        &scope,
                        ROW_ID,
                        1,
                        vec![("tenant_id".to_string(), DbValue::Text("x".to_string()))],
                    )
                    .await,
            ),
        ];
        for (label, result) in cases {
            let err = result.unwrap_err();
            assert!(
                matches!(err.code(), "invalid_value" | "invalid_column"),
                "{label}: {err:?}"
            );
        }

        // regression: an empty column map used to render `SET , version = ...`,
        // a Postgres syntax error reported as an unhealthy-backend failure.
        let h = Harness::new(mock_db());
        let err = h
            .backend
            .update(&schema, &scope, ROW_ID, 1, vec![])
            .await
            .unwrap_err();
        assert_eq!(
            err,
            DbError::InvalidValue("update requires at least one column value".to_string())
        );
        assert!(h.statements().is_empty(), "must not open a transaction");
    }

    #[tokio::test]
    async fn update_maps_every_backend_failure_to_a_backend_error() {
        let cols = || vec![("score".to_string(), DbValue::Int(1))];
        let scenarios: Vec<(&str, MockDatabase)> = vec![
            ("SET LOCAL fails", mock_db().append_exec_errors([boom()])),
            (
                "set_config fails",
                mock_db()
                    .append_exec_results(exec_ok(1))
                    .append_exec_errors([boom()]),
            ),
            (
                "UPDATE fails",
                mock_db()
                    .append_exec_results(exec_ok(2))
                    .append_query_errors([boom()]),
            ),
            (
                "existence probe fails",
                mock_db()
                    .append_exec_results(exec_ok(2))
                    .append_query_results([no_rows()])
                    .append_query_errors([boom()]),
            ),
            (
                "UPDATE row lacks version",
                mock_db()
                    .append_exec_results(exec_ok(2))
                    .append_query_results([vec![mock_row(vec![("other", big(1))])]]),
            ),
        ];

        for (label, db) in scenarios {
            let h = Harness::new(db);
            let err = h
                .backend
                .update(&schema_with_user_ref(), &scoped(), ROW_ID, 1, cols())
                .await
                .unwrap_err();
            assert_eq!(err.code(), "backend", "{label}: {err:?}");
        }
    }

    #[tokio::test]
    async fn delete_is_version_checked_and_scoped_in_one_statement() {
        let h = Harness::new(mock_db().append_exec_results([
            exec_result(0),
            exec_result(0),
            exec_result(1),
        ]));
        let scope = scoped();

        h.backend
            .delete(&schema_with_user_ref(), &scope, ROW_ID, 6)
            .await
            .unwrap();

        let stmts = h.statements();
        assert_eq!(stmts.len(), 5);
        assert_prologue(&stmts, &scope);
        assert_eq!(
            stmts[3].sql,
            "DELETE FROM \"app_core\".\"fishing_core\" \
             WHERE row_id = $1 AND version = $2 AND tenant_id = $3 AND community_id = $4"
        );
        assert_eq!(
            bound(&stmts[3]),
            vec![uuid_cell(), big(6), text("acme"), text("main")]
        );
        assert_eq!(stmts[4].sql, "COMMIT");
    }

    #[tokio::test]
    async fn delete_without_a_community_matches_only_tenant_wide_rows() {
        let h = Harness::new(mock_db().append_exec_results([
            exec_result(0),
            exec_result(0),
            exec_result(1),
        ]));

        h.backend
            .delete(&schema_with_user_ref(), &tenant_wide(), ROW_ID, 1)
            .await
            .unwrap();

        let stmts = h.statements();
        assert!(stmts[3]
            .sql
            .ends_with("AND tenant_id = $3 AND community_id IS NULL"));
        assert_eq!(bound(&stmts[3]), vec![uuid_cell(), big(1), text("acme")]);
    }

    #[tokio::test]
    async fn delete_of_a_stale_version_is_a_conflict_but_a_missing_row_is_not_found() {
        let probe = |exists: Vec<MockRow>| {
            Harness::new(
                mock_db()
                    .append_exec_results([exec_result(0), exec_result(0), exec_result(0)])
                    .append_query_results([exists]),
            )
        };

        let h = probe(vec![mock_row(vec![("present", Value::Int(Some(1)))])]);
        let err = h
            .backend
            .delete(&schema_with_user_ref(), &scoped(), ROW_ID, 1)
            .await
            .unwrap_err();
        assert_eq!(err, DbError::Conflict);
        let stmts = h.statements();
        assert_eq!(
            bound(&stmts[4]),
            vec![uuid_cell(), text("acme"), text("main")]
        );
        assert_eq!(stmts[5].sql, "COMMIT");

        let h = probe(no_rows());
        let err = h
            .backend
            .delete(&schema_with_user_ref(), &scoped(), ROW_ID, 1)
            .await
            .unwrap_err();
        assert_eq!(err, DbError::NotFound);
    }

    #[tokio::test]
    async fn delete_rejects_malformed_input_before_any_io() {
        let schema = schema_with_user_ref();
        let scope = scoped();

        let h = Harness::new(mock_db());
        let err = h
            .backend
            .delete(&schema, &scope, "not-a-uuid", 1)
            .await
            .unwrap_err();
        assert_eq!(err.code(), "invalid_value");
        assert!(h.statements().is_empty());

        let h = Harness::new(mock_db());
        let err = h
            .backend
            .delete(&schema, &scope, ROW_ID, u64::MAX)
            .await
            .unwrap_err();
        assert_eq!(
            err,
            DbError::InvalidValue("expected_version out of range".to_string())
        );
        assert!(h.statements().is_empty());
    }

    #[tokio::test]
    async fn delete_maps_every_backend_failure_to_a_backend_error() {
        let scenarios: Vec<(&str, MockDatabase)> = vec![
            ("SET LOCAL fails", mock_db().append_exec_errors([boom()])),
            (
                "set_config fails",
                mock_db()
                    .append_exec_results(exec_ok(1))
                    .append_exec_errors([boom()]),
            ),
            (
                "DELETE fails",
                mock_db()
                    .append_exec_results(exec_ok(2))
                    .append_exec_errors([boom()]),
            ),
            (
                "existence probe fails",
                mock_db()
                    .append_exec_results([exec_result(0), exec_result(0), exec_result(0)])
                    .append_query_errors([boom()]),
            ),
        ];

        for (label, db) in scenarios {
            let h = Harness::new(db);
            let err = h
                .backend
                .delete(&schema_with_user_ref(), &scoped(), ROW_ID, 1)
                .await
                .unwrap_err();
            assert_eq!(err.code(), "backend", "{label}: {err:?}");
        }
    }

    fn list_row(row_id: Value, version: i64, score: i64) -> MockRow {
        mock_row(vec![
            ("row_id", row_id),
            ("version", big(version)),
            ("user_ref", Value::Uuid(None)),
            ("score", big(score)),
            ("note", Value::String(None)),
        ])
    }

    #[tokio::test]
    async fn query_lists_scoped_rows_in_the_default_stable_order() {
        let h = Harness::new(
            mock_db()
                .append_exec_results(exec_ok(2))
                .append_query_results([vec![
                    list_row(uuid_cell(), 1, 10),
                    list_row(uuid_cell(), 2, 20),
                ]]),
        );
        let scope = scoped();

        let rows = h
            .backend
            .query(&schema_with_user_ref(), &scope, 25, 5, None)
            .await
            .unwrap();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[1].version, 2);
        assert_eq!(
            rows[0].columns,
            vec![
                ("user_ref".to_string(), DbValue::Null),
                ("score".to_string(), DbValue::Int(10)),
                ("note".to_string(), DbValue::Null),
            ]
        );

        let stmts = h.statements();
        assert_prologue(&stmts, &scope);
        assert_eq!(
            stmts[3].sql,
            "SELECT \"row_id\", \"version\", \"user_ref\", \"score\", \"note\" \
             FROM \"app_core\".\"fishing_core\" \
             WHERE tenant_id = $1 AND community_id = $2 \
             ORDER BY row_id ASC LIMIT $3 OFFSET $4"
        );
        assert_eq!(
            bound(&stmts[3]),
            vec![text("acme"), text("main"), big(25), big(5)]
        );
        assert_eq!(stmts[4].sql, "COMMIT");
    }

    #[tokio::test]
    async fn query_clamps_a_guest_page_size_to_the_host_ceiling() {
        let h = Harness::new(
            mock_db()
                .append_exec_results(exec_ok(2))
                .append_query_results([no_rows()]),
        );

        let rows = h
            .backend
            .query(&schema_with_user_ref(), &scoped(), u32::MAX, 0, None)
            .await
            .unwrap();
        assert!(rows.is_empty());

        let stmts = h.statements();
        assert_eq!(
            bound(&stmts[3]),
            vec![
                text("acme"),
                text("main"),
                big(i64::from(MAX_QUERY_LIMIT)),
                big(0)
            ]
        );
    }

    #[tokio::test]
    async fn query_without_a_community_binds_only_the_tenant() {
        let h = Harness::new(
            mock_db()
                .append_exec_results(exec_ok(2))
                .append_query_results([no_rows()]),
        );

        h.backend
            .query(&schema_with_user_ref(), &tenant_wide(), 10, 0, None)
            .await
            .unwrap();

        let stmts = h.statements();
        assert!(stmts[3].sql.contains(
            "WHERE tenant_id = $1 AND community_id IS NULL ORDER BY row_id ASC LIMIT $2 OFFSET $3"
        ));
        assert_eq!(bound(&stmts[3]), vec![text("acme"), big(10), big(0)]);
    }

    #[tokio::test]
    async fn query_on_a_cross_community_table_drops_only_the_tenant_predicate() {
        let h = Harness::new(
            mock_db()
                .append_exec_results(exec_ok(2))
                .append_query_results([no_rows()]),
        );

        h.backend
            .query(
                &schema_with_user_ref().with_cross_community_read(),
                &scoped(),
                10,
                0,
                None,
            )
            .await
            .unwrap();

        let stmts = h.statements();
        assert!(stmts[3]
            .sql
            .contains("WHERE TRUE ORDER BY row_id ASC LIMIT $1 OFFSET $2"));
        assert_eq!(bound(&stmts[3]), vec![big(10), big(0)]);
    }

    #[tokio::test]
    async fn query_renders_each_order_by_choice() {
        let cases = [
            (Some(OrderBy::Random), "ORDER BY random() LIMIT"),
            (
                Some(OrderBy::Column {
                    name: "score".to_string(),
                    descending: true,
                }),
                "ORDER BY \"fishing_core\".\"score\" DESC LIMIT",
            ),
            (
                Some(OrderBy::Column {
                    name: "created_at".to_string(),
                    descending: false,
                }),
                "ORDER BY \"fishing_core\".\"created_at\" ASC LIMIT",
            ),
        ];
        for (order_by, expected) in cases {
            let h = Harness::new(
                mock_db()
                    .append_exec_results(exec_ok(2))
                    .append_query_results([no_rows()]),
            );
            h.backend
                .query(&schema_with_user_ref(), &scoped(), 1, 0, order_by)
                .await
                .unwrap();
            let stmts = h.statements();
            assert!(stmts[3].sql.contains(expected), "{}", stmts[3].sql);
        }
    }

    #[tokio::test]
    async fn query_rejects_a_bad_order_by_column_before_any_io() {
        for name in ["tenant_id", "community_id", "ghost", "score; DROP TABLE x"] {
            let h = Harness::new(mock_db());
            let err = h
                .backend
                .query(
                    &schema_with_user_ref(),
                    &scoped(),
                    10,
                    0,
                    Some(OrderBy::Column {
                        name: name.to_string(),
                        descending: false,
                    }),
                )
                .await
                .unwrap_err();
            assert_eq!(err.code(), "invalid_column", "{name}");
            assert!(h.statements().is_empty(), "{name} reached the database");
        }
    }

    #[tokio::test]
    async fn query_maps_backend_and_decode_failures_to_backend_errors() {
        let scenarios: Vec<(&str, MockDatabase)> = vec![
            ("SET LOCAL fails", mock_db().append_exec_errors([boom()])),
            (
                "set_config fails",
                mock_db()
                    .append_exec_results(exec_ok(1))
                    .append_exec_errors([boom()]),
            ),
            (
                "SELECT fails",
                mock_db()
                    .append_exec_results(exec_ok(2))
                    .append_query_errors([boom()]),
            ),
            (
                "row lacks row_id",
                mock_db()
                    .append_exec_results(exec_ok(2))
                    .append_query_results([vec![mock_row(vec![
                        ("version", big(1)),
                        ("user_ref", Value::Uuid(None)),
                        ("score", big(1)),
                        ("note", Value::String(None)),
                    ])]]),
            ),
            (
                "row lacks version",
                mock_db()
                    .append_exec_results(exec_ok(2))
                    .append_query_results([vec![mock_row(vec![
                        ("row_id", uuid_cell()),
                        ("user_ref", Value::Uuid(None)),
                        ("score", big(1)),
                        ("note", Value::String(None)),
                    ])]]),
            ),
        ];

        for (label, db) in scenarios {
            let h = Harness::new(db);
            let err = h
                .backend
                .query(&schema_with_user_ref(), &scoped(), 10, 0, None)
                .await
                .unwrap_err();
            assert_eq!(err.code(), "backend", "{label}: {err:?}");
        }
    }

    #[tokio::test]
    async fn the_trait_object_dispatches_every_op_to_the_postgres_backend() {
        // Execs per op: insert 3 (2 scope + lock), get 2, update 2, delete 3
        // (2 scope + the DELETE, which reports 1 row affected), query 2.
        let execs = [0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0].map(exec_result);
        let h = Harness::new(mock_db().append_exec_results(execs).append_query_results([
            count_row(0),
            insert_returning_row(),
            vec![list_row(uuid_cell(), 1, 1)],
            vec![mock_row(vec![("version", big(2))])],
            vec![list_row(uuid_cell(), 2, 1)],
        ]));
        let backend: &dyn DbBackend = &h.backend;
        let (schema, scope) = (schema_with_user_ref(), scoped());
        let cols = || vec![("score".to_string(), DbValue::Int(1))];

        backend.insert(&schema, &scope, cols()).await.unwrap();
        assert_eq!(
            backend.get(&schema, &scope, ROW_ID).await.unwrap().version,
            1
        );
        backend
            .update(&schema, &scope, ROW_ID, 1, cols())
            .await
            .unwrap();
        backend.delete(&schema, &scope, ROW_ID, 1).await.unwrap();
        assert_eq!(
            backend
                .query(&schema, &scope, 10, 0, None)
                .await
                .unwrap()
                .len(),
            1
        );
    }

    #[test]
    fn db_value_converts_to_the_matching_bound_sea_value() {
        assert_eq!(db_value_to_sea_value(&DbValue::Null), Value::String(None));
        assert_eq!(
            db_value_to_sea_value(&DbValue::Bool(true)),
            Value::Bool(Some(true))
        );
        assert_eq!(db_value_to_sea_value(&DbValue::Int(-3)), big(-3));
        assert_eq!(
            db_value_to_sea_value(&DbValue::Float(1.5)),
            Value::Double(Some(1.5))
        );
        assert_eq!(db_value_to_sea_value(&DbValue::Text("t".into())), text("t"));
        assert_eq!(
            db_value_to_sea_value(&DbValue::Bytes(vec![1, 2])),
            Value::Bytes(Some(vec![1, 2]))
        );
    }

    #[test]
    fn every_db_error_has_a_stable_code_and_message() {
        let cases = [
            (
                DbError::NoTable,
                "no_table",
                "no table provisioned for this app",
            ),
            (
                DbError::InvalidColumn("c".into()),
                "invalid_column",
                "invalid column: c",
            ),
            (
                DbError::InvalidValue("v".into()),
                "invalid_value",
                "invalid value: v",
            ),
            (DbError::NotFound, "not_found", "row not found"),
            (DbError::Conflict, "conflict", "version conflict"),
            (
                DbError::QuotaExceeded("q".into()),
                "quota_exceeded",
                "quota exceeded: q",
            ),
            (DbError::Timeout, "timeout", "operation timed out"),
            (DbError::Backend("b".into()), "backend", "backend error: b"),
        ];
        for (err, code, message) in cases {
            assert_eq!(err.code(), code);
            assert_eq!(err.to_string(), message);
        }
    }

    fn typed_schema(sql_type: ColumnType) -> TableSchema {
        TableSchema::validated(
            AppSchema::Core,
            "typed",
            vec![ColumnDef {
                name: "c".to_string(),
                sql_type,
                nullable: true,
                is_user_ref: false,
            }],
        )
        .expect("typed schema is valid")
    }

    #[test]
    fn validate_column_value_enforces_each_declared_column_type() {
        let ok = |t, v: DbValue| validate_column_value(&typed_schema(t), "c", &v);
        let code = |t, v: DbValue| {
            validate_column_value(&typed_schema(t), "c", &v)
                .unwrap_err()
                .code()
        };

        assert!(ok(ColumnType::Uuid, DbValue::Text(Uuid::new_v4().to_string())).is_ok());
        assert_eq!(
            code(ColumnType::Uuid, DbValue::Text("nope".into())),
            "invalid_value"
        );

        assert!(ok(ColumnType::Int4, DbValue::Int(i64::from(i32::MAX))).is_ok());
        assert_eq!(
            code(ColumnType::Int4, DbValue::Int(i64::from(i32::MAX) + 1)),
            "invalid_value"
        );
        assert!(ok(ColumnType::Int8, DbValue::Int(i64::MIN)).is_ok());
        assert!(ok(ColumnType::Bool, DbValue::Bool(false)).is_ok());
        assert!(ok(
            ColumnType::Timestamptz,
            DbValue::Text("2026-01-01T00:00:00Z".into())
        )
        .is_ok());
        assert_eq!(
            code(ColumnType::Timestamptz, DbValue::Text("2026-01-01".into())),
            "invalid_value",
            "a bare date has no UTC offset"
        );
        assert_eq!(
            code(ColumnType::Timestamptz, DbValue::Text("now".into())),
            "invalid_value"
        );
        assert!(ok(ColumnType::Text, DbValue::Text("a".repeat(MAX_TEXT_BYTES))).is_ok());
        assert!(ok(ColumnType::Text, DbValue::Null).is_ok());

        assert!(ok(ColumnType::Jsonb, DbValue::Text("{\"k\":[1,2]}".into())).is_ok());
        assert_eq!(
            code(ColumnType::Jsonb, DbValue::Text("{not json".into())),
            "invalid_value"
        );
        assert_eq!(
            code(
                ColumnType::Jsonb,
                DbValue::Text(format!("\"{}\"", "a".repeat(MAX_JSONB_BYTES)))
            ),
            "quota_exceeded"
        );
    }

    #[test]
    fn validate_column_value_rejects_a_value_of_the_wrong_kind() {
        for (t, v) in [
            (ColumnType::Bool, DbValue::Int(1)),
            (ColumnType::Int8, DbValue::Text("1".into())),
            (ColumnType::Text, DbValue::Bool(true)),
            (ColumnType::Uuid, DbValue::Int(1)),
            (ColumnType::Jsonb, DbValue::Bytes(vec![1])),
        ] {
            let err = validate_column_value(&typed_schema(t), "c", &v).unwrap_err();
            assert_eq!(err.code(), "invalid_value", "{t:?} / {v:?}");
        }
    }

    #[test]
    fn validate_column_values_stops_at_the_first_bad_entry() {
        let schema = schema_with_user_ref();
        assert!(validate_column_values(&schema, &[]).is_ok());
        assert!(validate_column_values(&schema, &[("score".to_string(), DbValue::Int(1))]).is_ok());

        let err = validate_column_values(
            &schema,
            &[
                ("score".to_string(), DbValue::Int(1)),
                ("ghost".to_string(), DbValue::Int(1)),
                ("note".to_string(), DbValue::Bool(true)),
            ],
        )
        .unwrap_err();
        assert_eq!(err.code(), "invalid_column");
    }

    // Typed-column statements (regression: bundle-DB typed columns failed
    // against real Postgres because every value was bound as `text`). The
    // mock cannot reject a wrong cast, so these pin the exact SQL/bind shape;
    // `tests/postgres_integration.rs` proves it against a real server.

    const OTHER_UUID: &str = "9a1b2c3d-4e5f-4a6b-8c7d-0e1f2a3b4c5d";

    fn other_uuid() -> Uuid {
        Uuid::parse_str(OTHER_UUID).expect("OTHER_UUID is a valid UUID")
    }

    #[tokio::test]
    async fn insert_casts_only_the_typed_columns_and_binds_a_uuid_natively() {
        let h = Harness::new(
            mock_db()
                .append_exec_results(exec_ok(3))
                .append_query_results([count_row(0), insert_returning_row()]),
        );
        let scope = scoped();

        h.backend
            .insert(
                &wide_schema(),
                &scope,
                vec![
                    ("flag".to_string(), DbValue::Bool(true)),
                    ("small".to_string(), DbValue::Int(5)),
                    ("big".to_string(), DbValue::Int(9)),
                    ("note".to_string(), DbValue::Text("n".to_string())),
                    (
                        "seen_at".to_string(),
                        DbValue::Text("2026-01-02T03:04:05Z".to_string()),
                    ),
                    ("doc".to_string(), DbValue::Text("{\"a\":1}".to_string())),
                    (
                        "other_id".to_string(),
                        DbValue::Text(OTHER_UUID.to_uppercase()),
                    ),
                ],
            )
            .await
            .unwrap();

        let stmts = h.statements();
        assert_eq!(
            stmts[5].sql,
            "INSERT INTO \"app_community\".\"wide_tbl\" \
             (\"flag\", \"small\", \"big\", \"note\", \"seen_at\", \"doc\", \"other_id\", \
             \"tenant_id\", \"community_id\") \
             VALUES ($1, $2, $3, $4, $5::timestamptz, $6::jsonb, $7::uuid, $8, $9) \
             RETURNING row_id, version"
        );
        assert_eq!(
            bound(&stmts[5]),
            vec![
                Value::Bool(Some(true)),
                big(5),
                big(9),
                text("n"),
                text("2026-01-02T03:04:05Z"),
                text("{\"a\":1}"),
                Value::Uuid(Some(other_uuid())),
                text("acme"),
                text("main"),
            ]
        );
    }

    #[tokio::test]
    async fn insert_casts_a_user_ref_column_to_uuid() {
        let h = Harness::new(
            mock_db()
                .append_exec_results(exec_ok(3))
                .append_query_results([count_row(0), insert_returning_row()]),
        );

        h.backend
            .insert(
                &schema_with_user_ref(),
                &scoped(),
                vec![
                    (
                        "user_ref".to_string(),
                        DbValue::Text(OTHER_UUID.to_string()),
                    ),
                    ("score".to_string(), DbValue::Int(1)),
                ],
            )
            .await
            .unwrap();

        let stmts = h.statements();
        assert_eq!(
            stmts[5].sql,
            "INSERT INTO \"app_core\".\"fishing_core\" \
             (\"user_ref\", \"score\", \"tenant_id\", \"community_id\") \
             VALUES ($1::uuid, $2, $3, $4) RETURNING row_id, version"
        );
        assert_eq!(bound(&stmts[5])[0], Value::Uuid(Some(other_uuid())));
    }

    #[tokio::test]
    async fn insert_types_a_null_by_its_column_so_it_can_assign_into_a_non_text_column() {
        let h = Harness::new(
            mock_db()
                .append_exec_results(exec_ok(3))
                .append_query_results([count_row(0), insert_returning_row()]),
        );
        let nulls = ["flag", "small", "big", "note", "seen_at", "doc", "other_id"]
            .iter()
            .map(|c| (c.to_string(), DbValue::Null))
            .collect();

        h.backend
            .insert(&wide_schema(), &scoped(), nulls)
            .await
            .unwrap();

        let stmts = h.statements();
        assert!(stmts[5]
            .sql
            .contains("VALUES ($1, $2, $3, $4, $5::timestamptz, $6::jsonb, $7::uuid, $8, $9)"));
        assert_eq!(
            bound(&stmts[5]),
            vec![
                Value::Bool(None),
                Value::BigInt(None),
                Value::BigInt(None),
                Value::String(None),
                Value::String(None),
                Value::String(None),
                Value::Uuid(None),
                text("acme"),
                text("main"),
            ]
        );
    }

    #[tokio::test]
    async fn update_casts_typed_set_clauses_and_leaves_plain_ones_alone() {
        let h = Harness::new(
            mock_db()
                .append_exec_results(exec_ok(2))
                .append_query_results([vec![mock_row(vec![("version", big(4))])]]),
        );

        h.backend
            .update(
                &wide_schema(),
                &scoped(),
                ROW_ID,
                3,
                vec![
                    (
                        "seen_at".to_string(),
                        DbValue::Text("2026-01-02T03:04:05+02:00".to_string()),
                    ),
                    ("doc".to_string(), DbValue::Text("[1]".to_string())),
                    ("other_id".to_string(), DbValue::Null),
                    ("note".to_string(), DbValue::Text("n".to_string())),
                ],
            )
            .await
            .unwrap();

        let stmts = h.statements();
        assert_eq!(
            stmts[3].sql,
            "UPDATE \"app_community\".\"wide_tbl\" SET \"seen_at\" = $1::timestamptz, \
             \"doc\" = $2::jsonb, \"other_id\" = $3::uuid, \"note\" = $4, \
             version = version + 1, updated_at = now() \
             WHERE row_id = $5 AND version = $6 AND tenant_id = $7 AND community_id = $8 \
             RETURNING version"
        );
        assert_eq!(
            bound(&stmts[3]),
            vec![
                text("2026-01-02T03:04:05+02:00"),
                text("[1]"),
                Value::Uuid(None),
                text("n"),
                uuid_cell(),
                big(3),
                text("acme"),
                text("main"),
            ]
        );
    }

    #[tokio::test]
    async fn a_malformed_typed_value_is_rejected_before_any_io() {
        let bad: [(&str, DbValue); 4] = [
            ("other_id", DbValue::Text("nope".to_string())),
            ("seen_at", DbValue::Text("2026-01-02".to_string())),
            ("doc", DbValue::Text("{not json".to_string())),
            ("seen_at", DbValue::Int(1)),
        ];
        for (column, value) in bad {
            let h = Harness::new(mock_db());
            let err = h
                .backend
                .insert(
                    &wide_schema(),
                    &scoped(),
                    vec![(column.to_string(), value.clone())],
                )
                .await
                .unwrap_err();
            assert_eq!(err.code(), "invalid_value", "insert {column}={value:?}");
            assert!(h.statements().is_empty(), "{column} reached the database");

            let h = Harness::new(mock_db());
            let err = h
                .backend
                .update(
                    &wide_schema(),
                    &scoped(),
                    ROW_ID,
                    1,
                    vec![(column.to_string(), value.clone())],
                )
                .await
                .unwrap_err();
            assert_eq!(err.code(), "invalid_value", "update {column}={value:?}");
            assert!(h.statements().is_empty(), "{column} reached the database");
        }
    }

    #[tokio::test]
    async fn get_and_query_project_timestamptz_and_jsonb_as_text_in_sql() {
        let schema = wide_schema();
        let select = "SELECT \"row_id\", \"version\", \"flag\", \"small\", \"big\", \"note\", \
             CASE WHEN isfinite(\"seen_at\") \
             THEN to_char(\"seen_at\" AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') \
             ELSE \"seen_at\"::text END AS \"seen_at\", \
             \"doc\"::text AS \"doc\", \"other_id\" \
             FROM \"app_community\".\"wide_tbl\"";
        let all_null = || {
            wide_row(vec![
                ("flag", Value::Bool(None)),
                ("small", Value::Int(None)),
                ("big", Value::BigInt(None)),
                ("note", Value::String(None)),
                ("seen_at", Value::String(None)),
                ("doc", Value::String(None)),
                ("other_id", Value::Uuid(None)),
            ])
        };

        let h = Harness::new(
            mock_db()
                .append_exec_results(exec_ok(2))
                .append_query_results([vec![all_null()]]),
        );
        h.backend.get(&schema, &scoped(), ROW_ID).await.unwrap();
        let stmts = h.statements();
        assert_eq!(
            stmts[3].sql,
            format!("{select} WHERE row_id = $1 AND tenant_id = $2 AND community_id = $3")
        );

        let h = Harness::new(
            mock_db()
                .append_exec_results(exec_ok(2))
                .append_query_results([vec![all_null()]]),
        );
        h.backend
            .query(&schema, &scoped(), 10, 0, None)
            .await
            .unwrap();
        let stmts = h.statements();
        assert_eq!(
            stmts[3].sql,
            format!(
                "{select} WHERE tenant_id = $1 AND community_id = $2 \
                 ORDER BY row_id ASC LIMIT $3 OFFSET $4"
            )
        );
    }

    #[tokio::test]
    async fn query_orders_a_projected_column_by_its_stored_value_not_the_text_alias() {
        let h = Harness::new(
            mock_db()
                .append_exec_results(exec_ok(2))
                .append_query_results([no_rows()]),
        );
        h.backend
            .query(
                &wide_schema(),
                &scoped(),
                10,
                0,
                Some(OrderBy::Column {
                    name: "seen_at".to_string(),
                    descending: true,
                }),
            )
            .await
            .unwrap();
        let sql = &h.statements()[3].sql;
        assert!(
            sql.contains("ORDER BY \"wide_tbl\".\"seen_at\" DESC LIMIT"),
            "a bare `ORDER BY \"seen_at\"` would sort the text projection: {sql}"
        );
    }

    #[test]
    fn a_user_ref_column_reads_as_a_uuid_even_if_its_cached_type_says_otherwise() {
        let schema = TableSchema::validated(
            AppSchema::Core,
            "t",
            vec![ColumnDef {
                name: "owner".to_string(),
                sql_type: ColumnType::Text,
                nullable: true,
                is_user_ref: true,
            }],
        )
        .expect("schema is valid");
        assert_eq!(select_list(&schema), "\"row_id\", \"version\", \"owner\"");
    }

    #[tokio::test]
    async fn with_call_deadline_passes_results_and_errors_through() {
        let ok = with_call_deadline(async { Ok::<_, DbError>(7) }).await;
        assert_eq!(ok, Ok(7));

        let err = with_call_deadline(async { Err::<(), _>(DbError::Conflict) }).await;
        assert_eq!(err, Err(DbError::Conflict));
    }

    /// Virtual time: the deadline elapses instantly instead of really
    /// sleeping `CALL_DEADLINE`.
    #[tokio::test(start_paused = true)]
    async fn with_call_deadline_times_out_a_call_that_never_finishes() {
        let result = with_call_deadline(std::future::pending::<Result<(), DbError>>()).await;
        assert_eq!(result, Err(DbError::Timeout));
    }
}
