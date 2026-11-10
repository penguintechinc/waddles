//! `sender.build-request`: Kick public-API request templates for `chat.send`, `chat.delete`,
//! `mod.ban`, `mod.timeout` and `mod.unban`.
//!
//! API reference: <https://docs.kick.com/apis/chat>. The connector never holds a token and
//! never chooses a destination: the credential is the opaque slot `{{KICK_TOKEN}}` the host
//! substitutes at send time. `chat.send` posts as the application's bot (`type: "bot"`), which
//! Kick always delivers to the channel attached to the (host-resolved) token, so no channel id
//! is needed -- or accepted -- in the template. Any caller-supplied channel field in the
//! payload is IGNORED (never read), so the template is byte-identical with or without it.
//!
//! Moderation (<https://docs.kick.com/apis/moderation>, `/public/v1/moderation/bans`): Kick
//! takes `broadcaster_user_id` and `user_id` as JSON INTEGERS. The broadcaster is the opaque host
//! slot `{{KICK_BROADCASTER_ID}}` (the host writes it as a JSON integer from the community's own
//! connection, so a template can never aim the token at another channel); the target `user_id` is
//! the producer's platform id, parsed here as a non-zero `u64` and emitted as a literal integer.
//! The host refuses a request whose target equals the broadcaster (protected target).
//! `mod.timeout` requires `text` = `{"duration":<1..=10080 minutes>[,"reason":".."]}`; `mod.ban`
//! takes an optional plain-text reason in `text`; `mod.unban` takes no `text`.

use serde_json::{Value, json};

use crate::common::FrameErr;

const API: &str = "https://api.kick.com/public/v1";
/// Slot: bearer token (header value after `Bearer `).
pub const SLOT_TOKEN: &str = "{{KICK_TOKEN}}";
/// Slot: the community connection's broadcaster user id (host-written JSON integer).
pub const SLOT_BROADCASTER: &str = "{{KICK_BROADCASTER_ID}}";
/// Kick's documented ceiling for a timeout, in minutes (7 days).
pub const MAX_TIMEOUT_MINUTES: u64 = 10_080;
/// Kick's documented ban/timeout reason limit, in characters.
pub const MAX_REASON_CHARS: usize = 100;
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

/// The producer's target user id: a non-zero decimal `u64` (Kick user ids are integers).
fn target_user_id(payload: &Value) -> Result<u64, FrameErr> {
    let raw = payload
        .get("user_id")
        .and_then(Value::as_str)
        .ok_or_else(|| FrameErr::Malformed("payload needs string `user_id`".to_string()))?;
    if !raw.bytes().all(|b| b.is_ascii_digit()) {
        return Err(FrameErr::Malformed("`user_id` must be digits".to_string()));
    }
    match raw.parse::<u64>() {
        Ok(n) if n != 0 => Ok(n),
        _ => Err(FrameErr::Malformed(
            "`user_id` must be a non-zero integer".to_string(),
        )),
    }
}

/// A ban/timeout reason: bounded, no unsafe characters or slot braces.
fn clean_reason(raw: &str) -> Result<String, FrameErr> {
    reject_unsafe("reason", raw)?;
    if raw.trim().is_empty() || raw.chars().count() > MAX_REASON_CHARS {
        return Err(FrameErr::Malformed(format!(
            "reason must be 1..={MAX_REASON_CHARS} characters"
        )));
    }
    Ok(raw.to_string())
}

