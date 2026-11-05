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
    let from_sub = match sub
        .get("condition")
        .and_then(|c| c.get("broadcaster_user_id"))
    {
        None | Some(Value::Null) => None,
        Some(Value::String(id)) if !id.is_empty() => Some(id.as_str()),
        Some(_) => {
            return Err(FrameErr::Malformed(
                "subscription condition broadcaster id must be a non-empty string".to_string(),
            ));
        }
    };
    let event = root.get("event").filter(|e| e.is_object()).ok_or_else(|| {
        FrameErr::Malformed("eventsub notification has no event object".to_string())
    })?;
    build(event_type, event, from_sub, received_at).map(|e| vec![e])
}

/// Identity key for the actor: the platform user id, else the broadcaster id,
/// else the login when the wire has no id. `None` only for a system event
/// (no login at all), matching the hardcoded path's `actor: None`.
fn actor_key(user_id: Option<&str>, broadcaster_id: &str, login: Option<&str>) -> Option<String> {
    let login = login?;
    Some(
        user_id
            .filter(|s| !s.is_empty())
            .or((!broadcaster_id.is_empty()).then_some(broadcaster_id))
            .unwrap_or(login)
            .to_string(),
    )
}

/// Optional string field: absent/null -> `None`; any other type is `Malformed`
/// (never silently treated as absent).
fn opt_str(event: &Value, k: &str) -> Result<Option<String>, FrameErr> {
    match event.get(k) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(v)) => Ok(Some(v.clone())),
        Some(_) => Err(FrameErr::Malformed(format!("event.{k} must be a string"))),
    }
}

/// Optional field with a protocol-documented default (the hardcoded path's
/// metadata defaults): absent/null -> `default`; a present value of the wrong
/// type is `Malformed`.
fn str_or(event: &Value, k: &str, default: &str) -> Result<String, FrameErr> {
    match opt_str(event, k)? {
        Some(v) => Ok(v),
        None => Ok(default.to_string()),
    }
}

fn bool_or(event: &Value, k: &str, default: bool) -> Result<bool, FrameErr> {
    match event.get(k) {
        None | Some(Value::Null) => Ok(default),
        Some(Value::Bool(b)) => Ok(*b),
        Some(_) => Err(FrameErr::Malformed(format!("event.{k} must be a boolean"))),
    }
}

fn int_or(event: &Value, k: &str, default: i64) -> Result<i64, FrameErr> {
    match event.get(k) {
        None | Some(Value::Null) => Ok(default),
        Some(v) => v
            .as_i64()
            .ok_or_else(|| FrameErr::Malformed(format!("event.{k} must be an integer"))),
    }
}

