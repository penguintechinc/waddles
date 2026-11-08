//! Client-facing room media endpoints (WHIP-style publish, WHEP-style
//! subscribe), authenticated by a room join token in `Authorization: Bearer`.
//! Tokens are never put in URLs, so they cannot leak through access logs or
//! route-template metrics.
//!
//! - `POST   /rtc/rooms/{room_id}/whip`  (publish grant) SDP offer -> 201 + answer
//! - `POST   /rtc/rooms/{room_id}/whep`  (subscribe grant) `?source=<participant>`
//! - `DELETE /rtc/rooms/{room_id}/sessions/{session_id}` (session owner)

use std::sync::{Arc, OnceLock};

use async_trait::async_trait;
use axum::extract::{Path, Query, State};
use axum::http::{header, HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{delete, post};
use axum::Router;
use rtc::rtp_transceiver::rtp_sender::RtpCodecKind;
use serde::Deserialize;
use tokio::sync::Notify;
use uuid::Uuid;
use webrtc::media_stream::track_local::TrackLocal;
use webrtc::media_stream::track_remote::{TrackRemote, TrackRemoteEvent};
use webrtc::peer_connection::{
    PeerConnection, PeerConnectionEventHandler, RTCIceGatheringState, RTCPeerConnectionState,
    RTCSessionDescription,
};
use webrtc::rtp_transceiver::{RTCRtpTransceiverDirection, RTCRtpTransceiverInit};
use webrtc::runtime::Runtime;

use super::token::RoomClaims;
use super::{RoomError, RoomService};
use crate::egress::whep::{h264_codec, opus_codec, viewer_track};
use crate::error::ApiError;
use crate::rtc::fanout::{MediaFanouts, TrackFanout};
use crate::rtc::metrics::RtcMetrics;

/// Maps a [`RoomError`] onto the service's typed HTTP error.
pub(crate) fn api_error(e: RoomError) -> ApiError {
    match e {
        RoomError::NotFound => ApiError::NotFound("room or source not found".into()),
        RoomError::Invalid(m) => ApiError::BadRequest(m),
        RoomError::LimitReached(m) => ApiError::Forbidden(m),
        RoomError::Forbidden(m) => ApiError::Forbidden(m.into()),
        RoomError::Unauthorized => ApiError::Unauthorized("invalid room token".into()),
        RoomError::Unconfigured(m) => ApiError::Unimplemented(m.into()),
        RoomError::Unsupported(m) => ApiError::Unimplemented(m),
        RoomError::Ambiguous(m) => ApiError::BadRequest(m),
        RoomError::Backend(m) => ApiError::Internal(anyhow::anyhow!(m)),
    }
}

fn bearer(headers: &HeaderMap) -> Result<&str, ApiError> {
    headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .filter(|t| !t.is_empty())
        .ok_or_else(|| ApiError::Unauthorized("missing bearer room token".into()))
}

fn require_sdp(headers: &HeaderMap) -> Result<(), ApiError> {
    let ct = headers
        .get(header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or_default();
    if ct.starts_with("application/sdp") {
        Ok(())
    } else {
        Err(ApiError::BadRequest(
            "Content-Type must be application/sdp".into(),
        ))
    }
}

/// Where a handler finds its session once registered (set after `add_*`), so a
/// dead peer connection can be reaped from the room.
#[derive(Clone, Default)]
struct ReapSlot {
    target: Arc<OnceLock<(Arc<RoomService>, Uuid, Uuid)>>,
    ended: Arc<std::sync::atomic::AtomicBool>,
}

/// Spawns the reap of a session whose peer connection ended.
fn reap_if_registered(slot: &ReapSlot) {
    slot.ended.store(true, std::sync::atomic::Ordering::SeqCst);
    if let Some((svc, room, session)) = slot.target.get() {
        let (svc, room, session) = (Arc::clone(svc), *room, *session);
        tokio::spawn(async move { svc.reap_session(room, session).await });
    }
}

fn peer_ended(s: RTCPeerConnectionState) -> bool {
    matches!(
        s,
        RTCPeerConnectionState::Failed
            | RTCPeerConnectionState::Closed
            | RTCPeerConnectionState::Disconnected
    )
}

struct PublishHandler {
    reap: ReapSlot,
    fanouts: MediaFanouts,
    gather: Arc<Notify>,
    metrics: RtcMetrics,
    runtime: Arc<dyn Runtime>,
}

#[async_trait]
impl PeerConnectionEventHandler for PublishHandler {
    async fn on_ice_gathering_state_change(&self, s: RTCIceGatheringState) {
        if s == RTCIceGatheringState::Complete {
            self.gather.notify_one();
        }
    }
    async fn on_connection_state_change(&self, s: RTCPeerConnectionState) {
        if s == RTCPeerConnectionState::Failed {
            self.metrics.ice_failures_total.inc();
        }
        if peer_ended(s) {
            reap_if_registered(&self.reap);
        }
    }
    async fn on_track(&self, track: Arc<dyn TrackRemote>) {
        let kind = track.kind().await;
        let fanouts = self.fanouts.clone();
        let metrics = self.metrics.clone();
        self.runtime.spawn(Box::pin(async move {
            while let Some(ev) = track.poll().await {
                let TrackRemoteEvent::OnRtpPacket(p) = ev else {
                    continue;
                };
                metrics
                    .rtp_packets_total
                    .with_label_values(&["ingress"])
                    .inc();
                match kind {
                    RtpCodecKind::Video => fanouts.video.publish(p),
                    RtpCodecKind::Audio => fanouts.audio.publish(p),
                    _ => {}
                }
            }
        }));
    }
}

struct ViewerHandler {
    reap: ReapSlot,
    gather: Arc<Notify>,
    metrics: RtcMetrics,
}

#[async_trait]
impl PeerConnectionEventHandler for ViewerHandler {
    async fn on_ice_gathering_state_change(&self, s: RTCIceGatheringState) {
        if s == RTCIceGatheringState::Complete {
            self.gather.notify_one();
        }
    }
    async fn on_connection_state_change(&self, s: RTCPeerConnectionState) {
        if s == RTCPeerConnectionState::Failed {
            self.metrics.ice_failures_total.inc();
        }
        if peer_ended(s) {
            reap_if_registered(&self.reap);
        }
    }
}

/// Forwards one fanout to one viewer track, rewriting SSRC and payload type to
/// what the viewer's own negotiation produced.
fn spawn_forward(
    runtime: &dyn Runtime,
    fanout: Arc<TrackFanout>,
    track: Arc<webrtc::media_stream::track_local::static_rtp::TrackLocalStaticRTP>,
    ssrc: u32,
    pt: Option<u8>,
    metrics: RtcMetrics,
) {
    runtime.spawn(Box::pin(async move {
        let mut sub = fanout.subscribe();
        while let Some(mut p) = sub.recv(&metrics).await {
            p.header.ssrc = ssrc;
            if let Some(pt) = pt {
                p.header.payload_type = pt;
            }
            metrics
                .rtp_packets_total
                .with_label_values(&["egress"])
                .inc();
            if track.write_rtp(p).await.is_err() {
                break;
            }
        }
    }));
}

async fn await_gather(pc: &Arc<dyn PeerConnection>, gather: &Notify) -> Result<String, ApiError> {
    if tokio::time::timeout(RoomService::GATHER_TIMEOUT, gather.notified())
        .await
        .is_err()
    {
        let _ = pc.close().await;
        return Err(ApiError::Internal(anyhow::anyhow!(
            "ICE gathering timed out"
        )));
    }
    match pc.local_description().await {
        Some(d) => Ok(d.sdp),
        None => {
            let _ = pc.close().await;
            Err(ApiError::Internal(anyhow::anyhow!("no local description")))
        }
    }
}

fn claims_participant(c: &RoomClaims) -> Result<Uuid, ApiError> {
    Uuid::parse_str(&c.sub).map_err(|_| ApiError::Unauthorized("invalid room token".into()))
}

fn created(location: String, sdp: String) -> Response {
    (
        StatusCode::CREATED,
        [
            (header::CONTENT_TYPE, "application/sdp".to_string()),
            (header::LOCATION, location),
        ],
        sdp,
    )
        .into_response()
}

async fn publish(
    State(svc): State<Arc<RoomService>>,
    Path(room_id): Path<Uuid>,
    headers: HeaderMap,
    body: axum::body::Bytes,
) -> Result<Response, ApiError> {
    let claims = svc
        .authorize(bearer(&headers)?, room_id)
        .await
        .map_err(api_error)?;
    if !claims.publish {
        return Err(ApiError::Forbidden("token has no publish grant".into()));
    }
    require_sdp(&headers)?;
    let participant = claims_participant(&claims)?;
    let sdp = String::from_utf8(body.to_vec())
        .map_err(|_| ApiError::BadRequest("offer is not UTF-8".into()))?;
    let (has_video, has_audio) = (sdp.contains("m=video"), sdp.contains("m=audio"));
    if !has_video && !has_audio {
        return Err(ApiError::BadRequest("offer declares no media".into()));
    }
    let offer = RTCSessionDescription::offer(sdp)
        .map_err(|e| ApiError::BadRequest(format!("invalid SDP offer: {e}")))?;

    let fanouts = MediaFanouts::new();
    let gather = Arc::new(Notify::new());
    let runtime = svc.factory.runtime();
    let reap = ReapSlot::default();
    let handler = Arc::new(PublishHandler {
        reap: reap.clone(),
        fanouts: fanouts.clone(),
        gather: gather.clone(),
        metrics: svc.metrics.clone(),
        runtime,
    });
    let pc = svc
        .factory
        .build(handler)
        .await
        .map_err(|e| ApiError::Internal(anyhow::anyhow!(e.to_string())))?;
    for (want, kind) in [
        (has_video, RtpCodecKind::Video),
        (has_audio, RtpCodecKind::Audio),
    ] {
        if want {
            pc.add_transceiver_from_kind(
                kind,
                Some(RTCRtpTransceiverInit {
                    direction: RTCRtpTransceiverDirection::Recvonly,
                    ..Default::default()
                }),
            )
            .await
            .map_err(|e| ApiError::Internal(anyhow::anyhow!(e.to_string())))?;
        }
    }
    if let Err(e) = pc.set_remote_description(offer).await {
        let _ = pc.close().await;
        return Err(ApiError::BadRequest(format!("offer rejected: {e}")));
    }
    let answer = pc
        .create_answer(None)
        .await
        .map_err(|e| ApiError::Internal(anyhow::anyhow!(e.to_string())))?;
    pc.set_local_description(answer)
        .await
        .map_err(|e| ApiError::Internal(anyhow::anyhow!(e.to_string())))?;
    let answer_sdp = await_gather(&pc, &gather).await?;

    let session = match svc
        .add_publisher(
            room_id,
            participant,
            fanouts,
            pc.clone(),
            reap.ended.clone(),
            claims.exp,
        )
        .await
    {
        Ok(s) => s,
        Err(e) => {
            let _ = pc.close().await;
            return Err(api_error(e));
        }
    };
    let _ = reap.target.set((Arc::clone(&svc), room_id, session));
    if reap.ended.load(std::sync::atomic::Ordering::SeqCst) {
        reap_if_registered(&reap);
    }
    svc.metrics
        .sessions_active
        .with_label_values(&["room_whip"])
        .inc();
    Ok(created(
        format!("/rtc/rooms/{room_id}/sessions/{session}"),
        answer_sdp,
    ))
}

#[derive(Deserialize)]
struct SubscribeQuery {
    source: Option<Uuid>,
}

async fn subscribe(
    State(svc): State<Arc<RoomService>>,
    Path(room_id): Path<Uuid>,
    Query(q): Query<SubscribeQuery>,
    headers: HeaderMap,
    body: String,
) -> Result<Response, ApiError> {
    let claims = svc
        .authorize(bearer(&headers)?, room_id)
        .await
        .map_err(api_error)?;
    if !claims.subscribe {
        return Err(ApiError::Forbidden("token has no subscribe grant".into()));
    }
    require_sdp(&headers)?;
    let participant = claims_participant(&claims)?;
    let restricted = match claims.source.as_deref() {
        Some(s) => Some(
            Uuid::parse_str(s).map_err(|_| ApiError::Unauthorized("invalid room token".into()))?,
        ),
        None => None,
    };
    let source = match (restricted, q.source) {
        (Some(r), Some(w)) if r != w => {
            return Err(ApiError::Forbidden(
                "token is restricted to another source".into(),
            ))
        }
        (Some(r), _) => Some(r),
        (None, w) => w,
    };
    let (_src, fanouts) = svc
        .source_fanouts(room_id, source)
        .await
        .map_err(api_error)?;
    let offer = RTCSessionDescription::offer(body)
        .map_err(|e| ApiError::BadRequest(format!("invalid SDP offer: {e}")))?;

    let gather = Arc::new(Notify::new());
    let reap = ReapSlot::default();
    let handler = Arc::new(ViewerHandler {
        reap: reap.clone(),
        gather: gather.clone(),
        metrics: svc.metrics.clone(),
    });
    let pc = svc
        .factory
        .build(handler)
        .await
        .map_err(|e| ApiError::Internal(anyhow::anyhow!(e.to_string())))?;
    let (vtrack, vssrc) = viewer_track(RtpCodecKind::Video, h264_codec());
    let (atrack, assrc) = viewer_track(RtpCodecKind::Audio, opus_codec());
    for t in [
        vtrack.clone() as Arc<dyn TrackLocal>,
        atrack.clone() as Arc<dyn TrackLocal>,
    ] {
        if let Err(e) = pc.add_track(t).await {
            let _ = pc.close().await;
            return Err(ApiError::Internal(anyhow::anyhow!(e.to_string())));
        }
    }
    if let Err(e) = pc.set_remote_description(offer).await {
        let _ = pc.close().await;
        return Err(ApiError::BadRequest(format!("offer rejected: {e}")));
    }
    let answer = pc
        .create_answer(None)
        .await
        .map_err(|e| ApiError::Internal(anyhow::anyhow!(e.to_string())))?;
    pc.set_local_description(answer)
        .await
        .map_err(|e| ApiError::Internal(anyhow::anyhow!(e.to_string())))?;
    let answer_sdp = await_gather(&pc, &gather).await?;

    // Negotiated payload type per media kind, matched by the sender's codec mime
    // type (sender order is not guaranteed); packets are rewritten to match.
    let (mut video_pt, mut audio_pt) = (None, None);
    for sender in pc.get_senders().await {
        let Ok(params) = sender.get_parameters().await else {
            continue;
        };
        if let Some(c) = params.rtp_parameters.codecs.first() {
            let mime = c.rtp_codec.mime_type.to_ascii_lowercase();
            if mime.starts_with("video/") {
                video_pt = Some(c.payload_type);
            } else if mime.starts_with("audio/") {
                audio_pt = Some(c.payload_type);
            }
        }
    }
    let runtime = svc.factory.runtime();
    spawn_forward(
        &*runtime,
        fanouts.video,
        vtrack,
        vssrc,
        video_pt,
        svc.metrics.clone(),
    );
    spawn_forward(
        &*runtime,
        fanouts.audio,
        atrack,
        assrc,
        audio_pt,
        svc.metrics.clone(),
    );

    let session = match svc
        .add_subscriber(
            room_id,
            participant,
            pc.clone(),
            reap.ended.clone(),
            claims.exp,
        )
        .await
    {
        Ok(s) => s,
        Err(e) => {
            let _ = pc.close().await;
            return Err(api_error(e));
        }
    };
    let _ = reap.target.set((Arc::clone(&svc), room_id, session));
    if reap.ended.load(std::sync::atomic::Ordering::SeqCst) {
        reap_if_registered(&reap);
    }
    svc.metrics
        .sessions_active
        .with_label_values(&["room_whep"])
        .inc();
    Ok(created(
        format!("/rtc/rooms/{room_id}/sessions/{session}"),
        answer_sdp,
    ))
}

async fn end_session(
    State(svc): State<Arc<RoomService>>,
    Path((room_id, session)): Path<(Uuid, Uuid)>,
    headers: HeaderMap,
) -> Result<StatusCode, ApiError> {
    let claims = svc
        .authorize(bearer(&headers)?, room_id)
        .await
        .map_err(api_error)?;
    let participant = claims_participant(&claims)?;
    svc.end_session(room_id, session, participant)
        .await
        .map_err(api_error)?;
    Ok(StatusCode::NO_CONTENT)
}

/// Builds the room media sub-router, mounted on the shared HTTP port.
pub fn router(svc: Arc<RoomService>) -> Router {
    Router::new()
        .route("/rtc/rooms/{room_id}/whip", post(publish))
        .route("/rtc/rooms/{room_id}/whep", post(subscribe))
        .route(
            "/rtc/rooms/{room_id}/sessions/{session_id}",
            delete(end_session),
        )
        .with_state(svc)
}
