//! Golden parity vs the Python path: `normalize()` in `youtube_live_ingest.py` and
//! `_parse_message` in `youtube_live_poll.py`.
//!
//! Cases: the shared corpus vector plus extra goldens built from the Python tests'
//! fixtures. Expectations were cross-checked against the real Python `normalize()`
//! (see README, Parity evidence).

use serde_json::{Value, json};
use waddle_bundle_youtube_service::logic::{LogicError, normalize_raw, parse_frame};

const RECEIVED_AT: &str = "2026-11-05T00:00:00.000Z";

fn payload(e: &waddle_bundle_youtube_service::logic::Event) -> Value {
    serde_json::from_str(&e.payload_json).expect("payload is JSON")
}

/// The shared corpus vector: receiver-raw frame -> golden event.
#[test]
fn corpus_live_chat_message_matches_golden() {
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../tests/data_plane_parity/corpus/youtube/live_chat_message.json"
    );
    let v: Value =
        serde_json::from_slice(&std::fs::read(path).expect("corpus present")).expect("corpus JSON");
    let frame = serde_json::to_vec(&v["frame"]).expect("frame");
    let parsed = parse_frame(&frame, RECEIVED_AT).expect("parses");
    assert_eq!(parsed.events.len(), 1);
    let e = &parsed.events[0];
    assert_eq!(e.platform, v["golden"]["platform"]);
    assert_eq!(e.event_type, v["golden"]["event_type"]);
    assert_eq!(e.actor.as_deref(), v["golden"]["actor"].as_str());
    assert_eq!(payload(e), v["golden"]["payload"]);
    // occurred_at = the raw event's published_at (Python: raw.occurred_at or published_at).
    assert_eq!(e.occurred_at, "2026-09-11T00:00:00Z");
}

fn raw(extra: Value) -> Vec<u8> {
    let mut base = json!({"platform": "youtube", "text": "hello", "live_chat_id": "chat123"});
    base.as_object_mut()
        .expect("object")
        .extend(extra.as_object().expect("object").clone());
    serde_json::to_vec(&base).expect("ser")
}

/// Mirrors `test_builtin_youtube_live_ingest.py` one-for-one (raw-event path).
#[test]
fn python_normalize_cases() {
    // author_id missing -> actor "unknown", optional fields null.
    let p = parse_frame(&raw(json!({})), RECEIVED_AT).unwrap();
    let e = &p.events[0];
    assert_eq!(e.actor.as_deref(), Some("unknown"));
    assert_eq!(
        payload(e),
        json!({"text": "hello", "video_id": null, "live_chat_id": "chat123",
               "author_id": null, "display_name": null, "is_mod": false,
               "is_owner": false, "is_sponsor": false, "message_id": null,
               "published_at": null})
    );
    // occurred_at falls back to received_at ("now") when nothing else is present.
    assert_eq!(e.occurred_at, RECEIVED_AT);

    // published_at fallback; explicit occurred_at overrides.
    let p = parse_frame(
        &raw(json!({"published_at": "2026-01-01T00:00:00Z"})),
        RECEIVED_AT,
    )
    .unwrap();
    assert_eq!(p.events[0].occurred_at, "2026-01-01T00:00:00Z");
    let p = parse_frame(
        &raw(
            json!({"published_at": "2026-01-01T00:00:00Z", "occurred_at": "2026-02-02T00:00:00Z"}),
        ),
        RECEIVED_AT,
    )
    .unwrap();
    assert_eq!(p.events[0].occurred_at, "2026-02-02T00:00:00Z");

    // Truthy non-bool flags fail closed (badge-truthiness regression).
    let p = parse_frame(
        &raw(json!({"is_mod": 1, "is_owner": "yes", "is_sponsor": "true"})),
        RECEIVED_AT,
    )
    .unwrap();
    let pl = payload(&p.events[0]);
    assert_eq!(
        (
            pl["is_mod"].clone(),
            pl["is_owner"].clone(),
            pl["is_sponsor"].clone()
        ),
        (json!(false), json!(false), json!(false))
    );
    // Real booleans grant.
    let p = parse_frame(&raw(json!({"is_mod": true})), RECEIVED_AT).unwrap();
    assert_eq!(payload(&p.events[0])["is_mod"], json!(true));

    // platform defaults to youtube; explicit platform preserved.
    let mut v: Value = serde_json::from_slice(&raw(json!({}))).unwrap();
    v.as_object_mut().unwrap().remove("platform");
    let p = parse_frame(&serde_json::to_vec(&v).unwrap(), RECEIVED_AT).unwrap();
    assert_eq!(p.events[0].platform, "youtube");

    // Whitespace: stripped (incl. Python-only 0x1c..0x1f); whitespace-only text passes the
    // non-empty gate and becomes "" exactly like Python.
    let p = parse_frame(&raw(json!({"text": "\u{1c}  hi there \n"})), RECEIVED_AT).unwrap();
    assert_eq!(payload(&p.events[0])["text"], "hi there");
    let p = parse_frame(&raw(json!({"text": "   "})), RECEIVED_AT).unwrap();
    assert_eq!(payload(&p.events[0])["text"], "");
}

