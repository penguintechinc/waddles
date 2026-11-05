//! Twitch IRC: handshake template, line parsing and `PRIVMSG` normalization.
//! Parsing is a byte-exact port of `penguin_connector_twitch::irc::
//! parse_privmsg_line` and `svc_ingest::normalize::normalize_twitch_irc`.
//!
//! The overlay never names a channel, nick or password: the handshake carries
//! opaque slots the host substitutes from its pinned connection config.

use std::collections::HashMap;

use serde_json::{Map, Value, json};

use crate::common::{FrameErr, NormEvent};

/// Slot for the IRC password (host substitutes the token; the `oauth:` prefix
/// is part of the host-pinned slot prefix `PASS oauth:`).
pub const SLOT_PASS: &str = "{{TWITCH_PASS}}";
/// Slot for the bot nick.
pub const SLOT_NICK: &str = "{{TWITCH_NICK}}";
/// Slot for the channel, host-pinned (never overlay-chosen).
pub const SLOT_CHANNEL: &str = "{{TWITCH_CHANNEL}}";
/// Host handle names the slots resolve through.
pub const HANDLE_PASS: &str = "pass";
/// See [`HANDLE_PASS`].
pub const HANDLE_NICK: &str = "nick";
/// See [`HANDLE_PASS`].
pub const HANDLE_CHANNEL: &str = "channel";

/// Host `WsConfig::slot_prefixes` this handshake requires (documented for the
/// integrator; the host rejects a slot not preceded by one of these).
pub const REQUIRED_SLOT_PREFIXES: &[&str] = &["PASS oauth:", "NICK ", "JOIN #"];

/// Heartbeat body: a client `PING` whose `PONG` reply is a data frame that
/// resets the host idle timer.
pub const HEARTBEAT: &[u8] = b"PING :tmi.twitch.tv\r\n";

/// The handshake `(bytes, secret-refs)`. Independent of any session token, so a
/// reconnect re-sends exactly one `JOIN` (IRC `JOIN` is idempotent) and never
/// stacks a second subscription.
pub fn handshake() -> (Vec<u8>, Vec<(String, String)>) {
    let bytes = format!(
        "CAP REQ :twitch.tv/tags\r\nPASS oauth:{SLOT_PASS}\r\nNICK {SLOT_NICK}\r\nJOIN #{SLOT_CHANNEL}\r\n"
    )
    .into_bytes();
    let refs = vec![
        (SLOT_PASS.to_string(), HANDLE_PASS.to_string()),
        (SLOT_NICK.to_string(), HANDLE_NICK.to_string()),
        (SLOT_CHANNEL.to_string(), HANDLE_CHANNEL.to_string()),
    ];
    (bytes, refs)
}

/// One parsed `PRIVMSG`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChatMessage {
    /// Channel as on the wire (`#chan`).
    pub channel: String,
    /// Sender nick.
    pub sender: String,
    /// Message text.
    pub text: String,
    /// Raw tag segment.
    pub tags: Option<String>,
}

/// Parses a `PRIVMSG` line; any other line is `None`.
pub fn parse_privmsg_line(line: &str) -> Option<ChatMessage> {
    let mut rest = line;
    let tags = if let Some(after_at) = rest.strip_prefix('@') {
        let (tag_part, remainder) = after_at.split_once(' ')?;
        rest = remainder;
        Some(tag_part.to_string())
    } else {
        None
    };
    let rest = rest.strip_prefix(':')?;
    let (prefix, remainder) = rest.split_once(' ')?;
    let remainder = remainder.strip_prefix("PRIVMSG ")?;
    let (channel, text) = remainder.split_once(" :")?;
    let sender = prefix.split_once('!').map_or(prefix, |(nick, _)| nick);
    Some(ChatMessage {
        channel: channel.to_string(),
        sender: sender.to_string(),
        text: text.to_string(),
        tags,
    })
}

fn unescape_tag_value(value: &str) -> String {
    if !value.contains('\\') {
        return value.to_string();
    }
    let chars: Vec<char> = value.chars().collect();
    let mut out = String::with_capacity(value.len());
    let mut i = 0;
    while i < chars.len() {
        if chars[i] == '\\' && i + 1 < chars.len() {
            out.push(match chars[i + 1] {
                's' => ' ',
                ':' => ';',
                'r' => '\r',
                'n' => '\n',
                other => other,
            });
            i += 2;
        } else {
            if chars[i] != '\\' {
                out.push(chars[i]);
            }
            i += 1;
        }
    }
    out
}

