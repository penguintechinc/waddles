//! Twitch moderation request templates: `mod.ban`, `mod.timeout`, `mod.unban`.
//!
//! Endpoints (Helix, exactly as `core/svc_ingest/.../sender_exec/policy/twitch.rs` declares):
//! `POST /moderation/bans` (a `duration` in the body makes it a timeout; the two stay separate
//! kinds so each has its own audit identity) and `DELETE /moderation/bans?user_id=`.
//! `broadcaster_id` / `moderator_id` are the host slots from the community binding, NEVER read
//! from the payload. The host hands the connector only `user_id` (numeric Twitch id),
//! `message_id` and `text`; for these kinds `text` is a JSON object text carrying
//! `{duration?, reason?}`, i.e. the parameters travel in the one field the producer envelope has
//! for them. No handle reaches the template.
//! Message deletion lives in `sender.rs` (`chat.delete`); whispers (`dm.send`) stay host-duty gated.

use serde_json::{Value, json};

use crate::common::FrameErr;
use crate::sender::{
    HELIX, SLOT_BROADCASTER, SLOT_SENDER, Tpl, auth_headers, base_refs, id_arg, json_headers,
    reject_unsafe,
};

/// The kinds this module owns.
pub const KINDS: &[&str] = &["mod.ban", "mod.timeout", "mod.unban"];

/// Helix timeout lower bound in seconds.
pub const TIMEOUT_MIN_SECS: u64 = 1;
/// Helix timeout upper bound in seconds (14 days).
pub const TIMEOUT_MAX_SECS: u64 = 1_209_600;
/// Helix ban/timeout reason limit (characters).
pub const MAX_REASON_CHARS: usize = 500;

/// Parses the `text` parameter object (`{duration?, reason?}`); absent = no parameters.
/// Unknown keys are refused so a caller can never smuggle an extra Helix body field.
fn params(payload: &Value) -> Result<Value, FrameErr> {
    let Some(raw) = payload.get("text") else {
        return Ok(json!({}));
    };
    raw.as_str()
        .and_then(|t| serde_json::from_str::<Value>(t).ok())
        .filter(|v| {
            v.as_object()
                .is_some_and(|m| m.keys().all(|k| k == "duration" || k == "reason"))
        })
        .ok_or_else(|| {
            FrameErr::Malformed(
                "`text` must be a JSON object with only `duration` / `reason`".to_string(),
            )
        })
}

/// Optional `reason`, rejected (never stripped) when unsafe or oversize.
fn reason_arg(p: &Value) -> Result<String, FrameErr> {
    let Some(v) = p.get("reason") else {
        return Ok(String::new());
    };
    let r = v
        .as_str()
        .ok_or_else(|| FrameErr::Malformed("`reason` must be a string".to_string()))?;
    reject_unsafe("reason", r)?;
    if r.chars().count() > MAX_REASON_CHARS {
        return Err(FrameErr::Malformed(
            "reason exceeds 500 characters".to_string(),
        ));
    }
    Ok(r.to_string())
}

/// Validated timeout duration (integer seconds within Helix bounds).
fn duration_arg(p: &Value) -> Result<u64, FrameErr> {
    p.get("duration")
        .and_then(Value::as_u64)
        .filter(|d| (TIMEOUT_MIN_SECS..=TIMEOUT_MAX_SECS).contains(d))
        .ok_or_else(|| {
            FrameErr::Malformed(format!(
                "`duration` must be an integer {TIMEOUT_MIN_SECS}..={TIMEOUT_MAX_SECS} seconds"
            ))
        })
}

