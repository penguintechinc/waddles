//! Fixed, non-pluggable normalizers: raw platform payloads from
//! `penguin-connector-{twitch,discord}` -> `penguin_spine::PlatformEvent`.
//! Byte-exact ports of the Python predecessor's own normalizers (spec
//! S4.1: "normalizers absorbed as code") --
//! `core/svc_ingest/bundles/twitch_ingest.py`/`discord_ingest.py` for the
//! `PlatformEvent` field shape, and `core/svc_ingest/receivers/
//! twitch_irc.py`'s `_parse_tags`/`_parse_badges`/self-message-skip for
//! the Twitch IRCv3 tag decoding `penguin-connector-twitch` deliberately
//! leaves as an opaque raw string (its own module doc: "a caller wanting
//! Twitch-specific fields parses `ChatMessage::tags` itself ... see
//! `receivers/twitch_irc.py`'s `_parse_tags` for the reference behaviour
//! to port at that layer" -- this module is that port). Every function
//! here is a pure mapping with no I/O -- the connection lifecycle and
//! publish step live in `src/ingest/`.

use std::collections::HashMap;

use penguin_spine::{PlatformEvent, Source};

/// Builds a `serde_json::Map` payload from a JSON value -- `PlatformEvent`
/// requires a map, never a bare `Value` (`penguin_spine::PlatformEvent`'s
/// own doc comment).
fn payload_map(value: serde_json::Value) -> serde_json::Map<String, serde_json::Value> {
    match value {
        serde_json::Value::Object(map) => map,
        other => {
            let mut map = serde_json::Map::new();
            map.insert("value".to_string(), other);
            map
        }
    }
}

/// RFC 3339, UTC, millisecond precision, `Z` suffix -- the exact shape
/// `penguin_spine::PlatformEvent::occurred_at` validates against. Neither
/// connector's raw message type carries a reliable send timestamp of its
/// own, so this is the ingest arrival time -- matches the Python
/// predecessor's own `datetime.now(timezone.utc)` fallback when the raw
/// event has no authoritative timestamp field.
fn now_rfc3339_millis() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true)
}

/// Reverses IRCv3 message-tag value escaping (IRCv3.2 message-tags spec)
/// -- `\s` (space), `\:` (semicolon), `\\` (backslash), `\r`, `\n`. A
/// trailing lone backslash (malformed input) is dropped, never panics.
/// Byte-exact port of `receivers/twitch_irc.py::_unescape_tag_value`.
fn unescape_tag_value(value: &str) -> String {
    if !value.contains('\\') {
        return value.to_string();
    }
    let chars: Vec<char> = value.chars().collect();
    let mut out = String::with_capacity(value.len());
    let mut i = 0;
    while i < chars.len() {
        if chars[i] == '\\' && i + 1 < chars.len() {
            let escaped = match chars[i + 1] {
                's' => ' ',
                ':' => ';',
                '\\' => '\\',
                'r' => '\r',
                'n' => '\n',
                other => other,
            };
            out.push(escaped);
            i += 2;
        } else {
            if chars[i] != '\\' {
                out.push(chars[i]);
            }
            i += 1;
        }
    }
    out
}

/// Parses a raw IRCv3 `tag1=val1;tag2=val2` string into `{tag: value}`.
/// `None`/empty input (CAP not granted, or a non-tagged line) yields an
/// empty map, never panics. A valueless tag (`;mod;` with no `=`) maps to
/// `""`, matching the IRCv3 spec's boolean-flag tag shape. Byte-exact port
/// of `receivers/twitch_irc.py::_parse_tags` -- `penguin_connector_twitch::
/// irc::ChatMessage::tags` already strips the leading `@` `IrcTransport`
/// parses off itself, so this takes exactly the same `tag1=val1;...`
/// segment the Python `_parse_tags` receives as `raw_tags`.
fn parse_tags(raw_tags: Option<&str>) -> HashMap<String, String> {
    let mut tags = HashMap::new();
    let Some(raw_tags) = raw_tags.filter(|s| !s.is_empty()) else {
        return tags;
    };
    for pair in raw_tags.split(';') {
        if pair.is_empty() {
            continue;
        }
        match pair.split_once('=') {
            Some((key, value)) => {
                tags.insert(key.to_string(), unescape_tag_value(value));
            }
            None => {
                tags.insert(pair.to_string(), String::new());
            }
        }
    }
    tags
}

