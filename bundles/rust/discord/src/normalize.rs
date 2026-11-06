//! Normalization of Discord payloads into `normalized-event` shapes.
//!
//! `MESSAGE_CREATE` is a field-for-field port of
//! `penguin_connector_discord::gateway::normalize_message_create` +
//! `svc_ingest::normalize::normalize_discord` (event type `message`, the exact
//! seven payload keys). Interactions are NEW surface -- the hardcoded path has
//! no interaction handling -- so they have no golden to match; their shape is
//! documented in the README and pinned by this module's tests.

use serde_json::{Value, json};

use crate::error::{ConnectorError, Result};
use crate::perms::{DiscordPermissions, from_bits};
use crate::session::is_snowflake;

/// Platform slug on every event this connector emits.
pub const PLATFORM: &str = "discord";

/// The connector-side mirror of WIT `normalized-event` (the glue converts).
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

/// One inbound chat message (mirror of the crate's `ChatMessage`, minus the
/// username: the actor is the tokenized UUID, so the raw handle is never carried).
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
    /// Message text (empty for an attachment-only message).
    pub content: String,
}

/// A REQUIRED snowflake field: missing / non-string / non-numeric is a typed error,
/// never an empty id (an empty id must not reach an actor or source key).
fn req_snowflake(v: Option<&Value>, field: &str) -> Result<String> {
    match v {
        Some(Value::String(s)) if is_snowflake(s) => Ok(s.clone()),
        _ => Err(ConnectorError::Malformed(format!(
            "`{field}` missing or not a snowflake"
        ))),
    }
}

/// An OPTIONAL snowflake (absent / null = not present, e.g. a DM has no guild);
/// present-but-mistyped is a typed error.
fn opt_snowflake(v: Option<&Value>, field: &str) -> Result<Option<String>> {
    match v {
        None | Some(Value::Null) => Ok(None),
        Some(_) => req_snowflake(v, field).map(Some),
    }
}

/// Message text. Discord omits/nulls `content` when the Message Content intent is
/// not yet in effect or the message is attachment-only: that is a documented,
/// explicit empty string (parity corpus `empty_content_attachment_only`); any
/// other type is a typed error.
fn message_text(v: Option<&Value>) -> Result<String> {
    match v {
        None | Some(Value::Null) => Ok(String::new()),
        Some(Value::String(s)) => Ok(s.clone()),
        Some(_) => Err(ConnectorError::Malformed(
            "`content` is not a string".into(),
        )),
    }
}

/// Normalizes a `MESSAGE_CREATE` `d`. `Ok(None)` = the bot's own message (by id;
/// other bots are KEPT; an unknown `self_user_id` never filters).
///
/// # Errors
/// [`ConnectorError::Malformed`] when `author.id`, `channel_id` or the message `id`
/// is missing/not a snowflake, `guild_id` is mistyped, or `content` is not a
/// string. Fails loudly (the host drops and counts the frame) instead of
/// defaulting a missing id to an empty string.
pub fn normalize_message_create(
    d: &Value,
    self_user_id: Option<&str>,
) -> Result<Option<ChatMessage>> {
    let author_id = req_snowflake(d.get("author").and_then(|a| a.get("id")), "author.id")?;
    if self_user_id.is_some_and(|me| me == author_id) {
        return Ok(None);
    }
    Ok(Some(ChatMessage {
        guild_id: opt_snowflake(d.get("guild_id"), "guild_id")?,
        channel_id: req_snowflake(d.get("channel_id"), "channel_id")?,
        message_id: req_snowflake(d.get("id"), "id")?,
        author_id,
        content: message_text(d.get("content"))?,
    }))
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

/// One `GUILD_MEMBER_UPDATE`: the member's CURRENT full role set (Discord sends
/// no add/remove diff, only the resulting `roles`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MemberUpdate {
    /// Guild id (a member update is always guild-scoped).
    pub guild_id: String,
    /// The member's user id (resolved to the event `actor`, never emitted as a handle).
    pub user_id: String,
    /// Role snowflakes the member holds now (sorted + deduplicated: deterministic payload).
    pub role_ids: Vec<String>,
}

