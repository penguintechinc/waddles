//! Real-path tests for the raised-hand RPCs (`RaiseHand`, `LowerHand`, `ListHands`,
//! `AcknowledgeHand`, `ResetHands`): the tonic client drives the production mTLS accept
//! loop with real ES256 machine JWTs; the state under test is the live `RoomService`.
//!
//! Authorization model under test: NO caller-stated role is ever read. Authority is the
//! VERIFIED service-JWT scope plus the server-side member table (a participant is a member
//! once issued a join token; staff only when that join carried the moderate scope).
//! Covers the happy path, idempotency, ordering, negative auth for every hand RPC
//! (member-asserted moderator role ignored, mismatched actor, non-participant, wrong
//! scope/token/api_version/ids), the moderation flag, cross-community isolation
//! (regression) and cleanup on leave/kick/delete.

mod rooms_common;

use svc_streaming::rooms::grpc::pb::*;
use svc_streaming::rooms::grpc_auth::{SCOPE_MODERATE, SCOPE_READ, SCOPE_WRITE};
use tonic::{Code, Request};

use rooms_common::*;

const API: &str = "v1";

struct Tokens {
    read: String,
    write: String,
    moderate: String,
}

fn tokens(h: &Harness) -> Tokens {
    Tokens {
        read: jwt(&h.signing_pem, SCOPE_READ, |_| {}),
        write: jwt(&h.signing_pem, SCOPE_WRITE, |_| {}),
        moderate: jwt(&h.signing_pem, SCOPE_MODERATE, |_| {}),
    }
}

/// Joins `who` to `room`. `staff` joins with the moderate-scoped service JWT (the only way to
/// register staff); everyone else joins with the ordinary write scope.
async fn join(h: &mut Harness, t: &Tokens, community: &str, room: &str, who: &str, staff: bool) {
    let token = if staff { &t.moderate } else { &t.write };
    h.client
        .join_room(req(
            JoinRoomRequest {
                api_version: API.into(),
                community_id: community.into(),
                room_id: room.into(),
                participant_id: who.into(),
                publish: false,
                subscribe: true,
                ttl_secs: None,
                ..Default::default()
            },
            token,
        ))
        .await
        .unwrap();
}

fn raise(community: &str, room: &str, who: &str, actor: &str) -> RaiseHandRequest {
    RaiseHandRequest {
        api_version: API.into(),
        community_id: community.into(),
        room_id: room.into(),
        participant_id: who.into(),
        actor_id: actor.into(),
    }
}

/// `actor_role` is set to ADMIN on purpose in every request below that carries one: the
/// server must ignore it.
fn lower(community: &str, room: &str, who: &str, actor: &str) -> LowerHandRequest {
    LowerHandRequest {
        api_version: API.into(),
        community_id: community.into(),
        room_id: room.into(),
        participant_id: who.into(),
        actor_id: actor.into(),
        actor_role: RoomRole::Admin as i32,
    }
}

fn list(community: &str, room: &str, actor: &str) -> ListHandsRequest {
    ListHandsRequest {
        api_version: API.into(),
        community_id: community.into(),
        room_id: room.into(),
        actor_id: actor.into(),
    }
}

fn ack(community: &str, room: &str, who: &str, actor: &str) -> AcknowledgeHandRequest {
    AcknowledgeHandRequest {
        api_version: API.into(),
        community_id: community.into(),
        room_id: room.into(),
        participant_id: who.into(),
        actor_id: actor.into(),
        actor_role: RoomRole::Admin as i32,
    }
}

fn reset(community: &str, room: &str, actor: &str) -> ResetHandsRequest {
    ResetHandsRequest {
        api_version: API.into(),
        community_id: community.into(),
        room_id: room.into(),
        actor_id: actor.into(),
        actor_role: RoomRole::Admin as i32,
    }
}

async fn all_hands(
    h: &mut Harness,
    t: &Tokens,
    community: &str,
    room: &str,
    staff: &str,
) -> ListHandsResponse {
    h.client
        .list_hands(req(list(community, room, staff), &t.moderate))
        .await
        .unwrap()
        .into_inner()
}

fn ids(hands: &[RaisedHand]) -> Vec<String> {
    hands.iter().map(|h| h.participant_id.clone()).collect()
}

