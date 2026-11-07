//! YouTube live-chat moderation request templates: message delete, bans and moderators.
//!
//! The connector only SHAPES the request; the host verifies it against its exact per-kind
//! policy (`core/svc_ingest/.../policy/youtube.rs`) and enforces everything this module cannot:
//!
//! * the live chat is the host slot `{{YT_LIVE_CHAT_ID}}` (stamp-pinned, ownership proven by the
//!   community's connection) -- never a value from the payload;
//! * the target user is the host slot `{{YT_TARGET_USER}}` (the producer's tokenized UUID,
//!   detokenized host-side for the entry's own community) -- the payload's `user_id` is never
//!   embedded;
//! * the id of a message / ban / moderator to remove is proven by the host to belong to the
//!   pinned chat before the request is sent, and the per-community quota is charged.
//!
//! Payload (the host hands over only these keys): `message_id` (delete kinds), `user_id`
//! (ban/add, a UUID the connector validates but never embeds), `text` (`mod.ban` only: the ban
//! length in whole seconds; absent = permanent).

use serde_json::{Value, json};

use crate::logic::{
    API_BASE, CHAT_ID_HANDLE, CHAT_ID_SLOT, LogicError, RequestTpl, TOKEN_HANDLE, TOKEN_SLOT,
    check_template_safe, pct,
};

/// The kinds this module owns (exactly the host policy's moderation kinds).
pub const KINDS: &[&str] = &[
    "chat.delete",
    "mod.ban",
    "mod.unban",
    "mod.add",
    "mod.remove",
];

/// Host-filled slot for the detokenized target channel id.
pub const TARGET_USER_SLOT: &str = "{{YT_TARGET_USER}}";
/// Longest temporary ban accepted (14 days); longer is a typed error, never clamped.
pub const MAX_BAN_SECONDS: u64 = 14 * 24 * 60 * 60;

/// Parses the payload, requiring a JSON object.
fn parse(payload_json: &str) -> Result<Value, LogicError> {
    match serde_json::from_str::<Value>(payload_json) {
        Ok(v @ Value::Object(_)) => Ok(v),
        _ => Err(LogicError::Malformed("payload is not a JSON object".into())),
    }
}

/// A YouTube resource id: `^[A-Za-z0-9_.-]{1,128}$` (message ids look like `LCC.…`).
fn is_resource_id(s: &str) -> bool {
    (1..=128).contains(&s.len())
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'.'))
}

/// A required, template-safe id field.
fn id_field(p: &Value, key: &str) -> Result<String, LogicError> {
    match p.get(key) {
        Some(Value::String(s)) if !s.is_empty() => {
            check_template_safe(key, s)?;
            if !is_resource_id(s) {
                return Err(LogicError::Malformed(format!("{key} is not a valid id")));
            }
            Ok(s.clone())
        }
        _ => Err(LogicError::Malformed(format!("missing string field {key}"))),
    }
}

/// `true` for a canonical UUID, bare or in the `{user:<uuid>}` wrapper.
fn is_user_token(raw: &str) -> bool {
    let inner = raw
        .strip_prefix("{user:")
        .and_then(|r| r.strip_suffix('}'))
        .unwrap_or(raw);
    let b = inner.as_bytes();
    b.len() == 36
        && b.iter().enumerate().all(|(i, c)| match i {
            8 | 13 | 18 | 23 => *c == b'-',
            _ => c.is_ascii_hexdigit(),
        })
}

/// Requires `user_id` to be a tokenized UUID (a raw channel id is refused, never forwarded).
fn require_user_token(p: &Value) -> Result<(), LogicError> {
    match p.get("user_id") {
        Some(Value::String(s)) if is_user_token(s) => Ok(()),
        _ => Err(LogicError::Malformed(
            "user_id must be a tokenized user UUID".into(),
        )),
    }
}

/// Rejects keys the host never hands over (they would be silently ignored otherwise).
fn only_keys(p: &Value, allowed: &[&str]) -> Result<(), LogicError> {
    if let Some(extra) = p
        .as_object()
        .and_then(|o| o.keys().find(|k| !allowed.contains(&k.as_str())))
    {
        return Err(LogicError::Malformed(format!(
            "unexpected payload field {extra:?}"
        )));
    }
    Ok(())
}

