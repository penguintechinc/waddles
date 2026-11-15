//! Real-path tests for idle-room expiry (parity item C03, the Go-era 300 s
//! `EmptyTimeout`) and `Participant.joined_at`: rooms are created over mTLS gRPC with real
//! ES256 JWTs; expiry decisions run on a CONTROLLED clock (`expire_idle_at`, no sleeping),
//! a real WHIP publisher proves an occupied room is never reaped, and one test lets the
//! spawned background reaper run on the wall clock to prove it is wired up.

mod rooms_common;
mod rtc_common;

use std::sync::Arc;
use std::time::Duration;

use axum::http::StatusCode;
use svc_streaming::rooms::grpc::pb::*;
use svc_streaming::rooms::grpc_auth::{SCOPE_READ, SCOPE_WRITE};
use svc_streaming::rooms::media;
use svc_streaming::rtc::pc_factory::PeerConnectionFactory;
use tonic::Code;
use webrtc::media_stream::track_local::TrackLocal;

use rooms_common::*;
use rtc_common::*;

mod log_capture;
use log_capture::LogCapture;

fn counter(h: &Harness, name: &str) -> f64 {
    h.registry
        .gather()
        .iter()
        .filter(|f| f.name() == name)
        .flat_map(|f| f.get_metric())
        .map(|m| m.get_counter().value())
        .sum()
}

async fn room_exists(h: &mut Harness, community: &str, room: &str) -> bool {
    let read = jwt(&h.signing_pem, SCOPE_READ, |_| {});
    match h
        .client
        .get_room(req(
            GetRoomRequest {
                api_version: "v1".into(),
                community_id: community.into(),
                room_id: room.into(),
            },
            &read,
        ))
        .await
    {
        Ok(_) => true,
        Err(e) => {
            assert_eq!(e.code(), Code::NotFound, "{}", e.message());
            false
        }
    }
}

