//! `sender.build-request`: Kick public-API request templates for `chat.send` and `chat.delete`.
//!
//! API reference: <https://docs.kick.com/apis/chat>. The connector never holds a token and
//! never chooses a destination: the credential is the opaque slot `{{KICK_TOKEN}}` the host
//! substitutes at send time. `chat.send` posts as the application's bot (`type: "bot"`), which
//! Kick always delivers to the channel attached to the (host-resolved) token, so no channel id
//! is needed -- or accepted -- in the template. Any caller-supplied channel field in the
//! payload is IGNORED (never read), so the template is byte-identical with or without it.
//!
//! `mod.ban` / `mod.timeout` / `mod.unban` (<https://docs.kick.com/apis/moderation>) need the
//! broadcaster's id as a JSON INTEGER in the request body (`broadcaster_user_id`, `user_id`);
//! the host's template substituter can only write JSON strings, which Kick's integer fields
//! reject. They therefore fail loud (`Unsupported`) until the host gains an integer body slot;
//! see README "Needs from host seams".

use serde_json::{Value, json};

use crate::common::FrameErr;

const API: &str = "https://api.kick.com/public/v1";
/// Slot: bearer token (header value after `Bearer `).
pub const SLOT_TOKEN: &str = "{{KICK_TOKEN}}";
/// Kick's documented chat content limit (user-perceived characters).
pub const MAX_TEXT_CHARS: usize = 500;
/// Kick's documented byte ceiling for one chat message (UTF-8).
pub const MAX_TEXT_BYTES: usize = 2048;

/// Plain mirror of WIT `http-request-tpl`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Tpl {
    /// HTTP method.
    pub method: String,
    /// Absolute URL (fixed Kick API host; no slots).
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
    "channel_slug",
    "broadcaster_id",
    "broadcaster_user_id",
    "broadcaster",
    "chatroom_id",
    "room_id",
    "sender_id",
    "moderator_id",
    "type",
    "target",
];

fn auth_headers() -> Vec<(String, String)> {
    vec![("Authorization".to_string(), format!("Bearer {SLOT_TOKEN}"))]
}

fn base_refs() -> Vec<(String, String)> {
    vec![(SLOT_TOKEN.to_string(), "token".to_string())]
}

/// True for characters that can hide or reshape a slot: C0/C1 controls, line and paragraph
/// separators, and invisible format characters (zero-width, bidi controls, word joiner, BOM,
/// soft hyphen, tag characters).
pub fn is_unsafe_char(c: char) -> bool {
    c.is_control()
        || matches!(c,
            '\u{00AD}' | '\u{200B}'..='\u{200F}' | '\u{2028}'..='\u{202E}'
            | '\u{2060}'..='\u{206F}' | '\u{FEFF}' | '\u{E0000}'..='\u{E007F}')
}

/// The ONE gate for every guest-/user-derived string that reaches a request template. Rejects
/// (never strips) unsafe characters first, then any brace pair that could form or close a host
/// slot, so no later transformation can assemble a live `{{...}}`.
pub fn reject_unsafe(label: &str, raw: &str) -> Result<(), FrameErr> {
    if raw.chars().any(is_unsafe_char) {
        return Err(FrameErr::Malformed(format!(
            "{label} contains control or invisible characters"
        )));
    }
    if raw.contains("{{") || raw.contains("}}") {
        return Err(FrameErr::Malformed(format!(
            "{label} contains reserved brace pair"
        )));
    }
    Ok(())
}

fn clean_text(payload: &Value) -> Result<String, FrameErr> {
    let raw = payload
        .get("text")
        .or_else(|| payload.get("message"))
        .and_then(Value::as_str)
        .ok_or_else(|| FrameErr::Malformed("payload needs string `text`".to_string()))?;
    reject_unsafe("text", raw)?;
    if raw.trim().is_empty() {
        return Err(FrameErr::Malformed("text is empty".to_string()));
    }
    if raw.chars().count() > MAX_TEXT_CHARS || raw.len() > MAX_TEXT_BYTES {
        return Err(FrameErr::Malformed(
            "text exceeds Kick's chat message limit".to_string(),
        ));
    }
    Ok(raw.to_string())
}

