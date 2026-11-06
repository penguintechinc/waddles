//! `sender.build-request`: Discord REST request TEMPLATES (part B).
//!
//! The connector never holds a token and never picks a destination:
//!
//! * the credential is a `secret-refs` entry (`Authorization` -> opaque handle);
//!   the host substitutes the real `Bot ...` value at send time;
//! * the destination channel / guild is a `{target.channel_id}` /
//!   `{target.guild_id}` placeholder the HOST fills from the binding resolved
//!   from `target-source-id` (`app_outbound_bindings`). Payloads that try to
//!   name a destination are REFUSED (unknown fields are rejected), so an app
//!   bundle cannot steer a send to a channel it does not own;
//! * delete is channel-scoped (`/channels/{target.channel_id}/messages/{id}`),
//!   never resolved from a message id alone;
//! * guild scheduled events (`event.create|update|cancel|delete`) are pinned to
//!   `/guilds/{target.guild_id}/scheduled-events[/{id}]`: the guild is the
//!   HOST-filled binding placeholder, a `guild_id`/`channel_id` in the payload
//!   is refused, and every body is a fixed `EXTERNAL`/guild-only event shape.
//!
//! Every message body carries `allowed_mentions: {"parse":["users"]}` (the
//! `discord_mentions` backstop of the hardcoded REST client).

use serde_json::{Map, Value, json};

use crate::error::{ConnectorError, Result};
use crate::sanitize::guard_text;
use crate::session::is_snowflake;

/// Discord REST base the host pins egress to.
pub const API_BASE: &str = "https://discord.com/api/v10";
/// Host-substituted placeholder: the bound channel.
pub const TARGET_CHANNEL: &str = "{target.channel_id}";
/// Host-substituted placeholder: the bound guild.
pub const TARGET_GUILD: &str = "{target.guild_id}";
/// Host-substituted placeholder for a DM: the channel the HOST opened itself
/// (`POST /users/@me/channels`) for this operation's recipient, filled ONLY from
/// Discord's reply to that call. The host rejects any other source for it.
pub const DM_CHANNEL: &str = "{dm.channel_id}";
/// Host-only pseudo-header carrying the `{user:<uuid>}` recipient of a `dm.send`.
/// The host MUST consume and strip it; it is never sent to Discord.
pub const HOST_DM_RECIPIENT_HEADER: &str = "X-Waddle-Host-Dm-Recipient";
/// Opaque secret handle for the bot credential.
pub const AUTH_HANDLE: &str = "discord-bot";
/// Discord's hard message-content limit (characters).
pub const MAX_CONTENT_CHARS: usize = 2000;

/// Mirror of WIT `sender.action`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Action {
    /// `chat.send`, `chat.delete`, `dm.send`, `role.add`, `role.remove`,
    /// `event.create`, `event.update`, `event.cancel`, `event.delete`.
    pub kind: String,
    /// Opaque binding id the host resolves.
    pub target_source_id: String,
    /// Kind-specific JSON object.
    pub payload_json: String,
}

/// Mirror of WIT `sender.http-request-tpl`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HttpTpl {
    /// HTTP method.
    pub method: String,
    /// URL with host-substituted `{target.*}` placeholders.
    pub url: String,
    /// Non-secret headers.
    pub headers: Vec<(String, String)>,
    /// Request body, if any.
    pub body: Option<Vec<u8>>,
    /// `(header slot, opaque handle)`.
    pub secret_refs: Vec<(String, String)>,
}

/// Parses the payload as an object whose keys are exactly within `allowed`.
fn object(payload: &str, allowed: &[&str]) -> Result<Map<String, Value>> {
    let v: Value = serde_json::from_str(payload)
        .map_err(|_| ConnectorError::Malformed("payload is not JSON".into()))?;
    let Value::Object(map) = v else {
        return Err(ConnectorError::Malformed("payload is not an object".into()));
    };
    if let Some(k) = map.keys().find(|k| !allowed.contains(&k.as_str())) {
        // Includes any attempt to supply channel_id / guild_id.
        return Err(ConnectorError::Malformed(format!(
            "payload field `{k}` is not accepted for this action"
        )));
    }
    Ok(map)
}

fn str_field<'a>(map: &'a Map<String, Value>, key: &str) -> Result<&'a str> {
    map.get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| ConnectorError::Malformed(format!("missing string `{key}`")))
}

