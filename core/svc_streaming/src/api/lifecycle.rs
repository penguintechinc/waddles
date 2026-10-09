//! `POST .../configs/{config_id}/start`, `POST .../stop`,
//! `GET .../status` -- pipeline lifecycle, delegated to whichever
//! [`crate::api::engine::SharedEngine`] is wired into the router. Ported
//! from the Python alpha's `blueprints/streaming.py`
//! `start_forwarding`/`stop_forwarding`/`get_status`; the real ffmpeg
//! subprocess (`services/ffmpeg_engine.py`) is not reimplemented here --
//! that is `crate::pipeline::supervisor`'s job (S3). TRANSCODE-token
//! admission (`services/token_ledger_client.py`) **is** reimplemented here
//! (slice S2 of the Rust rewrite, `crate::billing::token_ledger`) since
//! [`start`] is this crate's direct analog of Python's
//! `start_forwarding`: the only handler holding both the caller's bearer
//! JWT and a `streaming_configs.transcode_enabled` row at the same time.

use axum::extract::{Path, State};
use axum::http::header::AUTHORIZATION;
use axum::http::HeaderMap;
use axum::Extension;
use chrono::Utc;
use sea_orm::{ColumnTrait, DatabaseConnection, EntityTrait, QueryFilter};

use crate::billing::token_ledger::TokenLedgerClient;
use crate::db::entities::{streaming_config, streaming_target};
use crate::error::ApiError;
use crate::http::auth::AuthenticatedClaims;
use crate::http::AppState;
use crate::pipeline::{
    AudioCodec, InputSpec, ObjectStoreRef, OutputSpec, PipelineError, PipelineSpec,
    TranscodeProfile, VideoCodec,
};
use crate::store::SecretRef;

use super::common::{fetch_config, pipeline_id_for_config};
use super::dto::PipelineStatusDto;
use super::engine::SharedEngine;
use super::extract::DbConn;
use super::response::ApiSuccess;
use super::tenancy::assert_tenant_owns_community;