fn counter(h: &Harness, name: &str, label: Option<(&str, &str)>) -> f64 {
    h.registry
        .gather()
        .iter()
        .filter(|f| f.name() == name)
        .flat_map(|f| f.get_metric())
        .filter(|m| {
            label.is_none_or(|(k, v)| {
                m.get_label()
                    .iter()
                    .any(|l| l.name() == k && l.value() == v)
            })
        })
        .map(|m| m.get_counter().value())
        .sum()
}

#[tokio::test]
async fn hands_end_to_end_over_mtls_with_service_jwt() {
    let mut h = harness(42400, true).await;
    let t = tokens(&h);
    let room = create_room(&mut h, "7", None).await.room_id;
    let (a, b, c, m) = (uid(), uid(), uid(), uid());
    for p in [&a, &b, &c] {
        join(&mut h, &t, "7", &room, p, false).await;
    }
    join(&mut h, &t, "7", &room, &m, true).await;
    assert!(all_hands(&mut h, &t, "7", &room, &m).await.hands.is_empty());

    // Raise a, b (each acting as themselves); re-raising a keeps its position AND time.
    let first = h
        .client
        .raise_hand(req(raise("7", &room, &a, &a), &t.write))
        .await
        .unwrap()
        .into_inner()
        .hand
        .unwrap();
    h.client
        .raise_hand(req(raise("7", &room, &b, &b), &t.write))
        .await
        .unwrap();
    let again = h
        .client
        .raise_hand(req(raise("7", &room, &a, &a), &t.write))
        .await
        .unwrap()
        .into_inner()
        .hand
        .unwrap();
    assert_eq!(first, again, "raise twice = no-op");
    assert_eq!(first.participant_id, a);
    assert!(
        first.raised_at > 0 && first.acknowledged_at.is_none() && first.acknowledged_by.is_none()
    );

    // Staff + moderate scope: the whole queue, FIFO, with the full count.
    let queue = all_hands(&mut h, &t, "7", &room, &m).await;
    assert_eq!(ids(&queue.hands), vec![a.clone(), b.clone()]);
    assert!(queue.hands[0].raised_at <= queue.hands[1].raised_at);
    assert_eq!((queue.count, queue.position), (2, None));

    // A member with the read scope sees only their own entry, the queue length and position;
    // a member who has not raised sees no entry but still the length.
    let mine = h
        .client
        .list_hands(req(list("7", &room, &b), &t.read))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(ids(&mine.hands), vec![b.clone()]);
    assert_eq!((mine.count, mine.position), (2, Some(2)));
    let none = h
        .client
        .list_hands(req(list("7", &room, &c), &t.read))
        .await
        .unwrap()
        .into_inner();
    assert!(none.hands.is_empty());
    assert_eq!((none.count, none.position), (2, None));
    // Even recorded staff with only the READ scope get the own view (least privilege).
    let staff_read = h
        .client
        .list_hands(req(list("7", &room, &m), &t.read))
        .await
        .unwrap()
        .into_inner();
    assert!(staff_read.hands.is_empty() && staff_read.count == 2);

    // Acknowledge b (moderate scope + staff actor): metadata shows in the list.
    let acked = h
        .client
        .acknowledge_hand(req(ack("7", &room, &b, &m), &t.moderate))
        .await
        .unwrap()
        .into_inner()
        .hand
        .unwrap();
    assert_eq!(acked.participant_id, b);
    assert_eq!(acked.acknowledged_by.as_deref(), Some(m.as_str()));
    assert!(acked.acknowledged_at.is_some());
    let queue = all_hands(&mut h, &t, "7", &room, &m).await;
    assert_eq!(queue.hands[1], acked);
    assert!(queue.hands[0].acknowledged_at.is_none());
    // A second acknowledge keeps the first.
    let second = h
        .client
        .acknowledge_hand(req(ack("7", &room, &b, &m), &t.moderate))
        .await
        .unwrap()
        .into_inner()
        .hand
        .unwrap();
    assert_eq!(second, acked);

    // Lower: own hand (write scope), no-op when not raised, and staff may lower another's.
    let lowered = h
        .client
        .lower_hand(req(lower("7", &room, &a, &a), &t.write))
        .await
        .unwrap()
        .into_inner();
    assert!(lowered.lowered);
    assert_eq!(lowered.participant_id, a);
    let noop = h
        .client
        .lower_hand(req(lower("7", &room, &a, &a), &t.write))
        .await
        .unwrap()
        .into_inner();
    assert!(
        !noop.lowered,
        "lowering a hand that is not raised is a no-op, not an error"
    );
    h.client
        .raise_hand(req(raise("7", &room, &c, &c), &t.write))
        .await
        .unwrap();
    let by_staff = h
        .client
        .lower_hand(req(lower("7", &room, &c, &m), &t.moderate))
        .await
        .unwrap()
        .into_inner();
    assert!(
        by_staff.lowered,
        "staff + moderate scope may lower another's hand"
    );
    assert_eq!(
        ids(&all_hands(&mut h, &t, "7", &room, &m).await.hands),
        vec![b.clone()]
    );

    // Reset (moderate + staff): clears the rest and reports how many; second reset clears 0.
    let cleared = h
        .client
        .reset_hands(req(reset("7", &room, &m), &t.moderate))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(
        (cleared.room_id.as_str(), cleared.cleared),
        (room.as_str(), 1)
    );
    assert!(all_hands(&mut h, &t, "7", &room, &m).await.hands.is_empty());
    let zero = h
        .client
        .reset_hands(req(reset("7", &room, &m), &t.moderate))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(zero.cleared, 0);

    // Telemetry: counters moved by exactly what happened, per-RPC series incl. the new RPCs.
    assert_eq!(
        counter(&h, "rooms_hands_raised_total", None),
        3.0,
        "a, b, c (re-raise not counted)"
    );
    assert_eq!(counter(&h, "rooms_hands_acknowledged_total", None), 1.0);
    assert_eq!(
        counter(&h, "rooms_hands_cleared_total", Some(("reason", "lower"))),
        2.0
    );
    assert_eq!(
        counter(&h, "rooms_hands_cleared_total", Some(("reason", "reset"))),
        1.0
    );
    for rpc in [
        "RaiseHand",
        "LowerHand",
        "ListHands",
        "AcknowledgeHand",
        "ResetHands",
        "JoinRoom",
    ] {
        assert!(
            counter(&h, "rooms_grpc_requests_total", Some(("rpc", rpc))) >= 1.0,
            "{rpc} counted"
        );
    }
    let families = h.registry.gather();
    for histogram in ["rooms_grpc_request_seconds", "rooms_hand_ack_wait_seconds"] {
        let count: u64 = families
            .iter()
            .filter(|f| f.name() == histogram)
            .flat_map(|f| f.get_metric())
            .map(|m| m.get_histogram().get_sample_count())
            .sum();
        assert!(count >= 1, "{histogram} recorded {count} observations");
    }
    let reset_latency = families
        .iter()
        .filter(|f| f.name() == "rooms_grpc_request_seconds")
        .flat_map(|f| f.get_metric())
        .filter(|m| {
            m.get_label()
                .iter()
                .any(|l| l.name() == "rpc" && l.value() == "ResetHands")
        })
        .map(|m| m.get_histogram().get_sample_count())
        .sum::<u64>();
    assert_eq!(
        reset_latency, 2,
        "ResetHands latency histogram has both calls"
    );
}

