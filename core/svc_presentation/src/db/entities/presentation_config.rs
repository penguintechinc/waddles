//! SeaORM entity for `presentation_config`
//! (`config/postgres/migrations/073_svc_presentation_overlays.sql`) --
//! per-community global theme/palette plus the Music Station toggle and
//! crawler scroll speed. Ported field-for-field from the Python alpha's
//! pydal binding (`core/svc_presentation/services/schema.py::
//! bind_presentation_tables`).

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
#[sea_orm(table_name = "presentation_config")]
pub struct Model {
    #[sea_orm(primary_key)]
    pub id: i32,
    pub community_id: i32,
    pub theme: String,
    pub primary_color: Option<String>,
    pub secondary_color: Option<String>,
    pub font_family: Option<String>,
    pub music_enabled: bool,
    pub crawler_speed_seconds: i32,
    pub config: Json,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}
