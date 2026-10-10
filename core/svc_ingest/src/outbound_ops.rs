//! Op-based outbound action schema + per-platform sender trait + dispatch
//! (provider-connection-framework Step 0, issue #719).
//!
//! **Wire shape (versioned, backward-compatible).** An outbound queue entry
//! is a JSON object:
//!
//! ```json
//! {"v":1,"op":"chat.send","platform":"twitch","channel":"#c","text":"hi"}
//! {"v":1,"op":"chat.delete","platform":"twitch","channel":"#c","message_id":"m1"}
//! {"v":1,"op":"dm.send","platform":"discord","user_id":"u1","text":"hi","origin_channel":"c1"}
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
//! * `origin_channel` -- required for `dm.send`: the channel of the event
//!   that triggered the bundle. The sender binds the DM target to that
//!   channel's community (a bundle names the recipient, so without this a
//!   bundle could DM any user the shared bot reaches).
//! * `op_id` / `exp_ms` -- optional confirmation handshake. When `op_id` is
//!   present the producer is waiting for a result: the drain executes the
//!   op and posts the outcome under that id (see `crate::outbound`). `exp_ms`
//!   is the epoch-millisecond deadline after which the producer has stopped
//!   waiting; the drain drops an entry that is already past it instead of
//!   executing an op the producer has reported as failed.
//!
//! Forward compat: unknown extra fields are ignored, so the new
//! `svc_action` producer keeps `channel`/`text` top-level and an *old*
//! consumer still delivers `chat.send` during a rolling upgrade.
//!
//! **Dispatch.** [`route`] sends a parsed [`OutboundAction`] to the named
//! platform's [`PlatformSender`]; an unknown platform or an op the platform
//! has no sender for is [`SenderError::Unsupported`] -- fail loud, never a
//! silent no-op. The Twitch `chat.delete`/`dm.send` implementations below
//! are still deliberate stubs returning `Unsupported` (Helix client
//! follow-up); Discord implements all three over `crate::discord_rest`.

use serde::Deserialize;

use crate::discord_rest::{DiscordRestClient, DiscordRestError};
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
    /// `user_id`, who must belong to the community of `origin_channel` (the
    /// channel of the event that triggered the bundle).
    DmSend {
        user_id: String,
        text: String,
        origin_channel: String,
    },
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
    #[serde(default)]
    origin_channel: Option<String>,
    #[serde(default)]
    op_id: Option<String>,
    #[serde(default)]
    exp_ms: Option<u64>,
}

/// Longest accepted `op_id` (it becomes part of a Valkey key).
const MAX_OP_ID_LEN: usize = 64;

/// A parsed queue entry: the action plus the optional confirmation
/// handshake fields (see the module doc's `op_id` / `exp_ms`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OutboundEntry {
    /// The validated action to execute.
    pub action: OutboundAction,
    /// Present when the producer is waiting for this op's outcome.
    pub op_id: Option<String>,
    /// Epoch-ms deadline after which the producer stopped waiting.
    pub exp_ms: Option<u64>,
}

/// `true` for a safe `op_id`: 1..=[`MAX_OP_ID_LEN`] ASCII alphanumerics or
/// `-`, so it can never smuggle a key separator into the ack key.
fn is_valid_op_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= MAX_OP_ID_LEN
        && id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
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
    parse_outbound_entry(raw, queue_platform).map(|entry| entry.action)
}

/// [`parse_outbound`] plus the confirmation-handshake fields (`op_id`,
/// `exp_ms`) the Discord drain needs to answer a waiting producer.
///
/// # Errors
/// As [`parse_outbound`], plus [`OutboundParseError::Malformed`] for an
/// `op_id` that is not a safe identifier.
pub fn parse_outbound_entry(
    raw: &str,
    queue_platform: &str,
) -> Result<OutboundEntry, OutboundParseError> {
    let wire: WireEnvelope =
        serde_json::from_str(raw).map_err(|e| OutboundParseError::Malformed(e.to_string()))?;
    if let Some(op_id) = &wire.op_id {
        if !is_valid_op_id(op_id) {
            return Err(OutboundParseError::Malformed(
                "op_id is not a safe identifier".to_string(),
            ));
        }
    }
    let (op_id, exp_ms) = (wire.op_id.clone(), wire.exp_ms);
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
    let action = match wire.op.as_deref().unwrap_or("chat.send") {
        "chat.send" => OutboundAction::ChatSend {
            channel: require(wire.channel, "chat.send", "channel")?,
            text: require(wire.text, "chat.send", "text")?,
        },
        "chat.delete" => OutboundAction::ChatDelete {
            channel: require(wire.channel, "chat.delete", "channel")?,
            message_id: require(wire.message_id, "chat.delete", "message_id")?,
        },
        "dm.send" => OutboundAction::DmSend {
            user_id: require(wire.user_id, "dm.send", "user_id")?,
            text: require(wire.text, "dm.send", "text")?,
            origin_channel: require(wire.origin_channel, "dm.send", "origin_channel")?,
        },
        other => return Err(OutboundParseError::UnknownOp(other.to_string())),
    };
    Ok(OutboundEntry {
        action,
        op_id,
        exp_ms,
    })
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
    /// The sender refused the action on policy grounds before attempting it
    /// (e.g. a DM target outside the triggering community). `reason` is a
    /// stable machine code the producer maps to its own error.
    #[error("{platform} {op} refused: {reason}")]
    Rejected {
        platform: &'static str,
        op: &'static str,
        reason: &'static str,
    },
}

