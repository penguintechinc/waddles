//! `sender.build-request`: Discord REST request TEMPLATES (part B).
//!
//! The overlay never holds a token and never picks a destination:
//!
//! * the credential is a `secret-refs` entry (`Authorization` -> opaque handle);
//!   the host substitutes the real `Bot ...` value at send time;
//! * the destination channel / guild is a `{target.channel_id}` /
//!   `{target.guild_id}` placeholder the HOST fills from the binding resolved
//!   from `target-source-id` (`app_outbound_bindings`). Payloads that try to
//!   name a destination are REFUSED (unknown fields are rejected), so an app
//!   bundle cannot steer a send to a channel it does not own;
//! * delete is channel-scoped (`/channels/{target.channel_id}/messages/{id}`),
//!   never resolved from a message id alone.
//!
//! Every message body carries `allowed_mentions: {"parse":["users"]}` (the
//! `discord_mentions` backstop of the hardcoded REST client).

use serde_json::{Map, Value, json};

use crate::error::{OverlayError, Result};
use crate::session::is_snowflake;

/// Discord REST base the host pins egress to.
pub const API_BASE: &str = "https://discord.com/api/v10";
/// Host-substituted placeholder: the bound channel.
pub const TARGET_CHANNEL: &str = "{target.channel_id}";
/// Host-substituted placeholder: the bound guild.
pub const TARGET_GUILD: &str = "{target.guild_id}";
/// Opaque secret handle for the bot credential.
pub const AUTH_HANDLE: &str = "discord-bot";
/// Discord's hard message-content limit (characters).
pub const MAX_CONTENT_CHARS: usize = 2000;

/// Mirror of WIT `sender.action`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Action {
    /// `chat.send`, `chat.delete`, `dm.send`, `dm.post`, `role.add`, `role.remove`.
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
        .map_err(|_| OverlayError::Malformed("payload is not JSON".into()))?;
    let Value::Object(map) = v else {
        return Err(OverlayError::Malformed("payload is not an object".into()));
    };
    if let Some(k) = map.keys().find(|k| !allowed.contains(&k.as_str())) {
        // Includes any attempt to supply channel_id / guild_id.
        return Err(OverlayError::Malformed(format!(
            "payload field `{k}` is not accepted for this action"
        )));
    }
    Ok(map)
}

fn str_field<'a>(map: &'a Map<String, Value>, key: &str) -> Result<&'a str> {
    map.get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| OverlayError::Malformed(format!("missing string `{key}`")))
}

fn snowflake(map: &Map<String, Value>, key: &str) -> Result<String> {
    let s = str_field(map, key)?;
    if is_snowflake(s) {
        Ok(s.to_string())
    } else {
        Err(OverlayError::Malformed(format!(
            "`{key}` is not a snowflake"
        )))
    }
}

/// A user reference: a snowflake, or the `{user:<uuid>}` placeholder the host
/// renders (the overlay holds no `connector.pii.read`, so it cannot resolve it).
fn user_ref(map: &Map<String, Value>, key: &str) -> Result<String> {
    let s = str_field(map, key)?;
    let placeholder = s
        .strip_prefix("{user:")
        .and_then(|r| r.strip_suffix('}'))
        .is_some_and(|u| u.len() == 36 && u.bytes().all(|b| b.is_ascii_hexdigit() || b == b'-'));
    if is_snowflake(s) || placeholder {
        Ok(s.to_string())
    } else {
        Err(OverlayError::Malformed(format!(
            "`{key}` must be a snowflake or a {{user:<uuid>}} placeholder"
        )))
    }
}

