//! Discord interaction replies (initial callback and followups).
//!
//! The interaction TOKEN never reaches this crate: the template carries the
//! host-filled `{interaction.id}` / `{interaction.application_id}` /
//! `{interaction.token}` slots, resolved from the host-held token store. The
//! only thing a producer names is an opaque handle (the interaction id), which
//! the host uses to look the record up; the host also proves the record
//! belongs to the sending community before any slot is filled.
//!
//! Payload (from the host's generic policy-op bag): `{"content": "<json>",
//! "message_id": "<handle>"}` where `content` is itself a JSON object:
//!
//! * `interaction.callback` -> `{"type": 4|5|6|7, "content"?, "ephemeral"?}`
//! * `interaction.followup` -> `{"content", "ephemeral"?}`
//!
//! Every message carries `allowed_mentions: {"parse":["users"]}` like all
//! other Discord sends; `ephemeral` maps to flag 64.

use serde_json::{Map, Value, json};

use crate::error::{ConnectorError, Result};
use crate::sanitize::guard_text;
use crate::sender::{API_BASE, HttpTpl, MAX_CONTENT_CHARS};
use crate::session::is_snowflake;

/// The kinds this module owns.
pub const KINDS: &[&str] = &["interaction.callback", "interaction.followup"];

/// Host-filled slot: the interaction id.
pub const INTERACTION_ID: &str = "{interaction.id}";
/// Host-filled slot: the application id the interaction was addressed to.
pub const INTERACTION_APP: &str = "{interaction.application_id}";
/// Host-filled slot: the 15-minute interaction token (never in guest memory).
pub const INTERACTION_TOKEN: &str = "{interaction.token}";
/// Message flag: only the invoker sees the message.
pub const FLAG_EPHEMERAL: u64 = 64;

fn reject_unknown(map: &Map<String, Value>, allowed: &[&str]) -> Result<()> {
    if let Some(k) = map.keys().find(|k| !allowed.contains(&k.as_str())) {
        return Err(ConnectorError::Malformed(format!(
            "payload field `{k}` is not accepted for this action"
        )));
    }
    Ok(())
}

/// Parses the outer bag and the embedded JSON object. The handle must be a
/// snowflake (the interaction id); the host re-validates and owns the lookup.
fn parse(payload_json: &str) -> Result<Map<String, Value>> {
    let outer: Value = serde_json::from_str(payload_json)
        .map_err(|_| ConnectorError::Malformed("payload is not JSON".into()))?;
    let Value::Object(outer) = outer else {
        return Err(ConnectorError::Malformed("payload is not an object".into()));
    };
    reject_unknown(&outer, &["content", "message_id"])?;
    let handle = outer
        .get("message_id")
        .and_then(Value::as_str)
        .ok_or_else(|| ConnectorError::Malformed("missing interaction handle".into()))?;
    if !is_snowflake(handle) {
        return Err(ConnectorError::Malformed(
            "interaction handle is not a snowflake".into(),
        ));
    }
    let inner = outer
        .get("content")
        .and_then(Value::as_str)
        .ok_or_else(|| ConnectorError::Malformed("missing interaction reply".into()))?;
    let Value::Object(inner) = serde_json::from_str(inner)
        .map_err(|_| ConnectorError::Malformed("interaction reply is not JSON".into()))?
    else {
        return Err(ConnectorError::Malformed(
            "interaction reply is not an object".into(),
        ));
    };
    Ok(inner)
}