/// `"moderator/1,subscriber/12,vip/1"` -> `["moderator", "subscriber",
/// "vip"]` -- names only (version numbers dropped). Byte-exact port of
/// `receivers/twitch_irc.py::_parse_badges`.
fn parse_badges(raw_badges: &str) -> Vec<String> {
    if raw_badges.is_empty() {
        return Vec::new();
    }
    raw_badges
        .split(',')
        .filter_map(|entry| {
            let name = entry.split('/').next().unwrap_or("");
            (!name.is_empty()).then(|| name.to_string())
        })
        .collect()
}

/// True when `sender` is this receiver's own configured nick
/// (case-insensitive) -- Twitch IRC echoes a bot's own `PRIVMSG` back to
/// it, and `penguin_connector_twitch::irc::IrcConnection::recv` performs no
/// self-filtering of its own (confirmed: its module doc scopes it to wire
/// protocol only). Byte-exact port of `receivers/twitch_irc.py`'s inline
/// `sender.lower() == self_nick_lower` skip.
#[must_use]
pub fn is_self_message(sender: &str, configured_nick: &str) -> bool {
    sender.eq_ignore_ascii_case(configured_nick)
}

/// Normalizes one Twitch IRC `PRIVMSG` into a `PlatformEvent`. Byte-exact
/// port of `twitch_ingest.py::normalize`'s payload shape, fed by this
/// module's own port of `receivers/twitch_irc.py`'s IRCv3 tag parsing
/// (`penguin-connector-twitch` leaves `ChatMessage::tags` as an opaque raw
/// string by design -- see this module's doc comment).
#[must_use]
pub fn normalize_twitch_irc(msg: &penguin_connector_twitch::irc::ChatMessage) -> PlatformEvent {
    let channel = msg.channel.trim_start_matches('#').to_string();
    let tags = parse_tags(msg.tags.as_deref());
    let badges = parse_badges(tags.get("badges").map(String::as_str).unwrap_or(""));
    let user_id = tags.get("user-id").filter(|s| !s.is_empty()).cloned();
    let is_vip = badges.iter().any(|b| b == "vip");
    let is_broadcaster = badges.iter().any(|b| b == "broadcaster");

    PlatformEvent {
        platform: "twitch".to_string(),
        event_type: "message".to_string(),
        actor: Some(msg.sender.clone()),
        payload: payload_map(serde_json::json!({
            "text": msg.text,
            "channel_name": channel,
            "author": msg.sender,
            "author_id": user_id,
            "user_id": user_id,
            "display_name": tags.get("display-name").filter(|s| !s.is_empty()),
            "message_id": tags.get("id").filter(|s| !s.is_empty()),
            "room_id": tags.get("room-id").filter(|s| !s.is_empty()),
            "badges": badges,
            "is_mod": tags.get("mod").map(String::as_str) == Some("1"),
            "is_subscriber": tags.get("subscriber").map(String::as_str) == Some("1"),
            "is_vip": is_vip,
            "is_broadcaster": is_broadcaster,
        })),
        occurred_at: now_rfc3339_millis(),
        source: Some(Source {
            platform: "twitch".to_string(),
            account_id: channel.clone(),
            channel_id: Some(channel),
        }),
    }
}