fn snowflake(map: &Map<String, Value>, key: &str) -> Result<String> {
    let s = str_field(map, key)?;
    if is_snowflake(s) {
        Ok(s.to_string())
    } else {
        Err(ConnectorError::Malformed(format!(
            "`{key}` is not a snowflake"
        )))
    }
}

/// A user reference: ONLY the `{user:<uuid>}` placeholder. The host resolves it
/// through its community-scoped tokenization; a raw platform id never crosses
/// this boundary (repo PII rule), so snowflakes are refused.
fn user_ref(map: &Map<String, Value>, key: &str) -> Result<String> {
    let s = str_field(map, key)?;
    guard_text(key, s, false)?;
    let placeholder = s
        .strip_prefix("{user:")
        .and_then(|r| r.strip_suffix('}'))
        .is_some_and(|u| u.len() == 36 && u.bytes().all(|b| b.is_ascii_hexdigit() || b == b'-'));
    if placeholder {
        Ok(s.to_string())
    } else {
        Err(ConnectorError::Malformed(format!(
            "`{key}` must be a {{user:<uuid>}} placeholder"
        )))
    }
}

/// A managed-role KEY (`[a-z][a-z0-9_-]{0,31}`), never a snowflake. The host maps
/// the key through the binding's managed-roles allowlist.
fn role_key(map: &Map<String, Value>) -> Result<String> {
    let s = str_field(map, "role")?;
    guard_text("role", s, false)?;
    let mut bytes = s.bytes();
    let ok = s.len() <= 32
        && bytes.next().is_some_and(|b| b.is_ascii_lowercase())
        && bytes.all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_' || b == b'-');
    if ok {
        Ok(s.to_string())
    } else {
        Err(ConnectorError::Malformed(
            "`role` must be a managed-role key ([a-z][a-z0-9_-]{0,31})".into(),
        ))
    }
}

fn content(map: &Map<String, Value>) -> Result<&str> {
    let c = str_field(map, "content")?;
    guard_text("content", c, true)?;
    if c.is_empty() {
        return Err(ConnectorError::Malformed("content is empty".into()));
    }
    if c.chars().count() > MAX_CONTENT_CHARS {
        return Err(ConnectorError::Malformed(
            "content exceeds 2000 characters".into(),
        ));
    }
    Ok(c)
}

fn message_body(text: &str) -> Result<Vec<u8>> {
    serde_json::to_vec(&json!({
        "content": text,
        "allowed_mentions": { "parse": ["users"] },
    }))
    .map_err(|e| ConnectorError::Backend(e.to_string()))
}

/// Discord's own field-length limits (Guild Scheduled Event resource).
pub const MAX_EVENT_NAME_CHARS: usize = 100;
/// Max scheduled-event description length (characters).
pub const MAX_EVENT_DESCRIPTION_CHARS: usize = 1000;
/// Max scheduled-event location length (characters).
pub const MAX_EVENT_LOCATION_CHARS: usize = 100;
/// `entity_type` EXTERNAL: the only entity type this connector emits.
const ENTITY_TYPE_EXTERNAL: u8 = 3;
/// `privacy_level` GUILD_ONLY: the only privacy level this connector emits.
const PRIVACY_LEVEL_GUILD_ONLY: u8 = 2;
/// `status` CANCELED (cancelling preserves history; DELETE removes it).
const STATUS_CANCELED: u8 = 4;

/// `true` for an RFC 3339 timestamp `YYYY-MM-DDTHH:MM:SS[.f+](Z|+HH:MM|-HH:MM)`
/// with in-range fields. Strict on purpose: it lands in a JSON body Discord
/// parses, and nothing but digits/`-`/`:`/`.`/`T`/`Z`/`+` may pass.
fn is_rfc3339(s: &str) -> bool {
    let b = s.as_bytes();
    let digits =
        |r: std::ops::Range<usize>| b.get(r).is_some_and(|x| x.iter().all(u8::is_ascii_digit));
    let num = |r: std::ops::Range<usize>| s.get(r).and_then(|x| x.parse::<u32>().ok());
    if b.len() < 20
        || !(digits(0..4) && b[4] == b'-' && digits(5..7) && b[7] == b'-' && digits(8..10))
        || !(b[10] == b'T'
            && digits(11..13)
            && b[13] == b':'
            && digits(14..16)
            && b[16] == b':'
            && digits(17..19))
    {
        return false;
    }
    let in_range = num(5..7).is_some_and(|m| (1..=12).contains(&m))
        && num(8..10).is_some_and(|d| (1..=31).contains(&d))
        && num(11..13).is_some_and(|h| h <= 23)
        && num(14..16).is_some_and(|m| m <= 59)
        && num(17..19).is_some_and(|x| x <= 60);
    let mut i = 19;
    if b[i] == b'.' {
        i += 1;
        let start = i;
        while i < b.len() && b[i].is_ascii_digit() {
            i += 1;
        }
        if i == start || i - start > 9 {
            return false;
        }
    }
    let tz = &s[i..];
    let tz_ok = tz == "Z"
        || (tz.len() == 6
            && tz.is_ascii()
            && (tz.starts_with('+') || tz.starts_with('-'))
            && tz.as_bytes()[3] == b':'
            && tz[1..3].bytes().all(|c| c.is_ascii_digit())
            && tz[4..6].bytes().all(|c| c.is_ascii_digit())
            && tz[1..3].parse::<u32>().is_ok_and(|h| h <= 23)
            && tz[4..6].parse::<u32>().is_ok_and(|m| m <= 59));
    in_range && tz_ok
}

