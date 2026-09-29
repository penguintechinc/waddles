//! The bundle `db` host capability -- structured insert/get/update/delete
//! against a single bundle-owned table in the shared `waddles` Postgres
//! instance (`app_core`/`app_community` schemas), shared by
//! `core/svc_process` and `core/svc_action` so the security-critical parts
//! -- identifier validation, tenant/community isolation, `user_ref`
//! enforcement, and quotas -- exist in exactly one place.
//!
//! Design doc:
//! `docs/superpowers/specs/2026-09-28-bundle-db-capability-and-schemas.md`
//! (Rev 5). hub-api's `waddles_bundle_migrator` role owns all DDL
//! (provisioning/migration/uninstall, PR #430) -- this crate is DML-only,
//! under the least-privilege `waddles_bundle_runtime` role
//! (`alembic/versions/0030_bundle_app_schemas.py`), and never issues
//! `CREATE`/`ALTER`/`DROP`.
//!
//! **Scope of this landing (first slice):** `insert`/`get`/`update`/
//! `delete` are implemented end to end (validation, RLS + explicit
//! predicate, quotas, statement timeout, per-call deadline). `query`
//! (list/filter) is not implemented here -- see this crate's PR
//! description "remaining work". [`schema::SchemaCache`] is populated
//! externally (mirrors `bundle_host_kv::authorize::CapabilitySnapshot`);
//! wiring a live hub-api-backed `bundle_loader` poll that calls
//! [`schema::SchemaCache::update`] is also out of scope here.
//!
//! **stage.wit note:** `wit/waddle-bundle/stage.wit`'s current `db`
//! interface (`execute(statement, params)`, raw parameterized SQL) is
//! superseded by this design (no bundle-supplied SQL, ever -- design doc
//! SS1 round-1 CRITICAL finding). This crate does **not** change
//! `stage.wit` or `core/bundle_executor`'s `bindgen!`-generated `db::Host`
//! trait -- doing so would require rewriting that crate's guest-facing
//! bindings/tests, a separate, larger change. This landing wires
//! `insert`/`get`/`update`/`delete` at the host-API's existing untyped
//! `{capability, op, args}` dispatch layer (`penguin_bundle_host::wire::
//! HostCallBody`), using op strings `"insert"`/`"get"`/`"update"`/
//! `"delete"` ahead of a corresponding WIT/executor change -- see the PR
//! description for the proposed WIT shape.

pub mod authorize;
pub mod backend;
mod limits;
mod metrics;
pub mod schema;
pub mod scope;

use std::time::Instant;

pub use authorize::CapabilitySnapshot;
pub use backend::{BoxFuture, DbBackend, DbError, DbValue, PostgresBackend, Row};
pub use limits::{
    MAX_JSONB_BYTES, MAX_OPS_PER_INVOKE, MAX_QUERY_LIMIT, MAX_ROWS_PER_APP, MAX_TEXT_BYTES,
};
pub use schema::{ColumnDef, ColumnType, SchemaCache, TableSchema};
pub use scope::{AppSchema, DbScope};

use authorize::authorize_db;

/// Entry point both stages call into -- resolves the app's schema,
/// authorizes the capability, and dispatches to `backend`, all under
/// [`backend::with_call_deadline`]. Construct one per service (holding an
/// `Arc<dyn DbBackend>` and `Arc<SchemaCache>`/`Arc<CapabilitySnapshot>`
/// shared across invocations, matching `bundle_host_kv::KvHost`'s own
/// "construct once, call per-invoke with a scope" usage).
pub struct DbHost<B: DbBackend> {
    backend: B,
}

impl<B: DbBackend> DbHost<B> {
    pub fn new(backend: B) -> Self {
        Self { backend }
    }

    fn resolve_and_authorize(
        &self,
        scope: &DbScope,
        schemas: &SchemaCache,
        snapshot: &CapabilitySnapshot,
    ) -> Result<std::sync::Arc<TableSchema>, DbError> {
        authorize_db(scope, snapshot)
            .map_err(|_| DbError::InvalidColumn("not_granted".to_string()))?;
        schemas.get(&scope.app_id).ok_or(DbError::NoTable)
    }

    fn finish(op: &'static str, scope: &DbScope, started: Instant, result: &Result<(), &DbError>) {
        let elapsed = started.elapsed().as_secs_f64();
        match result {
            Ok(()) => {
                metrics::record_op_duration(op, "ok", elapsed);
                metrics::record_call(op, "ok");
                tracing::info!(
                    tenant = %scope.tenant,
                    community = scope.community.as_deref().unwrap_or(""),
                    app_id = %scope.app_id,
                    op,
                    "bundle db op"
                );
            }
            Err(err) => {
                let kind = err.code();
                metrics::record_op_duration(op, "error", elapsed);
                metrics::record_call(op, kind);
                if matches!(err, DbError::QuotaExceeded(_)) {
                    metrics::record_quota_rejection(&scope.app_id, "row_count");
                }
                tracing::info!(
                    tenant = %scope.tenant,
                    community = scope.community.as_deref().unwrap_or(""),
                    app_id = %scope.app_id,
                    op,
                    error_kind = kind,
                    "bundle db op failed"
                );
            }
        }
    }

