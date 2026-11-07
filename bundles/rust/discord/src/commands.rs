//! Discord application-command registration (guild bulk overwrite) and the pure
//! reconcile planner that feeds it.
//!
//! The hub-api desired-state endpoint (`GET .../app-commands/desired`) returns the
//! union of the `webhook`-type commands of every community paired to a guild plus
//! the built-in `context` command, with an idempotency `version`. This module
//! turns that response into either "nothing to do" (`changed: false`, empty list)
//! or ONE `commands.register.guild` payload, and builds the request template.
//!
//! Security: the application id and the guild are host-filled placeholders
//! (`{app.application_id}` / `{target.guild_id}`); the payload may not name
//! either. Only `name` / `description` cross to Discord (the registered payload
//! the version hashes), plus the fixed free-text `args` option, so a bundle
//! cannot declare permissions, nested options, or DM scope through this path.

use serde_json::{Value, json};

use crate::error::{ConnectorError, Result};
use crate::sanitize::guard_text;
use crate::sender::{API_BASE, AUTH_HANDLE, HttpTpl};

/// The kinds this module owns.
pub const KINDS: &[&str] = &["commands.register", "commands.register.guild"];

/// Discord's hard cap on guild application commands.
pub const MAX_COMMANDS: usize = 100;
/// Discord's command-name length cap.
pub const MAX_NAME_CHARS: usize = 32;
/// Discord's command-description length cap.
pub const MAX_DESCRIPTION_CHARS: usize = 100;
/// Host-substituted placeholder: the application (client) id of the bot connection.
pub const APP_ID: &str = "{app.application_id}";
/// Host-substituted placeholder: the bound guild.
pub const TARGET_GUILD: &str = "{target.guild_id}";
/// The fixed free-text option every chat-input command carries (`/verb args:<text>`).
pub const ARGS_OPTION: &str = "args";

/// One reconcile decision.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Plan {
    /// The desired version matches what is registered: send nothing.
    Unchanged {
        /// The version that is already registered.
        version: String,
    },
    /// Bulk-overwrite the guild's commands with `payload_json`.
    Overwrite {
        /// Idempotency version to remember after a successful overwrite.
        version: String,
        /// `{"commands":[...]}`: the `commands.register.guild` payload.
        payload_json: String,
    },
}

/// Valid Discord chat-input command name: `[a-z0-9_-]{1,32}` (lowercase ASCII only;
/// the registry never emits anything else and a wider class would let a lookalike in).
#[must_use]
pub fn is_command_name(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= MAX_NAME_CHARS
        && s.bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_' || b == b'-')
}

fn command_object(name: &str, description: &str) -> Value {
    json!({
        "name": name,
        "type": 1,
        "description": description,
        "options": [{
            "type": 3,
            "name": ARGS_OPTION,
            "description": "Arguments",
            "required": false,
        }],
    })
}

fn validate(name: &str, description: &str) -> Result<()> {
    if !is_command_name(name) {
        return Err(ConnectorError::Malformed(
            "command name must be [a-z0-9_-]{1,32}".into(),
        ));
    }
    let chars = description.chars().count();
    if chars == 0 || chars > MAX_DESCRIPTION_CHARS {
        return Err(ConnectorError::Malformed(
            "command description must be 1-100 characters".into(),
        ));
    }
    guard_text("command description", description, false)
}

