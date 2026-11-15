//! Chat event parsing and reply shaping (pure, target-independent).
//!
//! Inbound `chat.message` payloads carry `text`, `channel_id` and, on
//! Twitch, `is_mod` / `is_broadcaster`. A missing role flag is treated as
//! NOT a moderator (fail closed), so platforms that do not supply roles never
//! grant mod powers.

use serde::Deserialize;
use serde_json::json;

/// Chat prefixes this bundle claims (mirrors `consumes.filters.command_prefix`).
pub const COMMANDS: [&str; 5] = ["!sr", "!songrequest", "!music", "!queue", "!dj"];
/// Replies are capped so one line fits every platform's chat limit.
pub const MAX_REPLY_CHARS: usize = 400;

/// The fields of an inbound chat payload the bundle reads.
#[derive(Debug, Deserialize)]
pub struct ChatMsg {
    pub text: String,
    #[serde(default)]
    pub channel_id: Option<String>,
    #[serde(default)]
    pub is_mod: bool,
    #[serde(default)]
    pub is_broadcaster: bool,
}

impl ChatMsg {
    /// Moderator or broadcaster, per the platform's own flags.
    pub fn is_privileged(&self) -> bool {
        self.is_mod || self.is_broadcaster
    }
}

/// Parses a payload, returning `Some` only for a DJ command line.
pub fn parse(payload_json: &str) -> Option<ChatMsg> {
    let msg: ChatMsg = serde_json::from_str(payload_json).ok()?;
    let first = msg.text.split_whitespace().next()?.to_ascii_lowercase();
    COMMANDS.contains(&first.as_str()).then_some(msg)
}

/// Canonical reply payload JSON (`{text, channel_id}`), truncated to the chat cap.
pub fn reply_payload(text: &str, channel_id: Option<&str>) -> String {
    let clipped: String = text.chars().take(MAX_REPLY_CHARS).collect();
    json!({"text": clipped, "channel_id": channel_id}).to_string()
}

/// The `{channel, text}` relay wire shape; `None` when the reply has no channel.
pub fn relay_message(payload_json: &str) -> Result<(String, String), String> {
    #[derive(Deserialize)]
    struct Reply {
        text: String,
        channel_id: Option<String>,
    }
    let r: Reply = serde_json::from_str(payload_json).map_err(|e| e.to_string())?;
    let channel = r
        .channel_id
        .ok_or_else(|| "reply requires a channel_id".to_string())?;
    Ok((
        json!({"channel": channel, "text": r.text}).to_string(),
        channel,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_dj_commands_parse() {
        assert!(parse(r#"{"text":"!sr never gonna","channel_id":"1"}"#).is_some());
        assert!(parse(r#"{"text":"  !MUSIC next"}"#).is_some());
        assert!(parse(r#"{"text":"!srx"}"#).is_none());
        assert!(parse(r#"{"text":"hello !sr"}"#).is_none());
        assert!(parse(r#"{"text":7}"#).is_none());
        assert!(parse("[]").is_none());
    }

    #[test]
    fn missing_role_flags_mean_not_a_moderator() {
        let m = parse(r#"{"text":"!dj"}"#).unwrap();
        assert!(!m.is_privileged());
        let m = parse(r#"{"text":"!dj","is_broadcaster":true}"#).unwrap();
        assert!(m.is_privileged());
    }

    #[test]
    fn replies_are_clipped_and_relay_needs_a_channel() {
        let long = "x".repeat(1000);
        let p = reply_payload(&long, Some("c1"));
        let (wire, ch) = relay_message(&p).unwrap();
        assert_eq!(ch, "c1");
        let v: serde_json::Value = serde_json::from_str(&wire).unwrap();
        assert_eq!(v["text"].as_str().unwrap().len(), MAX_REPLY_CHARS);
        assert!(relay_message(&reply_payload("hi", None)).is_err());
    }
}
