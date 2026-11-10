//! The real `db` v2 backend over the shared Postgres, as `waddles_bundle_runtime`.
//!
//! Isolation (same two independent layers as v1): RLS via `SET LOCAL` GUCs set by
//! the provisioned policy, PLUS an explicit `tenant_id = $1 AND community_id = $2`
//! predicate bound from the authenticated [`DbScope`] on every statement. The
//! guest supplies neither, and never a physical table name.
//!
//! **Transaction semantics (documented contract).** `transact` runs one batch in
//! one Postgres transaction at READ COMMITTED. Every write path (single ops are
//! one-op batches) first takes a per-`(tenant, community, bundle)` advisory
//! transaction lock, so writers within a scope are *serialized*: a
//! `guard-count` observes every previously committed write and no concurrent
//! write can slip in before commit (predicate-safe, like SERIALIZABLE, without
//! its retry storms). Rows touched by `update`/`guard-version` are additionally
//! locked `FOR UPDATE`. Readers never block. The lock is always acquired first
//! and rows second, so batches cannot deadlock each other. Any failing op rolls
//! back the whole batch; the error names the op index. A batch is bounded by
//! [`MAX_TX_OPS`] statements, the per-statement `statement_timeout`, and the
//! whole-call deadline.
//!
//! **Quotas** are maintained transactionally in `bundle_data.usage` (one row per
//! `(app_id, tenant, community)`, aggregated across all the bundle's tables),
//! updated in the SAME transaction as the write with a conditional upsert -- a
//! write that would exceed the row or byte cap affects zero rows and the batch
//! rolls back with `quota-exceeded`.

use sea_orm::error::RuntimeErr;
use sea_orm::{
    ConnectionTrait, DatabaseConnection, DbBackend as SeaDbBackend, DbErr, QueryResult, Statement,
    TransactionTrait, Value,
};
use uuid::Uuid;

use super::ast::{Condition, DbV2Error, QueryPage, QueryRequest, RowV2, TxOp, TxOutcome};
use super::plan;
use super::schema::{BundleSchemaV2, TableDefV2};
use crate::backend::{
    extract_value, lock_quota_scope, plan_writes, read_version, set_local_scope,
    validate_column_values, DbValue,
};
use crate::limits::{MAX_BYTES_PER_BUNDLE_COMMUNITY, MAX_ROWS_PER_BUNDLE_COMMUNITY, MAX_TX_OPS};
use crate::scope::{quote_ident, DbScope};
use crate::typed;

/// Maps a Postgres error to the v2 error surface by SQLSTATE.
fn map_db_err(err: DbErr) -> DbV2Error {
    let sqlstate = match &err {
        DbErr::Exec(RuntimeErr::SqlxError(e)) | DbErr::Query(RuntimeErr::SqlxError(e)) => {
            match e.as_ref() {
                sea_orm::sqlx::Error::Database(db) => db.code().map(|c| c.into_owned()),
                _ => None,
            }
        }
        _ => None,
    };
    match sqlstate.as_deref() {
        Some("23502") => DbV2Error::InvalidValue(
            "a required (NOT NULL, no default) column is missing or NULL".to_string(),
        ),
        Some("23505") => DbV2Error::Conflict("unique constraint violated".to_string()),
        Some("40001" | "40P01") => {
            DbV2Error::Conflict("serialization failure; retry the call".to_string())
        }
        Some("57014") => DbV2Error::Timeout,
        Some(code) if code.starts_with("22") => DbV2Error::InvalidValue(format!(
            "value rejected by the database as malformed or out of range (sqlstate {code})"
        )),
        _ => DbV2Error::Backend(err.to_string()),
    }
}

fn be(e: impl std::fmt::Display) -> DbV2Error {
    DbV2Error::Backend(e.to_string())
}

fn stmt(sql: String, values: Vec<Value>) -> Statement {
    Statement::from_sql_and_values(SeaDbBackend::Postgres, sql, values)
}

