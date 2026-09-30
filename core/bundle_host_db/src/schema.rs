//! Per-`app_id` table schema cache -- what [`crate::backend`] resolves
//! identifiers and column types from. **Never derived from a bundle
//! host-call's own `args`** -- populated only from hub-api's own
//! provisioning-time metadata (`bundle_data_table_name` + the manifest's
//! declared `data.table.columns[]`, design doc SS3.4/SS2.1), the same way
//! `core/bundle_host_kv::authorize::CapabilitySnapshot` is refreshed by
//! each stage's own DB-driven `bundle_loader` (that module's doc).
//!
//! **This landing's scope:** the cache type and its validation are
//! implemented and tested here; wiring a live `bundle_loader` poll that
//! calls [`SchemaCache::update`] from hub-api's `app_install_approvals`/
//! `bundle_table_schema_versions` tables is out of scope for this slice
//! (tracked as remaining work -- see this crate's top-level doc in
//! `lib.rs`). An `app_id` this cache has never heard of resolves to "no
//! table provisioned yet", which [`crate::authorize::authorize_db`]'s
//! caller must treat as a denial, never as "any table is fine."

use std::collections::HashMap;
use std::sync::{Arc, RwLock};

use crate::scope::{validate_identifier, AppSchema};

/// The type allowlist a bundle-declared column may have (design doc SS3.1).
/// `Uuid` used for `user_ref` too -- the wire distinction between "any
/// UUID" and "must be a platform user reference" lives in
/// [`ColumnDef::is_user_ref`], not in a separate variant here.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ColumnType {
    Uuid,
    Int4,
    Int8,
    Bool,
    /// `max_len` in bytes, <= 8192 (design doc SS3.1).
    Text,
    Timestamptz,
    /// `max_bytes` <= 16 KiB (design doc SS3.1).
    Jsonb,
}

/// One bundle-declared column, already validated at manifest-approval time
/// (hub-api, PR #430) -- this struct is the host-side runtime mirror of
/// that decision, not a second place that re-derives it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ColumnDef {
    pub name: String,
    pub sql_type: ColumnType,
    pub nullable: bool,
    /// True only for a `user_ref` column (design doc SS4) -- every value
    /// written to it must be a syntactically valid UUID string, enforced
    /// by [`crate::backend`] regardless of the column's declared
    /// [`ColumnType`] (always [`ColumnType::Uuid`] in practice, but this
    /// flag is what actually drives the stricter validation and the
    /// erasure-cascade eligibility, not the type alone).
    pub is_user_ref: bool,
}

/// The five platform-owned columns every bundle table has (design doc
/// SS3.3) -- never accepted in a guest-supplied `column-values` map,
/// regardless of whether a bundle happens to declare a same-named column
/// (manifest validation at approval time already rejects that collision;
/// this is the host-side defense-in-depth check at call time).
pub const PLATFORM_COLUMNS: &[&str] = &[
    "row_id",
    "tenant_id",
    "community_id",
    "version",
    "created_at",
    "updated_at",
];

/// One app's resolved table identity + declared columns -- everything
/// [`crate::backend`] needs to build a parameterized statement without
/// ever consulting guest input for a schema/table/column name.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TableSchema {
    pub schema: AppSchema,
    pub table: String,
    pub columns: Vec<ColumnDef>,
}

impl TableSchema {
    /// Validates `schema`/`table`/every declared column name (design doc
    /// SS3.2/SS3.4) before ever admitting this schema into a
    /// [`SchemaCache`] -- an identifier that fails validation here can
    /// never reach [`crate::backend`] at all.
    pub fn validated(
        schema: AppSchema,
        table: impl Into<String>,
        columns: Vec<ColumnDef>,
    ) -> Result<Self, String> {
        let table = table.into();
        validate_identifier(&table).map_err(|e| format!("invalid table identifier: {e:?}"))?;
        if columns.len() > 32 {
            return Err(format!(
                "too many declared columns ({}), cap is 32",
                columns.len()
            ));
        }
        for col in &columns {
            validate_identifier(&col.name)
                .map_err(|e| format!("invalid column identifier {:?}: {e:?}", col.name))?;
            if PLATFORM_COLUMNS.contains(&col.name.as_str()) {
                return Err(format!(
                    "column {:?} collides with a platform-owned column",
                    col.name
                ));
            }
        }
        Ok(Self {
            schema,
            table,
            columns,
        })
    }

    /// The column definition for `name`, if declared -- the single lookup
    /// every op uses to decide whether a guest-supplied column name is
    /// admissible, and to fetch its type/`user_ref` flag.
    pub fn column(&self, name: &str) -> Option<&ColumnDef> {
        self.columns.iter().find(|c| c.name == name)
    }

