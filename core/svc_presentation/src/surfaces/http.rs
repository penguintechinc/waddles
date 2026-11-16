//! Browser-facing viewer routes for the surface path (read-only, HTTP).
//!
//! * `GET /surfaces/{overlay_code}/live?surfaces=a,b&key=...` -- VIEW-guarded
//!   SSE: a `connected` event, then the cached replay frame of every
//!   requested replayable surface, then live frames.
//! * `GET /surfaces/{overlay_code}/view` -- the static rendering page
//!   ([`super::page`]).
//!
//! There is deliberately NO publish route here: publishing is in-cluster
//! traffic and goes over gRPC only ([`super::ingress`]). Community comes only
//! from the verified VIEW credential, never from the request.

use std::collections::HashSet;
use std::convert::Infallible;
use std::time::Duration;

use axum::extract::{Query, State};
use axum::response::sse::{Event, KeepAlive, Sse};
use axum::Extension;
use futures::stream::{self, Stream, StreamExt};
use overlay_auth::ViewCredential;
use serde::{Deserialize, Serialize};

use super::hub::{Recv, SurfaceSubscription};
use crate::error::ApiError;
use crate::http::AppState;

/// Most surfaces one stream may subscribe to.
pub const MAX_STREAM_SURFACES: usize = 8;

/// SSE keep-alive interval (matches the overlay live channel).
const HEARTBEAT: Duration = Duration::from_secs(15);

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

/// Subscribes to every hub key in `keys` for `community_id` and returns one SSE stream: a
/// `connected` event, the replay frame of each replayable key, then live frames. Shared by the
/// surface viewer and the composite-scene viewer so both honour the same subscribe-then-replay
/// protocol. Keys are chosen by the CALLER from verified state, never taken from the wire.
pub(crate) fn sse_for_keys(
    state: &AppState,
    community_id: i64,
    keys: &[String],
) -> Result<Sse<impl Stream<Item = Result<Event, Infallible>>>, ApiError> {
    // Subscribe first, then read replay: nothing published in between is lost.
    let mut live_streams = Vec::with_capacity(keys.len());
    let mut replays = Vec::new();
    for id in keys {
        let sub = state.surfaces.hub.subscribe(community_id, id);
        let replay = state.surfaces.hub.replay_for(community_id, id);
        let min_seq = replay.as_ref().map_or(0, |f| f.seq);
        if let Some(frame) = replay {
            // Widget keys are unbounded; fold them into one closed label.
            let label = if id.starts_with(super::WIDGET_KEY_PREFIX) {
                "widget"
            } else {
                id.as_str()
            };
            state.surfaces.metrics.replayed(label);
            if let Ok(event) = Event::default().json_data(&*frame) {
                replays.push(Ok(event));
            }
        }
        live_streams.push(live_stream(sub, min_seq).boxed());
    }
    let connected = Event::default()
        .json_data(Connected {
            connected: true,
            surfaces: keys,
        })
        .map_err(|e| ApiError::Internal(anyhow::anyhow!("connected frame: {e}")))?;
    let events = stream::once(async move { Ok(connected) })
        .chain(stream::iter(replays))
        .chain(stream::select_all(live_streams));
    Ok(Sse::new(events).keep_alive(KeepAlive::new().interval(HEARTBEAT)))
}

/// `GET /surfaces/{overlay_code}/live?surfaces=a,b&key=...`.
#[tracing::instrument(name = "surfaces.live", skip_all, fields(community_id = credential.community_id))]
pub async fn live(
    State(state): State<AppState>,
    Extension(credential): Extension<ViewCredential>,
    Query(query): Query<StreamQuery>,
) -> Result<Sse<impl Stream<Item = Result<Event, Infallible>>>, ApiError> {
    let ids = parse_surfaces(&state, &query.surfaces)?;
    sse_for_keys(&state, credential.community_id, &ids)
}
