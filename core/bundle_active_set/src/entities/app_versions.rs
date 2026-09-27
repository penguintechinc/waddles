//! Read-only SeaORM entity for `app_versions` (migration
//! `0022_app_versions_and_rbac`) -- the digest table. Only the columns
//! this crate's queries actually read are declared; `artifact_digest` is
//! the `sha256:<64 hex>` string the executor's `verify_digest`
//! (`core/bundle_executor/src/invoke.rs`) checks against.
//!
//! **Confirmed schema gap (flagged, not invented around):** this table
//! has no `component_key`/`sidecar_key` column -- the pre-publish
//! `app_version_uploads.staging_component_key` (migration
//! `0023_bundle_install_schema`) is never copied into a permanent column
//! here; hub-api's own `STATUS_ADDRESSING` -> `STATUS_PUBLISHING` ->
//! `STATUS_PUBLISHED` transition that would do that copy has no
//! implementation yet (`hub_api/services/bundle_version_service.py`).
//! `crate::query::derive_component_keys` fills this gap with a
//! content-addressed convention keyed on `artifact_digest` (matching the
//! `ADDRESSING` status name) until hub-api's publish step lands and
//! persists real bucket keys -- see that function's doc.
use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, Eq, DeriveEntityModel)]
#[sea_orm(table_name = "app_versions")]
pub struct Model {
    #[sea_orm(primary_key)]
    pub id: i64,
    pub app_id: String,
    pub version: String,
    pub artifact_digest: Option<String>,
    pub scan_status: String,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn table_name_matches_migration_0022() {
        assert_eq!(Entity.table_name(), "app_versions");
    }
}
