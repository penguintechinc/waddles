//! `GET/POST /communities/{community_id}/streaming/configs`,
//! `GET/PUT/DELETE .../configs/{config_id}` -- stream-configuration CRUD.
//! Ported from the Python alpha's `blueprints/streaming.py`
//! `get_config`/`set_config` (collapsed into this chunk's more RESTful
//! list/create/get/update/delete shape per this chunk's task spec).

use axum::extract::Path;
use axum::http::StatusCode;

use sea_orm::{ActiveModelTrait, ColumnTrait, EntityTrait, QueryFilter, Set};

use crate::db::entities::streaming_config;
use crate::error::ApiError;
use crate::http::auth::AuthenticatedClaims;
use crate::spec_builder::validate_config;

use super::common::{canonical_audio_codec, canonical_video_codec, enabled_targets, fetch_config};
use super::dto::{CreateConfigRequest, StreamingConfigDto, UpdateConfigRequest};
use super::extract::{DbConn, ValidatedJson};
use super::response::ApiSuccess;
use super::tenancy::assert_tenant_owns_community;

const VALID_SOURCE_TYPES: &[&str] = &["rtmp", "hls"];

fn validate_source_type(value: &str) -> Result<(), ApiError> {
    if VALID_SOURCE_TYPES.contains(&value) {
        Ok(())
    } else {
        Err(ApiError::BadRequest(format!(
            "source_type must be one of {VALID_SOURCE_TYPES:?}, got {value:?}"
        )))
    }
}

fn validate_bitrate(kbps: i32) -> Result<(), ApiError> {
    if kbps > 0 {
        Ok(())
    } else {
        Err(ApiError::BadRequest(
            "transcode_bitrate_kbps must be positive".into(),
        ))
    }
}

/// `GET /communities/{community_id}/streaming/configs` -- list this
/// community's stream configs (at most one today: `community_id` is
/// `UNIQUE` on `streaming_configs`, migration 079).
#[utoipa::path(
    get,
    path = "/communities/{community_id}/streaming/configs",
    params(("community_id" = i32, Path, description = "Community ID")),
    responses((status = 200, description = "Stream configs for the community", body = [StreamingConfigDto])),
    tag = "streaming"
)]
pub async fn list_configs(
    Path(community_id): Path<i32>,
    AuthenticatedClaims(claims): AuthenticatedClaims,
    DbConn(db): DbConn,
) -> Result<ApiSuccess<Vec<StreamingConfigDto>>, ApiError> {
    assert_tenant_owns_community(&db, &claims.tenant, community_id).await?;
    let rows = streaming_config::Entity::find()
        .filter(streaming_config::Column::CommunityId.eq(community_id))
        .all(&db)
        .await
        .map_err(|err| ApiError::Internal(err.into()))?;
    Ok(ApiSuccess::ok(
        rows.into_iter().map(StreamingConfigDto::from).collect(),
    ))
}

/// `POST /communities/{community_id}/streaming/configs` -- create the
/// community's stream config. `400` if one already exists (`community_id`
/// is `UNIQUE`) -- use `PUT .../configs/{id}` to change it instead.
#[utoipa::path(
    post,
    path = "/communities/{community_id}/streaming/configs",
    params(("community_id" = i32, Path, description = "Community ID")),
    request_body = CreateConfigRequest,
    responses((status = 201, description = "Config created", body = StreamingConfigDto)),
    tag = "streaming"
)]
pub async fn create_config(
    Path(community_id): Path<i32>,
    AuthenticatedClaims(claims): AuthenticatedClaims,
    DbConn(db): DbConn,
    ValidatedJson(body): ValidatedJson<CreateConfigRequest>,
) -> Result<ApiSuccess<StreamingConfigDto>, ApiError> {
    assert_tenant_owns_community(&db, &claims.tenant, community_id).await?;
    validate_source_type(&body.source_type)?;
    validate_bitrate(body.transcode_bitrate_kbps)?;
    let video_codec = canonical_video_codec(&body.video_codec)?;
    let audio_codec = canonical_audio_codec(&body.audio_codec)?;
    // Codec selection needs transcoding; reject the combination now rather
    // than at the first publish.
    validate_config(
        &streaming_config::Model {
            id: 0,
            community_id,
            source_url: String::new(),
            source_type: body.source_type.clone(),
            enabled: true,
            record_enabled: body.record_enabled,
            transcode_enabled: body.transcode_enabled,
            transcode_bitrate_kbps: body.transcode_bitrate_kbps,
            video_codec: video_codec.clone(),
            audio_codec: audio_codec.clone(),
        },
        &[],
    )?;

    let existing = streaming_config::Entity::find()
        .filter(streaming_config::Column::CommunityId.eq(community_id))
        .one(&db)
        .await
        .map_err(|err| ApiError::Internal(err.into()))?;
    if existing.is_some() {
        return Err(ApiError::BadRequest(
            "a stream configuration already exists for this community".into(),
        ));
    }

    let active = streaming_config::ActiveModel {
        community_id: Set(community_id),
        source_url: Set(body.source_url),
        source_type: Set(body.source_type),
        enabled: Set(true),
        record_enabled: Set(body.record_enabled),
        transcode_enabled: Set(body.transcode_enabled),
        transcode_bitrate_kbps: Set(body.transcode_bitrate_kbps),
        video_codec: Set(video_codec),
        audio_codec: Set(audio_codec),
        ..Default::default()
    };
    let model = active
        .insert(&db)
        .await
        .map_err(|err| ApiError::Internal(err.into()))?;
    Ok(ApiSuccess::with_status(
        StatusCode::CREATED,
        StreamingConfigDto::from(model),
    ))
}

