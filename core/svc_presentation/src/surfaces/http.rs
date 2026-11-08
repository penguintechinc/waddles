//! HTTP shell for the surface path.
//!
//! * `POST /internal/surfaces/{community}/{surface}/publish` -- PUSH-guarded
//!   (`overlay_auth::require_push_credential`: a hub-api-minted machine JWT
//!   scoped to exactly that community). The body IS the payload.
//! * `GET /surfaces/{overlay_code}/live?surfaces=a,b&key=...` -- VIEW-guarded
//!   SSE: a `connected` event, then the cached replay frame of every
//!   requested replayable surface, then live frames. Read-only.
//! * `GET /surfaces/{overlay_code}/view` -- the static rendering page.
//!
//! Community comes only from the verified credential, never from the body.

use std::collections::HashSet;
use std::convert::Infallible;
use std::time::{Duration, Instant};

use axum::body::Bytes;
use axum::extract::{Path, Query, State};
use axum::response::sse::{Event, KeepAlive, Sse};
use axum::{Extension, Json};
use futures::stream::{self, Stream, StreamExt};
use overlay_auth::{PushCredential, ViewCredential};
use overlay_schema::{AlertPayload, OverlayPush, Surface};
use serde::{Deserialize, Serialize};

use super::hub::{Recv, SurfaceFrame, SurfaceSubscription};
use super::PublishError;
use crate::error::ApiError;
use crate::http::AppState;
use crate::overlay::detok::DetokScope;
use crate::overlay::render::RenderTheme;

/// Most surfaces one stream may subscribe to.
pub const MAX_STREAM_SURFACES: usize = 8;

/// SSE keep-alive interval (matches the overlay live channel).
const HEARTBEAT: Duration = Duration::from_secs(15);

/// Path of the publish route.
#[derive(Debug, Deserialize)]
pub struct PublishPath {
    /// Numeric community segment; only consumed by the PUSH guard, which
    /// verifies the JWT is scoped to it. Handlers use the credential.
    #[allow(dead_code)]
    pub community: String,
    /// Surface id.
    pub surface: String,
}

/// Success body of a publish.
#[derive(Debug, Serialize)]
pub struct PublishResponse {
    /// Always `published`.
    pub status: &'static str,
    /// The surface published to.
    pub surface: String,
    /// Sequence number of the fanned-out frame.
    pub seq: u64,
    /// Whether the frame was also bridged into the `alert_box` overlay.
    pub bridged: bool,
}

fn publish_error_to_api(err: PublishError) -> ApiError {
    match err {
        PublishError::UnknownSurface => ApiError::NotFound("unknown surface".to_string()),
        PublishError::TooLarge { max } => {
            ApiError::PayloadTooLarge(format!("payload exceeds {max} bytes for this surface"))
        }
        PublishError::RateLimited { retry_after_secs } => {
            ApiError::TooManyRequests { retry_after_secs }
        }
        PublishError::Saturated => ApiError::TooManyRequests {
            retry_after_secs: 5,
        },
        PublishError::NotJson => ApiError::BadRequest("body is not valid JSON".to_string()),
        PublishError::Unsanitary(msg) => ApiError::Unprocessable {
            message: msg,
            violations: Vec::new(),
        },
        PublishError::Invalid(violations) => ApiError::Unprocessable {
            message: "payload does not match the surface schema".to_string(),
            violations,
        },
    }
}

/// `POST /internal/surfaces/{community}/{surface}/publish`.
#[tracing::instrument(
    name = "surfaces.publish",
    skip_all,
    fields(community_id = credential.community_id, surface = %params.surface)
)]
pub async fn publish(
    State(state): State<AppState>,
    Extension(credential): Extension<PushCredential>,
    Path(params): Path<PublishPath>,
    body: Bytes,
) -> Result<Json<PublishResponse>, ApiError> {
    let community_id = credential.community_id;
    let frame = state
        .surfaces
        .process(community_id, &params.surface, &body, Instant::now())
        .map_err(publish_error_to_api)?;
    let bridged = if frame.surface == "alert" {
        bridge_alert(&state, community_id, &frame).await
    } else {
        false
    };
    tracing::debug!(
        community_id,
        seq = frame.seq,
        bridged,
        "surface frame published"
    );
    Ok(Json(PublishResponse {
        status: "published",
        surface: frame.surface.clone(),
        seq: frame.seq,
        bridged,
    }))
}