/// `SELECT` list for v2 rows: `row_id`, `version`, RFC 3339 `created_at`/
/// `updated_at`, then each declared column in its read form.
fn select_list(def: &TableDefV2) -> String {
    let ts = |c: &str| {
        format!(
            "to_char({} AT TIME ZONE 'UTC', '{}') AS {}",
            quote_ident(c),
            typed::TIMESTAMPTZ_OUT_FORMAT,
            quote_ident(c)
        )
    };
    let mut exprs = vec![
        quote_ident("row_id"),
        quote_ident("version"),
        ts("created_at"),
        ts("updated_at"),
    ];
    exprs.extend(def.table.columns.iter().map(typed::select_expr));
    exprs.join(", ")
}

fn row_from(def: &TableDefV2, qr: &QueryResult) -> Result<RowV2, DbV2Error> {
    let row_id: Uuid = qr.try_get("", "row_id").map_err(be)?;
    let mut columns = Vec::with_capacity(def.table.columns.len());
    for col in &def.table.columns {
        columns.push((col.name.clone(), extract_value(qr, col)?));
    }
    Ok(RowV2 {
        table: def.logical.clone(),
        row_id: row_id.to_string(),
        version: read_version(qr)?,
        created_at: qr.try_get("", "created_at").map_err(be)?,
        updated_at: qr.try_get("", "updated_at").map_err(be)?,
        columns,
    })
}

fn parse_row_id(row_id: &str) -> Result<Uuid, DbV2Error> {
    Uuid::parse_str(row_id)
        .map_err(|_| DbV2Error::InvalidValue("row_id is not a valid UUID".into()))
}

fn version_i64(v: u64) -> Result<i64, DbV2Error> {
    i64::try_from(v).map_err(|_| DbV2Error::InvalidValue("expected_version out of range".into()))
}

/// A `transact` op resolved against the schema BEFORE the transaction opens, so
/// a malformed batch costs no round trip and holds no lock.
enum Planned<'a> {
    Insert {
        def: &'a TableDefV2,
        writes: Vec<crate::backend::WriteColumn>,
    },
    Update {
        def: &'a TableDefV2,
        row_id: Uuid,
        expected: u64,
        writes: Vec<crate::backend::WriteColumn>,
    },
    Delete {
        def: &'a TableDefV2,
        row_id: Uuid,
        expected: u64,
    },
    GuardVersion {
        def: &'a TableDefV2,
        row_id: Uuid,
        expected: u64,
    },
    GuardCount {
        def: &'a TableDefV2,
        filter: &'a [Condition],
        min: u64,
        max: u64,
    },
}

fn table<'a>(schema: &'a BundleSchemaV2, logical: &str) -> Result<&'a TableDefV2, DbV2Error> {
    schema.table(logical).map(|t| t.as_ref()).ok_or_else(|| {
        DbV2Error::InvalidTable(format!("{logical:?} is not a table this bundle declared"))
    })
}

fn plan_op<'a>(schema: &'a BundleSchemaV2, op: &'a TxOp) -> Result<Planned<'a>, DbV2Error> {
    Ok(match op {
        TxOp::Insert { table: t, columns } => {
            let def = table(schema, t)?;
            validate_column_values(&def.table, columns)?;
            Planned::Insert {
                def,
                writes: plan_writes(&def.table, columns)?,
            }
        }
        TxOp::Update {
            table: t,
            row_id,
            expected_version,
            columns,
        } => {
            let def = table(schema, t)?;
            if columns.is_empty() {
                return Err(DbV2Error::InvalidValue(
                    "update requires at least one column value".into(),
                ));
            }
            validate_column_values(&def.table, columns)?;
            Planned::Update {
                def,
                row_id: parse_row_id(row_id)?,
                expected: *expected_version,
                writes: plan_writes(&def.table, columns)?,
            }
        }
        TxOp::Delete {
            table: t,
            row_id,
            expected_version,
        } => Planned::Delete {
            def: table(schema, t)?,
            row_id: parse_row_id(row_id)?,
            expected: *expected_version,
        },
        TxOp::GuardVersion {
            table: t,
            row_id,
            expected_version,
        } => Planned::GuardVersion {
            def: table(schema, t)?,
            row_id: parse_row_id(row_id)?,
            expected: *expected_version,
        },
        TxOp::GuardCount {
            table: t,
            filter,
            min,
            max,
        } => {
            let def = table(schema, t)?;
            if min > max {
                return Err(DbV2Error::InvalidQuery("guard-count min > max".into()));
            }
            check_count_filter(def, filter)?;
            Planned::GuardCount {
                def,
                filter,
                min: *min,
                max: *max,
            }
        }
    })
}

