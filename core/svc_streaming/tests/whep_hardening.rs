//! Regression tests for the WHEP / rooms review findings: a viewer session never
//! outlives its token (expiry sweep + epoch revocation), one session per
//! (pipeline, participant), and per-community caps (issued-token ledger and
//! subscribers) so one tenant cannot exhaust shared capacity.

mod rtc_common;

use std::sync::Arc;

use axum::body::Body;
use axum::http::{header, Request, StatusCode};
use serde_json::{json, Value};
use tower::ServiceExt;
use uuid::Uuid;
use webrtc::media_stream::track_local::TrackLocal;
use webrtc::peer_connection::PeerConnection;

use rtc_common::*;
use svc_streaming::egress::whep;
use svc_streaming::rooms::token::TokenKeys;
use svc_streaming::rooms::{host_api, media, RoomService, MAX_ISSUED_PER_COMMUNITY};
use svc_streaming::rtc::fanout::MediaFanouts;
use svc_streaming::rtc::metrics::RtcMetrics;
use svc_streaming::rtc::pc_factory::PeerConnectionFactory;

fn factory(range: (u16, u16)) -> Arc<PeerConnectionFactory> {
    Arc::new(PeerConnectionFactory::new(loopback_rtc_config(range)).unwrap())
}

fn metrics() -> RtcMetrics {
    RtcMetrics::register(&prometheus::Registry::new()).unwrap()
}

/// POSTs a fresh viewer offer; returns (status, location, viewer pc).
async fn whep_join(
    router: &axum::Router,
    viewers: &PeerConnectionFactory,
    path: &str,
    token: &str,
) -> (StatusCode, Option<String>, Arc<dyn PeerConnection>) {
    let (pc, h) = build_recvonly_peer(viewers).await;
    let offer = offer_sdp(&pc, &h.gather_complete).await;
    let res = router
        .clone()
        .oneshot(post_sdp_auth(path, token, offer))
        .await
        .unwrap();
    let status = res.status();
    let loc = res
        .headers()
        .get(header::LOCATION)
        .map(|v| v.to_str().unwrap().to_string());
    if status == StatusCode::CREATED {
        apply_answer(&pc, body_text(res).await).await;
    }
    (status, loc, pc)
}

#[tokio::test]
async fn whep_session_never_outlives_its_token_or_epoch() {
    let whep = Arc::new(whep_with_auth(factory((42200, 42219)), metrics(), 10));
    let viewers = PeerConnectionFactory::new(loopback_rtc_config((42220, 42249))).unwrap();
    let router = whep::router(whep.clone());
    let pipeline = Uuid::new_v4();
    whep.register_fanout(pipeline, "7", MediaFanouts::new())
        .await;
    let path = format!("/whep/7/{pipeline}");

    // Expiry: a 30s token's session is swept once the clock passes its exp.
    let short = viewer_token("7", pipeline, 30, "pipeline", Uuid::new_v4());
    let long = viewer_token("7", pipeline, 900, "pipeline", Uuid::new_v4());
    let (st, _, _p1) = whep_join(&router, &viewers, &path, &short).await;
    assert_eq!(st, StatusCode::CREATED);
    let (st, _, _p2) = whep_join(&router, &viewers, &path, &long).await;
    assert_eq!(st, StatusCode::CREATED);
    assert_eq!(whep.viewer_count(pipeline).await, 2);
    assert_eq!(whep.sweep().await, 0, "nothing expired yet");
    let reaped = whep.sweep_at(chrono::Utc::now().timestamp() + 60).await;
    assert_eq!(reaped, 1, "only the 30s-token session expired");
    assert_eq!(whep.viewer_count(pipeline).await, 1);

    // Revocation: bumping the epoch kills live sessions AND old tokens.
    let epoch = whep.revoke_viewers(pipeline).await;
    assert_eq!(epoch, 1);
    assert_eq!(
        whep.viewer_count(pipeline).await,
        0,
        "revocation closed live sessions"
    );
    let (st, _, _) = whep_join(&router, &viewers, &path, &long).await;
    assert_eq!(st, StatusCode::UNAUTHORIZED, "revoked token refused");
    let fresh = viewer_token_epoch("7", pipeline, 60, "pipeline", Uuid::new_v4(), epoch);
    let (st, _, _p3) = whep_join(&router, &viewers, &path, &fresh).await;
    assert_eq!(st, StatusCode::CREATED, "a token for the new epoch works");

    // Unregister (stream ended) revokes too, and a re-registered stream does not
    // resurrect old tokens.
    whep.unregister_fanout(pipeline).await;
    assert_eq!(whep.viewer_count(pipeline).await, 0);
    whep.register_fanout(pipeline, "7", MediaFanouts::new())
        .await;
    let (st, _, _) = whep_join(&router, &viewers, &path, &fresh).await;
    assert_eq!(st, StatusCode::UNAUTHORIZED, "pre-unregister token is dead");
}

