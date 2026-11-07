//! Shared helpers for `tests/rtc_*.rs` -- not itself a test binary (`mod.rs`
//! under a `tests/rtc_common/` subdirectory is cargo's standard way to share
//! code between integration-test crates without it being picked up as its
//! own `#[test]` target).
//!
//! `dead_code` is allowed crate-wide for this module: each `tests/rtc_*.rs`
//! binary only uses a subset of these helpers, so from any single binary's
//! point of view the rest are legitimately "unused" -- the usual reason to
//! silence that lint for a shared test-support module.
#![allow(dead_code)]

use std::net::IpAddr;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

use async_trait::async_trait;
use axum::body::Body;
use axum::http::{header, Request, StatusCode};
use rtc::rtp_transceiver::rtp_sender::{
    RTCRtpCodec, RTCRtpCodingParameters, RTCRtpEncodingParameters, RtpCodecKind,
};
use tokio::sync::Notify;
use tower::ServiceExt;
use uuid::Uuid;
use webrtc::media_stream::track_local::static_rtp::TrackLocalStaticRTP;
use webrtc::media_stream::track_remote::{TrackRemote, TrackRemoteEvent};
use webrtc::media_stream::MediaStreamTrack;
use webrtc::peer_connection::{
    PeerConnection, PeerConnectionEventHandler, RTCIceGatheringState, RTCPeerConnectionState,
    RTCSessionDescription,
};
use webrtc::rtp_transceiver::{RTCRtpTransceiverDirection, RTCRtpTransceiverInit};

use svc_streaming::rtc::config::RtcConfig;
use svc_streaming::rtc::ingest_auth::{IngestAuthError, WhipTokenAuthorizer};
use svc_streaming::rtc::pc_factory::PeerConnectionFactory;

/// A [`WhipTokenAuthorizer`] fake that authorizes every token -- these
/// tests exercise `src/ingest/whip.rs`'s own behavior, not
/// `src/rtc/ingest_auth.rs`'s HTTP client (covered separately by that
/// module's own unit tests).
pub struct AllowAllAuthorizer;

#[async_trait]
impl WhipTokenAuthorizer for AllowAllAuthorizer {
    async fn authorize(&self, _token: &str) -> Result<bool, IngestAuthError> {
        Ok(true)
    }
}

/// A [`WhipTokenAuthorizer`] fake that denies every token.
pub struct DenyAllAuthorizer;

#[async_trait]
impl WhipTokenAuthorizer for DenyAllAuthorizer {
    async fn authorize(&self, _token: &str) -> Result<bool, IngestAuthError> {
        Ok(false)
    }
}

/// Builds an [`RtcConfig`] bound to loopback with an explicit, small port
/// range -- callers pass non-overlapping ranges so concurrently-running
/// `PeerConnectionFactory`s in one test never race for the same port.
pub fn loopback_rtc_config(range: (u16, u16)) -> RtcConfig {
    RtcConfig {
        bind_ip: "127.0.0.1".parse().unwrap(),
        udp_port_range: range,
        nat_1to1_ip: None,
    }
}

pub fn loopback_ip() -> IpAddr {
    "127.0.0.1".parse().unwrap()
}

/// No-op [`PeerConnectionEventHandler`] for a "raw" test peer (the external
/// WHIP publisher / WHEP viewer stand-in) that only needs to track ICE
/// gathering completion and connection state.
#[derive(Clone)]
pub struct TestPeerHandler {
    pub gather_complete: Arc<Notify>,
    pub connected: Arc<Notify>,
    /// Bumped once per received RTP packet on any remote track -- used by
    /// the WHEP-viewer side of the round-trip test.
    pub packets_received: Arc<AtomicUsize>,
}

impl TestPeerHandler {
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            gather_complete: Arc::new(Notify::new()),
            connected: Arc::new(Notify::new()),
            packets_received: Arc::new(AtomicUsize::new(0)),
        })
    }
}

