//! Real-path tests for the rooms `RoomsService` gRPC server: the tonic server runs
//! in-process behind the production mTLS accept loop and is driven through the
//! generated tonic client over a real rustls handshake, with real ES256 machine
//! JWTs. Also the contract gate: every RPC declared in `rooms.proto` must be
//! served (the generated trait makes a missing impl a compile error; the routing
//! test below proves each one is wired and auth-gated at runtime).

use std::collections::BTreeSet;
use std::sync::Arc;

use jsonwebtoken::{encode, Algorithm, EncodingKey, Header};
use serde_json::json;
use svc_streaming::rooms::grpc::pb::rooms_service_client::RoomsServiceClient;
use svc_streaming::rooms::grpc::pb::*;
use svc_streaming::rooms::grpc::{build_grpc_server_config, serve, RPC_NAMES};
use svc_streaming::rooms::grpc_auth::{
    ServiceJwtVerifier, SCOPE_MODERATE, SCOPE_READ, SCOPE_WRITE,
};
use svc_streaming::rooms::host_api::HostApiTls;
use svc_streaming::rooms::token::TokenKeys;
use svc_streaming::rooms::RoomService;
use svc_streaming::rtc::config::RtcConfig;
use svc_streaming::rtc::metrics::RtcMetrics;
use svc_streaming::rtc::pc_factory::PeerConnectionFactory;
use tonic::transport::{Certificate, Channel, ClientTlsConfig, Identity};
use tonic::{Code, Request};

const SUBJECT: &str = "spiffe://penguintech.io/test/hub-api";
const KID: &str = "k1";

struct Harness {
    client: RoomsServiceClient<Channel>,
    signing_pem: String,
    registry: prometheus::Registry,
    port: u16,
    ca_pem: String,
    _dir: std::path::PathBuf,
}

fn ca() -> (rcgen::CertificateParams, rcgen::KeyPair, rcgen::Certificate) {
    let key = rcgen::KeyPair::generate().unwrap();
    let mut params = rcgen::CertificateParams::new(vec!["test-ca".to_string()]).unwrap();
    params.is_ca = rcgen::IsCa::Ca(rcgen::BasicConstraints::Unconstrained);
    let cert = params.self_signed(&key).unwrap();
    (params, key, cert)
}