#[tokio::test]
async fn empty_room_is_reaped_at_its_timeout_with_hands_locks_and_an_info_log() {
    let log = LogCapture::install();
    let mut h = harness(42540, true).await;
    let write = jwt(&h.signing_pem, SCOPE_WRITE, |_| {});
    let moderate = jwt(
        &h.signing_pem,
        svc_streaming::rooms::grpc_auth::SCOPE_MODERATE,
        |_| {},
    );
    // A label that looks like a person's name must never reach telemetry.
    let room = h
        .client
        .create_room(req(
            CreateRoomRequest {
                api_version: "v1".into(),
                community_id: "7771".into(),
                label: Some("Alice Smith private huddle".into()),
                empty_timeout_secs: Some(300),
                ..Default::default()
            },
            &write,
        ))
        .await
        .unwrap()
        .into_inner()
        .room
        .unwrap();
    assert_eq!(room.empty_timeout_secs, 300);
    let t0 = room.created_at;

    // State that must be cleaned up with the room: a raised hand and a lock.
    let hand_owner = uid();
    h.client
        .join_room(req(
            JoinRoomRequest {
                api_version: "v1".into(),
                community_id: "7771".into(),
                room_id: room.room_id.clone(),
                participant_id: hand_owner.clone(),
                publish: false,
                subscribe: true,
                ttl_secs: None,
            },
            &write,
        ))
        .await
        .unwrap();
    h.client
        .raise_hand(req(
            RaiseHandRequest {
                api_version: "v1".into(),
                community_id: "7771".into(),
                room_id: room.room_id.clone(),
                participant_id: hand_owner.clone(),
                actor_id: hand_owner.clone(),
            },
            &write,
        ))
        .await
        .unwrap();
    let (actor, role) = admin();
    h.client
        .lock_room(req(
            LockRoomRequest {
                api_version: "v1".into(),
                community_id: "7771".into(),
                room_id: room.room_id.clone(),
                actor_id: actor,
                actor_role: role,
            },
            &moderate,
        ))
        .await
        .unwrap();

    // Controlled clock: one second early keeps it, the timeout takes it.
    assert_eq!(h.svc.expire_idle_at(t0 + 299).await, 0);
    assert!(room_exists(&mut h, "7771", &room.room_id).await);
    assert_eq!(counter(&h, "rooms_expired_total"), 0.0);
    assert_eq!(h.svc.expire_idle_at(t0 + 300).await, 1);
    assert!(!room_exists(&mut h, "7771", &room.room_id).await);
    assert_eq!(counter(&h, "rooms_expired_total"), 1.0);

    // Hands (and the lock) went with the room: NOT_FOUND, not an empty list.
    let read = jwt(&h.signing_pem, SCOPE_READ, |_| {});
    let e = h
        .client
        .list_hands(req(
            ListHandsRequest {
                api_version: "v1".into(),
                community_id: "7771".into(),
                room_id: room.room_id.clone(),
                actor_id: hand_owner.clone(),
            },
            &read,
        ))
        .await
        .unwrap_err();
    assert_eq!(e.code(), Code::NotFound);
    let hands_cleared = h
        .registry
        .gather()
        .iter()
        .filter(|f| f.name() == "rooms_hands_cleared_total")
        .flat_map(|f| f.get_metric())
        .filter(|m| m.get_label().iter().any(|l| l.value() == "expiry"))
        .map(|m| m.get_counter().value())
        .sum::<f64>();
    assert_eq!(hands_cleared, 1.0);

    // INFO log with numeric ids, and no name-like text anywhere.
    let line = log
        .text()
        .lines()
        .find(|l| l.contains("expired idle room") && l.contains(&room.room_id))
        .map(str::to_string)
        .expect("an INFO line for the expired room");
    assert!(line.contains("INFO"), "{line}");
    assert!(line.contains("community_id=7771"), "{line}");
    assert!(line.contains("empty_timeout_secs=300"), "{line}");
    assert!(
        !log.text().contains("Alice Smith"),
        "room labels must never be logged"
    );
}