/// `DELETE liveChat/<resource>?id=<id>`.
fn delete_tpl(resource: &str, id: &str) -> RequestTpl {
    RequestTpl {
        method: "DELETE".into(),
        url: format!("{API_BASE}/liveChat/{resource}?id={}", pct(id)),
        headers: vec![("Authorization".into(), format!("Bearer {TOKEN_SLOT}"))],
        body: None,
        secret_refs: vec![(TOKEN_SLOT.into(), TOKEN_HANDLE.into())],
    }
}

/// `POST liveChat/<resource>?part=snippet` with the given snippet fields plus the host chat id.
fn insert_tpl(resource: &str, mut snippet: Value) -> RequestTpl {
    snippet["liveChatId"] = Value::String(CHAT_ID_SLOT.to_string());
    RequestTpl {
        method: "POST".into(),
        url: format!("{API_BASE}/liveChat/{resource}?part=snippet"),
        headers: vec![
            ("Authorization".into(), format!("Bearer {TOKEN_SLOT}")),
            ("Content-Type".into(), "application/json".into()),
        ],
        body: Some(json!({ "snippet": snippet }).to_string().into_bytes()),
        secret_refs: vec![
            (TOKEN_SLOT.into(), TOKEN_HANDLE.into()),
            (CHAT_ID_SLOT.into(), CHAT_ID_HANDLE.into()),
        ],
    }
}

/// The optional ban length in whole seconds from `text`; `None` = permanent.
fn ban_seconds(p: &Value) -> Result<Option<u64>, LogicError> {
    match p.get("text") {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) => {
            let n: u64 = s
                .parse()
                .map_err(|_| LogicError::Malformed("ban duration is not whole seconds".into()))?;
            if (1..=MAX_BAN_SECONDS).contains(&n) {
                Ok(Some(n))
            } else {
                Err(LogicError::Malformed(format!(
                    "ban duration out of range 1..={MAX_BAN_SECONDS}"
                )))
            }
        }
        Some(_) => Err(LogicError::Malformed(
            "ban duration must be a decimal string".into(),
        )),
    }
}