macro_rules! code {
    ($call:expr, $code:expr, $why:expr) => {{
        let e = $call.await.unwrap_err();
        assert_eq!(e.code(), $code, "{}: {}", $why, e.message());
    }};
}

#[tokio::test]
async fn hand_rpcs_enforce_scope() {
    let mut h = harness(42420, true).await;
    let t = tokens(&h);
    let room = create_room(&mut h, "7", None).await.room_id;
    let (a, m) = (uid(), uid());
    join(&mut h, &t, "7", &room, &a, false).await;
    join(&mut h, &t, "7", &room, &m, true).await;
    h.client
        .raise_hand(req(raise("7", &room, &a, &a), &t.write))
        .await
        .unwrap();

    let denied = Code::PermissionDenied;
    code!(
        h.client.raise_hand(req(raise("7", &room, &a, &a), &t.read)),
        denied,
        "RaiseHand/read"
    );
    code!(
        h.client
            .raise_hand(req(raise("7", &room, &a, &a), &t.moderate)),
        denied,
        "RaiseHand/moderate"
    );
    code!(
        h.client.list_hands(req(list("7", &room, &a), &t.write)),
        denied,
        "ListHands/write"
    );
    code!(
        h.client.lower_hand(req(lower("7", &room, &a, &a), &t.read)),
        denied,
        "LowerHand/read"
    );
    code!(
        h.client
            .acknowledge_hand(req(ack("7", &room, &a, &m), &t.write)),
        denied,
        "Ack/write"
    );
    code!(
        h.client
            .acknowledge_hand(req(ack("7", &room, &a, &m), &t.read)),
        denied,
        "Ack/read"
    );
    code!(
        h.client.reset_hands(req(reset("7", &room, &m), &t.write)),
        denied,
        "Reset/write"
    );
    code!(
        h.client.reset_hands(req(reset("7", &room, &m), &t.read)),
        denied,
        "Reset/read"
    );
    // Lowering ANOTHER participant's hand with the write scope is refused even for recorded
    // staff: the verified scope is moderate-or-nothing for that action.
    code!(
        h.client
            .lower_hand(req(lower("7", &room, &a, &m), &t.write)),
        denied,
        "LowerOther/write"
    );
    assert_eq!(
        all_hands(&mut h, &t, "7", &room, &m).await.hands.len(),
        1,
        "queue untouched"
    );
}

