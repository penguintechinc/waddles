//! Discord Gateway wire spec: opcodes, frame envelope, the IDENTIFY / RESUME /
//! HEARTBEAT bodies, lifecycle classification and close-code classes.
//!
//! This is a faithful port of the wire logic in `penguin-connector-discord`'s
//! `gateway.rs` (the crate the hardcoded `ingest/discord.rs` drives). Sockets,
//! the heartbeat TIMER, ACK-timeout detection and reconnect backoff are HOST
//! concerns (generic WS transport + `ConnectorDriver`); only the bytes and the
//! decisions about bytes live here.

use serde::Deserialize;
use serde_json::{Value, json};

use crate::error::{ConnectorError, Result};
use crate::session::SessionState;

/// Dispatch: an event (`t` names it).
pub const OP_DISPATCH: u8 = 0;
/// Heartbeat (either direction).
pub const OP_HEARTBEAT: u8 = 1;
/// Identify: the login, sent after HELLO.
pub const OP_IDENTIFY: u8 = 2;
/// Resume: replay missed dispatches instead of a fresh IDENTIFY.
pub const OP_RESUME: u8 = 6;
/// Reconnect: the gateway asks the client to reconnect.
pub const OP_RECONNECT: u8 = 7;
/// Invalid session (`d` = resumable flag).
pub const OP_INVALID_SESSION: u8 = 9;
/// Hello: first frame, carries `heartbeat_interval`.
pub const OP_HELLO: u8 = 10;
/// Heartbeat ACK.
pub const OP_HEARTBEAT_ACK: u8 = 11;

/// `GUILDS | GUILD_MEMBERS | GUILD_MESSAGES | MESSAGE_CONTENT`. `GUILD_MEMBERS`
/// (1 << 1) is a PRIVILEGED intent added for inbound role sync
/// (`GUILD_MEMBER_UPDATE`): the "Server Members Intent" toggle MUST be on in the
/// Discord developer portal, or the gateway closes with 4014 (parked).
pub const DEFAULT_INTENTS: u64 = (1 << 0) | (1 << 1) | (1 << 9) | (1 << 15);

/// Slot the host substitutes with the bot token. The host MUST pin the prefix
/// [`TOKEN_PREFIX`] for it (WsConfig `slot_prefixes`); the slot is terminated by
/// the closing `"` of the JSON string.
pub const TOKEN_SLOT: &str = "{{DISCORD_BOT_TOKEN}}";
/// Opaque secret handle the host maps to the bot-token connection.
pub const TOKEN_HANDLE: &str = "discord-bot";
/// The bytes that precede [`TOKEN_SLOT`] in IDENTIFY (op 2); the host anchors
/// the full leading context at frame offset 0.
pub const TOKEN_PREFIX: &str = r#"{"op":2,"d":{"token":""#;
/// RESUME (op 6) counterpart of [`TOKEN_PREFIX`].
pub const TOKEN_PREFIX_RESUME: &str = r#"{"op":6,"d":{"token":""#;

/// The `{op, d, s, t}` envelope every gateway frame uses.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct Frame {
    /// Opcode.
    pub op: u8,
    /// Opcode-specific data.
    #[serde(default)]
    pub d: Value,
    /// Sequence number (dispatch only).
    #[serde(default)]
    pub s: Option<u64>,
    /// Event name (dispatch only).
    #[serde(default)]
    pub t: Option<String>,
}

/// Parses one gateway text frame.
///
/// # Errors
/// [`ConnectorError::Malformed`] when the bytes are not a gateway envelope.
pub fn parse_frame(bytes: &[u8]) -> Result<Frame> {
    serde_json::from_slice(bytes)
        .map_err(|_| ConnectorError::Malformed("not a gateway frame envelope".into()))
}

/// What a frame means for the connection, independent of any event it carries.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Lifecycle {
    /// HELLO: the host's heartbeat timer should run at this period (ms).
    Hello {
        /// `heartbeat_interval` in milliseconds.
        heartbeat_interval_ms: u64,
    },
    /// The gateway asked for an immediate heartbeat (op 1).
    HeartbeatRequest,
    /// The gateway acknowledged our last heartbeat (op 11).
    HeartbeatAck,
    /// Op 7: reconnect (resumable).
    Reconnect,
    /// Op 9: the session is invalid; `resumable` is Discord's own flag.
    InvalidSession {
        /// Whether an immediate RESUME may be attempted.
        resumable: bool,
    },
    /// `READY` dispatch: session state to persist.
    Ready(SessionState),
    /// `RESUMED` dispatch.
    Resumed,
    /// Any other dispatch (events are handled by the normalizer).
    Dispatch,
}