#[async_trait]
impl PeerConnectionEventHandler for TestPeerHandler {
    async fn on_ice_gathering_state_change(&self, state: RTCIceGatheringState) {
        if state == RTCIceGatheringState::Complete {
            self.gather_complete.notify_one();
        }
    }

    async fn on_connection_state_change(&self, state: RTCPeerConnectionState) {
        if state == RTCPeerConnectionState::Connected {
            self.connected.notify_one();
        }
    }

    async fn on_track(&self, track: Arc<dyn TrackRemote>) {
        let counter = Arc::clone(&self.packets_received);
        tokio::spawn(async move {
            while let Some(event) = track.poll().await {
                if let TrackRemoteEvent::OnRtpPacket(_packet) = event {
                    counter.fetch_add(1, Ordering::SeqCst);
                }
            }
        });
    }
}

/// Must match `MediaEngine::register_default_codecs`'s own Opus entry
/// exactly (mime type, clock rate, channels, **and** `sdp_fmtp_line`) --
/// `PeerConnectionFactory::build` registers that exact set on every
/// connection it builds, and codec matching during offer/answer generation
/// keys on all of those fields together, not payload type alone. A
/// mismatched `sdp_fmtp_line` here previously made this round trip flaky:
/// negotiation still completed (a different codec entry matched), but the
/// synthetic packets' payload type didn't correspond to what was actually
/// negotiated, so the receiver never surfaced them via `on_track`.
pub fn opus_codec() -> RTCRtpCodec {
    RTCRtpCodec {
        mime_type: "audio/opus".to_string(),
        clock_rate: 48000,
        channels: 2,
        sdp_fmtp_line: "minptime=10;useinbandfec=1".to_string(),
        rtcp_feedback: vec![],
    }
}

/// Builds a send-only Opus local track with a fresh SSRC -- the simplest
/// codec to round-trip reliably (a single default registration, no fmtp
/// negotiation), used to stand in for the "synthetic ... Opus track" the
/// task spec calls for. H264/video transceiver negotiation itself is
/// covered by `src/rtc/pc_factory.rs`'s own loopback unit tests
/// (`builds_a_peer_connection_on_loopback` et al.) and by
/// `offered_media_kinds` detecting an `m=video` line -- this round trip
/// stays audio-only to avoid H264 profile-level-id/fmtp matching between
/// two independently-built `MediaEngine`s becoming a source of test
/// flakiness unrelated to what this suite is actually verifying (WHIP/WHEP
/// session lifecycle + SFU fan-out).
pub fn opus_publisher_track() -> (Arc<TrackLocalStaticRTP>, u32) {
    let ssrc = Uuid::new_v4().as_u128() as u32;
    let stream_id = format!("test-publisher-{ssrc}");
    let track = Arc::new(TrackLocalStaticRTP::new(MediaStreamTrack::new(
        stream_id.clone(),
        stream_id.clone(),
        stream_id,
        RtpCodecKind::Audio,
        vec![RTCRtpEncodingParameters {
            rtp_coding_parameters: RTCRtpCodingParameters {
                ssrc: Some(ssrc),
                ..Default::default()
            },
            codec: opus_codec(),
            ..Default::default()
        }],
    )));
    (track, ssrc)
}

/// Builds a synthetic RTP packet carrying a fixed dummy payload -- stands
/// in for an encoded Opus frame. Currently unused by any `tests/rtc_*.rs`
/// file: see `src/rtc/mod.rs`'s documented in-process RTP delivery gap --
/// none of this suite's tests currently write real RTP through a
/// negotiated `PeerConnection` pair and assert delivery, so this is kept
/// as ready-to-use infrastructure for whoever verifies that path against a
/// real browser/second process.
pub fn synthetic_packet(seq: u16, ssrc: u32) -> rtc::rtp::Packet {
    rtc::rtp::Packet {
        header: rtc::rtp::Header {
            version: 2,
            sequence_number: seq,
            timestamp: u32::from(seq) * 960,
            ssrc,
            payload_type: 111,
            marker: true,
            ..Default::default()
        },
        payload: bytes::Bytes::from_static(b"synthetic-opus-frame"),
    }
}