fn map_pipeline_error(err: PipelineError) -> ApiError {
    match err {
        PipelineError::NotFound(id) => ApiError::NotFound(format!("pipeline {id} not found")),
        PipelineError::InvalidSpec(msg) => ApiError::BadRequest(msg),
        PipelineError::Unimplemented(what) => ApiError::Unimplemented(what.to_string()),
        // Not a true failure (see `PipelineError::NoFfmpegNeeded`'s own doc
        // comment) but this API layer does not orchestrate the RTP-direct
        // forward path it signals -- reported as not-yet-implemented rather
        // than silently claiming success.
        PipelineError::NoFfmpegNeeded => ApiError::Unimplemented(
            "pipeline requires no ffmpeg process (pure RTP forward); not orchestrated at the API layer yet"
                .into(),
        ),
        PipelineError::Unsupported(what) => {
            ApiError::Unimplemented(format!("{what} is not supported yet"))
        }
        PipelineError::Other(err) => ApiError::Internal(err),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn map_pipeline_error_covers_every_variant() {
        assert!(matches!(
            map_pipeline_error(PipelineError::NotFound(uuid::Uuid::nil())),
            ApiError::NotFound(_)
        ));
        assert!(matches!(
            map_pipeline_error(PipelineError::InvalidSpec("bad spec".into())),
            ApiError::BadRequest(_)
        ));
        assert!(matches!(
            map_pipeline_error(PipelineError::Unimplemented("x")),
            ApiError::Unimplemented(_)
        ));
        assert!(matches!(
            map_pipeline_error(PipelineError::NoFfmpegNeeded),
            ApiError::Unimplemented(_)
        ));
        assert!(matches!(
            map_pipeline_error(PipelineError::Unsupported("multi-input")),
            ApiError::Unimplemented(_)
        ));
        assert!(matches!(
            map_pipeline_error(PipelineError::Other(anyhow::anyhow!("boom"))),
            ApiError::Internal(_)
        ));
    }
}

/// Builds the [`PipelineSpec`] for `config` from its DB row plus its
/// enabled `streaming_targets`. `source_url` is mapped to a single
/// [`InputSpec::Pull`] (migration 079 stores one ingest URL per config,
/// not a per-protocol stream key/token) and every enabled target becomes
/// an [`OutputSpec::RtmpPush`] (the only protocol `streaming_targets`
/// models today -- `platform` distinguishes destinations like
/// twitch/youtube/facebook/custom, not RTMP vs SRT). `video` is resolved
/// by the caller ([`resolve_transcode_video`]) before this is called --
/// building the spec itself never touches the token ledger.
async fn build_pipeline_spec(
    db: &DatabaseConnection,
    tenant: &str,
    community_id: i32,
    config: &streaming_config::Model,
    video: VideoCodec,
) -> Result<PipelineSpec, ApiError> {
    let targets = streaming_target::Entity::find()
        .filter(streaming_target::Column::ConfigId.eq(config.id))
        .filter(streaming_target::Column::Enabled.eq(true))
        .all(db)
        .await
        .map_err(|err| ApiError::Internal(err.into()))?;

    let mut outputs = Vec::with_capacity(targets.len() + 1);
    for target in targets {
        let url_secret_ref: SecretRef =
            serde_json::from_str(&target.forward_url).map_err(|err| {
                ApiError::Internal(anyhow::anyhow!(
                    "stored target {} has a non-secret_ref forward_url: {err}",
                    target.id
                ))
            })?;
        outputs.push(OutputSpec::RtmpPush { url_secret_ref });
    }
    if config.record_enabled {
        outputs.push(OutputSpec::Record {
            profile: "default".into(),
            target: ObjectStoreRef {
                store: "default".into(),
                prefix: format!("{tenant}/{community_id}"),
            },
        });
    }

    Ok(PipelineSpec {
        id: pipeline_id_for_config(config.id),
        tenant: tenant.to_string(),
        community_id: community_id.to_string(),
        inputs: vec![InputSpec::Pull {
            url: config.source_url.clone(),
        }],
        profiles: vec![TranscodeProfile {
            name: "default".into(),
            video,
            audio: AudioCodec::Copy,
            resolution: None,
            fps: None,
        }],
        outputs,
    })
}

/// Extracts the raw `Bearer` token from the request's `Authorization`
/// header for pass-through to hub-api's token ledger -- [`AuthenticatedClaims`]
/// already validated this same header and decoded it into
/// `crate::http::auth::Claims`, but does not retain the raw token string,
/// so [`start`] re-reads the header directly rather than widening that
/// extractor's public shape. Returns
/// `None` if the header is missing/malformed, which should not happen
/// (axum's extractor ordering means [`AuthenticatedClaims`] already
/// rejected the request if so) -- treated as "no admission possible",
/// falling back to passthrough rather than panicking.
fn raw_bearer_token(headers: &HeaderMap) -> Option<&str> {
    headers
        .get(AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
}

/// TRANSCODE admission (BLOCK-WITH-FALLBACK, ported from the Python
/// alpha's `services/streaming_service.py::start_forwarding`): if
/// `config.transcode_enabled` is false, no ledger call is made and this
/// always resolves to [`VideoCodec::Copy`] (matching Python's "the debit
/// is only attempted `if config_row.transcode_enabled`"). Otherwise
/// attempts a real token debit; an affordable debit returns the
/// transcoded ([`VideoCodec::H264`]) profile, while an unaffordable one
/// (`insufficient_balance`) OR an unreachable ledger
/// (`ledger_unavailable`) falls back to passthrough -- `start()` still
/// succeeds and the stream still starts, just without transcoding. Never
/// returns an `Err`: a billing/entitlement check must never take down a
/// live-stream start.
async fn resolve_transcode_video(
    token_ledger: &TokenLedgerClient,
    hub_api_url: &str,
    bearer_token: Option<&str>,
    transcode_token_cost: i64,
    transcode_product_key: &str,
    community_id: i32,
    config: &streaming_config::Model,
) -> VideoCodec {
    if !config.transcode_enabled {
        return VideoCodec::Copy;
    }

    let Some(bearer_token) = bearer_token else {
        // Should not happen (see `raw_bearer_token`'s doc comment) -- a
        // missing bearer at this point means admission cannot be
        // attempted at all, so fail closed on the ledger call the same
        // way an unreachable ledger would: fall back to passthrough
        // rather than guessing an entitlement this handler can't verify.
        tracing::error!(
            community_id,
            config_id = config.id,
            "lifecycle: transcode admission requested but no bearer token present on the request, falling back to passthrough"
        );
        return VideoCodec::Copy;
    };

    let reference = format!("stream:{}:{}", config.id, Utc::now().to_rfc3339());
    let result = token_ledger
        .debit_transcoding_tokens(
            hub_api_url,
            bearer_token,
            community_id,
            transcode_token_cost,
            transcode_product_key,
            &reference,
        )
        .await;

    if result.ok {
        tracing::info!(
            community_id,
            config_id = config.id,
            balance_after = result.balance_after,
            "lifecycle: transcode admission granted"
        );
        VideoCodec::H264 {
            preset: "veryfast".into(),
            crf: None,
            bitrate_kbps: Some(config.transcode_bitrate_kbps.max(0) as u32),
        }
    } else {
        tracing::warn!(
            community_id,
            config_id = config.id,
            blocked_reason = result.blocked_reason.as_deref(),
            "lifecycle: transcode admission denied or unreachable, falling back to passthrough"
        );
        VideoCodec::Copy
    }
}

/// `POST .../configs/{config_id}/start`.
#[utoipa::path(
    post,
    path = "/communities/{community_id}/streaming/configs/{config_id}/start",
    params(
        ("community_id" = i32, Path, description = "Community ID"),
        ("config_id" = i32, Path, description = "Stream config ID"),
    ),
    responses(
        (status = 200, description = "Pipeline status after start", body = PipelineStatusDto),
        (status = 404, description = "Config not found"),
        (status = 501, description = "Pipeline engine not yet implemented (S3)"),
    ),
    tag = "streaming"
)]
pub async fn start(
    Path((community_id, config_id)): Path<(i32, i32)>,
    AuthenticatedClaims(claims): AuthenticatedClaims,
    State(state): State<AppState>,
    headers: HeaderMap,
    DbConn(db): DbConn,
    Extension(engine): Extension<SharedEngine>,
) -> Result<ApiSuccess<PipelineStatusDto>, ApiError> {
    assert_tenant_owns_community(&db, &claims.tenant, community_id).await?;
    let config = fetch_config(&db, community_id, config_id).await?;
    let video = resolve_transcode_video(
        &state.token_ledger,
        &state.config.cli.hub_api_url,
        raw_bearer_token(&headers),
        state.config.cli.transcode_token_cost,
        &state.config.cli.transcode_product_key,
        community_id,
        &config,
    )
    .await;
    let spec = build_pipeline_spec(&db, &claims.tenant, community_id, &config, video).await?;
    let handle = engine.start(spec).await.map_err(map_pipeline_error)?;
    let status = engine.status(handle.id).await.map_err(map_pipeline_error)?;
    Ok(ApiSuccess::ok(status.into()))
}

