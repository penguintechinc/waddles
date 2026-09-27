//! Read-only SeaORM entity for `app_source_bindings` -- the hub-api-owned
//! per-`(tenant, community, app)` binding of a bundle to one ingest-source
//! stream (`platform`/`source_id`). hub-api's own migration (landing in
//! parallel with this change) provisions this table plus a Postgres
//! consumer group named `app_id` on each bound source stream
//! (`penguin_spine::Scope::source_stream`); this crate only ever reads the
//! binding rows themselves, never creates or manages the Valkey consumer
//! group side of the contract.
//!
//! `community_id = 0` is the reserved tenant-wide sentinel, matching
//! `app_active_versions.community_id`'s own convention (migration
//! `0022_app_versions_and_rbac`) -- a binding scoped to `community_id = 0`
//! applies across every community in the tenant.
//!
//! Composite primary key `(tenant_id, community_id, app_id, platform,
//! source_id)`: a given bundle may bind to more than one source (multiple
//! rows sharing `(tenant_id, community_id, app_id)`), and a given source
//! may be bound to more than one bundle (multiple rows sharing `(tenant_id,
//! community_id, platform, source_id)`) -- both directions are legitimate,
//! so no narrower key is correct. `created_at` (present in the real table)
//! is deliberately not modeled here -- this crate declares only the
//! columns it actually reads, per `crate::entities`'s module doc.

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, Eq, DeriveEntityModel)]
#[sea_orm(table_name = "app_source_bindings")]
pub struct Model {
    #[sea_orm(primary_key, auto_increment = false)]
    pub tenant_id: i32,
    #[sea_orm(primary_key, auto_increment = false)]
    pub community_id: i32,
    #[sea_orm(primary_key, auto_increment = false)]
    pub app_id: String,
    #[sea_orm(primary_key, auto_increment = false)]
    pub platform: String,
    #[sea_orm(primary_key, auto_increment = false)]
    pub source_id: String,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn table_name_matches_the_hub_api_contract() {
        assert_eq!(Entity.table_name(), "app_source_bindings");
    }
}
