//! Read-only SeaORM entity for `app_versions` (migration
//! `0022_app_versions_and_rbac`, plus a parallel migration -- landing
//! separately, hub-api half of this same contract -- adding
//! `component_key TEXT`/`sidecar_key TEXT`). Only the columns this crate's
//! queries actually read are declared; `artifact_digest` is the
//! `sha256:<64 hex>` string the executor's `verify_digest`
//! (`core/bundle_executor/src/invoke.rs`) checks against.
//!
//! **`component_key`/`sidecar_key` contract (data-plane half; hub-api half
//! is the migration + the publish step populating them):**
//! `component_key` is the FULL MinIO key of the compiled `.wasm`, exactly
//! `bundles/{app_id}/{version}/{sha256}.wasm`; `sidecar_key` is the
//! manifest sidecar's key, nullable independently of `component_key`. Both
//! `Option<String>` here because the migration adding them and the
//! publish-step backfill are still landing -- a row published before
//! either lands has `component_key = NULL`. `crate::query::read_active_set`
//! uses the column directly when present and falls back to
//! `crate::query::derive_component_keys`'s content-addressed convention
//! only when it's NULL (an un-backfilled row, expected during rollout, not
//! a steady-state gap) -- see that function's doc for the fallback's own
//! visibility (structured `warn!` + a degraded-row metric).
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
    pub component_key: Option<String>,
    pub sidecar_key: Option<String>,
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
