//! Real-path moderation tests: real `webrtc-rs` peers over loopback join a room
//! through the host-API + media routers, then the moderation ops (mute, kick,
//! leave, lock, token revoke) are driven and their effect is observed on the
//! live media path / session table, not on mocks.

mod rtc_common;

use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::Duration;

use axum::body::Body;
use axum::http::{header, Request, StatusCode};
use serde_json::{json, Value};
use tower::ServiceExt;
use uuid::Uuid;
use webrtc::media_stream::track_local::static_rtp::TrackLocalStaticRTP;
use webrtc::media_stream::track_local::TrackLocal;
use webrtc::peer_connection::PeerConnection;

use rtc_common::*;
use svc_streaming::rooms::token::TokenKeys;
use svc_streaming::rooms::{host_api, media, RoomService};
use svc_streaming::rtc::metrics::RtcMetrics;
use svc_streaming::rtc::pc_factory::PeerConnectionFactory;

const SECRET: &str = "0123456789abcdef0123456789abcdef";

struct Harness {
    host: axum::Router,
    media: axum::Router,
    svc: Arc<RoomService>,
    pub_factory: PeerConnectionFactory,
    view_factory: PeerConnectionFactory,
}

fn harness(base: u16, moderation: bool) -> Harness {
    let server =
        Arc::new(PeerConnectionFactory::new(loopback_rtc_config((base, base + 19))).unwrap());
    let metrics = RtcMetrics::register(&prometheus::Registry::new()).unwrap();
    let svc = Arc::new(
        RoomService::new(
            server,
            metrics,
            Some(TokenKeys::new(SECRET).unwrap()),
            "http://svc.test".into(),
        )
        .with_moderation(moderation),
    );
    Harness {
        host: host_api::router(svc.clone()),
        media: media::router(svc.clone()),
        svc,
        pub_factory: PeerConnectionFactory::new(loopback_rtc_config((base + 20, base + 29)))
            .unwrap(),
        view_factory: PeerConnectionFactory::new(loopback_rtc_config((base + 30, base + 39)))
            .unwrap(),
    }
}

