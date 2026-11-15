//! Real-path tests for the rooms `RoomsService` gRPC server: the tonic server runs
//! in-process behind the production mTLS accept loop and is driven through the
//! generated tonic client over a real rustls handshake, with real ES256 machine
//! JWTs. Also the contract gate: every RPC declared in `rooms.proto` must be
//! served (the generated trait makes a missing impl a compile error; the routing
//! test below proves each one is wired and auth-gated at runtime).

mod rooms_common;

use std::collections::BTreeSet;

use jsonwebtoken::{encode, Algorithm, EncodingKey, Header};
use serde_json::json;
use svc_streaming::rooms::grpc::pb::*;
use svc_streaming::rooms::grpc::{build_grpc_server_config, RPC_NAMES};
use svc_streaming::rooms::grpc_auth::{SCOPE_MODERATE, SCOPE_READ, SCOPE_WRITE};
use svc_streaming::rooms::host_api::HostApiTls;
use tonic::{Code, Request};

use rooms_common::*;

#[tokio::test]
async fn every_proto_rpc_is_declared_served_and_auth_gated() {
    // 1. The proto text, the constant the server labels metrics with, and the tonic service
    //    descriptor all list the same RPCs.
    let proto = std::fs::read_to_string(
        std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../proto/waddles/streaming/internal/v1/rooms.proto"),
    )
    .unwrap();
    let declared: BTreeSet<String> = proto
        .lines()
        .filter_map(|l| l.trim().strip_prefix("rpc "))
        .map(|l| l.split('(').next().unwrap().trim().to_string())
        .collect();
    let constant: BTreeSet<String> = RPC_NAMES.iter().map(|s| s.to_string()).collect();
    assert_eq!(declared.len(), 18, "denominator: {declared:?}");
    assert_eq!(declared, constant, "RPC_NAMES drifted from rooms.proto");

    // 2. Each one is routed and demands a token: no token -> Unauthenticated, never
    //    Unimplemented (which is what an unserved RPC returns).
    let mut h = harness(42100, true).await;
    let mut seen = BTreeSet::new();
    macro_rules! gate {
        ($method:ident, $msg:expr, $name:literal) => {{
            let e = h.client.$method(Request::new($msg)).await.unwrap_err();
            assert_eq!(
                e.code(),
                Code::Unauthenticated,
                "{}: {}",
                $name,
                e.message()
            );
            seen.insert($name.to_string());
        }};
    }
    gate!(list_rooms, ListRoomsRequest::default(), "ListRooms");
    gate!(get_room, GetRoomRequest::default(), "GetRoom");
    gate!(
        list_participants,
        ListParticipantsRequest::default(),
        "ListParticipants"
    );
    gate!(create_room, CreateRoomRequest::default(), "CreateRoom");
    gate!(delete_room, DeleteRoomRequest::default(), "DeleteRoom");
    gate!(join_room, JoinRoomRequest::default(), "JoinRoom");
    gate!(leave_room, LeaveRoomRequest::default(), "LeaveRoom");
    gate!(
        kick_participant,
        KickParticipantRequest::default(),
        "KickParticipant"
    );
    gate!(
        mute_participant,
        MuteParticipantRequest::default(),
        "MuteParticipant"
    );
    gate!(
        unmute_participant,
        UnmuteParticipantRequest::default(),
        "UnmuteParticipant"
    );
    gate!(mute_all, MuteAllRequest::default(), "MuteAll");
    gate!(lock_room, LockRoomRequest::default(), "LockRoom");
    gate!(unlock_room, UnlockRoomRequest::default(), "UnlockRoom");
    gate!(list_hands, ListHandsRequest::default(), "ListHands");
    gate!(raise_hand, RaiseHandRequest::default(), "RaiseHand");
    gate!(lower_hand, LowerHandRequest::default(), "LowerHand");
    gate!(
        acknowledge_hand,
        AcknowledgeHandRequest::default(),
        "AcknowledgeHand"
    );
    gate!(reset_hands, ResetHandsRequest::default(), "ResetHands");
    assert_eq!(
        seen, declared,
        "an RPC in the proto is not exercised/served"
    );
}

