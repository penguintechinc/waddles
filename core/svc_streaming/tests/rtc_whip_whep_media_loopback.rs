//! Real-path WHIP publish -> WHEP subscribe loopback: a `webrtc-rs` publisher
//! writes RTP through a negotiated `PeerConnection` into `ingest::whip`, the
//! server's copy-path fanout carries it to `egress::whep`, and a second
//! `webrtc-rs` viewer peer must RECEIVE those packets over a live
//! `PeerConnection`. Asserts a positive received-packet count -- no faking.

mod rtc_common;

use std::sync::atomic::Ordering;
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
async fn whip_publish_media_reaches_whep_viewer_over_real_peer_connections() {
    let whip_factory =
        Arc::new(PeerConnectionFactory::new(loopback_rtc_config((41700, 41709))).unwrap());
    let publisher_factory =
        PeerConnectionFactory::new(loopback_rtc_config((41710, 41719))).unwrap();
    let whep_factory =
        Arc::new(PeerConnectionFactory::new(loopback_rtc_config((41720, 41729))).unwrap());
    let viewer_factory = PeerConnectionFactory::new(loopback_rtc_config((41730, 41739))).unwrap();

    let whip_metrics = RtcMetrics::register(&prometheus::Registry::new()).unwrap();
    let whep_metrics = RtcMetrics::register(&prometheus::Registry::new()).unwrap();
    let (ingest_tx, _ingest_rx) = mpsc::channel(4);
    let dir = std::env::temp_dir().join(format!("svc-streaming-loopback-{}", Uuid::new_v4()));

    let whip_state = Arc::new(WhipState::new(
        whip_factory,
        Arc::new(AllowAllAuthorizer),
        ingest_tx,
        dir,
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

    // Publisher.
    let (track, ssrc) = opus_publisher_track();
    let pub_handler = TestPeerHandler::new();
    let publisher_pc = publisher_factory
        .build(Arc::clone(&pub_handler))
        .await
        .unwrap();
    publisher_pc
        .add_track(Arc::clone(&track) as Arc<dyn TrackLocal>)
        .await
        .unwrap();
    let offer = offer_sdp(&publisher_pc, &pub_handler.gather_complete).await;
    let resp = oneshot(whip_router, post_sdp("/whip/loop-token", offer)).await;
    assert_status(&resp, StatusCode::CREATED);
    apply_answer(&publisher_pc, body_text(resp).await).await;
    tokio::time::timeout(Duration::from_secs(5), pub_handler.connected.notified())
        .await
        .expect("publisher connected");

    let pipeline_id = Uuid::new_v4();
    whep_state
        .register_fanout(
            pipeline_id,
            whip_state.fanout_for("loop-token").await.unwrap(),
        )
        .await;

    // Viewer.
    let (viewer_pc, viewer_handler) = build_recvonly_peer(&viewer_factory).await;
    let voffer = offer_sdp(&viewer_pc, &viewer_handler.gather_complete).await;
    let vresp = oneshot(
        whep_router,
        post_sdp(&format!("/whep/c/{pipeline_id}"), voffer),
    )
    .await;
    assert_status(&vresp, StatusCode::CREATED);
    apply_answer(&viewer_pc, body_text(vresp).await).await;
    tokio::time::timeout(Duration::from_secs(5), viewer_handler.connected.notified())
        .await
        .expect("viewer connected");

    // Publish until the viewer's own on_track counter moves.
    let mut seq = 0u16;
    let mut received = 0usize;
    for _ in 0..200 {
        track
            .write_rtp(synthetic_packet(seq, ssrc))
            .await
            .expect("publisher write_rtp");
        seq = seq.wrapping_add(1);
        tokio::time::sleep(Duration::from_millis(20)).await;
        received = viewer_handler.packets_received.load(Ordering::SeqCst);
        if received >= 5 {
            break;
        }
    }
    println!("viewer received {received} RTP packets over the live PeerConnection");
    assert!(received >= 5, "viewer received only {received} packets");

    let _ = publisher_pc.close().await;
    let _ = viewer_pc.close().await;
}
