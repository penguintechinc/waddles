//! SeaORM entity for `overlay_widget_registry` (migration 104).

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
#[sea_orm(table_name = "overlay_widget_registry")]
pub struct Model {
    #[sea_orm(primary_key)]
    pub id: i64,
    pub public_id: Uuid,
    pub tenant_id: i64,
    pub community_id: i64,
    pub bundle_app_id: String,
    pub widget_id: String,
    pub display_name: String,
    pub description: String,
    pub category: String,
    pub output_kind_code: i16,
    pub surface_id: Option<String>,
    pub allow_standalone_output: bool,
    pub default_x_px: i32,
    pub default_y_px: i32,
    pub default_width_px: i32,
    pub default_height_px: i32,
    pub default_z_index: i32,
    pub props_schema: Json,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}