impl SenderError {
    /// The stable code posted back to a waiting producer in the ack
    /// handshake (`crate::outbound`): `unsupported`, `failed`, or the
    /// [`Self::Rejected`] reason. Never contains message content or ids.
    #[must_use]
    pub fn ack_code(&self) -> &'static str {
        match self {
            Self::Unsupported { .. } => "unsupported",
            Self::Failed { .. } => "failed",
            Self::Rejected { reason, .. } => reason,
        }
    }
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
    /// `dm.send`. `origin_channel` is the channel of the event that
    /// triggered the bundle; the adapter must refuse (as
    /// [`SenderError::Rejected`]) a `user_id` outside that channel's
    /// community, or one it cannot verify belongs there.
    async fn send_dm(
        &self,
        user_id: &str,
        text: &str,
        origin_channel: &str,
    ) -> Result<(), SenderError>;
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
        OutboundAction::DmSend {
            user_id,
            text,
            origin_channel,
        } => sender.send_dm(user_id, text, origin_channel).await,
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

    async fn send_dm(
        &self,
        _user_id: &str,
        _text: &str,
        _origin_channel: &str,
    ) -> Result<(), SenderError> {
        // STUB: needs the Helix whisper client (follow-up).
        Err(SenderError::Unsupported {
            platform: "twitch".to_string(),
            op: "dm.send",
        })
    }
}

/// The Discord adapter: implements `chat.send`, `chat.delete` and `dm.send`
/// over the bot-token REST client ([`DiscordRestClient`]). Built with
/// [`Self::unconfigured`] (no bot token) every op is loudly
/// [`SenderError::Unsupported`] -- never a silent no-op.
///
/// Logs carry only platform/op/channel id; never message content, user ids
/// or the token (see `crate::discord_rest`'s PII-free logging note).
pub struct DiscordSender<'a> {
    rest: Option<&'a DiscordRestClient>,
}

impl<'a> DiscordSender<'a> {
    /// Wraps a configured REST client.
    #[must_use]
    pub fn new(rest: &'a DiscordRestClient) -> Self {
        Self { rest: Some(rest) }
    }

    /// A sender with no bot token configured: every op is `Unsupported`.
    #[must_use]
    pub fn unconfigured() -> Self {
        Self { rest: None }
    }

