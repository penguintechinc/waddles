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

use crate::limits::{MAX_NUMERIC_PRECISION, MAX_NUMERIC_SCALE};
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
    /// Exact decimal, `numeric(precision, scale)` -- the manifest's
    /// `numeric(p,s)` (`hub_api/services/bundle_data_schema.py`). Build one
    /// with [`ColumnType::numeric`] (bounds-checked); a value written to it
    /// is a decimal string or an integer, read back as the column's exact
    /// decimal text -- never a float (see `crate::typed`).
    Numeric {
        precision: u8,
        scale: u8,
    },
}

impl ColumnType {
    /// A bounds-checked [`ColumnType::Numeric`]: the same limits the
    /// manifest validator enforces at approval time (precision
    /// `1..=`[`MAX_NUMERIC_PRECISION`], scale `0..=`[`MAX_NUMERIC_SCALE`],
    /// scale <= precision).
    pub fn numeric(precision: u8, scale: u8) -> Result<Self, String> {
        validate_numeric_params(precision, scale)?;
        Ok(Self::Numeric { precision, scale })
    }
}

/// Re-checks a numeric column's `(precision, scale)` against the manifest
/// validator's limits -- [`ColumnType::Numeric`]'s fields are public, so
/// [`TableSchema::validated`] cannot assume [`ColumnType::numeric`] built it.
fn validate_numeric_params(precision: u8, scale: u8) -> Result<(), String> {
    if !(1..=MAX_NUMERIC_PRECISION).contains(&precision) {
        return Err(format!(
            "numeric precision {precision} is outside 1..={MAX_NUMERIC_PRECISION}"
        ));
    }
    if scale > MAX_NUMERIC_SCALE {
        return Err(format!("numeric scale {scale} exceeds {MAX_NUMERIC_SCALE}"));
    }
    if scale > precision {
        return Err(format!(
            "numeric scale {scale} exceeds precision {precision}"
        ));
    }
    Ok(())
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
    /// **Deliberate, explicit, per-table opt-in** for the cross-community
    /// read exception (`rules/critical-rules.md` PII Tokenization's
    /// `[[bundle-state-community-scoped]]` note: "the only exceptions are
    /// reputation + user details"). `false` for every table by default and
    /// for every table this crate's own `validated()` constructs -- only
    /// flippable afterward via [`Self::with_cross_community_read`], which a
    /// future platform-owned schema loader (never a bundle manifest) would
    /// call only for the specific tables the platform itself designates as
    /// intentionally cross-community (e.g. a hub-api-owned
    /// `reputation_global` keyed by `hub_user_id`).
    ///
    /// **What this does and does not bypass.** [`crate::backend`]'s `get`/
    /// `query` drop the explicit `tenant_id = $n AND community_id = $m`
    /// predicate (and its RLS counterpart is expected to carry a matching
    /// policy exception at the DDL layer -- out of this crate's scope, see
    /// `crate::backend`'s own doc) when this is `true`; `insert`/`update`/
    /// `delete` refuse outright (`DbError::InvalidColumn`) rather than ever
    /// writing a row with no tenant scope. **`app_id` scoping is never
    /// bypassed** -- this crate's whole design is "one table per app", so a
    /// cross-community-read table still only ever resolves and reads its
    /// own app's table, never another app's; "without the
    /// (tenant, community, app_id) row-scoping" in the exception's own
    /// design note refers to the tenant/community half of that triple, the
    /// part this flag actually controls.
    pub cross_community_read: bool,
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
            if let ColumnType::Numeric { precision, scale } = col.sql_type {
                validate_numeric_params(precision, scale)
                    .map_err(|e| format!("invalid column {:?}: {e}", col.name))?;
            }
        }
        Ok(Self {
            schema,
            table,
            columns,
            cross_community_read: false,
        })
    }

    /// Opts this already-[`validated`](Self::validated) table into the
    /// cross-community read exception -- see [`Self::cross_community_read`]'s
    /// own doc for exactly what this does and does not bypass. Builder-style
    /// so every existing `validated()` call site (every test in this crate,
    /// every production schema-loader row today) is unaffected; only a
    /// caller that explicitly opts in ever sets this.
    pub fn with_cross_community_read(mut self) -> Self {
        self.cross_community_read = true;
        self
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
    fn validated_defaults_cross_community_read_to_false() {
        let schema = TableSchema::validated(AppSchema::Core, "fishing_core", vec![]).unwrap();
        assert!(!schema.cross_community_read);
    }

    #[test]
    fn with_cross_community_read_opts_a_table_in() {
        let schema = TableSchema::validated(AppSchema::Core, "reputation_global", vec![])
            .unwrap()
            .with_cross_community_read();
        assert!(schema.cross_community_read);
    }

    #[test]
    fn numeric_accepts_the_manifest_bounds_and_rejects_everything_else() {
        for (p, sc) in [(1, 0), (10, 2), (12, 12), (38, 12), (38, 0)] {
            assert_eq!(
                ColumnType::numeric(p, sc),
                Ok(ColumnType::Numeric {
                    precision: p,
                    scale: sc
                }),
                "numeric({p},{sc})"
            );
        }
        for (p, sc) in [(0, 0), (39, 0), (10, 13), (5, 6), (255, 255)] {
            assert!(ColumnType::numeric(p, sc).is_err(), "numeric({p},{sc})");
        }
    }

    #[test]
    fn validated_rechecks_a_directly_constructed_numeric_column() {
        let numeric_column = |precision, scale| ColumnDef {
            name: "amount".to_string(),
            sql_type: ColumnType::Numeric { precision, scale },
            nullable: true,
            is_user_ref: false,
        };
        assert!(
            TableSchema::validated(AppSchema::Core, "money", vec![numeric_column(10, 2)]).is_ok()
        );
        for (p, sc) in [(0, 0), (39, 2), (10, 13), (2, 5)] {
            let err = TableSchema::validated(AppSchema::Core, "money", vec![numeric_column(p, sc)])
                .unwrap_err();
            assert!(
                err.contains("amount") && err.contains("numeric"),
                "numeric({p},{sc}) -> {err}"
            );
        }
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
