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
use svc_streaming::rooms::{host_api, media, RoomService};
use svc_streaming::rtc::metrics::RtcMetrics;
use svc_streaming::rtc::pc_factory::PeerConnectionFactory;

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
            Some(rtc_common::test_token_keys()),
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

/// `room.create` carries the room facts through the host API and `room.get` reads them back.
#[tokio::test]
async fn host_room_create_applies_room_facts() {
    let h = harness(42900, false);
    let (st, r) = call(
        &h.host,
        "room.create",
        json!({"community_id": "7", "kind": "stage", "allow_guests": true, "closed": false,
               "actor_scopes": ["rooms:update", "rooms:guests"]}),
    )
    .await;
    assert_eq!(st, StatusCode::OK, "{r}");
    assert_eq!(r["kind"], "stage", "{r}");
    assert_eq!(r["allow_guests"], true, "{r}");
    let (st, r) = call(
        &h.host,
        "room.create",
        json!({"community_id": "7", "kind": "bogus"}),
    )
    .await;
    assert_ne!(
        st,
        StatusCode::OK,
        "an unknown kind is refused, never coerced: {r}"
    );
    let (st, r) = call(
        &h.host,
        "room.create",
        json!({"community_id": "7", "starts_at": 20, "ends_at": 10,
               "actor_scopes": ["rooms:update"]}),
    )
    .await;
    assert_ne!(st, StatusCode::OK, "an inverted window is refused: {r}");
}

/// Defence in depth: any non-default fact needs a verified moderator/admin role on the call.
#[tokio::test]
async fn host_room_create_refuses_non_default_facts_without_a_staff_role() {
    let h = harness(42920, false);
    for facts in [
        json!({"kind": "stage"}),
        json!({"kind": "green_room"}),
        json!({"allow_guests": true}),
        json!({"starts_at": 5}),
        json!({"ends_at": 9}),
    ] {
        // No scopes, role-like strings, and the WRONG room scope never unlock a fact.
        let wrong = if facts.get("allow_guests").is_some() {
            "rooms:update"
        } else {
            "rooms:guests"
        };
        for scopes in [
            json!([]),
            json!([
                "community:admin",
                "community:moderate",
                "moderator",
                "admin"
            ]),
            json!(["rooms:create", "rooms:close", wrong]),
        ] {
            let mut body = json!({"community_id": "7", "actor_scopes": scopes});
            body.as_object_mut()
                .unwrap()
                .extend(facts.as_object().unwrap().clone());
            let (st, r) = call(&h.host, "room.create", body).await;
            assert_eq!(st, StatusCode::FORBIDDEN, "{facts} scopes={scopes}: {r}");
        }
        // The one specific scope ALONE is enough (no admin role involved).
        let need = if facts.get("allow_guests").is_some() {
            "rooms:guests"
        } else {
            "rooms:update"
        };
        let mut body = json!({"community_id": "7", "actor_scopes": [need]});
        body.as_object_mut()
            .unwrap()
            .extend(facts.as_object().unwrap().clone());
        let (st, r) = call(&h.host, "room.create", body).await;
        assert_eq!(st, StatusCode::OK, "{facts} with {need}: {r}");
    }
    // Nothing was created by the refused calls; the default room still needs no role.
    let (st, _) = call(&h.host, "room.create", json!({"community_id": "7"})).await;
    assert_eq!(st, StatusCode::OK);
    let (st, _) = call(
        &h.host,
        "room.create",
        json!({"community_id": "7", "kind": "community", "allow_guests": false}),
    )
    .await;
    assert_eq!(
        st,
        StatusCode::OK,
        "default facts stay open to a member-role caller"
    );
}