/// A counted filter must be index-supported (or empty: counts the whole scope,
/// which the row quota bounds).
fn check_count_filter(def: &TableDefV2, filter: &[Condition]) -> Result<(), DbV2Error> {
    if filter.is_empty() {
        return Ok(());
    }
    plan::check_indexed(def, filter, "")
}

/// The Postgres-backed `db` v2 engine.
pub struct PostgresV2Backend {
    conn: DatabaseConnection,
    max_rows: i64,
    max_bytes: i64,
}

impl PostgresV2Backend {
    /// Wraps a connection opened as `waddles_bundle_runtime`.
    pub fn new(conn: DatabaseConnection) -> Self {
        Self {
            conn,
            max_rows: MAX_ROWS_PER_BUNDLE_COMMUNITY,
            max_bytes: MAX_BYTES_PER_BUNDLE_COMMUNITY,
        }
    }

    /// Test-only: overrides the quota caps so tests can prove the boundary.
    #[cfg(any(test, feature = "test-util"))]
    pub fn with_quota_for_test(mut self, max_rows: i64, max_bytes: i64) -> Self {
        self.max_rows = max_rows;
        self.max_bytes = max_bytes;
        self
    }

    /// Fetches one row by id (scoped).
    pub async fn get(
        &self,
        scope: &DbScope,
        def: &TableDefV2,
        row_id: &str,
    ) -> Result<RowV2, DbV2Error> {
        let id = parse_row_id(row_id)?;
        let txn = self.conn.begin().await.map_err(be)?;
        set_local_scope(&txn, scope).await?;
        let sql = format!(
            "SELECT {} FROM {} WHERE row_id = $1 AND tenant_id = $2 AND community_id = $3",
            select_list(def),
            def.table.qualified_name()
        );
        let found = txn
            .query_one_raw(stmt(
                sql,
                vec![
                    Value::Uuid(Some(id)),
                    Value::Int(Some(scope.tenant_id)),
                    Value::Int(Some(scope.community_id)),
                ],
            ))
            .await
            .map_err(map_db_err)?;
        txn.commit().await.map_err(be)?;
        row_from(def, &found.ok_or(DbV2Error::NotFound)?)
    }

    /// Runs a filtered, ordered, cursor-paginated query (scoped, index-gated).
    pub async fn query(
        &self,
        scope: &DbScope,
        def: &TableDefV2,
        req: &QueryRequest,
    ) -> Result<QueryPage, DbV2Error> {
        let planned = plan::plan_query(
            def,
            req,
            &select_list(def),
            scope.tenant_id,
            scope.community_id,
        )?;
        let txn = self.conn.begin().await.map_err(be)?;
        set_local_scope(&txn, scope).await?;
        let rows = txn
            .query_all_raw(stmt(planned.sql.clone(), planned.values.clone()))
            .await
            .map_err(map_db_err)?;
        txn.commit().await.map_err(be)?;
        let mut out = rows
            .iter()
            .map(|r| row_from(def, r))
            .collect::<Result<Vec<_>, _>>()?;
        let limit = planned.limit as usize;
        let next_cursor = if out.len() > limit {
            out.truncate(limit);
            out.last().map(|last| {
                let name = planned.order.column.name.as_str();
                let value = match name {
                    "row_id" => None,
                    "version" => i64::try_from(last.version).ok().map(DbValue::Int),
                    "created_at" => Some(DbValue::Text(last.created_at.clone())),
                    "updated_at" => Some(DbValue::Text(last.updated_at.clone())),
                    other => last
                        .columns
                        .iter()
                        .find(|(c, _)| c == other)
                        .map(|(_, v)| v.clone()),
                };
                plan::encode_cursor(&planned.order, value.as_ref(), &last.row_id)
            })
        } else {
            None
        };
        Ok(QueryPage {
            rows: out,
            next_cursor,
        })
    }