#[tokio::test]
async fn full_room_lifecycle_over_mtls_with_service_jwt() {
    let mut h = harness(42120, true).await;
    let read = jwt(&h.signing_pem, SCOPE_READ, |_| {});
    let write = jwt(&h.signing_pem, SCOPE_WRITE, |_| {});
    let moderate = jwt(&h.signing_pem, SCOPE_MODERATE, |_| {});
    let (actor, role) = admin();

    let room = h
        .client
        .create_room(req(
            CreateRoomRequest {
                api_version: "v1".into(),
                community_id: "7".into(),
                label: Some("standup".into()),
                max_publishers: Some(4),
                empty_timeout_secs: None,
                ..Default::default()
            },
            &write,
        ))
        .await
        .unwrap()
        .into_inner()
        .room
        .unwrap();
    assert_eq!(room.label.as_deref(), Some("standup"));
    assert_eq!(room.max_publishers, 4);

    let listed = h
        .client
        .list_rooms(req(
            ListRoomsRequest {
                api_version: "v1".into(),
                community_id: "7".into(),
            },
            &read,
        ))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(listed.rooms.len(), 1);
    // Community scoping: another community sees nothing and cannot read the room.
    let other = h
        .client
        .list_rooms(req(
            ListRoomsRequest {
                api_version: "v1".into(),
                community_id: "8".into(),
            },
            &read,
        ))
        .await
        .unwrap()
        .into_inner();
    assert!(other.rooms.is_empty());
    let e = h
        .client
        .get_room(req(
            GetRoomRequest {
                api_version: "v1".into(),
                community_id: "8".into(),
                room_id: room.room_id.clone(),
            },
            &read,
        ))
        .await
        .unwrap_err();
    assert_eq!(e.code(), Code::NotFound);

    let participant = uuid::Uuid::new_v4().to_string();
    let joined = h
        .client
        .join_room(req(
            JoinRoomRequest {
                api_version: "v1".into(),
                community_id: "7".into(),
                room_id: room.room_id.clone(),
                participant_id: participant.clone(),
                publish: true,
                subscribe: true,
                ttl_secs: None,
                ..Default::default()
            },
            &write,
        ))
        .await
        .unwrap()
        .into_inner();
    assert!(!joined.token.is_empty());
    assert_eq!(joined.participant_id, participant);
    assert_eq!(
        joined.whip_url,
        format!("http://svc.test/rtc/rooms/{}/whip", room.room_id)
    );

    // Moderation: lock refuses further joins, unlock restores them.
    h.client
        .lock_room(req(
            LockRoomRequest {
                api_version: "v1".into(),
                community_id: "7".into(),
                room_id: room.room_id.clone(),
                actor_id: actor.clone(),
                actor_role: role,
            },
            &moderate,
        ))
        .await
        .unwrap();
    let join_req = || JoinRoomRequest {
        api_version: "v1".into(),
        community_id: "7".into(),
        room_id: room.room_id.clone(),
        participant_id: uuid::Uuid::new_v4().to_string(),
        publish: false,
        subscribe: true,
        ttl_secs: None,
        ..Default::default()
    };
    let e = h
        .client
        .join_room(req(join_req(), &write))
        .await
        .unwrap_err();
    assert_eq!(e.code(), Code::PermissionDenied, "{}", e.message());
    let participants = h
        .client
        .list_participants(req(
            ListParticipantsRequest {
                api_version: "v1".into(),
                community_id: "7".into(),
                room_id: room.room_id.clone(),
            },
            &read,
        ))
        .await
        .unwrap()
        .into_inner();
    assert!(participants.locked);
    h.client
        .unlock_room(req(
            UnlockRoomRequest {
                api_version: "v1".into(),
                community_id: "7".into(),
                room_id: room.room_id.clone(),
                actor_id: actor.clone(),
                actor_role: role,
            },
            &moderate,
        ))
        .await
        .unwrap();
    h.client.join_room(req(join_req(), &write)).await.unwrap();

    // mute/unmute/mute_all/kick/leave all succeed for an admin actor.
    let target = uuid::Uuid::new_v4().to_string();
    let actor_id = actor.clone();
    let muted = h
        .client
        .mute_participant(req(
            MuteParticipantRequest {
                api_version: "v1".into(),
                community_id: "7".into(),
                room_id: room.room_id.clone(),
                participant_id: target.clone(),
                actor_id: actor_id.clone(),
                actor_role: role,
            },
            &moderate,
        ))
        .await
        .unwrap()
        .into_inner();
    assert!(muted.muted);
    h.client
        .unmute_participant(req(
            UnmuteParticipantRequest {
                api_version: "v1".into(),
                community_id: "7".into(),
                room_id: room.room_id.clone(),
                participant_id: target.clone(),
                actor_id: actor_id.clone(),
                actor_role: role,
            },
            &moderate,
        ))
        .await
        .unwrap();
    h.client
        .mute_all(req(
            MuteAllRequest {
                api_version: "v1".into(),
                community_id: "7".into(),
                room_id: room.room_id.clone(),
                actor_id: actor_id.clone(),
                actor_role: role,
            },
            &moderate,
        ))
        .await
        .unwrap();
    h.client
        .kick_participant(req(
            KickParticipantRequest {
                api_version: "v1".into(),
                community_id: "7".into(),
                room_id: room.room_id.clone(),
                participant_id: target.clone(),
                actor_id: actor_id.clone(),
                actor_role: role,
            },
            &moderate,
        ))
        .await
        .unwrap();
    h.client
        .leave_room(req(
            LeaveRoomRequest {
                api_version: "v1".into(),
                community_id: "7".into(),
                room_id: room.room_id.clone(),
                participant_id: participant.clone(),
                actor_id: participant.clone(),
                actor_role: RoomRole::Member as i32,
            },
            &write,
        ))
        .await
        .unwrap();

    h.client
        .delete_room(req(
            DeleteRoomRequest {
                api_version: "v1".into(),
                community_id: "7".into(),
                room_id: room.room_id.clone(),
            },
            &write,
        ))
        .await
        .unwrap();
    let e = h
        .client
        .get_room(req(
            GetRoomRequest {
                api_version: "v1".into(),
                community_id: "7".into(),
                room_id: room.room_id,
            },
            &read,
        ))
        .await
        .unwrap_err();
    assert_eq!(e.code(), Code::NotFound);

    // Telemetry: per-RPC counter + latency histogram recorded, with ok and refused outcomes.
    let families = h.registry.gather();
    let counter = families
        .iter()
        .find(|f| f.name() == "rooms_grpc_requests_total")
        .expect("rooms_grpc_requests_total registered");
    let total: f64 = counter
        .get_metric()
        .iter()
        .map(|m| m.get_counter().value())
        .sum();
    assert!(total >= 15.0, "counted {total} grpc calls");
    let has = |code: &str| {
        counter.get_metric().iter().any(|m| {
            m.get_label()
                .iter()
                .any(|l| l.name() == "code" && l.value() == code)
        })
    };
    assert!(has("ok") && has("notfound") && has("permissiondenied"));
    assert!(families
        .iter()
        .any(|f| f.name() == "rooms_grpc_request_seconds"));
}

