//! Golden parity vs the Python path (`spectrum_ingest.normalize` /
//! `spectrum_poll._item_from_mapping`) using the shared corpus plus envelope cases.
//! Divergence by design: no raw member id / display name in the payload.

use serde_json::{Value, json};
use waddle_bundle_spectrum_service::logic::{LogicError, epoch_to_iso, parse_frame};

const RX: &str = "2026-11-05T00:00:00.000Z";
const SRC: &str = "poll-opaque-1";

fn corpus(name: &str) -> Value {
    let path = format!(
        "{}/../../../tests/data_plane_parity/corpus/spectrum/{name}.json",
        env!("CARGO_MANIFEST_DIR")
    );
    serde_json::from_slice(&std::fs::read(path).expect("corpus present")).expect("corpus JSON")
}

fn payload(v: &waddle_bundle_spectrum_service::logic::Event) -> Value {
    serde_json::from_str(&v.payload_json).expect("payload JSON")
}

/// The golden payload minus the PII keys the v3 boundary removes.
fn without(mut golden: Value, keys: &[&str]) -> Value {
    for k in keys {
        golden.as_object_mut().expect("object").remove(*k);
    }
    golden
}

#[test]
fn corpus_lobby_message_matches_golden_modulo_pii() {
    let v = corpus("lobby_message");
    let bytes = serde_json::to_vec(&v["frame"]).expect("ser");
    let p = parse_frame(&bytes, RX, SRC).expect("parses");
    assert_eq!(p.events.len(), 1);
    let e = &p.events[0];
    assert_eq!(e.platform, v["golden"]["platform"]);
    assert_eq!(e.event_type, v["golden"]["event_type"]);
    assert_eq!(e.actor.as_deref(), v["golden"]["actor"].as_str());
    assert_eq!(
        payload(e),
        without(
            v["golden"]["payload"].clone(),
            &["author_id", "display_name"]
        )
    );
    assert_eq!(e.occurred_at, "2026-09-11T00:00:00Z");
}

#[test]
fn corpus_roster_joined_matches_golden_modulo_pii() {
    let v = corpus("roster_joined");
    let bytes = serde_json::to_vec(&v["frame"]).expect("ser");
    let e = &parse_frame(&bytes, RX, SRC).expect("parses").events[0];
    assert_eq!(e.event_type, "member_joined");
    assert_eq!(e.actor.as_deref(), Some("member-1"));
    assert_eq!(
        payload(e),
        without(
            v["golden"]["payload"].clone(),
            &["member_id", "display_name"]
        )
    );
    assert_eq!(e.occurred_at, "2026-09-11T00:00:00Z");
}

#[test]
fn no_raw_handle_or_display_name_in_any_payload() {
    for name in ["lobby_message", "roster_joined"] {
        let v = corpus(name);
        let bytes = serde_json::to_vec(&v["frame"]).expect("ser");
        let e = &parse_frame(&bytes, RX, SRC).expect("parses").events[0];
        for needle in ["SomeCitizen", "NewRecruit", "spectrum-user-1", "member-1"] {
            assert!(!e.payload_json.contains(needle), "{name} leaked {needle}");
        }
    }
}

fn env(data: Value) -> Vec<u8> {
    serde_json::to_vec(&json!({"success": 1, "code": "OK", "data": data})).expect("ser")
}

#[test]
fn lobby_envelope_maps_orders_and_skips() {
    let frame = env(json!({"messages": [
        {"id": 2, "plaintext": "  second ", "time_created": 1789000001, "member": {"id": 77, "displayname": "Handle"}},
        {"id": "m1", "body": "first", "time_created": "1789000000.5", "member_id": "m-9"},
        {"id": "nope"},
        {"plaintext": "no id"},
        "not-an-object"
    ]}));
    let p = parse_frame(&frame, RX, SRC).expect("parses");
    assert_eq!(p.skipped, 3);
    assert_eq!(p.events.len(), 2);
    let (a, b) = (&p.events[0], &p.events[1]);
    assert_eq!(a.event_type, "message");
    assert_eq!(a.actor.as_deref(), Some("m-9"));
    assert_eq!(
        payload(a),
        json!({"text": "first", "kind": "lobby", "source_id": SRC, "thread_id": null,
               "message_id": "m1", "created_at": "2026-09-10T00:26:40.500000+00:00"})
    );
    assert_eq!(a.occurred_at, "2026-09-10T00:26:40.500000+00:00");
    assert_eq!(b.actor.as_deref(), Some("77"));
    assert_eq!(payload(&b.clone())["text"], "second");
    assert!(!b.payload_json.contains("Handle"));
}

#[test]
fn forum_envelope_thread_vs_reply() {
    let frame = env(json!({"threads": [
        {"id": "t1", "subject": "Recruiting", "time_created": 1},
        {"id": "t2", "plaintext": "re", "parent_id": "t1", "time_created": 2},
        {"id": "t3", "plaintext": "flag", "is_reply": true, "time_created": 3},
        {"id": "t4", "plaintext": "stringly", "is_reply": "false", "time_created": 4}
    ]}));
    let p = parse_frame(&frame, RX, SRC).expect("parses");
    let kinds: Vec<_> = p.events.iter().map(|e| e.event_type.as_str()).collect();
    // Strict is_reply: the string "false" is NOT a reply (Python bool() would say it is).
    assert_eq!(kinds, ["thread", "reply", "reply", "thread"]);
}

