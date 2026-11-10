//! `sender.build-request`: Helix request templates for `chat.send`,
//! `chat.delete` and `dm.send`.
//!
//! The connector never holds a token and never chooses a destination: the
//! broadcaster/sender/moderator ids and credentials are opaque slots with
//! handles; the host binds the target from `action.target-source-id` and
//! substitutes at send time. Any connector/caller-supplied channel field in the
//! payload is IGNORED (never read), so the template is byte-identical with or
//! without it.

use serde_json::{Value, json};

use crate::common::FrameErr;

pub(crate) const HELIX: &str = "https://api.twitch.tv/helix";
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

pub(crate) fn auth_headers() -> Vec<(String, String)> {
    vec![
        ("Authorization".to_string(), format!("Bearer {SLOT_TOKEN}")),
        ("Client-Id".to_string(), SLOT_CLIENT_ID.to_string()),
    ]
}

pub(crate) fn base_refs() -> Vec<(String, String)> {
    vec![
        (SLOT_TOKEN.to_string(), "token".to_string()),
        (SLOT_CLIENT_ID.to_string(), "client_id".to_string()),
        (SLOT_BROADCASTER.to_string(), "broadcaster_id".to_string()),
        (SLOT_SENDER.to_string(), "sender_id".to_string()),
    ]
}

/// True for characters that can hide or reshape a slot: C0/C1 controls, line
/// and paragraph separators, and invisible format characters (zero-width,
/// bidi controls, word joiner, BOM, soft hyphen, tag characters).
pub fn is_unsafe_char(c: char) -> bool {
    c.is_control()
        || matches!(c,
            '\u{00AD}' | '\u{200B}'..='\u{200F}' | '\u{2028}'..='\u{202E}'
            | '\u{2060}'..='\u{206F}' | '\u{FEFF}' | '\u{E0000}'..='\u{E007F}')
}

/// The ONE gate for every guest-/user-derived string that reaches a request
/// template. Rejects (never strips) unsafe characters first, then any brace
/// pair that could form or close a host slot, so no later transformation can
/// assemble a live `{{...}}`.
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

/// Validates user text: rejects (not strips) unsafe input, empty and oversize.
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
    if raw.chars().count() > MAX_TEXT_CHARS {
        return Err(FrameErr::Malformed(
            "text exceeds 500 characters".to_string(),
        ));
    }
    Ok(raw.to_string())
}

/// Appends every object key and string leaf of `v` to `out`, space-separated.
fn collect_strings(v: &Value, out: &mut String) {
    match v {
        Value::String(s) => {
            out.push(' ');
            out.push_str(s);
        }
        Value::Array(a) => a.iter().for_each(|x| collect_strings(x, out)),
        Value::Object(m) => {
            for (k, x) in m {
                out.push(' ');
                out.push_str(k);
                collect_strings(x, out);
            }
        }
        _ => {}
    }
}

/// Defence in depth on the finished template: the only `{{` / `}}` pairs
/// present must be the slots this module declares in `secret_refs`.
fn verify_slots(t: &Tpl) -> Result<(), FrameErr> {
    let mut all = format!("{} {:?}", t.url, t.headers);
    if let Some(b) = &t.body {
        // Scan JSON string leaves/keys, not raw bytes: nested objects legitimately end in `}}`.
        match serde_json::from_slice::<Value>(b) {
            Ok(v) => collect_strings(&v, &mut all),
            Err(_) => all.push_str(&String::from_utf8_lossy(b)),
        }
    }
    // Host-duty slots (callback, secret, segment id) carry no `secret-refs` handle: the host
    // fills them from its own state, so they are expected placeholders too.
    let expected: usize = t
        .secret_refs
        .iter()
        .map(|(slot, _)| slot.as_str())
        .chain(crate::eventsub_sub::DUTY_SLOTS.iter().copied())
        .map(|slot| all.matches(slot).count())
        .sum();
    if all.matches("{{").count() != expected || all.matches("}}").count() != expected {
        return Err(FrameErr::Malformed(
            "template carries an undeclared slot".to_string(),
        ));
    }
    Ok(())
}

pub(crate) fn id_arg(payload: &Value, key: &str, digits_only: bool) -> Result<String, FrameErr> {
    let v = payload
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| FrameErr::Malformed(format!("payload needs string `{key}`")))?;
    reject_unsafe(key, v)?;
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

/// Auth headers plus `Content-Type: application/json`.
pub(crate) fn json_headers() -> Vec<(String, String)> {
    let mut h = auth_headers();
    h.push(("Content-Type".to_string(), "application/json".to_string()));
    h
}