#[tokio::test]
async fn moderation_enforces_actor_role_and_feature_flag() {
    let mut h = harness(42140, true).await;
    let write = jwt(&h.signing_pem, SCOPE_WRITE, |_| {});
    let moderate = jwt(&h.signing_pem, SCOPE_MODERATE, |_| {});
    let room = h
        .client
        .create_room(req(
            CreateRoomRequest {
                api_version: "v1".into(),
                community_id: "7".into(),
                ..Default::default()
            },
            &write,
        ))
        .await
        .unwrap()
        .into_inner()
        .room
        .unwrap();
    let kick = |role: RoomRole| KickParticipantRequest {
        api_version: "v1".into(),
        community_id: "7".into(),
        room_id: room.room_id.clone(),
        participant_id: uuid::Uuid::new_v4().to_string(),
        actor_id: uuid::Uuid::new_v4().to_string(),
        actor_role: role as i32,
    };
    for role in [RoomRole::Member, RoomRole::Guest, RoomRole::Unspecified] {
        let e = h
            .client
            .kick_participant(req(kick(role), &moderate))
            .await
            .unwrap_err();
        assert_eq!(e.code(), Code::PermissionDenied, "{role:?}");
    }
    h.client
        .kick_participant(req(kick(RoomRole::Moderator), &moderate))
        .await
        .unwrap();

    // Flag OFF: moderation refused loudly (Unimplemented), not silently ignored.
    let mut off = harness(42160, false).await;
    let write = jwt(&off.signing_pem, SCOPE_WRITE, |_| {});
    let moderate = jwt(&off.signing_pem, SCOPE_MODERATE, |_| {});
    let room = off
        .client
        .create_room(req(
            CreateRoomRequest {
                api_version: "v1".into(),
                community_id: "7".into(),
                ..Default::default()
            },
            &write,
        ))
        .await
        .unwrap()
        .into_inner()
        .room
        .unwrap();
    let e = off
        .client
        .lock_room(req(
            LockRoomRequest {
                api_version: "v1".into(),
                community_id: "7".into(),
                room_id: room.room_id,
                actor_id: uuid::Uuid::new_v4().to_string(),
                actor_role: RoomRole::Admin as i32,
            },
            &moderate,
        ))
        .await
        .unwrap_err();
    assert_eq!(e.code(), Code::Unimplemented);
    assert!(e.message().contains("rooms.moderation"), "{}", e.message());
}