/// `GET /communities/{community_id}/streaming/configs/{config_id}`.
#[utoipa::path(
    get,
    path = "/communities/{community_id}/streaming/configs/{config_id}",
    params(
        ("community_id" = i32, Path, description = "Community ID"),
        ("config_id" = i32, Path, description = "Stream config ID"),
    ),
    responses(
        (status = 200, description = "Stream config", body = StreamingConfigDto),
        (status = 404, description = "Not found"),
    ),
    tag = "streaming"
)]
pub async fn get_config(
    Path((community_id, config_id)): Path<(i32, i32)>,
    AuthenticatedClaims(claims): AuthenticatedClaims,
    DbConn(db): DbConn,
) -> Result<ApiSuccess<StreamingConfigDto>, ApiError> {
    assert_tenant_owns_community(&db, &claims.tenant, community_id).await?;
    let model = fetch_config(&db, community_id, config_id).await?;
    Ok(ApiSuccess::ok(StreamingConfigDto::from(model)))
}

/// `PUT /communities/{community_id}/streaming/configs/{config_id}`.
#[utoipa::path(
    put,
    path = "/communities/{community_id}/streaming/configs/{config_id}",
    params(
        ("community_id" = i32, Path, description = "Community ID"),
        ("config_id" = i32, Path, description = "Stream config ID"),
    ),
    request_body = UpdateConfigRequest,
    responses(
        (status = 200, description = "Updated config", body = StreamingConfigDto),
        (status = 404, description = "Not found"),
    ),
    tag = "streaming"
)]
pub async fn update_config(
    Path((community_id, config_id)): Path<(i32, i32)>,
    AuthenticatedClaims(claims): AuthenticatedClaims,
    DbConn(db): DbConn,
    ValidatedJson(body): ValidatedJson<UpdateConfigRequest>,
) -> Result<ApiSuccess<StreamingConfigDto>, ApiError> {
    assert_tenant_owns_community(&db, &claims.tenant, community_id).await?;
    let model = fetch_config(&db, community_id, config_id).await?;

    if let Some(ref st) = body.source_type {
        validate_source_type(st)?;
    }
    if let Some(kbps) = body.transcode_bitrate_kbps {
        validate_bitrate(kbps)?;
    }
    let video_codec = body
        .video_codec
        .as_deref()
        .map(canonical_video_codec)
        .transpose()?;
    let audio_codec = body
        .audio_codec
        .as_deref()
        .map(canonical_audio_codec)
        .transpose()?;

    // Validate the *merged* config against the targets that already hang
    // off it: switching to AV1 must not silently break an SRT target that
    // inherits the codec, and a codec change needs transcoding on.
    let mut merged = model.clone();
    if let Some(v) = &video_codec {
        merged.video_codec = v.clone();
    }
    if let Some(v) = &audio_codec {
        merged.audio_codec = v.clone();
    }
    if let Some(v) = body.transcode_enabled {
        merged.transcode_enabled = v;
    }
    validate_config(&merged, &enabled_targets(&db, model.id).await?)?;

    let mut active: streaming_config::ActiveModel = model.into();
    if let Some(v) = body.source_url {
        active.source_url = Set(v);
    }
    if let Some(v) = body.source_type {
        active.source_type = Set(v);
    }
    if let Some(v) = body.enabled {
        active.enabled = Set(v);
    }
    if let Some(v) = body.record_enabled {
        active.record_enabled = Set(v);
    }
    if let Some(v) = body.transcode_enabled {
        active.transcode_enabled = Set(v);
    }
    if let Some(v) = body.transcode_bitrate_kbps {
        active.transcode_bitrate_kbps = Set(v);
    }
    if let Some(v) = video_codec {
        active.video_codec = Set(v);
    }
    if let Some(v) = audio_codec {
        active.audio_codec = Set(v);
    }

    let updated = active
        .update(&db)
        .await
        .map_err(|err| ApiError::Internal(err.into()))?;
    Ok(ApiSuccess::ok(StreamingConfigDto::from(updated)))
}

/// `DELETE /communities/{community_id}/streaming/configs/{config_id}`.
/// Cascades to `streaming_targets` at the DB level (migration 079's `ON
/// DELETE CASCADE`).
#[utoipa::path(
    delete,
    path = "/communities/{community_id}/streaming/configs/{config_id}",
    params(
        ("community_id" = i32, Path, description = "Community ID"),
        ("config_id" = i32, Path, description = "Stream config ID"),
    ),
    responses(
        (status = 200, description = "Deleted"),
        (status = 404, description = "Not found"),
    ),
    tag = "streaming"
)]
pub async fn delete_config(
    Path((community_id, config_id)): Path<(i32, i32)>,
    AuthenticatedClaims(claims): AuthenticatedClaims,
    DbConn(db): DbConn,
) -> Result<ApiSuccess<()>, ApiError> {
    assert_tenant_owns_community(&db, &claims.tenant, community_id).await?;
    let model = fetch_config(&db, community_id, config_id).await?;
    let active: streaming_config::ActiveModel = model.into();
    active
        .delete(&db)
        .await
        .map_err(|err| ApiError::Internal(err.into()))?;
    Ok(ApiSuccess::ok(()))
}