fn message_data(inner: &Map<String, Value>, require_content: bool) -> Result<Map<String, Value>> {
    let mut data = Map::new();
    match inner.get("content") {
        Some(Value::String(c)) => {
            guard_text("content", c, true)?;
            if c.is_empty() || c.chars().count() > MAX_CONTENT_CHARS {
                return Err(ConnectorError::Malformed(format!(
                    "content must be 1-{MAX_CONTENT_CHARS} characters"
                )));
            }
            data.insert("content".into(), Value::String(c.clone()));
        }
        Some(_) => return Err(ConnectorError::Malformed("content is not a string".into())),
        None if require_content => {
            return Err(ConnectorError::Malformed("missing content".into()));
        }
        None => {}
    }
    data.insert("allowed_mentions".into(), json!({ "parse": ["users"] }));
    match inner.get("ephemeral") {
        None => {}
        Some(Value::Bool(true)) => {
            data.insert("flags".into(), Value::from(FLAG_EPHEMERAL));
        }
        Some(Value::Bool(false)) => {}
        Some(_) => return Err(ConnectorError::Malformed("ephemeral is not a bool".into())),
    }
    Ok(data)
}

fn template(url: String, body: &Value) -> Result<HttpTpl> {
    Ok(HttpTpl {
        method: "POST".to_string(),
        url,
        headers: vec![("Content-Type".to_string(), "application/json".to_string())],
        body: Some(serde_json::to_vec(body).map_err(|e| ConnectorError::Backend(e.to_string()))?),
        // The URL carries the credential; no Authorization header (the host refuses one).
        secret_refs: Vec::new(),
    })
}