#[tokio::test]
async fn invalid_ids_are_invalid_argument() {
    let mut h = harness(42180, true).await;
    let read = jwt(&h.signing_pem, SCOPE_READ, |_| {});
    let e = h
        .client
        .get_room(req(
            GetRoomRequest {
                api_version: "v1".into(),
                community_id: "7".into(),
                room_id: "not-a-uuid".into(),
            },
            &read,
        ))
        .await
        .unwrap_err();
    assert_eq!(e.code(), Code::InvalidArgument);
}

#[tokio::test]
async fn bad_tokens_are_refused_before_any_work() {
    let mut h = harness(42200, true).await;
    let k = h.signing_pem.clone();
    let base_client = h.client.clone();
    let list = |t: String| {
        let mut c = base_client.clone();
        async move {
            c.list_rooms(req(
                ListRoomsRequest {
                    api_version: "v1".into(),
                    community_id: "7".into(),
                },
                &t,
            ))
            .await
        }
    };
    // Sanity: a good token works.
    assert!(list(jwt(&k, SCOPE_READ, |_| {})).await.is_ok());

    let wrong_scope = list(jwt(&k, SCOPE_WRITE, |_| {})).await.unwrap_err();
    assert_eq!(wrong_scope.code(), Code::PermissionDenied);
    let wrong_sub = list(jwt(&k, SCOPE_READ, |c| {
        c["sub"] = json!("spiffe://penguintech.io/x/other")
    }))
    .await
    .unwrap_err();
    assert_eq!(wrong_sub.code(), Code::PermissionDenied);

    let cases: Vec<(&str, String)> = vec![
        (
            "expired",
            jwt(&k, SCOPE_READ, |c| {
                c["exp"] = json!(now() - 3600);
                c["iat"] = json!(now() - 7200);
                c["nbf"] = json!(now() - 7200);
            }),
        ),
        (
            "wrong audience",
            jwt(&k, SCOPE_READ, |c| c["aud"] = json!("someone-else")),
        ),
        (
            "wrong issuer",
            jwt(&k, SCOPE_READ, |c| c["iss"] = json!("evil")),
        ),
        (
            "no jti",
            jwt(&k, SCOPE_READ, |c| {
                c.as_object_mut().unwrap().remove("jti");
            }),
        ),
        (
            "foreign key",
            jwt(
                &rcgen::KeyPair::generate().unwrap().serialize_pem(),
                SCOPE_READ,
                |_| {},
            ),
        ),
        ("garbage", "not.a.jwt".to_string()),
    ];
    for (name, token) in cases {
        let e = list(token).await.unwrap_err();
        assert_eq!(e.code(), Code::Unauthenticated, "{name}: {}", e.message());
    }

    // HS256 "signed" with the public key (alg confusion) and `alg: none` are refused.
    let mut hs = Header::new(Algorithm::HS256);
    hs.kid = Some(KID.into());
    let t = now();
    let confusion = encode(
        &hs,
        &json!({"iss":"hub-api","aud":"waddlebot-internal","sub":SUBJECT,"scope":SCOPE_READ,
                "iat":t,"nbf":t,"exp":t+60,"jti":"j"}),
        &EncodingKey::from_secret(b"public-key-bytes"),
    )
    .unwrap();
    assert_eq!(
        list(confusion).await.unwrap_err().code(),
        Code::Unauthenticated
    );

    // Missing header entirely.
    let e = h
        .client
        .list_rooms(Request::new(ListRoomsRequest {
            api_version: "v1".into(),
            community_id: "7".into(),
        }))
        .await
        .unwrap_err();
    assert_eq!(e.code(), Code::Unauthenticated);
}

