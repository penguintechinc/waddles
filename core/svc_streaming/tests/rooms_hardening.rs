//! Security-hardening tests for rooms: revocation is room/community-scoped and
//! quota-bound, per-room epochs invalidate old tokens, subscriber sessions are
//! replaced per participant and reaped when their peer connection dies, and
//! pipeline (WHEP) viewer tokens are only issued for streams a community owns.

mod rtc_common;

use std::sync::Arc;

use axum::body::Body;
use axum::http::{header, Request, StatusCode};
use serde_json::{json, Value};
use tower::ServiceExt;
use uuid::Uuid;
use webrtc::media_stream::track_local::TrackLocal;

use rtc_common::*;
use svc_streaming::egress::whep::WhepState;
use svc_streaming::rooms::token::TokenKeys;
use svc_streaming::rooms::{host_api, media, RoomService, MAX_REVOKED_PER_COMMUNITY};
use svc_streaming::rtc::fanout::MediaFanouts;
use svc_streaming::rtc::metrics::RtcMetrics;
use svc_streaming::rtc::pc_factory::PeerConnectionFactory;

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
    post_sdp_auth(path, token, sdp)
}

fn service(range: (u16, u16)) -> Arc<RoomService> {
    let factory = Arc::new(PeerConnectionFactory::new(loopback_rtc_config(range)).unwrap());
    let metrics = RtcMetrics::register(&prometheus::Registry::new()).unwrap();
    Arc::new(RoomService::new(
        factory,
        metrics,
        Some(TokenKeys::new(VIEWER_SECRET).unwrap()),
        "http://svc.test".into(),
    ))
}

#[tokio::test]
async fn revocation_is_scoped_bounded_and_epoch_invalidates_everything() {
    let svc = service((42100, 42109));
    let host = host_api::router(svc.clone());
    let media = media::router(svc.clone());
    let (_, a) = host_call(&host, "room.create", json!({"community_id": "7"})).await;
    let (_, b) = host_call(&host, "room.create", json!({"community_id": "7"})).await;
    let (_, c8) = host_call(&host, "room.create", json!({"community_id": "8"})).await;
    let (a, b, c8) = (
        a["room_id"].as_str().unwrap().to_string(),
        b["room_id"].as_str().unwrap().to_string(),
        c8["room_id"].as_str().unwrap().to_string(),
    );
    let (_, t) = host_call(
        &host,
        "token.issue",
        json!({"community_id":"7","room_id":a,"publish":true,"subscribe":true}),
    )
    .await;
    let tid = t["token_id"].as_str().unwrap().to_string();

    // Another community, or another room, cannot revoke this token id.
    let (st, _) = host_call(
        &host,
        "token.revoke",
        json!({"community_id":"8","room_id":c8,"token_id":tid}),
    )
    .await;
    assert_eq!(
        st,
        StatusCode::NOT_FOUND,
        "cross-tenant revoke of a foreign jti"
    );
    let (st, _) = host_call(
        &host,
        "token.revoke",
        json!({"community_id":"7","room_id":b,"token_id":tid}),
    )
    .await;
    assert_eq!(st, StatusCode::NOT_FOUND, "cross-room revoke");
    // Unknown jti is indistinguishable.
    let (st, _) = host_call(
        &host,
        "token.revoke",
        json!({"community_id":"7","room_id":a,"token_id":Uuid::new_v4()}),
    )
    .await;
    assert_eq!(st, StatusCode::NOT_FOUND);
    // Token still good for its own room after the failed attempts.
    assert!(svc
        .authorize(t["token"].as_str().unwrap(), a.parse().unwrap())
        .await
        .is_ok());

    // Per-room epoch: revoke_all invalidates every older token, new ones work.
    let (st, e) = host_call(
        &host,
        "token.revoke_all",
        json!({"community_id":"7","room_id":a}),
    )
    .await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(e["epoch"], 1);
    assert!(svc
        .authorize(t["token"].as_str().unwrap(), a.parse().unwrap())
        .await
        .is_err());
    let (st, _) = host_call(
        &host,
        "token.revoke_all",
        json!({"community_id":"8","room_id":a}),
    )
    .await;
    assert_eq!(st, StatusCode::NOT_FOUND, "epoch bump is community-scoped");
    let (_, t2) = host_call(
        &host,
        "token.issue",
        json!({"community_id":"7","room_id":a,"publish":false,"subscribe":true}),
    )
    .await;
    assert!(svc
        .authorize(t2["token"].as_str().unwrap(), a.parse().unwrap())
        .await
        .is_ok());
    // And the old token is refused at the HTTP boundary too.
    let res = media
        .clone()
        .oneshot(sdp_post(
            &format!("/rtc/rooms/{a}/whep"),
            t["token"].as_str().unwrap(),
            "v=0\r\n".into(),
        ))
        .await
        .unwrap();
    assert_eq!(res.status(), StatusCode::UNAUTHORIZED);

    // Per-community quota on live revocations; another community is unaffected.
    for _ in 0..MAX_REVOKED_PER_COMMUNITY {
        let (_, t) = host_call(
            &host,
            "token.issue",
            json!({"community_id":"7","room_id":a,"publish":false,"subscribe":true}),
        )
        .await;
        let (st, _) = host_call(
            &host,
            "token.revoke",
            json!({"community_id":"7","room_id":a,"token_id":t["token_id"]}),
        )
        .await;
        assert_eq!(st, StatusCode::OK);
    }
    let (_, t) = host_call(
        &host,
        "token.issue",
        json!({"community_id":"7","room_id":a,"publish":false,"subscribe":true}),
    )
    .await;
    let (st, b_) = host_call(
        &host,
        "token.revoke",
        json!({"community_id":"7","room_id":a,"token_id":t["token_id"]}),
    )
    .await;
    assert_eq!(st, StatusCode::TOO_MANY_REQUESTS, "{b_}");
    let (_, t8) = host_call(
        &host,
        "token.issue",
        json!({"community_id":"8","room_id":c8,"publish":false,"subscribe":true}),
    )
    .await;
    let (st, _) = host_call(
        &host,
        "token.revoke",
        json!({"community_id":"8","room_id":c8,"token_id":t8["token_id"]}),
    )
    .await;
    assert_eq!(
        st,
        StatusCode::OK,
        "community 7's quota never blocks community 8"
    );
}