/// Plans one reconcile from the desired-state response body.
///
/// `registered_version` is the version this guild last accepted (`None` = never).
/// An unchanged version yields [`Plan::Unchanged`] so the periodic reconcile costs
/// no Discord write (bulk overwrite is rate limited per guild).
///
/// # Errors
/// [`ConnectorError::Malformed`] for a response that is not the documented shape,
/// a bad name/description, a duplicate name, or more than [`MAX_COMMANDS`].
pub fn plan(desired_json: &[u8], registered_version: Option<&str>) -> Result<Plan> {
    let v: Value = serde_json::from_slice(desired_json)
        .map_err(|_| ConnectorError::Malformed("desired state is not JSON".into()))?;
    let version = v
        .get("version")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty() && s.len() <= 128 && s.is_ascii())
        .ok_or_else(|| ConnectorError::Malformed("desired state missing version".into()))?
        .to_string();
    let changed = v
        .get("changed")
        .and_then(Value::as_bool)
        .ok_or_else(|| ConnectorError::Malformed("desired state missing changed".into()))?;
    if !changed {
        return Ok(Plan::Unchanged { version });
    }
    if registered_version == Some(version.as_str()) {
        return Ok(Plan::Unchanged { version });
    }
    let list = v
        .get("commands")
        .and_then(Value::as_array)
        .ok_or_else(|| ConnectorError::Malformed("desired state missing commands".into()))?;
    if list.len() > MAX_COMMANDS {
        return Err(ConnectorError::Malformed(format!(
            "more than {MAX_COMMANDS} commands"
        )));
    }
    let mut seen = std::collections::BTreeSet::new();
    let mut out = Vec::with_capacity(list.len());
    for c in list {
        let name = c
            .get("name")
            .and_then(Value::as_str)
            .ok_or_else(|| ConnectorError::Malformed("command missing name".into()))?;
        let description = c
            .get("description")
            .and_then(Value::as_str)
            .ok_or_else(|| ConnectorError::Malformed("command missing description".into()))?;
        validate(name, description)?;
        if !seen.insert(name.to_string()) {
            return Err(ConnectorError::Malformed("duplicate command name".into()));
        }
        out.push(command_object(name, description));
    }
    let payload_json = serde_json::to_string(&json!({ "commands": out }))
        .map_err(|e| ConnectorError::Backend(e.to_string()))?;
    Ok(Plan::Overwrite {
        version,
        payload_json,
    })
}