/// Builds the template for `kind`. `target_source_id` must be non-empty: the
/// host resolves and pins the destination from it (never from this output).
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
        k if crate::moderation::KINDS.contains(&k) => crate::moderation::build(k, &payload),
        k if crate::eventsub_sub::KINDS.contains(&k) => crate::eventsub_sub::build(k, &payload),
        k if crate::schedule::KINDS.contains(&k) => crate::schedule::build(k, &payload),
        crate::clips::CREATE_KIND => crate::clips::build_create(&payload),
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
        "chat.clear" => crate::moderation::clear(&payload),
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

/// Builds the template for `kind`. `target_source_id` must be non-empty: the
/// host resolves and pins the destination from it (never from this output).
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
    fn connector_supplied_channel_is_ignored() {
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

    /// Regression: the check used to run BEFORE control chars were stripped, so
    /// `{<NUL>{TWITCH_TOKEN}}` assembled a live slot after filtering.
    #[test]
    fn control_or_invisible_chars_between_braces_are_rejected() {
        for sep in [
            '\u{0}',
            '\u{1}',
            '\u{7f}',
            '\u{200B}',
            '\u{2060}',
            '\u{FEFF}',
            '\u{202E}',
            '\u{E0041}',
            '\u{2028}',
            '\u{AD}',
        ] {
            let t = format!("{{{sep}{{TWITCH_TOKEN}}{sep}}}");
            for kind in ["chat.send", "dm.send"] {
                let p = json!({"text": t, "to_user_id": "1"}).to_string();
                assert!(
                    matches!(build_request(kind, "s", &p), Err(FrameErr::Malformed(_))),
                    "{kind} accepted U+{:04X}",
                    sep as u32
                );
            }
        }
    }

    #[test]
    fn lone_close_braces_and_every_slot_literal_are_rejected() {
        for t in [
            "}}",
            "a }} b",
            "{{",
            "{{TWITCH_BROADCASTER_ID}}",
            "{{TWITCH_SENDER_ID}}",
            "{{TWITCH_CLIENT_ID}}",
        ] {
            assert!(build_request("chat.send", "s", &text(t)).is_err(), "{t}");
        }
    }

    /// Lookalike braces are not slot syntax (the host matches ASCII bytes), so
    /// they pass as plain text but can never form a declared slot.
    #[test]
    fn unicode_lookalike_braces_never_form_a_slot() {
        let t = build_request(
            "chat.send",
            "s",
            &text("\u{FF5B}\u{FF5B}TWITCH_TOKEN\u{FF5D}\u{FF5D}"),
        )
        .unwrap();
        let body = String::from_utf8(t.body.clone().unwrap()).unwrap();
        assert_eq!(body.matches(SLOT_TOKEN).count(), 0);
        assert_eq!(
            body.matches("{{").count(),
            2,
            "only the two declared body slots"
        );
    }

    #[test]
    fn reject_unsafe_layer_alone_blocks_each_class() {
        assert!(reject_unsafe("x", "a}}b").is_err());
        assert!(reject_unsafe("x", "a{{b").is_err());
        assert!(reject_unsafe("x", "a\u{0}b").is_err());
        assert!(reject_unsafe("x", "a\u{200B}b").is_err());
        assert!(reject_unsafe("x", "plain {single} braces ok").is_ok());
    }

    #[test]
    fn verify_slots_catches_undeclared_slot() {
        let mut t = build_request("chat.send", "s", &text("hi")).unwrap();
        assert!(verify_slots(&t).is_ok());
        t.url.push_str("&x={{EVIL}}");
        assert!(verify_slots(&t).is_err());
        let mut t2 = build_request("chat.send", "s", &text("hi")).unwrap();
        t2.headers.push(("X".to_string(), "}}".to_string()));
        assert!(verify_slots(&t2).is_err());
    }

    #[test]
    fn id_fields_reject_unsafe_characters() {
        let p = json!({"message_id": "ab\u{0}c"}).to_string();
        assert!(build_request("chat.delete", "s", &p).is_err());
        let p = json!({"to_user_id": "12\u{200B}3", "text": "x"}).to_string();
        assert!(build_request("dm.send", "s", &p).is_err());
    }

    #[test]
    fn text_rules() {
        assert!(build_request("chat.send", "s", &text("   ")).is_err());
        assert!(build_request("chat.send", "s", &text(&"x".repeat(501))).is_err());
        assert!(build_request("chat.send", "s", "{}").is_err());
        // Control characters are REJECTED, never silently stripped.
        assert!(build_request("chat.send", "s", &text("a\r\nPRIVMSG b")).is_err());
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