/// Classifies a frame. Mirrors the opcode `match` in
/// `GatewaySession::next_chat_message`.
///
/// # Errors
/// [`ConnectorError::Malformed`] for a HELLO missing `heartbeat_interval`,
/// [`ConnectorError::Unsupported`] for an opcode the gateway never sends a client.
pub fn classify(frame: &Frame) -> Result<Lifecycle> {
    match frame.op {
        OP_HELLO => {
            let ms = frame
                .d
                .get("heartbeat_interval")
                .and_then(Value::as_u64)
                .ok_or_else(|| {
                    ConnectorError::Malformed("HELLO missing heartbeat_interval".into())
                })?;
            Ok(Lifecycle::Hello {
                heartbeat_interval_ms: ms,
            })
        }
        OP_HEARTBEAT => Ok(Lifecycle::HeartbeatRequest),
        OP_HEARTBEAT_ACK => Ok(Lifecycle::HeartbeatAck),
        OP_RECONNECT => Ok(Lifecycle::Reconnect),
        // Never collapse Discord's own flag to a fixed value (same rule as the crate).
        OP_INVALID_SESSION => match frame.d {
            Value::Bool(resumable) => Ok(Lifecycle::InvalidSession { resumable }),
            _ => Err(ConnectorError::Malformed(
                "INVALID_SESSION `d` is not a bool".into(),
            )),
        },
        OP_DISPATCH => match frame.t.as_deref() {
            Some("READY") => Ok(Lifecycle::Ready(SessionState {
                session_id: str_at(&frame.d, &["session_id"]),
                seq: frame.s,
                resume_gateway_url: str_at(&frame.d, &["resume_gateway_url"]),
                self_user_id: str_at(&frame.d, &["user", "id"]),
            })),
            Some("RESUMED") => Ok(Lifecycle::Resumed),
            _ => Ok(Lifecycle::Dispatch),
        },
        other => Err(ConnectorError::Unsupported(format!(
            "gateway opcode {other}"
        ))),
    }
}

fn str_at(v: &Value, path: &[&str]) -> Option<String> {
    let mut cur = v;
    for k in path {
        cur = cur.get(k)?;
    }
    cur.as_str().map(str::to_string)
}

/// Builds the IDENTIFY handshake bytes. The token is NEVER here: the slot is a
/// placeholder the host substitutes after this returns.
///
/// # Errors
/// [`ConnectorError::Backend`] only if JSON serialization fails (it cannot).
pub fn build_identify(intents: u64) -> Result<Vec<u8>> {
    token_first(
        OP_IDENTIFY,
        &json!({
            "intents": intents,
            "properties": {
                "os": "linux",
                "browser": "penguin-connector-discord",
                "device": "penguin-connector-discord",
            },
        }),
    )
}

