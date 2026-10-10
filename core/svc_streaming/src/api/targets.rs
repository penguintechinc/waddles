//! `GET/POST /communities/{community_id}/streaming/configs/{config_id}/targets`,
//! `DELETE /communities/{community_id}/streaming/targets/{target_id}` --
//! forward-target CRUD. Ported from the Python alpha's
//! `blueprints/streaming.py` `list_targets`/`add_target`/`remove_target`,
//! with SSRF validation (`services/url_guard.py`'s
//! `validate_outbound_url`) replaced by the stronger secret_ref-only
//! contract: a raw destination URL is never accepted in the first place,
//! so there is nothing to SSRF-validate at this layer -- see
//! `crate::api::dto::AddTargetRequest`.

use axum::extract::Path;
use axum::http::StatusCode;

use sea_orm::{ActiveModelTrait, ColumnTrait, EntityTrait, QueryFilter, Set};

use crate::db::entities::streaming_target;
use crate::error::ApiError;
use crate::http::auth::AuthenticatedClaims;
use crate::spec_builder::validate_new_target;

use super::common::{
    canonical_audio_codec, canonical_protocol, canonical_video_codec, enabled_targets, fetch_config,
};
use super::dto::{AddTargetRequest, SecretRefDto, StreamingTargetDto};
use super::extract::{DbConn, ValidatedJson};
use super::response::ApiSuccess;
use super::tenancy::assert_tenant_owns_community;

const VALID_PLATFORMS: &[&str] = &["twitch", "youtube", "facebook", "custom"];

fn target_dto(model: streaming_target::Model) -> Result<StreamingTargetDto, ApiError> {
    let url_secret_ref: crate::store::SecretRef = serde_json::from_str(&model.forward_url)
        .map_err(|err| {
            ApiError::Internal(anyhow::anyhow!(
                "stored target {} has a non-secret_ref forward_url: {err}",
                model.id
            ))
        })?;
    Ok(StreamingTargetDto {
        id: model.id,
        config_id: model.config_id,
        platform: model.platform,
        url_secret_ref: SecretRefDto::from(url_secret_ref),
        enabled: model.enabled,
        protocol: model.protocol,
        video_codec: model.video_codec,
        audio_codec: model.audio_codec,
    })
}

/// `GET .../configs/{config_id}/targets`.
#[utoipa::path(
    get,
    path = "/communities/{community_id}/streaming/configs/{config_id}/targets",
    params(
        ("community_id" = i32, Path, description = "Community ID"),
        ("config_id" = i32, Path, description = "Stream config ID"),
    ),
    responses((status = 200, description = "Forward targets", body = [StreamingTargetDto])),
    tag = "streaming"
)]
pub async fn list_targets(
    Path((community_id, config_id)): Path<(i32, i32)>,
    AuthenticatedClaims(claims): AuthenticatedClaims,
    DbConn(db): DbConn,
) -> Result<ApiSuccess<Vec<StreamingTargetDto>>, ApiError> {
    assert_tenant_owns_community(&db, &claims.tenant, community_id).await?;
    fetch_config(&db, community_id, config_id).await?;
    let rows = streaming_target::Entity::find()
        .filter(streaming_target::Column::ConfigId.eq(config_id))
        .all(&db)
        .await
        .map_err(|err| ApiError::Internal(err.into()))?;
    let dtos = rows
        .into_iter()
        .map(target_dto)
        .collect::<Result<Vec<_>, _>>()?;
    Ok(ApiSuccess::ok(dtos))
}

/// `POST .../configs/{config_id}/targets` -- rejects any body carrying a
/// raw destination URL/key; only a `url_secret_ref` is ever persisted.
#[utoipa::path(
    post,
    path = "/communities/{community_id}/streaming/configs/{config_id}/targets",
    params(
        ("community_id" = i32, Path, description = "Community ID"),
        ("config_id" = i32, Path, description = "Stream config ID"),
    ),
    request_body = AddTargetRequest,
    responses(
        (status = 201, description = "Target added", body = StreamingTargetDto),
        (status = 400, description = "Invalid platform, protocol or codec, a codec the protocol cannot carry (e.g. h265/av1 over RTMP), or a body shape error (e.g. inline URL)"),
        (status = 404, description = "Config not found"),
    ),
    tag = "streaming"
)]
pub async fn add_target(
    Path((community_id, config_id)): Path<(i32, i32)>,
    AuthenticatedClaims(claims): AuthenticatedClaims,
    DbConn(db): DbConn,
    ValidatedJson(body): ValidatedJson<AddTargetRequest>,
) -> Result<ApiSuccess<StreamingTargetDto>, ApiError> {
    assert_tenant_owns_community(&db, &claims.tenant, community_id).await?;
    let config = fetch_config(&db, community_id, config_id).await?;

    if !VALID_PLATFORMS.contains(&body.platform.as_str()) {
        return Err(ApiError::BadRequest(format!(
            "platform must be one of {VALID_PLATFORMS:?}"
        )));
    }

    let protocol = canonical_protocol(&body.protocol)?;
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

    let secret_ref: crate::store::SecretRef = body.url_secret_ref.into();
    let forward_url =
        serde_json::to_string(&secret_ref).map_err(|err| ApiError::Internal(err.into()))?;

    // Reject a codec the target's container cannot carry (e.g. h265/av1 on
    // an RTMP target) now, with the reason, instead of letting ffmpeg's tee
    // muxer drop the destination silently at publish time.
    let candidate = streaming_target::Model {
        id: 0,
        config_id,
        platform: body.platform.clone(),
        forward_url: forward_url.clone(),
        enabled: true,
        protocol: protocol.clone(),
        video_codec: video_codec.clone(),
        audio_codec: audio_codec.clone(),
    };
    validate_new_target(&config, &enabled_targets(&db, config_id).await?, &candidate)?;

    let active = streaming_target::ActiveModel {
        config_id: Set(config_id),
        platform: Set(body.platform),
        forward_url: Set(forward_url),
        enabled: Set(true),
        protocol: Set(protocol),
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
        target_dto(model)?,
    ))
}

/// `DELETE /communities/{community_id}/streaming/targets/{target_id}`.
#[utoipa::path(
    delete,
    path = "/communities/{community_id}/streaming/targets/{target_id}",
    params(
        ("community_id" = i32, Path, description = "Community ID"),
        ("target_id" = i32, Path, description = "Forward target ID"),
    ),
    responses(
        (status = 200, description = "Deleted"),
        (status = 404, description = "Not found"),
    ),
    tag = "streaming"
)]
pub async fn remove_target(
    Path((community_id, target_id)): Path<(i32, i32)>,
    AuthenticatedClaims(claims): AuthenticatedClaims,
    DbConn(db): DbConn,
) -> Result<ApiSuccess<()>, ApiError> {
    assert_tenant_owns_community(&db, &claims.tenant, community_id).await?;
    let target = streaming_target::Entity::find_by_id(target_id)
        .one(&db)
        .await
        .map_err(|err| ApiError::Internal(err.into()))?
        .ok_or_else(|| ApiError::NotFound(format!("target {target_id} not found")))?;
    // `target_id` alone does not prove the target's config belongs to
    // `community_id` -- confirm via the config it references.
    fetch_config(&db, community_id, target.config_id).await?;
    let active: streaming_target::ActiveModel = target.into();
    active
        .delete(&db)
        .await
        .map_err(|err| ApiError::Internal(err.into()))?;
    Ok(ApiSuccess::ok(()))
}
