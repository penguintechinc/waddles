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
use axum::http::HeaderMap;
use axum::Extension;
use sea_orm::DatabaseConnection;

use crate::billing::token_ledger::TokenLedgerClient;
use crate::db::entities::streaming_config;
use crate::error::ApiError;
use crate::http::auth::AuthenticatedClaims;
use crate::http::AppState;
use crate::pipeline::{InputSpec, ObjectStoreRef, OutputSpec, PipelineError, PipelineSpec};
use crate::spec_builder::{plan_push_outputs, DEFAULT_PROFILE};

use super::common::{enabled_targets, fetch_config, pipeline_id_for_config};
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
    fn idempotency_key_is_stable_across_retries_and_timestamp_free() {
        let first = admission_idempotency_key(7, 3, "adm-1");
        std::thread::sleep(std::time::Duration::from_millis(5));
        let retry = admission_idempotency_key(7, 3, "adm-1");
        assert_eq!(first, retry);
        assert_eq!(first, "stream:7:3:adm-1");
        assert_ne!(first, admission_idempotency_key(7, 3, "adm-2"));
        assert_ne!(first, admission_idempotency_key(7, 4, "adm-1"));
        assert_ne!(first, admission_idempotency_key(8, 3, "adm-1"));
    }

    #[test]
    fn admission_id_reuses_client_header_and_rejects_unsafe_values() {
        let mut headers = HeaderMap::new();
        headers.insert(ADMISSION_ID_HEADER, "adm-42".parse().unwrap());
        assert_eq!(admission_id_from_headers(&headers), "adm-42");
        assert_eq!(admission_id_from_headers(&headers), "adm-42");

        let long = "x".repeat(MAX_ADMISSION_ID_LEN + 1);
        for bad in ["", "has space", "semi;colon", long.as_str()] {
            let mut h = HeaderMap::new();
            h.insert(ADMISSION_ID_HEADER, bad.parse().unwrap());
            let id = admission_id_from_headers(&h);
            assert_ne!(id, bad);
            assert!(
                uuid::Uuid::parse_str(&id).is_ok(),
                "fell back to a fresh uuid"
            );
        }
        let a = admission_id_from_headers(&HeaderMap::new());
        let b = admission_id_from_headers(&HeaderMap::new());
        assert_ne!(a, b, "header-less requests are distinct attempts");
    }

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
/// an [`OutputSpec::RtmpPush`] or [`OutputSpec::SrtPush`] according to its
/// `protocol`, encoded with the codec the config/target select
/// ([`plan_push_outputs`] -- the same rules the write handlers and the
/// ingest orchestrator apply, so a combination ffmpeg cannot mux is a `400`
/// here rather than a destination silently dropped later).
/// `transcode_applied` is resolved by the caller
/// ([`resolve_transcode_admission`]) before this is called -- building the
/// spec itself never touches the token ledger.
async fn build_pipeline_spec(
    db: &DatabaseConnection,
    tenant: &str,
    community_id: i32,
    config: &streaming_config::Model,
    transcode_applied: bool,
) -> Result<PipelineSpec, ApiError> {
    let targets = enabled_targets(db, config.id).await?;
    let plan = plan_push_outputs(config, &targets, transcode_applied)?;

    let mut outputs = plan.outputs;
    if config.record_enabled {
        outputs.push(OutputSpec::Record {
            profile: DEFAULT_PROFILE.into(),
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
        profiles: plan.profiles,
        outputs,
    })
}

/// The acting member's hub-api user id: the validated JWT's `sub` (a `hub_users.id`). The
/// member's token itself is never forwarded to hub-api -- the debit is authenticated with this
/// service's own machine JWT and names the member as the actor. `None` for a non-numeric `sub`
/// (a service/legacy token), which cannot be admitted as a member.
fn actor_user_id(claims: &crate::http::auth::Claims) -> Option<i64> {
    claims.sub.parse::<i64>().ok().filter(|id| *id > 0)
}

/// Request header a client sets once per start attempt and reuses on every retry of it.
pub const ADMISSION_ID_HEADER: &str = "idempotency-key";

/// Longest admission id honoured; longer or non-token values are ignored (a fresh id is used).
const MAX_ADMISSION_ID_LEN: usize = 64;

/// Stable debit idempotency key for one admission: `stream:{community}:{config}:{admission_id}`.
///
/// Deterministic in its inputs and deliberately timestamp-free, so a retry of the same
/// admission produces the same key and hub-api debits once. `admission_id` is the client's
/// `Idempotency-Key` (identical on every retry of one start attempt) or, when absent, a fresh
/// UUID per call -- a distinct attempt, which is the only safe reading of a header-less request.
pub fn admission_idempotency_key(community_id: i32, config_id: i32, admission_id: &str) -> String {
    format!("stream:{community_id}:{config_id}:{admission_id}")
}

/// Extracts the admission id from the request headers, falling back to a fresh UUID.
///
/// Only `[A-Za-z0-9._-]` up to [`MAX_ADMISSION_ID_LEN`] is accepted so the value is safe to embed
/// in the key and log lines.
fn admission_id_from_headers(headers: &HeaderMap) -> String {
    headers
        .get(ADMISSION_ID_HEADER)
        .and_then(|v| v.to_str().ok())
        .map(str::trim)
        .filter(|v| {
            !v.is_empty()
                && v.len() <= MAX_ADMISSION_ID_LEN
                && v.bytes()
                    .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
        })
        .map(str::to_owned)
        .unwrap_or_else(|| uuid::Uuid::new_v4().to_string())
}

/// TRANSCODE admission (BLOCK-WITH-FALLBACK, ported from the Python
/// alpha's `services/streaming_service.py::start_forwarding`): if
/// `config.transcode_enabled` is false, no ledger call is made and this
/// always resolves to `false` (matching Python's "the debit is only
/// attempted `if config_row.transcode_enabled`"). Otherwise attempts a real
/// token debit; an affordable debit resolves to `true` (the pipeline then
/// re-encodes with the config's selected codec), while an unaffordable one
/// (`insufficient_balance`) OR an unreachable ledger (`ledger_unavailable`)
/// resolves to `false` -- `start()` still succeeds and the stream still
/// starts, just as a passthrough. Never returns an `Err`: a
/// billing/entitlement check must never take down a live-stream start.
#[allow(
    clippy::too_many_arguments,
    reason = "flat inputs of one admission decision, all resolved by the single caller"
)]
async fn resolve_transcode_admission(
    token_ledger: &TokenLedgerClient,
    hub_api_url: &str,
    actor_user_id: Option<i64>,
    transcode_token_cost: i64,
    transcode_product_key: &str,
    community_id: i32,
    config: &streaming_config::Model,
    admission_id: &str,
) -> bool {
    if !config.transcode_enabled {
        return false;
    }

    let Some(actor_user_id) = actor_user_id else {
        // The caller's `sub` is not a member user id, so there is no member to bill: admission
        // cannot be attempted. Loud, then the documented passthrough degrade.
        tracing::error!(
            community_id,
            config_id = config.id,
            "lifecycle: transcode admission requested but the caller's sub is not a numeric member id, falling back to passthrough"
        );
        return false;
    };

    let idempotency_key = admission_idempotency_key(community_id, config.id, admission_id);
    let reference = format!("stream:{}", config.id);
    let result = token_ledger
        .debit_transcoding_tokens(
            hub_api_url,
            actor_user_id,
            community_id,
            transcode_token_cost,
            transcode_product_key,
            &reference,
            &idempotency_key,
        )
        .await;

    if result.ok {
        tracing::info!(
            community_id,
            config_id = config.id,
            balance_after = result.balance_after,
            video_codec = %config.video_codec,
            audio_codec = %config.audio_codec,
            "lifecycle: transcode admission granted"
        );
        true
    } else {
        tracing::warn!(
            community_id,
            config_id = config.id,
            blocked_reason = result.blocked_reason.as_deref(),
            "lifecycle: transcode admission denied or unreachable, falling back to passthrough"
        );
        false
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
    DbConn(db): DbConn,
    Extension(engine): Extension<SharedEngine>,
    headers: HeaderMap,
) -> Result<ApiSuccess<PipelineStatusDto>, ApiError> {
    assert_tenant_owns_community(&db, &claims.tenant, community_id).await?;
    let config = fetch_config(&db, community_id, config_id).await?;
    let admission_id = admission_id_from_headers(&headers);
    let transcode_applied = resolve_transcode_admission(
        &state.token_ledger,
        &state.config.cli.hub_api_url,
        actor_user_id(&claims),
        state.config.cli.transcode_token_cost,
        &state.config.cli.transcode_product_key,
        community_id,
        &config,
        &admission_id,
    )
    .await;
    let spec = build_pipeline_spec(
        &db,
        &claims.tenant,
        community_id,
        &config,
        transcode_applied,
    )
    .await?;
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
