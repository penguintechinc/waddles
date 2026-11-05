//! Normalization of Discord payloads into `normalized-event` shapes.
//!
//! `MESSAGE_CREATE` is a field-for-field port of
//! `penguin_connector_discord::gateway::normalize_message_create` +
//! `svc_ingest::normalize::normalize_discord` (event type `message`, the exact
//! seven payload keys). Interactions are NEW surface -- the hardcoded path has
//! no interaction handling -- so they have no golden to match; their shape is
//! documented in the README and pinned by this module's tests.

use serde_json::{Value, json};

use crate::error::{OverlayError, Result};
use crate::perms::{DiscordPermissions, from_bits};
use crate::session::is_snowflake;

/// Platform slug on every event this overlay emits.
pub const PLATFORM: &str = "discord";

/// The overlay-side mirror of WIT `normalized-event` (the glue converts).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Event {
    /// Always [`PLATFORM`].
    pub platform: String,
    /// Dotted lowercase event type (`message`, `interaction.command`, ...).
    pub event_type: String,
    /// Author handle; the host's ingest tokenizes it exactly as for the hardcoded path.
    pub actor: Option<String>,
    /// Canonical JSON object text.
    pub payload_json: String,
    /// RFC 3339 arrival time (the host-supplied `received-at`).
    pub occurred_at: String,
}

/// One inbound chat message (mirror of the crate's `ChatMessage`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChatMessage {
    /// Guild id, `None` for a DM.
    pub guild_id: Option<String>,
    /// Channel id.
    pub channel_id: String,
    /// Message snowflake.
    pub message_id: String,
    /// Author user id.
    pub author_id: String,
    /// Author username.
    pub author_username: String,
    /// Message text.
    pub content: String,
}

/// Normalizes a `MESSAGE_CREATE` `d`. `None` = drop (self-authored by id, or
/// `author`/`author.id`/`channel_id` missing). Other bots are KEPT; an unknown
/// `self_user_id` never filters. Everything else reads leniently (empty
/// default), exactly as the crate does.
#[must_use]
pub fn normalize_message_create(d: &Value, self_user_id: Option<&str>) -> Option<ChatMessage> {
    let author = d.get("author")?;
    let author_id = author.get("id")?.as_str()?.to_string();
    if self_user_id.is_some_and(|me| me == author_id) {
        return None;
    }
    let lenient = |v: Option<&Value>| v.and_then(Value::as_str).unwrap_or_default().to_string();
    Some(ChatMessage {
        guild_id: d
            .get("guild_id")
            .and_then(Value::as_str)
            .map(str::to_string),
        channel_id: d.get("channel_id")?.as_str()?.to_string(),
        message_id: lenient(d.get("id")),
        author_id,
        author_username: lenient(author.get("username")),
        content: lenient(d.get("content")),
    })
}

/// Builds the `message` event (payload keys identical to `normalize_discord`).
/// `actor` is the ALREADY-tokenized canonical UUID/pseudonym for the author
/// (`identity.lookup`), never the raw handle (WIT `normalized-event.actor`).
#[must_use]
pub fn message_event(
    msg: &ChatMessage,
    perms: DiscordPermissions,
    occurred_at: &str,
    actor: String,
) -> Event {
    let payload = json!({
        "text": msg.content,
        "guild_id": msg.guild_id,
        "channel_id": msg.channel_id,
        "message_id": msg.message_id,
        "author_id": msg.author_id,
        "is_mod": perms.is_mod,
        "is_broadcaster": perms.is_broadcaster,
    });
    Event {
        platform: PLATFORM.to_string(),
        event_type: "message".to_string(),
        actor: Some(actor),
        payload_json: payload.to_string(),
        occurred_at: occurred_at.to_string(),
    }
}

/// Maps an interaction `type` to its event-type suffix. PING (1) is handled by
/// the host (3-second PONG deadline) and is NOT an event.
fn interaction_kind(t: u64) -> Option<&'static str> {
    match t {
        2 => Some("interaction.command"),
        3 => Some("interaction.component"),
        4 => Some("interaction.autocomplete"),
        5 => Some("interaction.modal"),
        _ => None,
    }
}

