//! Read-only SeaORM entity for `tenants` (migration 058) -- this service
//! only resolves a `StageEnvelope.tenant` slug to its integer FK for
//! `action_dispatch_log.tenant_id` (`crate::wiring::DbTenantResolver`),
//! mirroring the Python runner's `_resolve_tenant_id`. No other column is
//! declared; this service never writes this table.

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
