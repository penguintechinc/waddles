//! Discord message ops: `message.edit`, `reaction.add`, `message.embed` and `webhook.create`.
//!
//! Both act ONLY in the host-pinned channel (`{target.channel_id}`, proven against the stamped
//! origin by the executor; a bundle-named channel is not expressible). The producer's text rides
//! the policy `text_key` (`content`): the message body for an edit, the emoji for a reaction.
//! Edits only ever carry `content` + a fixed `allowed_mentions` backstop (the host additionally
//! restricts body keys), and reactions are a body-less `PUT .../@me` with the emoji
//! percent-encoded into the one path segment the policy reserves for it.

use serde_json::{Map, Value, json};

use crate::error::{ConnectorError, Result};
use crate::sanitize::guard_text;
use crate::sender::{HttpTpl, TARGET_CHANNEL, content, object, snowflake, str_field, tpl};

/// The kinds this module owns.
pub const KINDS: &[&str] = &[
    "message.edit",
    "reaction.add",
    "message.embed",
    "webhook.create",
];

/// Most embeds one message may carry (Discord limit).
const MAX_EMBEDS: usize = 10;
/// Discord's cap on the characters summed across every embed of one message.
const MAX_EMBED_TOTAL_CHARS: usize = 6000;
/// Most fields in one embed (Discord limit).
const MAX_EMBED_FIELDS: usize = 25;
/// Longest webhook name Discord accepts (characters).
const MAX_WEBHOOK_NAME_CHARS: usize = 80;

/// Longest percent-encoded emoji segment the host policy accepts (bytes).
const MAX_EMOJI_ENCODED: usize = 128;

