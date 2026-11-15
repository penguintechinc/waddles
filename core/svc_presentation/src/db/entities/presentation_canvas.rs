//! Projection of `presentation_config` onto the canvas column added by alembic 0113.
//!
//! A separate entity (same table) so the long-standing theme entity and its callers keep working
//! unchanged, and so a database that has not applied 0113 yet only breaks the scene routes, never
//! the theming of the existing overlays. The width/height are DERIVED from the code, never stored.

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, Eq, DeriveEntityModel)]
#[sea_orm(table_name = "presentation_config")]
pub struct Model {
    #[sea_orm(primary_key)]
    pub id: i32,
    pub community_id: i32,
    /// 1 = 1080p, 2 = 2k, 3 = 4k.
    pub canvas_resolution_code: i16,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}
