//! Op-based outbound action schema + per-platform sender trait + dispatch
//! (provider-connection-framework Step 0, issue #719).
//!
//! **Wire shape (versioned, backward-compatible).** An outbound queue entry
//! is a JSON object:
//!
//! ```json
//! {"v":1,"op":"chat.send","platform":"twitch","channel":"#c","text":"hi"}
//! {"v":1,"op":"chat.delete","platform":"twitch","channel":"#c","message_id":"m1"}
//! {"v":1,"op":"dm.send","platform":"discord","user_id":"u1","text":"hi"}
//! ```
//!
//! * `v` -- schema version. Absent = the legacy text-only
//!   `{"channel","text"}` shape (an in-flight message queued by a
//!   pre-upgrade `svc_action`), which is parsed as `chat.send`. A `v`
//!   greater than [`SCHEMA_VERSION`] is rejected loudly, never guessed at.
//! * `op` -- the verb; absent defaults to `chat.send` (legacy only), an
//!   unknown verb is [`OutboundParseError::UnknownOp`] (never defaulted).
//! * `platform` -- optional on the wire (legacy omits it); when present it
//!   must equal the queue's own platform or the entry is rejected as
//!   misrouted.
//!
//! Forward compat: unknown extra fields are ignored, so the new
//! `svc_action` producer keeps `channel`/`text` top-level and an *old*
//! consumer still delivers `chat.send` during a rolling upgrade.
//!
//! **Dispatch.** [`route`] sends a parsed [`OutboundAction`] to the named
//! platform's [`PlatformSender`]; an unknown platform or an op the platform
//! has not implemented is [`SenderError::Unsupported`] -- fail loud, never a
//! silent no-op. The Discord and Twitch `chat.delete`/`dm.send`
//! implementations below are deliberate stubs returning `Unsupported`; the
//! follow-up PRs (Discord REST sender, Twitch Helix client) replace them.

use serde::Deserialize;

use crate::outbound::IrcOutbound;

/// Highest queue-envelope schema version this consumer understands.
pub const SCHEMA_VERSION: u32 = 1;

/// A typed, validated outbound action -- the in-memory form of one queue
/// entry. Each variant carries exactly the arguments its op needs.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum OutboundAction {
    /// `chat.send` -- post `text` to `channel`.
    ChatSend { channel: String, text: String },
    /// `chat.delete` -- delete message `message_id` in `channel`.
    ChatDelete { channel: String, message_id: String },
    /// `dm.send` -- send `text` as a direct message to platform user
    /// `user_id`.
    DmSend { user_id: String, text: String },
}

impl OutboundAction {
    /// The wire/permission spelling of this action's op.
    #[must_use]
    pub fn op(&self) -> &'static str {
        match self {
            Self::ChatSend { .. } => "chat.send",
            Self::ChatDelete { .. } => "chat.delete",
            Self::DmSend { .. } => "dm.send",
        }
    }
}

/// Why a queue entry could not be turned into an [`OutboundAction`].
#[derive(Debug, PartialEq, Eq, thiserror::Error)]
pub enum OutboundParseError {
    /// Not valid JSON / wrong field types.
    #[error("malformed outbound payload: {0}")]
    Malformed(String),
    /// `v` newer than [`SCHEMA_VERSION`].
    #[error("unsupported outbound schema version {0} (max {SCHEMA_VERSION})")]
    UnsupportedVersion(u32),
    /// `op` is not a known verb.
    #[error("unknown outbound op {0:?}")]
    UnknownOp(String),
    /// A required argument for `op` is missing or empty.
    #[error("outbound op {op} requires a non-empty {field:?}")]
    MissingField {
        op: &'static str,
        field: &'static str,
    },
    /// `platform` on the entry differs from the queue it was popped from.
    #[error("outbound entry for platform {entry:?} found on the {queue:?} queue")]
    PlatformMismatch { entry: String, queue: String },
}

/// Loose wire form: every field optional so legacy + new shapes share one
/// deserializer; [`parse_outbound`] does the real validation.
#[derive(Debug, Deserialize)]
struct WireEnvelope {
    #[serde(default)]
    v: Option<u32>,
    #[serde(default)]
    op: Option<String>,
    #[serde(default)]
    platform: Option<String>,
    #[serde(default)]
    channel: Option<String>,
    #[serde(default)]
    text: Option<String>,
    #[serde(default)]
    message_id: Option<String>,
    #[serde(default)]
    user_id: Option<String>,
}

