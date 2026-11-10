//! `db` v2: multiple manifest-declared tables per bundle, typed columns with
//! declared indexes, a structured (AST-only, no SQL) filtered/ordered/paginated
//! query, bounded atomic transactions, and per-bundle-per-community quotas.
//!
//! Strictly additive: the v1 single-table `db` capability in the parent module
//! is untouched and keeps serving existing bundles. Every v2 call is scoped
//! host-side to the invocation's authenticated `(tenant, community, app_id)`;
//! a guest names only *logical* table and column names that its approved
//! manifest declared, and can never address another community, bundle, or a
//! physical table. Design + limits: `docs/bundle-storage-v2.md`.

pub mod ast;
pub mod backend;
mod plan;
pub mod schema;
pub mod wire;

#[cfg(test)]
mod testkit;

#[cfg(test)]
#[path = "mod_tests.rs"]
mod tests;

use std::future::Future;
use std::sync::Arc;
use std::time::Instant;

pub use ast::{
    CmpOp, Condition, DbV2Error, Order, QueryPage, QueryRequest, RowV2, TxOp, TxOutcome,
};
pub use backend::PostgresV2Backend;
pub use schema::{
    load_registry, BundleSchemaV2, IndexColumn, IndexDef, SchemaRegistryV2, TableDefV2,
};

use crate::authorize::{authorize_db, CapabilitySnapshot};
use crate::backend::DbValue;
use crate::metrics;
use crate::scope::DbScope;

/// Entry point both stages call into: authorizes, resolves the declared
/// schema, runs the op under the call deadline, and records metrics.
pub struct DbV2Host {
    backend: PostgresV2Backend,
}

async fn with_deadline<T>(fut: impl Future<Output = Result<T, DbV2Error>>) -> Result<T, DbV2Error> {
    match tokio::time::timeout(crate::limits::CALL_DEADLINE, fut).await {
        Ok(r) => r,
        Err(_) => Err(DbV2Error::Timeout),
    }
}

impl DbV2Host {
    pub fn new(backend: PostgresV2Backend) -> Self {
        Self { backend }
    }

    fn resolve(
        scope: &DbScope,
        registry: &SchemaRegistryV2,
        caps: &CapabilitySnapshot,
    ) -> Result<Arc<BundleSchemaV2>, DbV2Error> {
        authorize_db(scope, caps).map_err(|d| DbV2Error::Denied(d.message))?;
        registry.get(&scope.app_id).ok_or_else(|| {
            DbV2Error::InvalidTable("this bundle has no db v2 tables provisioned".into())
        })
    }

    fn finish<T>(op: &'static str, scope: &DbScope, started: Instant, r: &Result<T, DbV2Error>) {
        let secs = started.elapsed().as_secs_f64();
        match r {
            Ok(_) => {
                metrics::record_op_duration(op, "ok", secs);
                metrics::record_call(op, "ok");
                tracing::debug!(
                    tenant_id = scope.tenant_id,
                    community_id = scope.community_id,
                    app_id = %scope.app_id,
                    op,
                    "bundle db v2 op"
                );
            }
            Err(e) => {
                metrics::record_op_duration(op, "error", secs);
                metrics::record_call(op, e.code());
                if matches!(e, DbV2Error::QuotaExceeded(_)) {
                    metrics::record_quota_rejection(&scope.app_id, "v2_quota");
                }
                if matches!(e, DbV2Error::Backend(_) | DbV2Error::Timeout) {
                    tracing::error!(
                        tenant_id = scope.tenant_id,
                        community_id = scope.community_id,
                        app_id = %scope.app_id,
                        op,
                        error_kind = e.code(),
                        "bundle db v2 op failed: backend/timeout -- db unavailable or misconfigured"
                    );
                } else {
                    tracing::info!(
                        tenant_id = scope.tenant_id,
                        community_id = scope.community_id,
                        app_id = %scope.app_id,
                        op,
                        error_kind = e.code(),
                        "bundle db v2 op failed"
                    );
                }
            }
        }
    }

    /// Inserts one row (a one-op atomic batch).
    pub async fn insert(
        &self,
        scope: &DbScope,
        registry: &SchemaRegistryV2,
        caps: &CapabilitySnapshot,
        table: &str,
        columns: Vec<(String, DbValue)>,
    ) -> Result<RowV2, DbV2Error> {
        let started = Instant::now();
        let r = self
            .single(
                scope,
                registry,
                caps,
                TxOp::Insert {
                    table: table.to_string(),
                    columns,
                },
            )
            .await;
        Self::finish("v2.insert", scope, started, &r);
        r
    }

