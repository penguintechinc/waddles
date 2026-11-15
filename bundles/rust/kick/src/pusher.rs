//! Kick chat over Pusher: subscribe handshake template, keepalive body and
//! `App\Events\ChatMessageEvent` normalization.
//!
//! Kick's chat backend is a Pusher application (`wss://ws-<cluster>.pusher.com/app/<key>`);
//! the legacy Python receiver (`core/svc_ingest/receivers/kick_pusher.py`) speaks the wire
//! protocol directly. The host owns the socket; this module only builds the one subscribe
//! frame and turns delivered frames into events. The connector never names a chatroom: the
//! subscribe channel is an opaque host-pinned slot.
//!
//! Wire shape (Pusher protocol 7): every frame is `{"event": <name>, "data": <JSON STRING>,
//! "channel": <name>}`; `data` is a JSON-encoded string, not an object.

use serde_json::{Map, Value, json};

use crate::common::{FrameErr, NormEvent, badge_types, id_string, strict_flag};

/// Slot for the full Pusher channel name (`chatrooms.<id>.v2`), host-pinned from the binding.
pub const SLOT_CHANNEL: &str = "{{KICK_CHANNEL}}";
/// Host handle name the slot resolves through (a non-secret, host-answered value).
pub const HANDLE_CHANNEL: &str = "channel";

/// The exact bytes the handshake must start with; the host `WsConfig.slot_prefixes` entry for
/// the `channel` handle (the slot is anchored directly after it and before the closing quote).
pub const REQUIRED_SLOT_PREFIX: &str =
    r#"{"event":"pusher:subscribe","data":{"auth":"","channel":""#;

/// Keepalive body. A client `pusher:ping` is activity (it resets Pusher's inactivity timer, so
/// the server never needs to ping us) and is answered by a `pusher:pong` data frame that feeds
/// the host idle timer.
pub const HEARTBEAT: &[u8] = br#"{"event":"pusher:ping","data":{}}"#;

/// Pusher's own event name for one Kick chat message.
pub const CHAT_EVENT: &str = "App\\Events\\ChatMessageEvent";

/// The handshake `(bytes, secret-refs)`: one subscribe frame, slot only. Independent of any
/// session token, so a reconnect re-sends exactly one subscribe (Pusher subscribe is idempotent
/// per socket) and never stacks a second subscription.
pub fn handshake() -> (Vec<u8>, Vec<(String, String)>) {
    let bytes = format!("{REQUIRED_SLOT_PREFIX}{SLOT_CHANNEL}\"}}}}").into_bytes();
    let refs = vec![(SLOT_CHANNEL.to_string(), HANDLE_CHANNEL.to_string())];
    (bytes, refs)
}

/// Parses the JSON-string `data` field of a Pusher frame. Absent/`null` -> empty object; a
/// string that is not a JSON object, or any other type, is `None` (caller decides).
fn parse_data(frame: &Value) -> Option<Value> {
    match frame.get("data") {
        None | Some(Value::Null) => Some(Value::Object(Map::new())),
        Some(Value::String(s)) if s.is_empty() => Some(Value::Object(Map::new())),
        Some(Value::String(s)) => serde_json::from_str::<Value>(s)
            .ok()
            .filter(Value::is_object),
        Some(v @ Value::Object(_)) => Some(v.clone()),
        Some(_) => None,
    }
}

/// Normalizes one delivered Pusher frame into zero or one events.
///
/// Protocol frames (`pusher:connection_established`, `pusher_internal:subscription_succeeded`,
/// `pusher:ping`, `pusher:pong`) and every non-chat Kick event yield nothing (parity with the
/// Python receiver, which skips them at debug level). `pusher:error` is a loud
/// [`FrameErr::Backend`] so the host reacts instead of idling on a dead subscription. A frame
/// that is not a JSON object, or whose chat `data` is not a JSON object, is `Malformed`.
pub fn normalize_frame(bytes: &[u8], received_at: &str) -> Result<Vec<NormEvent>, FrameErr> {
    let frame: Value = serde_json::from_slice(bytes)
        .map_err(|_| FrameErr::Malformed("pusher frame is not JSON".to_string()))?;
    let Some(event) = frame.get("event").and_then(Value::as_str) else {
        return Err(FrameErr::Malformed(
            "pusher frame has no event name".to_string(),
        ));
    };
    match event {
        "pusher:error" => {
            let code = parse_data(&frame)
                .and_then(|d| d.get("code").and_then(Value::as_u64))
                .map_or_else(|| "unknown".to_string(), |c| c.to_string());
            Err(FrameErr::Backend(format!("pusher error code {code}")))
        }
        CHAT_EVENT => {
            let data = parse_data(&frame).ok_or_else(|| {
                FrameErr::Malformed("chat message data is not a JSON object".to_string())
            })?;
            Ok(normalize_chat(&data, received_at)?.into_iter().collect())
        }
        _ => Ok(Vec::new()),
    }
}

