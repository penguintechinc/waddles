//! Real-path rooms test: a room is created and joined entirely through the
//! host-API router (token issue) and the room media router (WHIP-style
//! publish, WHEP-style subscribe) using real `webrtc-rs` peers over loopback.
//! Asserts media RECEIVED by the subscriber, plus the auth/scoping refusals.

mod rtc_common;

use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::Duration;

use axum::body::Body;
use axum::http::{header, Request, StatusCode};
use serde_json::{json, Value};
use tower::ServiceExt;
use uuid::Uuid;
use webrtc::media_stream::track_local::TrackLocal;

use rtc_common::*;
use svc_streaming::rooms::token::TokenKeys;
use svc_streaming::rooms::{host_api, media, RoomService};
use svc_streaming::rtc::metrics::RtcMetrics;
use svc_streaming::rtc::pc_factory::PeerConnectionFactory;

const SECRET: &str = "0123456789abcdef0123456789abcdef";

async fn host_call(router: &axum::Router, op: &str, body: Value) -> (StatusCode, Value) {
    let res = router
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("/host/v1/webrtc/{op}"))
                .header(header::CONTENT_TYPE, "application/json")
                .body(Body::from(body.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = res.status();
    let bytes = axum::body::to_bytes(res.into_body(), usize::MAX)
        .await
        .unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

fn sdp_post(path: &str, token: &str, sdp: String) -> Request<Body> {
    Request::builder()
        .method("POST")
        .uri(path)
        .header(header::CONTENT_TYPE, "application/sdp")
        .header(header::AUTHORIZATION, format!("Bearer {token}"))
        .body(Body::from(sdp))
        .unwrap()
}

#[tokio::test]
async fn room_publish_reaches_room_subscriber_with_scoped_tokens() {
    let server_factory =
        Arc::new(PeerConnectionFactory::new(loopback_rtc_config((41900, 41919))).unwrap());
    let publisher_factory =
        PeerConnectionFactory::new(loopback_rtc_config((41920, 41929))).unwrap();
    let viewer_factory = PeerConnectionFactory::new(loopback_rtc_config((41930, 41939))).unwrap();
    let metrics = RtcMetrics::register(&prometheus::Registry::new()).unwrap();
    let svc = Arc::new(RoomService::new(
        server_factory,
        metrics,
        Some(TokenKeys::new(SECRET).unwrap()),
        "http://svc.test".into(),
    ));
    let host = host_api::router(svc.clone());
    let media = media::router(svc.clone());

    // Create the room (community 7) and a second room to prove token scoping.
    let (st, room) = host_call(&host, "room.create", json!({"community_id": "7"})).await;
    assert_eq!(st, StatusCode::OK, "{room}");
    let room_id = room["room_id"].as_str().unwrap().to_string();
    let (_, other) = host_call(&host, "room.create", json!({"community_id": "7"})).await;
    let other_id = other["room_id"].as_str().unwrap().to_string();

    // Cross-community lookup is indistinguishable from not-found.
    let (st, _) = host_call(
        &host,
        "room.get",
        json!({"community_id": "8", "room_id": room_id}),
    )
    .await;
    assert_eq!(st, StatusCode::NOT_FOUND);

    let (st, pubep) = host_call(
        &host,
        "whip.endpoint",
        json!({"community_id": "7", "room_id": room_id}),
    )
    .await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(
        pubep["url"],
        format!("http://svc.test/rtc/rooms/{room_id}/whip")
    );
    let pub_token = pubep["token"].as_str().unwrap().to_string();
    let publisher_id = pubep["participant_id"].as_str().unwrap().to_string();

    let (_, subep) = host_call(
        &host,
        "whep.endpoint",
        json!({"community_id": "7", "room_id": room_id, "source": publisher_id}),
    )
    .await;
    let sub_token = subep["token"].as_str().unwrap().to_string();

    // Refusals: no token, wrong grant, wrong room.
    let (pc0, h0) = build_recvonly_peer(&viewer_factory).await;
    let probe = offer_sdp(&pc0, &h0.gather_complete).await;
    let res = media
        .clone()
        .oneshot(post_sdp(
            &format!("/rtc/rooms/{room_id}/whep"),
            probe.clone(),
        ))
        .await
        .unwrap();
    assert_eq!(res.status(), StatusCode::UNAUTHORIZED);
    let res = media
        .clone()
        .oneshot(sdp_post(
            &format!("/rtc/rooms/{room_id}/whep"),
            &pub_token,
            probe.clone(),
        ))
        .await
        .unwrap();
    assert_eq!(
        res.status(),
        StatusCode::FORBIDDEN,
        "publish token must not subscribe"
    );
    let res = media
        .clone()
        .oneshot(sdp_post(
            &format!("/rtc/rooms/{other_id}/whep"),
            &sub_token,
            probe,
        ))
        .await
        .unwrap();
    assert_eq!(
        res.status(),
        StatusCode::UNAUTHORIZED,
        "token is room-scoped"
    );
    let _ = pc0.close().await;

    // Publisher joins.
    let (track, ssrc) = opus_publisher_track();
    let ph = TestPeerHandler::new();
    let ppc = publisher_factory.build(Arc::clone(&ph)).await.unwrap();
    ppc.add_track(Arc::clone(&track) as Arc<dyn TrackLocal>)
        .await
        .unwrap();
    let offer = offer_sdp(&ppc, &ph.gather_complete).await;
    let res = media
        .clone()
        .oneshot(sdp_post(
            &format!("/rtc/rooms/{room_id}/whip"),
            &pub_token,
            offer,
        ))
        .await
        .unwrap();
    assert_status(&res, StatusCode::CREATED);
    apply_answer(&ppc, body_text(res).await).await;
    tokio::time::timeout(Duration::from_secs(5), ph.connected.notified())
        .await
        .expect("publisher connected");

    // Subscriber joins, restricted to that publisher.
    let (vpc, vh) = build_recvonly_peer(&viewer_factory).await;
    let voffer = offer_sdp(&vpc, &vh.gather_complete).await;
    let res = media
        .clone()
        .oneshot(sdp_post(
            &format!("/rtc/rooms/{room_id}/whep"),
            &sub_token,
            voffer,
        ))
        .await
        .unwrap();
    assert_status(&res, StatusCode::CREATED);
    let session_path = location_of(&res);
    apply_answer(&vpc, body_text(res).await).await;
    tokio::time::timeout(Duration::from_secs(5), vh.connected.notified())
        .await
        .expect("subscriber connected");

    let (_, info) = host_call(
        &host,
        "room.get",
        json!({"community_id": "7", "room_id": room_id}),
    )
    .await;
    assert_eq!(info["subscriber_count"], 1);
    assert_eq!(info["publisher_ids"][0], publisher_id);

    let mut seq = 0u16;
    let mut received = 0;
    for _ in 0..200 {
        track.write_rtp(synthetic_packet(seq, ssrc)).await.unwrap();
        seq = seq.wrapping_add(1);
        tokio::time::sleep(Duration::from_millis(20)).await;
        received = vh.packets_received.load(Ordering::SeqCst);
        if received >= 5 {
            break;
        }
    }
    println!("room subscriber received {received} RTP packets");
    assert!(received >= 5, "subscriber received only {received}");

    // Revocation stops new joins; a subscriber can end its own session.
    let tid: Uuid = subep["token_id"].as_str().unwrap().parse().unwrap();
    let (st, _) = host_call(
        &host,
        "token.revoke",
        json!({"community_id": "7", "room_id": room_id, "token_id": tid}),
    )
    .await;
    assert_eq!(st, StatusCode::OK);
    let res = media
        .clone()
        .oneshot(
            Request::builder()
                .method("DELETE")
                .uri(&session_path)
                .header(header::AUTHORIZATION, format!("Bearer {sub_token}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(
        res.status(),
        StatusCode::UNAUTHORIZED,
        "revoked token refused"
    );

    // Closing the room removes it and drops its peers.
    let (st, _) = host_call(
        &host,
        "room.close",
        json!({"community_id": "7", "room_id": room_id}),
    )
    .await;
    assert_eq!(st, StatusCode::OK);
    let (st, _) = host_call(
        &host,
        "room.get",
        json!({"community_id": "7", "room_id": room_id}),
    )
    .await;
    assert_eq!(st, StatusCode::NOT_FOUND);
    let _ = ppc.close().await;
    let _ = vpc.close().await;
}

#[tokio::test]
async fn unsupported_paths_fail_loud() {
    let factory =
        Arc::new(PeerConnectionFactory::new(loopback_rtc_config((41940, 41949))).unwrap());
    let metrics = RtcMetrics::register(&prometheus::Registry::new()).unwrap();
    let svc = Arc::new(RoomService::new(
        factory,
        metrics,
        None,
        "http://svc.test".into(),
    ));
    let host = host_api::router(svc);
    let (_, room) = host_call(&host, "room.create", json!({"community_id": "7"})).await;
    let rid = room["room_id"].clone();
    let (st, b) = host_call(
        &host,
        "token.issue",
        json!({"community_id":"7","room_id":rid,"publish":true,"subscribe":false}),
    )
    .await;
    assert_eq!(st, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(b["error"], "unavailable");
    let (st, b) = host_call(
        &host,
        "track.route",
        json!({"community_id":"7","room_id":rid,"mode":"relay"}),
    )
    .await;
    assert_eq!(st, StatusCode::NOT_IMPLEMENTED);
    assert_eq!(b["error"], "not_implemented");
    let (st, _) = host_call(
        &host,
        "recording.start",
        json!({"community_id":"7","room_id":rid}),
    )
    .await;
    assert_eq!(st, StatusCode::NOT_IMPLEMENTED);
    let (st, b) = host_call(&host, "room.create", json!({"community_id":"7","bogus":1})).await;
    assert_eq!(st, StatusCode::BAD_REQUEST);
    assert_eq!(b["error"], "invalid_args");
    let (st, _) = host_call(&host, "nope", json!({})).await;
    assert_eq!(st, StatusCode::BAD_REQUEST);
}