fn require(
    value: Option<String>,
    op: &'static str,
    field: &'static str,
) -> Result<String, OutboundParseError> {
    value
        .filter(|s| !s.is_empty())
        .ok_or(OutboundParseError::MissingField { op, field })
}

/// Parses one raw queue entry popped from `queue_platform`'s outbound list
/// into a validated [`OutboundAction`] (see the module doc for the schema
/// and the legacy text-only fallback).
///
/// # Errors
/// [`OutboundParseError`] for malformed JSON, a too-new `v`, an unknown
/// `op`, a missing required argument, or a platform/queue mismatch.
pub fn parse_outbound(
    raw: &str,
    queue_platform: &str,
) -> Result<OutboundAction, OutboundParseError> {
    let wire: WireEnvelope =
        serde_json::from_str(raw).map_err(|e| OutboundParseError::Malformed(e.to_string()))?;
    if let Some(v) = wire.v {
        if v > SCHEMA_VERSION {
            return Err(OutboundParseError::UnsupportedVersion(v));
        }
    }
    if let Some(platform) = &wire.platform {
        if platform != queue_platform {
            return Err(OutboundParseError::PlatformMismatch {
                entry: platform.clone(),
                queue: queue_platform.to_string(),
            });
        }
    }
    // Legacy (no `op`) is chat.send; an explicit unknown op never defaults.
    match wire.op.as_deref().unwrap_or("chat.send") {
        "chat.send" => Ok(OutboundAction::ChatSend {
            channel: require(wire.channel, "chat.send", "channel")?,
            text: require(wire.text, "chat.send", "text")?,
        }),
        "chat.delete" => Ok(OutboundAction::ChatDelete {
            channel: require(wire.channel, "chat.delete", "channel")?,
            message_id: require(wire.message_id, "chat.delete", "message_id")?,
        }),
        "dm.send" => Ok(OutboundAction::DmSend {
            user_id: require(wire.user_id, "dm.send", "user_id")?,
            text: require(wire.text, "dm.send", "text")?,
        }),
        other => Err(OutboundParseError::UnknownOp(other.to_string())),
    }
}

/// A platform sender failure.
#[derive(Debug, thiserror::Error)]
pub enum SenderError {
    /// The platform has no implementation of this op (or the platform is not
    /// registered at all). Loud by design -- never a silent no-op.
    #[error("unsupported outbound op {op:?} for platform {platform:?}")]
    Unsupported { platform: String, op: &'static str },
    /// The platform call was attempted and failed.
    #[error("{platform} {op} failed: {message}")]
    Failed {
        platform: &'static str,
        op: &'static str,
        message: String,
    },
}

/// One platform's outbound adapter. Discord/Twitch (and later Slack/Kick/
/// ...) implement this; [`route`] dispatches a parsed action to it.
/// Deliberately has no default methods -- an adapter states, per op,
/// whether it implements it or returns [`SenderError::Unsupported`].
#[allow(async_fn_in_trait)] // pub trait, service binary only -- see outbound.rs::IrcOutbound's doc
pub trait PlatformSender: Send + Sync {
    /// Lower-case platform id (`"twitch"`), matching the permission-id
    /// `<platform>` and the queue key.
    fn platform(&self) -> &'static str;
    /// `chat.send`.
    async fn send_chat(&self, channel: &str, text: &str) -> Result<(), SenderError>;
    /// `chat.delete`.
    async fn delete_chat(&self, channel: &str, message_id: &str) -> Result<(), SenderError>;
    /// `dm.send`.
    async fn send_dm(&self, user_id: &str, text: &str) -> Result<(), SenderError>;
}

/// Invokes the `PlatformSender` method matching `action`'s op.
///
/// # Errors
/// Whatever the adapter returns, including [`SenderError::Unsupported`].
pub async fn dispatch_to<S: PlatformSender>(
    sender: &S,
    action: &OutboundAction,
) -> Result<(), SenderError> {
    match action {
        OutboundAction::ChatSend { channel, text } => sender.send_chat(channel, text).await,
        OutboundAction::ChatDelete {
            channel,
            message_id,
        } => sender.delete_chat(channel, message_id).await,
        OutboundAction::DmSend { user_id, text } => sender.send_dm(user_id, text).await,
    }
}

/// Routes `action` to the adapter registered for `platform`. A platform with
/// no registered adapter is [`SenderError::Unsupported`] (fail loud).
///
/// # Errors
/// [`SenderError`] from the chosen adapter, or `Unsupported` for an unknown
/// platform.
pub async fn route<T: PlatformSender, D: PlatformSender>(
    platform: &str,
    action: &OutboundAction,
    twitch: &T,
    discord: &D,
) -> Result<(), SenderError> {
    if platform == twitch.platform() {
        dispatch_to(twitch, action).await
    } else if platform == discord.platform() {
        dispatch_to(discord, action).await
    } else {
        Err(SenderError::Unsupported {
            platform: platform.to_string(),
            op: action.op(),
        })
    }
}

/// The Twitch adapter. `chat.send` delegates to the existing IRC sender
/// (behavior unchanged); `chat.delete`/`dm.send` are STUBS returning
/// `Unsupported` until the Twitch Helix client lands (follow-up PR).
pub struct TwitchSender<'a, S: IrcOutbound> {
    irc: &'a S,
}

