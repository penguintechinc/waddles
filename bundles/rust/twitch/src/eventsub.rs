//! EventSub notification normalization. Output is byte-identical (payload +
//! actor) to `core/svc_ingest/src/normalize.rs::normalize_twitch_eventsub`;
//! the host has already HMAC-verified and de-duplicated the body.

use serde_json::{Map, Value, json};

use crate::common::{FrameErr, NormEvent};

/// The event types the hardcoded path handles
/// (`ingest::twitch_eventsub::KNOWN_EVENT_TYPES`); anything else is ignored.
pub const KNOWN_EVENT_TYPES: &[&str] = &[
    "channel.follow",
    "channel.subscribe",
    "channel.subscription.gift",
    "channel.cheer",
    "channel.raid",
    "stream.online",
    "stream.offline",
];

fn object(value: Value) -> Map<String, Value> {
    match value {
        Value::Object(m) => m,
        other => {
            let mut m = Map::new();
            m.insert("value".to_string(), other);
            m
        }
    }
}

/// Normalizes one verified EventSub body into zero or one events.
///
/// Verification-handshake (`challenge`) and revocation (`status` != enabled)
/// bodies, and unhandled subscription types, yield no events. A body that is
/// not a JSON object or lacks `subscription.type` is `Malformed`.
pub fn normalize_eventsub(body: &[u8], received_at: &str) -> Result<Vec<NormEvent>, FrameErr> {
    let root: Value = serde_json::from_slice(body)
        .map_err(|_| FrameErr::Malformed("eventsub body is not JSON".to_string()))?;
    let Some(root) = root.as_object() else {
        return Err(FrameErr::Malformed(
            "eventsub body is not an object".to_string(),
        ));
    };
    if root.contains_key("challenge") {
        return Ok(Vec::new());
    }
    let sub = root
        .get("subscription")
        .and_then(Value::as_object)
        .ok_or_else(|| FrameErr::Malformed("eventsub body has no subscription".to_string()))?;
    if let Some(status) = sub.get("status").and_then(Value::as_str)
        && status != "enabled"
    {
        return Ok(Vec::new());
    }
    let event_type = sub
        .get("type")
        .and_then(Value::as_str)
        .ok_or_else(|| FrameErr::Malformed("eventsub subscription has no type".to_string()))?;
    if !KNOWN_EVENT_TYPES.contains(&event_type) {
        return Ok(Vec::new());
    }
    let from_sub = sub
        .get("condition")
        .and_then(|c| c.get("broadcaster_user_id"))
        .and_then(Value::as_str);
    let event = root.get("event").cloned().unwrap_or_default();
    Ok(vec![build(event_type, &event, from_sub, received_at)])
}

fn build(event_type: &str, event: &Value, from_sub: Option<&str>, received_at: &str) -> NormEvent {
    let s = |k: &str| event.get(k).and_then(Value::as_str).map(str::to_string);
    let broadcaster_id = s("broadcaster_user_id")
        .or_else(|| from_sub.map(str::to_string))
        .unwrap_or_default();
    let broadcaster_login = s("broadcaster_user_login");
    let user_id = s("user_id").or_else(|| s("from_broadcaster_user_id"));
    let user_login = s("user_login").or_else(|| s("from_broadcaster_user_login"));
    let display = s("user_name").or_else(|| s("from_broadcaster_user_name"));
    let tier = event.get("tier").and_then(Value::as_str).unwrap_or("1000");
    let flag = |k: &str| event.get(k).and_then(Value::as_bool).unwrap_or(false);
    let int = |k: &str, d: i64| event.get(k).and_then(Value::as_i64).unwrap_or(d);
    let metadata = match event_type {
        "channel.subscribe" => json!({"tier": tier, "is_gift": flag("is_gift")}),
        "channel.subscription.gift" => json!({
            "tier": tier, "total": int("total", 1), "is_anonymous": flag("is_anonymous")
        }),
        "channel.raid" => json!({"viewers": int("viewers", 0)}),
        "channel.cheer" => json!({"bits": int("bits", 0), "is_anonymous": flag("is_anonymous")}),
        "stream.online" => json!({
            "type": event.get("type").and_then(Value::as_str).unwrap_or("live"),
            "started_at": event.get("started_at").and_then(Value::as_str).unwrap_or(""),
        }),
        _ => json!({}),
    };
    NormEvent {
        event_type: event_type.to_string(),
        actor: user_login.clone().or_else(|| broadcaster_login.clone()),
        payload: object(json!({
            "broadcaster_id": broadcaster_id,
            "broadcaster_login": broadcaster_login,
            "user_id": user_id,
            "user_login": user_login,
            "user_display_name": display,
            "metadata": metadata,
        })),
        occurred_at: received_at.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const TS: &str = "2026-11-05T00:00:00.000Z";

    #[test]
    fn challenge_and_revocation_yield_nothing() {
        assert!(
            normalize_eventsub(br#"{"challenge":"x","subscription":{}}"#, TS)
                .unwrap()
                .is_empty()
        );
        let rev = br#"{"subscription":{"type":"channel.follow","status":"authorization_revoked"}}"#;
        assert!(normalize_eventsub(rev, TS).unwrap().is_empty());
    }

    #[test]
    fn malformed_bodies_fail_loud() {
        assert!(matches!(
            normalize_eventsub(b"nope", TS),
            Err(FrameErr::Malformed(_))
        ));
        assert!(matches!(
            normalize_eventsub(b"[1]", TS),
            Err(FrameErr::Malformed(_))
        ));
        assert!(matches!(
            normalize_eventsub(b"{}", TS),
            Err(FrameErr::Malformed(_))
        ));
        assert!(matches!(
            normalize_eventsub(br#"{"subscription":{}}"#, TS),
            Err(FrameErr::Malformed(_))
        ));
    }

    #[test]
    fn missing_event_uses_condition_broadcaster() {
        let b = br#"{"subscription":{"type":"stream.offline","condition":{"broadcaster_user_id":"9"}}}"#;
        let e = normalize_eventsub(b, TS).unwrap();
        assert_eq!(e[0].payload["broadcaster_id"], "9");
        assert_eq!(e[0].actor, None);
        assert_eq!(e[0].occurred_at, TS);
        assert!(e[0].payload_json().contains("\"metadata\":{}"));
    }

    #[test]
    fn non_object_payload_wraps() {
        assert_eq!(object(json!(5))["value"], 5);
    }
}
