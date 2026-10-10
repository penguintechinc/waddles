//! WHIP (WebRTC-HTTP Ingestion Protocol, IETF draft) ingest -- chunk S6.
//! Declares an axum sub-router (`router`) a later integration step mounts
//! onto the shared control-plane HTTP port (`MODULE_PORT`) -- WHIP
//! signaling has no dedicated port, per this module's original doc comment
//! and `docs/plans/2026-09-11-svc-streaming-pipeline-matrix.md` §2/§8.
//!
//! # Flow
//!
//! `POST /whip/{token}` (SDP offer, `Content-Type: application/sdp`):
//! 1. Authorizes `token` via [`crate::rtc::ingest_auth::WhipTokenAuthorizer`].
//! 2. Builds a receive-only ingest `PeerConnection`
//!    ([`crate::rtc::pc_factory::PeerConnectionFactory`]).
//! 3. Every received RTP packet feeds **both** the **copy** path (a
//!    [`crate::rtc::fanout::TrackFanout`] a WHEP viewer can subscribe to
//!    directly, no ffmpeg -- pipeline matrix §1/§7 case 6) **and** the
//!    **transcode** path (a [`crate::rtc::sdp_writer::WhipTranscodeBridge`]
//!    writing `input.sdp` + forwarding to local UDP, for a later ffmpeg
//!    process to read -- §2). The handler does both unconditionally: it has
//!    no way to know in advance whether any pipeline wants a transcode.
//! 4. Non-trickle ICE: waits for `RTCIceGatheringState::Complete` before
//!    answering, so `PATCH` (trickle) is not needed --
//!    [`trickle_ice_not_supported`] returns `405` for it.
//! 5. Pushes an [`IngestSession`] onto the shared ingest channel so
//!    `pipeline::supervisor` (S3, not yet implemented) can pick it up by
//!    `key` (the WHIP token) -- see [`WhipState`]'s doc comment for the
//!    `sdp_path` side-map this uses instead of extending [`IngestSession`].
//!
//! `DELETE /whip/{token}/{session_id}` tears the session down; `PATCH
//! /whip/{token}/{session_id}` is the trickle-ICE endpoint the WHIP spec
//! allows but this server does not implement (non-trickle only).