/// Feeds a validated `alert` surface frame through the existing `alert_box`
/// renderer (escaping included) into the live overlay hub, so OBS alert
/// overlays keep working. A bridge failure is logged loudly but does not fail
/// the publish: the surface fan-out already succeeded.
async fn bridge_alert(state: &AppState, community_id: i64, frame: &SurfaceFrame) -> bool {
    let p = &frame.payload;
    let (Some(alert_type), Some(message)) = (
        p.get("alert_type").and_then(|v| v.as_str()),
        p.get("message").and_then(|v| v.as_str()),
    ) else {
        tracing::error!("validated alert frame lacks alert_type/message; not bridged");
        return false;
    };
    let push = OverlayPush {
        alert: Some(AlertPayload {
            alert_type: alert_type.to_string(),
            user: None,
            display_name: None,
            amount: p.get("amount").cloned(),
            message: Some(message.to_string()),
        }),
        ..Default::default()
    };
    // Sanitization already removed every user token, so no resolve call (and
    // no tenant) is needed; the default theme avoids a database read.
    let scope = DetokScope::new(String::new(), community_id);
    match state
        .detokenizer
        .render_with_metrics(
            &scope,
            Surface::AlertBox,
            &push,
            &RenderTheme::default(),
            &state.render_metrics,
        )
        .await
    {
        Ok(rendered) => {
            state
                .frame_hub
                .publish(community_id, Surface::AlertBox, rendered);
            true
        }
        Err(err) => {
            tracing::error!(community_id, error = %err, "alert surface frame could not be bridged to alert_box");
            false
        }
    }
}

/// Query of the live stream and page routes.
#[derive(Debug, Deserialize)]
pub struct StreamQuery {
    /// Comma-separated surface ids.
    pub surfaces: String,
}

/// Parses and validates the requested surface list against the contract.
fn parse_surfaces(state: &AppState, raw: &str) -> Result<Vec<String>, ApiError> {
    let mut seen = HashSet::new();
    let mut out = Vec::new();
    for id in raw.split(',').map(str::trim).filter(|s| !s.is_empty()) {
        if state.surfaces.contract.get(id).is_none() {
            return Err(ApiError::NotFound("unknown surface".to_string()));
        }
        if seen.insert(id.to_string()) {
            out.push(id.to_string());
        }
    }
    if out.is_empty() || out.len() > MAX_STREAM_SURFACES {
        return Err(ApiError::BadRequest(format!(
            "request 1 to {MAX_STREAM_SURFACES} surfaces"
        )));
    }
    Ok(out)
}

#[derive(Serialize)]
struct Connected<'a> {
    connected: bool,
    surfaces: &'a [String],
}

/// One surface's live frames, skipping anything not newer than `min_seq`
/// (already delivered as the replay frame) and logging lag loudly.
fn live_stream(
    sub: SurfaceSubscription,
    min_seq: u64,
) -> impl Stream<Item = Result<Event, Infallible>> {
    stream::unfold((sub, min_seq), |(mut sub, min_seq)| async move {
        loop {
            match sub.recv().await {
                Some(Recv::Frame(frame)) if frame.seq > min_seq => {
                    match Event::default().json_data(&*frame) {
                        Ok(event) => return Some((Ok(event), (sub, min_seq))),
                        Err(err) => {
                            tracing::error!(error = %err, "surface frame serialization failed; skipped");
                        }
                    }
                }
                Some(Recv::Frame(_)) => {}
                Some(Recv::Lagged(n)) => {
                    tracing::warn!(dropped = n, "surface subscriber lagged; frames dropped");
                }
                None => return None,
            }
        }
    })
}

/// `GET /surfaces/{overlay_code}/live?surfaces=a,b&key=...`.
#[tracing::instrument(name = "surfaces.live", skip_all, fields(community_id = credential.community_id))]
pub async fn live(
    State(state): State<AppState>,
    Extension(credential): Extension<ViewCredential>,
    Query(query): Query<StreamQuery>,
) -> Result<Sse<impl Stream<Item = Result<Event, Infallible>>>, ApiError> {
    let ids = parse_surfaces(&state, &query.surfaces)?;
    let community_id = credential.community_id;

    // Subscribe first, then read replay: nothing published in between is lost.
    let mut live_streams = Vec::with_capacity(ids.len());
    let mut replays = Vec::new();
    for id in &ids {
        let sub = state.surfaces.hub.subscribe(community_id, id);
        let replay = state.surfaces.hub.replay_for(community_id, id);
        let min_seq = replay.as_ref().map_or(0, |f| f.seq);
        if let Some(frame) = replay {
            state.surfaces.metrics.replayed(id);
            if let Ok(event) = Event::default().json_data(&*frame) {
                replays.push(Ok(event));
            }
        }
        live_streams.push(live_stream(sub, min_seq).boxed());
    }
    let connected = Event::default()
        .json_data(Connected {
            connected: true,
            surfaces: &ids,
        })
        .map_err(|e| ApiError::Internal(anyhow::anyhow!("connected frame: {e}")))?;
    let events = stream::once(async move { Ok(connected) })
        .chain(stream::iter(replays))
        .chain(stream::select_all(live_streams));
    Ok(Sse::new(events).keep_alive(KeepAlive::new().interval(HEARTBEAT)))
}