/// A bounded, guarded, non-empty (after trim) single-line text field.
fn event_text(
    map: &Map<String, Value>,
    key: &str,
    max: usize,
    required: bool,
) -> Result<Option<String>> {
    let Some(v) = map.get(key) else {
        return if required {
            Err(ConnectorError::Malformed(format!("missing string `{key}`")))
        } else {
            Ok(None)
        };
    };
    let s = v
        .as_str()
        .ok_or_else(|| ConnectorError::Malformed(format!("`{key}` must be a string")))?;
    // Descriptions may be multi-line; names and locations may not.
    guard_text(key, s, key == "description")?;
    if s.chars().count() > max {
        return Err(ConnectorError::Malformed(format!(
            "`{key}` exceeds {max} characters"
        )));
    }
    if required && s.trim().is_empty() {
        return Err(ConnectorError::Malformed(format!("`{key}` is empty")));
    }
    Ok(Some(s.to_string()))
}

fn event_time(map: &Map<String, Value>, key: &str) -> Result<String> {
    let s = str_field(map, key)?;
    if is_rfc3339(s) {
        Ok(s.to_string())
    } else {
        Err(ConnectorError::Malformed(format!(
            "`{key}` is not an RFC 3339 timestamp"
        )))
    }
}

/// The FULL scheduled-event body (never a partial diff): fixed `EXTERNAL` /
/// guild-only shape, `name`/times/`location` required, `description` optional.
fn event_body(map: &Map<String, Value>) -> Result<Vec<u8>> {
    let name = event_text(map, "name", MAX_EVENT_NAME_CHARS, true)?;
    let location = event_text(map, "location", MAX_EVENT_LOCATION_CHARS, true)?;
    let description = event_text(map, "description", MAX_EVENT_DESCRIPTION_CHARS, false)?;
    let mut body = json!({
        "name": name,
        "scheduled_start_time": event_time(map, "scheduled_start_time")?,
        "scheduled_end_time": event_time(map, "scheduled_end_time")?,
        "privacy_level": PRIVACY_LEVEL_GUILD_ONLY,
        "entity_type": ENTITY_TYPE_EXTERNAL,
        "entity_metadata": { "location": location },
    });
    if let (Some(d), Some(obj)) = (description.filter(|d| !d.is_empty()), body.as_object_mut()) {
        obj.insert("description".to_string(), Value::String(d));
    }
    serde_json::to_vec(&body).map_err(|e| ConnectorError::Backend(e.to_string()))
}

/// Fields accepted by `event.create` / `event.update`.
const EVENT_FIELDS: [&str; 5] = [
    "name",
    "description",
    "scheduled_start_time",
    "scheduled_end_time",
    "location",
];