async fn call(router: &axum::Router, op: &str, body: Value) -> (StatusCode, Value) {
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

fn sdp_auth(path: &str, token: &str, sdp: String) -> Request<Body> {
    Request::builder()
        .method("POST")
        .uri(path)
        .header(header::CONTENT_TYPE, "application/sdp")
        .header(header::AUTHORIZATION, format!("Bearer {token}"))
        .body(Body::from(sdp))
        .unwrap()
}

async fn new_room(h: &Harness) -> String {
    let (st, r) = call(&h.host, "room.create", json!({"community_id": "7"})).await;
    assert_eq!(st, StatusCode::OK, "{r}");
    r["room_id"].as_str().unwrap().to_string()
}

async fn endpoint(h: &Harness, op: &str, room: &str, extra: Value) -> (StatusCode, Value) {
    let mut body = json!({"community_id": "7", "room_id": room});
    for (k, v) in extra.as_object().unwrap() {
        body[k] = v.clone();
    }
    call(&h.host, op, body).await
}

struct Publisher {
    id: String,
    token_id: String,
    pc: Arc<dyn PeerConnection>,
    track: Arc<TrackLocalStaticRTP>,
    ssrc: u32,
}

async fn join_publisher(h: &Harness, room: &str) -> Publisher {
    let (st, ep) = endpoint(h, "whip.endpoint", room, json!({})).await;
    assert_eq!(st, StatusCode::OK, "{ep}");
    let token = ep["token"].as_str().unwrap().to_string();
    let (track, ssrc) = opus_publisher_track();
    let ph = TestPeerHandler::new();
    let pc = h.pub_factory.build(Arc::clone(&ph)).await.unwrap();
    pc.add_track(Arc::clone(&track) as Arc<dyn TrackLocal>)
        .await
        .unwrap();
    let offer = offer_sdp(&pc, &ph.gather_complete).await;
    let res = h
        .media
        .clone()
        .oneshot(sdp_auth(&format!("/rtc/rooms/{room}/whip"), &token, offer))
        .await
        .unwrap();
    assert_status(&res, StatusCode::CREATED);
    apply_answer(&pc, body_text(res).await).await;
    tokio::time::timeout(Duration::from_secs(5), ph.connected.notified())
        .await
        .expect("publisher connected");
    Publisher {
        id: ep["participant_id"].as_str().unwrap().to_string(),
        token_id: ep["token_id"].as_str().unwrap().to_string(),
        pc,
        track,
        ssrc,
    }
}

struct Viewer {
    id: String,
    token: String,
    token_id: String,
    handler: Arc<TestPeerHandler>,
    pc: Arc<dyn PeerConnection>,
}

async fn join_viewer(h: &Harness, room: &str, source: &str) -> Viewer {
    let (st, ep) = endpoint(h, "whep.endpoint", room, json!({"source": source})).await;
    assert_eq!(st, StatusCode::OK, "{ep}");
    let token = ep["token"].as_str().unwrap().to_string();
    let (pc, handler) = build_recvonly_peer(&h.view_factory).await;
    let offer = offer_sdp(&pc, &handler.gather_complete).await;
    let res = h
        .media
        .clone()
        .oneshot(sdp_auth(&format!("/rtc/rooms/{room}/whep"), &token, offer))
        .await
        .unwrap();
    assert_status(&res, StatusCode::CREATED);
    apply_answer(&pc, body_text(res).await).await;
    tokio::time::timeout(Duration::from_secs(5), handler.connected.notified())
        .await
        .expect("viewer connected");
    Viewer {
        id: ep["participant_id"].as_str().unwrap().to_string(),
        token,
        token_id: ep["token_id"].as_str().unwrap().to_string(),
        handler,
        pc,
    }
}

/// Sends `n` packets spaced out; returns the next sequence number.
async fn pump(p: &Publisher, from: u16, n: u16) -> u16 {
    for i in 0..n {
        p.track
            .write_rtp(synthetic_packet(from.wrapping_add(i), p.ssrc))
            .await
            .unwrap();
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    from.wrapping_add(n)
}

fn mod_body(room: &str, extra: Value) -> Value {
    let mut b = json!({"community_id": "7", "room_id": room,
        "actor_id": Uuid::new_v4(), "actor_role": "moderator"});
    for (k, v) in extra.as_object().unwrap() {
        b[k] = v.clone();
    }
    b
}

async fn counts(h: &Harness, room: &str) -> Value {
    call(
        &h.host,
        "room.get",
        json!({"community_id": "7", "room_id": room}),
    )
    .await
    .1
}

#[tokio::test]
async fn moderation_is_off_by_default_and_fails_loud() {
    let h = harness(42100, false);
    let room = new_room(&h).await;
    for op in ["room.mute_all", "room.lock", "room.unlock"] {
        let (st, b) = call(&h.host, op, mod_body(&room, json!({}))).await;
        assert_eq!(st, StatusCode::NOT_IMPLEMENTED, "{op}: {b}");
        assert_eq!(b["error"], "not_implemented");
    }
    // Read-only listing stays available.
    let (st, b) = call(&h.host, "room.list", json!({"community_id": "7"})).await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(b["rooms"].as_array().unwrap().len(), 1);
}

#[tokio::test]
async fn muted_publisher_is_not_forwarded_until_unmuted() {
    let h = harness(42200, true);
    let room = new_room(&h).await;
    let p = join_publisher(&h, &room).await;
    let v = join_viewer(&h, &room, &p.id).await;

    let mut seq = pump(&p, 0, 40).await;
    let before = v.handler.packets_received.load(Ordering::SeqCst);
    assert!(before >= 5, "baseline media should flow, got {before}");

    // Non-moderators are refused.
    let (st, _) = call(
        &h.host,
        "room.mute",
        mod_body(
            &room,
            json!({"participant_id": p.id, "actor_role": "member"}),
        ),
    )
    .await;
    assert_eq!(st, StatusCode::FORBIDDEN);

    let (st, b) = call(
        &h.host,
        "room.mute",
        mod_body(&room, json!({"participant_id": p.id})),
    )
    .await;
    assert_eq!(st, StatusCode::OK, "{b}");
    let (_, parts) = call(
        &h.host,
        "room.participants",
        json!({"community_id": "7", "room_id": room}),
    )
    .await;
    let me = parts["participants"]
        .as_array()
        .unwrap()
        .iter()
        .find(|e| e["participant_id"] == p.id)
        .unwrap();
    assert_eq!(me["muted"], true);

    tokio::time::sleep(Duration::from_millis(300)).await; // drain in-flight
    let settled = v.handler.packets_received.load(Ordering::SeqCst);
    seq = pump(&p, seq, 40).await;
    tokio::time::sleep(Duration::from_millis(300)).await;
    let during = v.handler.packets_received.load(Ordering::SeqCst);
    assert_eq!(
        during, settled,
        "muted publisher's packets must not reach the viewer"
    );

    call(
        &h.host,
        "room.unmute",
        mod_body(&room, json!({"participant_id": p.id})),
    )
    .await;
    pump(&p, seq, 40).await;
    tokio::time::sleep(Duration::from_millis(300)).await;
    let after = v.handler.packets_received.load(Ordering::SeqCst);
    assert!(
        after > during + 3,
        "unmute resumes forwarding ({during} -> {after})"
    );

    // mute_all excludes the actor and mutes every publisher otherwise.
    let (st, b) = call(&h.host, "room.mute_all", mod_body(&room, json!({}))).await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(b["muted"], 1);
    let _ = (p.pc.close().await, v.pc.close().await);
}

#[tokio::test]
async fn kick_closes_live_peers_and_blocks_rejoin() {
    let h = harness(42300, true);
    let room = new_room(&h).await;
    let p = join_publisher(&h, &room).await;
    let v = join_viewer(&h, &room, &p.id).await;
    assert_eq!(counts(&h, &room).await["subscriber_count"], 1);

    // Cannot kick self; members cannot kick.
    let (st, _) = call(
        &h.host,
        "room.kick",
        mod_body(&room, json!({"participant_id": v.id, "actor_id": v.id})),
    )
    .await;
    assert_eq!(st, StatusCode::BAD_REQUEST);
    let (st, _) = call(
        &h.host,
        "room.kick",
        mod_body(
            &room,
            json!({"participant_id": v.id, "actor_role": "guest"}),
        ),
    )
    .await;
    assert_eq!(st, StatusCode::FORBIDDEN);

    let (st, b) = call(
        &h.host,
        "room.kick",
        mod_body(&room, json!({"participant_id": v.id})),
    )
    .await;
    assert_eq!(st, StatusCode::OK, "{b}");
    assert_eq!(b["sessions_closed"], 1);
    assert_eq!(counts(&h, &room).await["subscriber_count"], 0);

    // The kicked participant's token no longer authorizes a rejoin.
    let (pc, hh) = build_recvonly_peer(&h.view_factory).await;
    let offer = offer_sdp(&pc, &hh.gather_complete).await;
    let res = h
        .media
        .clone()
        .oneshot(sdp_auth(
            &format!("/rtc/rooms/{room}/whep"),
            &v.token,
            offer,
        ))
        .await
        .unwrap();
    assert_eq!(res.status(), StatusCode::UNAUTHORIZED);

    // Kicking the publisher drops the publisher session too.
    let (_, b) = call(
        &h.host,
        "room.kick",
        mod_body(&room, json!({"participant_id": p.id})),
    )
    .await;
    assert_eq!(b["sessions_closed"], 1);
    assert_eq!(counts(&h, &room).await["publisher_ids"], json!([]));
    let _ = (pc.close().await, p.pc.close().await, v.pc.close().await);
}

#[tokio::test]
async fn leave_closes_own_sessions_without_blocklisting() {
    let h = harness(42400, true);
    let room = new_room(&h).await;
    let p = join_publisher(&h, &room).await;
    let v = join_viewer(&h, &room, &p.id).await;
    let (st, b) = call(
        &h.host,
        "room.leave",
        mod_body(
            &room,
            json!({"participant_id": v.id, "actor_id": v.id, "actor_role": "guest"}),
        ),
    )
    .await;
    assert_eq!(st, StatusCode::OK, "{b}");
    assert_eq!(b["sessions_closed"], 1);
    assert_eq!(counts(&h, &room).await["subscriber_count"], 0);
    // Not blocklisted: the same token may rejoin.
    let (pc, hh) = build_recvonly_peer(&h.view_factory).await;
    let offer = offer_sdp(&pc, &hh.gather_complete).await;
    let res = h
        .media
        .clone()
        .oneshot(sdp_auth(
            &format!("/rtc/rooms/{room}/whep"),
            &v.token,
            offer,
        ))
        .await
        .unwrap();
    assert_status(&res, StatusCode::CREATED);
    // Leaving someone else needs a moderator.
    let (st, _) = call(
        &h.host,
        "room.leave",
        mod_body(
            &room,
            json!({"participant_id": p.id, "actor_role": "member"}),
        ),
    )
    .await;
    assert_eq!(st, StatusCode::FORBIDDEN);
    let _ = (pc.close().await, p.pc.close().await, v.pc.close().await);
}

#[tokio::test]
async fn locked_room_refuses_tokens_and_joins_until_unlocked() {
    let h = harness(42500, true);
    let room = new_room(&h).await;
    let p = join_publisher(&h, &room).await;
    let (_, pre) = endpoint(&h, "whep.endpoint", &room, json!({"source": p.id})).await;
    let pre_token = pre["token"].as_str().unwrap().to_string();
    let (st, _) = call(&h.host, "room.lock", mod_body(&room, json!({}))).await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(counts(&h, &room).await["locked"], true);

    for op in ["token.issue", "whip.endpoint", "whep.endpoint"] {
        let extra = if op == "token.issue" {
            json!({"publish": true, "subscribe": false})
        } else {
            json!({})
        };
        let (st, b) = endpoint(&h, op, &room, extra).await;
        assert_eq!(st, StatusCode::FORBIDDEN, "{op}: {b}");
    }
    // A token issued before the lock cannot join either.
    let (pc, hh) = build_recvonly_peer(&h.view_factory).await;
    let offer = offer_sdp(&pc, &hh.gather_complete).await;
    let res = h
        .media
        .clone()
        .oneshot(sdp_auth(
            &format!("/rtc/rooms/{room}/whep"),
            &pre_token,
            offer,
        ))
        .await
        .unwrap();
    assert_eq!(
        res.status(),
        StatusCode::FORBIDDEN,
        "join with a pre-lock token is refused"
    );

    let (st, _) = call(&h.host, "room.unlock", mod_body(&room, json!({}))).await;
    assert_eq!(st, StatusCode::OK);
    let (st, _) = endpoint(&h, "whep.endpoint", &room, json!({})).await;
    assert_eq!(st, StatusCode::OK, "unlock re-enables issuance");
    let _ = (pc.close().await, p.pc.close().await);
}

#[tokio::test]
async fn token_revoke_tears_down_the_live_session() {
    let h = harness(42600, true);
    let room = new_room(&h).await;
    let p = join_publisher(&h, &room).await;
    let v = join_viewer(&h, &room, &p.id).await;
    assert_eq!(counts(&h, &room).await["subscriber_count"], 1);
    let (st, _) = call(
        &h.host,
        "token.revoke",
        json!({"community_id": "7", "room_id": room, "token_id": v.token_id}),
    )
    .await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(counts(&h, &room).await["subscriber_count"], 0);
    // Revoking the publisher's token drops its WHIP session.
    call(
        &h.host,
        "token.revoke",
        json!({"community_id": "7", "room_id": room, "token_id": p.token_id}),
    )
    .await;
    assert_eq!(counts(&h, &room).await["publisher_ids"], json!([]));
    let _ = (p.pc.close().await, v.pc.close().await);
    let _ = &h.svc;
}

#[tokio::test]
async fn list_and_participants_are_community_scoped_and_ids_only() {
    let h = harness(42700, true);
    let room = new_room(&h).await;
    let p = join_publisher(&h, &room).await;
    let v = join_viewer(&h, &room, &p.id).await;
    let (_, mine) = call(&h.host, "room.list", json!({"community_id": "7"})).await;
    assert_eq!(mine["rooms"][0]["room_id"], room);
    let (_, other) = call(&h.host, "room.list", json!({"community_id": "8"})).await;
    assert_eq!(other["rooms"], json!([]));
    let (st, _) = call(
        &h.host,
        "room.participants",
        json!({"community_id": "8", "room_id": room}),
    )
    .await;
    assert_eq!(st, StatusCode::NOT_FOUND);
    let (_, parts) = call(
        &h.host,
        "room.participants",
        json!({"community_id": "7", "room_id": room}),
    )
    .await;
    assert_eq!(parts["count"], 2);
    for e in parts["participants"].as_array().unwrap() {
        let keys: Vec<&String> = e.as_object().unwrap().keys().collect();
        assert_eq!(keys, ["muted", "participant_id", "publisher", "subscriber"]);
        Uuid::parse_str(e["participant_id"].as_str().unwrap()).unwrap();
    }
    let _ = (p.pc.close().await, v.pc.close().await);
}