#[tokio::test]
async fn subscriber_sessions_are_replaced_per_participant_and_reaped() {
    let server = Arc::new(PeerConnectionFactory::new(loopback_rtc_config((42120, 42139))).unwrap());
    let publisher_factory =
        PeerConnectionFactory::new(loopback_rtc_config((42140, 42149))).unwrap();
    let viewer_factory = PeerConnectionFactory::new(loopback_rtc_config((42150, 42169))).unwrap();
    let metrics = RtcMetrics::register(&prometheus::Registry::new()).unwrap();
    let svc = Arc::new(RoomService::new(
        server,
        metrics,
        Some(TokenKeys::new(VIEWER_SECRET).unwrap()),
        "http://svc.test".into(),
    ));
    let host = host_api::router(svc.clone());
    let media = media::router(svc.clone());
    let (_, room) = host_call(&host, "room.create", json!({"community_id": "7"})).await;
    let room_id = room["room_id"].as_str().unwrap().to_string();

    // A live publisher so subscribers have a source.
    let (_, pubep) = host_call(
        &host,
        "whip.endpoint",
        json!({"community_id":"7","room_id":room_id}),
    )
    .await;
    let (track, _ssrc) = opus_publisher_track();
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
            pubep["token"].as_str().unwrap(),
            offer,
        ))
        .await
        .unwrap();
    assert_status(&res, StatusCode::CREATED);
    apply_answer(&ppc, body_text(res).await).await;

    // One participant (one token) subscribing twice keeps ONE session.
    let (_, sub) = host_call(
        &host,
        "whep.endpoint",
        json!({"community_id":"7","room_id":room_id}),
    )
    .await;
    let tok = sub["token"].as_str().unwrap().to_string();
    let mut viewers = Vec::new();
    for _ in 0..3 {
        let (pc, h) = build_recvonly_peer(&viewer_factory).await;
        let voffer = offer_sdp(&pc, &h.gather_complete).await;
        let res = media
            .clone()
            .oneshot(sdp_post(
                &format!("/rtc/rooms/{room_id}/whep"),
                &tok,
                voffer,
            ))
            .await
            .unwrap();
        assert_status(&res, StatusCode::CREATED);
        apply_answer(&pc, body_text(res).await).await;
        viewers.push(pc);
        let (_, info) = host_call(
            &host,
            "room.get",
            json!({"community_id":"7","room_id":room_id}),
        )
        .await;
        assert_eq!(info["subscriber_count"], 1, "replaced, not accumulated");
    }

    // A different participant adds a second session.
    let (_, sub2) = host_call(
        &host,
        "whep.endpoint",
        json!({"community_id":"7","room_id":room_id,"ttl_secs":30}),
    )
    .await;
    let (pc2, h2) = build_recvonly_peer(&viewer_factory).await;
    let voffer = offer_sdp(&pc2, &h2.gather_complete).await;
    let res = media
        .clone()
        .oneshot(sdp_post(
            &format!("/rtc/rooms/{room_id}/whep"),
            sub2["token"].as_str().unwrap(),
            voffer,
        ))
        .await
        .unwrap();
    assert_status(&res, StatusCode::CREATED);
    apply_answer(&pc2, body_text(res).await).await;
    let (_, info) = host_call(
        &host,
        "room.get",
        json!({"community_id":"7","room_id":room_id}),
    )
    .await;
    assert_eq!(info["subscriber_count"], 2);

    // Reaping. A session may not outlive its token: sweeping past the short-lived
    // token's expiry removes exactly that session (webrtc-rs gives no reliable
    // signal when a client silently vanishes, so the credential bounds it).
    let _ = pc2.close().await;
    let reaped = svc.sweep_at(chrono::Utc::now().timestamp() + 60).await;
    assert_eq!(reaped, 1, "only the 30s-token session had expired");
    let (_, info) = host_call(
        &host,
        "room.get",
        json!({"community_id":"7","room_id":room_id}),
    )
    .await;
    assert_eq!(info["subscriber_count"], 1);
    // Nothing else is reaped while sessions are live and tokens valid.
    assert_eq!(svc.sweep().await, 0);
    let _ = ppc.close().await;
    for v in viewers {
        let _ = v.close().await;
    }
}