use std::collections::HashMap;
use std::net::IpAddr;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use axum::extract::{Path, State};
use axum::http::{header, HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{delete, post};
use axum::Router;
use rtc::rtp_transceiver::rtp_sender::RtpCodecKind;
use tokio::sync::{mpsc, Notify, RwLock};
use uuid::Uuid;
use webrtc::media_stream::track_remote::{TrackRemote, TrackRemoteEvent};
use webrtc::peer_connection::{
    PeerConnection, PeerConnectionEventHandler, RTCIceGatheringState, RTCPeerConnectionState,
    RTCSessionDescription,
};
use webrtc::rtp_transceiver::{RTCRtpTransceiverDirection, RTCRtpTransceiverInit};
use webrtc::runtime::Runtime;

use crate::error::ApiError;
use crate::ingest::{IngestKind, IngestListener, IngestSession};
use crate::redact::fingerprint;
use crate::rtc::fanout::MediaFanouts;
use crate::rtc::ingest_auth::WhipTokenAuthorizer;
use crate::rtc::metrics::RtcMetrics;
use crate::rtc::pc_factory::PeerConnectionFactory;
use crate::rtc::sdp_writer::WhipTranscodeBridge;

/// How long a session waits for non-trickle ICE gathering to complete
/// before the `POST` is failed. Loopback/LAN gathering with only host
/// candidates (no STUN/TURN round trip) normally completes in well under a
/// second; this bounds the pathological case (e.g. an interface with no
/// usable address) instead of hanging the HTTP request forever.
const GATHER_TIMEOUT: Duration = Duration::from_secs(5);

/// One active WHIP publish: the pieces [`teardown_session`] needs to tear
/// down cleanly, plus the token it belongs to (checked so `DELETE
/// /whip/{other_token}/{session_id}` 404s instead of tearing down a
/// different token's session).
struct WhipSession {
    token: String,
    pc: Arc<dyn PeerConnection>,
}

/// Shared state for every WHIP session in the process. `router` is the
/// axum entrypoint; [`WhipListener`] (kept for [`IngestListener`]
/// trait-compatibility, see its doc comment) does not itself hold this --
/// a later integration step (outside this chunk: `src/http/mod.rs` /
/// `src/lib.rs`) constructs one `WhipState` and passes the same `Arc` to
/// both `router()` and whatever spawns `WhipListener` in the ingest
/// `JoinSet`.
///
/// **`IngestSession.sdp_path` side-map:** `src/ingest/mod.rs`'s
/// [`IngestSession`] has no `sdp_path` field to extend additively -- that
/// file is owned by a different chunk and out of this chunk's edit scope.
/// [`WhipState::sdp_path_for`] is the side-map the task spec calls for:
/// whichever chunk wires ffmpeg spawning (S3) looks up the `input.sdp` path
/// for a session's `key` (the WHIP token) here instead of through
/// `IngestSession` itself.
pub struct WhipState {
    factory: Arc<PeerConnectionFactory>,
    authorizer: Arc<dyn WhipTokenAuthorizer>,
    ingest_tx: mpsc::Sender<IngestSession>,
    stream_data_dir: PathBuf,
    bind_ip: IpAddr,
    metrics: RtcMetrics,
    /// Keyed by WHIP token -- the copy-path video+audio fanouts every WHEP
    /// viewer of a pure-copy pipeline subscribes to directly.
    fanouts: RwLock<HashMap<String, MediaFanouts>>,
    /// Keyed by WHIP token -- the transcode-path `input.sdp` + UDP
    /// forwarder bridge.
    bridges: RwLock<HashMap<String, Arc<WhipTranscodeBridge>>>,
    /// Keyed by session id (from the `Location` header / DELETE path).
    sessions: RwLock<HashMap<Uuid, WhipSession>>,
}

impl WhipState {
    /// Builds shared WHIP state. `stream_data_dir` and `bind_ip` come from
    /// `crate::config::Config` (`STREAM_DATA_DIR`, `BIND_ADDR`) -- passed
    /// as plain values rather than `&Config` so a caller building
    /// [`crate::rtc::config::RtcConfig`] and this state from the same
    /// `Config` does not need this module to depend on `crate::config`
    /// beyond what it already re-derives.
    pub fn new(
        factory: Arc<PeerConnectionFactory>,
        authorizer: Arc<dyn WhipTokenAuthorizer>,
        ingest_tx: mpsc::Sender<IngestSession>,
        stream_data_dir: PathBuf,
        bind_ip: IpAddr,
        metrics: RtcMetrics,
    ) -> Self {
        Self {
            factory,
            authorizer,
            ingest_tx,
            stream_data_dir,
            bind_ip,
            metrics,
            fanouts: RwLock::new(HashMap::new()),
            bridges: RwLock::new(HashMap::new()),
            sessions: RwLock::new(HashMap::new()),
        }
    }

    /// The copy-path video+audio fanouts for a WHIP token, if a session is
    /// currently publishing under it -- what `egress::whep`'s copy path
    /// subscribes to.
    pub async fn fanout_for(&self, token: &str) -> Option<MediaFanouts> {
        self.fanouts.read().await.get(token).cloned()
    }

    /// The `input.sdp` path for a WHIP token's transcode bridge, if one is
    /// active -- see this struct's doc comment on why this is a side-map
    /// instead of an [`IngestSession`] field.
    pub async fn sdp_path_for(&self, token: &str) -> Option<PathBuf> {
        self.bridges
            .read()
            .await
            .get(token)
            .map(|bridge| bridge.sdp_path.clone())
    }
}

/// Detects which media kinds an SDP offer declares, by scanning for `m=`
/// lines -- good enough to decide whether to open a video/audio
/// transceiver and whether the transcode bridge needs a video/audio leg;
/// a malformed offer with no matching `m=` line simply gets no
/// transceiver of that kind, which `set_remote_description` will reject
/// on its own if the offer is otherwise invalid.
fn offered_media_kinds(offer_sdp: &str) -> (bool, bool) {
    let mut has_video = false;
    let mut has_audio = false;
    for line in offer_sdp.lines() {
        if line.starts_with("m=video") {
            has_video = true;
        } else if line.starts_with("m=audio") {
            has_audio = true;
        }
    }
    (has_video, has_audio)
}

/// `PeerConnectionEventHandler` for one WHIP publish: republishes every
/// received RTP packet into both the copy-path fanout (matched by media
/// kind -- see [`crate::rtc::fanout::MediaFanouts`]) and the
/// transcode-path bridge, and signals ICE-gathering completion for the
/// non-trickle `POST` handler to wait on.
struct WhipIngestHandler {
    fanouts: MediaFanouts,
    bridge: Arc<WhipTranscodeBridge>,
    gather_complete: Arc<Notify>,
    metrics: RtcMetrics,
    runtime: Arc<dyn Runtime>,
}

#[async_trait]
impl PeerConnectionEventHandler for WhipIngestHandler {
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

    async fn on_track(&self, track: Arc<dyn TrackRemote>) {
        let kind = track.kind().await;
        tracing::debug!(?kind, "WHIP ingest on_track fired");
        let fanouts = self.fanouts.clone();
        let bridge = Arc::clone(&self.bridge);
        let metrics = self.metrics.clone();
        self.runtime.spawn(Box::pin(async move {
            while let Some(event) = track.poll().await {
                let TrackRemoteEvent::OnRtpPacket(packet) = event else {
                    continue;
                };
                metrics
                    .rtp_packets_total
                    .with_label_values(&["ingress"])
                    .inc();
                // Route by this track's own kind -- never by inspecting the
                // packet -- so a video packet only ever reaches the video
                // fanout/bridge leg, never an audio viewer's track.
                match kind {
                    RtpCodecKind::Video => {
                        bridge.forward_video(&packet).await;
                        fanouts.video.publish(packet);
                    }
                    RtpCodecKind::Audio => {
                        bridge.forward_audio(&packet).await;
                        fanouts.audio.publish(packet);
                    }
                    _ => {}
                }
            }
        }));
    }
}

/// `POST /whip/{token}` -- creates a new WHIP publish session.
async fn create_session(
    State(state): State<Arc<WhipState>>,
    Path(token): Path<String>,
    headers: HeaderMap,
    body: axum::body::Bytes,
) -> Result<Response, ApiError> {
    let content_type = headers
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .unwrap_or_default();
    if !content_type.starts_with("application/sdp") {
        return Err(ApiError::BadRequest(
            "Content-Type must be application/sdp".into(),
        ));
    }

    let allowed = state
        .authorizer
        .authorize(&token)
        .await
        .map_err(|err| ApiError::Internal(anyhow::anyhow!(err.to_string())))?;
    if !allowed {
        return Err(ApiError::Unauthorized(
            "WHIP token is not authorized for any pipeline input".into(),
        ));
    }

    let offer_sdp = String::from_utf8(body.to_vec())
        .map_err(|_| ApiError::BadRequest("offer body is not valid UTF-8".into()))?;
    let (has_video, has_audio) = offered_media_kinds(&offer_sdp);
    if !has_video && !has_audio {
        return Err(ApiError::BadRequest(
            "SDP offer declares no m=video or m=audio section".into(),
        ));
    }
    let offer = RTCSessionDescription::offer(offer_sdp)
        .map_err(|err| ApiError::BadRequest(format!("invalid SDP offer: {err}")))?;

    let runtime = state.factory.runtime();
    let bridge = Arc::new(
        WhipTranscodeBridge::create(
            &*runtime,
            &state.stream_data_dir,
            &token,
            state.bind_ip,
            has_video,
            has_audio,
        )
        .map_err(|err| ApiError::Internal(err.into()))?,
    );
    let fanouts = MediaFanouts::new();
    let gather_complete = Arc::new(Notify::new());

    let handler = Arc::new(WhipIngestHandler {
        fanouts: fanouts.clone(),
        bridge: Arc::clone(&bridge),
        gather_complete: Arc::clone(&gather_complete),
        metrics: state.metrics.clone(),
        runtime: Arc::clone(&runtime),
    });

    let pc = state
        .factory
        .build(handler)
        .await
        .map_err(|err| ApiError::Internal(anyhow::anyhow!(err.to_string())))?;

    if has_video {
        pc.add_transceiver_from_kind(
            RtpCodecKind::Video,
            Some(RTCRtpTransceiverInit {
                direction: RTCRtpTransceiverDirection::Recvonly,
                ..Default::default()
            }),
        )
        .await
        .map_err(|err| ApiError::Internal(anyhow::anyhow!(err.to_string())))?;
    }
    if has_audio {
        pc.add_transceiver_from_kind(
            RtpCodecKind::Audio,
            Some(RTCRtpTransceiverInit {
                direction: RTCRtpTransceiverDirection::Recvonly,
                ..Default::default()
            }),
        )
        .await
        .map_err(|err| ApiError::Internal(anyhow::anyhow!(err.to_string())))?;
    }

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

    // Replace any prior session for this token (a republish/reconnect) --
    // close the old PeerConnection so it doesn't keep forwarding into a
    // fanout/bridge pair that's about to be replaced.
    {
        let mut sessions = state.sessions.write().await;
        sessions.retain(|_, session| {
            if session.token == token {
                let stale_pc = Arc::clone(&session.pc);
                tokio::spawn(async move {
                    let _ = stale_pc.close().await;
                });
                false
            } else {
                true
            }
        });
    }

    state.fanouts.write().await.insert(token.clone(), fanouts);
    state.bridges.write().await.insert(token.clone(), bridge);

    let session_id = Uuid::new_v4();
    state.sessions.write().await.insert(
        session_id,
        WhipSession {
            token: token.clone(),
            pc,
        },
    );

    state
        .metrics
        .sessions_active
        .with_label_values(&["whip"])
        .inc();

    // Best-effort hand-off to the pipeline supervisor (S3, not yet
    // implemented) -- media is already flowing through the fanout/bridge
    // regardless of whether anything downstream is listening on this
    // channel yet, so a full channel or a dropped receiver is logged, not
    // failed back to the WHIP publisher.
    if state
        .ingest_tx
        .try_send(IngestSession {
            kind: IngestKind::Whip,
            key: token.clone(),
            stream: Box::new(tokio::io::empty()),
            span: tracing::Span::current(),
        })
        .is_err()
    {
        // The token is a bearer credential: log only its fingerprint,
        // never the raw value, at any level.
        tracing::debug!(
            key_hash = %fingerprint(&token),
            "ingest channel full or no receiver -- WHIP session accepted, pipeline hand-off skipped"
        );
    }

    let location = format!("/whip/{token}/{session_id}");
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

/// `DELETE /whip/{token}/{session_id}` -- tears a session down.
async fn teardown_session(
    State(state): State<Arc<WhipState>>,
    Path((token, session_id)): Path<(String, Uuid)>,
) -> Result<StatusCode, ApiError> {
    let session = {
        let mut sessions = state.sessions.write().await;
        match sessions.remove(&session_id) {
            Some(session) if session.token == token => Some(session),
            Some(other) => {
                // Session exists but under a different token -- put it back
                // and treat this as "not found for this token" (403 would
                // leak whether the id exists at all under another token).
                sessions.insert(session_id, other);
                None
            }
            None => None,
        }
    };

    let Some(session) = session else {
        return Err(ApiError::NotFound(format!(
            "no WHIP session {session_id} for token"
        )));
    };

    let _ = session.pc.close().await;
    state.fanouts.write().await.remove(&token);
    state.bridges.write().await.remove(&token);
    state
        .metrics
        .sessions_active
        .with_label_values(&["whip"])
        .dec();

    Ok(StatusCode::NO_CONTENT)
}

/// `PATCH /whip/{token}/{session_id}` -- trickle ICE. This server always
/// waits for full ICE gathering before answering (see [`GATHER_TIMEOUT`]),
/// so there is never a trickle candidate to accept; `405` with an `Allow`
/// header is the correct response for a method the resource understands
/// but does not support, per RFC 9110 §15.5.6, rather than a bare `404`.
async fn trickle_ice_not_supported() -> Response {
    (
        StatusCode::METHOD_NOT_ALLOWED,
        [(header::ALLOW, "DELETE")],
        "trickle ICE is not supported -- this server waits for full ICE \
         gathering before answering the initial offer (non-trickle only)",
    )
        .into_response()
}

/// Builds the WHIP axum sub-router. Not yet mounted onto the shared
/// control-plane router (`src/http/mod.rs::router`, outside this chunk's
/// file ownership) -- a later integration step nests this at `/whip`.
pub fn router(state: Arc<WhipState>) -> Router {
    Router::new()
        .route("/whip/{token}", post(create_session))
        .route(
            "/whip/{token}/{session_id}",
            delete(teardown_session).patch(trickle_ice_not_supported),
        )
        .with_state(state)
}

/// WHIP listener configuration -- kept for [`IngestListener`]
/// trait-compatibility with `ingest::rtmp`/`ingest::srt` (S4/S5), which
/// bind a dedicated port and loop forever accepting connections. WHIP has
/// no equivalent socket to bind: signaling rides the shared control-plane
/// HTTP port via [`router`], mounted by an integration step outside this
/// chunk's file ownership (see [`WhipState`]'s doc comment). `run` below
/// therefore does not implement a bind/accept loop -- there is nothing to
/// bind -- it just keeps `tx` alive and satisfies `IngestListener`'s
/// "runs forever until `tx` is dropped" contract.
#[derive(Debug, Clone)]
pub struct WhipListener {
    pub udp_port_range: (u16, u16),
}

impl IngestListener for WhipListener {
    async fn run(self, tx: mpsc::Sender<IngestSession>) -> anyhow::Result<()> {
        tracing::info!(
            udp_port_range = ?self.udp_port_range,
            "WHIP ingest is HTTP-hosted (router() on the shared control-plane \
             port), not a dedicated listener -- see src/ingest/whip.rs module docs"
        );
        // Hold `tx` so `IngestListener::run`'s "loops until `tx` is
        // dropped" contract is satisfied even though this implementation
        // never sends on it directly -- `WhipState::ingest_tx` (a clone
        // handed to `router()`'s caller) is what real WHIP sessions send
        // through.
        let _tx = tx;
        std::future::pending::<()>().await;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn offered_media_kinds_detects_both() {
        let sdp = "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n";
        assert_eq!(offered_media_kinds(sdp), (true, true));
    }

    #[test]
    fn offered_media_kinds_video_only() {
        let sdp = "v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n";
        assert_eq!(offered_media_kinds(sdp), (true, false));
    }

    #[test]
    fn offered_media_kinds_none() {
        let sdp = "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\n";
        assert_eq!(offered_media_kinds(sdp), (false, false));
    }

    #[tokio::test]
    async fn run_keeps_tx_alive_until_dropped() {
        let (tx, mut rx) = mpsc::channel(1);
        let listener = WhipListener {
            udp_port_range: (40000, 40100),
        };
        let handle = tokio::spawn(listener.run(tx));
        // `run` never resolves on its own (pending forever) -- proving the
        // trait contract ("loops until tx is dropped") means asserting the
        // task is still alive and the channel isn't closed yet, then
        // aborting rather than waiting for a future that never completes.
        tokio::time::sleep(Duration::from_millis(20)).await;
        assert!(!handle.is_finished());
        handle.abort();
        assert!(rx.try_recv().is_err());
    }
}