#[test]
fn missing_time_created_falls_back_to_received_at() {
    let frame = env(json!({"messages": [{"id": "m", "plaintext": "x"}]}));
    let e = &parse_frame(&frame, RX, SRC).expect("parses").events[0];
    assert_eq!(e.occurred_at, RX);
    assert_eq!(payload(e)["created_at"], Value::Null);
}

#[test]
fn epoch_iso_matches_python_isoformat() {
    for (x, want) in [
        (0.0, "1970-01-01T00:00:00+00:00"),
        (1_789_000_000.0, "2026-09-10T00:26:40+00:00"),
        (1_789_000_000.5, "2026-09-10T00:26:40.500000+00:00"),
        (1_789_000_000.123_456, "2026-09-10T00:26:40.123456+00:00"),
        (1_789_000_000.999_999_5, "2026-09-10T00:26:41+00:00"),
        (253_402_300_799.0, "9999-12-31T23:59:59+00:00"),
        (951_782_400.0, "2000-02-29T00:00:00+00:00"),
        (1_709_164_800.0, "2024-02-29T00:00:00+00:00"),
    ] {
        assert_eq!(epoch_to_iso(x).as_deref(), Some(want), "{x}");
    }
    for bad in [-1.0, f64::NAN, f64::INFINITY, 253_402_300_800.0] {
        assert_eq!(epoch_to_iso(bad), None);
    }
}

#[test]
fn org_event_change_normalizes_and_drops_organizer_id() {
    let frame = serde_json::to_vec(&json!({
        "kind": "events", "source_id": "org-1", "change": "rsvp_changed", "event_id": "e1",
        "title": "Op", "description": "d".repeat(3000), "organizer_id": "org-9",
        "rsvp_count": 5, "rsvp_previous": 3, "observed_at": "2026-09-11T00:00:00Z"
    }))
    .expect("ser");
    let e = &parse_frame(&frame, RX, SRC).expect("parses").events[0];
    assert_eq!(e.event_type, "org_event_rsvp_changed");
    assert_eq!(e.actor.as_deref(), Some("org-9"));
    let p = payload(e);
    assert_eq!(
        p["description"].as_str().expect("str").chars().count(),
        2000
    );
    assert_eq!(
        (p["rsvp_count"].clone(), p["rsvp_previous"].clone()),
        (json!(5), json!(3))
    );
    assert!(p.get("organizer_id").is_none());
}

#[test]
fn every_roster_and_event_change_maps() {
    for (c, t) in [
        ("joined", "member_joined"),
        ("left", "member_left"),
        ("roles_changed", "member_roles_changed"),
    ] {
        let f = serde_json::to_vec(
            &json!({"kind":"roster","source_id":"o","change":c,"member_id":"m"}),
        )
        .expect("ser");
        assert_eq!(
            parse_frame(&f, RX, SRC).expect("ok").events[0].event_type,
            t
        );
    }
    for (c, t) in [
        ("created", "org_event_created"),
        ("updated", "org_event_updated"),
        ("cancelled", "org_event_cancelled"),
        ("removed", "org_event_removed"),
        ("rsvp_changed", "org_event_rsvp_changed"),
    ] {
        let f =
            serde_json::to_vec(&json!({"kind":"events","source_id":"o","change":c,"event_id":"e"}))
                .expect("ser");
        let e = &parse_frame(&f, RX, SRC).expect("ok").events[0];
        assert_eq!((e.event_type.as_str(), e.actor.clone()), (t, None));
        assert_eq!(e.occurred_at, RX);
    }
}

fn malformed(bytes: &[u8]) -> String {
    match parse_frame(bytes, RX, SRC) {
        Err(LogicError::Malformed(m)) => m,
        other => panic!("expected Malformed, got {other:?}"),
    }
}

#[test]
fn fail_loud_on_bad_frames() {
    assert!(malformed(b"not json").contains("not JSON"));
    assert!(malformed(b"[1]").contains("not a JSON object"));
    assert!(malformed(b"{}").contains("neither"));
    assert!(
        malformed(br#"{"success":0,"code":"ErrApiLoginRequired"}"#).contains("ErrApiLoginRequired")
    );
    assert!(malformed(br#"{"success":0}"#).contains("code=unknown"));
    assert!(malformed(br#"{"success":1}"#).contains("no 'data'"));
    assert!(malformed(br#"{"success":1,"data":{"threads":[],"messages":[]}}"#).contains("both"));
    assert!(malformed(br#"{"kind":"lobby","source_id":"s","text":"  "}"#).contains("'text'"));
    assert!(malformed(br#"{"kind":"lobby","text":"x"}"#).contains("'source_id'"));
    assert!(malformed(br#"{"kind":"dm","text":"x","source_id":"s"}"#).contains("'kind'"));
    assert!(
        malformed(br#"{"kind":"roster","source_id":"s","change":"x","member_id":"m"}"#)
            .contains("change")
    );
    assert!(
        malformed(br#"{"kind":"roster","source_id":"s","change":"left"}"#).contains("member_id")
    );
    assert!(
        malformed(br#"{"kind":"events","source_id":"s","change":"x","event_id":"e"}"#)
            .contains("change")
    );
    assert!(
        malformed(br#"{"kind":"events","source_id":"s","change":"created"}"#).contains("event_id")
    );
}

#[test]
fn snapshot_envelope_is_unsupported_not_silent() {
    match parse_frame(br#"{"success":1,"data":{"members":[]}}"#, RX, SRC) {
        Err(LogicError::Unsupported(m)) => assert!(m.contains("snapshot")),
        other => panic!("expected Unsupported, got {other:?}"),
    }
}
