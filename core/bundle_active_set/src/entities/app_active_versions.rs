//! Read-only SeaORM entity for `app_active_versions` (migration
//! `0022_app_versions_and_rbac`) -- the hub-api-owned activation pointer:
//! one row per `(app_id, tenant_id, community_id)` naming the currently
//! active `app_versions.id`. `community_id = 0` is the reserved
//! tenant-wide sentinel (`communities.id` is a real `SERIAL` starting at
//! 1, so it never collides) -- see the migration's own column comment.

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, Eq, DeriveEntityModel)]
#[sea_orm(table_name = "app_active_versions")]
pub struct Model {
    #[sea_orm(primary_key, auto_increment = false)]
    pub app_id: String,
    #[sea_orm(primary_key, auto_increment = false)]
    pub tenant_id: i32,
    #[sea_orm(primary_key, auto_increment = false)]
    pub community_id: i32,
    pub version_id: i64,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn table_name_matches_migration_0022() {
        assert_eq!(Entity.table_name(), "app_active_versions");
    }
}