fn parse_tags(raw: Option<&str>) -> HashMap<String, String> {
    let mut tags = HashMap::new();
    let Some(raw) = raw.filter(|s| !s.is_empty()) else {
        return tags;
    };
    for pair in raw.split(';').filter(|p| !p.is_empty()) {
        match pair.split_once('=') {
            Some((k, v)) => tags.insert(k.to_string(), unescape_tag_value(v)),
            None => tags.insert(pair.to_string(), String::new()),
        };
    }
    tags
}

fn parse_badges(raw: &str) -> Vec<String> {
    raw.split(',')
        .filter_map(|e| {
            let n = e.split_once('/').map_or(e, |(name, _)| name);
            (!n.is_empty()).then(|| n.to_string())
        })
        .collect()
}

/// Normalizes one `PRIVMSG` (payload identical to the hardcoded path).
pub fn normalize_privmsg(msg: &ChatMessage, received_at: &str) -> NormEvent {
    let channel = msg.channel.trim_start_matches('#').to_string();
    let tags = parse_tags(msg.tags.as_deref());
    let badges = parse_badges(tags.get("badges").map_or("", String::as_str));
    let user_id = tags.get("user-id").filter(|s| !s.is_empty()).cloned();
    let non_empty = |k: &str| tags.get(k).filter(|s| !s.is_empty()).cloned();
    let payload: Map<String, Value> = match json!({
        "text": msg.text,
        "channel_name": channel,
        "author": msg.sender,
        "author_id": user_id,
        "user_id": user_id,
        "display_name": non_empty("display-name"),
        "message_id": non_empty("id"),
        "room_id": non_empty("room-id"),
        "is_mod": tags.get("mod").map(String::as_str) == Some("1"),
        "is_subscriber": tags.get("subscriber").map(String::as_str) == Some("1"),
        "is_vip": badges.iter().any(|b| b == "vip"),
        "is_broadcaster": badges.iter().any(|b| b == "broadcaster"),
        "badges": badges,
    }) {
        Value::Object(m) => m,
        _ => Map::new(),
    };
    NormEvent {
        event_type: "message".to_string(),
        actor_key: Some(match &user_id {
            Some(id) => id.clone(),
            None => msg.sender.clone(),
        }),
        payload,
        occurred_at: received_at.to_string(),
    }
}