/// A member who asserts an admin/moderator role (in `actor_role`) is ignored: the server
/// checks its own member table and the verified scope.
#[tokio::test]
async fn member_asserted_moderator_role_is_ignored() {
    let mut h = harness(42440, true).await;
    let t = tokens(&h);
    let room = create_room(&mut h, "7", None).await.room_id;
    let (a, member, m) = (uid(), uid(), uid());
    join(&mut h, &t, "7", &room, &a, false).await;
    join(&mut h, &t, "7", &room, &member, false).await;
    join(&mut h, &t, "7", &room, &m, true).await;
    h.client
        .raise_hand(req(raise("7", &room, &a, &a), &t.write))
        .await
        .unwrap();

    // Even holding the moderate scope on the call (hub-api minted it for some admin action),
    // an actor who joined as an ordinary member is not staff, whatever role the body claims.
    let denied = Code::PermissionDenied;
    code!(
        h.client
            .acknowledge_hand(req(ack("7", &room, &a, &member), &t.moderate)),
        denied,
        "ack as member"
    );
    code!(
        h.client
            .reset_hands(req(reset("7", &room, &member), &t.moderate)),
        denied,
        "reset as member"
    );
    code!(
        h.client
            .lower_hand(req(lower("7", &room, &a, &member), &t.moderate)),
        denied,
        "lower-other as member"
    );
    code!(
        h.client
            .list_hands(req(list("7", &room, &member), &t.moderate)),
        denied,
        "list-all as member"
    );
    // ...and the same body from a real staff member works, proving only the ACL differs.
    h.client
        .acknowledge_hand(req(ack("7", &room, &a, &m), &t.moderate))
        .await
        .unwrap();
    assert_eq!(all_hands(&mut h, &t, "7", &room, &m).await.hands.len(), 1);
    // A staff participant who also claims a LOWER role is still staff (role field unread).
    let mut r = reset("7", &room, &m);
    r.actor_role = RoomRole::Guest as i32;
    assert_eq!(
        h.client
            .reset_hands(req(r, &t.moderate))
            .await
            .unwrap()
            .into_inner()
            .cleared,
        1
    );
}

#[tokio::test]
async fn mismatched_actor_and_non_participants_are_refused() {
    let mut h = harness(42460, true).await;
    let t = tokens(&h);
    let room = create_room(&mut h, "7", None).await.room_id;
    let (a, b, stranger, m) = (uid(), uid(), uid(), uid());
    join(&mut h, &t, "7", &room, &a, false).await;
    join(&mut h, &t, "7", &room, &b, false).await;
    join(&mut h, &t, "7", &room, &m, true).await;
    let denied = Code::PermissionDenied;
    // b raising a's hand (actor != participant), even with a valid write token.
    code!(
        h.client
            .raise_hand(req(raise("7", &room, &a, &b), &t.write)),
        denied,
        "mismatched actor"
    );
    // Someone who never joined: raise, lower-own, list-own.
    code!(
        h.client
            .raise_hand(req(raise("7", &room, &stranger, &stranger), &t.write)),
        denied,
        "non-participant raise"
    );
    code!(
        h.client
            .lower_hand(req(lower("7", &room, &stranger, &stranger), &t.write)),
        denied,
        "non-participant lower"
    );
    code!(
        h.client
            .list_hands(req(list("7", &room, &stranger), &t.read)),
        denied,
        "non-participant list"
    );
    // A non-participant "moderator" (never registered as staff) is refused on staff actions
    // even with the moderate scope.
    code!(
        h.client
            .reset_hands(req(reset("7", &room, &stranger), &t.moderate)),
        denied,
        "non-participant reset"
    );
    code!(
        h.client
            .acknowledge_hand(req(ack("7", &room, &a, &stranger), &t.moderate)),
        denied,
        "non-participant ack"
    );
    // Nothing was raised by any of that.
    assert!(all_hands(&mut h, &t, "7", &room, &m).await.hands.is_empty());
    assert_eq!(counter(&h, "rooms_hands_raised_total", None), 0.0);
}