/// Builds the request template for `kind`.
///
/// `commands.register.guild` takes `{"commands":[{name,description}...]}` (the
/// array form [`plan`] emits, or bare name/description pairs) and returns the
/// guild bulk-overwrite PUT. The global `commands.register` kind is declared by
/// the host table but deliberately NOT built here: registration is per guild
/// (instant, no cross-guild leakage), so it fails loud instead of silently
/// widening scope.
///
/// # Errors
/// [`ConnectorError::Unsupported`] for any other kind; [`ConnectorError::Malformed`]
/// for a payload that names anything beyond the `commands` array or an invalid command.
pub fn build(kind: &str, payload_json: &str) -> Result<HttpTpl> {
    match kind {
        "commands.register.guild" => {
            let v: Value = serde_json::from_str(payload_json)
                .map_err(|_| ConnectorError::Malformed("payload is not JSON".into()))?;
            let Value::Object(map) = v else {
                return Err(ConnectorError::Malformed("payload is not an object".into()));
            };
            if let Some(k) = map.keys().find(|k| k.as_str() != "commands") {
                return Err(ConnectorError::Malformed(format!(
                    "payload field `{k}` is not accepted for this action"
                )));
            }
            let list = map
                .get("commands")
                .and_then(Value::as_array)
                .ok_or_else(|| ConnectorError::Malformed("missing `commands` array".into()))?;
            if list.len() > MAX_COMMANDS {
                return Err(ConnectorError::Malformed(format!(
                    "more than {MAX_COMMANDS} commands"
                )));
            }
            let mut seen = std::collections::BTreeSet::new();
            let mut out = Vec::with_capacity(list.len());
            for c in list {
                let name = c.get("name").and_then(Value::as_str).unwrap_or_default();
                let description = c
                    .get("description")
                    .and_then(Value::as_str)
                    .unwrap_or_default();
                validate(name, description)?;
                if !seen.insert(name.to_string()) {
                    return Err(ConnectorError::Malformed("duplicate command name".into()));
                }
                // Re-derived from name+description only: nothing else survives.
                out.push(command_object(name, description));
            }
            let body = serde_json::to_vec(&Value::Array(out))
                .map_err(|e| ConnectorError::Backend(e.to_string()))?;
            Ok(HttpTpl {
                method: "PUT".to_string(),
                url: format!("{API_BASE}/applications/{APP_ID}/guilds/{TARGET_GUILD}/commands"),
                headers: vec![("Content-Type".to_string(), "application/json".to_string())],
                body: Some(body),
                secret_refs: vec![("Authorization".to_string(), AUTH_HANDLE.to_string())],
            })
        }
        "commands.register" => Err(ConnectorError::Unsupported(
            "discord global command registration is not offered; register per guild".into(),
        )),
        other => Err(ConnectorError::Unsupported(format!(
            "discord {other} is not a command-registration kind"
        ))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn desired(changed: bool, version: &str, cmds: &Value) -> Vec<u8> {
        serde_json::to_vec(&json!({
            "success": true, "version": version, "changed": changed,
            "commands": cmds, "truncated": false,
        }))
        .unwrap()
    }

    #[test]
    fn unchanged_response_sends_nothing() {
        let p = plan(&desired(false, "v1", &json!([])), Some("v1")).unwrap();
        assert_eq!(
            p,
            Plan::Unchanged {
                version: "v1".into()
            }
        );
    }

    #[test]
    fn matching_registered_version_sends_nothing_even_if_changed_flag_set() {
        let d = desired(true, "v1", &json!([{"name":"ping","description":"Ping"}]));
        assert_eq!(
            plan(&d, Some("v1")).unwrap(),
            Plan::Unchanged {
                version: "v1".into()
            }
        );
    }

    #[test]
    fn changed_version_yields_one_overwrite_with_only_name_and_description() {
        let d = desired(
            true,
            "v2",
            &json!([
                {"name":"context","description":"Switch community","builtin":true,"appPublicIds":["x"]},
                {"name":"ping","description":"Ping","default_member_permissions":"8"}
            ]),
        );
        let Plan::Overwrite {
            version,
            payload_json,
        } = plan(&d, Some("v1")).unwrap()
        else {
            panic!("expected overwrite");
        };
        assert_eq!(version, "v2");
        let v: Value = serde_json::from_str(&payload_json).unwrap();
        let arr = v["commands"].as_array().unwrap();
        assert_eq!(arr.len(), 2);
        assert!(arr[1].get("default_member_permissions").is_none());
        assert!(arr[0].get("appPublicIds").is_none());
        assert_eq!(arr[1]["options"][0]["name"], ARGS_OPTION);
        // Round-trips through build.
        let t = build("commands.register.guild", &payload_json).unwrap();
        assert_eq!(t.method, "PUT");
        assert_eq!(
            t.url,
            "https://discord.com/api/v10/applications/{app.application_id}/guilds/{target.guild_id}/commands"
        );
        assert_eq!(
            t.secret_refs,
            vec![("Authorization".into(), "discord-bot".into())]
        );
        let body: Value = serde_json::from_slice(&t.body.unwrap()).unwrap();
        assert!(body.is_array());
    }

    #[test]
    fn bad_shapes_fail_loud() {
        let bad = [
            desired(true, "v", &json!([{"name":"Ping","description":"x"}])),
            desired(true, "v", &json!([{"name":"","description":"x"}])),
            desired(true, "v", &json!([{"name":"a b","description":"x"}])),
            desired(true, "v", &json!([{"name":"ping","description":""}])),
            desired(true, "v", &json!([{"name":"ping"}])),
            desired(
                true,
                "v",
                &json!([{"name":"a","description":"x"},{"name":"a","description":"y"}]),
            ),
            desired(
                true,
                "v",
                &json!([{"name":"ping","description":"{{SLOT}}"}]),
            ),
            desired(true, "", &json!([])),
            b"not json".to_vec(),
            serde_json::to_vec(&json!({"version":"v"})).unwrap(),
        ];
        for b in bad {
            assert!(
                matches!(plan(&b, None), Err(ConnectorError::Malformed(_))),
                "{}",
                String::from_utf8_lossy(&b)
            );
        }
    }

    #[test]
    fn more_than_the_discord_cap_is_refused_not_truncated() {
        let many: Vec<Value> = (0..=MAX_COMMANDS)
            .map(|i| json!({"name": format!("c{i}"), "description": "d"}))
            .collect();
        assert!(plan(&desired(true, "v", &json!(many)), None).is_err());
        let ok: Vec<Value> = (0..MAX_COMMANDS)
            .map(|i| json!({"name": format!("c{i}"), "description": "d"}))
            .collect();
        assert!(plan(&desired(true, "v", &json!(ok)), None).is_ok());
    }

    #[test]
    fn build_refuses_destination_fields_and_global_registration() {
        let named = r#"{"commands":[],"guild_id":"1"}"#;
        assert!(matches!(
            build("commands.register.guild", named),
            Err(ConnectorError::Malformed(_))
        ));
        let app = r#"{"commands":[],"application_id":"1"}"#;
        assert!(build("commands.register.guild", app).is_err());
        assert!(matches!(
            build("commands.register", r#"{"commands":[]}"#),
            Err(ConnectorError::Unsupported(_))
        ));
        assert!(matches!(
            build("other", "{}"),
            Err(ConnectorError::Unsupported(_))
        ));
        assert!(build("commands.register.guild", "[]").is_err());
        assert!(build("commands.register.guild", r#"{"x":1}"#).is_err());
    }

    #[test]
    fn empty_command_list_is_a_valid_clear() {
        let t = build("commands.register.guild", r#"{"commands":[]}"#).unwrap();
        assert_eq!(t.body.unwrap(), b"[]");
    }
}