/// A Kick message id: a UUID-shaped token (`[A-Za-z0-9-]`, 1..=64), safe as a URL path segment.
fn message_id(payload: &Value, key: &str) -> Result<String, FrameErr> {
    let v = payload
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| FrameErr::Malformed(format!("payload needs string `{key}`")))?;
    reject_unsafe(key, v)?;
    if v.is_empty() || v.len() > 64 || !v.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-') {
        return Err(FrameErr::Malformed(format!("`{key}` has an invalid shape")));
    }
    Ok(v.to_string())
}

/// Defence in depth on the finished template: the only `{{` / `}}` pairs present must be the
/// slots this module declares in `secret_refs`.
fn verify_slots(t: &Tpl) -> Result<(), FrameErr> {
    let mut all = format!("{} {:?}", t.url, t.headers);
    if let Some(b) = &t.body {
        all.push_str(&String::from_utf8_lossy(b));
    }
    let expected: usize = t
        .secret_refs
        .iter()
        .map(|(slot, _)| all.matches(slot.as_str()).count())
        .sum();
    if all.matches("{{").count() != expected || all.matches("}}").count() != expected {
        return Err(FrameErr::Malformed(
            "template carries an undeclared slot".to_string(),
        ));
    }
    Ok(())
}

fn build_unchecked(
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
    match kind {
        "chat.send" => {
            let mut body = json!({ "content": clean_text(&payload)?, "type": "bot" });
            if payload.get("reply_to_message_id").is_some() {
                body["reply_to_message_id"] =
                    Value::String(message_id(&payload, "reply_to_message_id")?);
            }
            let mut headers = auth_headers();
            headers.push(("Content-Type".to_string(), "application/json".to_string()));
            Ok(Tpl {
                method: "POST".to_string(),
                url: format!("{API}/chat"),
                headers,
                body: Some(body.to_string().into_bytes()),
                secret_refs: base_refs(),
            })
        }
        "chat.delete" => {
            let id = message_id(&payload, "message_id")?;
            Ok(Tpl {
                method: "DELETE".to_string(),
                url: format!("{API}/chat/{id}"),
                headers: auth_headers(),
                body: None,
                secret_refs: base_refs(),
            })
        }
        "mod.ban" | "mod.timeout" | "mod.unban" => Err(FrameErr::Unsupported(format!(
            "{kind} needs an integer-typed host body slot (Kick requires integer \
             broadcaster_user_id); not available yet"
        ))),
        other => Err(FrameErr::Unsupported(format!(
            "unsupported action kind {other}"
        ))),
    }
}