fn content(map: &Map<String, Value>) -> Result<&str> {
    let c = str_field(map, "content")?;
    if c.is_empty() {
        return Err(OverlayError::Malformed("content is empty".into()));
    }
    if c.chars().count() > MAX_CONTENT_CHARS {
        return Err(OverlayError::Malformed(
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
    .map_err(|e| OverlayError::Backend(e.to_string()))
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
/// `dm.send` is step one of Discord's two-call DM flow (open the DM channel);
/// the host feeds the returned channel id into `dm.post`. The host MUST also
/// enforce the community-membership gate of `send_dm_in_community` -- the
/// overlay cannot (see README "Host duties").
///
/// # Errors
/// [`OverlayError::Unsupported`] for an unknown kind; [`OverlayError::Malformed`]
/// for an empty target, a non-object payload, a field outside the kind's
/// allow-list (notably any destination), a bad snowflake, or bad content.
pub fn build_request(action: &Action) -> Result<HttpTpl> {
    if action.target_source_id.is_empty() || action.target_source_id.len() > 128 {
        return Err(OverlayError::Malformed("target-source-id invalid".into()));
    }
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
            let m = object(p, &["recipient_id"])?;
            let rid = user_ref(&m, "recipient_id")?;
            let body = serde_json::to_vec(&json!({ "recipient_id": rid }))
                .map_err(|e| OverlayError::Backend(e.to_string()))?;
            Ok(tpl("POST", "/users/@me/channels".to_string(), Some(body)))
        }
        "dm.post" => {
            let m = object(p, &["dm_channel_id", "content"])?;
            let ch = snowflake(&m, "dm_channel_id")?;
            let body = message_body(content(&m)?)?;
            Ok(tpl("POST", format!("/channels/{ch}/messages"), Some(body)))
        }
        "role.add" | "role.remove" => {
            let m = object(p, &["user_id", "role_id"])?;
            let user = user_ref(&m, "user_id")?;
            let role = snowflake(&m, "role_id")?;
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
        other => Err(OverlayError::Unsupported(format!("action kind `{other}`"))),
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

    #[test]
    fn a_payload_can_never_name_the_destination() {
        for (k, p) in [
            ("chat.send", r#"{"content":"x","channel_id":"123"}"#),
            ("chat.send", r#"{"content":"x","guild_id":"1"}"#),
            ("chat.delete", r#"{"message_id":"1","channel_id":"2"}"#),
            (
                "role.add",
                r#"{"user_id":"1","role_id":"2","guild_id":"3"}"#,
            ),
            (
                "dm.post",
                r#"{"dm_channel_id":"1","content":"x","channel_id":"2"}"#,
            ),
        ] {
            assert!(
                matches!(build_request(&act(k, p)), Err(OverlayError::Malformed(_))),
                "{k} {p}"
            );
        }
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
    fn dm_is_two_steps() {
        let open = build_request(&act("dm.send", r#"{"recipient_id":"444"}"#)).unwrap();
        assert_eq!(open.url, "https://discord.com/api/v10/users/@me/channels");
        assert_eq!(
            serde_json::from_slice::<Value>(open.body.as_ref().unwrap()).unwrap(),
            json!({"recipient_id":"444"})
        );
        let uuid = "{user:123e4567-e89b-12d3-a456-426614174000}";
        assert!(build_request(&act("dm.send", &format!(r#"{{"recipient_id":"{uuid}"}}"#))).is_ok());
        assert!(build_request(&act("dm.send", r#"{"recipient_id":"{user:zz}"}"#)).is_err());
        let post =
            build_request(&act("dm.post", r#"{"dm_channel_id":"88","content":"hi"}"#)).unwrap();
        assert_eq!(post.url, "https://discord.com/api/v10/channels/88/messages");
        assert_eq!(
            serde_json::from_slice::<Value>(post.body.as_ref().unwrap()).unwrap()["allowed_mentions"],
            json!({"parse":["users"]})
        );
    }

    #[test]
    fn role_ops_use_put_and_delete_on_the_pinned_guild() {
        let add = build_request(&act("role.add", r#"{"user_id":"1","role_id":"2"}"#)).unwrap();
        assert_eq!(
            (add.method.as_str(), add.url.as_str()),
            (
                "PUT",
                "https://discord.com/api/v10/guilds/{target.guild_id}/members/1/roles/2"
            )
        );
        let rm = build_request(&act("role.remove", r#"{"user_id":"1","role_id":"2"}"#)).unwrap();
        assert_eq!(rm.method, "DELETE");
        assert!(build_request(&act("role.add", r#"{"user_id":"1","role_id":"x"}"#)).is_err());
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
            Err(OverlayError::Unsupported(_))
        ));
        let mut a = act("chat.send", r#"{"content":"x"}"#);
        a.target_source_id.clear();
        assert!(build_request(&a).is_err());
        a.target_source_id = "x".repeat(129);
        assert!(build_request(&a).is_err());
    }
}