/// Builds a receive-only test viewer `PeerConnection` (audio only, mirrors
/// [`opus_publisher_track`]'s codec choice) against `factory`, returning it
/// alongside its event handler (for `packets_received`/`connected`).
pub async fn build_recvonly_peer(
    factory: &PeerConnectionFactory,
) -> (Arc<dyn PeerConnection>, Arc<TestPeerHandler>) {
    let handler = TestPeerHandler::new();
    let pc = factory
        .build(Arc::clone(&handler))
        .await
        .expect("test peer connection builds");
    pc.add_transceiver_from_kind(
        RtpCodecKind::Audio,
        Some(RTCRtpTransceiverInit {
            direction: RTCRtpTransceiverDirection::Recvonly,
            ..Default::default()
        }),
    )
    .await
    .expect("add recvonly audio transceiver");
    (pc, handler)
}

/// Drives `pc` through `create_offer` -> `set_local_description` -> wait
/// for ICE gathering to complete, returning the offer SDP text ready to
/// `POST` to a WHIP/WHEP router.
pub async fn offer_sdp(pc: &Arc<dyn PeerConnection>, gather_complete: &Arc<Notify>) -> String {
    let offer = pc.create_offer(None).await.expect("create_offer");
    pc.set_local_description(offer)
        .await
        .expect("set_local_description(offer)");
    tokio::time::timeout(
        std::time::Duration::from_secs(5),
        gather_complete.notified(),
    )
    .await
    .expect("ICE gathering completes within 5s");
    pc.local_description()
        .await
        .expect("local description present after gathering")
        .sdp
}

/// Applies a WHIP/WHEP-router-returned answer SDP text to `pc`.
pub async fn apply_answer(pc: &Arc<dyn PeerConnection>, answer_sdp: String) {
    let answer = RTCSessionDescription::answer(answer_sdp).expect("wrap answer SDP");
    pc.set_remote_description(answer)
        .await
        .expect("set_remote_description(answer)");
}

/// Sends `req` through `router` via `tower::ServiceExt::oneshot` and
/// returns the response, without needing a bound TCP listener -- the
/// axum `Router` returned by `whip::router`/`whep::router` is already a
/// complete `tower::Service<Request<Body>>`.
pub async fn oneshot(router: axum::Router, req: Request<Body>) -> axum::response::Response {
    router.oneshot(req).await.expect("router handles request")
}

pub fn post_sdp(path: &str, body: impl Into<String>) -> Request<Body> {
    Request::builder()
        .method("POST")
        .uri(path)
        .header(header::CONTENT_TYPE, "application/sdp")
        .body(Body::from(body.into()))
        .unwrap()
}

pub fn delete_request(path: &str) -> Request<Body> {
    Request::builder()
        .method("DELETE")
        .uri(path)
        .body(Body::empty())
        .unwrap()
}

pub fn patch_request(path: &str) -> Request<Body> {
    Request::builder()
        .method("PATCH")
        .uri(path)
        .body(Body::empty())
        .unwrap()
}

/// Extracts the `Location` header's value from a response, panicking with
/// a useful message if it's absent -- every successful WHIP/WHEP `POST`
/// must carry one.
pub fn location_of(response: &axum::response::Response) -> String {
    response
        .headers()
        .get(header::LOCATION)
        .expect("Location header present")
        .to_str()
        .unwrap()
        .to_string()
}

pub async fn body_text(response: axum::response::Response) -> String {
    let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .expect("read response body");
    String::from_utf8(bytes.to_vec()).expect("response body is UTF-8")
}

pub fn assert_status(response: &axum::response::Response, expected: StatusCode) {
    assert_eq!(
        response.status(),
        expected,
        "unexpected status (body not shown -- caller can add {{:?}} if debugging)"
    );
}