#[test]
fn python_normalize_rejections() {
    for bad in [
        json!({"text": null}),
        json!({"text": ""}),
        json!({"text": 5}),
        json!({"live_chat_id": ""}),
        json!({"live_chat_id": null}),
    ] {
        let r = parse_frame(&raw(bad.clone()), RECEIVED_AT);
        assert!(matches!(r, Err(LogicError::Malformed(_))), "{bad}");
    }
    // Missing keys entirely.
    let mut m = serde_json::Map::new();
    m.insert("text".into(), json!("x"));
    assert!(matches!(
        normalize_raw(&m, RECEIVED_AT),
        Err(LogicError::Malformed(_))
    ));
}

fn list_item(text: &str, id: &str) -> Value {
    json!({
        "id": id,
        "snippet": {"displayMessage": text, "publishedAt": "2026-09-11T00:00:00Z", "liveChatId": "chat123"},
        "authorDetails": {"channelId": "UCviewer1", "displayName": "Viewer One",
            "isChatModerator": false, "isChatOwner": false, "isChatSponsor": false}
    })
}

/// A real `liveChat/messages` response (Python `_chat_message`/`_messages` shape) yields
/// the same events the receiver+normalize pair did (video_id is host config -> null).
#[test]
fn list_response_matches_python_receiver_then_normalize() {
    let body = json!({
        "kind": "youtube#liveChatMessageListResponse",
        "pollingIntervalMillis": 5000,
        "nextPageToken": "TOK",
        "items": [list_item("  hello  ", "msg-1"), list_item("second", "msg-2")]
    });
    let p = parse_frame(&serde_json::to_vec(&body).unwrap(), RECEIVED_AT).unwrap();
    assert_eq!(p.events.len(), 2);
    assert_eq!(p.skipped, 0);
    let e = &p.events[0];
    assert_eq!(e.actor.as_deref(), Some("UCviewer1"));
    assert_eq!(e.occurred_at, "2026-09-11T00:00:00Z");
    assert_eq!(
        payload(e),
        json!({"text": "hello", "video_id": null, "live_chat_id": "chat123",
               "author_id": "UCviewer1", "display_name": "Viewer One", "is_mod": false,
               "is_owner": false, "is_sponsor": false, "message_id": "msg-1",
               "published_at": "2026-09-11T00:00:00Z"})
    );
    assert_eq!(payload(&p.events[1])["message_id"], "msg-2");
}

/// Items with no displayable text are skipped, never raised; missing authorDetails
/// defaults safely (Python `test_message_without_display_message_is_skipped`,
/// `test_missing_author_details_default_safely`).
#[test]
fn list_response_skips_and_defaults() {
    let body = json!({"items": [
        {"id": "a", "snippet": {"liveChatId": "chat123"}},
        {"id": "b", "snippet": {"displayMessage": "", "liveChatId": "chat123"}},
        {"id": "c", "snippet": {"displayMessage": "no chat id"}},
        {"id": "d", "snippet": {"displayMessage": "ok", "liveChatId": "chat123"}},
        "not-an-object"
    ]});
    let p = parse_frame(&serde_json::to_vec(&body).unwrap(), RECEIVED_AT).unwrap();
    assert_eq!(p.skipped, 4);
    assert_eq!(p.events.len(), 1);
    let e = &p.events[0];
    assert_eq!(e.actor.as_deref(), Some("unknown"));
    assert_eq!(e.occurred_at, RECEIVED_AT);
    let pl = payload(e);
    assert_eq!(pl["author_id"], Value::Null);
    assert_eq!(pl["display_name"], Value::Null);
    assert_eq!(pl["is_mod"], json!(false));
}

/// Super Chat / membership items use `displayMessage` too and normalize as `message`
/// (the Python path never branched on `snippet.type`).
#[test]
fn non_text_message_types_normalize_as_message() {
    let mut item = list_item("$5.00 from Alice!", "sc-1");
    item["snippet"]["type"] = json!("superChatEvent");
    let p = parse_frame(
        &serde_json::to_vec(&json!({"items": [item]})).unwrap(),
        RECEIVED_AT,
    )
    .unwrap();
    assert_eq!(p.events[0].event_type, "message");
}

#[test]
fn empty_items_is_zero_events_not_error() {
    let p = parse_frame(br#"{"items": []}"#, RECEIVED_AT).unwrap();
    assert!(p.events.is_empty());
}

#[test]
fn malformed_frames_fail_loud() {
    for bad in [
        &b"not json"[..],
        b"[1,2]",
        br#"{"items": "x"}"#,
        br#"{"something": "else"}"#,
    ] {
        assert!(
            matches!(parse_frame(bad, RECEIVED_AT), Err(LogicError::Malformed(_))),
            "{}",
            String::from_utf8_lossy(bad)
        );
    }
}

/// Logs/errors must stay PII-free: error text never echoes frame content.
#[test]
fn errors_do_not_echo_content() {
    let r = parse_frame(
        br#"{"text": "", "secret_marker": "SECRETXYZ"}"#,
        RECEIVED_AT,
    );
    let LogicError::Malformed(m) = r.unwrap_err() else {
        panic!("expected malformed");
    };
    assert!(!m.contains("SECRETXYZ"));
}
