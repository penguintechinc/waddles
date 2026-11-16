//! SeaORM entity for `overlay_widget_registry` (alembic 0113) -- widgets a presentation bundle
//! registered for a community, keyed by `(tenant_id, community_id, bundle_app_id, widget_id)`.
//! Read-only here; hub-api owns the writes. Only the columns the data plane reads are mapped.

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
#[sea_orm(table_name = "overlay_widget_registry")]
pub struct Model {
    #[sea_orm(primary_key)]
    pub id: i64,
    pub tenant_id: i64,
    pub community_id: i64,
    pub bundle_app_id: String,
    pub widget_id: String,
    pub output_kind_code: i16,
    pub surface_id: Option<String>,
    pub allow_standalone_output: bool,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}