/// Builds the guild scheduled-event request. The guild is ALWAYS the host's
/// `{target.guild_id}` placeholder.
fn build_event_request(kind: &str, p: &str) -> Result<HttpTpl> {
    let base = format!("/guilds/{TARGET_GUILD}/scheduled-events");
    match kind {
        "event.create" => {
            let m = object(p, &EVENT_FIELDS)?;
            Ok(tpl("POST", base, Some(event_body(&m)?)))
        }
        "event.update" => {
            let mut allowed = EVENT_FIELDS.to_vec();
            allowed.push("event_id");
            let m = object(p, &allowed)?;
            let id = snowflake(&m, "event_id")?;
            Ok(tpl("PATCH", format!("{base}/{id}"), Some(event_body(&m)?)))
        }
        "event.cancel" => {
            let m = object(p, &["event_id"])?;
            let id = snowflake(&m, "event_id")?;
            let body = serde_json::to_vec(&json!({ "status": STATUS_CANCELED }))
                .map_err(|e| ConnectorError::Backend(e.to_string()))?;
            Ok(tpl("PATCH", format!("{base}/{id}"), Some(body)))
        }
        _ => {
            let m = object(p, &["event_id"])?;
            let id = snowflake(&m, "event_id")?;
            Ok(tpl("DELETE", format!("{base}/{id}"), None))
        }
    }
}

fn tpl(method: &str, path: String, body: Option<Vec<u8>>) -> HttpTpl {
    let headers = if body.is_some() {
        vec![("Content-Type".to_string(), "application/json".to_string())]
    } else {
        Vec::new()
    };
    HttpTpl {
        method: method.to_string(),
        url: format!("{API_BASE}{path}"),
        headers,
        body,
        secret_refs: vec![("Authorization".to_string(), AUTH_HANDLE.to_string())],
    }
}