#[tokio::test]
async fn bad_tokens_versions_and_ids_are_refused_before_any_effect() {
    let mut h = harness(42480, true).await;
    let t = tokens(&h);
    let room = create_room(&mut h, "7", None).await.room_id;
    let (a, m) = (uid(), uid());
    join(&mut h, &t, "7", &room, &a, false).await;
    join(&mut h, &t, "7", &room, &m, true).await;

    // Missing / malformed / foreign / expired tokens -> UNAUTHENTICATED.
    let foreign = jwt(
        &rcgen::KeyPair::generate().unwrap().serialize_pem(),
        SCOPE_WRITE,
        |_| {},
    );
    let expired = jwt(&h.signing_pem, SCOPE_WRITE, |c| {
        c["iat"] = serde_json::json!(now() - 7200);
        c["nbf"] = serde_json::json!(now() - 7200);
        c["exp"] = serde_json::json!(now() - 3600);
    });
    for (name, token) in [
        ("garbage", "not.a.jwt".to_string()),
        ("foreign key", foreign),
        ("expired", expired),
    ] {
        code!(
            h.client.raise_hand(req(raise("7", &room, &a, &a), &token)),
            Code::Unauthenticated,
            name
        );
    }
    code!(
        h.client.raise_hand(Request::new(raise("7", &room, &a, &a))),
        Code::Unauthenticated,
        "no token"
    );
    code!(
        h.client.reset_hands(Request::new(reset("7", &room, &m))),
        Code::Unauthenticated,
        "no token"
    );

    // api_version: missing/unknown -> UNIMPLEMENTED naming the version.
    for v in ["", "v2"] {
        let mut r = raise("7", &room, &a, &a);
        r.api_version = v.into();
        code!(
            h.client.raise_hand(req(r, &t.write)),
            Code::Unimplemented,
            format!("raise api_version {v:?}")
        );
        let mut r = reset("7", &room, &m);
        r.api_version = v.into();
        code!(
            h.client.reset_hands(req(r, &t.moderate)),
            Code::Unimplemented,
            format!("reset api_version {v:?}")
        );
    }

    // Participant/actor ids are UUIDs, never names.
    let invalid = Code::InvalidArgument;
    for bad in [
        "alice",
        "",
        "not-a-uuid",
        "00000000-0000-0000-0000-00000000000",
    ] {
        code!(
            h.client
                .raise_hand(req(raise("7", &room, bad, &a), &t.write)),
            invalid,
            format!("participant {bad:?}")
        );
        code!(
            h.client
                .raise_hand(req(raise("7", &room, &a, bad), &t.write)),
            invalid,
            format!("actor {bad:?}")
        );
    }
    code!(
        h.client
            .acknowledge_hand(req(ack("7", &room, &a, "moderator-name"), &t.moderate)),
        invalid,
        "ack actor name"
    );
    code!(
        h.client
            .list_hands(req(list("7", "not-a-uuid", &a), &t.read)),
        invalid,
        "room id"
    );
    code!(
        h.client.list_hands(req(list("7", &room, ""), &t.read)),
        invalid,
        "list without actor"
    );
    // Nothing to acknowledge is loud (INVALID_ARGUMENT), not a silent success.
    code!(
        h.client
            .acknowledge_hand(req(ack("7", &room, &a, &m), &t.moderate)),
        invalid,
        "no raised hand"
    );
}