/// Normalizes a raw frame (one or more CRLF/LF-separated IRC lines).
///
/// `self_nick`, when known, drops the bot's own echoed `PRIVMSG` (parity with
/// the hardcoded path). The `connector` WIT gives the guest no self identity,
/// so the wasm entry point passes `None`; see README "Known gaps".
///
/// `PING`/numerics/`JOIN` acks yield nothing. `RECONNECT` and a failed login
/// `NOTICE` fail loud so the host reacts instead of silently idling.
pub fn normalize_frame(
    bytes: &[u8],
    received_at: &str,
    self_nick: Option<&str>,
) -> Result<Vec<NormEvent>, FrameErr> {
    let text = std::str::from_utf8(bytes)
        .map_err(|_| FrameErr::Malformed("irc frame is not UTF-8".to_string()))?;
    let mut out = Vec::new();
    for line in text.split('\n') {
        let line = line.trim_end_matches('\r');
        if line.is_empty() {
            continue;
        }
        if let Some(msg) = parse_privmsg_line(line) {
            if self_nick.is_some_and(|n| msg.sender.eq_ignore_ascii_case(n)) {
                continue;
            }
            out.push(normalize_privmsg(&msg, received_at));
        } else if line.contains(" RECONNECT") && line.starts_with(':') {
            return Err(FrameErr::Unsupported(
                "irc server requested reconnect; host must reconnect".to_string(),
            ));
        } else if line.contains(" NOTICE ")
            && (line.contains("Login authentication failed")
                || line.contains("Improperly formatted auth")
                || line.contains("Login unsuccessful"))
        {
            return Err(FrameErr::Backend("irc login rejected".to_string()));
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    const TS: &str = "2026-11-05T00:00:00.000Z";

    #[test]
    fn handshake_is_session_independent_single_join_and_slot_only() {
        let (bytes, refs) = handshake();
        let s = String::from_utf8(bytes).unwrap();
        assert_eq!(s.matches("JOIN").count(), 1);
        assert_eq!(s.matches("CAP REQ").count(), 1);
        assert_eq!(refs.len(), 3);
        for (slot, _) in &refs {
            assert!(s.contains(slot.as_str()));
        }
        // No literal credential/channel/nick anywhere outside slots.
        let stripped = refs
            .iter()
            .fold(s, |a, (slot, _)| a.replace(slot.as_str(), ""));
        assert_eq!(
            stripped,
            "CAP REQ :twitch.tv/tags\r\nPASS oauth:\r\nNICK \r\nJOIN #\r\n"
        );
        // Every slot is preceded by a required host prefix.
        let (b2, _) = handshake();
        let s2 = String::from_utf8(b2).unwrap();
        for p in REQUIRED_SLOT_PREFIXES {
            assert!(s2.contains(&format!("{p}{{{{")));
        }
    }

    #[test]
    fn ping_and_numerics_yield_nothing() {
        let f =
            b"PING :tmi.twitch.tv\r\n:tmi.twitch.tv 001 bot :Welcome\r\n:bot!bot@bot JOIN #c\r\n";
        assert!(normalize_frame(f, TS, None).unwrap().is_empty());
    }

    #[test]
    fn multi_line_frame_yields_each_privmsg() {
        let f = b":a!a@a PRIVMSG #c :one\r\nPING :x\r\n:b!b@b PRIVMSG #c :two\r\n";
        let ev = normalize_frame(f, TS, None).unwrap();
        assert_eq!(ev.len(), 2);
        assert_eq!(ev[1].payload["text"], "two");
    }

    #[test]
    fn self_message_dropped_case_insensitively() {
        let f = b":WaddleBot!w@w PRIVMSG #c :echo";
        assert!(
            normalize_frame(f, TS, Some("waddlebot"))
                .unwrap()
                .is_empty()
        );
        assert_eq!(normalize_frame(f, TS, None).unwrap().len(), 1);
    }

    #[test]
    fn reconnect_and_login_failure_fail_loud() {
        assert!(matches!(
            normalize_frame(b":tmi.twitch.tv RECONNECT\r\n", TS, None),
            Err(FrameErr::Unsupported(_))
        ));
        assert!(matches!(
            normalize_frame(
                b":tmi.twitch.tv NOTICE * :Login authentication failed\r\n",
                TS,
                None
            ),
            Err(FrameErr::Backend(_))
        ));
        assert!(matches!(
            normalize_frame(&[0xff, 0xfe], TS, None),
            Err(FrameErr::Malformed(_))
        ));
    }

    #[test]
    fn tag_edge_cases() {
        assert_eq!(
            unescape_tag_value(r"a\sb\:c\\d\re\nf\xg"),
            "a b;c\\d\re\nfxg"
        );
        assert_eq!(unescape_tag_value("end\\"), "end");
        assert!(parse_tags(None).is_empty());
        let t = parse_tags(Some("mod;;a=b"));
        assert_eq!(t["mod"], "");
        assert_eq!(t["a"], "b");
        assert!(parse_privmsg_line("garbage").is_none());
        assert!(parse_privmsg_line("@nospace").is_none());
    }
}

/// Self nick the guest passes to [`normalize_frame`]. `None` until the host
/// self-filter (or a `self-nick` in `connection-ctx`) lands; enabling the
/// overlay REQUIRES that host-side filter.
pub const GUEST_SELF_NICK: Option<&str> = None;

#[cfg(test)]
mod gap_tests {
    use super::*;

    /// Pins the open self-echo gap: with no guest self identity the bot's own
    /// echoed PRIVMSG is still published. When the host filter / ctx self-nick
    /// lands and `GUEST_SELF_NICK` becomes `Some`, THIS TEST FAILS on purpose:
    /// flip it to assert the echo is dropped and update the README.
    #[test]
    fn self_echo_gap_is_pinned_until_host_filter_lands() {
        let echo = b":WaddleBot!w@w PRIVMSG #c :echo of our own reply";
        let got = normalize_frame(echo, "2026-11-05T00:00:00.000Z", GUEST_SELF_NICK).unwrap();
        assert_eq!(GUEST_SELF_NICK, None);
        assert_eq!(got.len(), 1, "gap closed? update this test + README");
    }
}
