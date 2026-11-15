//! Telemetry emission for the rooms gRPC surface (`testing.md` Telemetry Validation):
//! every RPC emits a `rooms.grpc` span carrying the op, the NUMERIC community (and tenant,
//! when the service JWT has one) and the outcome -- and nothing name-like: no room label,
//! participant id, or non-numeric request text ever becomes a span attribute. Counts are
//! printed; zero spans is a failure, never a pass.

mod otel_common;
mod rooms_common;

use std::collections::BTreeMap;

use serde_json::json;
use svc_streaming::rooms::grpc::pb::*;
use svc_streaming::rooms::grpc_auth::{SCOPE_MODERATE, SCOPE_READ, SCOPE_WRITE};

use otel_common::OtelSink;
use rooms_common::*;

type Attrs = BTreeMap<String, String>;

fn rooms_grpc_spans(sink: &OtelSink) -> Vec<(String, Attrs)> {
    sink.spans()
        .into_iter()
        .map(|s| {
            let attrs: Attrs = s
                .attributes
                .iter()
                .map(|kv| (kv.key.to_string(), kv.value.to_string()))
                .collect();
            (s.name.to_string(), attrs)
        })
        .collect()
}

fn find<'a>(
    spans: &'a [(String, Attrs)],
    name: &str,
    rpc: &str,
    community: Option<&str>,
) -> Option<&'a Attrs> {
    spans
        .iter()
        .filter(|(n, a)| n == name && a.get("rpc").map(String::as_str) == Some(rpc))
        .map(|(_, a)| a)
        .find(|a| a.get("community_id").map(String::as_str) == community)
}

