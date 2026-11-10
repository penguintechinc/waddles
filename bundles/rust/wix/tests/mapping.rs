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
fn member_events_carry_only_ids() {
    for (wix, kind) in [
        ("wix.members.v1.member_created", "member.linked"),
        ("wix.members.v1.member_updated", "member.updated"),
        ("wix.members.v1.member_deleted", "member.deleted"),
    ] {
        let ev = json!({"id": "e1", "entityId": "mem-9", "eventTime": "2026-11-05T12:00:00Z",
                        "createdEvent": {"entity": "{\"contact\":{\"firstName\":\"X\"},\"loginEmail\":\"a@b.c\"}"}});
        let m = one(&token("inst-0001", wix, &ev));
        assert_eq!((m.platform.as_str(), m.event_type.as_str()), ("wix", kind));
        assert_eq!(m.member.as_deref(), Some("mem-9"));
        assert_eq!(m.occurred_at, "2026-11-05T12:00:00Z");
        assert_eq!(
            payload(&m),
            json!({"event_id": "e1", "instance_id": "inst-0001", "time_source": "wix"})
        );
        assert!(!m.payload_json.contains("a@b.c") && !m.payload_json.contains("firstName"));
    }
}

fn order(status: &str) -> Value {
    json!({"order": {"_id": "ord-1", "planId": "plan-7", "status": status,
                      "buyer": {"memberId": "mem-9", "contactId": "c-1"}}})
}

#[test]
fn subscription_started_changed_ended() {
    let act = |t: &str, st: &str| {
        let ev = domain("e2", "actionEvent", "body", &order(st));
        parse_frame(&token("inst-0001", t, &ev), RECV).expect("parses")
    };
    for t in ["order_purchased", "order_started", "order_resumed"] {
        let p = act(&format!("wix.pricing_plans.v2.{t}"), "ACTIVE");
        let m = &p.events[0];
        assert_eq!(m.event_type, "subscription.started", "{t}");
        assert_eq!(
            payload(m),
            json!({"event_id":"e2","instance_id":"inst-0001","time_source":"wix","order_id":"ord-1","plan_id":"plan-7","status":"ACTIVE"})
        );
        assert_eq!(m.member.as_deref(), Some("mem-9"));
    }
    // A purchase that has not started (PENDING) must not grant anything yet.
    let p = act("wix.pricing_plans.v2.order_purchased", "PENDING");
    assert_eq!((p.events.len(), p.skipped), (0, 1));
    let p = act("wix.pricing_plans.v2.order_updated", "ACTIVE");
    assert_eq!(p.events[0].event_type, "subscription.changed");
    for (t, reason, st) in [
        ("order_ended", "ended", "ENDED"),
        ("order_canceled", "canceled", "CANCELED"),
        ("order_paused", "paused", "PAUSED"),
    ] {
        let p = act(&format!("wix.pricing_plans.v2.{t}"), st);
        assert_eq!(p.events[0].event_type, "subscription.ended");
        assert_eq!(payload(&p.events[0])["reason"], reason);
    }
}

#[test]
fn order_entity_form_and_unknown_status() {
    // order_updated is a domain update event: the order is the entity itself.
    let o = json!({"_id": "ord-2", "planId": "plan-8", "status": "weird", "buyer": {"memberId": "mem-3"}});
    let ev = domain("e3", "updatedEvent", "currentEntity", &o);
    let m = one(&token(
        "inst-0001",
        "wix.pricing_plans.v2.order_updated",
        &ev,
    ));
    assert_eq!(payload(&m)["status"], "UNDEFINED");
    assert_eq!(payload(&m)["plan_id"], "plan-8");
}

#[test]
fn orders_without_a_member_or_with_other_kinds_are_skipped() {
    let mut o = order("ACTIVE");
    o["order"]["buyer"] = json!({"contactId": "c-1"});
    let ev = domain("e4", "actionEvent", "body", &o);
    let p = parse_frame(
        &token("inst-0001", "wix.pricing_plans.v2.order_started", &ev),
        RECV,
    )
    .expect("ok");
    assert_eq!((p.events.len(), p.skipped), (0, 1));
    for t in [
        "order_created",
        "order_cycle_started",
        "order_auto_renew_canceled",
    ] {
        let ev = domain("e5", "actionEvent", "body", &order("ACTIVE"));
        let p = parse_frame(
            &token("inst-0001", &format!("wix.pricing_plans.v2.{t}"), &ev),
            RECV,
        )
        .expect("ok");
        assert_eq!((p.events.len(), p.skipped), (0, 1), "{t}");
    }
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
        assert_eq!((m.event_type.as_str(), m.member.is_none()), (kind, true));
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
        "wix.members.v1.member_created",
        &json!({"id":"e","entityId":"m"}),
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
            "wix.members.v1.member_created",
            &json!({"id":"e","entityId":"m"}),
        ),
        token(
            "inst bad",
            "wix.members.v1.member_created",
            &json!({"id":"e","entityId":"m"}),
        ),
        token(
            "inst-0001",
            "wix.members.v1.member_created",
            &json!({"entityId":"m"}),
        ),
        token(
            "inst-0001",
            "wix.members.v1.member_created",
            &json!({"id":"e"}),
        ),
        token(
            "inst-0001",
            "wix.members.v1.member_created",
            &json!({"id":"e","entityId":"m/../x"}),
        ),
        token(
            "inst-0001",
            "wix.pricing_plans.v2.order_started",
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
    let o = json!({"order": {"_id": "o\"1", "planId": "p", "status": "ACTIVE", "buyer": {"memberId": "m"}}});
    let ev = domain("e12", "actionEvent", "body", &o);
    assert!(
        parse_frame(
            &token("inst-0001", "wix.pricing_plans.v2.order_started", &ev),
            RECV
        )
        .is_err()
    );
}