#[tokio::test]
async fn a_room_with_a_live_session_never_expires_and_reports_joined_at() {
    let mut h = harness(42560, true).await;
    let write = jwt(&h.signing_pem, SCOPE_WRITE, |_| {});
    let read = jwt(&h.signing_pem, SCOPE_READ, |_| {});
    let room = create_room(&mut h, "9", Some(60)).await;
    let participant = uid();

    let before = now();
    let joined = h
        .client
        .join_room(req(
            JoinRoomRequest {
                api_version: "v1".into(),
                community_id: "9".into(),
                room_id: room.room_id.clone(),
                participant_id: participant.clone(),
                publish: true,
                subscribe: false,
                ttl_secs: Some(600),
            },
            &write,
        ))
        .await
        .unwrap()
        .into_inner();

    // A real WHIP publisher over loopback.
    let (track, _ssrc) = opus_publisher_track();
    let handler = TestPeerHandler::new();
    let client_factory = PeerConnectionFactory::new(loopback_rtc_config((42580, 42589))).unwrap();
    let pc = client_factory.build(Arc::clone(&handler)).await.unwrap();
    pc.add_track(track as Arc<dyn TrackLocal>).await.unwrap();
    let offer = offer_sdp(&pc, &handler.gather_complete).await;
    let res = oneshot(
        media::router(h.svc.clone()),
        post_sdp_auth(
            &format!("/rtc/rooms/{}/whip", room.room_id),
            &joined.token,
            offer,
        ),
    )
    .await;
    assert_status(&res, StatusCode::CREATED);
    apply_answer(&pc, body_text(res).await).await;
    tokio::time::timeout(Duration::from_secs(10), handler.connected.notified())
        .await
        .expect("publisher connected");
    let after = now();

    // joined_at is the session's first-open time, as unix seconds.
    let participants = h
        .client
        .list_participants(req(
            ListParticipantsRequest {
                api_version: "v1".into(),
                community_id: "9".into(),
                room_id: room.room_id.clone(),
            },
            &read,
        ))
        .await
        .unwrap()
        .into_inner()
        .participants;
    assert_eq!(participants.len(), 1);
    assert_eq!(participants[0].participant_id, participant);
    let joined_at = participants[0]
        .joined_at
        .expect("joined_at populated at join");
    assert!(
        (before..=after).contains(&joined_at),
        "joined_at {joined_at} not within [{before}, {after}]"
    );

    // Occupied: however far the controlled clock jumps, the room stays.
    assert_eq!(h.svc.expire_idle_at(room.created_at + 1_000_000).await, 0);
    assert!(room_exists(&mut h, "9", &room.room_id).await);

    // The participant leaves (closing the live session): now the room is empty.
    let left = h
        .client
        .leave_room(req(
            LeaveRoomRequest {
                api_version: "v1".into(),
                community_id: "9".into(),
                room_id: room.room_id.clone(),
                participant_id: participant.clone(),
                actor_id: participant.clone(),
                actor_role: RoomRole::Member as i32,
            },
            &write,
        ))
        .await
        .unwrap()
        .into_inner();
    assert!(left.sessions_closed >= 1);
    let participants = h
        .client
        .list_participants(req(
            ListParticipantsRequest {
                api_version: "v1".into(),
                community_id: "9".into(),
                room_id: room.room_id.clone(),
            },
            &read,
        ))
        .await
        .unwrap()
        .into_inner()
        .participants;
    assert!(
        participants.is_empty(),
        "a participant with no session is not listed"
    );

    // The idle clock restarts when the room is first seen empty, then runs its 60 s.
    let t = room.created_at + 2_000_000;
    assert_eq!(
        h.svc.expire_idle_at(t).await,
        0,
        "first sighting starts the clock"
    );
    assert_eq!(h.svc.expire_idle_at(t + 59).await, 0);
    assert!(room_exists(&mut h, "9", &room.room_id).await);
    assert_eq!(h.svc.expire_idle_at(t + 60).await, 1);
    assert!(!room_exists(&mut h, "9", &room.room_id).await);
    let _ = pc.close().await;
}

#[tokio::test]
async fn the_background_reaper_is_wired_and_expires_an_idle_room() {
    // The one wall-clock test: proves `spawn_sweeper` actually drives expiry. The decision
    // logic itself is covered on a controlled clock above.
    let mut h = harness(42600, true).await;
    h.svc.spawn_sweeper(Duration::from_millis(50));
    let room = create_room(&mut h, "11", Some(1)).await;
    assert_eq!(room.empty_timeout_secs, 1);
    let deadline = std::time::Instant::now() + Duration::from_secs(60);
    loop {
        if !room_exists(&mut h, "11", &room.room_id).await {
            break;
        }
        assert!(
            std::time::Instant::now() < deadline,
            "idle room was not reaped within 60 s"
        );
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert_eq!(counter(&h, "rooms_expired_total"), 1.0);
}

#[tokio::test]
async fn never_expire_rooms_survive_and_the_default_is_the_go_era_300_seconds() {
    let mut h = harness(42620, true).await;
    let default = create_room(&mut h, "13", None).await;
    let never = create_room(&mut h, "13", Some(0)).await;
    assert_eq!(
        default.empty_timeout_secs, 300,
        "Go-era EmptyTimeout default"
    );
    assert_eq!(never.empty_timeout_secs, 0);
    let far = default.created_at + 10_000_000;
    assert_eq!(
        h.svc.expire_idle_at(far).await,
        1,
        "only the default-timeout room"
    );
    assert!(!room_exists(&mut h, "13", &default.room_id).await);
    assert!(room_exists(&mut h, "13", &never.room_id).await);
    assert_eq!(h.svc.expire_idle_at(far * 2).await, 0);
}