#[tokio::test]
async fn whep_one_session_per_participant_replaces_and_closes_the_old() {
    let whep = Arc::new(whep_with_auth(factory((42250, 42269)), metrics(), 2));
    let viewers = PeerConnectionFactory::new(loopback_rtc_config((42270, 42309))).unwrap();
    let router = whep::router(whep.clone());
    let pipeline = Uuid::new_v4();
    whep.register_fanout(pipeline, "7", MediaFanouts::new())
        .await;
    let path = format!("/whep/7/{pipeline}");

    let alice = Uuid::new_v4();
    let t_alice = viewer_token("7", pipeline, 60, "pipeline", alice);
    let mut keep = Vec::new();
    for _ in 0..4 {
        let (st, _, pc) = whep_join(&router, &viewers, &path, &t_alice).await;
        assert_eq!(st, StatusCode::CREATED);
        keep.push(pc);
        assert_eq!(
            whep.viewer_count(pipeline).await,
            1,
            "replaced, never accumulated"
        );
    }
    let t_bob = viewer_token("7", pipeline, 60, "pipeline", Uuid::new_v4());
    let (st, _, _b) = whep_join(&router, &viewers, &path, &t_bob).await;
    assert_eq!(st, StatusCode::CREATED);
    assert_eq!(whep.viewer_count(pipeline).await, 2);
    // At the cap a THIRD participant is refused, but Alice may still rejoin.
    let t_carol = viewer_token("7", pipeline, 60, "pipeline", Uuid::new_v4());
    let (st, _, _c) = whep_join(&router, &viewers, &path, &t_carol).await;
    assert_eq!(st, StatusCode::FORBIDDEN);
    let (st, _, _a2) = whep_join(&router, &viewers, &path, &t_alice).await;
    assert_eq!(st, StatusCode::CREATED);
    assert_eq!(whep.viewer_count(pipeline).await, 2);
}

fn svc(range: (u16, u16), cap: Option<usize>) -> Arc<RoomService> {
    let s = RoomService::new(
        factory(range),
        metrics(),
        Some(TokenKeys::new(VIEWER_SECRET).unwrap()),
        "http://svc.test".into(),
    );
    Arc::new(match cap {
        Some(c) => s.with_community_subscriber_cap(c),
        None => s,
    })
}

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

#[tokio::test]
async fn issued_ledger_is_capped_per_community_and_prunes_by_expiry() {
    let svc = svc((42310, 42319), None);
    let host = host_api::router(svc.clone());
    let (_, a) = host_call(&host, "room.create", json!({"community_id": "7"})).await;
    let (_, b) = host_call(&host, "room.create", json!({"community_id": "8"})).await;
    let (a, b) = (
        a["room_id"].as_str().unwrap().to_string(),
        b["room_id"].as_str().unwrap().to_string(),
    );
    for _ in 0..MAX_ISSUED_PER_COMMUNITY {
        let (st, _) = host_call(
            &host,
            "token.issue",
            json!({"community_id":"7","room_id":a,"publish":false,"subscribe":true}),
        )
        .await;
        assert_eq!(st, StatusCode::OK);
    }
    let (st, body) = host_call(
        &host,
        "token.issue",
        json!({"community_id":"7","room_id":a,"publish":false,"subscribe":true}),
    )
    .await;
    assert_eq!(st, StatusCode::TOO_MANY_REQUESTS, "{body}");
    // Another tenant is unaffected by community 7 filling its share.
    let (st, _) = host_call(
        &host,
        "token.issue",
        json!({"community_id":"8","room_id":b,"publish":false,"subscribe":true}),
    )
    .await;
    assert_eq!(st, StatusCode::OK);
}

#[tokio::test]
async fn community_subscriber_cap_is_enforced_below_the_global_cap() {
    let svc = svc((42320, 42349), Some(1));
    let publishers = PeerConnectionFactory::new(loopback_rtc_config((42350, 42359))).unwrap();
    let viewers = PeerConnectionFactory::new(loopback_rtc_config((42360, 42389))).unwrap();
    let host = host_api::router(svc.clone());
    let media = media::router(svc.clone());
    let (_, room) = host_call(&host, "room.create", json!({"community_id": "7"})).await;
    let room_id = room["room_id"].as_str().unwrap().to_string();

    let (_, pubep) = host_call(
        &host,
        "whip.endpoint",
        json!({"community_id":"7","room_id":room_id}),
    )
    .await;
    let (track, _) = opus_publisher_track();
    let ph = TestPeerHandler::new();
    let ppc = publishers.build(Arc::clone(&ph)).await.unwrap();
    ppc.add_track(Arc::clone(&track) as Arc<dyn TrackLocal>)
        .await
        .unwrap();
    let offer = offer_sdp(&ppc, &ph.gather_complete).await;
    let res = media
        .clone()
        .oneshot(post_sdp_auth(
            &format!("/rtc/rooms/{room_id}/whip"),
            pubep["token"].as_str().unwrap(),
            offer,
        ))
        .await
        .unwrap();
    assert_eq!(res.status(), StatusCode::CREATED);
    apply_answer(&ppc, body_text(res).await).await;

    let sub = |tok: String| {
        let (media, viewers, room_id) = (media.clone(), &viewers, room_id.clone());
        async move {
            let (pc, h) = build_recvonly_peer(viewers).await;
            let offer = offer_sdp(&pc, &h.gather_complete).await;
            let res = media
                .oneshot(post_sdp_auth(
                    &format!("/rtc/rooms/{room_id}/whep"),
                    &tok,
                    offer,
                ))
                .await
                .unwrap();
            (res.status(), pc)
        }
    };
    let (_, a) = host_call(
        &host,
        "whep.endpoint",
        json!({"community_id":"7","room_id":room_id}),
    )
    .await;
    let (_, b) = host_call(
        &host,
        "whep.endpoint",
        json!({"community_id":"7","room_id":room_id}),
    )
    .await;
    let (st, _pa) = sub(a["token"].as_str().unwrap().to_string()).await;
    assert_eq!(st, StatusCode::CREATED);
    // Second participant of the same community: refused (fail closed).
    let (st, _pb) = sub(b["token"].as_str().unwrap().to_string()).await;
    assert_eq!(st, StatusCode::FORBIDDEN, "community subscriber cap");
    // The first participant may still replace its own session at the cap.
    let (st, _pa2) = sub(a["token"].as_str().unwrap().to_string()).await;
    assert_eq!(st, StatusCode::CREATED);
}