#[tokio::test]
async fn client_without_a_certificate_from_the_configured_ca_is_refused() {
    let h = harness(42220, true).await;
    // No client certificate at all.
    let no_cert = connect(h.port, &h.ca_pem, None).await;
    let refused = match no_cert {
        Err(_) => true,
        Ok(mut c) => c
            .list_rooms(Request::new(ListRoomsRequest {
                api_version: "v1".into(),
                community_id: "7".into(),
            }))
            .await
            .is_err(),
    };
    assert!(
        refused,
        "a certificate-less client must not reach any handler"
    );
}

#[tokio::test]
async fn unknown_or_missing_api_version_is_unimplemented_not_served() {
    let mut h = harness(42240, true).await;
    let read = jwt(&h.signing_pem, SCOPE_READ, |_| {});
    // Missing (empty) and unknown versions are both refused, with the version named.
    for v in ["", "v2"] {
        let e = h
            .client
            .list_rooms(req(
                ListRoomsRequest {
                    api_version: v.into(),
                    community_id: "7".into(),
                },
                &read,
            ))
            .await
            .unwrap_err();
        assert_eq!(e.code(), Code::Unimplemented, "api_version {v:?}");
        assert!(
            e.message()
                .contains(&format!("api_version {v} not supported")),
            "message: {}",
            e.message()
        );
    }
    // A wrong version never reaches auth-free territory: no token still wins.
    let e = h
        .client
        .list_rooms(Request::new(ListRoomsRequest {
            api_version: "v2".into(),
            community_id: "7".into(),
        }))
        .await
        .unwrap_err();
    assert_eq!(e.code(), Code::Unauthenticated);
}

#[tokio::test]
async fn empty_timeout_is_defaulted_disabled_or_clamped_by_the_server() {
    let mut h = harness_with(
        42280,
        Opts {
            idle: Some(svc_streaming::rooms::IdlePolicy::new(120, 600).unwrap()),
            ..Opts::default()
        },
    )
    .await;
    let default = create_room(&mut h, "7", None).await;
    let never = create_room(&mut h, "7", Some(0)).await;
    let exact = create_room(&mut h, "7", Some(45)).await;
    let clamped = create_room(&mut h, "7", Some(u32::MAX)).await;
    assert_eq!(
        default.empty_timeout_secs, 120,
        "unset -> configured default"
    );
    assert_eq!(never.empty_timeout_secs, 0, "0 -> never expire");
    assert_eq!(exact.empty_timeout_secs, 45);
    assert_eq!(
        clamped.empty_timeout_secs, 600,
        "clamped to the configured maximum"
    );
    // The value round-trips through GetRoom and ListRooms.
    let read = jwt(&h.signing_pem, SCOPE_READ, |_| {});
    let got = h
        .client
        .get_room(req(
            GetRoomRequest {
                api_version: "v1".into(),
                community_id: "7".into(),
                room_id: exact.room_id.clone(),
            },
            &read,
        ))
        .await
        .unwrap()
        .into_inner()
        .room
        .unwrap();
    assert_eq!(got.empty_timeout_secs, 45);
}