/// Percent-encodes `s` (RFC 3986 unreserved characters stay literal).
fn percent_encode(s: &str) -> String {
    let mut out = String::with_capacity(s.len() * 3);
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'~') {
            out.push(char::from(b));
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

/// A reaction emoji: unicode emoji text (non-ASCII, at most 16 chars; keycaps `#`/`*`/digits
/// are allowed only alongside a non-ASCII part) or a custom emoji `name:snowflake`
/// (`name` = `[A-Za-z0-9_]{2,32}`). Returns the percent-encoded path segment.
fn emoji_segment(raw: &str) -> Result<String> {
    guard_text("emoji", raw, false)?;
    let bad = || ConnectorError::Malformed("`content` must be one emoji or `name:id`".into());
    let custom = raw.split_once(':').is_some_and(|(name, id)| {
        (2..=32).contains(&name.len())
            && name.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')
            && crate::session::is_snowflake(id)
    });
    let unicode = raw.chars().count() <= 16
        && !raw.is_ascii()
        && raw
            .chars()
            .all(|c| !c.is_ascii() || c == '#' || c == '*' || c.is_ascii_digit());
    if !(custom || unicode) {
        return Err(bad());
    }
    let enc = percent_encode(raw);
    if enc.len() > MAX_EMOJI_ENCODED {
        return Err(bad());
    }
    Ok(enc)
}

/// Builds the request template for `kind`.
///
/// # Errors
/// [`ConnectorError::Unsupported`] for a kind outside [`KINDS`]; [`ConnectorError::Malformed`] for
/// a field outside the allow-list (notably any channel/guild), a bad message id, empty/oversized
/// content, or an invalid emoji.
pub fn build(kind: &str, payload: &str) -> Result<HttpTpl> {
    match kind {
        "message.edit" => {
            let m = object(payload, &["message_id", "content"])?;
            let id = snowflake(&m, "message_id")?;
            let body = serde_json::to_vec(&json!({
                "content": content(&m)?,
                "allowed_mentions": { "parse": ["users"] },
            }))
            .map_err(|e| ConnectorError::Backend(e.to_string()))?;
            Ok(tpl(
                "PATCH",
                format!("/channels/{TARGET_CHANNEL}/messages/{id}"),
                Some(body),
            ))
        }
        "reaction.add" => {
            let m = object(payload, &["message_id", "content"])?;
            let id = snowflake(&m, "message_id")?;
            let emoji = emoji_segment(str_field(&m, "content")?)?;
            Ok(tpl(
                "PUT",
                format!("/channels/{TARGET_CHANNEL}/messages/{id}/reactions/{emoji}/@me"),
                None,
            ))
        }
        "message.embed" => {
            let m = object(payload, &["content"])?;
            let body = embed_body(str_field(&m, "content")?)?;
            Ok(tpl(
                "POST",
                format!("/channels/{TARGET_CHANNEL}/messages"),
                Some(body),
            ))
        }
        "webhook.create" => {
            let m = object(payload, &["name"])?;
            let name = str_field(&m, "name")?;
            guard_text("name", name, false)?;
            let chars = name.chars().count();
            if chars == 0 || chars > MAX_WEBHOOK_NAME_CHARS {
                return Err(ConnectorError::Malformed(format!(
                    "webhook `name` must be 1..={MAX_WEBHOOK_NAME_CHARS} characters"
                )));
            }
            let body = serde_json::to_vec(&json!({ "name": name }))
                .map_err(|e| ConnectorError::Backend(e.to_string()))?;
            Ok(tpl(
                "POST",
                format!("/channels/{TARGET_CHANNEL}/webhooks"),
                Some(body),
            ))
        }
        other => Err(ConnectorError::Unsupported(format!(
            "discord {other} is not a message op"
        ))),
    }
}

/// A `Malformed` connector error.
fn malformed(msg: impl Into<String>) -> ConnectorError {
    ConnectorError::Malformed(msg.into())
}

/// A string field of an embed object: bounded and smuggling-guarded; returns its char count.
fn embed_str(o: &Map<String, Value>, key: &str, max: usize) -> Result<usize> {
    let Some(v) = o.get(key) else { return Ok(0) };
    let s = v
        .as_str()
        .ok_or_else(|| malformed(format!("embed `{key}` must be a string")))?;
    guard_text(key, s, true)?;
    let n = s.chars().count();
    if n == 0 || n > max {
        return Err(malformed(format!(
            "embed `{key}` must be 1..={max} characters"
        )));
    }
    Ok(n)
}

/// Rejects any key of `o` outside `allowed`.
fn only(o: &Map<String, Value>, allowed: &[&str], what: &str) -> Result<()> {
    match o.keys().find(|k| !allowed.contains(&k.as_str())) {
        Some(k) => Err(malformed(format!("{what} field `{k}` is not accepted"))),
        None => Ok(()),
    }
}

/// Validates one embed (only title/description/color/timestamp/footer.text/fields; no media,
/// author or url, so the bundle can never make Discord fetch or link an arbitrary URL) and
/// returns the characters it contributes to the message total.
fn check_embed(e: &Value) -> Result<usize> {
    let o = e
        .as_object()
        .ok_or_else(|| malformed("an embed must be an object"))?;
    only(
        o,
        &[
            "title",
            "description",
            "color",
            "timestamp",
            "footer",
            "fields",
        ],
        "embed",
    )?;
    let mut total = embed_str(o, "title", 256)? + embed_str(o, "description", 4096)?;
    if let Some(c) = o.get("color") {
        c.as_u64()
            .filter(|c| *c <= 0x00FF_FFFF)
            .ok_or_else(|| malformed("embed `color` must be an integer 0..=16777215"))?;
    }
    if let Some(t) = o.get("timestamp") {
        let t = t
            .as_str()
            .ok_or_else(|| malformed("embed `timestamp` must be a string"))?;
        if !crate::sender::is_rfc3339(t) {
            return Err(malformed("embed `timestamp` must be RFC 3339"));
        }
    }
    if let Some(f) = o.get("footer") {
        let f = f
            .as_object()
            .ok_or_else(|| malformed("embed `footer` must be an object"))?;
        only(f, &["text"], "embed footer")?;
        total += embed_str(f, "text", 2048)?;
    }
    if let Some(fs) = o.get("fields") {
        let fs = fs
            .as_array()
            .filter(|a| a.len() <= MAX_EMBED_FIELDS)
            .ok_or_else(|| {
                malformed(format!(
                    "embed `fields` must hold at most {MAX_EMBED_FIELDS}"
                ))
            })?;
        for f in fs {
            let f = f
                .as_object()
                .ok_or_else(|| malformed("an embed field must be an object"))?;
            only(f, &["name", "value", "inline"], "embed field")?;
            if !(f.contains_key("name") && f.contains_key("value")) {
                return Err(malformed("an embed field needs `name` and `value`"));
            }
            total += embed_str(f, "name", 256)? + embed_str(f, "value", 1024)?;
            if let Some(i) = f.get("inline") {
                i.as_bool()
                    .ok_or_else(|| malformed("embed field `inline` must be a boolean"))?;
            }
        }
    }
    if total == 0 {
        return Err(malformed("an embed must carry text"));
    }
    Ok(total)
}

/// Builds the `message.embed` request body from the producer's wire text: a JSON object
/// `{content?, embeds}` (the host tool's `text_object`). Mentions are limited to users, like
/// every other message op.
fn embed_body(text: &str) -> Result<Vec<u8>> {
    let v: Value =
        serde_json::from_str(text).map_err(|_| malformed("embed text must be a JSON object"))?;
    let o = v
        .as_object()
        .ok_or_else(|| malformed("embed text must be a JSON object"))?;
    only(o, &["content", "embeds"], "embed message")?;
    let embeds = o
        .get("embeds")
        .and_then(Value::as_array)
        .filter(|a| (1..=MAX_EMBEDS).contains(&a.len()))
        .ok_or_else(|| malformed(format!("`embeds` must hold 1..={MAX_EMBEDS} embeds")))?;
    let mut total = 0;
    for e in embeds {
        total += check_embed(e)?;
    }
    if total > MAX_EMBED_TOTAL_CHARS {
        return Err(malformed(format!(
            "embeds exceed {MAX_EMBED_TOTAL_CHARS} characters"
        )));
    }
    let mut body = json!({
        "embeds": embeds,
        "allowed_mentions": { "parse": ["users"] },
    });
    if let Some(c) = o.get("content") {
        let c = c
            .as_str()
            .ok_or_else(|| malformed("`content` must be a string"))?;
        let m = Map::from_iter([("content".to_string(), Value::from(c))]);
        body["content"] = Value::from(content(&m)?);
    }
    serde_json::to_vec(&body).map_err(|e| ConnectorError::Backend(e.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    const BASE: &str = "https://discord.com/api/v10/channels/{target.channel_id}/messages";

    #[test]
    fn edit_is_channel_pinned_content_only_with_the_mention_backstop() {
        let t = build(
            "message.edit",
            r#"{"message_id":"42","content":"hi @everyone"}"#,
        )
        .unwrap();
        assert_eq!(t.method, "PATCH");
        assert_eq!(t.url, format!("{BASE}/42"));
        let b: Value = serde_json::from_slice(&t.body.unwrap()).unwrap();
        assert_eq!(
            b,
            json!({"content":"hi @everyone","allowed_mentions":{"parse":["users"]}})
        );
        for bad in [
            r#"{"message_id":"42"}"#,
            r#"{"message_id":"42","content":""}"#,
            r#"{"message_id":"../x","content":"a"}"#,
            r#"{"message_id":"42","content":"a","channel_id":"9"}"#,
            r#"{"message_id":"42","content":"a","embeds":[]}"#,
            r#"{"message_id":"42","content":"{{SECRET}}"}"#,
        ] {
            assert!(
                matches!(
                    build("message.edit", bad),
                    Err(ConnectorError::Malformed(_))
                ),
                "{bad}"
            );
        }
        let big = format!(r#"{{"message_id":"42","content":"{}"}}"#, "a".repeat(2001));
        assert!(build("message.edit", &big).is_err());
    }

    #[test]
    fn reaction_percent_encodes_unicode_and_custom_emoji() {
        let t = build("reaction.add", r#"{"message_id":"42","content":"👍"}"#).unwrap();
        assert_eq!(t.method, "PUT");
        assert_eq!(t.url, format!("{BASE}/42/reactions/%F0%9F%91%8D/@me"));
        assert!(t.body.is_none() && t.headers.is_empty());
        let t = build(
            "reaction.add",
            r#"{"message_id":"42","content":"wave_1:123456789"}"#,
        )
        .unwrap();
        assert_eq!(t.url, format!("{BASE}/42/reactions/wave_1%3A123456789/@me"));
    }

    #[test]
    fn reaction_refuses_anything_that_is_not_an_emoji() {
        let too_long = "👍".repeat(17);
        for bad in [
            "",
            "a",
            "abc",
            "../x",
            "👍/../x",
            "x:abc",
            "a:1",
            "name:12 3",
            "{{X}}",
            too_long.as_str(),
        ] {
            let p = serde_json::to_string(&json!({"message_id":"42","content":bad})).unwrap();
            assert!(build("reaction.add", &p).is_err(), "{bad:?}");
        }
        assert!(
            build(
                "reaction.add",
                r#"{"message_id":"42","content":"👍","channel_id":"1"}"#
            )
            .is_err()
        );
        assert!(build("reaction.add", r#"{"content":"👍"}"#).is_err());
    }

    #[test]
    fn unknown_kinds_are_unsupported_never_ok() {
        for k in ["other", "message.delete", ""] {
            assert!(
                matches!(build(k, "{}"), Err(ConnectorError::Unsupported(_))),
                "{k}"
            );
        }
    }

    #[test]
    fn embed_builds_a_channel_pinned_post_with_validated_embeds() {
        let text = r#"{"content":"hi","embeds":[{"title":"t","description":"d","color":255,"fields":[{"name":"n","value":"v","inline":true}],"footer":{"text":"f"},"timestamp":"2026-12-01T19:00:00Z"}]}"#;
        let payload = json!({ "content": text }).to_string();
        let t = build("message.embed", &payload).unwrap();
        assert_eq!((t.method.as_str(), t.url.as_str()), ("POST", BASE));
        let b: Value = serde_json::from_slice(&t.body.unwrap()).unwrap();
        assert_eq!(b["content"], "hi");
        assert_eq!(b["embeds"][0]["title"], "t");
        assert_eq!(b["allowed_mentions"], json!({"parse":["users"]}));
        assert!(b.get("channel_id").is_none());
    }

    #[test]
    fn embed_refuses_every_unvalidated_shape() {
        let wrap = |t: &str| json!({ "content": t }).to_string();
        let long = "x".repeat(4097);
        let eleven = [r#"{"title":"t"}"#; 11].join(",");
        let many_fields = [r#"{"name":"n","value":"v"}"#; 26].join(",");
        let total = vec![format!(r#"{{"description":"{}"}}"#, "y".repeat(4000)); 2].join(",");
        for bad in [
            "nope".to_string(),
            r#"{"embeds":[]}"#.to_string(),
            r#"{"content":"x"}"#.to_string(),
            r#"{"embeds":[{"title":"t"}],"channel_id":"1"}"#.to_string(),
            r#"{"embeds":[{"image":{"url":"https://x"}}]}"#.to_string(),
            r#"{"embeds":[{"author":{"name":"a"}}]}"#.to_string(),
            r#"{"embeds":[{"url":"https://x","title":"t"}]}"#.to_string(),
            r#"{"embeds":[{}]}"#.to_string(),
            r#"{"embeds":["s"]}"#.to_string(),
            r#"{"embeds":[{"title":"t","color":16777216}]}"#.to_string(),
            r#"{"embeds":[{"title":"t","timestamp":"soon"}]}"#.to_string(),
            r#"{"embeds":[{"title":"{{SECRET}}"}]}"#.to_string(),
            format!(r#"{{"embeds":[{{"description":"{long}"}}]}}"#),
            format!(r#"{{"embeds":[{eleven}]}}"#),
            format!(r#"{{"embeds":[{{"fields":[{many_fields}]}}]}}"#),
            format!(r#"{{"embeds":[{total}]}}"#),
            r#"{"embeds":[{"fields":[{"name":"n"}]}]}"#.to_string(),
            r#"{"content":"","embeds":[{"title":"t"}]}"#.to_string(),
        ] {
            assert!(build("message.embed", &wrap(&bad)).is_err(), "{bad}");
        }
        assert!(build("message.embed", r#"{"content":"{}","channel_id":"1"}"#).is_err());
    }

    #[test]
    fn webhook_create_posts_only_a_bounded_name_to_the_pinned_channel() {
        let t = build("webhook.create", r#"{"name":"bridge"}"#).unwrap();
        assert_eq!(t.method, "POST");
        assert_eq!(
            t.url,
            "https://discord.com/api/v10/channels/{target.channel_id}/webhooks"
        );
        let b: Value = serde_json::from_slice(&t.body.unwrap()).unwrap();
        assert_eq!(b, json!({"name":"bridge"}));
        let too_long = json!({"name": "n".repeat(81)}).to_string();
        for bad in [
            r#"{"name":""}"#,
            r#"{}"#,
            r#"{"name":"b","avatar":"x"}"#,
            r#"{"name":"b","channel_id":"1"}"#,
            r#"{"name":"{{x}}"}"#,
            too_long.as_str(),
        ] {
            assert!(build("webhook.create", bad).is_err(), "{bad}");
        }
    }
}
