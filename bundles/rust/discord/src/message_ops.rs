//! Discord message ops: `message.edit` and `reaction.add`.
//!
//! Both act ONLY in the host-pinned channel (`{target.channel_id}`, proven against the stamped
//! origin by the executor; a bundle-named channel is not expressible). The producer's text rides
//! the policy `text_key` (`content`): the message body for an edit, the emoji for a reaction.
//! Edits only ever carry `content` + a fixed `allowed_mentions` backstop (the host additionally
//! restricts body keys), and reactions are a body-less `PUT .../@me` with the emoji
//! percent-encoded into the one path segment the policy reserves for it.

use serde_json::json;

use crate::error::{ConnectorError, Result};
use crate::sanitize::guard_text;
use crate::sender::{HttpTpl, TARGET_CHANNEL, content, object, snowflake, str_field, tpl};

/// The kinds this module owns.
pub const KINDS: &[&str] = &["message.edit", "reaction.add"];

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
        && raw.chars().any(|c| !c.is_ascii())
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
        other => Err(ConnectorError::Unsupported(format!(
            "discord {other} is not a message op"
        ))),
    }
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
}
