//! `tool-provider` surface: pure gateway-lifecycle classification.
//!
//! The connector contract gives the guest no way to ask the host to reconnect,
//! to persist a session, or to track heartbeat ACKs, and a component holds no
//! state between calls. These read-only tools expose the connector's gateway
//! knowledge (the same `classify` / `classify_close_code` the receiver uses) so
//! the host's WS transport can drive RESUME/IDENTIFY decisions from the
//! connector instead of re-implementing Discord opcodes -- which is the point of
//! the connector model. They hold no credential and touch no network.

use serde_json::{Value, json};

use crate::gateway::{CloseCodeClass, Lifecycle, classify, classify_close_code, parse_frame};

/// Why a tool call failed (mapped to WIT `tool-error` by the glue).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ToolFail {
    /// No tool by that name.
    Unknown,
    /// The arguments do not match the tool's schema.
    InvalidArgs(String),
}

fn class_name(c: CloseCodeClass) -> &'static str {
    match c {
        CloseCodeClass::Resumable => "resumable",
        CloseCodeClass::ReconnectFresh => "reconnect_fresh",
        CloseCodeClass::Fatal => "fatal",
    }
}

fn lifecycle_json(l: &Lifecycle) -> Value {
    match l {
        Lifecycle::Hello {
            heartbeat_interval_ms,
        } => {
            json!({"action":"hello","heartbeat_interval_ms":heartbeat_interval_ms})
        }
        Lifecycle::HeartbeatRequest => json!({"action":"heartbeat_request"}),
        Lifecycle::HeartbeatAck => json!({"action":"heartbeat_ack"}),
        Lifecycle::Reconnect => json!({"action":"reconnect","resumable":true}),
        Lifecycle::InvalidSession { resumable } => {
            json!({"action":"invalid_session","resumable":resumable})
        }
        Lifecycle::Ready(s) => json!({"action":"ready","session":s}),
        Lifecycle::Resumed => json!({"action":"resumed"}),
        Lifecycle::Dispatch => json!({"action":"dispatch"}),
    }
}

/// A syntactically valid timestamp used ONLY to validate a timeout tool's other arguments through
/// the connector builder; the real `until` is computed by the host from `duration_seconds`.
const PROBE_UNTIL: &str = "2000-01-01T00:00:00Z";

/// Rejects any key of `a` outside `allowed` (arguments are never silently dropped).
fn only_keys(a: &Value, allowed: &[&str]) -> Result<(), ToolFail> {
    let m = a
        .as_object()
        .ok_or_else(|| ToolFail::InvalidArgs("args must be an object".into()))?;
    match m.keys().find(|k| !allowed.contains(&k.as_str())) {
        Some(k) => Err(ToolFail::InvalidArgs(format!(
            "argument `{k}` is not accepted"
        ))),
        None => Ok(()),
    }
}

/// A required string argument.
fn arg_str<'a>(a: &'a Value, key: &str) -> Result<&'a str, ToolFail> {
    a.get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| ToolFail::InvalidArgs(format!("missing string `{key}`")))
}

