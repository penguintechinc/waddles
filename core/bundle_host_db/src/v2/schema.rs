//! `db` v2 schema registry: every table a bundle declared, with its declared
//! columns and indexes. Like [`crate::schema::SchemaCache`] it is **never
//! derived from a host-call's own args** -- populated only from hub-api's
//! provisioning metadata (`bundle_data.table_registry`, see
//! [`load_registry`]), so a guest can only ever name a *logical* table that the
//! approved manifest declared, and the physical `(schema, table)` is resolved
//! here, never taken from the guest.

use std::collections::{BTreeMap, HashMap};
use std::sync::{Arc, RwLock};

use sea_orm::{ConnectionTrait, DatabaseConnection, DbBackend as SeaDbBackend, Statement};
use serde::Deserialize;

use crate::limits::MAX_TABLES_PER_BUNDLE;
use crate::schema::{ColumnDef, ColumnType, TableSchema};
use crate::scope::{validate_identifier, AppSchema};

/// Most declared indexes per table (mirrors `MAX_INDEXES` in
/// `hub_api/services/bundle_data_schema.py`).
pub const MAX_INDEXES_PER_TABLE: usize = 8;
/// Most bundle-declared columns per index (mirrors `MAX_INDEX_WIDTH`).
pub const MAX_INDEX_WIDTH: usize = 4;
/// Longest logical table name, in bytes.
pub const MAX_LOGICAL_NAME_LEN: usize = 32;

/// One column of a declared index.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IndexColumn {
    pub name: String,
    pub descending: bool,
    /// The index was built with `text_pattern_ops`, so `prefix` conditions on
    /// this column are index-supported under any collation.
    pub prefix_ops: bool,
}

/// One declared index. The provisioned index is always prefixed with
/// `(tenant_id, community_id)`; only the bundle's own columns appear here.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IndexDef {
    pub columns: Vec<IndexColumn>,
    pub unique: bool,
}

/// One declared table: the validated v1-shaped [`TableSchema`] (physical
/// identity + typed columns, reused so every v1 validation/binding helper
/// applies unchanged) plus its logical name and indexes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TableDefV2 {
    pub logical: String,
    pub table: TableSchema,
    pub indexes: Vec<IndexDef>,
}

impl TableDefV2 {
    /// Validates the logical name and every index against the declared
    /// columns before the table can enter a [`BundleSchemaV2`].
    pub fn validated(
        logical: impl Into<String>,
        table: TableSchema,
        indexes: Vec<IndexDef>,
    ) -> Result<Self, String> {
        let logical = logical.into();
        validate_identifier(&logical).map_err(|e| format!("invalid logical table name: {e:?}"))?;
        if logical.len() > MAX_LOGICAL_NAME_LEN {
            return Err(format!(
                "logical table name {logical:?} exceeds {MAX_LOGICAL_NAME_LEN} bytes"
            ));
        }
        if logical.contains("__") {
            return Err(format!(
                "logical table name {logical:?} must not contain \"__\""
            ));
        }
        if table.cross_community_read {
            return Err("a v2 table can never opt into cross-community reads".to_string());
        }
        if indexes.len() > MAX_INDEXES_PER_TABLE {
            return Err(format!(
                "{} indexes exceeds the cap of {MAX_INDEXES_PER_TABLE}",
                indexes.len()
            ));
        }
        for index in &indexes {
            if index.columns.is_empty() || index.columns.len() > MAX_INDEX_WIDTH {
                return Err(format!(
                    "an index must have 1..={MAX_INDEX_WIDTH} columns, got {}",
                    index.columns.len()
                ));
            }
            for col in &index.columns {
                let Some(def) = table.column(&col.name) else {
                    return Err(format!("index references undeclared column {:?}", col.name));
                };
                if col.prefix_ops && !matches!(def.sql_type, ColumnType::Text) {
                    return Err(format!(
                        "prefix_ops is only valid on a text column, {:?} is not",
                        col.name
                    ));
                }
            }
        }
        Ok(Self {
            logical,
            table,
            indexes,
        })
    }

    /// True when `column` is the first bundle column of some declared index.
    pub fn leads_an_index(&self, column: &str) -> bool {
        self.indexes
            .iter()
            .any(|i| i.columns.first().is_some_and(|c| c.name == column))
    }

    /// True when `column` leads an index built with `text_pattern_ops`.
    pub fn leads_a_prefix_index(&self, column: &str) -> bool {
        self.indexes.iter().any(|i| {
            i.columns
                .first()
                .is_some_and(|c| c.name == column && c.prefix_ops)
        })
    }
}

