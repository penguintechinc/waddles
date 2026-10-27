//! Fixed, non-pluggable normalizers: raw platform payloads from
//! `penguin-connector-{twitch,discord}` -> `penguin_spine::PlatformEvent`.
//! Byte-exact ports of the Python predecessor's own normalizers (spec
//! S4.1: "normalizers absorbed as code") --
//! `core/svc_ingest/builtin_handlers/twitch_ingest.py`/`discord_ingest.py` for the
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

/// Discord's `ADMINISTRATOR` permission bit (Gateway/REST permission
/// bitfield, <https://discord.com/developers/docs/topics/permissions>) --
/// Discord's closest analog to a Twitch channel owner: an admin can do
/// anything a guild owner can, short of deleting the guild itself.
const PERM_ADMINISTRATOR: u64 = 1 << 3;

/// Discord's `MANAGE_MESSAGES` permission bit -- the standard "moderator"
/// signal (delete/pin others' messages), distinct from `ADMINISTRATOR`/guild
/// ownership.
const PERM_MANAGE_MESSAGES: u64 = 1 << 13;

/// The cross-platform permission signal bundles already read
/// (`event.payload["is_mod"]`/`["is_broadcaster"]`, set for Twitch by
/// [`normalize_twitch_irc`]) -- [`compute_discord_permissions`] derives the
/// same two booleans from Discord's role/permission model so existing bundle
/// `is_mod`/`is_broadcaster` checks work unchanged on Discord. `Default`
/// (`false`/`false`) is the fail-closed value: a DM, a lookup failure, or any
/// case permissions genuinely can't be determined.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct DiscordPermissions {
    /// Standard-moderator signal: `MANAGE_MESSAGES`, or anything that
    /// already implies `is_broadcaster`.
    pub is_mod: bool,
    /// Guild-owner-or-`ADMINISTRATOR` signal -- Discord's closest analog to
    /// a Twitch broadcaster/channel owner.
    pub is_broadcaster: bool,
}

/// Derives [`DiscordPermissions`] from a resolved guild owner id, a member's
/// role ids, and the guild's role->permission-bitfield map. Pure (no I/O) so
/// every permission combination is unit-tested without a live Discord API
/// call -- see `crate::ingest::discord` for the REST lookup that gathers
/// these inputs and the fail-closed `DiscordPermissions::default()` it falls
/// back to on any lookup error.
///
/// `member_role_ids` is expected to already include the guild's own id (the
/// implicit `@everyone` role, which Discord's member-roles response omits
/// but whose permissions still apply to every member) -- the caller adds it
/// before calling this function.
#[must_use]
pub fn compute_discord_permissions(
    author_id: &str,
    guild_owner_id: &str,
    member_role_ids: &[String],
    guild_roles: &[(String, u64)],
) -> DiscordPermissions {
    let effective_permissions = guild_roles
        .iter()
        .filter(|(role_id, _)| member_role_ids.iter().any(|r| r == role_id))
        .fold(0u64, |acc, (_, perms)| acc | perms);

    let is_owner = !author_id.is_empty() && author_id == guild_owner_id;
    let has_administrator = effective_permissions & PERM_ADMINISTRATOR != 0;
    let has_manage_messages = effective_permissions & PERM_MANAGE_MESSAGES != 0;

    let is_broadcaster = is_owner || has_administrator;
    let is_mod = is_broadcaster || has_manage_messages;

    DiscordPermissions {
        is_mod,
        is_broadcaster,
    }
}

/// Normalizes one Discord `MESSAGE_CREATE` into a `PlatformEvent`. Byte-exact
/// port of `discord_ingest.py::normalize`'s payload shape, extended with
/// `is_mod`/`is_broadcaster` (gh: Discord admin/mutation commands fail
/// closed without them -- bundles gate on these two fields regardless of
/// platform, see [`DiscordPermissions`]). Discord's own self-message filter
/// already ran inside
/// `penguin_connector_discord::gateway::GatewaySession::next_chat_message`,
/// so every message reaching this function is from another user.
#[must_use]
pub fn normalize_discord(
    msg: &penguin_connector_discord::gateway::ChatMessage,
    perms: &DiscordPermissions,
) -> PlatformEvent {
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
            "is_mod": perms.is_mod,
            "is_broadcaster": perms.is_broadcaster,
        })),
        occurred_at: now_rfc3339_millis(),
        source: Some(Source {
            platform: "discord".to_string(),
            account_id: msg.guild_id.clone().unwrap_or_else(|| "dm".to_string()),
            channel_id: Some(msg.channel_id.clone()),
        }),
    }
}

