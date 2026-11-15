//! Host-API (`POST /host/v1/webrtc/{op}`) tests for the `room.hand.*` ops and
//! `room.staff.register` that the `webrtc` service bundle calls: request/response shapes per
//! `bundles/rust/webrtc/README.md` and the authorization model of the gRPC surface -- no
//! role in the body (unknown fields are refused), authority from the server-side member table
//! only. Real `RoomService`, real router; the stage's mTLS is covered by
//! `rooms_host_api_mtls.rs`.

mod rtc_common;

use std::sync::Arc;

use axum::body::Body;
use axum::http::{header, Request, StatusCode};
use serde_json::{json, Value};
use svc_streaming::rooms::{host_api, RoomService};
use svc_streaming::rtc::metrics::RtcMetrics;
use svc_streaming::rtc::pc_factory::PeerConnectionFactory;
use tower::ServiceExt;
use uuid::Uuid;

use rtc_common::*;

fn service(base: u16, moderation: bool) -> (axum::Router, Arc<RoomService>) {
    let factory =
        Arc::new(PeerConnectionFactory::new(loopback_rtc_config((base, base + 9))).unwrap());
    let metrics = RtcMetrics::register(&prometheus::Registry::new()).unwrap();
    let svc = Arc::new(
        RoomService::new(
            factory,
            metrics,
            Some(test_token_keys()),
            "http://svc.test".into(),
        )
        .with_moderation(moderation),
    );
    (host_api::router(svc.clone()), svc)
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

struct Room {
    router: axum::Router,
    id: String,
}

async fn new_room(router: &axum::Router, community: &str) -> Room {
    let (st, r) = call(router, "room.create", json!({"community_id": community})).await;
    assert_eq!(st, StatusCode::OK, "{r}");
    Room {
        router: router.clone(),
        id: r["room_id"].as_str().unwrap().to_string(),
    }
}

impl Room {
    /// Registers `who` as an ordinary member the way every host token op does.
    async fn join(&self, who: &str) {
        let (st, r) = call(
            &self.router,
            "token.issue",
            json!({"community_id": "7", "room_id": self.id, "participant_id": who,
                   "publish": false, "subscribe": true}),
        )
        .await;
        assert_eq!(st, StatusCode::OK, "{r}");
    }

    async fn staff(&self, who: &str) {
        let (st, r) = call(
            &self.router,
            "room.staff.register",
            json!({"api_version": "v1", "community_id": "7", "room_id": self.id, "actor_id": who}),
        )
        .await;
        assert_eq!(st, StatusCode::OK, "{r}");
        assert_eq!(r["staff"], true);
    }

    async fn op(&self, op: &str, extra: Value) -> (StatusCode, Value) {
        // Only the hand/staff ops carry a contract version; the moderation ops refuse the field.
        let mut body = json!({"community_id": "7", "room_id": self.id});
        if op.starts_with("room.hand.") || op.starts_with("room.staff.") {
            body["api_version"] = json!("v1");
        }
        for (k, v) in extra.as_object().unwrap() {
            body[k] = v.clone();
        }
        call(&self.router, op, body).await
    }
}

fn uid() -> String {
    Uuid::new_v4().to_string()
}

#[tokio::test]
async fn hand_ops_have_the_documented_shapes_and_flow() {
    let (router, _svc) = service(43000, true);
    let room = new_room(&router, "7").await;
    let (a, b, m) = (uid(), uid(), uid());
    room.join(&a).await;
    room.join(&b).await;
    room.staff(&m).await;

    // raise: {room_id, hand:{participant_id, raised_at}}; idempotent.
    let (st, r) = room
        .op(
            "room.hand.raise",
            json!({"participant_id": a, "actor_id": a}),
        )
        .await;
    assert_eq!(st, StatusCode::OK, "{r}");
    assert_eq!(r["room_id"], room.id);
    assert_eq!(r["hand"]["participant_id"], a);
    assert!(r["hand"]["raised_at"].as_i64().unwrap() > 0);
    assert!(
        r["hand"].get("acknowledged_at").is_none(),
        "absent until acknowledged: {r}"
    );
    let (_, again) = room
        .op(
            "room.hand.raise",
            json!({"participant_id": a, "actor_id": a}),
        )
        .await;
    assert_eq!(again, r, "raise twice is a no-op");
    room.op(
        "room.hand.raise",
        json!({"participant_id": b, "actor_id": b}),
    )
    .await;

    // list: staff get the ordered queue, members only their own entry (+ count, position).
    let (st, all) = room.op("room.hand.list", json!({"actor_id": m})).await;
    assert_eq!(st, StatusCode::OK, "{all}");
    assert_eq!(all["room_id"], room.id);
    let order: Vec<&str> = all["hands"]
        .as_array()
        .unwrap()
        .iter()
        .map(|h| h["participant_id"].as_str().unwrap())
        .collect();
    assert_eq!(order, vec![a.as_str(), b.as_str()]);
    assert_eq!(all["count"], 2);
    let (_, mine) = room.op("room.hand.list", json!({"actor_id": b})).await;
    assert_eq!(mine["hands"].as_array().unwrap().len(), 1);
    assert_eq!(
        (mine["count"].clone(), mine["position"].clone()),
        (json!(2), json!(2))
    );

    // ack: {room_id, hand:{..acknowledged_at, acknowledged_by}} (staff only).
    let (st, ack) = room
        .op("room.hand.ack", json!({"participant_id": a, "actor_id": m}))
        .await;
    assert_eq!(st, StatusCode::OK, "{ack}");
    assert_eq!(ack["hand"]["acknowledged_by"], m);
    assert!(ack["hand"]["acknowledged_at"].as_i64().is_some());

    // lower: own (member) and staff-on-other; {room_id, participant_id, lowered}; no-op = false.
    let (st, l) = room
        .op(
            "room.hand.lower",
            json!({"participant_id": b, "actor_id": b}),
        )
        .await;
    assert_eq!(
        (st, l["lowered"].clone(), l["participant_id"].clone()),
        (StatusCode::OK, json!(true), json!(b))
    );
    let (_, l) = room
        .op(
            "room.hand.lower",
            json!({"participant_id": b, "actor_id": b}),
        )
        .await;
    assert_eq!(l["lowered"], false, "no hand is a no-op, not an error");
    let (st, l) = room
        .op(
            "room.hand.lower",
            json!({"participant_id": a, "actor_id": m}),
        )
        .await;
    assert_eq!((st, l["lowered"].clone()), (StatusCode::OK, json!(true)));

    // reset: {room_id, cleared}.
    room.op(
        "room.hand.raise",
        json!({"participant_id": a, "actor_id": a}),
    )
    .await;
    let (st, r) = room.op("room.hand.reset", json!({"actor_id": m})).await;
    assert_eq!(
        (st, r["cleared"].clone(), r["room_id"].clone()),
        (StatusCode::OK, json!(1), json!(room.id))
    );
}

#[tokio::test]
async fn role_claims_are_refused_and_authority_is_the_member_table() {
    let (router, _svc) = service(43020, true);
    let room = new_room(&router, "7").await;
    let (a, member, stranger, m) = (uid(), uid(), uid(), uid());
    room.join(&a).await;
    room.join(&member).await;
    room.staff(&m).await;
    room.op(
        "room.hand.raise",
        json!({"participant_id": a, "actor_id": a}),
    )
    .await;

    // A role field is not part of the contract: refused outright (unknown field), so a bundle
    // that still sends one fails loudly instead of being silently believed.
    for (op, extra) in [
        (
            "room.hand.ack",
            json!({"participant_id": a, "actor_id": member, "actor_role": "admin"}),
        ),
        (
            "room.hand.reset",
            json!({"actor_id": member, "actor_role": "moderator"}),
        ),
        (
            "room.hand.lower",
            json!({"participant_id": a, "actor_id": member, "actor_role": "admin"}),
        ),
        (
            "room.hand.raise",
            json!({"participant_id": a, "actor_id": a, "actor_role": "member"}),
        ),
        (
            "room.hand.list",
            json!({"actor_id": member, "actor_role": "admin"}),
        ),
    ] {
        let (st, r) = room.op(op, extra).await;
        assert_eq!(st, StatusCode::BAD_REQUEST, "{op}: {r}");
        assert_eq!(r["error"], "invalid_args");
    }

    // A joined member is not staff, whatever they are: forbidden on every staff action.
    for (op, extra) in [
        (
            "room.hand.ack",
            json!({"participant_id": a, "actor_id": member}),
        ),
        ("room.hand.reset", json!({"actor_id": member})),
        (
            "room.hand.lower",
            json!({"participant_id": a, "actor_id": member}),
        ),
    ] {
        let (st, r) = room.op(op, extra).await;
        assert_eq!(st, StatusCode::FORBIDDEN, "{op}: {r}");
        assert_eq!(r["error"], "forbidden");
    }
    // Mismatched actor (member raising a's hand) and a non-participant are forbidden too.
    let (st, _) = room
        .op(
            "room.hand.raise",
            json!({"participant_id": a, "actor_id": member}),
        )
        .await;
    assert_eq!(st, StatusCode::FORBIDDEN);
    for (op, extra) in [
        (
            "room.hand.raise",
            json!({"participant_id": stranger, "actor_id": stranger}),
        ),
        (
            "room.hand.lower",
            json!({"participant_id": stranger, "actor_id": stranger}),
        ),
        ("room.hand.list", json!({"actor_id": stranger})),
        ("room.hand.reset", json!({"actor_id": stranger})),
        (
            "room.hand.ack",
            json!({"participant_id": a, "actor_id": stranger}),
        ),
    ] {
        let (st, r) = room.op(op, extra).await;
        assert_eq!(st, StatusCode::FORBIDDEN, "non-participant {op}: {r}");
    }
    // Staff still works, and a member's list never shows other people's entries.
    let (_, listed) = room.op("room.hand.list", json!({"actor_id": member})).await;
    assert_eq!(listed["hands"], json!([]));
    assert_eq!(listed["count"], 1);
    let (st, _) = room
        .op("room.hand.ack", json!({"participant_id": a, "actor_id": m}))
        .await;
    assert_eq!(st, StatusCode::OK);
}

#[tokio::test]
async fn api_version_ids_and_unknown_ops_are_loud() {
    let (router, _svc) = service(43040, true);
    let room = new_room(&router, "7").await;
    let (a, m) = (uid(), uid());
    room.join(&a).await;
    room.staff(&m).await;

    for v in [json!(""), json!("v2"), json!("V1")] {
        let (st, r) = room
            .op(
                "room.hand.raise",
                json!({"api_version": v, "participant_id": a, "actor_id": a}),
            )
            .await;
        assert_eq!(st, StatusCode::NOT_IMPLEMENTED, "{v}: {r}");
        assert!(
            r["message"].as_str().unwrap().contains("not supported"),
            "{r}"
        );
    }
    // A request with NO api_version is the same refusal (never silently v1).
    let (st, r) = call(
        &router,
        "room.hand.reset",
        json!({"community_id": "7", "room_id": room.id, "actor_id": m}),
    )
    .await;
    assert_eq!(st, StatusCode::NOT_IMPLEMENTED, "{r}");
    // Participant/actor ids are UUIDs, never names.
    for bad in ["alice", "", "not-a-uuid"] {
        let (st, r) = room
            .op(
                "room.hand.raise",
                json!({"participant_id": bad, "actor_id": a}),
            )
            .await;
        assert_eq!(st, StatusCode::BAD_REQUEST, "participant {bad:?}: {r}");
        let (st, r) = room
            .op(
                "room.hand.raise",
                json!({"participant_id": a, "actor_id": bad}),
            )
            .await;
        assert_eq!(st, StatusCode::BAD_REQUEST, "actor {bad:?}: {r}");
    }
    // Missing required field.
    let (st, _) = room.op("room.hand.ack", json!({"participant_id": a})).await;
    assert_eq!(st, StatusCode::BAD_REQUEST);
    // Nothing to acknowledge.
    let (st, r) = room
        .op("room.hand.ack", json!({"participant_id": a, "actor_id": m}))
        .await;
    assert_eq!(st, StatusCode::BAD_REQUEST, "{r}");
}

/// Regression: another community's room is NOT_FOUND for every hand op and for staff
/// registration -- also for an actor who is staff of the real room.
#[tokio::test]
async fn cross_community_hand_ops_are_not_found() {
    let (router, _svc) = service(43060, true);
    let room = new_room(&router, "7").await;
    let (a, m) = (uid(), uid());
    room.join(&a).await;
    room.staff(&m).await;
    room.op(
        "room.hand.raise",
        json!({"participant_id": a, "actor_id": a}),
    )
    .await;

    for (op, extra) in [
        (
            "room.hand.raise",
            json!({"participant_id": a, "actor_id": a}),
        ),
        (
            "room.hand.lower",
            json!({"participant_id": a, "actor_id": m}),
        ),
        ("room.hand.list", json!({"actor_id": m})),
        ("room.hand.ack", json!({"participant_id": a, "actor_id": m})),
        ("room.hand.reset", json!({"actor_id": m})),
        ("room.staff.register", json!({"actor_id": a})),
    ] {
        let mut body = json!({"api_version": "v1", "community_id": "8", "room_id": room.id});
        for (k, v) in extra.as_object().unwrap() {
            body[k] = v.clone();
        }
        let (st, r) = call(&router, op, body).await;
        assert_eq!(st, StatusCode::NOT_FOUND, "{op}: {r}");
        assert_eq!(r["error"], "not_found");
    }
    // Room 7 is untouched, and `a` was not promoted by the cross-community register attempt.
    let (st, _) = room.op("room.hand.reset", json!({"actor_id": a})).await;
    assert_eq!(st, StatusCode::FORBIDDEN);
    let (_, all) = room.op("room.hand.list", json!({"actor_id": m})).await;
    assert_eq!(all["count"], 1);
}

#[tokio::test]
async fn staff_ops_follow_the_moderation_flag_and_kicks_clear_hands() {
    // Flag off: staff operations are loudly unavailable, participant operations work.
    let (router, _svc) = service(43080, false);
    let room = new_room(&router, "7").await;
    let (a, m) = (uid(), uid());
    room.join(&a).await;
    let (st, r) = room.op("room.staff.register", json!({"actor_id": m})).await;
    assert_eq!(st, StatusCode::NOT_IMPLEMENTED, "{r}");
    let (st, _) = room
        .op(
            "room.hand.raise",
            json!({"participant_id": a, "actor_id": a}),
        )
        .await;
    assert_eq!(st, StatusCode::OK);
    let (st, _) = room.op("room.hand.list", json!({"actor_id": a})).await;
    assert_eq!(st, StatusCode::OK);

    // Flag on: a kick drops the kicked participant's hand and membership.
    let (router, _svc) = service(43100, true);
    let room = new_room(&router, "7").await;
    let (a, b, m) = (uid(), uid(), uid());
    room.join(&a).await;
    room.join(&b).await;
    room.staff(&m).await;
    room.op(
        "room.hand.raise",
        json!({"participant_id": a, "actor_id": a}),
    )
    .await;
    room.op(
        "room.hand.raise",
        json!({"participant_id": b, "actor_id": b}),
    )
    .await;
    let (st, r) = room
        .op(
            "room.kick",
            json!({"participant_id": a, "actor_id": m, "actor_role": "moderator"}),
        )
        .await;
    assert_eq!(st, StatusCode::OK, "{r}");
    let (_, all) = room.op("room.hand.list", json!({"actor_id": m})).await;
    assert_eq!(all["count"], 1);
    let (st, _) = room
        .op(
            "room.hand.raise",
            json!({"participant_id": a, "actor_id": a}),
        )
        .await;
    assert_eq!(st, StatusCode::FORBIDDEN, "kicked participant cannot raise");
    // ...and cannot obtain a new token to rejoin either.
    let (st, _) = call(
        &router,
        "token.issue",
        json!({"community_id": "7", "room_id": room.id, "participant_id": a, "publish": false, "subscribe": true}),
    )
    .await;
    assert_eq!(st, StatusCode::FORBIDDEN);
}
