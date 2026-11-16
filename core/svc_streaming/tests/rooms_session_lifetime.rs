//! Media session lifetime is decoupled from the join-token TTL: the token gates ADMISSION
//! only. An established session survives token expiry, its holder can still end it with the
//! (expired) token, and it ends on leave/kick/room close or the publisher idle timeout.

mod rtc_common;

use std::sync::Arc;

use axum::http::StatusCode;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine as _;
use serde_json::{json, Value};
use tower::ServiceExt;

use rtc_common::*;
use svc_streaming::rooms::token::RoomClaims;
use svc_streaming::rooms::{host_api, media, ActorRole, RoomService};
use svc_streaming::rtc::metrics::RtcMetrics;
use svc_streaming::rtc::pc_factory::PeerConnectionFactory;

fn service(range: (u16, u16), idle_secs: u32) -> Arc<RoomService> {
    let factory = Arc::new(PeerConnectionFactory::new(loopback_rtc_config(range)).unwrap());
    let metrics = RtcMetrics::register(&prometheus::Registry::new()).unwrap();
    Arc::new(
        RoomService::new(
            factory,
            metrics,
            Some(rtc_common::test_token_keys()),
            "http://svc.test".into(),
        )
        .with_moderation(true)
        .with_session_idle_secs(idle_secs),
    )
}