/// `room.update` replaces the facts for a verified moderator/admin only.
#[tokio::test]
async fn host_room_update_sets_facts_for_staff_only() {
    let h = harness(42940, false);
    let room = new_room(&h).await;
    let actor = uuid::Uuid::new_v4().to_string();
    let update = |who: &str, extra: Value| {
        let scopes = match who {
            "admin" => json!(["rooms:update", "rooms:guests"]),
            "updater" => json!(["rooms:update"]),
            "moderator" => json!(["community:moderate", "moderator"]),
            "member" => json!(["community:member"]),
            _ => json!([]),
        };
        let mut b = json!({"community_id": "7", "room_id": room, "actor_id": actor,
                           "actor_scopes": scopes});
        b.as_object_mut()
            .unwrap()
            .extend(extra.as_object().unwrap().clone());
        b
    };
    for role in ["member", "guest", "moderator"] {
        // (a moderator bundle carries no rooms:update)
        let (st, r) = call(
            &h.host,
            "room.update",
            update(role, json!({"kind": "stage"})),
        )
        .await;
        assert_eq!(st, StatusCode::FORBIDDEN, "{role}: {r}");
    }
    // A missing actor or role is refused before anything changes.
    let (st, _) = call(
        &h.host,
        "room.update",
        json!({"community_id": "7", "room_id": room, "kind": "stage"}),
    )
    .await;
    assert_ne!(st, StatusCode::OK);
    let (st, r) = call(
        &h.host,
        "room.get",
        json!({"community_id": "7", "room_id": room}),
    )
    .await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(
        r["kind"], "community",
        "refused updates changed nothing: {r}"
    );
    // rooms:update alone is enough for non-guest facts; enabling guests needs rooms:guests too.
    let (st, r) = call(
        &h.host,
        "room.update",
        update("updater", json!({"kind": "stage"})),
    )
    .await;
    assert_eq!(st, StatusCode::OK, "{r}");
    let (st, r) = call(
        &h.host,
        "room.update",
        update("updater", json!({"allow_guests": true})),
    )
    .await;
    assert_eq!(st, StatusCode::FORBIDDEN, "{r}");
    // A full-scope caller succeeds.
    let (st, r) = call(
        &h.host,
        "room.update",
        update(
            "admin",
            json!({"kind": "stage", "allow_guests": true, "closed": true}),
        ),
    )
    .await;
    assert_eq!(st, StatusCode::OK, "{r}");
    assert_eq!(
        (
            r["kind"].as_str(),
            r["allow_guests"].as_bool(),
            r["closed"].as_bool()
        ),
        (Some("stage"), Some(true), Some(true))
    );
    // PATCH: omitted facts are unchanged; an explicit null clears one bound.
    let (st, r) = call(
        &h.host,
        "room.update",
        update(
            "admin",
            json!({"closed": false, "starts_at": 5, "ends_at": 50}),
        ),
    )
    .await;
    assert_eq!(st, StatusCode::OK, "{r}");
    assert_eq!(
        (r["kind"].as_str(), r["allow_guests"].as_bool()),
        (Some("stage"), Some(true)),
        "omitted kind/allow_guests stay as they were: {r}"
    );
    let (_, r) = call(
        &h.host,
        "room.update",
        update("admin", json!({"ends_at": null})),
    )
    .await;
    assert_eq!(
        (r["starts_at"].as_i64(), r["ends_at"].as_i64()),
        (Some(5), None),
        "{r}"
    );
    // Nothing becomes a town hall here, and a town hall's kind is immutable.
    let (st, r) = call(
        &h.host,
        "room.update",
        update("admin", json!({"kind": "town_hall"})),
    )
    .await;
    assert_eq!(st, StatusCode::FORBIDDEN, "{r}");
    let th = h
        .svc
        .create_with_facts(
            "7",
            None,
            None,
            svc_streaming::rooms::RouteMode::Sfu,
            None,
            svc_streaming::rooms::RoomFacts {
                kind: svc_streaming::rooms::RoomKind::TownHall,
                ..Default::default()
            },
        )
        .await
        .unwrap();
    let mut b = update("admin", json!({"kind": "community"}));
    b["room_id"] = json!(th.room_id.to_string());
    let (st, r) = call(&h.host, "room.update", b).await;
    assert_eq!(st, StatusCode::FORBIDDEN, "{r}");
    let mut b = update("admin", json!({"allow_guests": true}));
    b["room_id"] = json!(th.room_id.to_string());
    let (st, r) = call(&h.host, "room.update", b).await;
    assert_eq!(st, StatusCode::OK, "{r}");
    assert_eq!(r["kind"], "town_hall");
    // Another community cannot reach the room; a bad resulting window is refused.
    let mut other = update("admin", json!({"closed": true}));
    other["community_id"] = json!("8");
    let (st, _) = call(&h.host, "room.update", other).await;
    assert_eq!(st, StatusCode::NOT_FOUND);
    let (st, _) = call(
        &h.host,
        "room.update",
        update("admin", json!({"starts_at": 20, "ends_at": 10})),
    )
    .await;
    assert_ne!(st, StatusCode::OK);
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
        "actor_id": Uuid::new_v4(), "actor_role": "moderator",
        "actor_scopes": ["rooms:kick", "rooms:mute", "rooms:lock"]});
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

    // A caller without the action's scope is refused (a role label is never read).
    let (st, _) = call(
        &h.host,
        "room.mute",
        mod_body(&room, json!({"participant_id": p.id, "actor_scopes": []})),
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
        mod_body(&room, json!({"participant_id": v.id, "actor_scopes": []})),
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
        mod_body(&room, json!({"participant_id": p.id, "actor_scopes": []})),
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
        assert_eq!(
            keys,
            [
                "joined_at",
                "muted",
                "participant_id",
                "publisher",
                "subscriber"
            ]
        );
        Uuid::parse_str(e["participant_id"].as_str().unwrap()).unwrap();
        assert!(
            e["joined_at"].as_i64().is_some_and(|t| t > 0),
            "joined_at is the unix-seconds join time, never a made-up 0"
        );
    }
    let _ = (p.pc.close().await, v.pc.close().await);
}