    /// Counts rows matching an index-supported filter (scoped).
    pub async fn count(
        &self,
        scope: &DbScope,
        def: &TableDefV2,
        filter: &[Condition],
    ) -> Result<u64, DbV2Error> {
        check_count_filter(def, filter)?;
        let (filter_sql, filter_values) = plan::plan_filter(def, filter, 3)?;
        let mut values = vec![
            Value::Int(Some(scope.tenant_id)),
            Value::Int(Some(scope.community_id)),
        ];
        values.extend(filter_values);
        let sql = format!(
            "SELECT COUNT(*)::bigint AS n FROM {} WHERE tenant_id = $1 AND community_id = $2 AND {filter_sql}",
            def.table.qualified_name()
        );
        let txn = self.conn.begin().await.map_err(be)?;
        set_local_scope(&txn, scope).await?;
        let row = txn
            .query_one_raw(stmt(sql, values))
            .await
            .map_err(map_db_err)?
            .ok_or_else(|| be("COUNT(*) returned no row"))?;
        txn.commit().await.map_err(be)?;
        let n: i64 = row.try_get("", "n").map_err(be)?;
        u64::try_from(n).map_err(|_| be("negative count"))
    }

    /// Runs a bounded batch atomically (see the module doc for the contract).
    pub async fn transact(
        &self,
        scope: &DbScope,
        schema: &BundleSchemaV2,
        ops: &[TxOp],
    ) -> Result<Vec<TxOutcome>, DbV2Error> {
        if ops.is_empty() || ops.len() > MAX_TX_OPS {
            return Err(DbV2Error::InvalidQuery(format!(
                "a transaction must hold 1..={MAX_TX_OPS} ops, got {}",
                ops.len()
            )));
        }
        let multi = ops.len() > 1;
        let planned = ops
            .iter()
            .enumerate()
            .map(|(i, op)| plan_op(schema, op).map_err(|e| annotate(multi, i, e)))
            .collect::<Result<Vec<_>, _>>()?;

        let txn = self.conn.begin().await.map_err(be)?;
        set_local_scope(&txn, scope).await?;
        lock_quota_scope(&txn, scope).await?;

        let mut outcomes = Vec::with_capacity(planned.len());
        let (mut rows_delta, mut bytes_delta) = (0i64, 0i64);
        for (i, p) in planned.into_iter().enumerate() {
            let step = exec_op(&txn, scope, p)
                .await
                .map_err(|e| annotate(multi, i, e))?;
            rows_delta += step.rows;
            bytes_delta += step.bytes;
            outcomes.push(step.outcome);
        }
        self.apply_usage(&txn, scope, rows_delta, bytes_delta)
            .await?;
        txn.commit().await.map_err(be)?;
        Ok(outcomes)
    }

    /// Conditional usage upsert: affects zero rows (-> quota error) when the
    /// write would push the bundle past its row or byte cap in this community.
    async fn apply_usage(
        &self,
        txn: &impl ConnectionTrait,
        scope: &DbScope,
        rows: i64,
        bytes: i64,
    ) -> Result<(), DbV2Error> {
        if rows == 0 && bytes == 0 {
            return Ok(());
        }
        if rows > self.max_rows || bytes > self.max_bytes {
            return Err(DbV2Error::QuotaExceeded(
                "write exceeds the bundle's storage quota".into(),
            ));
        }
        let sql = "INSERT INTO bundle_data.usage AS u (app_id, tenant_id, community_id, row_count, byte_count) \
                   VALUES ($1, $2, $3, GREATEST($4::bigint, 0), GREATEST($5::bigint, 0)) \
                   ON CONFLICT (app_id, tenant_id, community_id) DO UPDATE SET \
                     row_count = GREATEST(u.row_count + $4::bigint, 0), \
                     byte_count = GREATEST(u.byte_count + $5::bigint, 0) \
                   WHERE ($4::bigint <= 0 OR u.row_count + $4::bigint <= $6::bigint) \
                     AND ($5::bigint <= 0 OR u.byte_count + $5::bigint <= $7::bigint) \
                   RETURNING u.row_count";
        let applied = txn
            .query_one_raw(stmt(
                sql.to_string(),
                vec![
                    Value::String(Some(scope.app_id.clone())),
                    Value::Int(Some(scope.tenant_id)),
                    Value::Int(Some(scope.community_id)),
                    Value::BigInt(Some(rows)),
                    Value::BigInt(Some(bytes)),
                    Value::BigInt(Some(self.max_rows)),
                    Value::BigInt(Some(self.max_bytes)),
                ],
            ))
            .await
            .map_err(map_db_err)?;
        if applied.is_none() {
            return Err(DbV2Error::QuotaExceeded(format!(
                "bundle storage quota reached for this community (max {} rows / {} bytes)",
                self.max_rows, self.max_bytes
            )));
        }
        Ok(())
    }
}