impl<'a, S: IrcOutbound> TwitchSender<'a, S> {
    /// Wraps the existing IRC chat sender.
    #[must_use]
    pub fn new(irc: &'a S) -> Self {
        Self { irc }
    }
}

impl<S: IrcOutbound> PlatformSender for TwitchSender<'_, S> {
    fn platform(&self) -> &'static str {
        "twitch"
    }

    async fn send_chat(&self, channel: &str, text: &str) -> Result<(), SenderError> {
        self.irc
            .send(channel, text)
            .await
            .map_err(|e| SenderError::Failed {
                platform: "twitch",
                op: "chat.send",
                message: e.to_string(),
            })
    }

    async fn delete_chat(&self, _channel: &str, _message_id: &str) -> Result<(), SenderError> {
        // STUB: needs the Helix `DELETE /moderation/chat` client (follow-up).
        Err(SenderError::Unsupported {
            platform: "twitch".to_string(),
            op: "chat.delete",
        })
    }

    async fn send_dm(&self, _user_id: &str, _text: &str) -> Result<(), SenderError> {
        // STUB: needs the Helix whisper client (follow-up).
        Err(SenderError::Unsupported {
            platform: "twitch".to_string(),
            op: "dm.send",
        })
    }
}

/// The Discord adapter STUB. Discord `chat.send` is sent directly over REST
/// by `svc_action` (not via this queue), and the REST sender that would
/// implement `chat.delete`/`dm.send` here is a follow-up PR -- every op is
/// `Unsupported` until then.
pub struct DiscordSender;