async fn host_call(router: &axum::Router, op: &str, body: Value) -> (StatusCode, Value) {
    let res = router
        .clone()
        .oneshot(
            axum::http::Request::builder()
                .method("POST")
                .uri(format!("/host/v1/webrtc/{op}"))
                .header("content-type", "application/json")
                .body(axum::body::Body::from(body.to_string()))
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

/// Claims of a (valid) token, read without verification (test-only).
fn claims_of(token: &str) -> RoomClaims {
    let payload = token.split('.').nth(1).unwrap();
    serde_json::from_slice(&URL_SAFE_NO_PAD.decode(payload).unwrap()).unwrap()
}

/// The same claims re-signed with the room key, but already expired.
fn expired_twin(token: &str) -> String {
    let mut c = claims_of(token);
    let now = chrono::Utc::now().timestamp();
    c.iat = now - 600;
    c.exp = now - 300;
    rtc_common::test_token_keys().sign(&c).unwrap()
}

struct Live {
    svc: Arc<RoomService>,
    host: axum::Router,
    media: axum::Router,
    room_id: String,
    token: String,
    session_path: String,
    _pc: Arc<dyn webrtc::peer_connection::PeerConnection>,
}

async fn start(range: (u16, u16), idle_secs: u32) -> Live {
    let svc = service(range, idle_secs);
    let host = host_api::router(svc.clone());
    let media = media::router(svc.clone());
    let client =
        PeerConnectionFactory::new(loopback_rtc_config((range.1 + 1, range.1 + 10))).unwrap();
    let (_, room) = host_call(&host, "room.create", json!({"community_id": "7"})).await;
    let room_id = room["room_id"].as_str().unwrap().to_string();
    let (_, ep) = host_call(
        &host,
        "whip.endpoint",
        json!({"community_id":"7","room_id":room_id,"ttl_secs":30}),
    )
    .await;
    let token = ep["token"].as_str().unwrap().to_string();
    let (track, _) = opus_publisher_track();
    let ph = TestPeerHandler::new();
    let pc = client.build(Arc::clone(&ph)).await.unwrap();
    pc.add_track(track as Arc<dyn webrtc::media_stream::track_local::TrackLocal>)
        .await
        .unwrap();
    let offer = offer_sdp(&pc, &ph.gather_complete).await;
    let res = media
        .clone()
        .oneshot(post_sdp_auth(
            &format!("/rtc/rooms/{room_id}/whip"),
            &token,
            offer,
        ))
        .await
        .unwrap();
    assert_status(&res, StatusCode::CREATED);
    let session_path = location_of(&res);
    apply_answer(&pc, body_text(res).await).await;
    Live {
        svc,
        host,
        media,
        room_id,
        token,
        session_path,
        _pc: pc,
    }
}

async fn publisher_count(l: &Live) -> usize {
    let (_, info) = host_call(
        &l.host,
        "room.get",
        json!({"community_id":"7","room_id":l.room_id}),
    )
    .await;
    info["publisher_ids"].as_array().unwrap().len()
}

#[tokio::test]
async fn session_outlives_token_and_holder_can_end_it_with_the_expired_token() {
    let l = start((43100, 43119), 300).await;
    let at_token_expiry = chrono::Utc::now().timestamp() + 31;
    // Past the 30 s join-token expiry the session is still there (and sweep keeps it).
    assert_eq!(l.svc.sweep_at(at_token_expiry).await, 0);
    assert_eq!(publisher_count(&l).await, 1);

    // The expired token no longer ADMITS (new session refused)...
    let expired = expired_twin(&l.token);
    let res = l
        .media
        .clone()
        .oneshot(post_sdp_auth(
            &format!("/rtc/rooms/{}/whip", l.room_id),
            &expired,
            "v=0".to_string(),
        ))
        .await
        .unwrap();
    assert_status(&res, StatusCode::UNAUTHORIZED);

    // ...but it can still END the established session it opened.
    let res = l
        .media
        .clone()
        .oneshot(delete_auth(&l.session_path, &expired))
        .await
        .unwrap();
    assert_status(&res, StatusCode::NO_CONTENT);
    assert_eq!(publisher_count(&l).await, 0);
}

#[tokio::test]
async fn expired_token_cannot_end_a_session_after_kick_or_with_a_forged_signature() {
    let l = start((43130, 43149), 300).await;
    let claims = claims_of(&l.token);
    let participant: uuid::Uuid = claims.sub.parse().unwrap();
    let expired = expired_twin(&l.token);

    // Forged: tampered signature never verifies, expired or not.
    let mut forged = expired.clone();
    forged.pop();
    forged.push(if expired.ends_with('A') { 'B' } else { 'A' });
    let res = l
        .media
        .clone()
        .oneshot(delete_auth(&l.session_path, &forged))
        .await
        .unwrap();
    assert_status(&res, StatusCode::UNAUTHORIZED);
    assert_eq!(publisher_count(&l).await, 1);

    // Kicked: the blocklist still applies to an expired token; kick itself ends the session.
    let room: uuid::Uuid = l.room_id.parse().unwrap();
    let closed = l
        .svc
        .kick(
            "7",
            room,
            participant,
            uuid::Uuid::new_v4(),
            ActorRole::Admin,
        )
        .await
        .unwrap();
    assert_eq!(closed, 1, "kick ends the established session");
    let res = l
        .media
        .clone()
        .oneshot(delete_auth(&l.session_path, &expired))
        .await
        .unwrap();
    assert_status(&res, StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn leave_and_room_close_end_sessions_not_token_expiry() {
    let l = start((43160, 43179), 300).await;
    let claims = claims_of(&l.token);
    let participant: uuid::Uuid = claims.sub.parse().unwrap();
    let room: uuid::Uuid = l.room_id.parse().unwrap();
    // Leave (self) ends it.
    let n = l
        .svc
        .leave("7", room, participant, participant, ActorRole::Member)
        .await
        .unwrap();
    assert_eq!(n, 1);
    assert_eq!(publisher_count(&l).await, 0);

    let l2 = start((43190, 43209), 300).await;
    let room2: uuid::Uuid = l2.room_id.parse().unwrap();
    l2.svc.close("7", room2).await.unwrap();
    let (st, _) = host_call(
        &l2.host,
        "room.get",
        json!({"community_id":"7","room_id":l2.room_id}),
    )
    .await;
    assert_eq!(st, StatusCode::NOT_FOUND, "closing the room ends sessions");
}

#[tokio::test]
async fn idle_publisher_is_reaped_after_the_idle_timeout_only() {
    let l = start((43220, 43239), 300).await;
    let now = chrono::Utc::now().timestamp();
    // No RTP ever arrives: before the idle timeout nothing is reaped...
    assert_eq!(l.svc.sweep_at(now + 100).await, 0);
    assert_eq!(publisher_count(&l).await, 1);
    // ...at the timeout the silent publisher is.
    assert_eq!(l.svc.sweep_at(now + 301).await, 1);
    assert_eq!(publisher_count(&l).await, 0);
}

#[tokio::test]
async fn idle_timeout_zero_disables_idle_reaping() {
    let l = start((43250, 43269), 0).await;
    let now = chrono::Utc::now().timestamp();
    assert_eq!(l.svc.sweep_at(now + 10_000).await, 0);
    assert_eq!(publisher_count(&l).await, 1);
}
