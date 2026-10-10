//! SeaORM entity for the hub-owned `communities` table
//! (`config/postgres/migrations/000_create_base_schema.sql` + `058_tenants_and_claims.sql`).
//!
//! Read-only here, and only the two columns the overlay push path needs:
//! `id` and `tenant_id`. [`crate::overlay::community_ctx`] uses them to map a
//! PUSH credential's `community_id` to the tenant hub-api scopes display-name
//! resolution by (a community nests in exactly one tenant) -- the PUSH JWT
//! itself carries no tenant claim, and a tenant must never come from a
//! request body or path (`rules/security.md` Tenant Isolation).

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, Eq, DeriveEntityModel)]
#[sea_orm(table_name = "communities")]
pub struct Model {
    #[sea_orm(primary_key, auto_increment = false)]
    pub id: i32,
    pub tenant_id: i32,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}