/// Normalizes one Discord `MESSAGE_CREATE` into a `PlatformEvent`.
/// Byte-exact port of `discord_ingest.py::normalize`'s payload shape.
/// Discord's own self-message filter already ran inside
/// `penguin_connector_discord::gateway::GatewaySession::next_chat_message`,
/// so every message reaching this function is from another user.
#[must_use]
pub fn normalize_discord(msg: &penguin_connector_discord::gateway::ChatMessage) -> PlatformEvent {
    PlatformEvent {
        platform: "discord".to_string(),
        event_type: "message".to_string(),
        actor: Some(msg.author_username.clone()),
        payload: payload_map(serde_json::json!({
            "text": msg.content,
            "guild_id": msg.guild_id,
            "channel_id": msg.channel_id,
            "message_id": msg.message_id,
            "author_id": msg.author_id,
        })),
        occurred_at: now_rfc3339_millis(),
        source: Some(Source {
            platform: "discord".to_string(),
            account_id: msg.guild_id.clone().unwrap_or_else(|| "dm".to_string()),
            channel_id: Some(msg.channel_id.clone()),
        }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use penguin_connector_discord::gateway::ChatMessage as DiscordChatMessage;
    use penguin_connector_twitch::irc::ChatMessage as TwitchChatMessage;

    fn twitch_msg() -> TwitchChatMessage {
        TwitchChatMessage {
            channel: "#somechannel".to_string(),
            sender: "someuser".to_string(),
            text: "hello chat".to_string(),
            tags: None,
        }
    }

    fn discord_msg() -> DiscordChatMessage {
        DiscordChatMessage {
            guild_id: Some("111".to_string()),
            channel_id: "222".to_string(),
            message_id: "333".to_string(),
            author_id: "444".to_string(),
            author_username: "someuser".to_string(),
            content: "hello chat".to_string(),
        }
    }

    #[test]
    fn unescape_tag_value_reverses_known_escapes() {
        assert_eq!(unescape_tag_value("a\\sb"), "a b");
        assert_eq!(unescape_tag_value("a\\:b"), "a;b");
        assert_eq!(unescape_tag_value("a\\\\b"), "a\\b");
        assert_eq!(unescape_tag_value("a\\rb"), "a\rb");
        assert_eq!(unescape_tag_value("a\\nb"), "a\nb");
        assert_eq!(unescape_tag_value("plain"), "plain");
    }

    #[test]
    fn unescape_tag_value_drops_trailing_lone_backslash() {
        assert_eq!(unescape_tag_value("abc\\"), "abc");
    }

    #[test]
    fn parse_tags_handles_empty_and_none_input() {
        assert!(parse_tags(None).is_empty());
        assert!(parse_tags(Some("")).is_empty());
    }

    #[test]
    fn parse_tags_parses_key_value_pairs() {
        let tags = parse_tags(Some(
            "display-name=SomeUser;user-id=12345;mod=0;subscriber=1",
        ));
        assert_eq!(tags.get("display-name").unwrap(), "SomeUser");
        assert_eq!(tags.get("user-id").unwrap(), "12345");
        assert_eq!(tags.get("mod").unwrap(), "0");
        assert_eq!(tags.get("subscriber").unwrap(), "1");
    }

    #[test]
    fn parse_tags_valueless_tag_maps_to_empty_string() {
        let tags = parse_tags(Some("flag;other=1"));
        assert_eq!(tags.get("flag").unwrap(), "");
    }

    #[test]
    fn parse_badges_extracts_names_without_versions() {
        assert_eq!(
            parse_badges("moderator/1,subscriber/12,vip/1"),
            vec!["moderator", "subscriber", "vip"]
        );
    }

    #[test]
    fn parse_badges_empty_input_yields_empty_vec() {
        assert!(parse_badges("").is_empty());
    }

    #[test]
    fn is_self_message_is_case_insensitive() {
        assert!(is_self_message("WaddleBot", "waddlebot"));
        assert!(!is_self_message("someuser", "waddlebot"));
    }

    #[test]
    fn twitch_irc_normalizes_channel_actor_and_text_with_no_tags() {
        let event = normalize_twitch_irc(&twitch_msg());
        assert_eq!(event.platform, "twitch");
        assert_eq!(event.event_type, "message");
        assert_eq!(event.actor.as_deref(), Some("someuser"));
        assert_eq!(
            event.payload.get("text").and_then(|v| v.as_str()),
            Some("hello chat")
        );
        assert_eq!(
            event.payload.get("channel_name").and_then(|v| v.as_str()),
            Some("somechannel")
        );
        assert_eq!(
            event.payload.get("author_id"),
            Some(&serde_json::Value::Null)
        );
        assert_eq!(
            event.payload.get("is_mod").and_then(|v| v.as_bool()),
            Some(false)
        );
        let source = event.source.expect("twitch normalizer always sets source");
        assert_eq!(source.platform, "twitch");
        assert_eq!(source.account_id, "somechannel");
        assert_eq!(source.channel_id.as_deref(), Some("somechannel"));
    }

    #[test]
    fn twitch_irc_decodes_full_tag_set() {
        let mut msg = twitch_msg();
        msg.tags = Some(
            "display-name=SomeUser;user-id=999;id=abc-123;room-id=555;mod=1;subscriber=0;badges=moderator/1,vip/1"
                .to_string(),
        );
        let event = normalize_twitch_irc(&msg);
        assert_eq!(
            event.payload.get("display_name").and_then(|v| v.as_str()),
            Some("SomeUser")
        );
        assert_eq!(
            event.payload.get("user_id").and_then(|v| v.as_str()),
            Some("999")
        );
        assert_eq!(
            event.payload.get("author_id").and_then(|v| v.as_str()),
            Some("999")
        );
        assert_eq!(
            event.payload.get("message_id").and_then(|v| v.as_str()),
            Some("abc-123")
        );
        assert_eq!(
            event.payload.get("room_id").and_then(|v| v.as_str()),
            Some("555")
        );
        assert_eq!(
            event.payload.get("is_mod").and_then(|v| v.as_bool()),
            Some(true)
        );
        assert_eq!(
            event.payload.get("is_subscriber").and_then(|v| v.as_bool()),
            Some(false)
        );
        assert_eq!(
            event.payload.get("is_vip").and_then(|v| v.as_bool()),
            Some(true)
        );
        assert_eq!(
            event
                .payload
                .get("badges")
                .and_then(|v| v.as_array())
                .map(|a| a.len()),
            Some(2)
        );
    }

    #[test]
    fn twitch_irc_broadcaster_badge_sets_is_broadcaster() {
        let mut msg = twitch_msg();
        msg.tags = Some("badges=broadcaster/1".to_string());
        let event = normalize_twitch_irc(&msg);
        assert_eq!(
            event
                .payload
                .get("is_broadcaster")
                .and_then(|v| v.as_bool()),
            Some(true)
        );
    }

    #[test]
    fn twitch_irc_strips_leading_hash_from_channel() {
        let mut msg = twitch_msg();
        msg.channel = "nohash".to_string();
        let event = normalize_twitch_irc(&msg);
        assert_eq!(event.source.unwrap().account_id, "nohash");
    }

    #[test]
    fn twitch_irc_occurred_at_is_valid_rfc3339_millis_z() {
        let event = normalize_twitch_irc(&twitch_msg());
        assert!(event.occurred_at.ends_with('Z'));
        assert!(chrono::DateTime::parse_from_rfc3339(&event.occurred_at).is_ok());
    }

    #[test]
    fn discord_normalizes_guild_channel_author_and_text() {
        let event = normalize_discord(&discord_msg());
        assert_eq!(event.platform, "discord");
        assert_eq!(event.event_type, "message");
        assert_eq!(event.actor.as_deref(), Some("someuser"));
        assert_eq!(
            event.payload.get("text").and_then(|v| v.as_str()),
            Some("hello chat")
        );
        let source = event.source.expect("discord normalizer always sets source");
        assert_eq!(source.platform, "discord");
        assert_eq!(source.account_id, "111");
        assert_eq!(source.channel_id.as_deref(), Some("222"));
    }

    #[test]
    fn discord_dm_with_no_guild_id_uses_dm_account_id() {
        let mut msg = discord_msg();
        msg.guild_id = None;
        let event = normalize_discord(&msg);
        assert_eq!(event.source.unwrap().account_id, "dm");
    }

    #[test]
    fn discord_occurred_at_is_valid_rfc3339_millis_z() {
        let event = normalize_discord(&discord_msg());
        assert!(event.occurred_at.ends_with('Z'));
        assert!(chrono::DateTime::parse_from_rfc3339(&event.occurred_at).is_ok());
    }

    #[test]
    fn payload_map_wraps_a_non_object_value() {
        let map = payload_map(serde_json::json!("not an object"));
        assert_eq!(
            map.get("value").and_then(|v| v.as_str()),
            Some("not an object")
        );
    }

    #[test]
    fn payload_map_passes_through_an_object_value() {
        let map = payload_map(serde_json::json!({"a": 1}));
        assert_eq!(map.get("a").and_then(|v| v.as_i64()), Some(1));
    }
}