/// Builds the Helix template for `kind`. Unknown kinds fail loudly as `Unsupported`.
pub fn build(kind: &str, payload: &Value) -> Result<Tpl, FrameErr> {
    let ban_url = format!(
        "{HELIX}/moderation/bans?broadcaster_id={SLOT_BROADCASTER}&moderator_id={SLOT_SENDER}"
    );
    match kind {
        "mod.ban" | "mod.timeout" => {
            let p = params(payload)?;
            let mut data = json!({
                "user_id": id_arg(payload, "user_id", true)?,
                "reason": reason_arg(&p)?,
            });
            if kind == "mod.timeout" {
                data["duration"] = json!(duration_arg(&p)?);
            } else if p.get("duration").is_some() {
                return Err(FrameErr::Malformed(
                    "`duration` is not valid for a permanent ban; use mod.timeout".to_string(),
                ));
            }
            Ok(Tpl {
                method: "POST".to_string(),
                url: ban_url,
                headers: json_headers(),
                body: Some(json!({ "data": data }).to_string().into_bytes()),
                secret_refs: base_refs(),
            })
        }
        "mod.unban" => {
            if payload.get("text").is_some() {
                return Err(FrameErr::Malformed(
                    "mod.unban takes no parameters".to_string(),
                ));
            }
            Ok(Tpl {
                method: "DELETE".to_string(),
                url: format!("{ban_url}&user_id={}", id_arg(payload, "user_id", true)?),
                headers: auth_headers(),
                body: None,
                secret_refs: base_refs(),
            })
        }
        other => Err(FrameErr::Unsupported(format!(
            "twitch {other} is not a moderation kind"
        ))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sender::build_request;

    /// Producer-shaped payload: `user_id` plus `text` = JSON-object text of the params.
    fn pl(user_id: &str, params: Value) -> String {
        let mut p = json!({ "user_id": user_id });
        if params.as_object().is_some_and(|m| !m.is_empty()) {
            p["text"] = json!(params.to_string());
        }
        p.to_string()
    }

    fn body(t: &Tpl) -> Value {
        serde_json::from_slice(t.body.as_ref().unwrap()).unwrap()
    }

    #[test]
    fn ban_is_a_permanent_ban_with_host_slots_only() {
        let t = build_request("mod.ban", "s", &pl("4242", json!({"reason":"spam"}))).unwrap();
        assert_eq!(t.method, "POST");
        assert_eq!(
            t.url,
            "https://api.twitch.tv/helix/moderation/bans?broadcaster_id={{TWITCH_BROADCASTER_ID}}&moderator_id={{TWITCH_SENDER_ID}}"
        );
        assert_eq!(
            body(&t),
            json!({"data":{"user_id":"4242","reason":"spam"}}),
            "no duration = permanent"
        );
        let bare = build_request("mod.ban", "s", &pl("4242", json!({}))).unwrap();
        assert_eq!(body(&bare)["data"]["reason"], "");
    }

    #[test]
    fn timeout_carries_duration_and_bounds_are_enforced() {
        let ok = |d: Value| pl("1", json!({ "duration": d }));
        let t = build_request("mod.timeout", "s", &ok(json!(600))).unwrap();
        assert_eq!(body(&t)["data"]["duration"], 600);
        for good in [1u64, 1_209_600] {
            assert!(build_request("mod.timeout", "s", &ok(json!(good))).is_ok());
        }
        for bad in [
            json!(0),
            json!(1_209_601),
            json!(-5),
            json!("60"),
            json!(1.5),
            Value::Null,
        ] {
            assert!(
                build_request("mod.timeout", "s", &ok(bad.clone())).is_err(),
                "{bad}"
            );
        }
        assert!(
            build_request("mod.timeout", "s", &pl("1", json!({}))).is_err(),
            "duration required"
        );
        assert!(
            build_request("mod.ban", "s", &ok(json!(60))).is_err(),
            "ban must not carry a duration"
        );
    }

    #[test]
    fn unban_is_a_bodyless_delete_with_user_id_query() {
        let t = build_request("mod.unban", "s", &pl("77", json!({}))).unwrap();
        assert_eq!(t.method, "DELETE");
        assert!(t.body.is_none());
        assert!(
            t.url
                .ends_with("&moderator_id={{TWITCH_SENDER_ID}}&user_id=77")
        );
        assert!(build_request("mod.unban", "s", &pl("77", json!({"duration":5}))).is_err());
    }

    #[test]
    fn bundle_supplied_destination_ids_never_reach_the_template() {
        for kind in KINDS {
            let mut base: Value = serde_json::from_str(&pl("9", json!({}))).unwrap();
            if *kind == "mod.timeout" {
                base["text"] = json!(json!({"duration":60}).to_string());
            }
            let plain = build_request(kind, "s", &base.to_string()).unwrap();
            for key in crate::sender::IGNORED_TARGET_KEYS {
                let mut p = base.clone();
                p[*key] = json!("666");
                let t = build_request(kind, "s", &p.to_string()).unwrap();
                assert_eq!(t, plain, "{kind}/{key} influenced the template");
                assert!(!t.url.contains("666"));
            }
        }
    }

    #[test]
    fn hostile_inputs_are_rejected_not_sanitised() {
        for kind in KINDS {
            for bad in [
                "12x",
                "",
                "1&moderator_id=5",
                "{{TWITCH_TOKEN}}",
                "1\u{200B}2",
            ] {
                assert!(
                    build_request(kind, "s", &pl(bad, json!({"duration":60}))).is_err(),
                    "{kind}/{bad:?}"
                );
            }
        }
        for reason in ["x{{TWITCH_TOKEN}}", "a\nb", "a\u{202E}b", &"y".repeat(501)] {
            let p = pl("1", json!({ "reason": reason }));
            assert!(build_request("mod.ban", "s", &p).is_err(), "{reason:?}");
        }
        assert!(build_request("mod.ban", "s", &pl("1", json!({"reason":5}))).is_err());
        let max = pl("1", json!({"reason":"y".repeat(500)}));
        assert!(build_request("mod.ban", "s", &max).is_ok());
        // `text` that is not an object with only duration/reason is refused (no body smuggling).
        for text in [
            "nope",
            "[]",
            r#"{"user_id":"2"}"#,
            r#"{"duration":5,"x":1}"#,
        ] {
            let p = json!({"user_id":"1","text":text}).to_string();
            assert!(build_request("mod.timeout", "s", &p).is_err(), "{text}");
        }
    }

    #[test]
    fn unknown_kind_fails_loud_and_non_moderation_kinds_are_not_claimed() {
        assert!(matches!(
            build("other", &json!({})),
            Err(FrameErr::Unsupported(_))
        ));
        assert!(matches!(
            build_request("mod.kick", "s", "{}"),
            Err(FrameErr::Unsupported(_))
        ));
        assert!(matches!(
            build_request("mod.ban", "", "{}"),
            Err(FrameErr::Malformed(_))
        ));
    }
}
