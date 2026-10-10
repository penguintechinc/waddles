//! WHEP (WebRTC-HTTP Egress Protocol, IETF draft) egress -- chunk S6, the
//! SFU side of `docs/plans/2026-09-11-svc-streaming-pipeline-matrix.md`
//! §4/§9.4: **one encode, RTP forwarded to N `PeerConnection`s**, never one
//! ffmpeg `-f rtp` output per viewer. Declares an axum sub-router
//! (`router`) a later integration step mounts onto the shared
//! control-plane HTTP port, exactly like `src/ingest/whip.rs`.
//!
//! # Copy vs transcoded source
//!
//! A WHEP viewer always subscribes to a [`crate::rtc::fanout::TrackFanout`]
//! -- which fanout depends on whether the pipeline's output profile is a
//! pure copy of a WHIP input (subscribes directly to
//! `crate::ingest::whip::WhipState`'s fanout for that token -- §1/§7 case
//! 6, "no ffmpeg process") or a transcoded profile (subscribes to a fanout
//! fed by [`crate::rtc::rtp_leg::RtpIngress`] reading ffmpeg's `-f rtp`
//! output -- §4). [`WhepState::register_fanout`] is how either source
//! publishes a `PipelineId`'s fanout for viewers to find; **nothing in this
//! service yet calls it for a real pipeline** -- `pipeline::supervisor`
//! (S3) is still a stub (see `src/rtc/mod.rs`'s "Not done in this chunk"
//! section), so this chunk exposes the mechanism and its test coverage,
//! not an end-to-end wire-up.
//!
//! `OutputSpec::Whep{profile}` (the pipeline-model contract) carries a
//! profile name, not a fanout -- resolving "this `PipelineId` + profile ->
//! this fanout" is `pipeline::supervisor`'s job once implemented;
//! [`WhepSink`] below stays a thin [`OutputSink`] stub for that same reason
//! (§ "Not done in this chunk" in `src/rtc/mod.rs`).

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use axum::extract::{Path, State};
use axum::http::{header, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{delete, post};
use axum::Router;
use rtc::rtp_transceiver::rtp_sender::{
    RTCRtpCodec, RTCRtpCodingParameters, RTCRtpEncodingParameters, RtpCodecKind,
};
use tokio::sync::{Notify, RwLock};
use tracing::Instrument as _;
use uuid::Uuid;
use webrtc::media_stream::track_local::static_rtp::TrackLocalStaticRTP;
use webrtc::media_stream::track_local::TrackLocal;
use webrtc::media_stream::MediaStreamTrack;
use webrtc::peer_connection::{
    PeerConnection, PeerConnectionEventHandler, RTCIceGatheringState, RTCPeerConnectionState,
    RTCSessionDescription,
};
use webrtc::runtime::Runtime;

use crate::egress::{OutputSink, SinkError};
use crate::error::ApiError;
use crate::pipeline::model::{OutputSpec, PipelineId};
use crate::rtc::fanout::{MediaFanouts, TrackFanout};
use crate::rtc::metrics::RtcMetrics;
use crate::rtc::pc_factory::PeerConnectionFactory;

/// Same rationale as `src/ingest/whip.rs::GATHER_TIMEOUT`.
const GATHER_TIMEOUT: Duration = Duration::from_secs(5);

/// Default `WHEP_MAX_VIEWERS` -- no env var exists for this yet
/// (`src/config.rs` is outside this chunk's file ownership; adding one is a
/// follow-up there, not here). [`WhepState::new`] takes the limit as a
/// parameter so a caller can override it once that env var exists without
/// this module changing.
pub const DEFAULT_MAX_VIEWERS: usize = 50;

struct WhepViewerSession {
    pipeline_id: PipelineId,
    pc: Arc<dyn PeerConnection>,
}

/// Shared state for every WHEP viewer session in the process.
pub struct WhepState {
    factory: Arc<PeerConnectionFactory>,
    metrics: RtcMetrics,
    max_viewers: usize,
    /// Keyed by `PipelineId` -- registered by whichever source (WHIP copy
    /// path or a transcoded [`crate::rtc::rtp_leg::RtpIngress`]) is
    /// feeding that pipeline's output right now. See this module's doc
    /// comment for why nothing wires this from a real pipeline yet.
    fanouts: RwLock<HashMap<PipelineId, MediaFanouts>>,
    sessions: RwLock<HashMap<Uuid, WhepViewerSession>>,
}

impl WhepState {
    pub fn new(
        factory: Arc<PeerConnectionFactory>,
        metrics: RtcMetrics,
        max_viewers: usize,
    ) -> Self {
        Self {
            factory,
            metrics,
            max_viewers,
            fanouts: RwLock::new(HashMap::new()),
            sessions: RwLock::new(HashMap::new()),
        }
    }

    /// Registers (or replaces) the video+audio fanouts viewers of
    /// `pipeline_id` should subscribe to. Called by whichever source is
    /// currently feeding that pipeline's output -- the WHIP ingest handler
    /// for a copy path, or an `RtpIngress` reader for a transcoded one.
    pub async fn register_fanout(&self, pipeline_id: PipelineId, fanouts: MediaFanouts) {
        self.fanouts.write().await.insert(pipeline_id, fanouts);
    }

    /// Removes a pipeline's fanout registration -- called when its source
    /// stops (WHIP publisher disconnects, or the transcode leg's ffmpeg
    /// process exits).
    pub async fn unregister_fanout(&self, pipeline_id: PipelineId) {
        self.fanouts.write().await.remove(&pipeline_id);
    }

    /// Current viewer count for a pipeline -- used to enforce
    /// `WHEP_MAX_VIEWERS` before a new subscription is created. Not the
    /// same as `TrackFanout::subscriber_count`: a pipeline typically has
    /// two fanouts (video, audio) and one viewer subscribes to both, so
    /// this counts *sessions*, not fanout subscriptions.
    pub async fn viewer_count(&self, pipeline_id: PipelineId) -> usize {
        self.sessions
            .read()
            .await
            .values()
            .filter(|session| session.pipeline_id == pipeline_id)
            .count()
    }
}

/// `PeerConnectionEventHandler` for one WHEP viewer: no inbound media to
/// forward (send-only), just ICE-gathering / failure bookkeeping.
struct WhepViewerHandler {
    gather_complete: Arc<Notify>,
    metrics: RtcMetrics,
}

#[async_trait]
impl PeerConnectionEventHandler for WhepViewerHandler {
    async fn on_ice_gathering_state_change(&self, state: RTCIceGatheringState) {
        if state == RTCIceGatheringState::Complete {
            self.gather_complete.notify_one();
        }
    }

    async fn on_connection_state_change(&self, state: RTCPeerConnectionState) {
        if state == RTCPeerConnectionState::Failed {
            self.metrics.ice_failures_total.inc();
        }
    }
}

/// Spawns the forwarding task that pulls packets off `fanout` and writes
/// them to `track`, rewriting the SSRC to the one this viewer's track
/// advertised in its SDP (mirrors `webrtc-rs`'s own `broadcast` example:
/// each viewer gets a fresh SSRC, independent of the publisher's).
fn spawn_viewer_forward(
    runtime: &dyn Runtime,
    fanout: Arc<TrackFanout>,
    track: Arc<TrackLocalStaticRTP>,
    ssrc: u32,
    metrics: RtcMetrics,
) {
    // Created here (not inside the task) so it parents to the signaling
    // request's span: the viewer's whole forwarding lifetime is one child of
    // the `POST /whep/...` trace. Carries only a packet count -- no viewer
    // address or SSRC.
    let span = tracing::info_span!("egress.whep_forward", packets = tracing::field::Empty);
    runtime.spawn(Box::pin(
        async move {
            let mut subscriber = fanout.subscribe();
            let mut forwarded: u64 = 0;
            while let Some(mut packet) = subscriber.recv(&metrics).await {
                packet.header.ssrc = ssrc;
                metrics
                    .rtp_packets_total
                    .with_label_values(&["egress"])
                    .inc();
                if track.write_rtp(packet).await.is_err() {
                    break;
                }
                forwarded += 1;
            }
            tracing::Span::current().record("packets", forwarded);
        }
        .instrument(span),
    ));
}

/// Builds a send-only local track for one viewer, with a fresh SSRC
/// derived from a UUID (this crate avoids adding the `rand` crate as a new
/// dependency -- see `src/rtc/mod.rs`; the low 32 bits of a fresh v4 UUID
/// are exactly as suitable a random SSRC source as `rand::random::<u32>()`
/// would be, since RTP only requires the value to be unpredictable enough
/// to avoid collisions, not cryptographically secure).
fn viewer_track(kind: RtpCodecKind, codec: RTCRtpCodec) -> (Arc<TrackLocalStaticRTP>, u32) {
    let ssrc = Uuid::new_v4().as_u128() as u32;
    let stream_id = format!("svc-streaming-whep-{ssrc}");
    let track = Arc::new(TrackLocalStaticRTP::new(MediaStreamTrack::new(
        stream_id.clone(),
        stream_id.clone(),
        stream_id,
        kind,
        vec![RTCRtpEncodingParameters {
            rtp_coding_parameters: RTCRtpCodingParameters {
                ssrc: Some(ssrc),
                ..Default::default()
            },
            codec,
            ..Default::default()
        }],
    )));
    (track, ssrc)
}

/// Matches `MediaEngine::register_default_codecs`'s first H264 entry
/// (`packetization-mode=1`, the non-interleaved mode most broadly
/// supported by encoders/browsers) exactly -- mime type, clock rate,
/// channels, **and** `sdp_fmtp_line` all have to match for codec matching
/// to pick this entry during offer/answer generation; see
/// [`opus_codec`]'s doc comment for what goes wrong when they don't.
fn h264_codec() -> RTCRtpCodec {
    RTCRtpCodec {
        mime_type: "video/H264".to_string(),
        clock_rate: 90000,
        channels: 0,
        sdp_fmtp_line: "level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42001f"
            .to_string(),
        rtcp_feedback: vec![],
    }
}

/// Matches `MediaEngine::register_default_codecs`'s Opus entry exactly --
/// every `PeerConnectionFactory`-built connection registers that exact
/// codec set, and matching keys on all of `RTCRtpCodec`'s fields together,
/// not payload type alone. A mismatched `sdp_fmtp_line` here would let
/// negotiation complete against a *different* codec entry while the
/// packets this service actually sends carry a payload type the far end
/// never negotiated for this track -- silently dropped, not an error.
fn opus_codec() -> RTCRtpCodec {
    RTCRtpCodec {
        mime_type: "audio/opus".to_string(),
        clock_rate: 48000,
        channels: 2,
        sdp_fmtp_line: "minptime=10;useinbandfec=1".to_string(),
        rtcp_feedback: vec![],
    }
}

/// `POST /whep/{community_id}/{pipeline_id}` -- creates a new WHEP viewer
/// session for `pipeline_id`. `community_id` is accepted for URL shape
/// parity with the REST control-plane (`/communities/{community_id}/...`
/// in `src/api/mod.rs`) but not yet validated against the pipeline's own
/// `community_id` -- that check needs `pipeline::supervisor`'s spec lookup
/// (S3, not yet implemented) and is a follow-up once it exists, not a
/// silent gap: a mismatched `community_id` currently still resolves by
/// `pipeline_id` alone.
async fn create_session(
    State(state): State<Arc<WhepState>>,
    Path((_community_id, pipeline_id)): Path<(String, PipelineId)>,
    body: String,
) -> Result<Response, ApiError> {
    let fanouts = state
        .fanouts
        .read()
        .await
        .get(&pipeline_id)
        .cloned()
        .ok_or_else(|| {
            ApiError::NotFound(format!("no active source for pipeline {pipeline_id}"))
        })?;

    if state.viewer_count(pipeline_id).await >= state.max_viewers {
        return Err(ApiError::Forbidden(format!(
            "WHEP_MAX_VIEWERS ({}) reached for this pipeline",
            state.max_viewers
        )));
    }

    let offer = RTCSessionDescription::offer(body)
        .map_err(|err| ApiError::BadRequest(format!("invalid SDP offer: {err}")))?;

    let gather_complete = Arc::new(Notify::new());
    let handler = Arc::new(WhepViewerHandler {
        gather_complete: Arc::clone(&gather_complete),
        metrics: state.metrics.clone(),
    });

    let pc = state
        .factory
        .build(handler)
        .await
        .map_err(|err| ApiError::Internal(anyhow::anyhow!(err.to_string())))?;

    let runtime = state.factory.runtime();
    let (video_track, video_ssrc) = viewer_track(RtpCodecKind::Video, h264_codec());
    let (audio_track, audio_ssrc) = viewer_track(RtpCodecKind::Audio, opus_codec());

    for track in [
        Arc::clone(&video_track) as Arc<dyn TrackLocal>,
        Arc::clone(&audio_track) as Arc<dyn TrackLocal>,
    ] {
        if let Err(err) = pc.add_track(track).await {
            let _ = pc.close().await;
            return Err(ApiError::Internal(anyhow::anyhow!(err.to_string())));
        }
    }

    spawn_viewer_forward(
        &*runtime,
        fanouts.video,
        video_track,
        video_ssrc,
        state.metrics.clone(),
    );
    spawn_viewer_forward(
        &*runtime,
        fanouts.audio,
        audio_track,
        audio_ssrc,
        state.metrics.clone(),
    );

    if let Err(err) = pc.set_remote_description(offer).await {
        let _ = pc.close().await;
        return Err(ApiError::BadRequest(format!("offer rejected: {err}")));
    }
    let answer = match pc.create_answer(None).await {
        Ok(answer) => answer,
        Err(err) => {
            let _ = pc.close().await;
            return Err(ApiError::Internal(anyhow::anyhow!(err.to_string())));
        }
    };
    if let Err(err) = pc.set_local_description(answer).await {
        let _ = pc.close().await;
        return Err(ApiError::Internal(anyhow::anyhow!(err.to_string())));
    }

    if tokio::time::timeout(GATHER_TIMEOUT, gather_complete.notified())
        .await
        .is_err()
    {
        let _ = pc.close().await;
        return Err(ApiError::Internal(anyhow::anyhow!(
            "ICE gathering did not complete within {GATHER_TIMEOUT:?}"
        )));
    }

    let answer_sdp = match pc.local_description().await {
        Some(desc) => desc.sdp,
        None => {
            let _ = pc.close().await;
            return Err(ApiError::Internal(anyhow::anyhow!(
                "no local description after gathering completed"
            )));
        }
    };

    let session_id = Uuid::new_v4();
    state
        .sessions
        .write()
        .await
        .insert(session_id, WhepViewerSession { pipeline_id, pc });
    state
        .metrics
        .sessions_active
        .with_label_values(&["whep"])
        .inc();

    let location = format!("/whep/{_community_id}/{pipeline_id}/{session_id}");
    Ok((
        StatusCode::CREATED,
        [
            (header::CONTENT_TYPE, "application/sdp".to_string()),
            (header::LOCATION, location),
        ],
        answer_sdp,
    )
        .into_response())
}

/// `DELETE /whep/{community_id}/{pipeline_id}/{session_id}` -- tears a
/// viewer session down.
async fn teardown_session(
    State(state): State<Arc<WhepState>>,
    Path((_community_id, pipeline_id, session_id)): Path<(String, PipelineId, Uuid)>,
) -> Result<StatusCode, ApiError> {
    let session = {
        let mut sessions = state.sessions.write().await;
        match sessions.remove(&session_id) {
            Some(session) if session.pipeline_id == pipeline_id => Some(session),
            Some(other) => {
                sessions.insert(session_id, other);
                None
            }
            None => None,
        }
    };

    let Some(session) = session else {
        return Err(ApiError::NotFound(format!(
            "no WHEP session {session_id} for this pipeline"
        )));
    };

    let _ = session.pc.close().await;
    state
        .metrics
        .sessions_active
        .with_label_values(&["whep"])
        .dec();

    Ok(StatusCode::NO_CONTENT)
}

/// Builds the WHEP axum sub-router. Not yet mounted onto the shared
/// control-plane router -- see `src/ingest/whip.rs::router`'s doc comment
/// for the identical integration-gap rationale.
pub fn router(state: Arc<WhepState>) -> Router {
    Router::new()
        .route("/whep/{community_id}/{pipeline_id}", post(create_session))
        .route(
            "/whep/{community_id}/{pipeline_id}/{session_id}",
            delete(teardown_session),
        )
        .with_state(state)
}

/// [`OutputSink`] stub for `OutputSpec::Whep` -- see this module's doc
/// comment for why resolving a `PipelineId` to a fanout is
/// `pipeline::supervisor`'s job (S3, not yet implemented), which this
/// trait implementation cannot itself perform.
#[derive(Debug, Clone, Default)]
pub struct WhepSink;

impl OutputSink for WhepSink {
    async fn start(&self, pipeline_id: PipelineId, _spec: OutputSpec) -> Result<(), SinkError> {
        tracing::debug!(%pipeline_id, "WhepSink::start is not yet implemented -- see src/egress/whep.rs module docs");
        Err(SinkError::Unimplemented("WhepSink::start"))
    }

    async fn stop(&self, pipeline_id: PipelineId) -> Result<(), SinkError> {
        tracing::debug!(%pipeline_id, "WhepSink::stop is not yet implemented -- see src/egress/whep.rs module docs");
        Err(SinkError::Unimplemented("WhepSink::stop"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn start_reports_unimplemented() {
        let sink = WhepSink;
        let spec = OutputSpec::Whep {
            profile: "1080p60".into(),
        };
        let err = sink.start(Uuid::nil(), spec).await.unwrap_err();
        assert!(matches!(err, SinkError::Unimplemented("WhepSink::start")));
    }

    #[tokio::test]
    async fn stop_reports_unimplemented() {
        let sink = WhepSink;
        let err = sink.stop(Uuid::nil()).await.unwrap_err();
        assert!(matches!(err, SinkError::Unimplemented("WhepSink::stop")));
    }

    #[tokio::test]
    async fn viewer_count_is_zero_for_unknown_pipeline() {
        let registry = prometheus::Registry::new();
        let metrics = RtcMetrics::register(&registry).unwrap();
        let factory = Arc::new(
            PeerConnectionFactory::new(crate::rtc::config::RtcConfig {
                bind_ip: "127.0.0.1".parse().unwrap(),
                udp_port_range: (41400, 41410),
                nat_1to1_ip: None,
            })
            .unwrap(),
        );
        let state = WhepState::new(factory, metrics, DEFAULT_MAX_VIEWERS);
        assert_eq!(state.viewer_count(Uuid::new_v4()).await, 0);
    }

    #[tokio::test]
    async fn register_and_unregister_fanout_round_trips() {
        let registry = prometheus::Registry::new();
        let metrics = RtcMetrics::register(&registry).unwrap();
        let factory = Arc::new(
            PeerConnectionFactory::new(crate::rtc::config::RtcConfig {
                bind_ip: "127.0.0.1".parse().unwrap(),
                udp_port_range: (41420, 41430),
                nat_1to1_ip: None,
            })
            .unwrap(),
        );
        let state = WhepState::new(factory, metrics, DEFAULT_MAX_VIEWERS);
        let pipeline_id = Uuid::new_v4();
        assert!(state.fanouts.read().await.get(&pipeline_id).is_none());

        state
            .register_fanout(pipeline_id, MediaFanouts::new())
            .await;
        assert!(state.fanouts.read().await.get(&pipeline_id).is_some());

        state.unregister_fanout(pipeline_id).await;
        assert!(state.fanouts.read().await.get(&pipeline_id).is_none());
    }
}