/// Normalizes a `GUILD_MEMBER_UPDATE` `d`. `Ok(None)` = the bot's own member
/// record (by id): our own role changes are never reconciled back.
///
/// # Errors
/// [`ConnectorError::Malformed`] when `guild_id` / `user.id` is missing or not a
/// snowflake, or `roles` is missing / not an array of snowflake strings. Fails
/// loudly rather than reconciling a guess (roles are authz).
pub fn normalize_member_update(
    d: &Value,
    self_user_id: Option<&str>,
) -> Result<Option<MemberUpdate>> {
    let user_id = req_snowflake(d.get("user").and_then(|u| u.get("id")), "user.id")?;
    if self_user_id.is_some_and(|me| me == user_id) {
        return Ok(None);
    }
    Ok(Some(MemberUpdate {
        guild_id: req_snowflake(d.get("guild_id"), "guild_id")?,
        user_id,
        role_ids: role_id_list(d.get("roles"), "roles")?,
    }))
}

/// A REQUIRED array of snowflake strings, returned sorted + deduplicated.
fn role_id_list(v: Option<&Value>, field: &str) -> Result<Vec<String>> {
    let Some(Value::Array(items)) = v else {
        return Err(ConnectorError::Malformed(format!(
            "`{field}` missing or not an array"
        )));
    };
    let mut out = Vec::with_capacity(items.len());
    for item in items {
        out.push(req_snowflake(Some(item), field)?);
    }
    out.sort();
    out.dedup();
    Ok(out)
}

/// Builds the `guild.member.update` event. `actor` is the ALREADY-tokenized
/// canonical UUID/pseudonym of the member (`identity.lookup`). The payload
/// carries the guild id, the member snowflake (same convention as `author_id`
/// on `message`; the hub links it to a hub user) and role ids -- never a
/// username / nickname / avatar.
#[must_use]
pub fn member_update_event(update: &MemberUpdate, occurred_at: &str, actor: String) -> Event {
    let payload = json!({
        "guild_id": update.guild_id,
        "user_id": update.user_id,
        "role_ids": update.role_ids,
    });
    Event {
        platform: PLATFORM.to_string(),
        event_type: "guild.member.update".to_string(),
        actor: Some(actor),
        payload_json: payload.to_string(),
        occurred_at: occurred_at.to_string(),
    }
}

/// Which `GUILD_ROLE_*` dispatch a role event came from.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RoleChange {
    /// `GUILD_ROLE_CREATE`.
    Create,
    /// `GUILD_ROLE_UPDATE`.
    Update,
    /// `GUILD_ROLE_DELETE`.
    Delete,
}

impl RoleChange {
    fn suffix(self) -> &'static str {
        match self {
            Self::Create => "create",
            Self::Update => "update",
            Self::Delete => "delete",
        }
    }
}

