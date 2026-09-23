//! Read-only SeaORM entity for `communities` (migration 000) -- this
//! service only resolves a `StageEnvelope.community` slug (the `name`
//! column) to its integer FK for `action_dispatch_log.community_id`
//! (`crate::wiring::DbTenantResolver`). No other column is declared; this
//! service never writes this table.

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, Eq, DeriveEntityModel)]
#[sea_orm(table_name = "communities")]
pub struct Model {
    #[sea_orm(primary_key)]
    pub id: i32,
    pub name: String,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn table_name_matches_migration_000() {
        assert_eq!(Entity.table_name(), "communities");
    }
}
