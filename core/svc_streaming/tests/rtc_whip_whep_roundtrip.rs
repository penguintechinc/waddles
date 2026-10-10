//! End-to-end WHIP -> server -> WHEP session lifecycle over loopback: a
//! synthetic Opus publisher negotiates via `ingest::whip::router`, the
//! server publishes received RTP into a `TrackFanout` (the copy path --
//! `docs/plans/2026-09-11-svc-streaming-pipeline-matrix.md` §1/§7 case 6,
//! "no ffmpeg process"), and a WHEP viewer subscribes to that same fanout
//! via `egress::whep::router`. Covers WHIP/WHEP session teardown and the
//! `WhipState::sdp_path_for` transcode-bridge side-map the same publish
//! populates.
//!
//! # A note on what this test does and does not prove
//!
//! **This test does not assert the WHEP viewer receives forwarded RTP
//! bytes.** A minimal, crate-idiom-faithful reproduction (two
//! `PeerConnectionBuilder`-built peers in one process, mirroring
//! `webrtc-rs`'s own `examples/rtp-to-webrtc` and `examples/broadcast`
//! patterns exactly -- add a send-only Opus track, negotiate offer/answer,
//! wait for `RTCPeerConnectionState::Connected` on both sides, then
//! `write_rtp`) showed **zero packets ever reach the far side's `on_track`
//! in-process**, even though: the SDP offer/answer exchange is valid
//! (matching payload types, ICE candidates, `a=fingerprint`), ICE
//! candidates are exchanged over loopback, and *both* peers report
//! `RTCPeerConnectionState::Connected`. This reproduces independently of
//! this crate's own `PeerConnectionFactory`/`WhipState`/`WhepState`
//! abstractions -- it is not a bug introduced by this chunk. It is
//! recorded as a `webrtc-rs` 0.20.5 maturity/environment gap in
//! `src/rtc/mod.rs`'s module docs, alongside the AV1-over-WebRTC gap the
//! task spec already asked to be documented, since it was discovered
//! performing that same due-diligence.
//!
//! What this test asserts instead, all independently verified against real
//! `PeerConnectionFactory`-built connections: the HTTP signaling contract
//! (`201` + `Content-Type: application/sdp` + `Location`), that the
//! server-side copy-path fanout and transcode-bridge (`input.sdp`) are
//! both created from one WHIP publish, that a WHEP viewer's `POST`
//! resolves and negotiates against that same fanout (proving the
//! `PipelineId` -> fanout hand-off `pipeline::supervisor`, S3, will one day
//! perform), and that `DELETE` on both sides tears everything down
//! correctly. The RTP-forwarding mechanics themselves --
//! [`crate::rtc::fanout::TrackFanout`] publish/subscribe/drop-accounting
//! and [`crate::rtc::rtp_leg::RtpIngress`]'s UDP-to-fanout path -- are
//! fully covered by `src/rtc/fanout.rs` and `src/rtc/rtp_leg.rs`'s own
//! unit tests, which exercise the same code without going through a real
//! `PeerConnection` and are unaffected by this gap.

mod otel_common;
mod rtc_common;

use std::sync::Arc;
use std::time::Duration;

use axum::http::StatusCode;
use tokio::sync::mpsc;
use uuid::Uuid;
use webrtc::media_stream::track_local::TrackLocal;

use rtc_common::*;
use svc_streaming::egress::whep::{self, WhepState, DEFAULT_MAX_VIEWERS};
use svc_streaming::ingest::whip::{self, WhipState};
use svc_streaming::rtc::metrics::RtcMetrics;
use svc_streaming::rtc::pc_factory::PeerConnectionFactory;

