//! `/api/v1/internal/streaming/*` -- service-to-service routes gated by
//! [`crate::http::auth::ServiceKey`] (`X-Service-Key`), not a user JWT.
//!
//! **Known gap (not fixable within this chunk's file ownership):**
//! `src/http/mod.rs` wraps the *entire* `.nest("/api/v1", crate::api::router())`
//! subtree in `route_layer(auth::require_auth)` (axum's `route_layer`
//! layers each top-level registered route -- including a `nest` -- as one
//! opaque endpoint, so it does not stop at this module's boundary the way
//! `crate::http::auth`'s own doc comment claims). That means these routes
//! currently also require a valid user JWT in addition to `X-Service-Key`,
//! which defeats the purpose of a pure service-to-service credential.
//! Fixing it means exempting `/api/v1/internal/*` from that layer in
//! `src/http/mod.rs`, which is outside `src/api/**`/`src/db/**`. The
//! `ServiceKey` extraction below is still correct and ready for that fix.

use axum::Extension;
use sea_orm::{ColumnTrait, EntityTrait, QueryFilter};
use serde::{Deserialize, Serialize};
use utoipa::ToSchema;

use crate::db::entities::streaming_config;
use crate::error::ApiError;
use crate::http::auth::ServiceKey;
use crate::pipeline::PipelineState;

use super::common::pipeline_id_for_config;
use super::dto::{state_str, PipelineStatusDto};
use super::engine::SharedEngine;
use super::extract::{DbConn, ValidatedJson};
use super::response::ApiSuccess;

/// One running pipeline, for the overlay/webui.
#[derive(Debug, Clone, Serialize, ToSchema)]
pub struct RunningPipelineDto {
    pub community_id: i32,
    pub config_id: i32,
    pub status: PipelineStatusDto,
}

/// `GET /api/v1/internal/streaming/pipelines` -- every pipeline this
/// process currently reports as running/starting/degraded, across all
/// tenants (this is a trusted internal caller, not user-scoped).
/// [`crate::pipeline::PipelineEngine`] has no `list()` method, so this
/// iterates enabled `streaming_configs` and probes each one's derived
/// [`crate::pipeline::PipelineId`] via `status()`, keeping only the ones
/// that resolve to a running state; a status lookup failure (e.g. the
/// production default engine returning `Unimplemented` -- see
/// `crate::api::engine::default_engine`) is logged at DEBUG and simply
/// excluded, not surfaced as a request error.
#[utoipa::path(
    get,
    path = "/internal/streaming/pipelines",
    responses((status = 200, description = "Running pipelines", body = [RunningPipelineDto])),
    tag = "streaming-internal"
)]
pub async fn list_pipelines(
    DbConn(db): DbConn,
    Extension(engine): Extension<SharedEngine>,
    _service: ServiceKey,
) -> Result<ApiSuccess<Vec<RunningPipelineDto>>, ApiError> {
    let configs = streaming_config::Entity::find()
        .filter(streaming_config::Column::Enabled.eq(true))
        .all(&db)
        .await
        .map_err(|err| ApiError::Internal(err.into()))?;

    let mut running = Vec::new();
    for config in configs {
        let id = pipeline_id_for_config(config.id);
        match engine.status(id).await {
            Ok(status)
                if matches!(
                    status.state,
                    PipelineState::Running | PipelineState::Starting | PipelineState::Degraded
                ) =>
            {
                running.push(RunningPipelineDto {
                    community_id: config.community_id,
                    config_id: config.id,
                    status: PipelineStatusDto {
                        id: status.id,
                        state: state_str(status.state).to_string(),
                        detail: status.detail,
                    },
                });
            }
            Ok(_) => {}
            Err(err) => {
                tracing::debug!(
                    config_id = config.id,
                    error = %err,
                    "pipeline status check failed while listing running pipelines"
                );
            }
        }
    }
    Ok(ApiSuccess::ok(running))
}

/// Ingest protocol presenting the stream key -- accepted for forward
/// compatibility; see [`ingest_auth`]'s doc comment for why it does not
/// yet change the lookup.
#[derive(Debug, Deserialize, ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum IngestKind {
    Rtmp,
    Srt,
    Whip,
}

/// `POST /api/v1/internal/streaming/ingest-auth` request body.
#[derive(Deserialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct IngestAuthRequest {
    pub kind: IngestKind,
    pub key: String,
}

/// Hand-written `Debug`: `key` is the presented stream key/token -- a bearer
/// credential -- so a derived `{:?}` of the request would print it. Shows
/// the key's fingerprint instead.
impl std::fmt::Debug for IngestAuthRequest {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("IngestAuthRequest")
            .field("kind", &self.kind)
            .field("key_hash", &crate::redact::fingerprint(&self.key))
            .finish()
    }
}

/// `POST /api/v1/internal/streaming/ingest-auth` response body.
#[derive(Debug, Serialize, ToSchema)]
pub struct IngestAuthResponse {
    pub allowed: bool,
    pub community_id: Option<i32>,
    pub config_id: Option<i32>,
}

/// `POST /api/v1/internal/streaming/ingest-auth` -- called by the ingest
/// listeners (S4 RTMP/S5 SRT/S6 WHIP) to authorize a stream key at connect
/// time. Migration 079 has no dedicated `stream_key` column on
/// `streaming_configs`; `source_url` is the closest existing column and is
/// used as the match target for every `kind` pending a real column (a
/// schema change outside this chunk's file ownership -- SQL migrations are
/// owned elsewhere).
#[utoipa::path(
    post,
    path = "/internal/streaming/ingest-auth",
    request_body = IngestAuthRequest,
    responses((status = 200, description = "Authorization decision", body = IngestAuthResponse)),
    tag = "streaming-internal"
)]
pub async fn ingest_auth(
    DbConn(db): DbConn,
    _service: ServiceKey,
    ValidatedJson(body): ValidatedJson<IngestAuthRequest>,
) -> Result<ApiSuccess<IngestAuthResponse>, ApiError> {
    let _kind = body.kind;
    let config = streaming_config::Entity::find()
        .filter(streaming_config::Column::SourceUrl.eq(body.key))
        .filter(streaming_config::Column::Enabled.eq(true))
        .one(&db)
        .await
        .map_err(|err| ApiError::Internal(err.into()))?;

    let response = match config {
        Some(c) => IngestAuthResponse {
            allowed: true,
            community_id: Some(c.community_id),
            config_id: Some(c.id),
        },
        None => IngestAuthResponse {
            allowed: false,
            community_id: None,
            config_id: None,
        },
    };
    Ok(ApiSuccess::ok(response))
}