/// Every table one bundle declared, keyed by logical name.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BundleSchemaV2 {
    pub app_id: String,
    tables: BTreeMap<String, Arc<TableDefV2>>,
}

impl BundleSchemaV2 {
    /// Builds a bundle schema, rejecting duplicate logical names, duplicate
    /// physical tables, and more than [`MAX_TABLES_PER_BUNDLE`] tables.
    pub fn validated(app_id: impl Into<String>, tables: Vec<TableDefV2>) -> Result<Self, String> {
        if tables.is_empty() {
            return Err("a v2 bundle schema must declare at least one table".to_string());
        }
        if tables.len() > MAX_TABLES_PER_BUNDLE {
            return Err(format!(
                "{} tables exceeds the cap of {MAX_TABLES_PER_BUNDLE}",
                tables.len()
            ));
        }
        let mut map = BTreeMap::new();
        let mut physical = std::collections::HashSet::new();
        for def in tables {
            if !physical.insert((def.table.schema.as_str(), def.table.table.clone())) {
                return Err(format!(
                    "two logical tables map to physical table {:?}",
                    def.table.table
                ));
            }
            let logical = def.logical.clone();
            if map.insert(logical.clone(), Arc::new(def)).is_some() {
                return Err(format!("logical table {logical:?} declared twice"));
            }
        }
        Ok(Self {
            app_id: app_id.into(),
            tables: map,
        })
    }

    /// The declared table named `logical`, if any.
    pub fn table(&self, logical: &str) -> Option<&Arc<TableDefV2>> {
        self.tables.get(logical)
    }

    /// Logical names, sorted.
    pub fn table_names(&self) -> impl Iterator<Item = &str> {
        self.tables.keys().map(String::as_str)
    }
}

/// Per-`app_id` registry of [`BundleSchemaV2`]s, refreshed from hub-api's
/// provisioning metadata. An unknown `app_id` means "no v2 tables
/// provisioned" -- callers must deny, never fall back to another table.
#[derive(Default)]
pub struct SchemaRegistryV2 {
    inner: RwLock<HashMap<String, Arc<BundleSchemaV2>>>,
}

impl SchemaRegistryV2 {
    pub fn new() -> Self {
        Self::default()
    }

    /// Replaces `app_id`'s schema wholesale (never merges).
    pub fn update(&self, schema: BundleSchemaV2) {
        self.inner
            .write()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .insert(schema.app_id.clone(), Arc::new(schema));
    }

    /// Drops `app_id` (uninstall / teardown).
    pub fn remove(&self, app_id: &str) {
        self.inner
            .write()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .remove(app_id);
    }

    /// The bundle's schema, or `None` when nothing is provisioned.
    pub fn get(&self, app_id: &str) -> Option<Arc<BundleSchemaV2>> {
        self.inner
            .read()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .get(app_id)
            .cloned()
    }

    /// Replaces the whole registry atomically (used by [`load_registry`] so a
    /// refresh never exposes a half-loaded view and drops uninstalled apps).
    fn replace_all(&self, schemas: Vec<BundleSchemaV2>) {
        let map = schemas
            .into_iter()
            .map(|s| (s.app_id.clone(), Arc::new(s)))
            .collect();
        *self
            .inner
            .write()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = map;
    }
}

#[derive(Deserialize)]
struct ColumnJson {
    name: String,
    #[serde(rename = "type")]
    kind: String,
    #[serde(default)]
    precision: Option<u8>,
    #[serde(default)]
    scale: Option<u8>,
    #[serde(default)]
    nullable: bool,
    #[serde(default)]
    user_ref: bool,
}

#[derive(Deserialize)]
struct IndexColumnJson {
    name: String,
    #[serde(default)]
    descending: bool,
    #[serde(default)]
    prefix_ops: bool,
}

#[derive(Deserialize)]
struct IndexJson {
    columns: Vec<IndexColumnJson>,
    #[serde(default)]
    unique: bool,
}

