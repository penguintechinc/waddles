//! `src/egress/whep.rs`: `WHEP_MAX_VIEWERS` enforcement and the "no
//! registered source" 404 -- both checked before a `PeerConnection` is even
//! built, so `create_session` never does the expensive negotiation work for
//! a request it's going to reject.

mod rtc_common;

use std::sync::Arc;

use axum::http::StatusCode;
use uuid::Uuid;

use rtc_common::*;
use svc_streaming::egress::whep;
use svc_streaming::rtc::fanout::MediaFanouts;
use svc_streaming::rtc::metrics::RtcMetrics;
use svc_streaming::rtc::pc_factory::PeerConnectionFactory;

#[tokio::test]
async fn no_registered_source_is_404() {
    let factory =
        Arc::new(PeerConnectionFactory::new(loopback_rtc_config((41800, 41805))).unwrap());
    let registry = prometheus::Registry::new();
    let metrics = RtcMetrics::register(&registry).unwrap();
    let state = Arc::new(whep_with_auth(factory, metrics, 50));
    let router = whep::router(state);
    let pipeline = Uuid::new_v4();
    let token = viewer_token("community-x", pipeline, 60, "pipeline", Uuid::new_v4());

    let response = oneshot(
        router,
        post_sdp_auth(
            &format!("/whep/community-x/{pipeline}"),
            &token,
            "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n",
        ),
    )
    .await;

    assert_status(&response, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn viewer_over_the_limit_is_rejected_with_403() {
    let whep_factory =
        Arc::new(PeerConnectionFactory::new(loopback_rtc_config((41810, 41819))).unwrap());
    let viewer_factory = PeerConnectionFactory::new(loopback_rtc_config((41820, 41829))).unwrap();
    let registry = prometheus::Registry::new();
    let metrics = RtcMetrics::register(&registry).unwrap();

    // `max_viewers = 1` -- the second concurrent viewer must be rejected.
    let state = Arc::new(whep_with_auth(whep_factory, metrics, 1));
    let pipeline_id = Uuid::new_v4();
    state
        .register_fanout(pipeline_id, "community-x", MediaFanouts::new())
        .await;
    let t1 = viewer_token("community-x", pipeline_id, 60, "pipeline", Uuid::new_v4());
    let t2 = viewer_token("community-x", pipeline_id, 60, "pipeline", Uuid::new_v4());
    let router = whep::router(Arc::clone(&state));

    let (first_pc, first_handler) = build_recvonly_peer(&viewer_factory).await;
    let first_offer = offer_sdp(&first_pc, &first_handler.gather_complete).await;
    let first_response = oneshot(
        router.clone(),
        post_sdp_auth(
            &format!("/whep/community-x/{pipeline_id}"),
            &t1,
            first_offer,
        ),
    )
    .await;
    assert_status(&first_response, StatusCode::CREATED);

    // Second viewer, same pipeline, while the first is still active.
    let (second_pc, second_handler) = build_recvonly_peer(&viewer_factory).await;
    let second_offer = offer_sdp(&second_pc, &second_handler.gather_complete).await;
    let second_response = oneshot(
        router,
        post_sdp_auth(
            &format!("/whep/community-x/{pipeline_id}"),
            &t2,
            second_offer,
        ),
    )
    .await;
    assert_status(&second_response, StatusCode::FORBIDDEN);

    assert_eq!(state.viewer_count(pipeline_id).await, 1);

    let _ = first_pc.close().await;
    let _ = second_pc.close().await;
}