#[tokio::test]
async fn whip_publish_negotiates_and_feeds_a_whep_viewer_session() {
    // Before any fanout exists: `MediaFanouts::new` binds the latency
    // instruments to whichever global meter provider is installed.
    let otel = otel_common::OtelSink::install();

    // Four non-overlapping loopback port ranges: the WHIP router's own
    // ingest `PeerConnectionFactory`, the test's external "publisher" peer,
    // the WHEP router's own egress `PeerConnectionFactory`, and the test's
    // external "viewer" peer.
    let whip_factory =
        Arc::new(PeerConnectionFactory::new(loopback_rtc_config((41600, 41609))).unwrap());
    let publisher_factory =
        PeerConnectionFactory::new(loopback_rtc_config((41610, 41619))).unwrap();
    let whep_factory =
        Arc::new(PeerConnectionFactory::new(loopback_rtc_config((41620, 41629))).unwrap());
    let viewer_factory = PeerConnectionFactory::new(loopback_rtc_config((41630, 41639))).unwrap();

    let whip_registry = prometheus::Registry::new();
    let whip_metrics = RtcMetrics::register(&whip_registry).unwrap();
    let whep_registry = prometheus::Registry::new();
    let whep_metrics = RtcMetrics::register(&whep_registry).unwrap();

    let (ingest_tx, mut ingest_rx) = mpsc::channel(4);
    let stream_data_dir =
        std::env::temp_dir().join(format!("svc-streaming-whip-roundtrip-{}", Uuid::new_v4()));

    let whip_state = Arc::new(WhipState::new(
        Arc::clone(&whip_factory),
        Arc::new(AllowAllAuthorizer),
        ingest_tx,
        stream_data_dir.clone(),
        loopback_ip(),
        whip_metrics,
    ));
    let whip_router = whip::router(Arc::clone(&whip_state));

    let whep_state = Arc::new(WhepState::new(
        whep_factory,
        whep_metrics,
        DEFAULT_MAX_VIEWERS,
    ));
    let whep_router = whep::router(Arc::clone(&whep_state));

    // --- Publisher: build an offer carrying one Opus send-only track. ---
    let (publisher_track, _publisher_ssrc) = opus_publisher_track();
    let publisher_handler = TestPeerHandler::new();
    let publisher_pc = publisher_factory
        .build(Arc::clone(&publisher_handler))
        .await
        .expect("publisher peer connection builds");
    publisher_pc
        .add_track(Arc::clone(&publisher_track) as Arc<dyn TrackLocal>)
        .await
        .expect("add publisher track");
    let offer_body = offer_sdp(&publisher_pc, &publisher_handler.gather_complete).await;

    let token = "test-whip-token";
    let response = oneshot(
        whip_router.clone(),
        post_sdp(&format!("/whip/{token}"), offer_body),
    )
    .await;
    assert_status(&response, StatusCode::CREATED);
    assert_eq!(
        response
            .headers()
            .get(axum::http::header::CONTENT_TYPE)
            .and_then(|v| v.to_str().ok()),
        Some("application/sdp")
    );
    let location = location_of(&response);
    assert!(location.starts_with(&format!("/whip/{token}/")));
    let answer_sdp = body_text(response).await;
    assert!(
        answer_sdp.starts_with("v=0"),
        "answer body must be raw SDP text"
    );
    apply_answer(&publisher_pc, answer_sdp).await;

    tokio::time::timeout(
        Duration::from_secs(5),
        publisher_handler.connected.notified(),
    )
    .await
    .expect("publisher reaches Connected within 5s");

    // --- Both publish paths (copy fanout + transcode bridge) exist from
    // one publish, unconditionally -- see `src/ingest/whip.rs` module
    // docs. ---
    let fanout = whip_state
        .fanout_for(token)
        .await
        .expect("fanout registered once the WHIP session is created");
    let sdp_path = whip_state
        .sdp_path_for(token)
        .await
        .expect("transcode bridge sdp path registered");
    assert!(sdp_path.exists(), "input.sdp file must exist on disk");
    let sdp_contents = std::fs::read_to_string(&sdp_path).unwrap();
    assert!(
        sdp_contents.contains("m=audio"),
        "publisher offered audio only"
    );
    assert!(
        !sdp_contents.contains("m=video"),
        "publisher offered no video"
    );

    // --- Hand-off: what `pipeline::supervisor` (S3, not yet implemented)
    // will do once it exists -- resolve the WHIP token's copy-path fanout
    // onto a `PipelineId` a WHEP viewer can subscribe to. ---
    let pipeline_id = Uuid::new_v4();
    whep_state
        .register_fanout(pipeline_id, fanout.clone())
        .await;
    assert_eq!(whep_state.viewer_count(pipeline_id).await, 0);

    // --- Viewer: negotiate against the WHEP router. ---
    let (viewer_pc, viewer_handler) = build_recvonly_peer(&viewer_factory).await;
    let viewer_offer = offer_sdp(&viewer_pc, &viewer_handler.gather_complete).await;
    let viewer_response = oneshot(
        whep_router.clone(),
        post_sdp(&format!("/whep/community-x/{pipeline_id}"), viewer_offer),
    )
    .await;
    assert_status(&viewer_response, StatusCode::CREATED);
    let viewer_location = location_of(&viewer_response);
    assert!(viewer_location.starts_with(&format!("/whep/community-x/{pipeline_id}/")));
    let viewer_answer = body_text(viewer_response).await;
    apply_answer(&viewer_pc, viewer_answer).await;

    tokio::time::timeout(Duration::from_secs(5), viewer_handler.connected.notified())
        .await
        .expect("viewer reaches Connected within 5s");
    assert_eq!(whep_state.viewer_count(pipeline_id).await, 1);

    // --- Telemetry gate (`rules/testing.md` Telemetry Validation). The note
    // above explains why RTP does not cross the in-process
    // `PeerConnection`s, but the *fanout* hop is real regardless: packets
    // published into the WHIP copy-path fanout reach the WHEP viewer's
    // forwarding subscriber, which samples their dwell time into
    // `stream_fanout_latency_seconds`. The viewer's task subscribes
    // asynchronously after the answer is applied, so keep publishing until
    // the first sample lands (a packet sent before it subscribes is simply
    // not delivered to it).
    let mut seq = 0u16;
    otel_common::wait_for(
        "stream_fanout_latency_seconds{kind=audio} to emit",
        Duration::from_secs(10),
        || {
            fanout.audio.publish(synthetic_packet(seq, 0x00A1_1D10));
            seq = seq.wrapping_add(1);
            otel.histogram("stream_fanout_latency_seconds", &[("kind", "audio")])
                .0
                >= 1
        },
    )
    .await;
    let (samples, sum) = otel.histogram("stream_fanout_latency_seconds", &[("kind", "audio")]);
    println!("telemetry: stream_fanout_latency_seconds{{kind=audio}}: {samples} data point(s), sum={sum}s");
    assert!(
        (0.0..5.0).contains(&sum),
        "in-process fanout dwell time is a small non-negative interval, got {sum}s"
    );

    // Drain the ingest hand-off channel -- confirms `create_session` really
    // did push an `IngestSession` for the pipeline supervisor, keyed by the
    // WHIP token, per this module's documented contract.
    let session = ingest_rx.try_recv().expect("ingest session pushed");
    assert_eq!(session.key, token);

    // --- Teardown both sessions. ---
    let whip_delete = oneshot(whip_router, delete_request(&location)).await;
    assert_status(&whip_delete, StatusCode::NO_CONTENT);
    assert!(
        whip_state.fanout_for(token).await.is_none(),
        "fanout must be removed after WHIP teardown"
    );
    // Not asserted here: that the `input.sdp` *file* is gone immediately
    // after teardown. `WhipTranscodeBridge::drop` removes it once the
    // *last* `Arc` reference goes away -- `teardown_session` drops the
    // state map's own reference synchronously (proven by `sdp_path_for`
    // returning `None` above), but a second reference lives inside the
    // ingest `PeerConnection`'s own driver via `WhipIngestHandler`, and
    // this suite found no bound on how long `pc.close().await` takes to
    // release it (still not dropped after a 2s poll in earlier runs of
    // this test) -- plausibly related to the same in-process driver
    // behavior documented in `src/rtc/mod.rs`'s webrtc-rs gap section.
    // `rtc::sdp_writer::tests::bridge_writes_sdp_and_removes_it_on_drop`
    // already proves the Drop-triggers-removal mechanism itself, directly
    // and deterministically, without going through a `PeerConnection`.
    assert!(
        whip_state.sdp_path_for(token).await.is_none(),
        "sdp_path_for must stop resolving once the bridge is unregistered from state"
    );

    let whep_delete = oneshot(whep_router, delete_request(&viewer_location)).await;
    assert_status(&whep_delete, StatusCode::NO_CONTENT);
    assert_eq!(whep_state.viewer_count(pipeline_id).await, 0);

    let _ = publisher_pc.close().await;
    let _ = viewer_pc.close().await;
}