/// The moderation template for `kind`. `text` is the producer's optional free text (see the
/// module doc for each op's grammar); an unexpected `text` fails loud rather than being ignored.
fn moderation_tpl(kind: &str, payload: &Value) -> Result<Tpl, FrameErr> {
    let user_id = target_user_id(payload)?;
    let text = payload.get("text").and_then(Value::as_str);
    let mut body = json!({ "broadcaster_user_id": SLOT_BROADCASTER, "user_id": user_id });
    match kind {
        "mod.ban" => {
            if let Some(t) = text {
                body["reason"] = Value::String(clean_reason(t)?);
            }
        }
        "mod.timeout" => {
            let raw = text.ok_or_else(|| {
                FrameErr::Malformed("mod.timeout needs `text` with a duration".to_string())
            })?;
            let spec: Value = serde_json::from_str(raw).map_err(|_| {
                FrameErr::Malformed("mod.timeout `text` is not a JSON object".to_string())
            })?;
            let obj = spec.as_object().ok_or_else(|| {
                FrameErr::Malformed("mod.timeout `text` is not a JSON object".to_string())
            })?;
            if obj.keys().any(|k| k != "duration" && k != "reason") {
                return Err(FrameErr::Malformed(
                    "mod.timeout `text` has an unknown key".to_string(),
                ));
            }
            let minutes = obj
                .get("duration")
                .and_then(Value::as_u64)
                .filter(|m| (1..=MAX_TIMEOUT_MINUTES).contains(m))
                .ok_or_else(|| {
                    FrameErr::Malformed(format!(
                        "duration must be an integer 1..={MAX_TIMEOUT_MINUTES} minutes"
                    ))
                })?;
            body["duration"] = Value::from(minutes);
            if let Some(r) = obj.get("reason") {
                let r = r
                    .as_str()
                    .ok_or_else(|| FrameErr::Malformed("reason must be a string".to_string()))?;
                body["reason"] = Value::String(clean_reason(r)?);
            }
        }
        _ => {
            if text.is_some() {
                return Err(FrameErr::Malformed("mod.unban takes no `text`".to_string()));
            }
        }
    }
    let mut headers = auth_headers();
    headers.push(("Content-Type".to_string(), "application/json".to_string()));
    let mut secret_refs = base_refs();
    secret_refs.push((SLOT_BROADCASTER.to_string(), "broadcaster_id".to_string()));
    Ok(Tpl {
        method: if kind == "mod.unban" {
            "DELETE"
        } else {
            "POST"
        }
        .to_string(),
        url: format!("{API}/moderation/bans"),
        headers,
        body: Some(body.to_string().into_bytes()),
        secret_refs,
    })
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
        "mod.ban" | "mod.timeout" | "mod.unban" => moderation_tpl(kind, &payload),
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

    fn body_of(t: &Tpl) -> Value {
        serde_json::from_slice(t.body.as_ref().unwrap()).unwrap()
    }

    #[test]
    fn mod_ban_is_a_post_with_a_host_broadcaster_slot_and_an_integer_target() {
        let p = json!({"user_id": "4242", "text": "spam"}).to_string();
        let t = build_request("mod.ban", "s", &p).unwrap();
        assert_eq!(t.method, "POST");
        assert_eq!(t.url, "https://api.kick.com/public/v1/moderation/bans");
        let b = body_of(&t);
        assert_eq!(b["broadcaster_user_id"], SLOT_BROADCASTER);
        assert_eq!(b["user_id"], 4242);
        assert!(b["user_id"].is_u64());
        assert_eq!(b["reason"], "spam");
        assert!(b.get("duration").is_none());
        assert_eq!(
            t.secret_refs,
            vec![
                (SLOT_TOKEN.into(), "token".into()),
                (SLOT_BROADCASTER.into(), "broadcaster_id".into())
            ]
        );
        // No reason: key absent.
        let t = build_request("mod.ban", "s", &json!({"user_id": "7"}).to_string()).unwrap();
        assert!(body_of(&t).get("reason").is_none());
    }

    #[test]
    fn mod_timeout_requires_a_bounded_duration() {
        let ok = |d: Value| {
            let text = json!({"duration": d, "reason": "calm down"}).to_string();
            build_request(
                "mod.timeout",
                "s",
                &json!({"user_id": "9", "text": text}).to_string(),
            )
        };
        let b = body_of(&ok(json!(10)).unwrap());
        assert_eq!(b["duration"], 10);
        assert_eq!(b["reason"], "calm down");
        assert!(ok(json!(MAX_TIMEOUT_MINUTES)).is_ok());
        for bad in [
            json!(0),
            json!(MAX_TIMEOUT_MINUTES + 1),
            json!(-1),
            json!("5"),
            json!(1.5),
        ] {
            assert!(ok(bad.clone()).is_err(), "{bad}");
        }
        // Missing / malformed / extra-key text all fail loud.
        for text in [
            None,
            Some("nope"),
            Some("[1]"),
            Some(r#"{"duration":5,"x":1}"#),
            Some("{}"),
        ] {
            let mut p = json!({"user_id": "9"});
            if let Some(t) = text {
                p["text"] = Value::String(t.to_string());
            }
            assert!(
                build_request("mod.timeout", "s", &p.to_string()).is_err(),
                "{text:?}"
            );
        }
    }

    #[test]
    fn mod_unban_is_a_delete_with_no_text() {
        let t = build_request("mod.unban", "s", &json!({"user_id": "31"}).to_string()).unwrap();
        assert_eq!(t.method, "DELETE");
        assert_eq!(body_of(&t)["user_id"], 31);
        let with_text = json!({"user_id": "31", "text": "x"}).to_string();
        assert!(build_request("mod.unban", "s", &with_text).is_err());
    }

    #[test]
    fn moderation_target_must_be_a_non_zero_digit_string_and_reasons_are_sanitized() {
        for bad in [
            json!("0"),
            json!(""),
            json!("-1"),
            json!("1e3"),
            json!("12 "),
            json!(5),
            json!("18446744073709551616"),
        ] {
            let p = json!({"user_id": bad}).to_string();
            for k in ["mod.ban", "mod.unban"] {
                assert!(build_request(k, "s", &p).is_err(), "{k} {bad}");
            }
        }
        assert!(build_request("mod.ban", "s", "{}").is_err());
        for reason in ["x {{KICK_TOKEN}}", "a\u{200B}b", "", &"r".repeat(101)] {
            let p = json!({"user_id": "1", "text": reason}).to_string();
            assert!(build_request("mod.ban", "s", &p).is_err(), "{reason:?}");
        }
        assert!(
            build_request(
                "mod.ban",
                "s",
                &json!({"user_id":"1","text":"r".repeat(100)}).to_string()
            )
            .is_ok()
        );
    }

    #[test]
    fn a_connector_supplied_broadcaster_is_never_read_for_moderation() {
        let plain = build_request("mod.ban", "s", &json!({"user_id": "5"}).to_string()).unwrap();
        let p = json!({"user_id": "5", "broadcaster_user_id": "999", "broadcaster_id": 999})
            .to_string();
        assert_eq!(build_request("mod.ban", "s", &p).unwrap(), plain);
    }
}