/// `POST .../configs/{config_id}/stop` -- idempotent: stopping an
/// already-stopped or unknown pipeline is not an error (mirrors
/// [`crate::pipeline::PipelineEngine::stop`]'s own contract).
#[utoipa::path(
    post,
    path = "/communities/{community_id}/streaming/configs/{config_id}/stop",
    params(
        ("community_id" = i32, Path, description = "Community ID"),
        ("config_id" = i32, Path, description = "Stream config ID"),
    ),
    responses(
        (status = 200, description = "Pipeline status after stop", body = PipelineStatusDto),
        (status = 404, description = "Config not found"),
    ),
    tag = "streaming"
)]
pub async fn stop(
    Path((community_id, config_id)): Path<(i32, i32)>,
    AuthenticatedClaims(claims): AuthenticatedClaims,
    DbConn(db): DbConn,
    Extension(engine): Extension<SharedEngine>,
) -> Result<ApiSuccess<PipelineStatusDto>, ApiError> {
    assert_tenant_owns_community(&db, &claims.tenant, community_id).await?;
    let config = fetch_config(&db, community_id, config_id).await?;
    let id = pipeline_id_for_config(config.id);
    engine.stop(id).await.map_err(map_pipeline_error)?;
    Ok(ApiSuccess::ok(PipelineStatusDto {
        id,
        state: "stopped".to_string(),
        detail: None,
    }))
}

/// `GET .../configs/{config_id}/status`.
#[utoipa::path(
    get,
    path = "/communities/{community_id}/streaming/configs/{config_id}/status",
    params(
        ("community_id" = i32, Path, description = "Community ID"),
        ("config_id" = i32, Path, description = "Stream config ID"),
    ),
    responses(
        (status = 200, description = "Current pipeline status", body = PipelineStatusDto),
        (status = 404, description = "Config or pipeline not found"),
    ),
    tag = "streaming"
)]
pub async fn status(
    Path((community_id, config_id)): Path<(i32, i32)>,
    AuthenticatedClaims(claims): AuthenticatedClaims,
    DbConn(db): DbConn,
    Extension(engine): Extension<SharedEngine>,
) -> Result<ApiSuccess<PipelineStatusDto>, ApiError> {
    assert_tenant_owns_community(&db, &claims.tenant, community_id).await?;
    let config = fetch_config(&db, community_id, config_id).await?;
    let id = pipeline_id_for_config(config.id);
    let status = engine.status(id).await.map_err(map_pipeline_error)?;
    Ok(ApiSuccess::ok(status.into()))
}
