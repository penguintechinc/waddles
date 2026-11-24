//! Event mapping for the Wix connector: envelope decode, every documented event
//! family, skip rules and hostile-input rejection. The signature segment is a
//! placeholder: the host verifies it, the bundle never does.

use serde_json::{Value, json};
use waddle_bundle_wix_service::logic::{LogicError, Mapped, parse_frame};

const RECV: &str = "2026-11-06T00:00:00.000Z";

fn b64(s: &str) -> String {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = String::new();
    for ch in s.as_bytes().chunks(3) {
        let n = ch
            .iter()
            .enumerate()
            .fold(0u32, |a, (i, b)| a | (u32::from(*b) << (16 - 8 * i)));
        for i in 0..=ch.len() {
            out.push(char::from(T[((n >> (18 - 6 * i)) & 63) as usize]));
        }
    }
    out
}

fn token(instance: &str, event_type: &str, event: &Value) -> Vec<u8> {
    let env = json!({
        "instanceId": instance,
        "eventType": event_type,
        "identity": "{}",
        "data": event.to_string(),
    });
    let payload = json!({"data": env.to_string(), "iat": 1});
    format!(
        "{}.{}.AAAA",
        b64(r#"{"alg":"RS256"}"#),
        b64(&payload.to_string())
    )
    .into_bytes()
}

fn domain(id: &str, key: &str, inner_key: &str, entity: &Value) -> Value {
    json!({
        "id": id, "entityFqdn": "x", "slug": "s", "entityId": "ent-1",
        "eventTime": "2026-11-05T12:00:00.000Z",
        key: { inner_key: entity.to_string() },
    })
}

fn one(t: &[u8]) -> Mapped {
    let p = parse_frame(t, RECV).expect("parses");
    assert_eq!((p.events.len(), p.skipped), (1, 0));
    p.events.into_iter().next().expect("one event")
}

fn payload(m: &Mapped) -> Value {
    serde_json::from_str(&m.payload_json).expect("payload JSON")
}

#[test]
fn member_webhooks_produce_zero_records() {
    for wix in [
        "wix.members.v1.member_created",
        "wix.members.v1.member_updated",
        "wix.members.v1.member_deleted",
    ] {
        let ev = json!({"id": "e1", "entityId": "mem-9", "eventTime": "2026-11-05T12:00:00Z",
                        "createdEvent": {"entity": "{\"contact\":{\"firstName\":\"X\"},\"loginEmail\":\"a@b.c\"}"}});
        let p = parse_frame(&token("inst-0001", wix, &ev), RECV).expect("ok");
        assert_eq!((p.events.len(), p.skipped), (0, 1), "{wix}");
    }
}

#[test]
fn manifest_does_not_request_pii_read() {
    for f in ["bundle.yaml", "hub-manifest.yaml"] {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join(f);
        let text = std::fs::read_to_string(&path).expect("manifest");
        assert!(text.contains("permissions:"), "{f}: no permissions block");
        assert!(
            !text.contains("connector.pii.read"),
            "{f} still requests pii.read"
        );
        assert!(
            !text.contains("identity.lookup"),
            "{f} still mentions identity.lookup"
        );
    }
}

fn order(status: &str) -> Value {
    json!({"order": {"_id": "ord-1", "planId": "plan-7", "status": status,
                      "buyer": {"memberId": "mem-9", "contactId": "c-1"}}})
}

/// Wix syncs EVENTS only (decision 2026-11-12): every pricing-plan / order /
/// subscription webhook is ignored and produces no record, whatever its shape.
#[test]
fn subscription_webhooks_are_ignored_and_emit_no_records() {
    let kinds = [
        "order_purchased",
        "order_started",
        "order_resumed",
        "order_updated",
        "order_marked_as_paid",
        "order_ended",
        "order_canceled",
        "order_paused",
        "order_created",
        "order_cycle_started",
        "order_auto_renew_canceled",
        "plan_created",
        "plan_updated",
    ];
    let bodies = [
        domain("e2", "actionEvent", "body", &order("ACTIVE")),
        domain(
            "e3",
            "updatedEvent",
            "currentEntity",
            &order("ACTIVE")["order"],
        ),
        // Even a hostile/malformed body is not parsed at all.
        json!({"id": "e4"}),
        domain(
            "e5",
            "actionEvent",
            "body",
            &json!({"order": {"_id": "o\"1", "planId": "p", "status": "ACTIVE", "buyer": {"memberId": "m"}}}),
        ),
    ];
    let mut checked = 0;
    for t in kinds {
        for ev in &bodies {
            let p = parse_frame(
                &token("inst-0001", &format!("wix.pricing_plans.v2.{t}"), ev),
                RECV,
            )
            .expect("ignored, not an error");
            assert_eq!((p.events.len(), p.skipped), (0, 1), "{t}");
            assert!(
                p.events
                    .iter()
                    .all(|m| !m.event_type.starts_with("subscription."))
            );
            checked += 1;
        }
    }
    assert_eq!(checked, kinds.len() * bodies.len());
}

#[test]
fn event_records_still_flow_alongside_ignored_subscriptions() {
    let e = json!({"_id": "wev-1", "title": "Meetup"});
    let ev = domain("e6", "createdEvent", "entity", &e);
    let m = one(&token("inst-0001", "wix.events.v3.event_created", &ev));
    assert_eq!(m.event_type, "event.created");
    let sub = domain("e7", "actionEvent", "body", &order("ACTIVE"));
    let p = parse_frame(
        &token("inst-0001", "wix.pricing_plans.v2.order_started", &sub),
        RECV,
    )
    .expect("ok");
    assert!(p.events.is_empty());
}

#[test]
fn calendar_events_map_without_an_actor() {
    let e = json!({"_id": "wev-1", "title": "Mee\u{0}tup", "status": "UPCOMING",
        "dateAndTimeSettings": {"startDate": "2026-12-01T18:00:00Z", "endDate": "2026-12-01T20:00:00Z", "timeZoneId": "Europe/Berlin"},
        "location": {"name": "Hall A"}, "description": "never forwarded"});
    for (t, kind) in [
        ("event_created", "event.created"),
        ("event_updated", "event.updated"),
        ("event_canceled", "event.canceled"),
    ] {
        let ev = domain("e6", "createdEvent", "entity", &e);
        let m = one(&token("inst-0001", &format!("wix.events.v3.{t}"), &ev));
        assert_eq!(m.event_type.as_str(), kind);
        assert_eq!(
            payload(&m),
            json!({"event_id":"e6","instance_id":"inst-0001","time_source":"wix","wix_event_id":"wev-1","title":"Meetup",
                   "starts_at":"2026-12-01T18:00:00Z","ends_at":"2026-12-01T20:00:00Z",
                   "timezone":"Europe/Berlin","location":"Hall A","status":"UPCOMING"})
        );
    }
    // Deleted maps to canceled with status DELETED; action body may wrap `{event}`.
    let ev = domain("e7", "actionEvent", "body", &json!({"event": e}));
    let m = one(&token("inst-0001", "wix.events.v3.event_canceled", &ev));
    assert_eq!(payload(&m)["wix_event_id"], "wev-1");
    let ev = domain("e8", "deletedEvent", "deletedEntity", &e);
    let m = one(&token("inst-0001", "wix.events.v3.event_deleted", &ev));
    assert_eq!(
        (m.event_type.as_str(), payload(&m)["status"].as_str()),
        ("event.canceled", Some("DELETED"))
    );
}

#[test]
fn missing_optional_event_fields_become_null_and_long_text_is_capped() {
    let long = "a".repeat(1000);
    let e = json!({"_id": "wev-2", "title": long});
    let ev = domain("e9", "createdEvent", "entity", &e);
    let m = one(&token("inst-0001", "wix.events.v3.event_created", &ev));
    let p = payload(&m);
    assert_eq!(p["title"].as_str().map(str::len), Some(256));
    assert!(p["starts_at"].is_null() && p["location"].is_null() && p["status"].is_null());
    // eventTime absent -> the frame's received_at.
    let mut ev = domain("e10", "createdEvent", "entity", &e);
    ev.as_object_mut().expect("obj").remove("eventTime");
    let fallback = one(&token("inst-0001", "wix.events.v3.event_created", &ev));
    assert_eq!(fallback.occurred_at, RECV);
    // The receive-time fallback is flagged non-orderable so consumers never compare clocks.
    assert_eq!(payload(&fallback)["time_source"], "received");
}

#[test]
fn unknown_event_types_are_skipped_not_errors() {
    let ev = domain("e11", "createdEvent", "entity", &json!({}));
    for t in [
        "wix.stores.v1.product_created",
        "wix.members.v1.member_blocked",
        "wix.events.v3.event_ended",
        "wix.events.v2.rsvp_created",
        "x",
    ] {
        let p = parse_frame(&token("inst-0001", t, &ev), RECV).expect("ok");
        assert_eq!((p.events.len(), p.skipped), (0, 1), "{t}");
    }
}

#[test]
fn malformed_frames_fail_loud() {
    let good = String::from_utf8(token(
        "inst-0001",
        "wix.events.v3.event_created",
        &json!({"id":"e","createdEvent":{"entity":"{\"_id\":\"ev-1\"}"}}),
    ))
    .expect("utf8");
    let bad_frames: Vec<Vec<u8>> = vec![
        vec![],
        b"a.b".to_vec(),
        b"a.b.c.d".to_vec(),
        b"a.!!!.c".to_vec(),
        vec![0xff, 0xfe],
        format!("{}.{}.x", b64("{}"), b64("not json")).into_bytes(),
        format!("{}.{}.x", b64("{}"), b64(r#"{"data":"{}"}"#)).into_bytes(),
        token(
            "",
            "wix.events.v3.event_created",
            &json!({"id":"e","createdEvent":{"entity":"{\"_id\":\"ev-1\"}"}}),
        ),
        token(
            "inst bad",
            "wix.events.v3.event_created",
            &json!({"id":"e","createdEvent":{"entity":"{\"_id\":\"ev-1\"}"}}),
        ),
        token(
            "inst-0001",
            "wix.events.v3.event_created",
            &json!({"createdEvent":{"entity":"{\"_id\":\"ev-1\"}"}}),
        ),
        token(
            "inst-0001",
            "wix.events.v3.event_created",
            &json!({"id":"e","createdEvent":{"entity":"{\"_id\":\"ev/../x\"}"}}),
        ),
        token(
            "inst-0001",
            "wix.events.v3.event_created",
            &json!({"id":"e"}),
        ),
        token(
            "inst-0001",
            "wix.events.v3.event_created",
            &json!({"id":"e","createdEvent":{"entity":"{}"}}),
        ),
        token(
            "inst-0001",
            "wix.events.v3.event_created",
            &json!({"id":"e","createdEvent":{"entity":"nope"}}),
        ),
    ];
    for f in &bad_frames {
        assert!(
            matches!(parse_frame(f, RECV), Err(LogicError::Malformed(_))),
            "{:?}",
            String::from_utf8_lossy(f)
        );
    }
    assert!(parse_frame(good.as_bytes(), RECV).is_ok());
    // A trailing newline from the transport is tolerated.
    assert!(parse_frame(format!("{good}\n").as_bytes(), RECV).is_ok());
}

#[test]
fn hostile_ids_never_reach_the_payload() {
    let e = json!({"_id": "w\"1", "title": "x"});
    let ev = domain("e12", "createdEvent", "entity", &e);
    assert!(
        parse_frame(
            &token("inst-0001", "wix.events.v3.event_created", &ev),
            RECV
        )
        .is_err()
    );
}