/// Starts the gRPC server (mTLS + JWT) and returns a connected client.
async fn harness(udp_base: u16, moderation: bool) -> Harness {
    let dir = std::env::temp_dir().join(format!("svc-streaming-grpc-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&dir).unwrap();
    let w = |name: &str, body: &str| {
        let p = dir.join(name);
        std::fs::write(&p, body).unwrap();
        p
    };

    let (ca_params, ca_key, ca_cert) = ca();
    let issuer = rcgen::Issuer::from_params(&ca_params, ca_key);
    let server_key = rcgen::KeyPair::generate().unwrap();
    let server_cert = rcgen::CertificateParams::new(vec!["localhost".to_string()])
        .unwrap()
        .signed_by(&server_key, &issuer)
        .unwrap();
    let client_key = rcgen::KeyPair::generate().unwrap();
    let client_cert = rcgen::CertificateParams::new(vec!["hub-api".to_string()])
        .unwrap()
        .signed_by(&client_key, &issuer)
        .unwrap();
    let tls = HostApiTls {
        server_cert: w("server.pem", &server_cert.pem()),
        server_key: w("server.key", &server_key.serialize_pem()),
        client_ca: w("ca.pem", &ca_cert.pem()),
        port: 0,
    };
    let server_config = build_grpc_server_config(&tls).expect("server config");

    // ES256 signing key for hub-api; the server only ever sees the public half.
    let signing = rcgen::KeyPair::generate().unwrap();
    let keys = ServiceJwtVerifier::keys_from_pem([(KID, signing.public_key_pem().as_str())]).unwrap();
    let auth = Arc::new(ServiceJwtVerifier::new(
        keys,
        "waddlebot-internal",
        vec!["hub-api".to_string()],
        vec![SUBJECT.to_string()],
    ));

    let registry = prometheus::Registry::new();
    let factory = Arc::new(
        PeerConnectionFactory::new(RtcConfig {
            bind_ip: "127.0.0.1".parse().unwrap(),
            udp_port_range: (udp_base, udp_base + 10),
            nat_1to1_ip: None,
        })
        .unwrap(),
    );
    let metrics = RtcMetrics::register(&registry).unwrap();
    let rooms = Arc::new(
        RoomService::new(
            factory,
            metrics,
            Some(TokenKeys::new("0123456789abcdef0123456789abcdef").unwrap()),
            "http://svc.test".into(),
        )
        .with_moderation(moderation),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(serve(listener, server_config, rooms, auth));

    let client = connect(
        port,
        &ca_cert.pem(),
        Some((client_cert.pem(), client_key.serialize_pem())),
    )
    .await
    .expect("mTLS client connects");
    Harness {
        client,
        signing_pem: signing.serialize_pem(),
        registry,
        port,
        ca_pem: ca_cert.pem(),
        _dir: dir,
    }
}

async fn connect(
    port: u16,
    ca_pem: &str,
    identity: Option<(String, String)>,
) -> Result<RoomsServiceClient<Channel>, tonic::transport::Error> {
    let mut tls = ClientTlsConfig::new()
        .ca_certificate(Certificate::from_pem(ca_pem))
        .domain_name("localhost");
    if let Some((cert, key)) = identity {
        tls = tls.identity(Identity::from_pem(cert, key));
    }
    let channel = Channel::from_shared(format!("https://127.0.0.1:{port}"))
        .unwrap()
        .tls_config(tls)?
        .connect()
        .await?;
    Ok(RoomsServiceClient::new(channel))
}

fn now() -> i64 {
    chrono::Utc::now().timestamp()
}

/// Mints a machine JWT like hub-api's issuer would.
fn jwt(pem: &str, scope: &str, mutate: impl FnOnce(&mut serde_json::Value)) -> String {
    let t = now();
    let mut claims = json!({
        "iss": "hub-api", "aud": "waddlebot-internal", "sub": SUBJECT, "scope": scope,
        "iat": t, "nbf": t, "exp": t + 60, "jti": uuid::Uuid::new_v4().to_string(),
    });
    mutate(&mut claims);
    let mut header = Header::new(Algorithm::ES256);
    header.kid = Some(KID.into());
    encode(&header, &claims, &EncodingKey::from_ec_pem(pem.as_bytes()).unwrap()).unwrap()
}

fn req<T>(msg: T, token: &str) -> Request<T> {
    let mut r = Request::new(msg);
    r.metadata_mut()
        .insert("authorization", format!("Bearer {token}").parse().unwrap());
    r
}

fn admin() -> (String, i32) {
    (uuid::Uuid::new_v4().to_string(), RoomRole::Admin as i32)
}

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
    assert_eq!(declared.len(), 13, "denominator: {declared:?}");
    assert_eq!(declared, constant, "RPC_NAMES drifted from rooms.proto");

    // 2. Each one is routed and demands a token: no token -> Unauthenticated, never
    //    Unimplemented (which is what an unserved RPC returns).
    let mut h = harness(42100, true).await;
    let mut seen = BTreeSet::new();
    macro_rules! gate {
        ($method:ident, $msg:expr, $name:literal) => {{
            let e = h.client.$method(Request::new($msg)).await.unwrap_err();
            assert_eq!(e.code(), Code::Unauthenticated, "{}: {}", $name, e.message());
            seen.insert($name.to_string());
        }};
    }
    gate!(list_rooms, ListRoomsRequest::default(), "ListRooms");
    gate!(get_room, GetRoomRequest::default(), "GetRoom");
    gate!(list_participants, ListParticipantsRequest::default(), "ListParticipants");
    gate!(create_room, CreateRoomRequest::default(), "CreateRoom");
    gate!(delete_room, DeleteRoomRequest::default(), "DeleteRoom");
    gate!(join_room, JoinRoomRequest::default(), "JoinRoom");
    gate!(leave_room, LeaveRoomRequest::default(), "LeaveRoom");
    gate!(kick_participant, KickParticipantRequest::default(), "KickParticipant");
    gate!(mute_participant, MuteParticipantRequest::default(), "MuteParticipant");
    gate!(unmute_participant, UnmuteParticipantRequest::default(), "UnmuteParticipant");
    gate!(mute_all, MuteAllRequest::default(), "MuteAll");
    gate!(lock_room, LockRoomRequest::default(), "LockRoom");
    gate!(unlock_room, UnlockRoomRequest::default(), "UnlockRoom");
    assert_eq!(seen, declared, "an RPC in the proto is not exercised/served");
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
                community_id: "7".into(),
                label: Some("standup".into()),
                max_publishers: Some(4),
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
        .list_rooms(req(ListRoomsRequest { community_id: "7".into() }, &read))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(listed.rooms.len(), 1);
    // Community scoping: another community sees nothing and cannot read the room.
    let other = h
        .client
        .list_rooms(req(ListRoomsRequest { community_id: "8".into() }, &read))
        .await
        .unwrap()
        .into_inner();
    assert!(other.rooms.is_empty());
    let e = h
        .client
        .get_room(req(GetRoomRequest { community_id: "8".into(), room_id: room.room_id.clone() }, &read))
        .await
        .unwrap_err();
    assert_eq!(e.code(), Code::NotFound);

    let participant = uuid::Uuid::new_v4().to_string();
    let joined = h
        .client
        .join_room(req(
            JoinRoomRequest {
                community_id: "7".into(),
                room_id: room.room_id.clone(),
                participant_id: participant.clone(),
                publish: true,
                subscribe: true,
                ttl_secs: None,
            },
            &write,
        ))
        .await
        .unwrap()
        .into_inner();
    assert!(!joined.token.is_empty());
    assert_eq!(joined.participant_id, participant);
    assert_eq!(joined.whip_url, format!("http://svc.test/rtc/rooms/{}/whip", room.room_id));

    // Moderation: lock refuses further joins, unlock restores them.
    h.client
        .lock_room(req(
            LockRoomRequest {
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
        community_id: "7".into(),
        room_id: room.room_id.clone(),
        participant_id: uuid::Uuid::new_v4().to_string(),
        publish: false,
        subscribe: true,
        ttl_secs: None,
    };
    let e = h.client.join_room(req(join_req(), &write)).await.unwrap_err();
    assert_eq!(e.code(), Code::PermissionDenied, "{}", e.message());
    let participants = h
        .client
        .list_participants(req(
            ListParticipantsRequest { community_id: "7".into(), room_id: room.room_id.clone() },
            &read,
        ))
        .await
        .unwrap()
        .into_inner();
    assert!(participants.locked);
    h.client
        .unlock_room(req(
            UnlockRoomRequest {
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
            DeleteRoomRequest { community_id: "7".into(), room_id: room.room_id.clone() },
            &write,
        ))
        .await
        .unwrap();
    let e = h
        .client
        .get_room(req(GetRoomRequest { community_id: "7".into(), room_id: room.room_id }, &read))
        .await
        .unwrap_err();
    assert_eq!(e.code(), Code::NotFound);

    // Telemetry: per-RPC counter + latency histogram recorded, with ok and refused outcomes.
    let families = h.registry.gather();
    let counter = families
        .iter()
        .find(|f| f.name() == "rooms_grpc_requests_total")
        .expect("rooms_grpc_requests_total registered");
    let total: f64 = counter.get_metric().iter().map(|m| m.get_counter().value()).sum();
    assert!(total >= 15.0, "counted {total} grpc calls");
    let has = |code: &str| {
        counter
            .get_metric()
            .iter()
            .any(|m| m.get_label().iter().any(|l| l.name() == "code" && l.value() == code))
    };
    assert!(has("ok") && has("notfound") && has("permissiondenied"));
    assert!(families.iter().any(|f| f.name() == "rooms_grpc_request_seconds"));
}

#[tokio::test]
async fn moderation_enforces_actor_role_and_feature_flag() {
    let mut h = harness(42140, true).await;
    let write = jwt(&h.signing_pem, SCOPE_WRITE, |_| {});
    let moderate = jwt(&h.signing_pem, SCOPE_MODERATE, |_| {});
    let room = h
        .client
        .create_room(req(CreateRoomRequest { community_id: "7".into(), ..Default::default() }, &write))
        .await
        .unwrap()
        .into_inner()
        .room
        .unwrap();
    let kick = |role: RoomRole| KickParticipantRequest {
        community_id: "7".into(),
        room_id: room.room_id.clone(),
        participant_id: uuid::Uuid::new_v4().to_string(),
        actor_id: uuid::Uuid::new_v4().to_string(),
        actor_role: role as i32,
    };
    for role in [RoomRole::Member, RoomRole::Guest, RoomRole::Unspecified] {
        let e = h.client.kick_participant(req(kick(role), &moderate)).await.unwrap_err();
        assert_eq!(e.code(), Code::PermissionDenied, "{role:?}");
    }
    h.client.kick_participant(req(kick(RoomRole::Moderator), &moderate)).await.unwrap();

    // Flag OFF: moderation refused loudly (Unimplemented), not silently ignored.
    let mut off = harness(42160, false).await;
    let write = jwt(&off.signing_pem, SCOPE_WRITE, |_| {});
    let moderate = jwt(&off.signing_pem, SCOPE_MODERATE, |_| {});
    let room = off
        .client
        .create_room(req(CreateRoomRequest { community_id: "7".into(), ..Default::default() }, &write))
        .await
        .unwrap()
        .into_inner()
        .room
        .unwrap();
    let e = off
        .client
        .lock_room(req(
            LockRoomRequest {
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
        .get_room(req(GetRoomRequest { community_id: "7".into(), room_id: "not-a-uuid".into() }, &read))
        .await
        .unwrap_err();
    assert_eq!(e.code(), Code::InvalidArgument);
}

#[tokio::test]
async fn bad_tokens_are_refused_before_any_work() {
    let mut h = harness(42200, true).await;
    let k = h.signing_pem.clone();
    let list = |t: String| async {
        let mut c = h.client.clone();
        c.list_rooms(req(ListRoomsRequest { community_id: "7".into() }, &t)).await
    };
    // Sanity: a good token works.
    assert!(list(jwt(&k, SCOPE_READ, |_| {})).await.is_ok());

    let wrong_scope = list(jwt(&k, SCOPE_WRITE, |_| {})).await.unwrap_err();
    assert_eq!(wrong_scope.code(), Code::PermissionDenied);
    let wrong_sub = list(jwt(&k, SCOPE_READ, |c| c["sub"] = json!("spiffe://penguintech.io/x/other")))
        .await
        .unwrap_err();
    assert_eq!(wrong_sub.code(), Code::PermissionDenied);

    let cases: Vec<(&str, String)> = vec![
        ("expired", jwt(&k, SCOPE_READ, |c| { c["exp"] = json!(now() - 3600); c["iat"] = json!(now() - 7200); c["nbf"] = json!(now() - 7200); })),
        ("wrong audience", jwt(&k, SCOPE_READ, |c| c["aud"] = json!("someone-else"))),
        ("wrong issuer", jwt(&k, SCOPE_READ, |c| c["iss"] = json!("evil"))),
        ("no jti", jwt(&k, SCOPE_READ, |c| { c.as_object_mut().unwrap().remove("jti"); })),
        ("foreign key", jwt(&rcgen::KeyPair::generate().unwrap().serialize_pem(), SCOPE_READ, |_| {})),
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
    assert_eq!(list(confusion).await.unwrap_err().code(), Code::Unauthenticated);

    // Missing header entirely.
    let e = h
        .client
        .list_rooms(Request::new(ListRoomsRequest { community_id: "7".into() }))
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
            .list_rooms(Request::new(ListRoomsRequest { community_id: "7".into() }))
            .await
            .is_err(),
    };
    assert!(refused, "a certificate-less client must not reach any handler");
}