/// Normalizes a `GUILD_ROLE_CREATE|UPDATE|DELETE` `d` into a `guild.role.*` event.
/// CREATE/UPDATE nest the role under `role`; DELETE carries a bare `role_id`.
/// No actor (the dispatch names none) and no role name is emitted -- only ids.
///
/// # Errors
/// [`ConnectorError::Malformed`] when `guild_id` or the role id is missing / not
/// a snowflake.
pub fn role_event(change: RoleChange, d: &Value, occurred_at: &str) -> Result<Event> {
    let role_id = match change {
        RoleChange::Delete => req_snowflake(d.get("role_id"), "role_id")?,
        RoleChange::Create | RoleChange::Update => {
            req_snowflake(d.get("role").and_then(|r| r.get("id")), "role.id")?
        }
    };
    let payload = json!({
        "guild_id": req_snowflake(d.get("guild_id"), "guild_id")?,
        "role_id": role_id,
    });
    Ok(Event {
        platform: PLATFORM.to_string(),
        event_type: format!("guild.role.{}", change.suffix()),
        actor: None,
        payload_json: payload.to_string(),
        occurred_at: occurred_at.to_string(),
    })
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
/// [`ConnectorError::Malformed`] for a missing/invalid `id`/`type`,
/// [`ConnectorError::Unsupported`] for an interaction type this connector does not know.
pub fn interaction_event(
    body: &Value,
    occurred_at: &str,
    tokenize: &dyn Fn(&str) -> Result<String>,
) -> Result<Option<Event>> {
    let t = body
        .get("type")
        .and_then(Value::as_u64)
        .ok_or_else(|| ConnectorError::Malformed("interaction missing type".into()))?;
    if t == 1 {
        return Ok(None);
    }
    let kind = interaction_kind(t)
        .ok_or_else(|| ConnectorError::Unsupported(format!("interaction type {t}")))?;
    let id = req_snowflake(body.get("id"), "interaction id")?;

    let user = body
        .get("member")
        .and_then(|m| m.get("user"))
        .or_else(|| body.get("user"));
    let author_id = opt_snowflake(user.and_then(|u| u.get("id")), "user.id")?;
    let guild_id = opt_snowflake(body.get("guild_id"), "guild_id")?;
    let channel_id = match body.get("channel_id") {
        Some(c) => opt_snowflake(Some(c), "channel_id")?,
        None => opt_snowflake(body.get("channel").and_then(|c| c.get("id")), "channel.id")?,
    };
    // No `member` (a DM interaction) = no guild permissions; a present-but-garbled
    // bitfield is a typed error, never silently 0.
    let bits: u64 = match body.get("member").and_then(|m| m.get("permissions")) {
        None | Some(Value::Null) => 0,
        Some(Value::String(p)) => p
            .parse()
            .map_err(|_| ConnectorError::Malformed("member.permissions not a bitfield".into()))?,
        Some(_) => {
            return Err(ConnectorError::Malformed(
                "member.permissions not a string".into(),
            ));
        }
    };
    let perms = from_bits(bits, false);
    let data = body.get("data");
    // An absent optional `data.<k>` is JSON null in the payload (documented shape).
    let field = |k: &str| match data.and_then(|d| d.get(k)) {
        Some(v) => v.clone(),
        None => Value::Null,
    };

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
        actor: author_id.as_deref().map(tokenize).transpose()?,
        payload_json: payload.to_string(),
        occurred_at: occurred_at.to_string(),
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn msg(v: Value, me: Option<&str>) -> Result<Option<ChatMessage>> {
        normalize_message_create(&v, me)
    }

    fn base() -> Value {
        json!({"author":{"id":"5","username":"B","bot":true},"channel_id":"2","id":"3","content":"x"})
    }

    #[test]
    fn keeps_other_bots_filters_self_and_never_filters_when_unknown() {
        assert!(msg(base(), Some("9")).unwrap().is_some());
        assert_eq!(msg(base(), Some("5")).unwrap(), None);
        assert!(msg(base(), None).unwrap().is_some());
    }

    #[test]
    fn missing_or_mistyped_required_ids_fail_loud_never_default_to_empty() {
        // regression (no-silent-fallback gate): an empty id must never flow into an
        // actor / source key (tenant-isolation risk).
        let mut cases: Vec<Value> = Vec::new();
        for key in ["id", "channel_id"] {
            let mut v = base();
            v.as_object_mut().unwrap().remove(key);
            cases.push(v);
            let mut v = base();
            v[key] = json!("");
            cases.push(v);
            let mut v = base();
            v[key] = json!(5);
            cases.push(v);
            let mut v = base();
            v[key] = json!("12ab");
            cases.push(v);
        }
        for bad_author in [json!({}), json!({"id":""}), json!({"id":7}), json!("x")] {
            let mut v = base();
            v["author"] = bad_author;
            cases.push(v);
        }
        let mut v = base();
        v.as_object_mut().unwrap().remove("author");
        cases.push(v);
        let mut v = base();
        v["guild_id"] = json!(111); // mistyped (present but not a string)
        cases.push(v);
        let mut v = base();
        v["content"] = json!(5);
        cases.push(v);
        for (i, v) in cases.into_iter().enumerate() {
            assert!(
                matches!(msg(v.clone(), None), Err(ConnectorError::Malformed(_))),
                "case {i}: {v}"
            );
        }
    }

    #[test]
    fn absent_content_and_guild_are_explicit_documented_cases() {
        let mut v = base();
        v["content"] = Value::Null;
        assert_eq!(msg(v, None).unwrap().unwrap().content, "");
        let mut v = base();
        v.as_object_mut().unwrap().remove("content");
        let m = msg(v, None).unwrap().unwrap();
        assert_eq!((m.content.as_str(), m.guild_id), ("", None));
        let mut v = base();
        v["guild_id"] = Value::Null;
        assert_eq!(msg(v, None).unwrap().unwrap().guild_id, None);
    }

    #[test]
    fn garbled_interaction_fields_fail_loud() {
        let ok = |m: Value| interaction_event(&m, "t", &tok);
        let b = || json!({"id":"9","type":2,"guild_id":"1","channel_id":"2","member":{"user":{"id":"4"},"permissions":"8"},"data":{"name":"x"}});
        assert!(ok(b()).unwrap().is_some());
        for (k, v) in [
            ("guild_id", json!(1)),
            ("channel_id", json!("")),
            ("id", json!("")),
        ] {
            let mut m = b();
            m[k] = v;
            assert!(ok(m).is_err(), "{k}");
        }
        let mut m = b();
        m["member"]["permissions"] = json!("not-a-number");
        assert!(ok(m).is_err());
        let mut m = b();
        m["member"]["permissions"] = json!(8);
        assert!(ok(m).is_err());
        let mut m = b();
        m["member"]["user"]["id"] = json!("");
        assert!(ok(m).is_err());
    }

    #[test]
    fn message_event_has_exactly_the_seven_payload_keys() {
        let m = msg(
            json!({"author":{"id":"4","username":"u"},"channel_id":"2","id":"3","content":"hi","guild_id":"1"}),
            None,
        )
        .unwrap()
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
            Err(ConnectorError::Malformed(_))
        ));
        assert!(matches!(
            interaction_event(&json!({"id":"1","type":9}), "t", &tok),
            Err(ConnectorError::Unsupported(_))
        ));
        assert!(matches!(
            interaction_event(&json!({"id":"abc","type":2}), "t", &tok),
            Err(ConnectorError::Malformed(_))
        ));
    }

    #[test]
    fn member_update_carries_sorted_roles_and_never_a_handle() {
        let d = json!({"guild_id":"111","user":{"id":"444","username":"secretname"},
            "nick":"nicky","roles":["30","20","20"]});
        let u = normalize_member_update(&d, None).unwrap().unwrap();
        assert_eq!(u.role_ids, ["20", "30"]);
        let e = member_update_event(&u, "ts", "uuid-of-444".to_string());
        assert_eq!(e.event_type, "guild.member.update");
        assert_eq!(e.actor.as_deref(), Some("uuid-of-444"));
        assert!(!e.payload_json.contains("secretname") && !e.payload_json.contains("nicky"));
        let p: Value = serde_json::from_str(&e.payload_json).unwrap();
        assert_eq!(
            p,
            json!({"guild_id":"111","user_id":"444","role_ids":["20","30"]})
        );
    }

    #[test]
    fn member_update_for_the_bot_is_dropped_and_bad_input_fails_loud() {
        let d = json!({"guild_id":"111","user":{"id":"999"},"roles":["1"]});
        assert_eq!(normalize_member_update(&d, Some("999")).unwrap(), None);
        assert!(normalize_member_update(&d, Some("1")).unwrap().is_some());
        for bad in [
            json!({"user":{"id":"4"},"roles":[]}),
            json!({"guild_id":"1","roles":[]}),
            json!({"guild_id":"1","user":{"id":"4"}}),
            json!({"guild_id":"1","user":{"id":"4"},"roles":"x"}),
            json!({"guild_id":"1","user":{"id":"4"},"roles":["abc"]}),
            json!({"guild_id":"1","user":{"id":"4"},"roles":[5]}),
        ] {
            assert!(
                matches!(
                    normalize_member_update(&bad, None),
                    Err(ConnectorError::Malformed(_))
                ),
                "{bad}"
            );
        }
    }

    #[test]
    fn role_events_map_create_update_delete_with_ids_only() {
        let nested = json!({"guild_id":"111","role":{"id":"55","name":"Secret Mods"}});
        for (c, t) in [
            (RoleChange::Create, "guild.role.create"),
            (RoleChange::Update, "guild.role.update"),
        ] {
            let e = role_event(c, &nested, "ts").unwrap();
            assert_eq!((e.event_type.as_str(), e.actor.clone()), (t, None));
            assert!(!e.payload_json.contains("Secret Mods"));
            let p: Value = serde_json::from_str(&e.payload_json).unwrap();
            assert_eq!(p, json!({"guild_id":"111","role_id":"55"}));
        }
        let del = role_event(
            RoleChange::Delete,
            &json!({"guild_id":"111","role_id":"55"}),
            "ts",
        )
        .unwrap();
        assert_eq!(del.event_type, "guild.role.delete");
        for (c, bad) in [
            (RoleChange::Create, json!({"guild_id":"1"})),
            (RoleChange::Update, json!({"guild_id":"1","role":{}})),
            (RoleChange::Delete, json!({"guild_id":"1"})),
            (RoleChange::Delete, json!({"role_id":"5"})),
        ] {
            assert!(matches!(
                role_event(c, &bad, "ts"),
                Err(ConnectorError::Malformed(_))
            ));
        }
    }
}