#[tokio::test]
async fn staff_hand_ops_are_refused_loudly_while_the_flag_is_off() {
    let mut h = harness(42500, false).await;
    let t = tokens(&h);
    let room = create_room(&mut h, "7", None).await.room_id;
    let (a, m) = (uid(), uid());
    join(&mut h, &t, "7", &room, &a, false).await;
    join(&mut h, &t, "7", &room, &m, true).await;
    // Participant-level hand operations are not moderation: they work with the flag off.
    h.client
        .raise_hand(req(raise("7", &room, &a, &a), &t.write))
        .await
        .unwrap();
    let own = h
        .client
        .list_hands(req(list("7", &room, &a), &t.read))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(own.hands.len(), 1);
    // Staff actions fail loud (UNIMPLEMENTED naming the flag), never silently succeed.
    let e = h
        .client
        .acknowledge_hand(req(ack("7", &room, &a, &m), &t.moderate))
        .await
        .unwrap_err();
    assert_eq!(e.code(), Code::Unimplemented);
    assert!(e.message().contains("rooms.moderation"), "{}", e.message());
    code!(
        h.client
            .reset_hands(req(reset("7", &room, &m), &t.moderate)),
        Code::Unimplemented,
        "reset"
    );
    code!(
        h.client
            .lower_hand(req(lower("7", &room, &a, &m), &t.moderate)),
        Code::Unimplemented,
        "lower-other"
    );
    code!(
        h.client.list_hands(req(list("7", &room, &m), &t.moderate)),
        Code::Unimplemented,
        "list-all"
    );
    // Lowering one's own still works.
    assert!(
        h.client
            .lower_hand(req(lower("7", &room, &a, &a), &t.write))
            .await
            .unwrap()
            .into_inner()
            .lowered
    );
}

/// Regression: a hand RPC naming another community's room must be indistinguishable from an
/// unknown room (NOT_FOUND) -- never a read, write or existence oracle across communities,
/// not even for recorded staff of the real room.
#[tokio::test]
async fn cross_community_hand_access_is_not_found() {
    let mut h = harness(42520, true).await;
    let t = tokens(&h);
    let room = create_room(&mut h, "7", None).await.room_id;
    let (a, m) = (uid(), uid());
    join(&mut h, &t, "7", &room, &a, false).await;
    join(&mut h, &t, "7", &room, &m, true).await;
    h.client
        .raise_hand(req(raise("7", &room, &a, &a), &t.write))
        .await
        .unwrap();

    let nf = Code::NotFound;
    code!(
        h.client
            .raise_hand(req(raise("8", &room, &a, &a), &t.write)),
        nf,
        "RaiseHand"
    );
    code!(
        h.client.list_hands(req(list("8", &room, &a), &t.read)),
        nf,
        "ListHands own"
    );
    code!(
        h.client.list_hands(req(list("8", &room, &m), &t.moderate)),
        nf,
        "ListHands all"
    );
    code!(
        h.client
            .lower_hand(req(lower("8", &room, &a, &a), &t.write)),
        nf,
        "LowerHand own"
    );
    code!(
        h.client
            .lower_hand(req(lower("8", &room, &a, &m), &t.moderate)),
        nf,
        "LowerHand other"
    );
    code!(
        h.client
            .acknowledge_hand(req(ack("8", &room, &a, &m), &t.moderate)),
        nf,
        "AcknowledgeHand"
    );
    code!(
        h.client
            .reset_hands(req(reset("8", &room, &m), &t.moderate)),
        nf,
        "ResetHands"
    );
    // Joining the room as community 8 is the same not-found: no way to become a member there.
    code!(
        h.client.join_room(req(
            JoinRoomRequest {
                api_version: API.into(),
                community_id: "8".into(),
                room_id: room.clone(),
                participant_id: uid(),
                publish: false,
                subscribe: true,
                ttl_secs: None,
                ..Default::default()
            },
            &t.moderate
        )),
        nf,
        "JoinRoom"
    );
    // The same answer as a room that never existed (no oracle).
    let e_real = h
        .client
        .list_hands(req(list("8", &room, &a), &t.read))
        .await
        .unwrap_err();
    let e_ghost = h
        .client
        .list_hands(req(list("8", &uid(), &a), &t.read))
        .await
        .unwrap_err();
    assert_eq!(
        (e_real.code(), e_real.message()),
        (e_ghost.code(), e_ghost.message())
    );
    // Room 7's queue is untouched by all of the above.
    let left = all_hands(&mut h, &t, "7", &room, &m).await;
    assert_eq!(ids(&left.hands), vec![a]);
    assert!(left.hands[0].acknowledged_at.is_none());
}

