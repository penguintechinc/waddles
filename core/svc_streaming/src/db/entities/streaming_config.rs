//! SeaORM entity for `streaming_configs`
//! (`config/postgres/migrations/079_svc_streaming.sql`) -- this service's
//! own table. `created_at`/`updated_at` are intentionally omitted: no
//! response DTO surfaces them (matching the Python alpha's
//! `services/streaming_service.StreamConfigDTO`), which sidesteps
//! `TIMESTAMPTZ` vs sqlite `TEXT` portability concerns for the in-memory
//! sqlite DB integration tests use.

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, Eq, DeriveEntityModel)]
#[sea_orm(table_name = "streaming_configs")]
pub struct Model {
    #[sea_orm(primary_key)]
    pub id: i32,
    pub community_id: i32,
    pub source_url: String,
    pub source_type: String,
    pub enabled: bool,
    pub record_enabled: bool,
    pub transcode_enabled: bool,
    pub transcode_bitrate_kbps: i32,
    /// Video codec family used when transcoding (`h264`/`h265`/`av1`,
    /// `crate::pipeline::codec::VideoFamily`). Meaningful only with
    /// `transcode_enabled`; added by alembic `0056_streaming_codec_columns`.
    pub video_codec: String,
    /// Audio handling (`copy`/`aac`/`opus`,
    /// `crate::pipeline::codec::AudioChoice`).
    pub audio_codec: String,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}