/// Builds the request template for moderation `kind`.
///
/// # Errors
/// [`LogicError::Unsupported`] for a kind this module does not own, [`LogicError::Malformed`]
/// for a payload that does not match the kind (missing/invalid id, raw user id, extra key,
/// out-of-range ban length) -- never a default.
pub fn build(kind: &str, payload_json: &str) -> Result<RequestTpl, LogicError> {
    if !KINDS.contains(&kind) {
        return Err(LogicError::Unsupported(format!(
            "unsupported action kind: {kind}"
        )));
    }
    let p = parse(payload_json)?;
    match kind {
        "chat.delete" => {
            only_keys(&p, &["message_id"])?;
            Ok(delete_tpl("messages", &id_field(&p, "message_id")?))
        }
        "mod.unban" => {
            only_keys(&p, &["message_id"])?;
            Ok(delete_tpl("bans", &id_field(&p, "message_id")?))
        }
        "mod.remove" => {
            only_keys(&p, &["message_id"])?;
            Ok(delete_tpl("moderators", &id_field(&p, "message_id")?))
        }
        "mod.ban" => {
            only_keys(&p, &["user_id", "text"])?;
            require_user_token(&p)?;
            let mut snippet = json!({
                "type": "permanent",
                "bannedUserDetails": {"channelId": TARGET_USER_SLOT},
            });
            if let Some(secs) = ban_seconds(&p)? {
                snippet["type"] = json!("temporary");
                snippet["banDurationSeconds"] = json!(secs);
            }
            Ok(insert_tpl("bans", snippet))
        }
        "mod.add" => {
            only_keys(&p, &["user_id"])?;
            require_user_token(&p)?;
            Ok(insert_tpl(
                "moderators",
                json!({"moderatorDetails": {"channelId": TARGET_USER_SLOT}}),
            ))
        }
        _ => Err(LogicError::Unsupported(format!(
            "unsupported action kind: {kind}"
        ))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const USER: &str = "123e4567-e89b-12d3-a456-426614174000";

    fn body(t: &RequestTpl) -> Value {
        serde_json::from_slice(t.body.as_ref().unwrap()).unwrap()
    }

    #[test]
    fn deletes_are_bodyless_with_an_encoded_literal_id() {
        for (kind, res) in [
            ("chat.delete", "messages"),
            ("mod.unban", "bans"),
            ("mod.remove", "moderators"),
        ] {
            let t = build(kind, r#"{"message_id":"abc_-123"}"#).unwrap();
            assert_eq!(t.method, "DELETE");
            assert_eq!(t.url, format!("{API_BASE}/liveChat/{res}?id=abc_-123"));
            let dotted = build(kind, r#"{"message_id":"LCC.Ci8KGkNP-x_1"}"#).unwrap();
            assert!(dotted.url.ends_with("?id=LCC.Ci8KGkNP-x_1"));
            assert!(t.body.is_none());
            assert_eq!(
                t.secret_refs,
                vec![(TOKEN_SLOT.into(), TOKEN_HANDLE.into())]
            );
        }
    }

    #[test]
    fn ids_that_could_break_out_of_the_query_are_refused() {
        for bad in [
            "a&liveChatId=x",
            "a b",
            "a/../b",
            "a%26b",
            "{{X}}",
            "",
            "a\u{200b}b",
        ] {
            let p = json!({ "message_id": bad }).to_string();
            assert!(build("chat.delete", &p).is_err(), "{bad:?}");
        }
        assert!(build("chat.delete", r#"{"message_id":7}"#).is_err());
        assert!(build("chat.delete", r#"{}"#).is_err());
    }

    #[test]
    fn ban_is_permanent_without_a_duration_and_temporary_with_one() {
        let p = json!({"user_id": USER}).to_string();
        let t = build("mod.ban", &p).unwrap();
        let b = body(&t);
        assert_eq!(b["snippet"]["type"], "permanent");
        assert!(b["snippet"].get("banDurationSeconds").is_none());
        assert_eq!(b["snippet"]["liveChatId"], CHAT_ID_SLOT);
        assert_eq!(
            b["snippet"]["bannedUserDetails"]["channelId"],
            TARGET_USER_SLOT
        );
        assert_eq!(t.url, format!("{API_BASE}/liveChat/bans?part=snippet"));
        let p = json!({"user_id": format!("{{user:{USER}}}"), "text": "600"}).to_string();
        let b = body(&build("mod.ban", &p).unwrap());
        assert_eq!(b["snippet"]["type"], "temporary");
        assert_eq!(b["snippet"]["banDurationSeconds"], 600);
    }

    #[test]
    fn bad_durations_are_typed_errors_never_clamped() {
        for bad in ["0", "-1", "abc", "1.5", "1209601", ""] {
            let p = json!({"user_id": USER, "text": bad}).to_string();
            assert!(build("mod.ban", &p).is_err(), "{bad:?}");
        }
        let ok = json!({"user_id": USER, "text": "1209600"}).to_string();
        assert!(build("mod.ban", &ok).is_ok());
    }

    #[test]
    fn the_target_is_never_embedded_and_a_raw_channel_id_is_refused() {
        let t = build("mod.add", &json!({"user_id": USER}).to_string()).unwrap();
        let s = String::from_utf8(t.body.unwrap()).unwrap();
        assert!(!s.contains(USER));
        assert!(s.contains(TARGET_USER_SLOT));
        for raw in ["UCabcdefghijk", "", "{user:nope}"] {
            let p = json!({ "user_id": raw }).to_string();
            assert!(build("mod.add", &p).is_err(), "{raw:?}");
            assert!(build("mod.ban", &p).is_err(), "{raw:?}");
        }
    }

    #[test]
    fn a_chat_id_or_extra_key_in_the_payload_is_refused() {
        for (kind, p) in [
            ("mod.ban", json!({"user_id": USER, "live_chat_id": "x"})),
            ("mod.add", json!({"user_id": USER, "liveChatId": "x"})),
            (
                "chat.delete",
                json!({"message_id": "m", "live_chat_id": "x"}),
            ),
        ] {
            assert!(build(kind, &p.to_string()).is_err(), "{kind}");
        }
    }

    #[test]
    fn unknown_kinds_and_non_object_payloads_fail_loud() {
        assert!(matches!(
            build("mod.nuke", "{}"),
            Err(LogicError::Unsupported(_))
        ));
        assert!(matches!(
            build("chat.send", "{}"),
            Err(LogicError::Unsupported(_))
        ));
        assert!(build("mod.add", "[]").is_err());
        assert!(build("mod.add", "nope").is_err());
    }
}