/// Builds the chat event from a Pusher `ChatMessageEvent` data object.
///
/// Empty/absent `content` is dropped (`Ok(None)`, as the Python receiver does). A message with
/// no sender id is `Malformed`: the Python path published it with a null actor, which would let
/// an authorless message past every actor-keyed gate, so the bundle refuses it (declared
/// divergence). Role flags come ONLY from the real boolean `true` of the sender flags OR a
/// badge of that role; any other type fails closed.
pub fn normalize_chat(data: &Value, received_at: &str) -> Result<Option<NormEvent>, FrameErr> {
    let Some(content) = data
        .get("content")
        .and_then(Value::as_str)
        .filter(|c| !c.is_empty())
    else {
        return Ok(None);
    };
    let sender = data.get("sender").filter(|s| s.is_object());
    let Some(author_id) = sender.and_then(|s| s.get("id")).and_then(id_string) else {
        return Err(FrameErr::Malformed(
            "chat message has no sender id".to_string(),
        ));
    };
    let username = sender
        .and_then(|s| s.get("username"))
        .and_then(Value::as_str)
        .filter(|u| !u.is_empty());
    let handle = sender
        .and_then(|s| s.get("slug"))
        .and_then(Value::as_str)
        .filter(|u| !u.is_empty())
        .or(username);
    let badges = badge_types(sender.and_then(|s| s.get("identity")));
    let has = |name: &str| badges.iter().any(|b| b == name);
    let flag = |k: &str| strict_flag(sender.and_then(|s| s.get(k)));
    let payload: Map<String, Value> = match json!({
        "text": content.trim(),
        "author": handle,
        "author_id": author_id,
        "user_id": author_id,
        "display_name": username,
        "message_id": data.get("id").and_then(id_string),
        "room_id": data.get("chatroom_id").and_then(id_string),
        "is_mod": flag("is_moderator") || has("moderator"),
        "is_subscriber": flag("is_subscriber") || has("subscriber"),
        "is_vip": has("vip"),
        "is_broadcaster": flag("is_channel_owner") || has("broadcaster"),
        "badges": badges,
    }) {
        Value::Object(m) => m,
        _ => Map::new(),
    };
    Ok(Some(NormEvent {
        event_type: "message".to_string(),
        actor_key: Some(author_id),
        payload,
        occurred_at: received_at.to_string(),
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    const TS: &str = "2026-11-07T00:00:00.000Z";

    fn chat(data: &Value) -> Vec<u8> {
        json!({"event": CHAT_EVENT, "channel": "chatrooms.9.v2", "data": data.to_string()})
            .to_string()
            .into_bytes()
    }

    #[test]
    fn handshake_is_one_subscribe_with_only_the_channel_slot() {
        let (bytes, refs) = handshake();
        let s = String::from_utf8(bytes).unwrap();
        assert_eq!(
            s,
            r#"{"event":"pusher:subscribe","data":{"auth":"","channel":"{{KICK_CHANNEL}}"}}"#
        );
        assert_eq!(s.matches("subscribe").count(), 1);
        assert_eq!(
            refs,
            vec![(SLOT_CHANNEL.to_string(), "channel".to_string())]
        );
        assert!(s.contains(&format!("{REQUIRED_SLOT_PREFIX}{SLOT_CHANNEL}\"")));
        let v: Value = serde_json::from_str(&s.replace(SLOT_CHANNEL, "chatrooms.1.v2")).unwrap();
        assert_eq!(v["data"]["channel"], "chatrooms.1.v2");
    }

    #[test]
    fn heartbeat_is_a_pusher_ping() {
        let v: Value = serde_json::from_slice(HEARTBEAT).unwrap();
        assert_eq!(v["event"], "pusher:ping");
    }

    #[test]
    fn protocol_and_foreign_events_yield_nothing() {
        for ev in [
            "pusher:connection_established",
            "pusher_internal:subscription_succeeded",
            "pusher:ping",
            "pusher:pong",
            "App\\Events\\SubscriptionEvent",
            "Subscription",
        ] {
            let f = json!({"event": ev, "data": "{}"}).to_string();
            assert!(
                normalize_frame(f.as_bytes(), TS).unwrap().is_empty(),
                "{ev}"
            );
        }
    }

    #[test]
    fn chat_message_maps_to_a_twitch_shaped_event() {
        let data = json!({
            "id": "msg-abc", "chatroom_id": 12345, "content": "  hello chat  ",
            "created_at": "2026-09-11T12:00:00.000000Z",
            "sender": {"id": 999, "username": "PenguinFan", "slug": "penguinfan",
                "identity": {"badges": [{"type": "moderator"}, {"type": "subscriber"}]}}
        });
        let ev = normalize_frame(&chat(&data), TS).unwrap();
        assert_eq!(ev.len(), 1);
        let e = &ev[0];
        assert_eq!(e.event_type, "message");
        assert_eq!(e.actor_key.as_deref(), Some("999"));
        assert_eq!(e.occurred_at, TS);
        let p = &e.payload;
        assert_eq!(p["text"], "hello chat");
        assert_eq!(p["author"], "penguinfan");
        assert_eq!(p["display_name"], "PenguinFan");
        assert_eq!(p["author_id"], "999");
        assert_eq!(p["user_id"], "999");
        assert_eq!(p["message_id"], "msg-abc");
        assert_eq!(p["room_id"], "12345");
        assert_eq!(p["is_mod"], true);
        assert_eq!(p["is_subscriber"], true);
        assert_eq!(p["is_vip"], false);
        assert_eq!(p["is_broadcaster"], false);
        assert_eq!(p["badges"], json!(["moderator", "subscriber"]));
    }

    #[test]
    fn explicit_sender_flags_and_owner_badge_grant_roles() {
        let data = json!({"content": "x", "sender": {"id": "7", "is_moderator": true,
            "is_channel_owner": true, "identity": {"badges": [{"type": "vip"}]}}});
        let e = normalize_chat(&data, TS).unwrap().unwrap();
        assert_eq!(e.payload["is_mod"], true);
        assert_eq!(e.payload["is_broadcaster"], true);
        assert_eq!(e.payload["is_vip"], true);
        let owner_badge = json!({"content": "x", "sender": {"id": 7,
            "identity": {"badges": [{"type": "broadcaster"}]}}});
        let e = normalize_chat(&owner_badge, TS).unwrap().unwrap();
        assert_eq!(e.payload["is_broadcaster"], true);
        assert_eq!(e.payload["is_mod"], false);
    }

    /// A producer bug / spoof must never promote: only the real boolean `true` grants.
    #[test]
    fn non_boolean_flags_fail_closed() {
        for bad in [json!("true"), json!(1), json!("yes"), json!(["x"])] {
            let data = json!({"content": "x", "sender": {"id": 7, "is_moderator": bad,
                "is_subscriber": bad, "is_channel_owner": bad}});
            let e = normalize_chat(&data, TS).unwrap().unwrap();
            for k in ["is_mod", "is_subscriber", "is_broadcaster"] {
                assert_eq!(e.payload[k], false, "{k} with {bad}");
            }
        }
    }

    #[test]
    fn empty_content_is_dropped_and_missing_sender_id_is_loud() {
        for content in [json!(""), json!(null), json!(5)] {
            let d = json!({"content": content, "sender": {"id": 1}});
            assert!(normalize_chat(&d, TS).unwrap().is_none());
        }
        assert!(
            normalize_chat(&json!({"sender": {"id": 1}}), TS)
                .unwrap()
                .is_none()
        );
        for sender in [
            json!({"username": "x"}),
            json!({"id": ""}),
            json!(null),
            json!({"id": -3}),
        ] {
            let d = json!({"content": "hi", "sender": sender});
            assert!(
                matches!(normalize_chat(&d, TS), Err(FrameErr::Malformed(_))),
                "{sender}"
            );
        }
    }

    #[test]
    fn malformed_frames_fail_loud() {
        for bad in [&b"nope"[..], b"[1]", b"{}", br#"{"event":5}"#] {
            assert!(
                matches!(normalize_frame(bad, TS), Err(FrameErr::Malformed(_))),
                "{}",
                String::from_utf8_lossy(bad)
            );
        }
        for data in [json!("[1]"), json!("{not json"), json!(5)] {
            let f = json!({"event": CHAT_EVENT, "data": data}).to_string();
            assert!(matches!(
                normalize_frame(f.as_bytes(), TS),
                Err(FrameErr::Malformed(_))
            ));
        }
    }

    #[test]
    fn pusher_error_is_a_backend_failure_with_the_code_only() {
        let f = json!({"event": "pusher:error",
            "data": json!({"code": 4201, "message": "secret detail"}).to_string()})
        .to_string();
        let Err(FrameErr::Backend(m)) = normalize_frame(f.as_bytes(), TS) else {
            panic!("expected backend error");
        };
        assert!(m.contains("4201") && !m.contains("secret"), "{m}");
    }

    #[test]
    fn data_may_also_arrive_as_an_object() {
        let f =
            json!({"event": CHAT_EVENT, "data": {"content": "x", "sender": {"id": 3}}}).to_string();
        assert_eq!(normalize_frame(f.as_bytes(), TS).unwrap().len(), 1);
    }
}
