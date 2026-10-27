//! Read-only SeaORM entity for `app_versions` (migration
//! `0022_app_versions_and_rbac`, plus two parallel migrations -- landing
//! separately, hub-api half of this same contract -- adding
//! `component_key TEXT`/`sidecar_key TEXT` (migration
//! `0024_app_versions_component_key`) and the artifact-signature columns
//! below (migration `0040_bundle_artifact_signature`)). Only the columns
//! this crate's queries actually read are declared; `artifact_digest` is
//! the `sha256:<64 hex>` string the executor's `verify_digest`
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
//!
//! **`artifact_signature`/`artifact_signature_key_id`/
//! `artifact_signed_approval_id` (spec SS5.6/Gemini review condition 9):**
//! written by `hub_api/services/bundle_signing_service.py` at the same
//! approval step that writes `app_install_approvals`. Surfaced here for
//! observability/audit (`ActiveBundleRow` carries them through
//! `crate::query::read_active_set`) -- the AUTHORITATIVE verification path
//! is `core/bundle_executor/src/signing.rs`, which checks the signed
//! `.json` sidecar object fetched from the bucket (the wire protocol's
//! `LoadBody`, an external `penguin-bundle-host` crate this repo does not
//! own, has no signature-shaped field to carry these columns over), not a
//! direct comparison against these DB columns.
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
    pub artifact_signature: Option<String>,
    pub artifact_signature_key_id: Option<String>,
    pub artifact_signed_approval_id: Option<i64>,
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
