//! Discord moderation request templates: `mod.kick`, `mod.ban`, `mod.unban`, `mod.timeout`.
//!
//! Shapes the REST request only; every guarantee that needs the platform lives HOST-side in the
//! Discord duty hook (`core/svc_ingest/.../sender_exec/discord_duties.rs`): the guild is the
//! community's binding (`{target.guild_id}`), the target is a tokenized `{user:<uuid>}` the host
//! detokenizes inside its own boundary (`{target.user}`), and the role-hierarchy / owner / self
//! checks run before anything is sent. A payload can therefore never name a guild, a raw user id,
//! a channel or a role: unknown fields are refused. Policy table: `policy/discord.rs`.
//!
//! Timeouts: Discord caps `communication_disabled_until` at 28 days from now. This guest has no
//! trusted clock, so the HOST turns the producer's duration into the absolute `until` timestamp
//! (and enforces the 28-day cap) before calling the connector; the connector only validates the
//! timestamp's shape. `clear: true` lifts a timeout (`communication_disabled_until: null`).

use serde_json::{Value, json};

use crate::error::{ConnectorError, Result};
use crate::sender::{HttpTpl, TARGET_GUILD, is_rfc3339, object, tpl, user_ref};

/// The kinds this module owns.
pub const KINDS: &[&str] = &["mod.kick", "mod.ban", "mod.unban", "mod.timeout"];

/// Discord's cap on `delete_message_seconds` (7 days).
pub const MAX_DELETE_MESSAGE_SECONDS: u64 = 604_800;
/// Discord's cap on a timeout (28 days), enforced by the host against its own clock.
pub const MAX_TIMEOUT_SECONDS: u64 = 28 * 24 * 60 * 60;

/// Parses the optional integer field `key` within `0..=max` (default 0 when absent).
fn bounded_u64(map: &serde_json::Map<String, Value>, key: &str, max: u64) -> Result<u64> {
    match map.get(key) {
        None => Ok(0),
        Some(v) => v.as_u64().filter(|n| *n <= max).ok_or_else(|| {
            ConnectorError::Malformed(format!("`{key}` must be an integer 0..={max}"))
        }),
    }
}

fn json_body(v: &Value) -> Result<Vec<u8>> {
    serde_json::to_vec(v).map_err(|e| ConnectorError::Backend(e.to_string()))
}

