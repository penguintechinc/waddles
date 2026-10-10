//! SeaORM entity for `streaming_targets`
//! (`config/postgres/migrations/079_svc_streaming.sql`) -- this service's
//! own table.
//!
//! `forward_url` (`VARCHAR(1024)`) never holds a raw destination URL with
//! an embedded stream key: the API layer
//! (`crate::api::targets`/`crate::api::dto`) only accepts a
//! `url_secret_ref` in request bodies and persists
//! `serde_json::to_string(&SecretRef)` into this column -- migration 079
//! predates the secret_ref model and has no dedicated column for it, so the
//! existing `VARCHAR(1024)` column is reused to hold the serialized
//! reference rather than a resolved secret, per `rules/client.md` Secrets &
//! Credentials.

use sea_orm::entity::prelude::*;

#[derive(Clone, Debug, PartialEq, Eq, DeriveEntityModel)]
#[sea_orm(table_name = "streaming_targets")]
pub struct Model {
    #[sea_orm(primary_key)]
    pub id: i32,
    pub config_id: i32,
    pub platform: String,
    /// Serialized [`crate::store::SecretRef`] JSON, never a raw URL.
    pub forward_url: String,
    pub enabled: bool,
    /// Push protocol (`rtmp`/`srt`, `crate::pipeline::codec::TargetProtocol`):
    /// decides the container (FLV vs MPEG-TS) and so which codecs the
    /// target can carry. Added by alembic `0056_streaming_codec_columns`.
    pub protocol: String,
    /// Per-target video codec override; `NULL` = the protocol default
    /// (RTMP: `h264`, SRT: the config's `video_codec`).
    pub video_codec: Option<String>,
    /// Per-target audio codec override; `NULL` = the config's `audio_codec`.
    pub audio_codec: Option<String>,
}

#[derive(Copy, Clone, Debug, EnumIter, DeriveRelation)]
pub enum Relation {}

impl ActiveModelBehavior for ActiveModel {}