#[tokio::test]
async fn community_bound_token_cannot_reach_another_community() {
    let mut h = harness(42300, true).await;
    let bound = |scope: &str| jwt(&h.signing_pem, scope, |c| c["community"] = json!(7));
    let (read, write) = (bound(SCOPE_READ), bound(SCOPE_WRITE));
    let room = h
        .client
        .create_room(req(
            CreateRoomRequest {
                api_version: "v1".into(),
                community_id: "7".into(),
                ..Default::default()
            },
            &write,
        ))
        .await
        .unwrap()
        .into_inner()
        .room
        .unwrap();
    // The bound community works.
    h.client
        .get_room(req(
            GetRoomRequest {
                api_version: "v1".into(),
                community_id: "7".into(),
                room_id: room.room_id.clone(),
            },
            &read,
        ))
        .await
        .unwrap();
    // Any other community named in the body is NOT_FOUND, for reads and writes alike, and a
    // room is never created in it.
    for other in ["8", "", "07", "7 "] {
        let e = h
            .client
            .get_room(req(
                GetRoomRequest {
                    api_version: "v1".into(),
                    community_id: other.into(),
                    room_id: room.room_id.clone(),
                },
                &read,
            ))
            .await
            .unwrap_err();
        assert_eq!(
            e.code(),
            Code::NotFound,
            "community {other:?}: {}",
            e.message()
        );
        let e = h
            .client
            .create_room(req(
                CreateRoomRequest {
                    api_version: "v1".into(),
                    community_id: other.into(),
                    ..Default::default()
                },
                &write,
            ))
            .await
            .unwrap_err();
        assert_eq!(e.code(), Code::NotFound, "create in {other:?}");
    }
    assert_eq!(h.svc.list_rooms("8").await.unwrap().len(), 0);
    // A non-numeric `community` claim is an invalid token, not "no binding".
    let bad = jwt(&h.signing_pem, SCOPE_READ, |c| c["community"] = json!("7"));
    let e = h
        .client
        .list_rooms(req(
            ListRoomsRequest {
                api_version: "v1".into(),
                community_id: "7".into(),
            },
            &bad,
        ))
        .await
        .unwrap_err();
    assert_eq!(e.code(), Code::Unauthenticated);
}

#[tokio::test]
async fn health_is_serving_when_ready_and_demands_a_service_jwt() {
    use tonic_health::pb::health_check_response::ServingStatus;
    use tonic_health::pb::health_client::HealthClient;
    use tonic_health::pb::HealthCheckRequest;

    let h = harness(42320, true).await;
    let ch = channel(h.port, &h.ca_pem, Some(h.client_identity.clone()))
        .await
        .unwrap();
    let mut health = HealthClient::new(ch);
    let check = |service: &str| HealthCheckRequest {
        service: service.into(),
    };
    let read = jwt(&h.signing_pem, SCOPE_READ, |_| {});
    for service in ["", svc_streaming::rooms::grpc::HEALTH_SERVICE_NAME] {
        let r = health
            .check(req(check(service), &read))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(r.status(), ServingStatus::Serving, "service {service:?}");
    }
    // Unknown services are NOT_FOUND per the grpc.health.v1 contract.
    let e = health.check(req(check("nope"), &read)).await.unwrap_err();
    assert_eq!(e.code(), Code::NotFound);
    // Health is an inter-service call like any other: no token, wrong scope, bad token.
    let e = health.check(Request::new(check(""))).await.unwrap_err();
    assert_eq!(e.code(), Code::Unauthenticated);
    let write = jwt(&h.signing_pem, SCOPE_WRITE, |_| {});
    let e = health.check(req(check(""), &write)).await.unwrap_err();
    assert_eq!(e.code(), Code::PermissionDenied);
    let e = health.check(req(check(""), "not.a.jwt")).await.unwrap_err();
    assert_eq!(e.code(), Code::Unauthenticated);
}