fn parse_column(c: ColumnJson) -> Result<ColumnDef, String> {
    let sql_type = match c.kind.as_str() {
        "uuid" => ColumnType::Uuid,
        "int4" => ColumnType::Int4,
        "int8" => ColumnType::Int8,
        "bool" => ColumnType::Bool,
        "text" => ColumnType::Text,
        "timestamptz" => ColumnType::Timestamptz,
        "jsonb" => ColumnType::Jsonb,
        "numeric" => ColumnType::numeric(
            c.precision.ok_or("numeric column without precision")?,
            c.scale.ok_or("numeric column without scale")?,
        )?,
        other => return Err(format!("unknown column type {other:?}")),
    };
    Ok(ColumnDef {
        name: c.name,
        sql_type,
        nullable: c.nullable,
        is_user_ref: c.user_ref,
    })
}

/// One `bundle_data.table_registry` row, as decoded from Postgres.
pub struct RegistryRow {
    pub app_id: String,
    pub logical_name: String,
    pub schema_kind: String,
    pub physical_table: String,
    pub columns_json: String,
    pub indexes_json: String,
}

/// Turns raw registry rows into validated per-bundle schemas. Any invalid row
/// fails the WHOLE load (fail loud: a half-trusted registry is never served).
pub fn build_schemas(rows: Vec<RegistryRow>) -> Result<Vec<BundleSchemaV2>, String> {
    let mut by_app: BTreeMap<String, Vec<TableDefV2>> = BTreeMap::new();
    for row in rows {
        let schema = match row.schema_kind.as_str() {
            "core" => AppSchema::Core,
            "community" => AppSchema::Community,
            other => return Err(format!("unknown schema_kind {other:?} for {}", row.app_id)),
        };
        let columns_raw: Vec<ColumnJson> = serde_json::from_str(&row.columns_json)
            .map_err(|e| format!("{}/{}: bad columns json: {e}", row.app_id, row.logical_name))?;
        let columns = columns_raw
            .into_iter()
            .map(parse_column)
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| format!("{}/{}: {e}", row.app_id, row.logical_name))?;
        let indexes_raw: Vec<IndexJson> = serde_json::from_str(&row.indexes_json)
            .map_err(|e| format!("{}/{}: bad indexes json: {e}", row.app_id, row.logical_name))?;
        let indexes = indexes_raw
            .into_iter()
            .map(|i| IndexDef {
                unique: i.unique,
                columns: i
                    .columns
                    .into_iter()
                    .map(|c| IndexColumn {
                        name: c.name,
                        descending: c.descending,
                        prefix_ops: c.prefix_ops,
                    })
                    .collect(),
            })
            .collect();
        let table = TableSchema::validated(schema, row.physical_table, columns)
            .map_err(|e| format!("{}/{}: {e}", row.app_id, row.logical_name))?;
        let def = TableDefV2::validated(row.logical_name.clone(), table, indexes)
            .map_err(|e| format!("{}/{}: {e}", row.app_id, row.logical_name))?;
        by_app.entry(row.app_id).or_default().push(def);
    }
    by_app
        .into_iter()
        .map(|(app_id, defs)| {
            BundleSchemaV2::validated(app_id.clone(), defs).map_err(|e| format!("{app_id}: {e}"))
        })
        .collect()
}