/// Each moderation action needs ITS OWN verified scope: the moderator role alone, and another
/// action's scope, never suffice; the specific scope alone does.
#[tokio::test]
async fn host_moderation_ops_need_their_own_scope() {
    let h = harness(43100, true);
    let room = new_room(&h).await;
    let victim = Uuid::new_v4().to_string();
    let cases = [
        ("room.kick", "rooms:kick", json!({"participant_id": victim})),
        ("room.mute", "rooms:mute", json!({"participant_id": victim})),
        (
            "room.unmute",
            "rooms:mute",
            json!({"participant_id": victim}),
        ),
        ("room.mute_all", "rooms:mute", json!({})),
        ("room.lock", "rooms:lock", json!({})),
        ("room.unlock", "rooms:lock", json!({})),
    ];
    for (op, scope, extra) in cases {
        let others: Vec<&str> = [
            "rooms:kick",
            "rooms:mute",
            "rooms:lock",
            "rooms:hands",
            "rooms:update",
        ]
        .into_iter()
        .filter(|s| *s != scope)
        .collect();
        for scopes in [
            json!([]),
            json!([
                "community:moderate",
                "moderator",
                "streaming.rooms:moderate"
            ]),
            json!(others),
        ] {
            let mut b = mod_body(&room, extra.clone());
            b["actor_scopes"] = scopes.clone();
            let (st, r) = call(&h.host, op, b).await;
            assert_eq!(st, StatusCode::FORBIDDEN, "{op} with {scopes}: {r}");
        }
        let mut b = mod_body(&room, extra.clone());
        b["actor_scopes"] = json!([scope]);
        let (st, r) = call(&h.host, op, b).await;
        assert_ne!(st, StatusCode::FORBIDDEN, "{op} with only {scope}: {r}");
    }
}
