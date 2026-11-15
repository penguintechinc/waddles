//! `twitch.live.channels`: the Twitch implementation of the common live-channels contract
//! (`bundles/contracts/live-channels-contract.v1.json`, tool name `{platform}.live.channels`).
//!
//! The host pins the community's broadcaster (`{{TWITCH_BROADCASTER_ID}}` slot); the caller never
//! supplies a channel. One `GET /users?id=` (display name, stable id) plus one
//! `GET /streams?user_id=` (live state) per call. `scope: community` answers for the bound
//! channel; `scope: linked` (members' linked channels) needs a member enumeration the connector
//! does not have, so it fails loudly `platform_unavailable` instead of answering an empty list.

use serde_json::{Value, json};
use waddle_sdk::tool::ToolError;

use crate::helix::{bad, clip_chars, coded, get_json, object, status_error};
use crate::sender::{HELIX, SLOT_BROADCASTER};
use crate::tools::{ClockHost, HelixHost};

/// Runs the tool.
///
/// # Errors
/// `InvalidArgs("<code>[: detail]")` with `code` in `platform_unavailable|rate_limited|not_connected`,
/// or a plain schema message for bad arguments.
pub fn channels(
    host: &dyn HelixHost,
    clock: &dyn ClockHost,
    args: &Value,
) -> Result<Value, ToolError> {
    let obj = object(args, &["scope", "cursor", "limit"])?;
    let scope = obj
        .get("scope")
        .and_then(Value::as_str)
        .ok_or_else(|| bad("scope is required (community|linked)"))?;
    if !matches!(scope, "community" | "linked") {
        return Err(bad("scope must be community or linked"));
    }
    if let Some(l) = obj.get("limit") {
        l.as_u64()
            .filter(|l| (1..=100).contains(l))
            .ok_or_else(|| bad("limit must be an integer 1..=100"))?;
    }
    if let Some(c) = obj.get("cursor") {
        c.as_str()
            .filter(|c| (1..=512).contains(&c.len()))
            .ok_or_else(|| bad("cursor must be 1..=512 characters"))?;
        // The bound channel is a single page; no cursor was ever issued.
        return Err(bad("cursor is not valid for this result"));
    }
    if scope == "linked" {
        return Err(coded(
            "platform_unavailable",
            "linked-channel enumeration is not supported by the twitch connector",
        ));
    }
    let (us, users) = get_json(host, &format!("{HELIX}/users?id={SLOT_BROADCASTER}"))?;
    if !(200..300).contains(&us) {
        return Err(status_error(us));
    }
    let user = users
        .get("data")
        .and_then(Value::as_array)
        .and_then(|d| d.first())
        .ok_or_else(|| coded("not_connected", "the bound broadcaster was not found"))?;
    let channel_ref = user
        .get("id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty() && s.len() <= 256)
        .ok_or_else(|| coded("platform_unavailable", "malformed user reply"))?;
    let display = user
        .get("display_name")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(|s| clip_chars(s, 100))
        .ok_or_else(|| coded("platform_unavailable", "malformed user reply"))?;
    let (ss, streams) = get_json(host, &format!("{HELIX}/streams?user_id={SLOT_BROADCASTER}"))?;
    if !(200..300).contains(&ss) {
        return Err(status_error(ss));
    }
    let live = streams
        .get("data")
        .and_then(Value::as_array)
        .ok_or_else(|| coded("platform_unavailable", "malformed streams reply"))?
        .iter()
        .find(|s| s.get("type").and_then(Value::as_str) == Some("live"));
    let mut ch = serde_json::Map::new();
    ch.insert("channel_ref".to_string(), json!(channel_ref));
    ch.insert("display_name".to_string(), json!(display));
    ch.insert("is_live".to_string(), json!(live.is_some()));
    if let Some(s) = live {
        let text = |k: &str, max: usize| {
            s.get(k)
                .and_then(Value::as_str)
                .filter(|v| !v.is_empty())
                .map(|v| clip_chars(v, max))
        };
        for (out, key, max) in [
            ("title", "title", 300),
            ("category", "game_name", 100),
            ("started_at", "started_at", 40),
            ("thumbnail_ref", "thumbnail_url", 512),
        ] {
            if let Some(v) = text(key, max) {
                ch.insert(out.to_string(), json!(v));
            }
        }
        if let Some(v) = s.get("viewer_count").and_then(Value::as_u64) {
            ch.insert("viewers".to_string(), json!(v));
        }
    }
    Ok(json!({
        "channels": [Value::Object(ch)],
        "cursor": null,
        "as_of": clock.now_rfc3339(),
    }))
}

#[cfg(test)]
mod tests {
    use std::sync::Mutex;

    use super::*;

    struct Fake {
        users: (u16, Value),
        streams: (u16, Value),
        urls: Mutex<Vec<String>>,
    }
    impl HelixHost for Fake {
        fn get(&self, url: &str) -> Result<(u16, Vec<u8>), ToolError> {
            self.urls.lock().unwrap().push(url.to_string());
            let (s, b) = if url.contains("/users?") {
                &self.users
            } else {
                &self.streams
            };
            Ok((*s, b.to_string().into_bytes()))
        }
    }
    struct Clock;
    impl ClockHost for Clock {
        fn now_rfc3339(&self) -> String {
            "2026-11-07T12:00:00.000Z".to_string()
        }
    }
    fn fake(streams: Value) -> Fake {
        Fake {
            users: (200, json!({"data":[{"id":"111","login":"chan","display_name":"Chan"}]})),
            streams: (200, streams),
            urls: Mutex::new(vec![]),
        }
    }
    fn community() -> Value {
        json!({"scope":"community"})
    }
    fn err(e: ToolError) -> String {
        match e {
            ToolError::InvalidArgs(m) => m,
            o => panic!("{o:?}"),
        }
    }

    #[test]
    fn live_channel_carries_contract_fields_only() {
        let f = fake(json!({"data":[{"type":"live","title":"Speedrun","game_name":"Chess","viewer_count":42,"started_at":"2026-11-07T10:00:00Z","thumbnail_url":"https://x/{width}.jpg","user_login":"chan","user_name":"Chan"}]}));
        let out = channels(&f, &Clock, &community()).unwrap();
        assert_eq!(
            out,
            json!({"channels":[{"channel_ref":"111","display_name":"Chan","is_live":true,"title":"Speedrun","category":"Chess","viewers":42,"started_at":"2026-11-07T10:00:00Z","thumbnail_ref":"https://x/{width}.jpg"}],"cursor":null,"as_of":"2026-11-07T12:00:00.000Z"})
        );
        let urls = f.urls.lock().unwrap().clone();
        assert_eq!(urls[0], "https://api.twitch.tv/helix/users?id={{TWITCH_BROADCASTER_ID}}");
        assert_eq!(urls[1], "https://api.twitch.tv/helix/streams?user_id={{TWITCH_BROADCASTER_ID}}");
    }

    #[test]
    fn offline_is_a_real_answer_with_is_live_false() {
        let out = channels(&fake(json!({"data":[]})), &Clock, &community()).unwrap();
        assert_eq!(out["channels"][0]["is_live"], false);
        assert!(out["channels"][0].get("viewers").is_none());
    }

    #[test]
    fn errors_are_loud_and_coded() {
        let mut f = fake(json!({"data":[]}));
        f.users = (401, json!({}));
        assert!(err(channels(&f, &Clock, &community()).unwrap_err()).starts_with("not_connected"));
        let mut f = fake(json!({"data":[]}));
        f.streams = (429, json!({}));
        assert!(err(channels(&f, &Clock, &community()).unwrap_err()).starts_with("rate_limited"));
        let mut f = fake(json!({"data":[]}));
        f.streams = (503, json!({}));
        assert!(err(channels(&f, &Clock, &community()).unwrap_err()).starts_with("platform_unavailable"));
        let mut f = fake(json!({"data":[]}));
        f.users = (200, json!({"data":[]}));
        assert!(err(channels(&f, &Clock, &community()).unwrap_err()).starts_with("not_connected"));
        // linked scope: loud, never an empty success.
        let e = err(channels(&fake(json!({"data":[]})), &Clock, &json!({"scope":"linked"})).unwrap_err());
        assert!(e.starts_with("platform_unavailable"));
    }

    #[test]
    fn schema_violations_and_destinations_are_rejected_before_any_request() {
        let f = fake(json!({"data":[]}));
        for bad in [
            json!({}),
            json!({"scope":"all"}),
            json!({"scope":"community","limit":0}),
            json!({"scope":"community","limit":101}),
            json!({"scope":"community","cursor":""}),
            json!({"scope":"community","cursor":"abc"}),
            json!({"scope":"community","broadcaster_id":"5"}),
            json!({"scope":"community","extra":1}),
        ] {
            assert!(channels(&f, &Clock, &bad).is_err(), "{bad}");
        }
        assert!(f.urls.lock().unwrap().is_empty());
    }
}