    pub async fn insert(
        &self,
        scope: &DbScope,
        schemas: &SchemaCache,
        snapshot: &CapabilitySnapshot,
        column_values: Vec<(String, DbValue)>,
    ) -> Result<Row, DbError> {
        let started = Instant::now();
        let outcome = async {
            let schema = self.resolve_and_authorize(scope, schemas, snapshot)?;
            backend::with_call_deadline(self.backend.insert(&schema, scope, column_values)).await
        }
        .await;
        Self::finish("insert", scope, started, &outcome.as_ref().map(|_| ()));
        outcome
    }

    pub async fn get(
        &self,
        scope: &DbScope,
        schemas: &SchemaCache,
        snapshot: &CapabilitySnapshot,
        row_id: &str,
    ) -> Result<Row, DbError> {
        let started = Instant::now();
        let outcome = async {
            let schema = self.resolve_and_authorize(scope, schemas, snapshot)?;
            backend::with_call_deadline(self.backend.get(&schema, scope, row_id)).await
        }
        .await;
        Self::finish("get", scope, started, &outcome.as_ref().map(|_| ()));
        if let Ok(row) = &outcome {
            metrics::record_rows("get", 1);
            let _ = row;
        }
        outcome
    }

    pub async fn update(
        &self,
        scope: &DbScope,
        schemas: &SchemaCache,
        snapshot: &CapabilitySnapshot,
        row_id: &str,
        expected_version: u64,
        column_values: Vec<(String, DbValue)>,
    ) -> Result<Row, DbError> {
        let started = Instant::now();
        let outcome = async {
            let schema = self.resolve_and_authorize(scope, schemas, snapshot)?;
            backend::with_call_deadline(self.backend.update(
                &schema,
                scope,
                row_id,
                expected_version,
                column_values,
            ))
            .await
        }
        .await;
        Self::finish("update", scope, started, &outcome.as_ref().map(|_| ()));
        outcome
    }

    pub async fn delete(
        &self,
        scope: &DbScope,
        schemas: &SchemaCache,
        snapshot: &CapabilitySnapshot,
        row_id: &str,
        expected_version: u64,
    ) -> Result<(), DbError> {
        let started = Instant::now();
        let outcome = async {
            let schema = self.resolve_and_authorize(scope, schemas, snapshot)?;
            backend::with_call_deadline(self.backend.delete(
                &schema,
                scope,
                row_id,
                expected_version,
            ))
            .await
        }
        .await;
        Self::finish("delete", scope, started, &outcome.as_ref().map(|_| ()));
        outcome
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::ColumnDef;
    use crate::scope::AppSchema;
    use std::sync::Arc;

    /// A fake backend for exercising `DbHost`'s authorize/resolve wiring
    /// without a live Postgres connection.
    struct FakeBackend {
        row: Row,
    }

    impl DbBackend for FakeBackend {
        fn insert<'a>(
            &'a self,
            _schema: &'a TableSchema,
            _scope: &'a DbScope,
            column_values: Vec<(String, DbValue)>,
        ) -> BoxFuture<'a, Result<Row, DbError>> {
            let row = Row {
                row_id: self.row.row_id.clone(),
                version: 1,
                columns: column_values,
            };
            Box::pin(async move { Ok(row) })
        }

