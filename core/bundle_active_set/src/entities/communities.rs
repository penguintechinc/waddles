//! Read-only SeaORM entity for `communities` (migration `058_tenants_and_claims.sql`
//! -- see `hub_api/services/schema.py`'s pydal binding of the same table,
//! `Field("name", ...)`/`Field("tenant_id", ...)`). `name` is this table's
//! slug-equivalent column (no separate `slug` column exists -- confirmed
//! against `hub_api/services/superadmin_service.py`'s own `dal.communities
//! .name == slug` lookup), the value `crate::scope::resolve_scope` returns
//! as the community half of a `penguin_spine::Scope`.
//!
//! `tenant_id` is declared (unlike `core/svc_action/src/db/entities/
//! communities.rs`'s minimal slug-to-id entity, which doesn't need it) so
//! `resolve_scope` can verify the resolved community actually belongs to
//! the requested tenant, not just that SOME community with that numeric id
//! exists somewhere in the table -- a cross-tenant id collision must never
//! silently resolve to the wrong tenant's community.

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, Eq, DeriveEntityModel)]
#[sea_orm(table_name = "communities")]
pub struct Model {
    #[sea_orm(primary_key)]
    pub id: i32,
    pub name: String,
    pub tenant_id: i32,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn table_name_matches_migration_058() {
        assert_eq!(Entity.table_name(), "communities");
    }
}