/// Builds the request template for `kind`.
///
/// # Errors
/// [`ConnectorError::Unsupported`] for another kind; [`ConnectorError::Malformed`]
/// for an unknown field, a bad handle/type/content, or an unsupported callback type.
pub fn build(kind: &str, payload_json: &str) -> Result<HttpTpl> {
    match kind {
        "interaction.callback" => {
            let inner = parse(payload_json)?;
            reject_unknown(&inner, &["type", "content", "ephemeral"])?;
            let t = inner
                .get("type")
                .and_then(Value::as_u64)
                .ok_or_else(|| ConnectorError::Malformed("missing callback type".into()))?;
            let body = match t {
                // CHANNEL_MESSAGE_WITH_SOURCE / UPDATE_MESSAGE carry a message.
                4 | 7 => json!({ "type": t, "data": message_data(&inner, true)? }),
                // DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE: only the ephemeral flag may ride.
                5 => {
                    let mut data = Map::new();
                    if inner.get("ephemeral") == Some(&Value::Bool(true)) {
                        data.insert("flags".into(), Value::from(FLAG_EPHEMERAL));
                    }
                    if inner.contains_key("content") {
                        return Err(ConnectorError::Malformed(
                            "a deferred ack carries no content".into(),
                        ));
                    }
                    if data.is_empty() {
                        json!({ "type": 5 })
                    } else {
                        json!({ "type": 5, "data": data })
                    }
                }
                // DEFERRED_UPDATE_MESSAGE: no data.
                6 => {
                    if inner.contains_key("content") || inner.contains_key("ephemeral") {
                        return Err(ConnectorError::Malformed(
                            "a deferred update carries no content".into(),
                        ));
                    }
                    json!({ "type": 6 })
                }
                other => {
                    return Err(ConnectorError::Unsupported(format!(
                        "interaction callback type {other}"
                    )));
                }
            };
            template(
                format!("{API_BASE}/interactions/{INTERACTION_ID}/{INTERACTION_TOKEN}/callback"),
                &body,
            )
        }
        "interaction.followup" => {
            let inner = parse(payload_json)?;
            reject_unknown(&inner, &["content", "ephemeral"])?;
            let data = message_data(&inner, true)?;
            template(
                format!("{API_BASE}/webhooks/{INTERACTION_APP}/{INTERACTION_TOKEN}"),
                &Value::Object(data),
            )
        }
        other => Err(ConnectorError::Unsupported(format!(
            "discord {other} is not an interaction kind"
        ))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bag(inner: &Value) -> String {
        json!({"content": inner.to_string(), "message_id": "1234567890123456"}).to_string()
    }

    fn body(t: &HttpTpl) -> Value {
        serde_json::from_slice(t.body.as_ref().unwrap()).unwrap()
    }

    #[test]
    fn callback_message_defer_and_update_shapes() {
        let t = build(
            "interaction.callback",
            &bag(&json!({"type":4,"content":"hi","ephemeral":true})),
        )
        .unwrap();
        assert_eq!(
            t.url,
            "https://discord.com/api/v10/interactions/{interaction.id}/{interaction.token}/callback"
        );
        assert!(t.secret_refs.is_empty());
        let b = body(&t);
        assert_eq!(b["type"], 4);
        assert_eq!(b["data"]["flags"], 64);
        assert_eq!(b["data"]["allowed_mentions"]["parse"][0], "users");
        assert_eq!(
            body(&build("interaction.callback", &bag(&json!({"type":5}))).unwrap()),
            json!({"type":5})
        );
        assert_eq!(
            body(
                &build(
                    "interaction.callback",
                    &bag(&json!({"type":5,"ephemeral":true}))
                )
                .unwrap()
            )["data"]["flags"],
            64
        );
        assert_eq!(
            body(&build("interaction.callback", &bag(&json!({"type":6}))).unwrap()),
            json!({"type":6})
        );
        assert_eq!(
            body(
                &build(
                    "interaction.callback",
                    &bag(&json!({"type":7,"content":"x"}))
                )
                .unwrap()
            )["type"],
            7
        );
    }

    #[test]
    fn followup_targets_the_host_filled_application_and_token() {
        let t = build("interaction.followup", &bag(&json!({"content":"later"}))).unwrap();
        assert_eq!(
            t.url,
            "https://discord.com/api/v10/webhooks/{interaction.application_id}/{interaction.token}"
        );
        assert_eq!(body(&t)["content"], "later");
        assert!(body(&t).get("flags").is_none());
    }

    #[test]
    fn token_and_destination_fields_are_refused() {
        for extra in [
            "token",
            "interaction_token",
            "application_id",
            "channel_id",
            "guild_id",
        ] {
            let mut v = json!({"content":"x"});
            v[extra] = json!("y");
            assert!(build("interaction.followup", &bag(&v)).is_err(), "{extra}");
        }
        let outer =
            json!({"content":"{\"content\":\"x\"}","message_id":"1234567890123456","token":"t"});
        assert!(build("interaction.followup", &outer.to_string()).is_err());
    }

    #[test]
    fn bad_inputs_fail_loud_never_default() {
        let bad_handle = json!({"content":"{\"content\":\"x\"}","message_id":"abc"}).to_string();
        assert!(build("interaction.followup", &bad_handle).is_err());
        assert!(build("interaction.followup", "not json").is_err());
        assert!(build("interaction.followup", &bag(&json!({"content":""}))).is_err());
        assert!(build("interaction.followup", &bag(&json!({"content":"{{SLOT}}"}))).is_err());
        assert!(
            build(
                "interaction.followup",
                &bag(&json!({"content":"x".repeat(2001)}))
            )
            .is_err()
        );
        assert!(
            build(
                "interaction.followup",
                &bag(&json!({"content":"x","ephemeral":"yes"}))
            )
            .is_err()
        );
        assert!(build("interaction.callback", &bag(&json!({"type":9}))).is_err());
        assert!(matches!(
            build("interaction.callback", &bag(&json!({"type":8}))),
            Err(ConnectorError::Unsupported(_))
        ));
        assert!(
            build(
                "interaction.callback",
                &bag(&json!({"type":5,"content":"x"}))
            )
            .is_err()
        );
        assert!(
            build(
                "interaction.callback",
                &bag(&json!({"type":6,"ephemeral":true}))
            )
            .is_err()
        );
        assert!(build("interaction.callback", &bag(&json!({"content":"x"}))).is_err());
        assert!(matches!(
            build("other", "{}"),
            Err(ConnectorError::Unsupported(_))
        ));
    }
}
