//! SeaORM entity for `overlay_scene_components` (migration 104).

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
#[sea_orm(table_name = "overlay_scene_components")]
pub struct Model {
    #[sea_orm(primary_key)]
    pub id: i64,
    pub public_id: Uuid,
    pub tenant_id: i64,
    pub community_id: i64,
    pub bundle_app_id: String,
    pub widget_id: String,
    pub surface_id: String,
    pub output_kind_code: i16,
    pub display_name: String,
    pub x_px: i32,
    pub y_px: i32,
    pub width_px: i32,
    pub height_px: i32,
    pub z_index: i32,
    pub status_code: i16,
    pub props: Json,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}

