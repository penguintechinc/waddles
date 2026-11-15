//! Room facts (kind / closed / schedule / guests) set over gRPC by a write-scoped service
//! caller and enforced server-side on `JoinRoom`; staff bypass for locked rooms is verified
//! (moderate-scope service JWT) and never claimable from a request.

mod rooms_common;

use rooms_common::*;
use svc_streaming::rooms::grpc::pb::*;
use svc_streaming::rooms::grpc_auth::{SCOPE_MODERATE, SCOPE_READ, SCOPE_WRITE};
use tonic::Code;

const API: &str = "v1";

struct Tok {
    read: String,
    write: String,
    moderate: String,
}

fn tokens(h: &Harness) -> Tok {
    Tok {
        read: jwt(&h.signing_pem, SCOPE_READ, |_| {}),
        write: jwt(&h.signing_pem, SCOPE_WRITE, |_| {}),
        moderate: jwt(&h.signing_pem, SCOPE_MODERATE, |_| {}),
    }
}

fn facts_create(kind: RoomKind) -> CreateRoomRequest {
    CreateRoomRequest {
        api_version: API.into(),
        community_id: "7".into(),
        kind: kind as i32,
        ..Default::default()
    }
}

async fn create(h: &mut Harness, t: &Tok, r: CreateRoomRequest) -> Room {
    h.client
        .create_room(req(r, &t.write))
        .await
        .unwrap()
        .into_inner()
        .room
        .unwrap()
}

async fn update(
    h: &mut Harness,
    token: &str,
    room: &str,
    f: impl FnOnce(&mut UpdateRoomRequest),
) -> Result<Room, tonic::Status> {
    let mut r = UpdateRoomRequest {
        api_version: API.into(),
        community_id: "7".into(),
        room_id: room.into(),
        ..Default::default()
    };
    f(&mut r);
    h.client
        .update_room(req(r, token))
        .await
        .map(|x| x.into_inner().room.unwrap())
}

#[allow(clippy::too_many_arguments)]
async fn join(
    h: &mut Harness,
    token: &str,
    room: &str,
    who: &str,
    publish: bool,
    guest: bool,
) -> Result<JoinRoomResponse, tonic::Status> {
    h.client
        .join_room(req(
            JoinRoomRequest {
                api_version: API.into(),
                community_id: "7".into(),
                room_id: room.into(),
                participant_id: who.into(),
                publish,
                subscribe: true,
                guest,
                ..Default::default()
            },
            token,
        ))
        .await
        .map(|r| r.into_inner())
}

fn denied(r: Result<JoinRoomResponse, tonic::Status>) -> String {
    let e = r.expect_err("join must be refused");
    assert_eq!(e.code(), Code::PermissionDenied, "{e}");
    e.message().to_string()
}

#[tokio::test]
async fn facts_round_trip_create_get_update() {
    let mut h = harness(44100, true).await;
    let t = tokens(&h);
    let now = now();
    let room = create(
        &mut h,
        &t,
        CreateRoomRequest {
            starts_at: Some(now - 10),
            ends_at: Some(now + 3600),
            allow_guests: true,
            ..facts_create(RoomKind::Stage)
        },
    )
    .await;
    assert_eq!(room.kind, RoomKind::Stage as i32);
    assert!(!room.closed && room.allow_guests);
    assert_eq!(room.starts_at, Some(now - 10));
    assert_eq!(room.ends_at, Some(now + 3600));

    // Unspecified kind defaults to COMMUNITY, guests default OFF, no window.
    let plain = create(&mut h, &t, facts_create(RoomKind::Unspecified)).await;
    assert_eq!(plain.kind, RoomKind::Community as i32);
    assert!(!plain.allow_guests && plain.starts_at.is_none() && plain.ends_at.is_none());

    let up = update(&mut h, &t.write, &room.room_id, |r| {
        r.kind = RoomKind::GreenRoom as i32;
        r.closed = true;
    })
    .await
    .unwrap();
    assert_eq!(up.kind, RoomKind::GreenRoom as i32);
    assert!(up.closed);
    assert!(
        up.starts_at.is_none() && !up.allow_guests,
        "update replaces the full fact set"
    );
    let got = h
        .client
        .get_room(req(
            GetRoomRequest {
                api_version: API.into(),
                community_id: "7".into(),
                room_id: room.room_id.clone(),
            },
            &t.read,
        ))
        .await
        .unwrap()
        .into_inner()
        .room
        .unwrap();
    assert_eq!(got.kind, RoomKind::GreenRoom as i32);
    assert!(got.closed);
}

