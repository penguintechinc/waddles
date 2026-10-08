//! SeaORM entity for `overlay_surfaces`
//! (`config/postgres/migrations/073_svc_presentation_overlays.sql`) --
//! which [`overlay_schema::Surface`]s are enabled per community, plus a
//! per-surface styling/theme override. Ported field-for-field from the
//! Python alpha's pydal binding
//! (`core/svc_presentation/services/schema.py::bind_presentation_tables`).
//! `created_at`/`updated_at` are intentionally omitted -- no P1 code path
//! surfaces them, matching `core/svc_streaming/src/db/entities/
//! streaming_config.rs`'s same precedent (sidesteps `TIMESTAMPTZ` vs
//! sqlite `TEXT` portability concerns for in-memory sqlite tests).

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
#[sea_orm(table_name = "overlay_surfaces")]
pub struct Model {
    #[sea_orm(primary_key)]
    pub id: i32,
    pub community_id: i32,
    /// Wire form matches [`overlay_schema::Surface::as_str`] exactly
    /// (`"full_screen"`, `"media"`, ...) -- stored as free text rather than
    /// a DB-level enum so a new `Surface` variant (#458) never needs a
    /// migration here.
    pub surface: String,
    pub enabled: bool,
    /// Per-surface styling/theme override (`JSONB`).
    pub config: Json,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}
