//! `sender.build-request`: Helix request templates for `chat.send`,
//! `chat.delete` and `dm.send`.
//!
//! The overlay never holds a token and never chooses a destination: the
//! broadcaster/sender/moderator ids and credentials are opaque slots with
//! handles; the host binds the target from `action.target-source-id` and
//! substitutes at send time. Any overlay/caller-supplied channel field in the
//! payload is IGNORED (never read), so the template is byte-identical with or
//! without it.

use serde_json::{Value, json};

use crate::common::FrameErr;

const HELIX: &str = "https://api.twitch.tv/helix";
/// Slot: bearer token (header value after `Bearer `).
pub const SLOT_TOKEN: &str = "{{TWITCH_TOKEN}}";
/// Slot: application client id header.
pub const SLOT_CLIENT_ID: &str = "{{TWITCH_CLIENT_ID}}";
/// Slot: host-pinned broadcaster (channel owner) user id.
pub const SLOT_BROADCASTER: &str = "{{TWITCH_BROADCASTER_ID}}";
/// Slot: host-pinned sending bot/moderator user id.
pub const SLOT_SENDER: &str = "{{TWITCH_SENDER_ID}}";
/// Max Twitch chat message length (characters).
pub const MAX_TEXT_CHARS: usize = 500;

/// Plain mirror of WIT `http-request-tpl`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Tpl {
    /// HTTP method.
    pub method: String,
    /// Absolute URL (fixed Helix host; slots only in the query).
    pub url: String,
    /// Header name/value pairs.
    pub headers: Vec<(String, String)>,
    /// Optional JSON body.
    pub body: Option<Vec<u8>>,
    /// `(slot, handle)` pairs the host substitutes.
    pub secret_refs: Vec<(String, String)>,
}

/// Payload keys naming a destination; present-and-ignored by design.
pub const IGNORED_TARGET_KEYS: &[&str] = &[
    "channel",
    "channel_name",
    "broadcaster_id",
    "broadcaster",
    "room_id",
    "sender_id",
    "moderator_id",
    "target",
];

fn auth_headers() -> Vec<(String, String)> {
    vec![
        ("Authorization".to_string(), format!("Bearer {SLOT_TOKEN}")),
        ("Client-Id".to_string(), SLOT_CLIENT_ID.to_string()),
    ]
}

fn base_refs() -> Vec<(String, String)> {
    vec![
        (SLOT_TOKEN.to_string(), "token".to_string()),
        (SLOT_CLIENT_ID.to_string(), "client_id".to_string()),
        (SLOT_BROADCASTER.to_string(), "broadcaster_id".to_string()),
        (SLOT_SENDER.to_string(), "sender_id".to_string()),
    ]
}

/// Validates + sanitizes user text. Rejects empty/oversize text and any `{{`
/// (a hostile text must never smuggle a slot the host would substitute).
fn clean_text(payload: &Value) -> Result<String, FrameErr> {
    let raw = payload
        .get("text")
        .or_else(|| payload.get("message"))
        .and_then(Value::as_str)
        .ok_or_else(|| FrameErr::Malformed("payload needs string `text`".to_string()))?;
    if raw.contains("{{") {
        return Err(FrameErr::Malformed(
            "text contains reserved `{{`".to_string(),
        ));
    }
    let text: String = raw.chars().filter(|c| !c.is_control()).collect();
    if text.trim().is_empty() {
        return Err(FrameErr::Malformed("text is empty".to_string()));
    }
    if text.chars().count() > MAX_TEXT_CHARS {
        return Err(FrameErr::Malformed(
            "text exceeds 500 characters".to_string(),
        ));
    }
    Ok(text)
}

fn id_arg(payload: &Value, key: &str, digits_only: bool) -> Result<String, FrameErr> {
    let v = payload
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| FrameErr::Malformed(format!("payload needs string `{key}`")))?;
    let ok = !v.is_empty()
        && v.len() <= 64
        && v.bytes().all(|b| {
            if digits_only {
                b.is_ascii_digit()
            } else {
                b.is_ascii_alphanumeric() || b == b'-' || b == b'_'
            }
        });
    if ok {
        Ok(v.to_string())
    } else {
        Err(FrameErr::Malformed(format!("`{key}` has an invalid shape")))
    }
}