#[tokio::test]
async fn health_is_not_serving_when_the_room_service_is_not_ready() {
    use tonic_health::pb::health_check_response::ServingStatus;
    use tonic_health::pb::health_client::HealthClient;
    use tonic_health::pb::HealthCheckRequest;

    let h = harness_with(
        42340,
        Opts {
            token_secret: false,
            ..Opts::default()
        },
    )
    .await;
    assert!(h.svc.readiness().is_err());
    let ch = channel(h.port, &h.ca_pem, Some(h.client_identity.clone()))
        .await
        .unwrap();
    let mut health = HealthClient::new(ch);
    let read = jwt(&h.signing_pem, SCOPE_READ, |_| {});
    for service in ["", svc_streaming::rooms::grpc::HEALTH_SERVICE_NAME] {
        let r = health
            .check(req(
                HealthCheckRequest {
                    service: service.into(),
                },
                &read,
            ))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(r.status(), ServingStatus::NotServing, "service {service:?}");
    }
}

#[test]
fn grpc_tls_config_names_its_crypto_provider_explicitly() {
    // svc_streaming's lock holds BOTH rustls backends (ring and aws-lc-rs), so a bare
    // `ServerConfig::builder()` cannot pick one: it panics unless a process default has been
    // installed. The gRPC listener must therefore name its provider itself and must not lean
    // on (or install) a process-wide default.
    assert!(
        rustls::crypto::CryptoProvider::get_default().is_none(),
        "this test binary must run with no process-default provider installed"
    );
    let dir = std::env::temp_dir().join(format!("svc-streaming-tls-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&dir).unwrap();
    let (ca_params, ca_key, ca_cert) = {
        let key = rcgen::KeyPair::generate().unwrap();
        let mut params = rcgen::CertificateParams::new(vec!["test-ca".to_string()]).unwrap();
        params.is_ca = rcgen::IsCa::Ca(rcgen::BasicConstraints::Unconstrained);
        let cert = params.self_signed(&key).unwrap();
        (params, key, cert)
    };
    let issuer = rcgen::Issuer::from_params(&ca_params, ca_key);
    let server_key = rcgen::KeyPair::generate().unwrap();
    let server_cert = rcgen::CertificateParams::new(vec!["localhost".to_string()])
        .unwrap()
        .signed_by(&server_key, &issuer)
        .unwrap();
    let write = |name: &str, body: String| {
        let p = dir.join(name);
        std::fs::write(&p, body).unwrap();
        p
    };
    let tls = HostApiTls {
        server_cert: write("server.pem", server_cert.pem()),
        server_key: write("server.key", server_key.serialize_pem()),
        client_ca: write("ca.pem", ca_cert.pem()),
        port: 0,
    };
    let cfg = build_grpc_server_config(&tls).expect("explicit-provider config builds");
    assert!(
        rustls::crypto::CryptoProvider::get_default().is_none(),
        "building the listener config must not install a process default"
    );
    assert_eq!(cfg.alpn_protocols, vec![b"h2".to_vec()]);
    // The provider is the one the code named (ring), not whichever backend won a default race:
    // ring's default kx groups are exactly the config's.
    let ring = rustls::crypto::ring::default_provider();
    let names = |p: &rustls::crypto::CryptoProvider| {
        p.kx_groups.iter().map(|g| g.name()).collect::<Vec<_>>()
    };
    assert_eq!(names(cfg.crypto_provider()), names(&ring));
    assert!(!cfg.crypto_provider().cipher_suites.is_empty());
    let _ = std::fs::remove_dir_all(&dir);
}
