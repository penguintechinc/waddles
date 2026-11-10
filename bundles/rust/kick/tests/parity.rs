//! Golden parity against the legacy Python receiver: every
//! `tests/data_plane_parity/corpus/kick/*.json` vector (receiver-raw dict + the golden the real
//! Python `KickPusherReceiver` -> `kick_ingest.normalize` path produced) is turned back into the
//! Pusher wire frame that receiver would have consumed, run through the bundle, and compared
//! with the golden modulo the DECLARED divergences below (each must actually apply -- a stale
//! declaration fails).

use std::fs;
use std::path::Path;

use serde_json::{Map, Value, json};
use waddle_bundle_kick_service::pusher;

const TS: &str = "2026-11-07T00:00:00.000Z";

fn vectors() -> Vec<Value> {
    let dir =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../tests/data_plane_parity/corpus/kick");
    let mut files: Vec<_> = fs::read_dir(&dir)
        .unwrap_or_else(|e| panic!("read {dir:?}: {e}"))
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().and_then(|e| e.to_str()) == Some("json"))
        .collect();
    files.sort();
    files
        .iter()
        .map(|p| serde_json::from_str(&fs::read_to_string(p).unwrap()).unwrap())
        .collect()
}

/// The Pusher wire frame the legacy receiver would have turned into this raw dict.
fn wire_frame(raw: &Value) -> Vec<u8> {
    let badges: Vec<Value> = raw["badges"]
        .as_array()
        .unwrap()
        .iter()
        .map(|b| json!({ "type": b }))
        .collect();
    let data = json!({
        "id": raw["message_id"],
        "chatroom_id": raw["chatroom_id"],
        "content": raw["text"],
        "created_at": raw["created_at"],
        "sender": {
            "id": raw["author_id"].as_str().unwrap().parse::<u64>().unwrap(),
            "username": raw["display_name"],
            "identity": { "badges": badges },
        },
    });
    json!({"event": pusher::CHAT_EVENT, "channel": "chatrooms.12345.v2", "data": data.to_string()})
        .to_string()
        .into_bytes()
}

/// Declared divergences, applied to the Python golden payload:
///  - `channel_slug`: dropped; the channel is the host-pinned source, not a guest value.
///  - `created_at`: dropped; `occurred_at` is the host-stamped arrival time.
///  - `chatroom_id` (int) -> `room_id` (string): the Twitch-shaped, PII-classified field name.
///  - `is_owner` -> `is_broadcaster`: ditto.
///  - added: `author` (handle, for tokenization), `user_id` (= author_id), `is_vip`.
fn expected_payload(golden: &Value) -> Value {
    let g = golden["payload"].as_object().unwrap();
    let mut out = Map::new();
    for (k, v) in g {
        match k.as_str() {
            "channel_slug" | "created_at" => {}
            "chatroom_id" => {
                out.insert("room_id".into(), json!(v.as_u64().unwrap().to_string()));
            }
            "is_owner" => {
                out.insert("is_broadcaster".into(), v.clone());
            }
            _ => {
                out.insert(k.clone(), v.clone());
            }
        }
    }
    out.insert("author".into(), g["display_name"].clone());
    out.insert("user_id".into(), g["author_id"].clone());
    let vip = g["badges"].as_array().unwrap().iter().any(|b| b == "vip");
    out.insert("is_vip".into(), json!(vip));
    Value::Object(out)
}

#[test]
fn bundle_matches_every_python_golden_modulo_declared_divergences() {
    let vs = vectors();
    assert!(vs.len() >= 4, "corpus must hold the kick vectors");
    let mut compared = 0;
    for v in &vs {
        let id = v["id"].as_str().unwrap();
        assert_eq!(v["platform"], "kick");
        assert_eq!(v["outcome"], "event", "{id}");
        let got = pusher::normalize_frame(&wire_frame(&v["frame"]), TS).unwrap();
        assert_eq!(got.len(), 1, "{id}");
        let e = &got[0];
        let g = &v["golden"];
        assert_eq!(Value::String(e.event_type.clone()), g["event_type"], "{id}");
        // The identity key is the platform user id; the host swaps it for the UUID actor.
        assert_eq!(e.actor_key.as_deref(), g["actor"].as_str(), "{id}");
        assert_eq!(e.occurred_at, TS, "{id}");
        assert_eq!(
            Value::Object(e.payload.clone()),
            expected_payload(g),
            "{id}: payload differs beyond the declared divergences"
        );
        compared += 1;
    }
    println!("kick parity: {compared}/{} vectors pass", vs.len());
    assert_eq!(compared, vs.len());
}

/// Every payload field the bundle emits is in the host PII classifier's closed schema
/// (`core/svc_process/src/pii_tokenize.rs`), so no event is dead-lettered as unclassified.
#[test]
fn emitted_chat_fields_are_all_pii_classified() {
    const CLASSIFIED: &[&str] = &[
        "text",
        "metadata",
        "channel_name",
        "author",
        "author_id",
        "user_id",
        "display_name",
        "message_id",
        "room_id",
        "badges",
        "is_mod",
        "is_subscriber",
        "is_vip",
        "is_broadcaster",
        "broadcaster_id",
        "broadcaster_login",
        "user_login",
        "user_display_name",
    ];
    for v in vectors() {
        let got = pusher::normalize_frame(&wire_frame(&v["frame"]), TS).unwrap();
        for k in got[0].payload.keys() {
            assert!(CLASSIFIED.contains(&k.as_str()), "unclassified field {k}");
        }
    }
}