/// Builds the request template for `action`.
///
/// `dm.send` returns the MESSAGE request against `{dm.channel_id}`; the HOST opens
/// the DM channel for the `{user:<uuid>}` recipient itself and fills the
/// placeholder only from Discord's reply to that call. The host MUST also enforce
/// the `send_dm_in_community` gate and managed-role allowlist -- the connector
/// cannot (see README "Host duties").
///
/// # Errors
/// [`ConnectorError::Unsupported`] for an unknown kind; [`ConnectorError::Malformed`]
/// for an empty target, a non-object payload, a field outside the kind's
/// allow-list (notably any destination), a bad snowflake, or bad content.
pub fn build_request(action: &Action) -> Result<HttpTpl> {
    if action.target_source_id.is_empty() || action.target_source_id.len() > 128 {
        return Err(ConnectorError::Malformed("target-source-id invalid".into()));
    }
    guard_text("target-source-id", &action.target_source_id, false)?;
    let p = action.payload_json.as_str();
    match action.kind.as_str() {
        "chat.send" => {
            let m = object(p, &["content"])?;
            let body = message_body(content(&m)?)?;
            Ok(tpl(
                "POST",
                format!("/channels/{TARGET_CHANNEL}/messages"),
                Some(body),
            ))
        }
        "chat.delete" => {
            let m = object(p, &["message_id"])?;
            let id = snowflake(&m, "message_id")?;
            Ok(tpl(
                "DELETE",
                format!("/channels/{TARGET_CHANNEL}/messages/{id}"),
                None,
            ))
        }
        "dm.send" => {
            let m = object(p, &["recipient_id", "content"])?;
            let rid = user_ref(&m, "recipient_id")?;
            let body = message_body(content(&m)?)?;
            let mut t = tpl(
                "POST",
                format!("/channels/{DM_CHANNEL}/messages"),
                Some(body),
            );
            t.headers.push((HOST_DM_RECIPIENT_HEADER.to_string(), rid));
            Ok(t)
        }
        "role.add" | "role.remove" => {
            let m = object(p, &["user_id", "role"])?;
            let user = user_ref(&m, "user_id")?;
            let role = format!("{{target.role.{}}}", role_key(&m)?);
            let method = if action.kind == "role.add" {
                "PUT"
            } else {
                "DELETE"
            };
            Ok(tpl(
                method,
                format!("/guilds/{TARGET_GUILD}/members/{user}/roles/{role}"),
                None,
            ))
        }
        "event.create" | "event.update" | "event.cancel" | "event.delete" => {
            build_event_request(action.kind.as_str(), p)
        }
        other => Err(ConnectorError::Unsupported(format!(
            "action kind `{other}`"
        ))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn act(kind: &str, payload: &str) -> Action {
        Action {
            kind: kind.into(),
            target_source_id: "bind-1".into(),
            payload_json: payload.into(),
        }
    }

    #[test]
    fn chat_send_is_host_pinned_guarded_and_tokenless() {
        let t = build_request(&act("chat.send", r#"{"content":"hello @everyone"}"#)).unwrap();
        assert_eq!(t.method, "POST");
        assert_eq!(
            t.url,
            "https://discord.com/api/v10/channels/{target.channel_id}/messages"
        );
        assert_eq!(
            t.secret_refs,
            vec![("Authorization".to_string(), "discord-bot".to_string())]
        );
        let b: Value = serde_json::from_slice(t.body.as_ref().unwrap()).unwrap();
        assert_eq!(b["content"], "hello @everyone");
        assert_eq!(b["allowed_mentions"], json!({"parse":["users"]}));
        assert!(
            t.headers
                .iter()
                .all(|(k, _)| !k.eq_ignore_ascii_case("authorization"))
        );
    }

    const UUID_REF: &str = "{user:123e4567-e89b-12d3-a456-426614174000}";

    #[test]
    fn a_payload_can_never_name_the_destination_recipient_or_privilege() {
        let cases: Vec<(&str, String)> = vec![
            ("chat.send", r#"{"content":"x","channel_id":"123"}"#.into()),
            ("chat.send", r#"{"content":"x","guild_id":"1"}"#.into()),
            (
                "chat.delete",
                r#"{"message_id":"1","channel_id":"2"}"#.into(),
            ),
            (
                "role.add",
                format!(r#"{{"user_id":"{UUID_REF}","role":"vip","guild_id":"3"}}"#),
            ),
            // regression: raw role snowflake = privilege escalation
            (
                "role.add",
                format!(r#"{{"user_id":"{UUID_REF}","role_id":"2"}}"#),
            ),
            (
                "role.remove",
                format!(r#"{{"user_id":"{UUID_REF}","role_id":"2"}}"#),
            ),
            (
                "role.add",
                format!(r#"{{"user_id":"{UUID_REF}","role":"vip","role_id":"2"}}"#),
            ),
            // regression: dm destination steering
            (
                "dm.send",
                format!(r#"{{"recipient_id":"{UUID_REF}","content":"x","dm_channel_id":"9"}}"#),
            ),
            (
                "dm.send",
                format!(r#"{{"recipient_id":"{UUID_REF}","content":"x","channel_id":"9"}}"#),
            ),
        ];
        for (k, p) in cases {
            assert!(
                matches!(
                    build_request(&act(k, &p)),
                    Err(ConnectorError::Malformed(_))
                ),
                "{k} {p}"
            );
        }
        // dm.post no longer exists: the host runs the whole two-step from dm.send.
        assert!(matches!(
            build_request(&act("dm.post", r#"{"dm_channel_id":"88","content":"hi"}"#)),
            Err(ConnectorError::Unsupported(_))
        ));
    }

    #[test]
    fn delete_is_channel_scoped() {
        let t = build_request(&act("chat.delete", r#"{"message_id":"555"}"#)).unwrap();
        assert_eq!(t.method, "DELETE");
        assert_eq!(
            t.url,
            "https://discord.com/api/v10/channels/{target.channel_id}/messages/555"
        );
        assert!(t.body.is_none() && t.headers.is_empty());
        assert!(build_request(&act("chat.delete", r#"{"message_id":"../x"}"#)).is_err());
        assert!(build_request(&act("chat.delete", r#"{}"#)).is_err());
    }

    #[test]
    fn dm_send_carries_content_and_a_host_filled_channel_placeholder() {
        let t = build_request(&act(
            "dm.send",
            &format!(r#"{{"recipient_id":"{UUID_REF}","content":"hi"}}"#),
        ))
        .unwrap();
        assert_eq!(t.method, "POST");
        assert_eq!(
            t.url,
            "https://discord.com/api/v10/channels/{dm.channel_id}/messages"
        );
        let b: Value = serde_json::from_slice(t.body.as_ref().unwrap()).unwrap();
        assert_eq!(b["content"], "hi");
        assert_eq!(b["allowed_mentions"], json!({"parse":["users"]}));
        // The recipient travels in a host-only pseudo-header (stripped before sending).
        assert!(
            t.headers
                .contains(&(HOST_DM_RECIPIENT_HEADER.to_string(), UUID_REF.to_string()))
        );
        // Only the {user:<uuid>} placeholder: raw snowflakes and malformed refs are refused.
        for bad in [
            "444",
            "{user:zz}",
            "{user:123e4567-e89b-12d3-a456-42661417400}",
            "",
        ] {
            let p = format!(r#"{{"recipient_id":"{bad}","content":"hi"}}"#);
            assert!(build_request(&act("dm.send", &p)).is_err(), "{bad}");
        }
        assert!(
            build_request(&act(
                "dm.send",
                &format!(r#"{{"recipient_id":"{UUID_REF}"}}"#)
            ))
            .is_err()
        );
    }

    #[test]
    fn role_ops_use_a_managed_role_key_user_placeholder_and_the_pinned_guild() {
        let add = build_request(&act(
            "role.add",
            &format!(r#"{{"user_id":"{UUID_REF}","role":"verified"}}"#),
        ))
        .unwrap();
        assert_eq!(add.method, "PUT");
        assert_eq!(
            add.url,
            format!(
                "https://discord.com/api/v10/guilds/{{target.guild_id}}/members/{UUID_REF}/roles/{{target.role.verified}}"
            )
        );
        let rm = build_request(&act(
            "role.remove",
            &format!(r#"{{"user_id":"{UUID_REF}","role":"verified"}}"#),
        ))
        .unwrap();
        assert_eq!(rm.method, "DELETE");
        let long = "a".repeat(33);
        for bad_role in [
            "",
            "Verified",
            "1",
            "a b",
            "../x",
            "x}.{target.guild_id",
            &long,
        ] {
            let p = format!(r#"{{"user_id":"{UUID_REF}","role":"{bad_role}"}}"#);
            assert!(build_request(&act("role.add", &p)).is_err(), "{bad_role}");
        }
        // Raw snowflake user ids are refused too.
        assert!(build_request(&act("role.add", r#"{"user_id":"1","role":"vip"}"#)).is_err());
    }

    #[test]
    fn content_rules_and_input_validation_fail_loud() {
        assert!(build_request(&act("chat.send", r#"{"content":""}"#)).is_err());
        let big = format!(r#"{{"content":"{}"}}"#, "a".repeat(2001));
        assert!(build_request(&act("chat.send", &big)).is_err());
        let ok = format!(r#"{{"content":"{}"}}"#, "é".repeat(2000));
        assert!(build_request(&act("chat.send", &ok)).is_ok());
        assert!(build_request(&act("chat.send", "[]")).is_err());
        assert!(build_request(&act("chat.send", "nope")).is_err());
        assert!(build_request(&act("chat.send", r#"{}"#)).is_err());
        assert!(matches!(
            build_request(&act("moderation.ban", "{}")),
            Err(ConnectorError::Unsupported(_))
        ));
        let mut a = act("chat.send", r#"{"content":"x"}"#);
        a.target_source_id.clear();
        assert!(build_request(&a).is_err());
        a.target_source_id = "x".repeat(129);
        assert!(build_request(&a).is_err());
    }

    #[test]
    fn slot_smuggling_in_any_templated_string_is_rejected() {
        // regression (Twitch connector HIGH): control/zero-width/lookalike chars around braces.
        for evil in [
            "{\\u0001{SECRET}}",
            "{\\u200B{SECRET}}",
            "{\\uFF5B{SECRET}}",
            "{{SECRET}}",
            "{\\n{SECRET}\\n}",
        ] {
            let p = format!(r#"{{"content":"{evil}"}}"#);
            assert!(
                build_request(&act("chat.send", &p)).is_err(),
                "content {evil}"
            );
            let p = format!(r#"{{"recipient_id":"{UUID_REF}","content":"{evil}"}}"#);
            assert!(
                build_request(&act("dm.send", &p)).is_err(),
                "dm.send content {evil}"
            );
            let p = format!(r#"{{"recipient_id":"{evil}","content":"x"}}"#);
            assert!(
                build_request(&act("dm.send", &p)).is_err(),
                "recipient {evil}"
            );
            let p = format!(r#"{{"user_id":"{evil}","role":"vip"}}"#);
            assert!(build_request(&act("role.add", &p)).is_err(), "user {evil}");
            let p = format!(r#"{{"user_id":"{UUID_REF}","role":"{evil}"}}"#);
            assert!(build_request(&act("role.add", &p)).is_err(), "role {evil}");
            let mut a = act("chat.send", r#"{"content":"x"}"#);
            a.target_source_id = serde_json::from_str::<String>(&format!("\"{evil}\"")).unwrap();
            assert!(build_request(&a).is_err(), "target {evil}");
        }
        // Multi-line text with no delimiters still works.
        assert!(build_request(&act("chat.send", r#"{"content":"a\nb"}"#)).is_ok());
    }

    const EV: &str = r#""name":"Movie night","description":"Bring snacks","scheduled_start_time":"2026-12-01T19:00:00+00:00","scheduled_end_time":"2026-12-01T21:00:00Z","location":"Online""#;

    #[test]
    fn event_create_emits_the_exact_pinned_request_bytes() {
        let t = build_request(&act("event.create", &format!("{{{EV}}}"))).unwrap();
        assert_eq!(t.method, "POST");
        assert_eq!(
            t.url,
            "https://discord.com/api/v10/guilds/{target.guild_id}/scheduled-events"
        );
        assert_eq!(
            t.headers,
            vec![("Content-Type".to_string(), "application/json".to_string())]
        );
        assert_eq!(
            t.secret_refs,
            vec![("Authorization".to_string(), "discord-bot".to_string())]
        );
        // Exact wire bytes (serde_json emits object keys in sorted order).
        let expect = br#"{"description":"Bring snacks","entity_metadata":{"location":"Online"},"entity_type":3,"name":"Movie night","privacy_level":2,"scheduled_end_time":"2026-12-01T21:00:00Z","scheduled_start_time":"2026-12-01T19:00:00+00:00"}"#;
        assert_eq!(t.body.as_deref(), Some(expect.as_slice()));
    }

    #[test]
    fn event_update_cancel_delete_are_guild_pinned_and_id_scoped() {
        let upd = build_request(&act(
            "event.update",
            &format!(r#"{{"event_id":"1234567890",{EV}}}"#),
        ))
        .unwrap();
        assert_eq!(upd.method, "PATCH");
        assert_eq!(
            upd.url,
            "https://discord.com/api/v10/guilds/{target.guild_id}/scheduled-events/1234567890"
        );
        let can = build_request(&act("event.cancel", r#"{"event_id":"42"}"#)).unwrap();
        assert_eq!(can.method, "PATCH");
        assert_eq!(can.body.as_deref(), Some(br#"{"status":4}"#.as_slice()));
        let del = build_request(&act("event.delete", r#"{"event_id":"42"}"#)).unwrap();
        assert_eq!(del.method, "DELETE");
        assert_eq!(
            del.url,
            "https://discord.com/api/v10/guilds/{target.guild_id}/scheduled-events/42"
        );
        assert!(del.body.is_none() && del.headers.is_empty());
        for t in [&upd, &can, &del] {
            assert_eq!(
                t.secret_refs,
                vec![("Authorization".to_string(), "discord-bot".to_string())]
            );
        }
    }

    #[test]
    fn event_description_is_optional_and_empty_is_omitted() {
        let p = r#"{"name":"n","description":"","scheduled_start_time":"2026-12-01T19:00:00Z","scheduled_end_time":"2026-12-01T20:00:00.5Z","location":"x"}"#;
        let t = build_request(&act("event.create", p)).unwrap();
        let b: Value = serde_json::from_slice(&t.body.unwrap()).unwrap();
        assert!(b.get("description").is_none());
    }

    #[test]
    fn event_payload_can_never_name_the_guild_channel_or_privilege() {
        let ok_id = r#""event_id":"42""#;
        let cases: Vec<(&str, String)> = vec![
            // regression: guild pinning -- a payload guild/channel is REFUSED, never honoured
            ("event.create", format!(r#"{{{EV},"guild_id":"999"}}"#)),
            ("event.create", format!(r#"{{{EV},"channel_id":"999"}}"#)),
            ("event.create", format!(r#"{{{EV},"event_id":"42"}}"#)),
            (
                "event.update",
                format!(r#"{{{ok_id},{EV},"guild_id":"999"}}"#),
            ),
            (
                "event.update",
                format!(r#"{{{ok_id},{EV},"entity_type":2}}"#),
            ),
            ("event.update", format!(r#"{{{ok_id},{EV},"status":4}}"#)),
            ("event.create", format!(r#"{{{EV},"privacy_level":1}}"#)),
            ("event.cancel", r#"{"event_id":"42","guild_id":"9"}"#.into()),
            ("event.delete", r#"{"event_id":"42","guild_id":"9"}"#.into()),
            (
                "event.delete",
                r#"{"event_id":"42","channel_id":"9"}"#.into(),
            ),
        ];
        for (k, p) in cases {
            assert!(
                matches!(
                    build_request(&act(k, &p)),
                    Err(ConnectorError::Malformed(_))
                ),
                "{k} {p}"
            );
        }
    }

    #[test]
    fn event_ids_and_fields_reject_injection_and_bad_shapes() {
        for bad in [
            "",
            "abc",
            "../x",
            "1/2",
            "1?x=2",
            "12 3",
            "{target.guild_id}",
            "-1",
        ] {
            for k in ["event.cancel", "event.delete"] {
                let p = format!(r#"{{"event_id":"{bad}"}}"#);
                assert!(build_request(&act(k, &p)).is_err(), "{k} {bad}");
            }
            let p = format!(r#"{{"event_id":"{bad}",{EV}}}"#);
            assert!(
                build_request(&act("event.update", &p)).is_err(),
                "update {bad}"
            );
        }
        assert!(build_request(&act("event.cancel", "{}")).is_err());
        assert!(build_request(&act("event.update", &format!("{{{EV}}}"))).is_err());
        let long = |n: usize| "a".repeat(n);
        let mk = |name: &str, desc: &str, start: &str, loc: &str| {
            format!(
                r#"{{"name":"{name}","description":"{desc}","scheduled_start_time":"{start}","scheduled_end_time":"2026-12-01T21:00:00Z","location":"{loc}"}}"#
            )
        };
        let ok_t = "2026-12-01T19:00:00Z";
        assert!(build_request(&act("event.create", &mk("n", "d", ok_t, "l"))).is_ok());
        assert!(
            build_request(&act(
                "event.create",
                &mk(&long(100), &long(1000), ok_t, &long(100))
            ))
            .is_ok()
        );
        for p in [
            mk("", "d", ok_t, "l"),
            mk("  ", "d", ok_t, "l"),
            mk(&long(101), "d", ok_t, "l"),
            mk("n", &long(1001), ok_t, "l"),
            mk("n", "d", ok_t, ""),
            mk("n", "d", ok_t, &long(101)),
            mk("n", "d", "2026-12-01", "l"),
            mk("n", "d", "2026-13-01T19:00:00Z", "l"),
            mk("n", "d", "2026-12-01T25:00:00Z", "l"),
            mk("n", "d", "2026-12-01T19:00:00", "l"),
            mk("n", "d", "2026-12-01T19:00:00+0000", "l"),
            mk("n", "d", "2026-12-01T19:00:00+00:0\\u00e9", "l"),
            mk("n", "d", "2026-12-01T19:00:00.Z", "l"),
            mk("n", "d", "2026-12-01T19:00:00Z\\\"}", "l"),
            // slot smuggling in every templated text field
            mk("{{SECRET}}", "d", ok_t, "l"),
            mk("n", "{\\u0001{SECRET}}", ok_t, "l"),
            mk("n", "d", ok_t, "{\\u200B{SECRET}}"),
            mk("n\\nline", "d", ok_t, "l"),
            mk("n", "d", ok_t, "l\\nline"),
        ] {
            assert!(build_request(&act("event.create", &p)).is_err(), "{p}");
        }
        // description may be multi-line (guarded text allows newline/tab only there)
        assert!(build_request(&act("event.create", &mk("n", "a\\nb\\tc", ok_t, "l"))).is_ok());
        // non-string fields
        assert!(build_request(&act("event.create", r#"{"name":1}"#)).is_err());
        assert!(build_request(&act("event.create", "[]")).is_err());
        assert!(build_request(&act("event.create", "nope")).is_err());
    }

    #[test]
    fn rfc3339_accepts_only_the_documented_shapes() {
        for ok in [
            "2026-12-01T19:00:00Z",
            "2026-12-01T19:00:00+00:00",
            "2026-12-01T19:00:00-05:30",
            "2026-12-01T19:00:00.123456Z",
        ] {
            assert!(is_rfc3339(ok), "{ok}");
        }
        for bad in [
            "",
            "2026-12-01T19:00:00",
            "2026-12-01 19:00:00Z",
            "2026-00-01T19:00:00Z",
            "2026-12-32T19:00:00Z",
            "2026-12-01T24:00:00Z",
            "2026-12-01T19:60:00Z",
            "2026-12-01T19:00:61Z",
            "2026-12-01T19:00:00.Z",
            "2026-12-01T19:00:00.1234567890Z",
            "2026-12-01T19:00:00+24:00",
            "2026-12-01T19:00:00+00:60",
            "2026-12-01T19:00:00+0000",
            "2026-12-01T19:00:00ZZ",
            "2026-12-01T19:00:00+00:\u{e9}",
            "2026-12-01T19:00:00+é:00",
        ] {
            assert!(!is_rfc3339(bad), "{bad}");
        }
    }
}