/// Serializes `{"op":<op>,"d":{"token":"<slot>",<rest...>}}` with the token
/// slot FIRST. `serde_json` sorts map keys, which would bury the slot behind
/// other fields; the host pins an offset-0-anchored prefix
/// (`{"op":N,"d":{"token":"`) so the leading context must be stable.
fn token_first(op: u8, rest: &Value) -> Result<Vec<u8>> {
    let r = serde_json::to_string(rest).map_err(|e| ConnectorError::Backend(e.to_string()))?;
    let inner = r.strip_prefix('{').filter(|i| i.len() > 1).ok_or_else(|| {
        ConnectorError::Backend("handshake fields must be a non-empty object".into())
    })?;
    Ok(format!(r#"{{"op":{op},"d":{{"token":"{TOKEN_SLOT}",{inner}}}"#).into_bytes())
}

/// Builds the RESUME handshake bytes from a persisted session (token slot only).
///
/// # Errors
/// [`ConnectorError::Malformed`] when `session` has no `session_id`.
pub fn build_resume(session: &SessionState) -> Result<Vec<u8>> {
    let id = session
        .session_id
        .as_deref()
        .ok_or_else(|| ConnectorError::Malformed("resume needs a session_id".into()))?;
    token_first(OP_RESUME, &json!({ "session_id": id, "seq": session.seq }))
}

/// Builds a HEARTBEAT body carrying the last-seen sequence (`null` before any).
///
/// # Errors
/// [`ConnectorError::Backend`] only if JSON serialization fails (it cannot).
pub fn build_heartbeat(seq: Option<u64>) -> Result<Vec<u8>> {
    let v = json!({ "op": OP_HEARTBEAT, "d": seq });
    serde_json::to_vec(&v).map_err(|e| ConnectorError::Backend(e.to_string()))
}

/// How a Gateway close code must be handled (Discord's documented table).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CloseCodeClass {
    /// RESUME is possible: 4000-4003, 4005, 4008.
    Resumable,
    /// Reconnect but re-IDENTIFY: 4007, 4009 and any undocumented code.
    ReconnectFresh,
    /// Stop retrying: 4004, 4010-4014.
    Fatal,
}

/// Classifies a close code -- identical mapping to
/// `penguin_connector_discord::gateway::classify_close_code`.
#[must_use]
pub fn classify_close_code(code: u16) -> CloseCodeClass {
    match code {
        4004 | 4010 | 4011 | 4012 | 4013 | 4014 => CloseCodeClass::Fatal,
        4000..=4003 | 4005 | 4008 => CloseCodeClass::Resumable,
        _ => CloseCodeClass::ReconnectFresh,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn frame(raw: &str) -> Frame {
        parse_frame(raw.as_bytes()).unwrap()
    }

    #[test]
    fn identify_has_the_slot_right_after_the_pinned_prefix_and_no_token() {
        let bytes = build_identify(DEFAULT_INTENTS).unwrap();
        let text = String::from_utf8(bytes).unwrap();
        assert!(
            text.starts_with(&format!("{TOKEN_PREFIX}{TOKEN_SLOT}\"")),
            "{text}"
        );
        assert_eq!(text.matches(TOKEN_SLOT).count(), 1);
        let v: Value = serde_json::from_str(&text).unwrap();
        assert_eq!(v["op"], 2);
        assert_eq!(v["d"]["intents"], 33283);
        assert_eq!(v["d"]["properties"]["os"], "linux");
    }

    #[test]
    fn resume_carries_session_and_seq_and_the_slot_only() {
        let s = SessionState {
            session_id: Some("sess9".into()),
            seq: Some(8),
            ..SessionState::default()
        };
        let text = String::from_utf8(build_resume(&s).unwrap()).unwrap();
        assert!(
            text.starts_with(&format!("{TOKEN_PREFIX_RESUME}{TOKEN_SLOT}\"")),
            "{text}"
        );
        let v: Value = serde_json::from_str(&text).unwrap();
        assert_eq!(v["op"], 6);
        assert_eq!(v["d"]["session_id"], "sess9");
        assert_eq!(v["d"]["seq"], 8);
        assert!(build_resume(&SessionState::default()).is_err());
    }

    #[test]
    fn heartbeat_is_null_until_a_sequence_exists() {
        let v: Value = serde_json::from_slice(&build_heartbeat(None).unwrap()).unwrap();
        assert_eq!((v["op"].as_u64(), v["d"].is_null()), (Some(1), true));
        let v: Value = serde_json::from_slice(&build_heartbeat(Some(5)).unwrap()).unwrap();
        assert_eq!(v["d"], 5);
    }

    #[test]
    fn classifies_every_control_opcode() {
        assert_eq!(
            classify(&frame(r#"{"op":10,"d":{"heartbeat_interval":41250}}"#)).unwrap(),
            Lifecycle::Hello {
                heartbeat_interval_ms: 41250
            }
        );
        assert_eq!(
            classify(&frame(r#"{"op":1,"d":null}"#)).unwrap(),
            Lifecycle::HeartbeatRequest
        );
        assert_eq!(
            classify(&frame(r#"{"op":11}"#)).unwrap(),
            Lifecycle::HeartbeatAck
        );
        assert_eq!(
            classify(&frame(r#"{"op":7,"d":null}"#)).unwrap(),
            Lifecycle::Reconnect
        );
        assert_eq!(
            classify(&frame(r#"{"op":9,"d":true}"#)).unwrap(),
            Lifecycle::InvalidSession { resumable: true }
        );
        // Missing/non-bool `d` is a typed error, never a defaulted `false`.
        for raw in [
            r#"{"op":9}"#,
            r#"{"op":9,"d":null}"#,
            r#"{"op":9,"d":"yes"}"#,
        ] {
            assert!(
                matches!(classify(&frame(raw)), Err(ConnectorError::Malformed(_))),
                "{raw}"
            );
        }
        assert_eq!(
            classify(&frame(r#"{"op":0,"s":3,"t":"RESUMED","d":{}}"#)).unwrap(),
            Lifecycle::Resumed
        );
        assert_eq!(
            classify(&frame(r#"{"op":0,"s":3,"t":"GUILD_CREATE","d":{}}"#)).unwrap(),
            Lifecycle::Dispatch
        );
    }

    #[test]
    fn ready_extracts_session_state() {
        let l = classify(&frame(
            r#"{"op":0,"s":1,"t":"READY","d":{"session_id":"abc","resume_gateway_url":"wss://r","user":{"id":"999"}}}"#,
        ))
        .unwrap();
        assert_eq!(
            l,
            Lifecycle::Ready(SessionState {
                session_id: Some("abc".into()),
                seq: Some(1),
                resume_gateway_url: Some("wss://r".into()),
                self_user_id: Some("999".into()),
            })
        );
    }

    #[test]
    fn bad_hello_and_unknown_opcode_fail_loud() {
        assert!(matches!(
            classify(&frame(r#"{"op":10,"d":{}}"#)),
            Err(ConnectorError::Malformed(_))
        ));
        assert!(matches!(
            classify(&frame(r#"{"op":99}"#)),
            Err(ConnectorError::Unsupported(_))
        ));
        assert!(parse_frame(b"nope").is_err());
        assert!(parse_frame(br#"{"d":1}"#).is_err());
    }

    #[test]
    fn close_codes_match_the_documented_table() {
        for c in [4004, 4010, 4011, 4012, 4013, 4014] {
            assert_eq!(classify_close_code(c), CloseCodeClass::Fatal, "{c}");
        }
        for c in [4000, 4001, 4002, 4003, 4005, 4008] {
            assert_eq!(classify_close_code(c), CloseCodeClass::Resumable, "{c}");
        }
        for c in [4007, 4009, 1000, 1001, 4999] {
            assert_eq!(
                classify_close_code(c),
                CloseCodeClass::ReconnectFresh,
                "{c}"
            );
        }
    }
}