/// Builds the template for `kind`. `target_source_id` must be non-empty: the host resolves and
/// pins the destination from it (never from this output).
pub fn build_request(
    kind: &str,
    target_source_id: &str,
    payload_json: &str,
) -> Result<Tpl, FrameErr> {
    let t = build_unchecked(kind, target_source_id, payload_json)?;
    verify_slots(&t)?;
    Ok(t)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn text(t: &str) -> String {
        json!({ "text": t }).to_string()
    }

    #[test]
    fn chat_send_is_a_bot_post_with_only_the_token_slot() {
        let t = build_request("chat.send", "src-1", &text("hi")).unwrap();
        assert_eq!(t.method, "POST");
        assert_eq!(t.url, "https://api.kick.com/public/v1/chat");
        let body: Value = serde_json::from_slice(t.body.as_ref().unwrap()).unwrap();
        assert_eq!(body, json!({"content": "hi", "type": "bot"}));
        assert_eq!(t.secret_refs, vec![(SLOT_TOKEN.into(), "token".into())]);
        assert!(
            t.headers
                .contains(&("Authorization".into(), "Bearer {{KICK_TOKEN}}".into()))
        );
        assert!(!format!("{t:?}").contains("src-1"));
    }

    #[test]
    fn reply_id_is_validated_and_included() {
        let p = json!({"text":"x","reply_to_message_id":"828f83bb-e391-4c78-9e91-af35a172840e"});
        let t = build_request("chat.send", "s", &p.to_string()).unwrap();
        let body: Value = serde_json::from_slice(t.body.as_ref().unwrap()).unwrap();
        assert_eq!(
            body["reply_to_message_id"],
            "828f83bb-e391-4c78-9e91-af35a172840e"
        );
        for bad in ["a/b", "a b", "", "x{{KICK_TOKEN}}", "../x"] {
            let p = json!({"text":"x","reply_to_message_id":bad}).to_string();
            assert!(build_request("chat.send", "s", &p).is_err(), "{bad}");
        }
    }

    #[test]
    fn chat_delete_puts_only_a_validated_id_in_the_path() {
        let p = json!({"message_id": "828f83bb-e391-4c78-9e91-af35a172840e"}).to_string();
        let t = build_request("chat.delete", "s", &p).unwrap();
        assert_eq!(t.method, "DELETE");
        assert_eq!(
            t.url,
            "https://api.kick.com/public/v1/chat/828f83bb-e391-4c78-9e91-af35a172840e"
        );
        assert!(t.body.is_none());
        for bad in ["a/../b", "a?x=1", "a#f", "a%2f", "{{KICK_TOKEN}}", ""] {
            let p = json!({ "message_id": bad }).to_string();
            assert!(build_request("chat.delete", "s", &p).is_err(), "{bad}");
        }
        assert!(build_request("chat.delete", "s", "{}").is_err());
    }

    #[test]
    fn connector_supplied_destination_is_ignored() {
        let plain = build_request("chat.send", "s", &text("hi")).unwrap();
        for key in IGNORED_TARGET_KEYS {
            let p = json!({ "text": "hi", *key: "evil" }).to_string();
            assert_eq!(build_request("chat.send", "s", &p).unwrap(), plain, "{key}");
        }
    }

    #[test]
    fn slot_smuggling_and_invisible_characters_are_rejected() {
        assert!(build_request("chat.send", "s", &text("steal {{KICK_TOKEN}}")).is_err());
        for sep in [
            '\u{0}',
            '\u{7f}',
            '\u{200B}',
            '\u{2060}',
            '\u{FEFF}',
            '\u{202E}',
            '\u{E0041}',
            '\u{AD}',
        ] {
            let t = format!("{{{sep}{{KICK_TOKEN}}{sep}}}");
            assert!(
                build_request("chat.send", "s", &text(&t)).is_err(),
                "{sep:?}"
            );
        }
        assert!(build_request("chat.send", "s", &text("a}}b")).is_err());
    }

    #[test]
    fn text_limits_are_enforced() {
        assert!(build_request("chat.send", "s", &text(&"a".repeat(500))).is_ok());
        assert!(build_request("chat.send", "s", &text(&"a".repeat(501))).is_err());
        // 500 four-byte chars = 2000 bytes: inside both limits (the byte ceiling is a backstop).
        assert!(build_request("chat.send", "s", &text(&"\u{1F600}".repeat(500))).is_ok());
        assert!(build_request("chat.send", "s", &text("   ")).is_err());
        assert!(build_request("chat.send", "s", "{}").is_err());
    }

    #[test]
    fn missing_target_bad_payload_and_unknown_kind_fail_loud() {
        assert!(matches!(
            build_request("chat.send", "", &text("hi")),
            Err(FrameErr::Malformed(_))
        ));
        for bad in ["nope", "[1]"] {
            assert!(matches!(
                build_request("chat.send", "s", bad),
                Err(FrameErr::Malformed(_))
            ));
        }
        assert!(matches!(
            build_request("dm.send", "s", &text("hi")),
            Err(FrameErr::Unsupported(_))
        ));
    }

    /// Pins the open integer-slot gap: when the host grows an integer body slot this test must
    /// fail on purpose, prompting the real implementation + policy kinds.
    #[test]
    fn moderation_fails_loud_until_the_host_has_integer_body_slots() {
        for k in ["mod.ban", "mod.timeout", "mod.unban"] {
            let e = build_request(k, "s", &json!({"user_id": "5"}).to_string()).unwrap_err();
            let FrameErr::Unsupported(m) = e else {
                panic!("{k} must be Unsupported");
            };
            assert!(m.contains("integer"), "{m}");
        }
    }
}