/// Normalizes an interaction body (webhook POST, or a gateway
/// `INTERACTION_CREATE` `d`). Returns `Ok(None)` for PING.
///
/// The interaction `token` (a 15-minute reply credential) is deliberately NOT
/// copied into the event. Permissions come from the interaction's own resolved
/// `member.permissions` bitfield -- no REST call.
///
/// # Errors
/// [`OverlayError::Malformed`] for a missing/invalid `id`/`type`,
/// [`OverlayError::Unsupported`] for an interaction type this overlay does not know.
pub fn interaction_event(
    body: &Value,
    occurred_at: &str,
    tokenize: &dyn Fn(&str) -> Result<String>,
) -> Result<Option<Event>> {
    let t = body
        .get("type")
        .and_then(Value::as_u64)
        .ok_or_else(|| OverlayError::Malformed("interaction missing type".into()))?;
    if t == 1 {
        return Ok(None);
    }
    let kind = interaction_kind(t)
        .ok_or_else(|| OverlayError::Unsupported(format!("interaction type {t}")))?;
    let id = body
        .get("id")
        .and_then(Value::as_str)
        .filter(|s| is_snowflake(s))
        .ok_or_else(|| OverlayError::Malformed("interaction id not a snowflake".into()))?;

    let user = body
        .get("member")
        .and_then(|m| m.get("user"))
        .or_else(|| body.get("user"));
    let author_id = user.and_then(|u| u.get("id")).and_then(Value::as_str);
    let guild_id = body.get("guild_id").and_then(Value::as_str);
    let channel_id = body.get("channel_id").and_then(Value::as_str).or_else(|| {
        body.get("channel")
            .and_then(|c| c.get("id"))
            .and_then(Value::as_str)
    });
    let bits: u64 = body
        .get("member")
        .and_then(|m| m.get("permissions"))
        .and_then(Value::as_str)
        .and_then(|p| p.parse().ok())
        .unwrap_or(0);
    let perms = from_bits(bits, false);
    let data = body.get("data");
    let field = |k: &str| data.and_then(|d| d.get(k)).cloned().unwrap_or(Value::Null);

    let payload = json!({
        "interaction_id": id,
        "guild_id": guild_id,
        "channel_id": channel_id,
        "author_id": author_id,
        "command": field("name"),
        "custom_id": field("custom_id"),
        "options": field("options"),
        "values": field("values"),
        "is_mod": perms.is_mod,
        "is_broadcaster": perms.is_broadcaster,
    });
    Ok(Some(Event {
        platform: PLATFORM.to_string(),
        event_type: kind.to_string(),
        actor: author_id.map(tokenize).transpose()?,
        payload_json: payload.to_string(),
        occurred_at: occurred_at.to_string(),
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn msg(v: Value, me: Option<&str>) -> Option<ChatMessage> {
        normalize_message_create(&v, me)
    }

    #[test]
    fn keeps_other_bots_filters_self_and_never_filters_when_unknown() {
        let base = json!({"author":{"id":"5","username":"B","bot":true},"channel_id":"2"});
        assert!(msg(base.clone(), Some("9")).is_some());
        assert!(msg(json!({"author":{"id":"9"},"channel_id":"2"}), Some("9")).is_none());
        assert!(msg(json!({"author":{"id":"9"},"channel_id":"2"}), None).is_some());
    }

    #[test]
    fn missing_required_fields_drop_but_optional_ones_default() {
        assert!(msg(json!({"channel_id":"2"}), None).is_none());
        assert!(msg(json!({"author":{},"channel_id":"2"}), None).is_none());
        assert!(msg(json!({"author":{"id":"1"}}), None).is_none());
        let m = msg(
            json!({"author":{"id":"1"},"channel_id":"2","content":null}),
            None,
        )
        .unwrap();
        assert_eq!(
            (
                m.content.as_str(),
                m.message_id.as_str(),
                m.author_username.as_str()
            ),
            ("", "", "")
        );
        assert_eq!(m.guild_id, None);
    }

    #[test]
    fn message_event_has_exactly_the_seven_payload_keys() {
        let m = msg(
            json!({"author":{"id":"4","username":"u"},"channel_id":"2","id":"3","content":"hi","guild_id":"1"}),
            None,
        )
        .unwrap();
        let e = message_event(
            &m,
            DiscordPermissions {
                is_mod: true,
                is_broadcaster: false,
            },
            "t",
            "actor-uuid".to_string(),
        );
        let p: Value = serde_json::from_str(&e.payload_json).unwrap();
        let mut keys: Vec<_> = p.as_object().unwrap().keys().cloned().collect();
        keys.sort();
        assert_eq!(
            keys,
            [
                "author_id",
                "channel_id",
                "guild_id",
                "is_broadcaster",
                "is_mod",
                "message_id",
                "text"
            ]
        );
        assert_eq!(
            (e.platform.as_str(), e.event_type.as_str()),
            ("discord", "message")
        );
        assert_eq!(e.actor.as_deref(), Some("actor-uuid"));
    }

    fn tok(id: &str) -> Result<String> {
        Ok(format!("uuid-of-{id}"))
    }

    fn interaction(t: u64, data: Value) -> Value {
        json!({
            "id":"777","application_id":"1","type":t,"token":"SECRET-TOKEN",
            "guild_id":"111","channel_id":"222",
            "member":{"user":{"id":"444","username":"someuser"},"permissions":"8192"},
            "data":data
        })
    }

    #[test]
    fn interaction_kinds_and_payload_never_carry_the_token() {
        for (t, kind) in [
            (2, "interaction.command"),
            (3, "interaction.component"),
            (4, "interaction.autocomplete"),
            (5, "interaction.modal"),
        ] {
            let e = interaction_event(
                &interaction(t, json!({"name":"ban","custom_id":"x"})),
                "ts",
                &tok,
            )
            .unwrap()
            .unwrap();
            assert_eq!(e.event_type, kind);
            assert!(!e.payload_json.contains("SECRET-TOKEN"));
            let p: Value = serde_json::from_str(&e.payload_json).unwrap();
            assert_eq!(p["command"], "ban");
            assert_eq!(p["author_id"], "444");
            assert_eq!(
                (p["is_mod"].as_bool(), p["is_broadcaster"].as_bool()),
                (Some(true), Some(false))
            );
            assert_eq!(e.actor.as_deref(), Some("uuid-of-444"));
        }
    }

    #[test]
    fn dm_interaction_uses_top_level_user_and_channel_object() {
        let b = json!({"id":"9","type":2,"user":{"id":"5","username":"dmuser"},"channel":{"id":"77"},"data":{"name":"x"}});
        let e = interaction_event(&b, "t", &tok).unwrap().unwrap();
        let p: Value = serde_json::from_str(&e.payload_json).unwrap();
        assert_eq!(
            (
                p["author_id"].clone(),
                p["channel_id"].clone(),
                p["guild_id"].clone()
            ),
            (json!("5"), json!("77"), Value::Null)
        );
        assert_eq!(p["is_mod"], false);
    }

    #[test]
    fn ping_is_not_an_event_and_bad_input_fails_loud() {
        assert_eq!(
            interaction_event(&json!({"id":"1","type":1}), "t", &tok).unwrap(),
            None
        );
        assert!(matches!(
            interaction_event(&json!({"id":"1"}), "t", &tok),
            Err(OverlayError::Malformed(_))
        ));
        assert!(matches!(
            interaction_event(&json!({"id":"1","type":9}), "t", &tok),
            Err(OverlayError::Unsupported(_))
        ));
        assert!(matches!(
            interaction_event(&json!({"id":"abc","type":2}), "t", &tok),
            Err(OverlayError::Malformed(_))
        ));
    }
}