/// Builds the template for `kind`. `target_source_id` must be non-empty: the
/// host resolves and pins the destination from it (never from this output).
pub fn build_request(
    kind: &str,
    target_source_id: &str,
    payload_json: &str,
) -> Result<Tpl, FrameErr> {
    if target_source_id.is_empty() {
        return Err(FrameErr::Malformed(
            "target-source-id is required".to_string(),
        ));
    }
    let payload: Value = serde_json::from_str(payload_json)
        .map_err(|_| FrameErr::Malformed("payload-json is not JSON".to_string()))?;
    if !payload.is_object() {
        return Err(FrameErr::Malformed(
            "payload-json is not an object".to_string(),
        ));
    }
    let json_headers = || {
        let mut h = auth_headers();
        h.push(("Content-Type".to_string(), "application/json".to_string()));
        h
    };
    match kind {
        "chat.send" => {
            let text = clean_text(&payload)?;
            let body = json!({
                "broadcaster_id": SLOT_BROADCASTER,
                "sender_id": SLOT_SENDER,
                "message": text,
            });
            Ok(Tpl {
                method: "POST".to_string(),
                url: format!("{HELIX}/chat/messages"),
                headers: json_headers(),
                body: Some(body.to_string().into_bytes()),
                secret_refs: base_refs(),
            })
        }
        "chat.delete" => {
            let message_id = id_arg(&payload, "message_id", false)?;
            Ok(Tpl {
                method: "DELETE".to_string(),
                url: format!(
                    "{HELIX}/moderation/chat?broadcaster_id={SLOT_BROADCASTER}&moderator_id={SLOT_SENDER}&message_id={message_id}"
                ),
                headers: auth_headers(),
                body: None,
                secret_refs: base_refs(),
            })
        }
        "dm.send" => {
            let to = id_arg(&payload, "to_user_id", true)?;
            let text = clean_text(&payload)?;
            Ok(Tpl {
                method: "POST".to_string(),
                url: format!("{HELIX}/whispers?from_user_id={SLOT_SENDER}&to_user_id={to}"),
                headers: json_headers(),
                body: Some(json!({ "message": text }).to_string().into_bytes()),
                secret_refs: base_refs(),
            })
        }
        other => Err(FrameErr::Unsupported(format!(
            "unsupported action kind {other}"
        ))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn text(t: &str) -> String {
        json!({ "text": t }).to_string()
    }

    #[test]
    fn chat_send_holds_no_token_and_no_destination() {
        let t = build_request("chat.send", "src-1", &text("hi")).unwrap();
        assert_eq!(t.method, "POST");
        let body = String::from_utf8(t.body.clone().unwrap()).unwrap();
        assert!(body.contains(SLOT_BROADCASTER) && body.contains(SLOT_SENDER));
        assert!(
            !body.contains("src-1"),
            "source id must not leak into the body"
        );
        // Every secret-ref slot appears in the template.
        let all = format!("{:?}{}", t.headers, body);
        for (slot, _) in &t.secret_refs {
            assert!(all.contains(slot.as_str()), "{slot}");
        }
    }

    #[test]
    fn overlay_supplied_channel_is_ignored() {
        let plain = build_request("chat.send", "s", &text("hi")).unwrap();
        for key in IGNORED_TARGET_KEYS {
            let p = json!({ "text": "hi", *key: "evilchannel" }).to_string();
            let t = build_request("chat.send", "s", &p).unwrap();
            assert_eq!(t, plain, "{key} must not influence the template");
        }
        let del = json!({"message_id":"abc-1","channel":"evil","broadcaster_id":"999"}).to_string();
        let t = build_request("chat.delete", "s", &del).unwrap();
        assert!(!t.url.contains("evil") && !t.url.contains("999"));
    }

    #[test]
    fn slot_smuggling_is_rejected() {
        let e = build_request("chat.send", "s", &text("steal {{TWITCH_TOKEN}}"));
        assert!(matches!(e, Err(FrameErr::Malformed(_))));
    }

    #[test]
    fn text_rules() {
        assert!(build_request("chat.send", "s", &text("   ")).is_err());
        assert!(build_request("chat.send", "s", &text(&"x".repeat(501))).is_err());
        assert!(build_request("chat.send", "s", "{}").is_err());
        let t = build_request("chat.send", "s", &text("a\r\nPRIVMSG b")).unwrap();
        assert!(!String::from_utf8(t.body.unwrap()).unwrap().contains('\r'));
        // `message` alias accepted.
        let m = json!({"message":"hey"}).to_string();
        assert!(build_request("chat.send", "s", &m).is_ok());
    }

    #[test]
    fn delete_and_dm_validate_ids() {
        let ok = json!({"message_id":"abc-123"}).to_string();
        let t = build_request("chat.delete", "s", &ok).unwrap();
        assert_eq!(t.method, "DELETE");
        assert!(t.url.ends_with("message_id=abc-123") && t.body.is_none());
        let bad = json!({"message_id":"a&b=1"}).to_string();
        assert!(build_request("chat.delete", "s", &bad).is_err());
        assert!(build_request("chat.delete", "s", "{}").is_err());

        let dm = json!({"to_user_id":"12345","text":"psst"}).to_string();
        let t = build_request("dm.send", "s", &dm).unwrap();
        assert!(t.url.contains("/whispers?") && t.url.ends_with("to_user_id=12345"));
        let bad = json!({"to_user_id":"12x","text":"psst"}).to_string();
        assert!(build_request("dm.send", "s", &bad).is_err());
    }

    #[test]
    fn envelope_errors() {
        assert!(build_request("chat.send", "", &text("x")).is_err());
        assert!(build_request("chat.send", "s", "nope").is_err());
        assert!(build_request("chat.send", "s", "[]").is_err());
        assert!(matches!(
            build_request("moderation.ban", "s", "{}"),
            Err(FrameErr::Unsupported(_))
        ));
    }
}