/// Maps a `discord.moderation.*` / `discord.message.*` tool call to `(registered op, connector
/// payload)`. `user` is the uuid of the tokenized `{user:<uuid>}` reference.
fn write_tool_payload(name: &str, a: &Value) -> Result<(&'static str, Value), ToolFail> {
    let user =
        |a: &Value| -> Result<String, ToolFail> { Ok(format!("{{user:{}}}", arg_str(a, "user")?)) };
    Ok(match name {
        "discord.moderation.kick" => {
            only_keys(a, &["user"])?;
            ("mod.kick", json!({"user_id": user(a)?}))
        }
        "discord.moderation.unban" => {
            only_keys(a, &["user"])?;
            ("mod.unban", json!({"user_id": user(a)?}))
        }
        "discord.moderation.ban" => {
            only_keys(a, &["user", "delete_message_seconds"])?;
            let mut p = json!({"user_id": user(a)?});
            if let Some(v) = a.get("delete_message_seconds") {
                p["delete_message_seconds"] = v.clone();
            }
            ("mod.ban", p)
        }
        "discord.moderation.timeout" => {
            only_keys(a, &["user", "duration_seconds"])?;
            a.get("duration_seconds")
                .and_then(Value::as_u64)
                .filter(|s| (1..=crate::moderation::MAX_TIMEOUT_SECONDS).contains(s))
                .ok_or_else(|| {
                    ToolFail::InvalidArgs(format!(
                        "`duration_seconds` must be an integer 1..={}",
                        crate::moderation::MAX_TIMEOUT_SECONDS
                    ))
                })?;
            (
                "mod.timeout",
                json!({"user_id": user(a)?, "until": PROBE_UNTIL}),
            )
        }
        "discord.moderation.untimeout" => {
            only_keys(a, &["user"])?;
            ("mod.timeout", json!({"user_id": user(a)?, "clear": true}))
        }
        "discord.message.edit" => {
            only_keys(a, &["message_id", "content"])?;
            (
                "message.edit",
                json!({"message_id": arg_str(a, "message_id")?, "content": arg_str(a, "content")?}),
            )
        }
        "discord.message.react" => {
            only_keys(a, &["message_id", "emoji"])?;
            (
                "reaction.add",
                json!({"message_id": arg_str(a, "message_id")?, "content": arg_str(a, "emoji")?}),
            )
        }
        "discord.message.delete" => {
            only_keys(a, &["message_id"])?;
            (
                "chat.delete",
                json!({"message_id": arg_str(a, "message_id")?}),
            )
        }
        _ => return Err(ToolFail::Unknown),
    })
}

