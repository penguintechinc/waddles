//! Shared helpers used by more than one handler module (`configs`,
//! `targets`, `lifecycle`).

use sea_orm::{ColumnTrait, DatabaseConnection, EntityTrait, QueryFilter};
use uuid::Uuid;

use crate::db::entities::{streaming_config, streaming_target};
use crate::error::ApiError;
use crate::pipeline::codec::{AudioChoice, TargetProtocol, VideoFamily};
use crate::pipeline::PipelineId;

/// Loads `streaming_configs` row `config_id`, scoped to `community_id` --
/// a config belonging to a different community (even a real row ID) is
/// reported as [`ApiError::NotFound`], never returned.
pub(crate) async fn fetch_config(
    db: &DatabaseConnection,
    community_id: i32,
    config_id: i32,
) -> Result<streaming_config::Model, ApiError> {
    streaming_config::Entity::find_by_id(config_id)
        .filter(streaming_config::Column::CommunityId.eq(community_id))
        .one(db)
        .await
        .map_err(|err| ApiError::Internal(err.into()))?
        .ok_or_else(|| {
            ApiError::NotFound(format!(
                "stream config {config_id} not found for community {community_id}"
            ))
        })
}

/// Derives a stable [`PipelineId`] from a `streaming_configs.id` so
/// `/start`, `/stop`, and `/status` all address the same running pipeline
/// without a dedicated mapping column -- migration 079 has no
/// `pipeline_id` column on `streaming_configs`/`streaming_sessions`, and
/// adding one is a schema change outside this chunk's file ownership
/// (SQL migrations are owned elsewhere). `Uuid::from_u128` is deterministic
/// and collision-free across the `i32` domain of `streaming_configs.id`.
pub(crate) fn pipeline_id_for_config(config_id: i32) -> PipelineId {
    Uuid::from_u128(config_id as u128)
}

/// Validates a request's `video_codec` and returns its canonical stored
/// spelling (`HEVC` -> `h265`); an unknown codec is a `400`, never a default.
pub(crate) fn canonical_video_codec(raw: &str) -> Result<String, ApiError> {
    raw.parse::<VideoFamily>()
        .map(|family| family.as_str().to_string())
        .map_err(|err| ApiError::BadRequest(err.to_string()))
}

/// Validates a request's `audio_codec` and returns its canonical spelling.
pub(crate) fn canonical_audio_codec(raw: &str) -> Result<String, ApiError> {
    raw.parse::<AudioChoice>()
        .map(|choice| choice.as_str().to_string())
        .map_err(|err| ApiError::BadRequest(err.to_string()))
}

/// Validates a request's target `protocol` and returns its canonical
/// spelling.
pub(crate) fn canonical_protocol(raw: &str) -> Result<String, ApiError> {
    raw.parse::<TargetProtocol>()
        .map(|protocol| protocol.as_str().to_string())
        .map_err(|err| ApiError::BadRequest(err.to_string()))
}

/// The enabled forward targets of `config_id` -- the set a config or
/// target write must stay consistent with (disabled rows never reach an
/// ffmpeg argv).
pub(crate) async fn enabled_targets(
    db: &DatabaseConnection,
    config_id: i32,
) -> Result<Vec<streaming_target::Model>, ApiError> {
    streaming_target::Entity::find()
        .filter(streaming_target::Column::ConfigId.eq(config_id))
        .filter(streaming_target::Column::Enabled.eq(true))
        .all(db)
        .await
        .map_err(|err| ApiError::Internal(err.into()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canonical_helpers_normalize_and_reject() {
        assert_eq!(canonical_video_codec(" HEVC ").unwrap(), "h265");
        assert_eq!(canonical_video_codec("AV1").unwrap(), "av1");
        assert_eq!(canonical_audio_codec("Opus").unwrap(), "opus");
        assert_eq!(canonical_protocol("SRT").unwrap(), "srt");
        for bad in [
            canonical_video_codec("vp9"),
            canonical_audio_codec("flac"),
            canonical_protocol("whip"),
        ] {
            assert!(matches!(bad.unwrap_err(), ApiError::BadRequest(_)));
        }
    }

    #[test]
    fn pipeline_id_for_config_is_deterministic_and_distinct() {
        assert_eq!(pipeline_id_for_config(7), pipeline_id_for_config(7));
        assert_ne!(pipeline_id_for_config(7), pipeline_id_for_config(8));
    }
}