#[tokio::test]
async fn hands_and_membership_are_cleared_on_leave_kick_and_room_delete() {
    let mut h = harness(42540, true).await;
    let t = tokens(&h);
    let room = create_room(&mut h, "7", None).await.room_id;
    let (leaver, kicked, stays, m) = (uid(), uid(), uid(), uid());
    for p in [&leaver, &kicked, &stays] {
        join(&mut h, &t, "7", &room, p, false).await;
        h.client
            .raise_hand(req(raise("7", &room, p, p), &t.write))
            .await
            .unwrap();
    }
    join(&mut h, &t, "7", &room, &m, true).await;

    // Leave (self-service): only the leaver's hand goes, and they are no longer a member.
    h.client
        .leave_room(req(
            LeaveRoomRequest {
                api_version: API.into(),
                community_id: "7".into(),
                room_id: room.clone(),
                participant_id: leaver.clone(),
                actor_id: leaver.clone(),
                actor_role: RoomRole::Member as i32,
            },
            &t.write,
        ))
        .await
        .unwrap();
    assert_eq!(
        ids(&all_hands(&mut h, &t, "7", &room, &m).await.hands),
        vec![kicked.clone(), stays.clone()]
    );
    code!(
        h.client
            .raise_hand(req(raise("7", &room, &leaver, &leaver), &t.write)),
        Code::PermissionDenied,
        "left participant is no longer a member"
    );

    // Kick (moderator): the kicked participant's hand goes, and they cannot rejoin or raise.
    h.client
        .kick_participant(req(
            KickParticipantRequest {
                api_version: API.into(),
                community_id: "7".into(),
                room_id: room.clone(),
                participant_id: kicked.clone(),
                actor_id: m.clone(),
                actor_role: RoomRole::Moderator as i32,
            },
            &t.moderate,
        ))
        .await
        .unwrap();
    assert_eq!(
        ids(&all_hands(&mut h, &t, "7", &room, &m).await.hands),
        vec![stays.clone()]
    );
    code!(
        h.client
            .raise_hand(req(raise("7", &room, &kicked, &kicked), &t.write)),
        Code::PermissionDenied,
        "kicked participant cannot raise"
    );
    code!(
        h.client.join_room(req(
            JoinRoomRequest {
                api_version: API.into(),
                community_id: "7".into(),
                room_id: room.clone(),
                participant_id: kicked.clone(),
                publish: false,
                subscribe: true,
                ttl_secs: None,
                ..Default::default()
            },
            &t.write
        )),
        Code::PermissionDenied,
        "kicked participant cannot get a new join token"
    );

    // Delete the room: the queue goes with it (NOT_FOUND, not an empty list).
    h.client
        .delete_room(req(
            DeleteRoomRequest {
                api_version: API.into(),
                community_id: "7".into(),
                room_id: room.clone(),
            },
            &t.write,
        ))
        .await
        .unwrap();
    code!(
        h.client.list_hands(req(list("7", &room, &m), &t.moderate)),
        Code::NotFound,
        "after delete"
    );
    for (reason, want) in [("leave", 1.0), ("kick", 1.0), ("delete", 1.0)] {
        assert_eq!(
            counter(&h, "rooms_hands_cleared_total", Some(("reason", reason))),
            want,
            "cleared by {reason}"
        );
    }
}

#[tokio::test]
async fn hand_queue_is_bounded_and_resource_exhausted_is_reported() {
    let mut h = harness(42560, true).await;
    let t = tokens(&h);
    let room = create_room(&mut h, "7", None).await.room_id;
    let m = uid();
    join(&mut h, &t, "7", &room, &m, true).await;
    for _ in 0..svc_streaming::rooms::MAX_HANDS_PER_ROOM {
        let p = uid();
        join(&mut h, &t, "7", &room, &p, false).await;
        h.client
            .raise_hand(req(raise("7", &room, &p, &p), &t.write))
            .await
            .unwrap();
    }
    let p = uid();
    join(&mut h, &t, "7", &room, &p, false).await;
    code!(
        h.client
            .raise_hand(req(raise("7", &room, &p, &p), &t.write)),
        Code::ResourceExhausted,
        "queue full"
    );
    assert_eq!(
        all_hands(&mut h, &t, "7", &room, &m).await.hands.len(),
        svc_streaming::rooms::MAX_HANDS_PER_ROOM
    );
}