    /// Updates one row, gated on `expected_version`.
    // Each argument is a distinct, independently-sourced input (scope, registry,
    // grants, and the guest's op); bundling them would only add a throwaway struct.
    #[allow(clippy::too_many_arguments)]
    pub async fn update(
        &self,
        scope: &DbScope,
        registry: &SchemaRegistryV2,
        caps: &CapabilitySnapshot,
        table: &str,
        row_id: &str,
        expected_version: u64,
        columns: Vec<(String, DbValue)>,
    ) -> Result<RowV2, DbV2Error> {
        let started = Instant::now();
        let r = self
            .single(
                scope,
                registry,
                caps,
                TxOp::Update {
                    table: table.to_string(),
                    row_id: row_id.to_string(),
                    expected_version,
                    columns,
                },
            )
            .await;
        Self::finish("v2.update", scope, started, &r);
        r
    }

    async fn single(
        &self,
        scope: &DbScope,
        registry: &SchemaRegistryV2,
        caps: &CapabilitySnapshot,
        op: TxOp,
    ) -> Result<RowV2, DbV2Error> {
        let schema = Self::resolve(scope, registry, caps)?;
        let mut out = with_deadline(self.backend.transact(scope, &schema, &[op])).await?;
        match out.pop() {
            Some(TxOutcome::Row(row)) => Ok(row),
            _ => Err(DbV2Error::Backend("write returned no row".into())),
        }
    }

    /// Deletes one row, gated on `expected_version`.
    pub async fn delete(
        &self,
        scope: &DbScope,
        registry: &SchemaRegistryV2,
        caps: &CapabilitySnapshot,
        table: &str,
        row_id: &str,
        expected_version: u64,
    ) -> Result<(), DbV2Error> {
        let started = Instant::now();
        let r = async {
            let schema = Self::resolve(scope, registry, caps)?;
            let ops = [TxOp::Delete {
                table: table.to_string(),
                row_id: row_id.to_string(),
                expected_version,
            }];
            with_deadline(self.backend.transact(scope, &schema, &ops))
                .await
                .map(|_| ())
        }
        .await;
        Self::finish("v2.delete", scope, started, &r);
        r
    }

    /// Fetches one row by id.
    pub async fn get(
        &self,
        scope: &DbScope,
        registry: &SchemaRegistryV2,
        caps: &CapabilitySnapshot,
        table: &str,
        row_id: &str,
    ) -> Result<RowV2, DbV2Error> {
        let started = Instant::now();
        let r = async {
            let schema = Self::resolve(scope, registry, caps)?;
            let def = schema.table(table).ok_or_else(|| {
                DbV2Error::InvalidTable(format!("{table:?} is not a table this bundle declared"))
            })?;
            with_deadline(self.backend.get(scope, def, row_id)).await
        }
        .await;
        Self::finish("v2.get", scope, started, &r);
        r
    }

    /// Runs a filtered, ordered, paginated query.
    pub async fn query(
        &self,
        scope: &DbScope,
        registry: &SchemaRegistryV2,
        caps: &CapabilitySnapshot,
        req: &QueryRequest,
    ) -> Result<QueryPage, DbV2Error> {
        let started = Instant::now();
        let r = async {
            let schema = Self::resolve(scope, registry, caps)?;
            let def = schema.table(&req.table).ok_or_else(|| {
                DbV2Error::InvalidTable(format!(
                    "{:?} is not a table this bundle declared",
                    req.table
                ))
            })?;
            with_deadline(self.backend.query(scope, def, req)).await
        }
        .await;
        if let Ok(page) = &r {
            metrics::record_rows("v2.query", page.rows.len() as u64);
        }
        Self::finish("v2.query", scope, started, &r);
        r
    }

    /// Counts rows matching a filter.
    pub async fn count(
        &self,
        scope: &DbScope,
        registry: &SchemaRegistryV2,
        caps: &CapabilitySnapshot,
        table: &str,
        filter: &[Condition],
    ) -> Result<u64, DbV2Error> {
        let started = Instant::now();
        let r = async {
            let schema = Self::resolve(scope, registry, caps)?;
            let def = schema.table(table).ok_or_else(|| {
                DbV2Error::InvalidTable(format!("{table:?} is not a table this bundle declared"))
            })?;
            with_deadline(self.backend.count(scope, def, filter)).await
        }
        .await;
        Self::finish("v2.count", scope, started, &r);
        r
    }

    /// Runs a bounded atomic batch.
    pub async fn transact(
        &self,
        scope: &DbScope,
        registry: &SchemaRegistryV2,
        caps: &CapabilitySnapshot,
        ops: &[TxOp],
    ) -> Result<Vec<TxOutcome>, DbV2Error> {
        let started = Instant::now();
        let r = async {
            let schema = Self::resolve(scope, registry, caps)?;
            with_deadline(self.backend.transact(scope, &schema, ops)).await
        }
        .await;
        Self::finish("v2.transact", scope, started, &r);
        r
    }
}