        fn get<'a>(
            &'a self,
            _schema: &'a TableSchema,
            _scope: &'a DbScope,
            _row_id: &'a str,
        ) -> BoxFuture<'a, Result<Row, DbError>> {
            let row = self.row.clone();
            Box::pin(async move { Ok(row) })
        }

        fn update<'a>(
            &'a self,
            _schema: &'a TableSchema,
            _scope: &'a DbScope,
            _row_id: &'a str,
            _expected_version: u64,
            column_values: Vec<(String, DbValue)>,
        ) -> BoxFuture<'a, Result<Row, DbError>> {
            let row = Row {
                row_id: self.row.row_id.clone(),
                version: 2,
                columns: column_values,
            };
            Box::pin(async move { Ok(row) })
        }

        fn delete<'a>(
            &'a self,
            _schema: &'a TableSchema,
            _scope: &'a DbScope,
            _row_id: &'a str,
            _expected_version: u64,
        ) -> BoxFuture<'a, Result<(), DbError>> {
            Box::pin(async move { Ok(()) })
        }
    }

    fn sample_schema() -> TableSchema {
        TableSchema::validated(
            AppSchema::Core,
            "fishing_core",
            vec![ColumnDef {
                name: "score".to_string(),
                sql_type: ColumnType::Int8,
                nullable: true,
                is_user_ref: false,
            }],
        )
        .unwrap()
    }

    fn granted_snapshot(app_id: &str) -> CapabilitySnapshot {
        let snapshot = CapabilitySnapshot::new();
        snapshot.update(app_id, [authorize::DB_PERMISSION_ID.to_string()]);
        snapshot
    }

    #[tokio::test]
    async fn insert_denies_when_capability_not_declared() {
        let host = DbHost::new(FakeBackend {
            row: Row {
                row_id: "00000000-0000-0000-0000-000000000000".to_string(),
                version: 1,
                columns: vec![],
            },
        });
        let scope = DbScope::new("acme", None, "waddles.bot.a");
        let schemas = SchemaCache::new();
        schemas.update("waddles.bot.a", sample_schema());
        let snapshot = CapabilitySnapshot::new(); // never declared

        let err = host
            .insert(&scope, &schemas, &snapshot, vec![])
            .await
            .unwrap_err();
        assert_eq!(err.code(), "invalid_column");
    }

    #[tokio::test]
    async fn insert_denies_when_no_table_provisioned() {
        let host = DbHost::new(FakeBackend {
            row: Row {
                row_id: "00000000-0000-0000-0000-000000000000".to_string(),
                version: 1,
                columns: vec![],
            },
        });
        let scope = DbScope::new("acme", None, "waddles.bot.a");
        let schemas = SchemaCache::new(); // never provisioned
        let snapshot = granted_snapshot("waddles.bot.a");

        let err = host
            .insert(&scope, &schemas, &snapshot, vec![])
            .await
            .unwrap_err();
        assert_eq!(err, DbError::NoTable);
    }

    #[tokio::test]
    async fn insert_succeeds_when_granted_and_provisioned() {
        let host = DbHost::new(FakeBackend {
            row: Row {
                row_id: "00000000-0000-0000-0000-000000000000".to_string(),
                version: 1,
                columns: vec![],
            },
        });
        let scope = DbScope::new("acme", None, "waddles.bot.a");
        let schemas = SchemaCache::new();
        schemas.update("waddles.bot.a", sample_schema());
        let snapshot = granted_snapshot("waddles.bot.a");

        let row = host
            .insert(
                &scope,
                &schemas,
                &snapshot,
                vec![("score".to_string(), DbValue::Int(42))],
            )
            .await
            .unwrap();
        assert_eq!(row.version, 1);
    }

    #[tokio::test]
    async fn get_update_delete_all_gate_on_the_same_authorize_and_schema_checks() {
        let host = DbHost::new(FakeBackend {
            row: Row {
                row_id: "00000000-0000-0000-0000-000000000000".to_string(),
                version: 1,
                columns: vec![],
            },
        });
        let scope = DbScope::new("acme", None, "waddles.bot.unknown");
        let schemas = SchemaCache::new();
        let snapshot = CapabilitySnapshot::new();

        assert!(host
            .get(
                &scope,
                &schemas,
                &snapshot,
                "00000000-0000-0000-0000-000000000000"
            )
            .await
            .is_err());
        assert!(host
            .update(
                &scope,
                &schemas,
                &snapshot,
                "00000000-0000-0000-0000-000000000000",
                1,
                vec![]
            )
            .await
            .is_err());
        assert!(host
            .delete(
                &scope,
                &schemas,
                &snapshot,
                "00000000-0000-0000-0000-000000000000",
                1
            )
            .await
            .is_err());
    }

    #[tokio::test]
    async fn two_apps_never_share_a_schema_lookup() {
        let host = DbHost::new(FakeBackend {
            row: Row {
                row_id: "00000000-0000-0000-0000-000000000000".to_string(),
                version: 1,
                columns: vec![],
            },
        });
        let schemas = SchemaCache::new();
        schemas.update("waddles.bot.a", sample_schema());
        let snapshot = granted_snapshot("waddles.bot.b"); // different app granted

        let scope_b = DbScope::new("acme", None, "waddles.bot.b");
        let err = host
            .insert(&scope_b, &schemas, &snapshot, vec![])
            .await
            .unwrap_err();
        assert_eq!(
            err,
            DbError::NoTable,
            "app b is granted the capability but has no provisioned table"
        );
    }

    #[test]
    fn arc_schema_cache_is_shareable_across_tasks() {
        let cache = Arc::new(SchemaCache::new());
        cache.update("a", sample_schema());
        let cache2 = Arc::clone(&cache);
        assert!(cache2.get("a").is_some());
    }
}