/// Normalizes one Twitch EventSub `notification` event into a
/// `PlatformEvent`. Byte-exact port of the legacy `eventsub.py::
/// build_raw_event`'s payload shape (per-event-type metadata folded into a
/// `metadata` sub-object), fed by `crate::ingest::twitch_eventsub::
/// handle_webhook`'s already-verified, already-deduped `event`/
/// `subscription.condition.broadcaster_user_id`.
///
/// `broadcaster_user_id_from_subscription` is the fallback used when the
/// event payload itself omits `broadcaster_user_id` (matches the legacy
/// module's own `event.get("broadcaster_user_id") or subscription.get(
/// "condition", {}).get("broadcaster_user_id", "")` precedence).
#[must_use]
pub fn normalize_twitch_eventsub(
    event_type: &str,
    event: &serde_json::Value,
    broadcaster_user_id_from_subscription: Option<&str>,
) -> PlatformEvent {
    let str_field = |key: &str| event.get(key).and_then(|v| v.as_str()).map(str::to_string);

    let broadcaster_id = str_field("broadcaster_user_id")
        .or_else(|| broadcaster_user_id_from_subscription.map(str::to_string))
        .unwrap_or_default();
    let broadcaster_login = str_field("broadcaster_user_login");
    let user_id = str_field("user_id").or_else(|| str_field("from_broadcaster_user_id"));
    let user_login = str_field("user_login").or_else(|| str_field("from_broadcaster_user_login"));
    let user_display_name =
        str_field("user_name").or_else(|| str_field("from_broadcaster_user_name"));

    let metadata = match event_type {
        "channel.subscribe" => serde_json::json!({
            "tier": event.get("tier").and_then(|v| v.as_str()).unwrap_or("1000"),
            "is_gift": event.get("is_gift").and_then(|v| v.as_bool()).unwrap_or(false),
        }),
        "channel.subscription.gift" => serde_json::json!({
            "tier": event.get("tier").and_then(|v| v.as_str()).unwrap_or("1000"),
            "total": event.get("total").and_then(|v| v.as_i64()).unwrap_or(1),
            "is_anonymous": event.get("is_anonymous").and_then(|v| v.as_bool()).unwrap_or(false),
        }),
        "channel.raid" => serde_json::json!({
            "viewers": event.get("viewers").and_then(|v| v.as_i64()).unwrap_or(0),
        }),
        "channel.cheer" => serde_json::json!({
            "bits": event.get("bits").and_then(|v| v.as_i64()).unwrap_or(0),
            "is_anonymous": event.get("is_anonymous").and_then(|v| v.as_bool()).unwrap_or(false),
        }),
        // gh #287 S10 parity: real Twitch payload is `{id,
        // broadcaster_user_id, broadcaster_user_login,
        // broadcaster_user_name, type, started_at}`.
        "stream.online" => serde_json::json!({
            "type": event.get("type").and_then(|v| v.as_str()).unwrap_or("live"),
            "started_at": event.get("started_at").and_then(|v| v.as_str()).unwrap_or(""),
        }),
        // `stream.offline` carries no extra fields -- metadata is
        // deliberately empty, matching the legacy module's own
        // `test_normalizes_a_stream_offline_event` assertion.
        _ => serde_json::json!({}),
    };

    PlatformEvent {
        platform: "twitch".to_string(),
        event_type: event_type.to_string(),
        actor: user_login.clone().or_else(|| broadcaster_login.clone()),
        payload: payload_map(serde_json::json!({
            "broadcaster_id": broadcaster_id,
            "broadcaster_login": broadcaster_login,
            "user_id": user_id,
            "user_login": user_login,
            "user_display_name": user_display_name,
            "metadata": metadata,
        })),
        occurred_at: now_rfc3339_millis(),
        source: Some(Source {
            platform: "twitch".to_string(),
            account_id: broadcaster_id.clone(),
            channel_id: Some(broadcaster_id),
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

    /// An escape sequence outside the five known ones (`\s \: \\ \r \n`)
    /// passes the escaped character through literally -- IRCv3's own
    /// `UNESCAPE_SEQ` default case, distinct from every known-escape case
    /// `unescape_tag_value_reverses_known_escapes` above already covers.
    #[test]
    fn unescape_tag_value_passes_through_an_unknown_escape_literally() {
        assert_eq!(unescape_tag_value("a\\xb"), "axb");
    }

    #[test]
    fn parse_tags_handles_empty_and_none_input() {
        assert!(parse_tags(None).is_empty());
        assert!(parse_tags(Some("")).is_empty());
    }

    /// A doubled `;;` separator (or a leading/trailing one) yields an empty
    /// segment between/around real pairs -- skipped outright, not inserted
    /// as a spurious empty-keyed tag.
    #[test]
    fn parse_tags_skips_empty_segments_from_doubled_separators() {
        let tags = parse_tags(Some(";a=1;;b=2;"));
        assert_eq!(tags.len(), 2);
        assert_eq!(tags.get("a").unwrap(), "1");
        assert_eq!(tags.get("b").unwrap(), "2");
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
        let event = normalize_discord(&discord_msg(), &DiscordPermissions::default());
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
        let event = normalize_discord(&msg, &DiscordPermissions::default());
        assert_eq!(event.source.unwrap().account_id, "dm");
    }

    #[test]
    fn discord_occurred_at_is_valid_rfc3339_millis_z() {
        let event = normalize_discord(&discord_msg(), &DiscordPermissions::default());
        assert!(event.occurred_at.ends_with('Z'));
        assert!(chrono::DateTime::parse_from_rfc3339(&event.occurred_at).is_ok());
    }

    /// Default (unresolved/unknown) permissions never leak a privileged
    /// flag into the payload -- the fail-closed baseline every other
    /// `discord_permissions_*` test below is a deviation from.
    #[test]
    fn discord_payload_defaults_is_mod_and_is_broadcaster_false() {
        let event = normalize_discord(&discord_msg(), &DiscordPermissions::default());
        assert_eq!(
            event.payload.get("is_mod").and_then(|v| v.as_bool()),
            Some(false)
        );
        assert_eq!(
            event
                .payload
                .get("is_broadcaster")
                .and_then(|v| v.as_bool()),
            Some(false)
        );
    }

    /// Resolved permissions pass straight through into the payload --
    /// proves the plumbing between [`compute_discord_permissions`] and
    /// [`normalize_discord`], independent of the bit-computation tests
    /// below.
    #[test]
    fn discord_payload_carries_resolved_permissions_through() {
        let perms = DiscordPermissions {
            is_mod: true,
            is_broadcaster: true,
        };
        let event = normalize_discord(&discord_msg(), &perms);
        assert_eq!(
            event.payload.get("is_mod").and_then(|v| v.as_bool()),
            Some(true)
        );
        assert_eq!(
            event
                .payload
                .get("is_broadcaster")
                .and_then(|v| v.as_bool()),
            Some(true)
        );
    }

    #[test]
    fn discord_permissions_guild_owner_is_broadcaster_and_mod() {
        let perms = compute_discord_permissions("owner-1", "owner-1", &[], &[]);
        assert!(perms.is_broadcaster);
        assert!(perms.is_mod);
    }

    #[test]
    fn discord_permissions_administrator_role_is_broadcaster_and_mod() {
        let member_roles = vec!["role-admin".to_string()];
        let guild_roles = vec![("role-admin".to_string(), PERM_ADMINISTRATOR)];
        let perms = compute_discord_permissions("user-1", "owner-1", &member_roles, &guild_roles);
        assert!(perms.is_broadcaster);
        assert!(perms.is_mod);
    }

    #[test]
    fn discord_permissions_manage_messages_role_is_mod_only() {
        let member_roles = vec!["role-mod".to_string()];
        let guild_roles = vec![("role-mod".to_string(), PERM_MANAGE_MESSAGES)];
        let perms = compute_discord_permissions("user-1", "owner-1", &member_roles, &guild_roles);
        assert!(!perms.is_broadcaster);
        assert!(perms.is_mod);
    }

    #[test]
    fn discord_permissions_plain_member_is_neither() {
        let member_roles = vec!["role-everyone".to_string()];
        let guild_roles = vec![("role-everyone".to_string(), 0u64)];
        let perms = compute_discord_permissions("user-1", "owner-1", &member_roles, &guild_roles);
        assert!(!perms.is_broadcaster);
        assert!(!perms.is_mod);
    }

    /// No roles resolved at all (e.g. a DM, or a lookup failure the caller
    /// in `crate::ingest::discord` already defaulted before ever reaching
    /// this function) -- fails closed, never defaults to allow.
    #[test]
    fn discord_permissions_no_roles_is_neither() {
        let perms = compute_discord_permissions("user-1", "owner-1", &[], &[]);
        assert!(!perms.is_broadcaster);
        assert!(!perms.is_mod);
    }

    /// A role unrelated to moderation (e.g. a cosmetic/color role) must
    /// never accidentally grant either flag -- proves the permission check
    /// is bit-specific, not "has any non-zero role".
    #[test]
    fn discord_permissions_unrelated_permission_bit_grants_neither() {
        const PERM_SEND_MESSAGES: u64 = 1 << 11;
        let member_roles = vec!["role-chatty".to_string()];
        let guild_roles = vec![("role-chatty".to_string(), PERM_SEND_MESSAGES)];
        let perms = compute_discord_permissions("user-1", "owner-1", &member_roles, &guild_roles);
        assert!(!perms.is_broadcaster);
        assert!(!perms.is_mod);
    }

    #[test]
    fn eventsub_raid_normalizes_metadata_and_source() {
        let event = serde_json::json!({
            "broadcaster_user_id": "111",
            "broadcaster_user_login": "somechannel",
            "from_broadcaster_user_id": "222",
            "from_broadcaster_user_login": "raider",
            "from_broadcaster_user_name": "Raider",
            "viewers": 42,
        });
        let platform_event = normalize_twitch_eventsub("channel.raid", &event, None);
        assert_eq!(platform_event.platform, "twitch");
        assert_eq!(platform_event.event_type, "channel.raid");
        assert_eq!(platform_event.actor.as_deref(), Some("raider"));
        assert_eq!(
            platform_event
                .payload
                .get("broadcaster_id")
                .and_then(|v| v.as_str()),
            Some("111")
        );
        assert_eq!(
            platform_event
                .payload
                .get("user_id")
                .and_then(|v| v.as_str()),
            Some("222")
        );
        assert_eq!(
            platform_event
                .payload
                .get("metadata")
                .and_then(|m| m.get("viewers"))
                .and_then(|v| v.as_i64()),
            Some(42)
        );
        let source = platform_event
            .source
            .expect("eventsub normalizer always sets source");
        assert_eq!(source.account_id, "111");
        assert_eq!(source.channel_id.as_deref(), Some("111"));
    }

    #[test]
    fn eventsub_falls_back_to_subscription_broadcaster_id_when_event_omits_it() {
        let event = serde_json::json!({});
        let platform_event = normalize_twitch_eventsub("stream.offline", &event, Some("999"));
        assert_eq!(
            platform_event
                .payload
                .get("broadcaster_id")
                .and_then(|v| v.as_str()),
            Some("999")
        );
        assert_eq!(
            platform_event.payload.get("metadata"),
            Some(&serde_json::json!({}))
        );
    }

    #[test]
    fn eventsub_stream_online_carries_type_and_started_at() {
        let event = serde_json::json!({
            "broadcaster_user_id": "111",
            "type": "live",
            "started_at": "2026-09-28T00:00:00Z",
        });
        let platform_event = normalize_twitch_eventsub("stream.online", &event, None);
        let metadata = platform_event.payload.get("metadata").unwrap();
        assert_eq!(metadata.get("type").and_then(|v| v.as_str()), Some("live"));
        assert_eq!(
            metadata.get("started_at").and_then(|v| v.as_str()),
            Some("2026-09-28T00:00:00Z")
        );
    }

    #[test]
    fn eventsub_occurred_at_is_valid_rfc3339_millis_z() {
        let event = serde_json::json!({"broadcaster_user_id": "111"});
        let platform_event = normalize_twitch_eventsub("channel.raid", &event, None);
        assert!(platform_event.occurred_at.ends_with('Z'));
        assert!(chrono::DateTime::parse_from_rfc3339(&platform_event.occurred_at).is_ok());
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