    pub fn qualified_name(&self) -> String {
        format!(
            "{}.{}",
            crate::scope::quote_ident(self.schema.as_str()),
            crate::scope::quote_ident(&self.table)
        )
    }
}

/// Per-`app_id` cache of resolved [`TableSchema`]s -- refreshed by each
/// stage's own `bundle_loader` (see module doc). Keyed by `app_id` alone,
/// the same single-key convention `bundle_host_kv::authorize::
/// CapabilitySnapshot` already uses.
#[derive(Default)]
pub struct SchemaCache {
    inner: RwLock<HashMap<String, Arc<TableSchema>>>,
}

impl SchemaCache {
    pub fn new() -> Self {
        Self::default()
    }

    /// Replaces `app_id`'s schema wholesale. Never merges -- a schema-
    /// version upgrade or a table teardown must fully replace/remove the
    /// prior entry, not accumulate stale columns.
    pub fn update(&self, app_id: impl Into<String>, schema: TableSchema) {
        let mut guard = self
            .inner
            .write()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        guard.insert(app_id.into(), Arc::new(schema));
    }

    pub fn remove(&self, app_id: &str) {
        self.inner
            .write()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .remove(app_id);
    }

    /// `None` means "no table provisioned for this app" -- callers must
    /// treat that as a denial, never fall back to any other table.
    pub fn get(&self, app_id: &str) -> Option<Arc<TableSchema>> {
        self.inner
            .read()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .get(app_id)
            .cloned()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_columns() -> Vec<ColumnDef> {
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
        ]
    }

    #[test]
    fn validated_accepts_a_well_formed_schema() {
        let schema =
            TableSchema::validated(AppSchema::Core, "fishing_core", sample_columns()).unwrap();
        assert_eq!(schema.table, "fishing_core");
        assert_eq!(schema.columns.len(), 2);
    }

    #[test]
    fn validated_rejects_an_invalid_table_identifier() {
        assert!(TableSchema::validated(AppSchema::Core, "Fishing-Core", vec![]).is_err());
    }

    #[test]
    fn validated_rejects_a_column_colliding_with_a_platform_column() {
        let columns = vec![ColumnDef {
            name: "tenant_id".to_string(),
            sql_type: ColumnType::Uuid,
            nullable: true,
            is_user_ref: false,
        }];
        let err = TableSchema::validated(AppSchema::Core, "fishing_core", columns).unwrap_err();
        assert!(err.contains("platform-owned"));
    }

    #[test]
    fn validated_rejects_over_column_cap() {
        let columns: Vec<ColumnDef> = (0..33)
            .map(|i| ColumnDef {
                name: format!("c{i}"),
                sql_type: ColumnType::Int4,
                nullable: true,
                is_user_ref: false,
            })
            .collect();
        assert!(TableSchema::validated(AppSchema::Core, "fishing_core", columns).is_err());
    }

    #[test]
    fn column_looks_up_by_declared_name_only() {
        let schema =
            TableSchema::validated(AppSchema::Core, "fishing_core", sample_columns()).unwrap();
        assert!(schema.column("score").is_some());
        assert!(schema.column("row_id").is_none());
        assert!(schema.column("not_declared").is_none());
    }

    #[test]
    fn qualified_name_is_schema_qualified_and_quoted() {
        let schema =
            TableSchema::validated(AppSchema::Community, "superpenguin_fishing_core", vec![])
                .unwrap();
        assert_eq!(
            schema.qualified_name(),
            "\"app_community\".\"superpenguin_fishing_core\""
        );
    }

    #[test]
    fn schema_cache_update_replaces_rather_than_merges() {
        let cache = SchemaCache::new();
        let schema_a =
            TableSchema::validated(AppSchema::Core, "fishing_core", sample_columns()).unwrap();
        cache.update("app_a", schema_a);
        assert_eq!(cache.get("app_a").unwrap().columns.len(), 2);

        let schema_b = TableSchema::validated(AppSchema::Core, "fishing_core", vec![]).unwrap();
        cache.update("app_a", schema_b);
        assert_eq!(
            cache.get("app_a").unwrap().columns.len(),
            0,
            "a fresh update must fully replace the prior schema, not merge into it"
        );
    }

    #[test]
    fn schema_cache_get_is_none_for_an_unknown_app() {
        let cache = SchemaCache::new();
        assert!(cache.get("never_provisioned").is_none());
    }

    #[test]
    fn schema_cache_remove_clears_the_entry() {
        let cache = SchemaCache::new();
        let schema = TableSchema::validated(AppSchema::Core, "fishing_core", vec![]).unwrap();
        cache.update("app_a", schema);
        cache.remove("app_a");
        assert!(cache.get("app_a").is_none());
    }
}
