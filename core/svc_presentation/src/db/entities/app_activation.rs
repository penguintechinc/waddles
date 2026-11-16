//! SeaORM entity for the hub-owned `app_activations` table (migration 069), read-only and only the
//! columns needed to answer "is this bundle enabled for this community": a widget push is refused
//! unless its owning bundle is. Mirrors hub-api's own `enabled == True` check.

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, Eq, DeriveEntityModel)]
#[sea_orm(table_name = "app_activations")]
pub struct Model {
    #[sea_orm(primary_key)]
    pub id: i32,
    pub community_id: i32,
    pub tenant_id: i32,
    pub app_id: String,
    pub enabled: Option<bool>,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}
