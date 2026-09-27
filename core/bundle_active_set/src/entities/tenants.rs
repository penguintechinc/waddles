//! Read-only SeaORM entity for `tenants` (migration `058_tenants_and_claims.sql`,
//! hub-api's own auth-chain schema -- see `hub_api/app.py::
//! _bind_reference_tables`'s pydal binding of the same table). This crate
//! only resolves [`crate::query::ActiveSetRead`]'s numeric
//! `BUNDLE_SCOPE_TENANT_ID`/`BUNDLE_SCOPE_COMMUNITY_ID` scope (already
//! numeric, unlike `core/svc_action/src/db/entities/tenants.rs`'s
//! slug-to-id direction) to the tenant `slug`
//! `penguin_spine::Scope::source_stream` needs (`crate::scope::
//! resolve_scope`). Mirrors `core/svc_action`'s own minimal `tenants`
//! entity exactly (same table, same two columns) -- this service never
//! writes this table.

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, Eq, DeriveEntityModel)]
#[sea_orm(table_name = "tenants")]
pub struct Model {
    #[sea_orm(primary_key)]
    pub id: i32,
    pub slug: String,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn table_name_matches_migration_058() {
        assert_eq!(Entity.table_name(), "tenants");
    }
}