/// Names the failing op in a multi-op batch (a single-op call keeps its plain
/// error so `update`/`delete` still surface `not-found`).
fn annotate(multi: bool, i: usize, e: DbV2Error) -> DbV2Error {
    if !multi {
        return e;
    }
    let tag = |m: String| format!("op {i}: {m}");
    match e {
        DbV2Error::InvalidTable(m) => DbV2Error::InvalidTable(tag(m)),
        DbV2Error::InvalidColumn(m) => DbV2Error::InvalidColumn(tag(m)),
        DbV2Error::InvalidValue(m) => DbV2Error::InvalidValue(tag(m)),
        DbV2Error::InvalidQuery(m) => DbV2Error::InvalidQuery(tag(m)),
        DbV2Error::Conflict(m) => DbV2Error::Conflict(tag(m)),
        DbV2Error::QuotaExceeded(m) => DbV2Error::QuotaExceeded(tag(m)),
        DbV2Error::NotFound => DbV2Error::Conflict(tag("row not found".into())),
        other => other,
    }
}

struct Step {
    outcome: TxOutcome,
    rows: i64,
    bytes: i64,
}

const SZ: &str = "pg_column_size(\"_row\".*)::bigint AS \"_sz\"";

fn scope_values(scope: &DbScope) -> Vec<Value> {
    vec![
        Value::Int(Some(scope.tenant_id)),
        Value::Int(Some(scope.community_id)),
    ]
}