impl PlatformSender for DiscordSender {
    fn platform(&self) -> &'static str {
        "discord"
    }

    async fn send_chat(&self, _channel: &str, _text: &str) -> Result<(), SenderError> {
        Err(SenderError::Unsupported {
            platform: "discord".to_string(),
            op: "chat.send",
        })
    }

    async fn delete_chat(&self, _channel: &str, _message_id: &str) -> Result<(), SenderError> {
        // STUB: Discord REST `DELETE /channels/{id}/messages/{id}` (follow-up).
        Err(SenderError::Unsupported {
            platform: "discord".to_string(),
            op: "chat.delete",
        })
    }

    async fn send_dm(&self, _user_id: &str, _text: &str) -> Result<(), SenderError> {
        // STUB: Discord REST create-DM-channel + post (follow-up).
        Err(SenderError::Unsupported {
            platform: "discord".to_string(),
            op: "dm.send",
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use penguin_connector_twitch::TwitchError;
    use std::sync::Mutex;

    #[derive(Default)]
    struct RecordingIrc {
        calls: Mutex<Vec<(String, String)>>,
    }

    impl IrcOutbound for RecordingIrc {
        async fn send(&self, channel: &str, text: &str) -> Result<(), TwitchError> {
            self.calls
                .lock()
                .unwrap()
                .push((channel.to_string(), text.to_string()));
            Ok(())
        }
    }

    #[test]
    fn legacy_text_only_payload_parses_as_chat_send() {
        let a = parse_outbound(r##"{"channel":"#c","text":"hi"}"##, "twitch").unwrap();
        assert_eq!(
            a,
            OutboundAction::ChatSend {
                channel: "#c".into(),
                text: "hi".into()
            }
        );
    }

    #[test]
    fn versioned_chat_send_with_extra_fields_parses() {
        let raw = r##"{"v":1,"op":"chat.send","platform":"twitch","channel":"#c","text":"hi","future":1}"##;
        assert_eq!(parse_outbound(raw, "twitch").unwrap().op(), "chat.send");
    }

    #[test]
    fn new_ops_parse_with_typed_args() {
        let del = parse_outbound(
            r##"{"v":1,"op":"chat.delete","channel":"#c","message_id":"m1"}"##,
            "twitch",
        )
        .unwrap();
        assert_eq!(
            del,
            OutboundAction::ChatDelete {
                channel: "#c".into(),
                message_id: "m1".into()
            }
        );
        let dm = parse_outbound(
            r#"{"v":1,"op":"dm.send","user_id":"u1","text":"hi"}"#,
            "twitch",
        )
        .unwrap();
        assert_eq!(dm.op(), "dm.send");
    }

    #[test]
    fn unknown_op_fails_loud_never_defaults() {
        let err = parse_outbound(
            r##"{"v":1,"op":"bogus","channel":"#c","text":"hi"}"##,
            "twitch",
        )
        .unwrap_err();
        assert_eq!(err, OutboundParseError::UnknownOp("bogus".into()));
    }

    #[test]
    fn newer_schema_version_is_rejected() {
        let err = parse_outbound(r##"{"v":2,"channel":"#c","text":"hi"}"##, "twitch").unwrap_err();
        assert_eq!(err, OutboundParseError::UnsupportedVersion(2));
    }

    #[test]
    fn missing_or_empty_args_are_rejected() {
        assert!(matches!(
            parse_outbound(r##"{"op":"chat.delete","channel":"#c"}"##, "twitch"),
            Err(OutboundParseError::MissingField {
                op: "chat.delete",
                field: "message_id"
            })
        ));
        assert!(matches!(
            parse_outbound(r#"{"op":"dm.send","user_id":"","text":"x"}"#, "twitch"),
            Err(OutboundParseError::MissingField {
                op: "dm.send",
                field: "user_id"
            })
        ));
        assert!(matches!(
            parse_outbound("not json", "twitch"),
            Err(OutboundParseError::Malformed(_))
        ));
    }

    #[test]
    fn platform_mismatch_is_rejected() {
        let err = parse_outbound(
            r##"{"v":1,"op":"chat.send","platform":"discord","channel":"c","text":"t"}"##,
            "twitch",
        )
        .unwrap_err();
        assert!(matches!(err, OutboundParseError::PlatformMismatch { .. }));
    }

    #[tokio::test]
    async fn chat_send_routes_to_the_twitch_irc_sender() {
        let irc = RecordingIrc::default();
        let twitch = TwitchSender::new(&irc);
        let action = OutboundAction::ChatSend {
            channel: "#c".into(),
            text: "hi".into(),
        };
        route("twitch", &action, &twitch, &DiscordSender)
            .await
            .unwrap();
        assert_eq!(
            irc.calls.lock().unwrap().as_slice(),
            &[("#c".to_string(), "hi".to_string())]
        );
    }

    #[tokio::test]
    async fn new_ops_route_to_the_adapter_and_return_unsupported() {
        let irc = RecordingIrc::default();
        let twitch = TwitchSender::new(&irc);
        let del = OutboundAction::ChatDelete {
            channel: "#c".into(),
            message_id: "m".into(),
        };
        let dm = OutboundAction::DmSend {
            user_id: "u".into(),
            text: "t".into(),
        };
        for (platform, action, op) in [
            ("twitch", &del, "chat.delete"),
            ("twitch", &dm, "dm.send"),
            ("discord", &del, "chat.delete"),
            ("discord", &dm, "dm.send"),
        ] {
            let err = route(platform, action, &twitch, &DiscordSender)
                .await
                .unwrap_err();
            match err {
                SenderError::Unsupported { platform: p, op: o } => {
                    assert_eq!((p.as_str(), o), (platform, op));
                }
                other => panic!("expected Unsupported, got {other}"),
            }
        }
        assert!(
            irc.calls.lock().unwrap().is_empty(),
            "no IRC send for new ops"
        );
    }

    #[tokio::test]
    async fn unknown_platform_is_unsupported_not_a_silent_noop() {
        let irc = RecordingIrc::default();
        let twitch = TwitchSender::new(&irc);
        let action = OutboundAction::ChatSend {
            channel: "c".into(),
            text: "t".into(),
        };
        let err = route("myspace", &action, &twitch, &DiscordSender)
            .await
            .unwrap_err();
        assert!(matches!(err, SenderError::Unsupported { .. }));
    }

    #[tokio::test]
    async fn discord_chat_send_stub_is_unsupported() {
        let err = DiscordSender.send_chat("c", "t").await.unwrap_err();
        assert!(matches!(err, SenderError::Unsupported { .. }));
    }
}