#[tokio::test]
async fn rpc_spans_carry_numeric_ids_op_and_outcome_and_no_names() {
    let sink = OtelSink::install();
    let mut h = harness(42700, true).await;
    let write = jwt(&h.signing_pem, SCOPE_WRITE, |_| {});
    let read = jwt(&h.signing_pem, SCOPE_READ, |_| {});
    let moderate = jwt(&h.signing_pem, SCOPE_MODERATE, |_| {});
    let tenant_write = jwt(&h.signing_pem, SCOPE_WRITE, |c| c["tenant"] = json!("12"));
    let named_tenant = jwt(&h.signing_pem, SCOPE_READ, |c| {
        c["tenant"] = json!("system")
    });

    let label = "Zed Quincy private standup";
    let room = h
        .client
        .create_room(req(
            CreateRoomRequest {
                api_version: "v1".into(),
                community_id: "7".into(),
                label: Some(label.into()),
                ..Default::default()
            },
            &tenant_write,
        ))
        .await
        .unwrap()
        .into_inner()
        .room
        .unwrap();
    let participant = uid();
    h.client
        .join_room(req(
            JoinRoomRequest {
                api_version: "v1".into(),
                community_id: "7".into(),
                room_id: room.room_id.clone(),
                participant_id: participant.clone(),
                publish: false,
                subscribe: true,
                ttl_secs: None,
            },
            &write,
        ))
        .await
        .unwrap();
    let who = |c: &str, r: &str| RaiseHandRequest {
        api_version: "v1".into(),
        community_id: c.into(),
        room_id: r.into(),
        participant_id: participant.clone(),
        actor_id: participant.clone(),
    };
    h.client
        .raise_hand(req(who("7", &room.room_id), &write))
        .await
        .unwrap();
    // Refused calls: cross-community (NOT_FOUND), insufficient role (PERMISSION_DENIED),
    // wrong api_version (UNIMPLEMENTED), and a hostile non-numeric community string.
    h.client
        .raise_hand(req(who("8", &room.room_id), &write))
        .await
        .unwrap_err();
    h.client
        .acknowledge_hand(req(
            AcknowledgeHandRequest {
                api_version: "v1".into(),
                community_id: "7".into(),
                room_id: room.room_id.clone(),
                participant_id: participant.clone(),
                actor_id: uid(),
                actor_role: RoomRole::Member as i32,
            },
            &moderate,
        ))
        .await
        .unwrap_err();
    h.client
        .list_hands(req(
            ListHandsRequest {
                api_version: "v2".into(),
                community_id: "7".into(),
                room_id: room.room_id.clone(),
                actor_id: participant.clone(),
            },
            &read,
        ))
        .await
        .unwrap_err();
    h.client
        .list_hands(req(
            ListHandsRequest {
                api_version: "v1".into(),
                community_id: "alice@example.com".into(),
                room_id: room.room_id.clone(),
                actor_id: participant.clone(),
            },
            &named_tenant,
        ))
        .await
        .unwrap_err();

    let spans = rooms_grpc_spans(sink);
    let rpc_spans: Vec<_> = spans.iter().filter(|(n, _)| n == "rooms.grpc").collect();
    println!(
        "telemetry: {} span(s) exported, {} rooms.grpc span(s)",
        spans.len(),
        rpc_spans.len()
    );
    assert!(
        rpc_spans.len() >= 6,
        "denominator: expected >= 6 rooms.grpc spans, got {}",
        rpc_spans.len()
    );

    let create = find(&spans, "rooms.grpc", "CreateRoom", Some("7")).expect("CreateRoom span");
    assert_eq!(create.get("op").map(String::as_str), Some("CreateRoom"));
    assert_eq!(create.get("outcome").map(String::as_str), Some("ok"));
    assert_eq!(
        create.get("tenant_id").map(String::as_str),
        Some("12"),
        "numeric tenant claim"
    );

    let ok = find(&spans, "rooms.grpc", "RaiseHand", Some("7")).expect("RaiseHand span");
    assert_eq!(ok.get("outcome").map(String::as_str), Some("ok"));
    assert!(
        !ok.contains_key("tenant_id"),
        "no tenant claim -> no tenant attribute"
    );

    let cross = find(&spans, "rooms.grpc", "RaiseHand", Some("8")).expect("cross-community span");
    assert_eq!(cross.get("outcome").map(String::as_str), Some("notfound"));

    let denied = find(&spans, "rooms.grpc", "AcknowledgeHand", Some("7")).expect("ack span");
    assert_eq!(
        denied.get("outcome").map(String::as_str),
        Some("permissiondenied")
    );

    let version = find(&spans, "rooms.grpc", "ListHands", Some("7")).expect("api_version span");
    assert_eq!(
        version.get("outcome").map(String::as_str),
        Some("unimplemented")
    );

    // A non-numeric community string is never recorded; a non-numeric tenant claim neither.
    let hostile = find(&spans, "rooms.grpc", "ListHands", None).expect("hostile-community span");
    assert!(!hostile.contains_key("community_id"));
    assert!(
        !hostile.contains_key("tenant_id"),
        "tenant 'system' is not numeric"
    );

    // The service-level span of the hand op names the op and the numeric community.
    let inner = spans
        .iter()
        .find(|(n, a)| n == "raise_hand" && a.get("community_id").map(String::as_str) == Some("7"))
        .map(|(_, a)| a)
        .unwrap_or_else(|| {
            let names: Vec<_> = spans.iter().map(|(n, a)| format!("{n} {a:?}")).collect();
            panic!("RoomService raise_hand span missing; exported: {names:#?}")
        });
    assert_eq!(inner.get("op").map(String::as_str), Some("hand_raise"));

    // PII hygiene across EVERY exported span attribute: no label, no participant UUID, no
    // request text that looks like an address.
    let mut examined = 0;
    for (name, attrs) in &spans {
        for (k, v) in attrs {
            examined += 1;
            assert!(
                !v.contains("Zed Quincy"),
                "{name}.{k} leaked the room label: {v}"
            );
            assert!(
                !v.contains(&participant),
                "{name}.{k} leaked a participant id: {v}"
            );
            assert!(
                !v.contains("example.com"),
                "{name}.{k} leaked request text: {v}"
            );
        }
    }
    println!("telemetry: {examined} span attribute value(s) scanned for PII");
    assert!(examined > 0, "zero attributes examined is a failure");
}
