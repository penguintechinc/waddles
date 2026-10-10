//! SeaORM entity for `caption_events`
//! (`config/postgres/migrations/102_caption_events_rust_port.sql`) -- the
//! short-lived history behind the caption overlay's reconnect replay (the
//! Rust port of `core/browser_source_core_module`'s `caption_events` use).
//!
//! The column types here are the ones migration 102 reconciles an existing
//! 007-shaped table to (`BIGINT` ids, `DOUBLE PRECISION` confidence):
//! sqlx decodes strictly by SQL type, so an INT4 `community_id` or NUMERIC
//! `confidence_score` would fail at runtime rather than coerce.
//!
//! There is deliberately NO username/display-name column: only `user_ref`,
//! the tenant-tokenized UUID of the author, is stored (`critical-rules.md`
//! PII Tokenization). The legacy `username` column from migration 007 still
//! exists in an upgraded database for the Python writer, but this entity
//! never selects or writes it.

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, DeriveEntityModel)]
#[sea_orm(table_name = "caption_events")]
pub struct Model {
    #[sea_orm(primary_key)]
    pub id: i64,
    pub community_id: i64,
    pub user_ref: Option<Uuid>,
    pub platform: String,
    pub original_message: String,
    pub translated_message: Option<String>,
    pub detected_language: Option<String>,
    pub target_language: Option<String>,
    pub confidence_score: Option<f64>,
    pub created_at: DateTimeWithTimeZone,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}