/// Dispatches one tool call.
///
/// * `classify_frame` `{frame: <gateway envelope object>}` -> `{action, ...}`
/// * `classify_close_code` `{code: <u16>}` -> `{class}`
///
/// # Errors
/// [`ToolFail::Unknown`] for an unknown name, [`ToolFail::InvalidArgs`] for
/// malformed arguments or a frame that is not a valid gateway envelope.
pub fn call(name: &str, args: &[u8]) -> Result<Vec<u8>, ToolFail> {
    let a: Value = serde_json::from_slice(args)
        .map_err(|_| ToolFail::InvalidArgs("args are not JSON".into()))?;
    let out = match name {
        "classify_frame" => {
            let frame = a
                .get("frame")
                .ok_or_else(|| ToolFail::InvalidArgs("missing `frame`".into()))?;
            let bytes = serde_json::to_vec(frame)
                .map_err(|_| ToolFail::InvalidArgs("frame not serializable".into()))?;
            let f = parse_frame(&bytes).map_err(|e| ToolFail::InvalidArgs(e.to_string()))?;
            let l = classify(&f).map_err(|e| ToolFail::InvalidArgs(e.to_string()))?;
            lifecycle_json(&l)
        }
        "classify_close_code" => {
            let code = a
                .get("code")
                .and_then(Value::as_u64)
                .ok_or_else(|| ToolFail::InvalidArgs("`code` must be a u16".into()))
                .and_then(|c| {
                    u16::try_from(c)
                        .map_err(|_| ToolFail::InvalidArgs("`code` must be a u16".into()))
                })?;
            json!({"class": class_name(classify_close_code(code))})
        }
        // Host-mediated write tools: the HOST executes the pinned scheduled-event
        // sender template (guild + credential from the community's binding). The
        // guest only validates the arguments through the SAME template builder the
        // sender uses, so a tool can never accept what the sender would refuse
        // (notably any `guild_id` / `channel_id`).
        other if other.starts_with("discord.scheduled_event.") => {
            let kind = match &other["discord.scheduled_event.".len()..] {
                "create" => "event.create",
                "update" => "event.update",
                "cancel" => "event.cancel",
                "delete" => "event.delete",
                _ => return Err(ToolFail::Unknown),
            };
            let payload = serde_json::to_string(&a)
                .map_err(|_| ToolFail::InvalidArgs("args not serializable".into()))?;
            crate::sender::build_request(&crate::sender::Action {
                kind: kind.to_string(),
                target_source_id: "tool".to_string(),
                payload_json: payload,
            })
            .map_err(|e| ToolFail::InvalidArgs(e.to_string()))?;
            json!({"validated": true, "op": kind})
        }
        // Moderation + message-op tools: same pattern. The args are mapped to the connector
        // payload of the registered op and validated through the SAME builder the sender uses;
        // the HOST executes (guild/credential from the binding, role hierarchy, audit).
        other
            if other.starts_with("discord.moderation.")
                || other.starts_with("discord.message.") =>
        {
            let (kind, payload) = write_tool_payload(other, &a)?;
            crate::sender::build_request(&crate::sender::Action {
                kind: kind.to_string(),
                target_source_id: "tool".to_string(),
                payload_json: payload.to_string(),
            })
            .map_err(|e| ToolFail::InvalidArgs(e.to_string()))?;
            json!({"validated": true, "op": kind})
        }
        _ => return Err(ToolFail::Unknown),
    };
    serde_json::to_vec(&out).map_err(|e| ToolFail::InvalidArgs(e.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn run(name: &str, args: Value) -> Result<Value, ToolFail> {
        call(name, &serde_json::to_vec(&args).unwrap()).map(|b| serde_json::from_slice(&b).unwrap())
    }

    #[test]
    fn classifies_frames() {
        assert_eq!(
            run("classify_frame", json!({"frame":{"op":7}})).unwrap(),
            json!({"action":"reconnect","resumable":true})
        );
        assert_eq!(
            run("classify_frame", json!({"frame":{"op":9,"d":false}})).unwrap(),
            json!({"action":"invalid_session","resumable":false})
        );
        assert_eq!(
            run(
                "classify_frame",
                json!({"frame":{"op":10,"d":{"heartbeat_interval":1000}}})
            )
            .unwrap()["heartbeat_interval_ms"],
            1000
        );
        assert_eq!(
            run("classify_frame", json!({"frame":{"op":11}})).unwrap()["action"],
            "heartbeat_ack"
        );
        assert_eq!(
            run("classify_frame", json!({"frame":{"op":1}})).unwrap()["action"],
            "heartbeat_request"
        );
        let ready = run(
            "classify_frame",
            json!({"frame":{"op":0,"s":1,"t":"READY","d":{"session_id":"s","user":{"id":"9"}}}}),
        )
        .unwrap();
        assert_eq!(ready["session"]["session_id"], "s");
        assert_eq!(
            run("classify_frame", json!({"frame":{"op":0,"t":"RESUMED"}})).unwrap()["action"],
            "resumed"
        );
        assert_eq!(
            run("classify_frame", json!({"frame":{"op":0,"t":"X"}})).unwrap()["action"],
            "dispatch"
        );
    }

    #[test]
    fn classifies_close_codes() {
        assert_eq!(
            run("classify_close_code", json!({"code":4004})).unwrap()["class"],
            "fatal"
        );
        assert_eq!(
            run("classify_close_code", json!({"code":4000})).unwrap()["class"],
            "resumable"
        );
        assert_eq!(
            run("classify_close_code", json!({"code":4009})).unwrap()["class"],
            "reconnect_fresh"
        );
    }

    #[test]
    fn bad_calls_fail_loud() {
        assert_eq!(run("nope", json!({})), Err(ToolFail::Unknown));
        assert!(matches!(
            run("classify_frame", json!({})),
            Err(ToolFail::InvalidArgs(_))
        ));
        assert!(matches!(
            run("classify_frame", json!({"frame":{"op":99}})),
            Err(ToolFail::InvalidArgs(_))
        ));
        assert!(matches!(
            run("classify_frame", json!({"frame":"x"})),
            Err(ToolFail::InvalidArgs(_))
        ));
        assert!(matches!(
            run("classify_close_code", json!({"code":70000})),
            Err(ToolFail::InvalidArgs(_))
        ));
        assert!(matches!(
            call("classify_frame", b"nope"),
            Err(ToolFail::InvalidArgs(_))
        ));
    }

    #[test]
    fn scheduled_event_tools_validate_through_the_sender_templates() {
        let ev = json!({"name":"n","scheduled_start_time":"2026-12-01T19:00:00Z","scheduled_end_time":"2026-12-01T20:00:00Z","location":"l"});
        assert_eq!(
            run("discord.scheduled_event.create", ev.clone()).unwrap(),
            json!({"validated":true,"op":"event.create"})
        );
        let mut upd = ev.clone();
        upd["event_id"] = json!("42");
        assert!(run("discord.scheduled_event.update", upd).is_ok());
        assert!(run("discord.scheduled_event.cancel", json!({"event_id":"42"})).is_ok());
        assert!(run("discord.scheduled_event.delete", json!({"event_id":"42"})).is_ok());
        // a guild / channel can never be supplied, nor a malformed id
        let mut g = ev.clone();
        g["guild_id"] = json!("9");
        assert!(matches!(
            run("discord.scheduled_event.create", g),
            Err(ToolFail::InvalidArgs(_))
        ));
        assert!(
            run(
                "discord.scheduled_event.delete",
                json!({"event_id":"42","guild_id":"9"})
            )
            .is_err()
        );
        assert!(run("discord.scheduled_event.delete", json!({"event_id":"../x"})).is_err());
        assert!(matches!(
            run("discord.scheduled_event.explode", json!({})),
            Err(ToolFail::Unknown)
        ));
    }

    const U: &str = "123e4567-e89b-12d3-a456-426614174000";

    #[test]
    fn moderation_and_message_tools_validate_through_the_sender_templates() {
        for (tool, args, op) in [
            ("discord.moderation.kick", json!({"user":U}), "mod.kick"),
            ("discord.moderation.unban", json!({"user":U}), "mod.unban"),
            ("discord.moderation.ban", json!({"user":U}), "mod.ban"),
            (
                "discord.moderation.ban",
                json!({"user":U,"delete_message_seconds":604800}),
                "mod.ban",
            ),
            (
                "discord.moderation.timeout",
                json!({"user":U,"duration_seconds":2419200}),
                "mod.timeout",
            ),
            (
                "discord.moderation.untimeout",
                json!({"user":U}),
                "mod.timeout",
            ),
            (
                "discord.message.edit",
                json!({"message_id":"5","content":"x"}),
                "message.edit",
            ),
            (
                "discord.message.react",
                json!({"message_id":"5","emoji":"\u{1F44D}"}),
                "reaction.add",
            ),
            (
                "discord.message.delete",
                json!({"message_id":"5"}),
                "chat.delete",
            ),
        ] {
            assert_eq!(
                run(tool, args).unwrap(),
                json!({"validated":true,"op":op}),
                "{tool}"
            );
        }
    }

    #[test]
    fn moderation_and_message_tools_refuse_bad_arguments() {
        for (tool, args) in [
            ("discord.moderation.kick", json!({"user":U,"guild_id":"1"})),
            (
                "discord.moderation.kick",
                json!({"user":"700000000000000001"}),
            ),
            ("discord.moderation.kick", json!({})),
            (
                "discord.moderation.ban",
                json!({"user":U,"delete_message_seconds":604801}),
            ),
            ("discord.moderation.timeout", json!({"user":U})),
            (
                "discord.moderation.timeout",
                json!({"user":U,"duration_seconds":0}),
            ),
            (
                "discord.moderation.timeout",
                json!({"user":U,"duration_seconds":2419201}),
            ),
            (
                "discord.moderation.untimeout",
                json!({"user":U,"duration_seconds":5}),
            ),
            (
                "discord.message.edit",
                json!({"message_id":"5","content":"x","channel_id":"1"}),
            ),
            (
                "discord.message.react",
                json!({"message_id":"5","emoji":"abc"}),
            ),
            ("discord.message.delete", json!({"message_id":"../x"})),
        ] {
            assert!(
                matches!(run(tool, args.clone()), Err(ToolFail::InvalidArgs(_))),
                "{tool} {args}"
            );
        }
        assert_eq!(
            run("discord.moderation.nuke", json!({})),
            Err(ToolFail::Unknown)
        );
        assert_eq!(
            run("discord.message.send_embed", json!({})),
            Err(ToolFail::Unknown)
        );
    }
}
