//! Room tokens are ES256 only (owner decision; no HS256 shared secret anywhere), with the
//! short join TTLs of the shared grants contract (`bundles/rust/webrtc/contract/grants.json`:
//! default 60 s, join max 900 s). A token minted through the real gRPC `JoinRoom` is verified
//! by the live `RoomService`/media router; forged HS256, `alg: none`, foreign-key and
//! wrong-`kid` tokens are refused at both layers.

mod rooms_common;

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine as _;
use jsonwebtoken::{encode, Algorithm, EncodingKey, Header};
use serde_json::{json, Value};
use svc_streaming::rooms::grpc::pb::*;
use svc_streaming::rooms::grpc_auth::SCOPE_WRITE;
use svc_streaming::rooms::{media, RoomError};
use tonic::Code;
use uuid::Uuid;

use rooms_common::*;

fn segment(token: &str, i: usize) -> Value {
    serde_json::from_slice(
        &URL_SAFE_NO_PAD
            .decode(token.split('.').nth(i).unwrap())
            .unwrap(),
    )
    .unwrap()
}

async fn join(
    h: &mut Harness,
    room: &str,
    ttl: Option<i64>,
) -> Result<JoinRoomResponse, tonic::Status> {
    let write = jwt(&h.signing_pem, SCOPE_WRITE, |_| {});
    h.client
        .join_room(req(
            JoinRoomRequest {
                api_version: "v1".into(),
                community_id: "7".into(),
                room_id: room.into(),
                participant_id: uid(),
                publish: false,
                subscribe: true,
                ttl_secs: ttl,
            },
            &write,
        ))
        .await
        .map(|r| r.into_inner())
}

async fn whip_status(router: &axum::Router, room: &str, token: &str) -> axum::http::StatusCode {
    use tower::ServiceExt;
    router
        .clone()
        .oneshot(
            axum::http::Request::builder()
                .method("POST")
                .uri(format!("/rtc/rooms/{room}/whip"))
                .header("content-type", "application/sdp")
                .header("authorization", format!("Bearer {token}"))
                .body(axum::body::Body::from("v=0\r\n"))
                .unwrap(),
        )
        .await
        .unwrap()
        .status()
}

#[tokio::test]
async fn join_tokens_are_es256_with_the_contracts_short_ttls() {
    let mut h = harness(42800, true).await;
    let room = create_room(&mut h, "7", None).await.room_id;

    // Default: 60 s, ES256, kid in the header, subject = the participant UUID only.
    let before = now();
    let joined = join(&mut h, &room, None).await.unwrap();
    let header = segment(&joined.token, 0);
    assert_eq!(header["alg"], "ES256");
    assert_eq!(header["kid"], "room-test");
    let claims = segment(&joined.token, 1);
    assert_eq!(claims["sub"], joined.participant_id);
    assert_eq!(claims["room"], room);
    for forbidden in [
        "name",
        "display_name",
        "username",
        "email",
        "nickname",
        "label",
    ] {
        assert!(claims.get(forbidden).is_none(), "token carries {forbidden}");
    }
    let ttl = joined.expires_at - before;
    assert!(
        (59..=62).contains(&ttl),
        "default join ttl was {ttl} s, want ~60"
    );

    // Bounds: 900 is the join maximum; above it and below the 30 s floor are refused.
    let max = join(&mut h, &room, Some(900)).await.unwrap();
    assert!((max.expires_at - now() - 900).abs() <= 2);
    for bad in [901, 3600, 29, 0, -5] {
        let e = join(&mut h, &room, Some(bad)).await.unwrap_err();
        assert_eq!(
            e.code(),
            Code::InvalidArgument,
            "ttl {bad}: {}",
            e.message()
        );
    }
    // The live service verifies what it minted.
    let room_id = Uuid::parse_str(&room).unwrap();
    assert!(h.svc.authorize(&joined.token, room_id).await.is_ok());
}

#[tokio::test]
async fn forged_hs256_none_foreign_key_and_wrong_kid_tokens_are_refused() {
    let mut h = harness(42820, true).await;
    let room = create_room(&mut h, "7", None).await.room_id;
    let room_id = Uuid::parse_str(&room).unwrap();
    let real = join(&mut h, &room, None).await.unwrap().token;
    let claims = segment(&real, 1);
    let media = media::router(h.svc.clone());
    assert_ne!(
        whip_status(&media, &room, &real).await,
        axum::http::StatusCode::UNAUTHORIZED,
        "positive control: the real token passes auth (the junk SDP is refused later)"
    );

    let mut forged: Vec<(&str, String)> = Vec::new();
    // HS256 with an attacker-chosen secret, copying the real claims and kid.
    let mut hs = Header::new(Algorithm::HS256);
    hs.kid = Some("room-test".into());
    forged.push((
        "hs256",
        encode(
            &hs,
            &claims,
            &EncodingKey::from_secret(b"0123456789abcdef0123456789abcdef"),
        )
        .unwrap(),
    ));
    // `alg: none` in several spellings, with the real claims and no/empty signature.
    let b64 = |v: Value| URL_SAFE_NO_PAD.encode(serde_json::to_vec(&v).unwrap());
    for alg in ["none", "None", "NONE"] {
        forged.push((
            "alg none",
            format!(
                "{}.{}.",
                b64(json!({"alg": alg, "kid": "room-test", "typ": "JWT"})),
                b64(claims.clone())
            ),
        ));
    }
    // ES256 from another key pair (even naming the right kid).
    let other = rcgen::KeyPair::generate().unwrap();
    let mut es = Header::new(Algorithm::ES256);
    es.kid = Some("room-test".into());
    forged.push((
        "foreign es256",
        encode(
            &es,
            &claims,
            &EncodingKey::from_ec_pem(other.serialize_pem().as_bytes()).unwrap(),
        )
        .unwrap(),
    ));
    // Right key pair material is not available to the attacker, but a token whose header names a
    // different kid is refused regardless: re-sign the real payload under another kid with the
    // foreign key (cannot be the real key) to prove the kid pin on its own is not the only gate.
    let mut wrong_kid = Header::new(Algorithm::ES256);
    wrong_kid.kid = Some("other-key".into());
    forged.push((
        "wrong kid",
        encode(
            &wrong_kid,
            &claims,
            &EncodingKey::from_ec_pem(other.serialize_pem().as_bytes()).unwrap(),
        )
        .unwrap(),
    ));
    // A token with no kid at all.
    forged.push((
        "no kid",
        encode(
            &Header::new(Algorithm::ES256),
            &claims,
            &EncodingKey::from_ec_pem(other.serialize_pem().as_bytes()).unwrap(),
        )
        .unwrap(),
    ));
    assert!(forged.len() >= 7, "denominator");

    for (name, token) in &forged {
        assert!(
            matches!(
                h.svc.authorize(token, room_id).await,
                Err(RoomError::Unauthorized)
            ),
            "{name}: RoomService accepted a forged token"
        );
        assert_eq!(
            whip_status(&media, &room, token).await,
            axum::http::StatusCode::UNAUTHORIZED,
            "{name}: media endpoint accepted a forged token"
        );
    }
}