    fn client(&self, op: &'static str) -> Result<&'a DiscordRestClient, SenderError> {
        self.rest.ok_or_else(|| SenderError::Unsupported {
            platform: "discord".to_string(),
            op,
        })
    }
}

/// Maps a REST failure to the loud, content-free [`SenderError::Failed`]; a
/// community-binding refusal is the distinct [`SenderError::Rejected`] so the
/// producer can tell "policy said no" from "Discord failed".
fn discord_failed(op: &'static str, err: &DiscordRestError) -> SenderError {
    if matches!(err, DiscordRestError::NotInCommunity) {
        return SenderError::Rejected {
            platform: "discord",
            op,
            reason: "not_in_community",
        };
    }
    SenderError::Failed {
        platform: "discord",
        op,
        message: err.to_string(),
    }
}

impl PlatformSender for DiscordSender<'_> {
    fn platform(&self) -> &'static str {
        "discord"
    }

    async fn send_chat(&self, channel: &str, text: &str) -> Result<(), SenderError> {
        let rest = self.client("chat.send")?;
        rest.send_message(channel, text)
            .await
            .map_err(|e| discord_failed("chat.send", &e))?;
        tracing::debug!(
            platform = "discord",
            op = "chat.send",
            channel_id = channel,
            "sent"
        );
        Ok(())
    }

    async fn delete_chat(&self, channel: &str, message_id: &str) -> Result<(), SenderError> {
        let rest = self.client("chat.delete")?;
        rest.delete_message(channel, message_id)
            .await
            .map_err(|e| discord_failed("chat.delete", &e))?;
        tracing::info!(
            platform = "discord",
            op = "chat.delete",
            channel_id = channel,
            "deleted message"
        );
        Ok(())
    }

    async fn send_dm(
        &self,
        user_id: &str,
        text: &str,
        origin_channel: &str,
    ) -> Result<(), SenderError> {
        let rest = self.client("dm.send")?;
        rest.send_dm_in_community(origin_channel, user_id, text)
            .await
            .map_err(|e| discord_failed("dm.send", &e))?;
        tracing::info!(platform = "discord", op = "dm.send", "sent direct message");
        Ok(())
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
            r#"{"v":1,"op":"dm.send","user_id":"u1","text":"hi","origin_channel":"c1"}"#,
            "twitch",
        )
        .unwrap();
        assert_eq!(
            dm,
            OutboundAction::DmSend {
                user_id: "u1".into(),
                text: "hi".into(),
                origin_channel: "c1".into()
            }
        );
    }

    /// A `dm.send` without the triggering channel is refused at parse time:
    /// the community binding cannot be enforced without it.
    #[test]
    fn dm_send_requires_origin_channel() {
        assert!(matches!(
            parse_outbound(
                r#"{"v":1,"op":"dm.send","user_id":"u1","text":"hi"}"#,
                "discord"
            ),
            Err(OutboundParseError::MissingField {
                op: "dm.send",
                field: "origin_channel"
            })
        ));
    }

    #[test]
    fn entry_carries_the_confirmation_handshake_fields() {
        let entry = parse_outbound_entry(
            r#"{"v":1,"op":"chat.delete","platform":"discord","channel":"1","message_id":"2","op_id":"abc-123","exp_ms":1700000000000}"#,
            "discord",
        )
        .unwrap();
        assert_eq!(entry.op_id.as_deref(), Some("abc-123"));
        assert_eq!(entry.exp_ms, Some(1_700_000_000_000));
        assert_eq!(entry.action.op(), "chat.delete");
        let legacy = parse_outbound_entry(r##"{"channel":"#c","text":"hi"}"##, "twitch").unwrap();
        assert_eq!((legacy.op_id, legacy.exp_ms), (None, None));
    }

    /// `op_id` becomes part of a Valkey key: anything but a short
    /// alphanumeric/dash identifier is refused (no key-separator smuggling).
    #[test]
    fn unsafe_op_id_is_rejected() {
        for bad in ["", "a:b", "../x", "a b", &"x".repeat(MAX_OP_ID_LEN + 1)] {
            let raw = format!(
                r#"{{"v":1,"op":"chat.delete","channel":"1","message_id":"2","op_id":{}}}"#,
                serde_json::json!(bad)
            );
            assert!(
                matches!(
                    parse_outbound_entry(&raw, "discord"),
                    Err(OutboundParseError::Malformed(_))
                ),
                "{bad:?}"
            );
        }
    }

    #[test]
    fn ack_codes_are_stable_and_content_free() {
        assert_eq!(
            SenderError::Unsupported {
                platform: "x".into(),
                op: "dm.send"
            }
            .ack_code(),
            "unsupported"
        );
        assert_eq!(
            SenderError::Failed {
                platform: "discord",
                op: "dm.send",
                message: "m".into()
            }
            .ack_code(),
            "failed"
        );
        assert_eq!(
            SenderError::Rejected {
                platform: "discord",
                op: "dm.send",
                reason: "not_in_community"
            }
            .ack_code(),
            "not_in_community"
        );
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
        route("twitch", &action, &twitch, &DiscordSender::unconfigured())
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
            origin_channel: "c".into(),
        };
        for (platform, action, op) in [
            ("twitch", &del, "chat.delete"),
            ("twitch", &dm, "dm.send"),
            ("discord", &del, "chat.delete"),
            ("discord", &dm, "dm.send"),
        ] {
            let err = route(platform, action, &twitch, &DiscordSender::unconfigured())
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
        let err = route("myspace", &action, &twitch, &DiscordSender::unconfigured())
            .await
            .unwrap_err();
        assert!(matches!(err, SenderError::Unsupported { .. }));
    }

    #[tokio::test]
    async fn unconfigured_discord_sender_is_unsupported() {
        let err = DiscordSender::unconfigured()
            .send_chat("c", "t")
            .await
            .unwrap_err();
        assert!(matches!(err, SenderError::Unsupported { .. }));
    }

    fn rest(server: &wiremock::MockServer) -> DiscordRestClient {
        DiscordRestClient::new(crate::config::Secret::new("tok"), server.uri()).unwrap()
    }

    /// Mounts the community lookup the DM binding performs: origin channel
    /// `1` belongs to guild `20`, in which user `3` is a member.
    async fn mount_member_of_guild(server: &wiremock::MockServer) {
        use wiremock::matchers::{method, path};
        use wiremock::{Mock, ResponseTemplate};
        Mock::given(method("GET"))
            .and(path("/channels/1"))
            .respond_with(
                ResponseTemplate::new(200)
                    .set_body_json(serde_json::json!({"id":"1","guild_id":"20"})),
            )
            .mount(server)
            .await;
        Mock::given(method("GET"))
            .and(path("/guilds/20/members/3"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({})))
            .mount(server)
            .await;
    }

    /// The three Discord ops route through `PlatformSender` to the REST
    /// client (this is the "no longer Unsupported" proof).
    #[tokio::test]
    async fn discord_ops_route_to_rest_client() {
        use wiremock::matchers::{method, path};
        use wiremock::{Mock, MockServer, ResponseTemplate};
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/channels/1/messages"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"id":"9"})))
            .expect(1)
            .mount(&server)
            .await;
        Mock::given(method("DELETE"))
            .and(path("/channels/1/messages/2"))
            .respond_with(ResponseTemplate::new(204))
            .expect(1)
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .and(path("/users/@me/channels"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"id":"5"})))
            .expect(1)
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .and(path("/channels/5/messages"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"id":"9"})))
            .expect(1)
            .mount(&server)
            .await;
        mount_member_of_guild(&server).await;
        let client = rest(&server);
        let irc = RecordingIrc::default();
        let twitch = TwitchSender::new(&irc);
        let discord = DiscordSender::new(&client);
        for action in [
            OutboundAction::ChatSend {
                channel: "1".into(),
                text: "hi".into(),
            },
            OutboundAction::ChatDelete {
                channel: "1".into(),
                message_id: "2".into(),
            },
            OutboundAction::DmSend {
                user_id: "3".into(),
                text: "hi".into(),
                origin_channel: "1".into(),
            },
        ] {
            route("discord", &action, &twitch, &discord).await.unwrap();
        }
    }

    #[tokio::test]
    async fn discord_cannot_dm_is_loud_failed_without_content() {
        use wiremock::matchers::{method, path};
        use wiremock::{Mock, MockServer, ResponseTemplate};
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/users/@me/channels"))
            .respond_with(ResponseTemplate::new(403))
            .mount(&server)
            .await;
        mount_member_of_guild(&server).await;
        let client = rest(&server);
        let err = DiscordSender::new(&client)
            .send_dm("3", "secret body", "1")
            .await
            .unwrap_err();
        match err {
            SenderError::Failed {
                platform,
                op,
                message,
            } => {
                assert_eq!((platform, op), ("discord", "dm.send"));
                assert!(!message.contains("secret body"));
                assert!(message.contains("cannot be direct-messaged"));
            }
            other => panic!("expected Failed, got {other}"),
        }
    }

    #[tokio::test]
    async fn discord_rest_failures_map_to_failed_for_each_op() {
        use wiremock::{Mock, MockServer, ResponseTemplate};
        let server = MockServer::start().await;
        Mock::given(wiremock::matchers::any())
            .respond_with(ResponseTemplate::new(500))
            .mount(&server)
            .await;
        let client = rest(&server);
        let d = DiscordSender::new(&client);
        assert!(matches!(
            d.send_chat("1", "x").await,
            Err(SenderError::Failed {
                op: "chat.send",
                ..
            })
        ));
        assert!(matches!(
            d.delete_chat("1", "2").await,
            Err(SenderError::Failed {
                op: "chat.delete",
                ..
            })
        ));
        assert!(matches!(
            d.send_dm("1", "x", "1").await,
            Err(SenderError::Failed { op: "dm.send", .. })
        ));
    }

    /// A DM target outside the triggering community is the distinct
    /// `Rejected { not_in_community }` outcome, never a send and never a
    /// generic `Failed`.
    #[tokio::test]
    async fn discord_dm_to_a_non_member_is_rejected_and_never_sent() {
        use wiremock::matchers::{method, path};
        use wiremock::{Mock, MockServer, ResponseTemplate};
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/channels/1"))
            .respond_with(
                ResponseTemplate::new(200)
                    .set_body_json(serde_json::json!({"id":"1","guild_id":"20"})),
            )
            .mount(&server)
            .await;
        Mock::given(method("GET"))
            .and(path("/guilds/20/members/3"))
            .respond_with(
                ResponseTemplate::new(404).set_body_json(serde_json::json!({"code":10007})),
            )
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .and(path("/users/@me/channels"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"id":"5"})))
            .expect(0)
            .mount(&server)
            .await;
        let client = rest(&server);
        let err = DiscordSender::new(&client)
            .send_dm("3", "x", "1")
            .await
            .unwrap_err();
        assert!(
            matches!(
                err,
                SenderError::Rejected {
                    platform: "discord",
                    op: "dm.send",
                    reason: "not_in_community"
                }
            ),
            "{err}"
        );
        assert_eq!(err.ack_code(), "not_in_community");
    }
}
