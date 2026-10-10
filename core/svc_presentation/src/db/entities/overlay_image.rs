//! SeaORM entity for `overlay_images`
//! (`config/postgres/migrations/101_overlay_images.sql`) -- metadata for
//! images uploaded through `crate::images::upload` (P6) and resolved by
//! `crate::images::render` (P9). The image bytes themselves live only in
//! SeaweedFS (`object_key`); this row is purely metadata + the
//! community-scoping boundary -- see `crate::images::asset_store`'s module
//! doc for why every lookup filters on `community_id` in addition to
//! `asset_id`.

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, Eq, DeriveEntityModel)]
#[sea_orm(table_name = "overlay_images")]
pub struct Model {
    #[sea_orm(primary_key)]
    pub id: i64,
    pub community_id: i64,
    pub asset_id: Uuid,
    pub object_key: String,
    pub content_type: String,
    pub size_bytes: i64,
    pub sha256: String,
    pub position_x: Option<i32>,
    pub position_y: Option<i32>,
    pub width: Option<i32>,
    pub height: Option<i32>,
    pub duration_ms: Option<i64>,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}