#[tokio::test]
async fn facts_are_validated_and_scope_gated() {
    let mut h = harness(44120, true).await;
    let t = tokens(&h);
    let room = create(&mut h, &t, facts_create(RoomKind::Community)).await;

    // Closed set: an unknown kind is refused, never coerced.
    let e = h
        .client
        .create_room(req(
            CreateRoomRequest {
                kind: 99,
                ..facts_create(RoomKind::Community)
            },
            &t.write,
        ))
        .await
        .unwrap_err();
    assert_eq!(e.code(), Code::InvalidArgument);
    let e = update(&mut h, &t.write, &room.room_id, |r| r.kind = 42)
        .await
        .unwrap_err();
    assert_eq!(e.code(), Code::InvalidArgument);

    // Window must be well-formed.
    let e = update(&mut h, &t.write, &room.room_id, |r| {
        r.starts_at = Some(200);
        r.ends_at = Some(100);
    })
    .await
    .unwrap_err();
    assert_eq!(e.code(), Code::InvalidArgument);
    let e = update(&mut h, &t.write, &room.room_id, |r| r.starts_at = Some(-1))
        .await
        .unwrap_err();
    assert_eq!(e.code(), Code::InvalidArgument);

    // Read scope cannot write facts; missing room / wrong community is not found.
    let e = update(&mut h, &t.read, &room.room_id, |_| {})
        .await
        .unwrap_err();
    assert_eq!(e.code(), Code::PermissionDenied);
    let e = update(&mut h, &t.write, &uid(), |_| {}).await.unwrap_err();
    assert_eq!(e.code(), Code::NotFound);
    let e = h
        .client
        .update_room(req(
            UpdateRoomRequest {
                api_version: API.into(),
                community_id: "8".into(),
                room_id: room.room_id.clone(),
                ..Default::default()
            },
            &t.write,
        ))
        .await
        .unwrap_err();
    assert_eq!(e.code(), Code::NotFound);
    let e = h
        .client
        .update_room(req(
            UpdateRoomRequest {
                api_version: "v2".into(),
                community_id: "7".into(),
                room_id: room.room_id,
                ..Default::default()
            },
            &t.write,
        ))
        .await
        .unwrap_err();
    assert_eq!(e.code(), Code::Unimplemented);
}

#[tokio::test]
async fn stage_is_subscribe_only_for_non_staff() {
    let mut h = harness(44140, true).await;
    let t = tokens(&h);
    let room = create(&mut h, &t, facts_create(RoomKind::Stage)).await;
    let (viewer, host) = (uid(), uid());
    assert!(
        denied(join(&mut h, &t.write, &room.room_id, &viewer, true, false).await)
            .contains("subscribe-only")
    );
    join(&mut h, &t.write, &room.room_id, &viewer, false, false)
        .await
        .unwrap();
    // Staff (verified moderate scope) may publish on the stage.
    join(&mut h, &t.moderate, &room.room_id, &host, true, false)
        .await
        .unwrap();
}

#[tokio::test]
async fn green_room_is_staff_only() {
    let mut h = harness(44160, true).await;
    let t = tokens(&h);
    let room = create(&mut h, &t, facts_create(RoomKind::GreenRoom)).await;
    let who = uid();
    assert!(
        denied(join(&mut h, &t.write, &room.room_id, &who, false, false).await)
            .contains("green room")
    );
    join(&mut h, &t.moderate, &room.room_id, &who, true, false)
        .await
        .unwrap();
}

#[tokio::test]
async fn closed_room_refuses_everyone_including_staff_until_reopened() {
    let mut h = harness(44180, true).await;
    let t = tokens(&h);
    let room = create(&mut h, &t, facts_create(RoomKind::Community)).await;
    let who = uid();
    join(&mut h, &t.write, &room.room_id, &who, false, false)
        .await
        .unwrap();
    update(&mut h, &t.write, &room.room_id, |r| {
        r.kind = RoomKind::Community as i32;
        r.closed = true;
    })
    .await
    .unwrap();
    assert!(
        denied(join(&mut h, &t.write, &room.room_id, &uid(), false, false).await)
            .contains("closed")
    );
    assert!(
        denied(join(&mut h, &t.moderate, &room.room_id, &uid(), false, false).await)
            .contains("closed")
    );
    update(&mut h, &t.write, &room.room_id, |r| {
        r.kind = RoomKind::Community as i32;
    })
    .await
    .unwrap();
    join(&mut h, &t.write, &room.room_id, &uid(), false, false)
        .await
        .unwrap();
}