/// Builds the request template for the moderation `kind`.
///
/// # Errors
/// [`ConnectorError::Unsupported`] for a kind outside [`KINDS`]; [`ConnectorError::Malformed`] for
/// a non-object payload, a field outside the kind's allow-list (notably any guild / channel / raw
/// user id), a user that is not a `{user:<uuid>}` placeholder, or an out-of-range value.
pub fn build(kind: &str, payload: &str) -> Result<HttpTpl> {
    let members = format!("/guilds/{TARGET_GUILD}/members");
    let bans = format!("/guilds/{TARGET_GUILD}/bans");
    match kind {
        "mod.kick" => {
            let m = object(payload, &["user_id"])?;
            let user = user_ref(&m, "user_id")?;
            Ok(tpl("DELETE", format!("{members}/{user}"), None))
        }
        "mod.unban" => {
            let m = object(payload, &["user_id"])?;
            let user = user_ref(&m, "user_id")?;
            Ok(tpl("DELETE", format!("{bans}/{user}"), None))
        }
        "mod.ban" => {
            let m = object(payload, &["user_id", "delete_message_seconds"])?;
            let user = user_ref(&m, "user_id")?;
            let secs = bounded_u64(&m, "delete_message_seconds", MAX_DELETE_MESSAGE_SECONDS)?;
            let body = json_body(&json!({ "delete_message_seconds": secs }))?;
            Ok(tpl("PUT", format!("{bans}/{user}"), Some(body)))
        }
        "mod.timeout" => {
            let m = object(payload, &["user_id", "until", "clear"])?;
            let user = user_ref(&m, "user_id")?;
            let until = match (m.get("until"), m.get("clear")) {
                (Some(Value::String(u)), None) if is_rfc3339(u) => Value::String(u.clone()),
                (None, Some(Value::Bool(true))) => Value::Null,
                _ => {
                    return Err(ConnectorError::Malformed(
                        "timeout needs exactly one of an RFC 3339 `until` or `clear: true`".into(),
                    ));
                }
            };
            let body = json_body(&json!({ "communication_disabled_until": until }))?;
            Ok(tpl("PATCH", format!("{members}/{user}"), Some(body)))
        }
        other => Err(ConnectorError::Unsupported(format!(
            "discord {other} is not a moderation kind"
        ))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const U: &str = "{user:123e4567-e89b-12d3-a456-426614174000}";

    fn p(extra: &str) -> String {
        format!(r#"{{"user_id":"{U}"{extra}}}"#)
    }

    #[test]
    fn every_kind_builds_a_host_pinned_template() {
        let g = "https://discord.com/api/v10/guilds/{target.guild_id}";
        let t = build("mod.kick", &p("")).unwrap();
        assert_eq!(t.method, "DELETE");
        assert_eq!(t.url, format!("{g}/members/{U}"));
        assert!(t.body.is_none() && t.headers.is_empty());
        let t = build("mod.unban", &p("")).unwrap();
        assert_eq!(
            (t.method.as_str(), t.url),
            ("DELETE", format!("{g}/bans/{U}"))
        );
        let t = build("mod.ban", &p(r#","delete_message_seconds":3600"#)).unwrap();
        assert_eq!((t.method.as_str(), t.url), ("PUT", format!("{g}/bans/{U}")));
        let b: Value = serde_json::from_slice(&t.body.unwrap()).unwrap();
        assert_eq!(b, json!({"delete_message_seconds": 3600}));
        let t = build("mod.timeout", &p(r#","until":"2026-12-01T00:00:00Z""#)).unwrap();
        assert_eq!(
            (t.method.as_str(), t.url),
            ("PATCH", format!("{g}/members/{U}"))
        );
        let b: Value = serde_json::from_slice(&t.body.unwrap()).unwrap();
        assert_eq!(
            b,
            json!({"communication_disabled_until": "2026-12-01T00:00:00Z"})
        );
        let t = build("mod.timeout", &p(r#","clear":true"#)).unwrap();
        let b: Value = serde_json::from_slice(&t.body.unwrap()).unwrap();
        assert_eq!(b, json!({"communication_disabled_until": null}));
        let auth = vec![("Authorization".to_string(), "discord-bot".to_string())];
        for t in [
            build("mod.kick", &p("")).unwrap(),
            build("mod.ban", &p("")).unwrap(),
            build("mod.unban", &p("")).unwrap(),
        ] {
            assert_eq!(t.secret_refs, auth);
        }
    }

    #[test]
    fn ban_defaults_to_no_message_deletion_and_enforces_the_cap() {
        let b: Value =
            serde_json::from_slice(&build("mod.ban", &p("")).unwrap().body.unwrap()).unwrap();
        assert_eq!(b["delete_message_seconds"], 0);
        assert!(build("mod.ban", &p(r#","delete_message_seconds":604800"#)).is_ok());
        for bad in [
            r#","delete_message_seconds":604801"#,
            r#","delete_message_seconds":-1"#,
            r#","delete_message_seconds":"9""#,
            r#","delete_message_seconds":1.5"#,
        ] {
            assert!(
                matches!(build("mod.ban", &p(bad)), Err(ConnectorError::Malformed(_))),
                "{bad}"
            );
        }
    }

    #[test]
    fn a_payload_can_never_name_the_guild_channel_role_or_a_raw_user() {
        for (k, extra) in [
            ("mod.kick", r#","guild_id":"1""#),
            ("mod.kick", r#","channel_id":"1""#),
            ("mod.ban", r#","roles":["1"]"#),
            ("mod.unban", r#","guild_id":"1""#),
            (
                "mod.timeout",
                r#","until":"2026-12-01T00:00:00Z","roles":["1"]"#,
            ),
            (
                "mod.timeout",
                r#","until":"2026-12-01T00:00:00Z","nick":"x""#,
            ),
        ] {
            assert!(
                matches!(build(k, &p(extra)), Err(ConnectorError::Malformed(_))),
                "{k} {extra}"
            );
        }
        // a raw snowflake / malformed reference as the target is refused (PII rule)
        for bad in [
            "700000000000000001",
            "{user:zz}",
            "",
            "{user:123e4567-e89b-12d3-a456-42661417400}",
            "{{SECRET}}",
        ] {
            let payload = format!(r#"{{"user_id":"{bad}"}}"#);
            for k in ["mod.kick", "mod.unban"] {
                assert!(build(k, &payload).is_err(), "{k} {bad}");
            }
        }
        assert!(build("mod.kick", "{}").is_err());
        assert!(build("mod.kick", "[]").is_err());
        assert!(build("mod.kick", "nope").is_err());
    }

    #[test]
    fn timeout_requires_exactly_one_of_until_or_clear() {
        for bad in [
            "",
            r#","until":"tomorrow""#,
            r#","until":"2026-12-01T00:00:00Z","clear":true"#,
            r#","clear":false"#,
            r#","until":null"#,
            r#","until":5"#,
        ] {
            assert!(
                matches!(
                    build("mod.timeout", &p(bad)),
                    Err(ConnectorError::Malformed(_))
                ),
                "{bad}"
            );
        }
    }

    #[test]
    fn unknown_kinds_are_unsupported_never_ok() {
        for k in ["other", "moderation.ban", "mod.nuke", ""] {
            assert!(
                matches!(build(k, &p("")), Err(ConnectorError::Unsupported(_))),
                "{k}"
            );
        }
    }
}
