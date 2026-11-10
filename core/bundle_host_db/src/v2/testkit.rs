//! Docker-free fixtures shared by the v2 unit tests: a declared `events`
//! table, a granted capability snapshot, and scripted `MockDatabase` rows.
//! `tests/postgres_v2_integration.rs` still proves the same flows against a
//! real server (roles, RLS, advisory locks); these cover the branch logic.

use std::collections::BTreeMap;

use sea_orm::{DatabaseConnection, DbBackend as SeaDbBackend, MockDatabase, MockExecResult, Value};
use uuid::Uuid;

use super::schema::{BundleSchemaV2, IndexColumn, IndexDef, SchemaRegistryV2, TableDefV2};
use crate::authorize::{CapabilitySnapshot, DB_PERMISSION_ID};
use crate::schema::{ColumnDef, ColumnType, TableSchema};
use crate::scope::{AppSchema, DbScope};

pub(crate) type MockRow = BTreeMap<String, Value>;

pub(crate) const APP_ID: &str = "waddles.bot.a";
pub(crate) const ROW_ID: &str = "11111111-1111-4111-8111-111111111111";
pub(crate) const OTHER_ROW_ID: &str = "22222222-2222-4222-8222-222222222222";

pub(crate) fn scope() -> DbScope {
    DbScope::new(7, 3, APP_ID)
}

/// `events(title text NOT NULL, start_at timestamptz NOT NULL, score int8)`,
/// indexed on `start_at` and (prefix-capable) `title`.
pub(crate) fn events_def() -> TableDefV2 {
    let cols = vec![
        ColumnDef {
            name: "title".into(),
            sql_type: ColumnType::Text,
            nullable: false,
            is_user_ref: false,
        },
        ColumnDef {
            name: "start_at".into(),
            sql_type: ColumnType::Timestamptz,
            nullable: false,
            is_user_ref: false,
        },
        ColumnDef {
            name: "score".into(),
            sql_type: ColumnType::Int8,
            nullable: true,
            is_user_ref: false,
        },
    ];
    let table = TableSchema::validated(AppSchema::Community, "a__events", cols)
        .expect("fixture table is valid");
    let idx = |name: &str, prefix_ops: bool| IndexDef {
        columns: vec![IndexColumn {
            name: name.into(),
            descending: false,
            prefix_ops,
        }],
        unique: false,
    };
    TableDefV2::validated(
        "events",
        table,
        vec![idx("start_at", false), idx("title", true)],
    )
    .expect("fixture def is valid")
}

pub(crate) fn schema() -> BundleSchemaV2 {
    BundleSchemaV2::validated(APP_ID, vec![events_def()]).expect("fixture schema is valid")
}

pub(crate) fn registry() -> SchemaRegistryV2 {
    let r = SchemaRegistryV2::new();
    r.update(schema());
    r
}

pub(crate) fn granted_caps() -> CapabilitySnapshot {
    let c = CapabilitySnapshot::new();
    c.update(APP_ID, [DB_PERMISSION_ID.to_string()]);
    c
}

pub(crate) fn mock_db() -> MockDatabase {
    MockDatabase::new(SeaDbBackend::Postgres)
}

pub(crate) fn exec_ok(n: usize) -> Vec<MockExecResult> {
    vec![
        MockExecResult {
            last_insert_id: 0,
            rows_affected: 0,
        };
        n
    ]
}

pub(crate) fn conn(db: MockDatabase) -> DatabaseConnection {
    db.into_connection()
}

pub(crate) fn cells(cells: Vec<(&str, Value)>) -> MockRow {
    cells.into_iter().map(|(k, v)| (k.to_string(), v)).collect()
}

pub(crate) fn uuid(s: &str) -> Value {
    Value::Uuid(Some(Uuid::parse_str(s).expect("valid uuid")))
}

pub(crate) fn text(s: &str) -> Value {
    Value::String(Some(s.to_string()))
}

pub(crate) fn big(n: i64) -> Value {
    Value::BigInt(Some(n))
}

pub(crate) fn int(n: i32) -> Value {
    Value::Int(Some(n))
}

/// A full v2 result row for `events` (what `select_list` returns).
pub(crate) fn event_row(id: &str, version: i32, title: &str, start_at: &str) -> MockRow {
    cells(vec![
        ("row_id", uuid(id)),
        ("version", int(version)),
        ("created_at", text("2026-01-01T00:00:00.000000Z")),
        ("updated_at", text("2026-01-02T00:00:00.000000Z")),
        ("title", text(title)),
        ("start_at", text(start_at)),
        ("score", Value::BigInt(None)),
    ])
}

/// [`event_row`] plus the `_sz` the write paths read for byte accounting.
pub(crate) fn event_row_sz(id: &str, version: i32, title: &str, size: i64) -> MockRow {
    let mut r = event_row(id, version, title, "2026-03-01T00:00:00.000000Z");
    r.insert("_sz".into(), big(size));
    r
}

pub(crate) fn count_row(n: i64) -> Vec<MockRow> {
    vec![cells(vec![("n", big(n))])]
}

pub(crate) fn usage_row() -> Vec<MockRow> {
    vec![cells(vec![("row_count", big(1))])]
}

/// A Postgres error carrying a SQLSTATE, to drive `map_db_err`.
#[derive(Debug)]
pub(crate) struct PgErr(pub &'static str);

impl std::fmt::Display for PgErr {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "pg error {}", self.0)
    }
}

impl std::error::Error for PgErr {}

impl sea_orm::sqlx::error::DatabaseError for PgErr {
    fn message(&self) -> &str {
        "fake database error"
    }
    fn code(&self) -> Option<std::borrow::Cow<'_, str>> {
        Some(std::borrow::Cow::Borrowed(self.0))
    }
    fn as_error(&self) -> &(dyn std::error::Error + Send + Sync + 'static) {
        self
    }
    fn as_error_mut(&mut self) -> &mut (dyn std::error::Error + Send + Sync + 'static) {
        self
    }
    fn into_error(self: Box<Self>) -> Box<dyn std::error::Error + Send + Sync + 'static> {
        self
    }
    fn kind(&self) -> sea_orm::sqlx::error::ErrorKind {
        sea_orm::sqlx::error::ErrorKind::Other
    }
}

/// A `DbErr::Exec` wrapping a database error with the given SQLSTATE.
pub(crate) fn pg_exec_err(code: &'static str) -> sea_orm::DbErr {
    sea_orm::DbErr::Exec(sea_orm::error::RuntimeErr::SqlxError(
        sea_orm::sqlx::Error::Database(Box::new(PgErr(code))).into(),
    ))
}

/// A `DbErr::Query` wrapping a database error with the given SQLSTATE.
pub(crate) fn pg_query_err(code: &'static str) -> sea_orm::DbErr {
    sea_orm::DbErr::Query(sea_orm::error::RuntimeErr::SqlxError(
        sea_orm::sqlx::Error::Database(Box::new(PgErr(code))).into(),
    ))
}