fn build(
    event_type: &str,
    event: &Value,
    from_sub: Option<&str>,
    received_at: &str,
) -> Result<NormEvent, FrameErr> {
    // The broadcaster id is REQUIRED: it keys the actor and the host's source,
    // so an empty/missing one must never flow downstream. A raid carries the
    // channel as `to_broadcaster_user_id` (the hardcoded path fabricated an
    // empty id there -- declared divergence).
    let is_raid = event_type == "channel.raid";
    let mut broadcaster_id = opt_str(event, "broadcaster_user_id")?.filter(|s| !s.is_empty());
    if broadcaster_id.is_none() && is_raid {
        broadcaster_id = opt_str(event, "to_broadcaster_user_id")?.filter(|s| !s.is_empty());
    }
    let broadcaster_id = match broadcaster_id.or_else(|| from_sub.map(str::to_string)) {
        Some(id) => id,
        None => {
            return Err(FrameErr::Malformed(
                "eventsub event has no broadcaster id".to_string(),
            ));
        }
    };
    let broadcaster_login = opt_str(event, "broadcaster_user_login")?;
    let user_id = match opt_str(event, "user_id")? {
        Some(v) => Some(v),
        None => opt_str(event, "from_broadcaster_user_id")?,
    };
    let user_login = match opt_str(event, "user_login")? {
        Some(v) => Some(v),
        None => opt_str(event, "from_broadcaster_user_login")?,
    };
    let display = match opt_str(event, "user_name")? {
        Some(v) => Some(v),
        None => opt_str(event, "from_broadcaster_user_name")?,
    };
    let metadata = match event_type {
        "channel.subscribe" => json!({
            "tier": str_or(event, "tier", "1000")?,
            "is_gift": bool_or(event, "is_gift", false)?,
        }),
        "channel.subscription.gift" => json!({
            "tier": str_or(event, "tier", "1000")?,
            "total": int_or(event, "total", 1)?,
            "is_anonymous": bool_or(event, "is_anonymous", false)?,
        }),
        "channel.raid" => json!({"viewers": int_or(event, "viewers", 0)?}),
        "channel.cheer" => json!({
            "bits": int_or(event, "bits", 0)?,
            "is_anonymous": bool_or(event, "is_anonymous", false)?,
        }),
        "stream.online" => json!({
            "type": str_or(event, "type", "live")?,
            "started_at": str_or(event, "started_at", "")?,
        }),
        _ => json!({}),
    };
    Ok(NormEvent {
        event_type: event_type.to_string(),
        actor_key: actor_key(
            user_id.as_deref(),
            &broadcaster_id,
            user_login.as_deref().or(broadcaster_login.as_deref()),
        ),
        payload: object(json!({
            "broadcaster_id": broadcaster_id,
            "broadcaster_login": broadcaster_login,
            "user_id": user_id,
            "user_login": user_login,
            "user_display_name": display,
            "metadata": metadata,
        })),
        occurred_at: received_at.to_string(),
    })
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

    fn malformed(body: &str) -> bool {
        matches!(
            normalize_eventsub(body.as_bytes(), TS),
            Err(FrameErr::Malformed(_))
        )
    }

    #[test]
    fn condition_broadcaster_used_when_event_omits_it() {
        let b = br#"{"subscription":{"type":"stream.offline","condition":{"broadcaster_user_id":"9"}},"event":{}}"#;
        let e = normalize_eventsub(b, TS).unwrap();
        assert_eq!(e[0].payload["broadcaster_id"], "9");
        assert_eq!(e[0].actor_key, None);
        assert_eq!(e[0].occurred_at, TS);
        assert!(e[0].payload_json().contains("\"metadata\":{}"));
    }

    /// Regression (no-stubs gate): a missing/non-object `event` used to be
    /// defaulted to null and fabricate an empty event.
    #[test]
    fn missing_or_non_object_event_is_malformed() {
        let sub =
            r#""subscription":{"type":"channel.follow","condition":{"broadcaster_user_id":"9"}}"#;
        assert!(malformed(&format!("{{{sub}}}")));
        assert!(malformed(&format!("{{{sub},\"event\":null}}")));
        assert!(malformed(&format!("{{{sub},\"event\":\"x\"}}")));
        assert!(malformed(&format!("{{{sub},\"event\":[1]}}")));
    }

    /// Regression: an absent broadcaster id used to default to "" and flow into
    /// the payload/actor/source (tenant-isolation hazard).
    #[test]
    fn missing_or_empty_broadcaster_id_is_malformed() {
        let sub = r#""subscription":{"type":"channel.follow","condition":{}}"#;
        assert!(malformed(&format!("{{{sub},\"event\":{{}}}}")));
        assert!(malformed(&format!(
            "{{{sub},\"event\":{{\"broadcaster_user_id\":\"\"}}}}"
        )));
        let sub2 =
            r#""subscription":{"type":"channel.follow","condition":{"broadcaster_user_id":""}}"#;
        assert!(malformed(&format!("{{{sub2},\"event\":{{}}}}")));
    }

    #[test]
    fn wrong_typed_fields_are_malformed_not_defaulted() {
        let head = r#"{"subscription":{"type":"#;
        let cond = r#","condition":{"broadcaster_user_id":"9"}}"#;
        let cases = [
            ("channel.follow", r#"{"broadcaster_user_id":5}"#),
            ("channel.follow", r#"{"user_id":7}"#),
            ("channel.follow", r#"{"user_login":false}"#),
            ("channel.follow", r#"{"user_name":[]}"#),
            ("channel.subscribe", r#"{"tier":1000}"#),
            ("channel.subscribe", r#"{"is_gift":"yes"}"#),
            ("channel.subscription.gift", r#"{"total":"5"}"#),
            ("channel.raid", r#"{"viewers":"many"}"#),
            ("channel.cheer", r#"{"bits":1.5}"#),
            ("stream.online", r#"{"started_at":123}"#),
        ];
        for (ty, ev) in cases {
            assert!(
                malformed(&format!("{head}\"{ty}\"{cond},\"event\":{ev}}}")),
                "{ty} {ev}"
            );
        }
        let bad_cond = r#"{"subscription":{"type":"channel.follow","condition":{"broadcaster_user_id":9}},"event":{}}"#;
        assert!(malformed(bad_cond));
    }

    #[test]
    fn absent_optional_metadata_uses_documented_defaults() {
        let b = br#"{"subscription":{"type":"channel.cheer","condition":{"broadcaster_user_id":"9"}},"event":{"bits":null}}"#;
        let e = normalize_eventsub(b, TS).unwrap();
        assert_eq!(e[0].payload["metadata"]["bits"], 0);
        assert_eq!(e[0].payload["metadata"]["is_anonymous"], false);
    }

    #[test]
    fn non_object_payload_wraps() {
        assert_eq!(object(json!(5))["value"], 5);
    }
}
