//! Read-only SeaORM entity for `app_install_approvals` (migration
//! `0023_bundle_install_schema`) -- the current-approval record this
//! crate's `read_active_set` (`crate::query`) requires alongside
//! `app_active_versions` (spec's "ACTIVE, APPROVED bundle config").
//! `superseded_by IS NULL` is "the current approval" (the migration's own
//! partial unique index enforces at most one per
//! `(app_id, version, tenant_id, community_id)`).
//!
//! **Sentinel mismatch with `app_active_versions` (documented, handled in
//! `crate::query`, not a schema change):** `app_active_versions
//! .community_id` is `NOT NULL DEFAULT 0` (0 = tenant-wide, since Postgres
//! composite PK columns can't be NULL); `app_install_approvals
//! .community_id` is a nullable `INTEGER REFERENCES communities(id)`
//! (NULL = tenant-wide, no sentinel needed since it isn't a PK column).
//! Two different encodings of the same "tenant-wide" concept across two
//! tables from the same migration pair -- `crate::query::read_active_set`
//! matches `approvals.community_id = active.community_id OR
//! (approvals.community_id IS NULL AND active.community_id = 0)` rather
//! than assuming either table's convention.

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, Eq, DeriveEntityModel)]
#[sea_orm(table_name = "app_install_approvals")]
pub struct Model {
    #[sea_orm(primary_key)]
    pub id: i64,
    pub tenant_id: i32,
    pub community_id: Option<i32>,
    pub app_id: String,
    pub version: String,
    pub superseded_by: Option<i64>,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn table_name_matches_migration_0023() {
        assert_eq!(Entity.table_name(), "app_install_approvals");
    }
}