#[tokio::test]
async fn pipeline_viewer_tokens_only_for_streams_the_community_owns() {
    let factory =
        Arc::new(PeerConnectionFactory::new(loopback_rtc_config((42170, 42179))).unwrap());
    let metrics = RtcMetrics::register(&prometheus::Registry::new()).unwrap();
    let keys = TokenKeys::new(VIEWER_SECRET).unwrap();
    let whep = Arc::new(
        WhepState::new(factory.clone(), metrics.clone(), 5).with_viewer_auth(keys.clone()),
    );
    let pipeline = Uuid::new_v4();
    whep.register_fanout(pipeline, "7", MediaFanouts::new())
        .await;
    let svc = Arc::new(
        RoomService::new(factory, metrics, Some(keys), "http://svc.test".into())
            .with_whep(whep.clone()),
    );
    let host = host_api::router(svc);
    let router = svc_streaming::egress::whep::router(whep);

    // Another community cannot mint a viewer token for community 7's stream.
    let (st, _) = host_call(
        &host,
        "pipeline.whep_token",
        json!({"community_id":"8","pipeline_id":pipeline}),
    )
    .await;
    assert_eq!(st, StatusCode::NOT_FOUND);
    let (st, _) = host_call(
        &host,
        "pipeline.whep_token",
        json!({"community_id":"7","pipeline_id":Uuid::new_v4()}),
    )
    .await;
    assert_eq!(st, StatusCode::NOT_FOUND);
    let (st, _) = host_call(
        &host,
        "pipeline.whep_token",
        json!({"community_id":"7","pipeline_id":pipeline,"ttl_secs":99999}),
    )
    .await;
    assert_eq!(st, StatusCode::BAD_REQUEST);

    let (st, t) = host_call(
        &host,
        "pipeline.whep_token",
        json!({"community_id":"7","pipeline_id":pipeline}),
    )
    .await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(t["url"], format!("http://svc.test/whep/7/{pipeline}"));
    // The minted token passes WHEP authentication (an unparsable offer is then a
    // 400 from SDP parsing -- NOT 401/403), and is refused for another community.
    let tok = t["token"].as_str().unwrap();
    let res = router
        .clone()
        .oneshot(post_sdp_auth(
            &format!("/whep/7/{pipeline}"),
            tok,
            "v=0\r\n",
        ))
        .await
        .unwrap();
    assert_eq!(res.status(), StatusCode::BAD_REQUEST);
    let res = router
        .oneshot(post_sdp_auth(
            &format!("/whep/8/{pipeline}"),
            tok,
            "v=0\r\n",
        ))
        .await
        .unwrap();
    assert_eq!(res.status(), StatusCode::FORBIDDEN);
}