#[tokio::test]
async fn schedule_window_refuses_non_staff_outside_it() {
    let mut h = harness(44200, true).await;
    let t = tokens(&h);
    let n = now();
    let future = create(
        &mut h,
        &t,
        CreateRoomRequest {
            starts_at: Some(n + 3600),
            ends_at: Some(n + 7200),
            ..facts_create(RoomKind::Community)
        },
    )
    .await;
    let past = create(
        &mut h,
        &t,
        CreateRoomRequest {
            starts_at: Some(n - 7200),
            ends_at: Some(n - 3600),
            ..facts_create(RoomKind::Community)
        },
    )
    .await;
    let open_ended = create(
        &mut h,
        &t,
        CreateRoomRequest {
            starts_at: Some(n - 60),
            ..facts_create(RoomKind::Community)
        },
    )
    .await;
    for r in [&future, &past] {
        assert!(
            denied(join(&mut h, &t.write, &r.room_id, &uid(), false, false).await)
                .contains("window")
        );
        // Staff may enter outside the window (set up before the start / wrap up after).
        join(&mut h, &t.moderate, &r.room_id, &uid(), false, false)
            .await
            .unwrap();
    }
    join(&mut h, &t.write, &open_ended.room_id, &uid(), false, false)
        .await
        .unwrap();
}

#[tokio::test]
async fn guests_follow_allow_guests_and_are_subscribe_only() {
    let mut h = harness(44220, true).await;
    let t = tokens(&h);
    let closed_to_guests = create(&mut h, &t, facts_create(RoomKind::Community)).await;
    let open_to_guests = create(
        &mut h,
        &t,
        CreateRoomRequest {
            allow_guests: true,
            ..facts_create(RoomKind::Community)
        },
    )
    .await;
    assert!(denied(
        join(
            &mut h,
            &t.write,
            &closed_to_guests.room_id,
            &uid(),
            false,
            true
        )
        .await
    )
    .contains("guests are not allowed"));
    assert!(denied(
        join(
            &mut h,
            &t.write,
            &open_to_guests.room_id,
            &uid(),
            true,
            true
        )
        .await
    )
    .contains("subscribe-only"));
    join(
        &mut h,
        &t.write,
        &open_to_guests.room_id,
        &uid(),
        false,
        true,
    )
    .await
    .unwrap();
    // A non-guest member is unaffected by allow_guests=false.
    join(
        &mut h,
        &t.write,
        &closed_to_guests.room_id,
        &uid(),
        true,
        false,
    )
    .await
    .unwrap();
}

async fn lock(h: &mut Harness, t: &Tok, room: &str) {
    let (actor, role) = admin();
    h.client
        .lock_room(req(
            LockRoomRequest {
                api_version: API.into(),
                community_id: "7".into(),
                room_id: room.into(),
                actor_id: actor,
                actor_role: role,
            },
            &t.moderate,
        ))
        .await
        .unwrap();
}

#[tokio::test]
async fn staff_bypasses_a_locked_room_only_with_verified_moderate_scope() {
    let mut h = harness(44240, true).await;
    let t = tokens(&h);
    let room = create(&mut h, &t, facts_create(RoomKind::Community)).await;
    lock(&mut h, &t, &room.room_id).await;

    // Ordinary (write-scope) joiner: refused.
    let member = uid();
    assert!(
        denied(join(&mut h, &t.write, &room.room_id, &member, false, false).await)
            .contains("locked")
    );
    // Read scope cannot join at all.
    let e = join(&mut h, &t.read, &room.room_id, &member, false, false)
        .await
        .unwrap_err();
    assert_eq!(e.code(), Code::PermissionDenied);

    // Verified moderate scope: staff bypasses the lock...
    let staff = uid();
    join(&mut h, &t.moderate, &room.room_id, &staff, false, false)
        .await
        .unwrap();
    // ...and is now staff in the room ACL (visible as a participant record), but the
    // bypass is per call: the SAME participant with a plain write-scope join is still refused
    // (a request cannot claim staff, and past staff status is not a token-time credential).
    assert!(
        denied(join(&mut h, &t.write, &room.room_id, &staff, false, false).await)
            .contains("locked")
    );

    // A moderate-scope token for a different community-bound claim cannot bypass either.
    let other = jwt(&h.signing_pem, SCOPE_MODERATE, |c| {
        c["community"] = 8.into()
    });
    let e = join(&mut h, &other, &room.room_id, &uid(), false, false)
        .await
        .unwrap_err();
    assert_eq!(e.code(), Code::NotFound);

    // Facts still apply to staff: a closed room refuses even the bypass.
    update(&mut h, &t.write, &room.room_id, |r| {
        r.kind = RoomKind::Community as i32;
        r.closed = true;
    })
    .await
    .unwrap();
    assert!(
        denied(join(&mut h, &t.moderate, &room.room_id, &uid(), false, false).await)
            .contains("closed")
    );
}