/// Reads `bundle_data.table_registry` (hub-api provisioning metadata, SELECT
/// granted to `waddles_bundle_runtime`) and atomically replaces `registry`'s
/// contents. Returns the number of bundles loaded. On any error the previous
/// registry is left untouched and the error is returned -- never a partial view.
pub async fn load_registry(
    conn: &DatabaseConnection,
    registry: &SchemaRegistryV2,
) -> Result<usize, String> {
    let rows = conn
        .query_all_raw(Statement::from_string(
            SeaDbBackend::Postgres,
            "SELECT app_id, logical_name, schema_kind, physical_table, \
                    columns::text AS columns_json, indexes::text AS indexes_json \
             FROM bundle_data.table_registry ORDER BY app_id, logical_name"
                .to_string(),
        ))
        .await
        .map_err(|e| format!("reading bundle_data.table_registry: {e}"))?;
    let mut raw = Vec::with_capacity(rows.len());
    for r in &rows {
        let get = |col: &str| -> Result<String, String> {
            r.try_get::<String>("", col).map_err(|e| e.to_string())
        };
        raw.push(RegistryRow {
            app_id: get("app_id")?,
            logical_name: get("logical_name")?,
            schema_kind: get("schema_kind")?,
            physical_table: get("physical_table")?,
            columns_json: get("columns_json")?,
            indexes_json: get("indexes_json")?,
        });
    }
    let schemas = build_schemas(raw)?;
    let n = schemas.len();
    registry.replace_all(schemas);
    Ok(n)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cols() -> Vec<ColumnDef> {
        vec![
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
        ]
    }

    fn table(physical: &str) -> TableSchema {
        TableSchema::validated(AppSchema::Community, physical, cols()).unwrap()
    }

    fn idx(col: &str) -> IndexDef {
        IndexDef {
            columns: vec![IndexColumn {
                name: col.into(),
                descending: false,
                prefix_ops: false,
            }],
            unique: false,
        }
    }

    #[test]
    fn index_on_an_undeclared_column_is_rejected() {
        let err =
            TableDefV2::validated("events", table("a__events"), vec![idx("nope")]).unwrap_err();
        assert!(err.contains("undeclared column"), "{err}");
    }

    #[test]
    fn prefix_ops_only_on_text() {
        let mut i = idx("start_at");
        i.columns[0].prefix_ops = true;
        assert!(TableDefV2::validated("events", table("a__events"), vec![i]).is_err());
    }

    #[test]
    fn logical_name_rules() {
        for bad in ["Events", "a__b", "", "1x", "x-y", &"a".repeat(33)] {
            assert!(
                TableDefV2::validated(bad, table("a__events"), vec![]).is_err(),
                "{bad:?}"
            );
        }
        assert!(TableDefV2::validated("events_2", table("a__events"), vec![]).is_ok());
    }

    #[test]
    fn cross_community_read_tables_are_refused() {
        let t = table("a__events").with_cross_community_read();
        assert!(TableDefV2::validated("events", t, vec![]).is_err());
    }

    #[test]
    fn duplicate_logical_or_physical_tables_are_rejected() {
        let a = TableDefV2::validated("events", table("a__events"), vec![]).unwrap();
        let b = TableDefV2::validated("events", table("a__other"), vec![]).unwrap();
        assert!(BundleSchemaV2::validated("app", vec![a.clone(), b]).is_err());
        let c = TableDefV2::validated("other", table("a__events"), vec![]).unwrap();
        assert!(BundleSchemaV2::validated("app", vec![a, c]).is_err());
    }

    #[test]
    fn too_many_tables_are_rejected() {
        let defs = (0..=MAX_TABLES_PER_BUNDLE)
            .map(|i| {
                TableDefV2::validated(format!("t{i}"), table(&format!("a__t{i}")), vec![]).unwrap()
            })
            .collect();
        assert!(BundleSchemaV2::validated("app", defs).is_err());
    }

    #[test]
    fn leading_index_lookup() {
        let d = TableDefV2::validated("events", table("a__events"), vec![idx("start_at")]).unwrap();
        assert!(d.leads_an_index("start_at"));
        assert!(!d.leads_an_index("title"));
        assert!(!d.leads_a_prefix_index("start_at"));
    }

    #[test]
    fn registry_rows_build_validated_schemas_and_bad_rows_fail_the_whole_load() {
        let row = |app: &str, logical: &str, table: &str, cols: &str| RegistryRow {
            app_id: app.into(),
            logical_name: logical.into(),
            schema_kind: "community".into(),
            physical_table: table.into(),
            columns_json: cols.into(),
            indexes_json: r#"[{"columns":[{"name":"start_at"}],"unique":false}]"#.into(),
        };
        let good = r#"[{"name":"start_at","type":"timestamptz"},{"name":"amt","type":"numeric","precision":10,"scale":2,"nullable":true}]"#;
        let schemas = build_schemas(vec![
            row("app_a", "events", "app_a__events", good),
            row("app_a", "notes", "app_a__notes", good),
            row("app_b", "events", "app_b__events", good),
        ])
        .unwrap();
        assert_eq!(schemas.len(), 2);
        assert_eq!(
            schemas[0].table_names().collect::<Vec<_>>(),
            ["events", "notes"]
        );
        let bad = r#"[{"name":"x","type":"blob"}]"#;
        assert!(build_schemas(vec![
            row("app_a", "events", "app_a__events", good),
            row("app_b", "events", "app_b__events", bad),
        ])
        .is_err());
    }

    #[test]
    fn registry_update_get_remove() {
        let reg = SchemaRegistryV2::new();
        assert!(reg.get("app").is_none());
        let d = TableDefV2::validated("events", table("a__events"), vec![]).unwrap();
        reg.update(BundleSchemaV2::validated("app", vec![d]).unwrap());
        assert!(reg.get("app").unwrap().table("events").is_some());
        assert!(reg.get("app").unwrap().table("other").is_none());
        reg.remove("app");
        assert!(reg.get("app").is_none());
    }
}