async fn exec_op(
    txn: &impl ConnectionTrait,
    scope: &DbScope,
    op: Planned<'_>,
) -> Result<Step, DbV2Error> {
    match op {
        Planned::Insert { def, writes } => {
            let mut cols: Vec<String> = writes.iter().map(|w| w.quoted.clone()).collect();
            cols.push(quote_ident("tenant_id"));
            cols.push(quote_ident("community_id"));
            let mut phs: Vec<String> = writes
                .iter()
                .enumerate()
                .map(|(i, w)| w.placeholder(i + 1))
                .collect();
            let mut values: Vec<Value> = writes.into_iter().map(|w| w.value).collect();
            values.extend(scope_values(scope));
            phs.push(format!("${}", values.len() - 1));
            phs.push(format!("${}", values.len()));
            let sql = format!(
                "INSERT INTO {} AS \"_row\" ({}) VALUES ({}) RETURNING {}, {SZ}",
                def.table.qualified_name(),
                cols.join(", "),
                phs.join(", "),
                select_list(def)
            );
            let qr = txn
                .query_one_raw(stmt(sql, values))
                .await
                .map_err(map_db_err)?
                .ok_or_else(|| be("INSERT ... RETURNING produced no row"))?;
            let size: i64 = qr.try_get("", "_sz").map_err(be)?;
            Ok(Step {
                outcome: TxOutcome::Row(row_from(def, &qr)?),
                rows: 1,
                bytes: size,
            })
        }
        Planned::Update {
            def,
            row_id,
            expected,
            writes,
        } => {
            let lock_sql = format!(
                "SELECT version, pg_column_size(\"_row\".*)::bigint AS \"_sz\" FROM {} AS \"_row\" \
                 WHERE row_id = $1 AND tenant_id = $2 AND community_id = $3 FOR UPDATE",
                def.table.qualified_name()
            );
            let mut lock_vals = vec![Value::Uuid(Some(row_id))];
            lock_vals.extend(scope_values(scope));
            let locked = txn
                .query_one_raw(stmt(lock_sql, lock_vals))
                .await
                .map_err(map_db_err)?
                .ok_or(DbV2Error::NotFound)?;
            if read_version(&locked)? != expected {
                return Err(DbV2Error::Conflict("version mismatch".into()));
            }
            let old_size: i64 = locked.try_get("", "_sz").map_err(be)?;

            let sets: Vec<String> = writes
                .iter()
                .enumerate()
                .map(|(i, w)| format!("{} = {}", w.quoted, w.placeholder(i + 1)))
                .collect();
            let mut values: Vec<Value> = writes.into_iter().map(|w| w.value).collect();
            let base = values.len();
            values.push(Value::Uuid(Some(row_id)));
            values.extend(scope_values(scope));
            let sql = format!(
                "UPDATE {} AS \"_row\" SET {}, version = \"_row\".version + 1, updated_at = now() \
                 WHERE row_id = ${} AND tenant_id = ${} AND community_id = ${} \
                 RETURNING {}, {SZ}",
                def.table.qualified_name(),
                sets.join(", "),
                base + 1,
                base + 2,
                base + 3,
                select_list(def)
            );
            let qr = txn
                .query_one_raw(stmt(sql, values))
                .await
                .map_err(map_db_err)?
                .ok_or_else(|| be("UPDATE ... RETURNING produced no row after row lock"))?;
            let new_size: i64 = qr.try_get("", "_sz").map_err(be)?;
            Ok(Step {
                outcome: TxOutcome::Row(row_from(def, &qr)?),
                rows: 0,
                bytes: new_size - old_size,
            })
        }
        Planned::Delete {
            def,
            row_id,
            expected,
        } => {
            let sql = format!(
                "DELETE FROM {} AS \"_row\" WHERE row_id = $1 AND version = $2 \
                 AND tenant_id = $3 AND community_id = $4 RETURNING {SZ}",
                def.table.qualified_name()
            );
            let mut values = vec![
                Value::Uuid(Some(row_id)),
                Value::BigInt(Some(version_i64(expected)?)),
            ];
            values.extend(scope_values(scope));
            match txn
                .query_one_raw(stmt(sql, values))
                .await
                .map_err(map_db_err)?
            {
                Some(qr) => {
                    let size: i64 = qr.try_get("", "_sz").map_err(be)?;
                    Ok(Step {
                        outcome: TxOutcome::Done,
                        rows: -1,
                        bytes: -size,
                    })
                }
                None => {
                    let exists_sql = format!(
                        "SELECT 1 AS present FROM {} WHERE row_id = $1 AND tenant_id = $2 AND community_id = $3",
                        def.table.qualified_name()
                    );
                    let mut v = vec![Value::Uuid(Some(row_id))];
                    v.extend(scope_values(scope));
                    let exists = txn
                        .query_one_raw(stmt(exists_sql, v))
                        .await
                        .map_err(map_db_err)?;
                    Err(if exists.is_some() {
                        DbV2Error::Conflict("version mismatch".into())
                    } else {
                        DbV2Error::NotFound
                    })
                }
            }
        }
        Planned::GuardVersion {
            def,
            row_id,
            expected,
        } => {
            let sql = format!(
                "SELECT version FROM {} WHERE row_id = $1 AND tenant_id = $2 AND community_id = $3 FOR UPDATE",
                def.table.qualified_name()
            );
            let mut values = vec![Value::Uuid(Some(row_id))];
            values.extend(scope_values(scope));
            let qr = txn
                .query_one_raw(stmt(sql, values))
                .await
                .map_err(map_db_err)?
                .ok_or(DbV2Error::NotFound)?;
            if read_version(&qr)? != expected {
                return Err(DbV2Error::Conflict("guard-version mismatch".into()));
            }
            Ok(Step {
                outcome: TxOutcome::Done,
                rows: 0,
                bytes: 0,
            })
        }
        Planned::GuardCount {
            def,
            filter,
            min,
            max,
        } => {
            let (filter_sql, filter_values) = plan::plan_filter(def, filter, 3)?;
            let mut values = scope_values(scope);
            values.extend(filter_values);
            let sql = format!(
                "SELECT COUNT(*)::bigint AS n FROM {} WHERE tenant_id = $1 AND community_id = $2 AND {filter_sql}",
                def.table.qualified_name()
            );
            let qr = txn
                .query_one_raw(stmt(sql, values))
                .await
                .map_err(map_db_err)?
                .ok_or_else(|| be("COUNT(*) returned no row"))?;
            let n: i64 = qr.try_get("", "n").map_err(be)?;
            let n = u64::try_from(n).map_err(|_| be("negative count"))?;
            if n < min || n > max {
                return Err(DbV2Error::Conflict(format!(
                    "guard-count {n} outside [{min}, {max}]"
                )));
            }
            Ok(Step {
                outcome: TxOutcome::Done,
                rows: 0,
                bytes: 0,
            })
        }
    }
}
