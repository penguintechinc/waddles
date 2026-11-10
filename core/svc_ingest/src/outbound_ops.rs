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
//! {"v":1,"op":"event.create","platform":"discord","text":"{event json}","origin_channel":"<guild>"}
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
//! * `origin_channel` on `chat.send`/`chat.delete` -- the host-pinned
//!   destination stamped by the producer. The drain refuses (typed
//!   [`SenderError::Rejected`], see [`crate::outbound_pin`]) any entry whose
//!   `channel` differs from it or that lacks it, so the destination is never
//!   bundle-chosen; pre-upgrade un-stamped entries are refused loudly.
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
//! silent no-op. Twitch `chat.delete`/`dm.send` run through the generic executor
//! ([`TwitchSender::with_executor`]); Discord implements all three over
//! `crate::discord_rest`. Every other op is an [`OutboundAction::PolicyOp`] looked up in the
//! sender-exec policy table (`crate::connector::sender_exec::policy`) -- there is no closed
//! op enum to extend.

use serde::Deserialize;

use crate::connector::sender_exec::{
    DutyHook, ExecError, ExecRequest, FlagGate, SenderExecutor, SlotSource, TemplateBuilder,
};
use crate::discord_rest::{DiscordRestClient, DiscordRestError};
use crate::outbound::{IrcOutbound, IrcSendError};
use crate::outbound_creds::CredError;

/// Highest queue-envelope schema version this consumer understands.
pub const SCHEMA_VERSION: u32 = 1;

/// A typed, validated outbound action -- the in-memory form of one queue
/// entry. Each variant carries exactly the arguments its op needs.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum OutboundAction {
    /// `chat.send` -- post `text` to `channel`.
    ChatSend {
        channel: String,
        text: String,
        /// Host-pinned destination stamped by the producer; `channel` must
        /// equal it ([`crate::outbound_pin`]) or the send is refused.
        origin_channel: Option<String>,
    },
    /// `chat.delete` -- delete message `message_id` in `channel`.
    ChatDelete {
        channel: String,
        message_id: String,
        /// Host-pinned destination, as for [`Self::ChatSend`].
        origin_channel: Option<String>,
    },
    /// `dm.send` -- send `text` as a direct message to platform user
    /// `user_id`, who must belong to the community of `origin_channel` (the
    /// channel of the event that triggered the bundle).
    DmSend {
        user_id: String,
        text: String,
        origin_channel: String,
        /// Numeric tenant id from the MAC-bound stamp (`None` = absent); the
        /// executor's user detokenization needs it.
        tenant_id: Option<i64>,
    },
    /// `role.add` / `role.remove` (T6d-2, executor path only) -- grant or
    /// revoke the managed role `role_key` for the (tokenized) user
    /// `user_id`. The producer names a managed-role KEY, never a role
    /// snowflake, and never a guild: the host maps the key through the
    /// community's binding allowlist and pins the guild itself. The key
    /// travels in the wire `text` field so the `pin_sig` MAC covers it.
    RoleOp {
        /// `true` = `role.add`, `false` = `role.remove`.
        add: bool,
        user_id: String,
        role_key: String,
        origin_channel: String,
        /// Numeric tenant id from the MAC-bound stamp, as for [`Self::DmSend`].
        tenant_id: Option<i64>,
    },
    /// `role.set` / `role.list` (executor path only) -- a host-side BULK role op. `set` makes the
    /// user hold exactly `role_keys` among the community's managed-role allowlist (the host reads
    /// the member's current roles, then applies the minimal add/remove ops, each through the
    /// normal pinned `role.add|remove` gates); `list` returns the managed-role keys with their
    /// assignability. Keys only -- never a snowflake, never a guild.
    RoleBulk {
        kind: RoleBulkKind,
        /// Tokenized recipient (`set` only).
        user_id: Option<String>,
        /// Desired managed-role keys (`set` only; sorted, deduplicated, 1..=[`MAX_BULK_KEYS`]).
        role_keys: Vec<String>,
        /// Channel or `guild:` scope exactly as for [`Self::RoleOp`].
        origin_channel: String,
        /// Numeric tenant id from the MAC-bound stamp.
        tenant_id: Option<i64>,
    },
    /// `event.create|update|cancel|delete` (executor path only) -- mirror a
    /// Waddles calendar event onto the community's Discord guild as a
    /// scheduled event. The guild is NEVER named by the producer: the host
    /// pins it from the community's binding. `expect_guild` (the wire
    /// `origin_channel`, MAC-covered) is only an ASSERTION the host compares to
    /// that binding and refuses on mismatch -- it is not a destination.
    EventOp {
        kind: EventKind,
        /// The Discord scheduled-event id (wire `message_id`); `Some` for
        /// update/cancel/delete, `None` for create.
        event_id: Option<String>,
        /// The event field object (wire `text`, a JSON object); `Some` for
        /// create/update, `None` for cancel/delete.
        event_json: Option<String>,
        /// The guild the producer expects the binding to resolve to; `None`
        /// (wire `-`) = no assertion, the host pins from the binding alone.
        expect_guild: Option<String>,
        /// Numeric tenant id from the MAC-bound stamp.
        tenant_id: Option<i64>,
    },
    /// Any other op registered in the sender-exec policy table (moderation, message ops,
    /// EventSub, command registration, interaction replies ...). The op is the table's own
    /// `&'static` spelling, so an op string that is not in the table can never become one
    /// ([`OutboundParseError::UnknownOp`]); the table's `KindPolicy` -- not this type -- decides
    /// the request shape, slots, credential scope and duty.
    PolicyOp(PolicyOp),
}

/// The generic argument bag of an [`OutboundAction::PolicyOp`]. Every field is producer-supplied
/// and MAC-covered by the stamp, and NONE of them is a destination the host trusts: guild,
/// broadcaster, moderator and credential values are filled by the host from bindings, and the
/// channel is re-proven against the stamped `origin_channel` before anything is built.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PolicyOp {
    /// The op spelling, taken from the policy table.
    pub op: &'static str,
    /// Chat/channel the op acts in (re-pinned against `origin_channel`).
    pub channel: Option<String>,
    /// Free text / JSON object the connector shapes into the body (`text_key`).
    pub text: Option<String>,
    /// Target message / subscription / resource id.
    pub message_id: Option<String>,
    /// Target user (tokenized or platform id, per the op's duty).
    pub user_id: Option<String>,
    /// Stamp-proven origin channel (or `guild:` scope) of the triggering event.
    pub origin_channel: Option<String>,
    /// Numeric tenant id from the MAC-bound stamp.
    pub tenant_id: Option<i64>,
}

/// Longest accepted id-like field of a [`PolicyOp`].
const MAX_POLICY_ID_LEN: usize = 256;
/// Longest accepted `text` of a [`PolicyOp`] (command-registration bodies are the largest).
const MAX_POLICY_TEXT_LEN: usize = 262_144;

/// Most role keys one `role.set` may name.
pub const MAX_BULK_KEYS: usize = 16;

/// Which bulk role operation an [`OutboundAction::RoleBulk`] is.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RoleBulkKind {
    /// `role.set`.
    Set,
    /// `role.list`.
    List,
}

/// Which guild scheduled-event operation an [`OutboundAction::EventOp`] is.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EventKind {
    /// `event.create` -- `POST .../scheduled-events`.
    Create,
    /// `event.update` -- `PATCH .../scheduled-events/{id}` (full field set).
    Update,
    /// `event.cancel` -- `PATCH` with `status=CANCELED` (history preserved).
    Cancel,
    /// `event.delete` -- `DELETE .../scheduled-events/{id}`.
    Delete,
}

impl EventKind {
    /// The wire spelling of this op.
    #[must_use]
    pub fn op(self) -> &'static str {
        match self {
            Self::Create => "event.create",
            Self::Update => "event.update",
            Self::Cancel => "event.cancel",
            Self::Delete => "event.delete",
        }
    }
}

impl OutboundAction {
    /// The wire/permission spelling of this action's op.
    #[must_use]
    pub fn op(&self) -> &'static str {
        match self {
            Self::ChatSend { .. } => "chat.send",
            Self::ChatDelete { .. } => "chat.delete",
            Self::DmSend { .. } => "dm.send",
            Self::RoleOp { add: true, .. } => "role.add",
            Self::RoleOp { add: false, .. } => "role.remove",
            Self::EventOp { kind, .. } => kind.op(),
            Self::RoleBulk {
                kind: RoleBulkKind::Set,
                ..
            } => "role.set",
            Self::RoleBulk {
                kind: RoleBulkKind::List,
                ..
            } => "role.list",
            Self::PolicyOp(p) => p.op,
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
    #[serde(default)]
    community_id: Option<i64>,
    #[serde(default)]
    tenant_id: Option<i64>,
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
    /// Numeric `communities.id` whose credential this send uses, stamped by
    /// the producer from the binding-verified scope. `None` when absent or
    /// not a positive id (never defaulted; a per-community drain refuses it).
    pub community_id: Option<i64>,
}

/// `true` for a safe `op_id`: 1..=[`MAX_OP_ID_LEN`] ASCII alphanumerics or
/// `-`, so it can never smuggle a key separator into the ack key.
fn is_valid_op_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= MAX_OP_ID_LEN
        && id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
}

/// The `&'static` spelling of a role op (error payloads are static).
fn role_op_name(op: &str) -> &'static str {
    if op == "role.add" {
        "role.add"
    } else {
        "role.remove"
    }
}

/// The `&'static` spelling of an event op (error payloads are static).
fn event_kind(op: &str) -> Option<EventKind> {
    match op {
        "event.create" => Some(EventKind::Create),
        "event.update" => Some(EventKind::Update),
        "event.cancel" => Some(EventKind::Cancel),
        "event.delete" => Some(EventKind::Delete),
        _ => None,
    }
}

/// Builds the [`OutboundAction::EventOp`] for `kind`, enforcing the field
/// shape per kind: create has text and no event id; update has both; cancel
/// and delete have an event id and no text. A field a kind does not take is
/// refused, never ignored (it is MAC-covered, so it would be a producer bug).
fn parse_event_op(
    kind: EventKind,
    wire: &WireEnvelope,
) -> Result<OutboundAction, OutboundParseError> {
    let op = kind.op();
    let non_empty = |v: &Option<String>| v.clone().filter(|s| !s.is_empty());
    let (event_id, event_json) = (non_empty(&wire.message_id), non_empty(&wire.text));
    let unexpected =
        |field: &'static str| OutboundParseError::Malformed(format!("{op} must not carry {field}"));
    let (event_id, event_json) = match kind {
        EventKind::Create => {
            if event_id.is_some() {
                return Err(unexpected("message_id"));
            }
            (
                None,
                Some(event_json.ok_or(OutboundParseError::MissingField { op, field: "text" })?),
            )
        }
        EventKind::Update => (
            Some(event_id.ok_or(OutboundParseError::MissingField {
                op,
                field: "message_id",
            })?),
            Some(event_json.ok_or(OutboundParseError::MissingField { op, field: "text" })?),
        ),
        EventKind::Cancel | EventKind::Delete => {
            if event_json.is_some() {
                return Err(unexpected("text"));
            }
            (
                Some(event_id.ok_or(OutboundParseError::MissingField {
                    op,
                    field: "message_id",
                })?),
                None,
            )
        }
    };
    Ok(OutboundAction::EventOp {
        kind,
        event_id,
        event_json,
        expect_guild: match require(wire.origin_channel.clone(), op, "origin_channel")?.as_str() {
            "-" => None,
            g if !g.is_empty() && g.len() <= 20 && g.bytes().all(|b| b.is_ascii_digit()) => {
                Some(g.to_string())
            }
            _ => {
                return Err(OutboundParseError::Malformed(format!(
                    "{op} origin_channel must be a guild snowflake or -"
                )))
            }
        },
        tenant_id: wire.tenant_id.filter(|t| *t > 0),
    })
}

/// `[a-z][a-z0-9_-]{0,31}` -- the managed-role KEY charset (a snowflake can never match).
fn is_bulk_role_key(k: &str) -> bool {
    let b = k.as_bytes();
    !b.is_empty()
        && b.len() <= 32
        && b[0].is_ascii_lowercase()
        && b.iter()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == b'_' || *c == b'-')
}

/// Builds [`OutboundAction::RoleBulk`]: `set` carries a user and 1..=[`MAX_BULK_KEYS`] keys
/// (wire `text`, comma-joined, MAC-covered); `list` carries neither (a field a kind does not
/// take is refused, never ignored).
fn parse_role_bulk(
    kind: RoleBulkKind,
    wire: &WireEnvelope,
) -> Result<OutboundAction, OutboundParseError> {
    let op: &'static str = match kind {
        RoleBulkKind::Set => "role.set",
        RoleBulkKind::List => "role.list",
    };
    let non_empty = |v: &Option<String>| v.clone().filter(|s| !s.is_empty());
    let (user_id, text) = (non_empty(&wire.user_id), non_empty(&wire.text));
    let origin_channel = require(wire.origin_channel.clone(), op, "origin_channel")?;
    let (user_id, role_keys) = match kind {
        RoleBulkKind::Set => {
            let user = user_id.ok_or(OutboundParseError::MissingField {
                op,
                field: "user_id",
            })?;
            let text = text.ok_or(OutboundParseError::MissingField { op, field: "text" })?;
            let mut keys: Vec<String> = text.split(',').map(str::to_string).collect();
            keys.sort();
            keys.dedup();
            if keys.len() > MAX_BULK_KEYS || !keys.iter().all(|k| is_bulk_role_key(k)) {
                return Err(OutboundParseError::Malformed(format!(
                    "{op} text must be 1..={MAX_BULK_KEYS} managed-role keys"
                )));
            }
            (Some(user), keys)
        }
        RoleBulkKind::List => {
            if user_id.is_some() || text.is_some() {
                return Err(OutboundParseError::Malformed(format!(
                    "{op} must not carry user_id or text"
                )));
            }
            (None, Vec::new())
        }
    };
    Ok(OutboundAction::RoleBulk {
        kind,
        user_id,
        role_keys,
        origin_channel,
        tenant_id: wire.tenant_id.filter(|t| *t > 0),
    })
}

/// Builds an [`OutboundAction::PolicyOp`], bounding every field (a field over its bound is
/// refused, never truncated).
fn parse_policy_op(
    op: &'static str,
    wire: WireEnvelope,
) -> Result<OutboundAction, OutboundParseError> {
    let non_empty = |v: Option<String>| v.filter(|s| !s.is_empty());
    let bounded = |field: &'static str,
                   v: Option<String>,
                   max: usize|
     -> Result<Option<String>, OutboundParseError> {
        match non_empty(v) {
            Some(s) if s.len() > max || s.contains('\0') => Err(OutboundParseError::Malformed(
                format!("{op} field {field} is out of bounds"),
            )),
            other => Ok(other),
        }
    };
    Ok(OutboundAction::PolicyOp(PolicyOp {
        op,
        channel: bounded("channel", wire.channel, MAX_POLICY_ID_LEN)?,
        text: bounded("text", wire.text, MAX_POLICY_TEXT_LEN)?,
        message_id: bounded("message_id", wire.message_id, MAX_POLICY_ID_LEN)?,
        user_id: bounded("user_id", wire.user_id, MAX_POLICY_ID_LEN)?,
        origin_channel: bounded("origin_channel", wire.origin_channel, MAX_POLICY_ID_LEN)?,
        tenant_id: wire.tenant_id.filter(|t| *t > 0),
    }))
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
    let community_id = wire.community_id.filter(|c| *c > 0);
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
            origin_channel: wire.origin_channel.filter(|s| !s.is_empty()),
        },
        "chat.delete" => OutboundAction::ChatDelete {
            channel: require(wire.channel, "chat.delete", "channel")?,
            message_id: require(wire.message_id, "chat.delete", "message_id")?,
            origin_channel: wire.origin_channel.filter(|s| !s.is_empty()),
        },
        "dm.send" => OutboundAction::DmSend {
            user_id: require(wire.user_id, "dm.send", "user_id")?,
            text: require(wire.text, "dm.send", "text")?,
            origin_channel: require(wire.origin_channel, "dm.send", "origin_channel")?,
            tenant_id: wire.tenant_id.filter(|t| *t > 0),
        },
        op @ ("role.add" | "role.remove") => {
            let name = role_op_name(op);
            OutboundAction::RoleOp {
                add: op == "role.add",
                user_id: require(wire.user_id, name, "user_id")?,
                role_key: require(wire.text, name, "text")?,
                origin_channel: require(wire.origin_channel, name, "origin_channel")?,
                tenant_id: wire.tenant_id.filter(|t| *t > 0),
            }
        }
        "role.set" => parse_role_bulk(RoleBulkKind::Set, &wire)?,
        "role.list" => parse_role_bulk(RoleBulkKind::List, &wire)?,
        other => match event_kind(other) {
            Some(kind) => parse_event_op(kind, &wire)?,
            // Table lookup, not a closed enum: any op registered for THIS queue's platform in
            // the policy table is accepted; everything else is an explicit UnknownOp.
            None => {
                match crate::connector::sender_exec::policy::registered_op(queue_platform, other) {
                    Some(op) => parse_policy_op(op, wire)?,
                    None => return Err(OutboundParseError::UnknownOp(other.to_string())),
                }
            }
        },
    };
    Ok(OutboundEntry {
        action,
        op_id,
        exp_ms,
        community_id,
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

/// Host-pin gate: returns the canonical pinned destination or a loud,
/// typed [`SenderError::Rejected`] (never sends to an unpinned target).
fn enforce_pin(
    platform: &'static str,
    op: &'static str,
    channel: &str,
    origin_channel: &Option<String>,
) -> Result<String, SenderError> {
    crate::outbound_pin::pin_target(platform, channel, origin_channel.as_deref()).map_err(|e| {
        crate::outbound_pin::record_refusal(platform, op, e);
        SenderError::Rejected {
            platform,
            op,
            reason: e.reason(),
        }
    })
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
        OutboundAction::ChatSend {
            channel,
            text,
            origin_channel,
        } => {
            let pinned = enforce_pin(sender.platform(), "chat.send", channel, origin_channel)?;
            sender.send_chat(&pinned, text).await
        }
        OutboundAction::ChatDelete {
            channel,
            message_id,
            origin_channel,
        } => {
            let pinned = enforce_pin(sender.platform(), "chat.delete", channel, origin_channel)?;
            sender.delete_chat(&pinned, message_id).await
        }
        OutboundAction::DmSend {
            user_id,
            text,
            origin_channel,
            ..
        } => sender.send_dm(user_id, text, origin_channel).await,
        // Role and event ops exist only on the generic executor path; a per-platform
        // sender has no such method -- fail loud, never a silent no-op.
        OutboundAction::RoleOp { .. }
        | OutboundAction::RoleBulk { .. }
        | OutboundAction::EventOp { .. }
        | OutboundAction::PolicyOp(_) => Err(SenderError::Unsupported {
            platform: sender.platform().to_string(),
            op: action.op(),
        }),
    }
}

/// Executes one parsed, stamp-verified action through the generic outbound
/// executor (P6a T6d-1) instead of a per-platform [`PlatformSender`]. The
/// drain calls it only AFTER its integrity gate (HMAC + expiry) passed; the
/// implementation re-applies the destination pin before anything else.
#[allow(async_fn_in_trait)] // pub trait, service binary only -- see outbound.rs::IrcOutbound's doc
pub trait ActionExecutor: Send + Sync {
    /// Runs `action` for `platform` on behalf of `community_id`. `Ok(Some(id))`
    /// carries the platform id a create-style op produced (a new Discord
    /// scheduled event's snowflake); every other op is `Ok(None)`.
    ///
    /// # Errors
    /// [`SenderError`] -- `Unsupported` for a kind/platform/host duty the
    /// executor does not implement, `Rejected` for a host-policy refusal
    /// (pin, flag, template, credential), `Failed` for a failed attempt.
    async fn execute_action(
        &self,
        platform: &'static str,
        community_id: Option<i64>,
        action: &OutboundAction,
    ) -> Result<Option<String>, SenderError>;
}

/// The "no executor configured" marker: lets the legacy drain paths be
/// instantiated with `None::<&NoExecutor>`. Calling it fails loud.
#[derive(Clone, Copy, Debug, Default)]
pub struct NoExecutor;

impl ActionExecutor for NoExecutor {
    async fn execute_action(
        &self,
        platform: &'static str,
        _community_id: Option<i64>,
        action: &OutboundAction,
    ) -> Result<Option<String>, SenderError> {
        Err(SenderError::Unsupported {
            platform: platform.to_string(),
            op: action.op(),
        })
    }
}

/// Maps an executor failure onto the drain's three outcomes. The code is
/// the executor's stable, content-free [`ExecError::code`]; no URL, id,
/// credential or message text can reach the producer's ack or the log.
pub(crate) fn exec_to_sender_error(
    platform: &'static str,
    op: &'static str,
    e: &ExecError,
) -> SenderError {
    match e {
        ExecError::UnsupportedPlatform
        | ExecError::UnsupportedKind
        | ExecError::HostDutyMissing => SenderError::Unsupported {
            platform: platform.to_string(),
            op,
        },
        // A scheduled event deleted out of band (Discord 404 on update/cancel/
        // delete): a distinct stable code so the producer can recreate it.
        ExecError::Upstream(404) if op.starts_with("event.") => SenderError::Rejected {
            platform,
            op,
            reason: "upstream_not_found",
        },
        e if e.is_policy_refusal() => SenderError::Rejected {
            platform,
            op,
            reason: e.code(),
        },
        e => SenderError::Failed {
            platform,
            op,
            message: e.code().to_string(),
        },
    }
}

impl<B, S, F, D> ActionExecutor for SenderExecutor<B, S, F, D>
where
    B: TemplateBuilder,
    S: SlotSource,
    F: FlagGate,
    D: DutyHook,
{
    async fn execute_action(
        &self,
        platform: &'static str,
        community_id: Option<i64>,
        action: &OutboundAction,
    ) -> Result<Option<String>, SenderError> {
        // The same pin gate `dispatch_to` applies, so a destination the
        // stamp does not prove is refused (and counted) before the executor
        // builds anything.
        match action {
            OutboundAction::ChatSend {
                channel,
                origin_channel,
                ..
            } => {
                enforce_pin(platform, "chat.send", channel, origin_channel)?;
            }
            OutboundAction::ChatDelete {
                channel,
                origin_channel,
                ..
            } => {
                enforce_pin(platform, "chat.delete", channel, origin_channel)?;
            }
            // A policy op naming a channel is re-pinned against the stamp here (counted);
            // one without a channel is pinned (or refused) by the executor from the kind's
            // own `requires_pin` / stamp-slot declaration.
            OutboundAction::PolicyOp(PolicyOp {
                op,
                channel: Some(channel),
                origin_channel,
                ..
            }) => {
                enforce_pin(platform, op, channel, origin_channel)?;
            }
            OutboundAction::PolicyOp(_)
            | OutboundAction::DmSend { .. }
            | OutboundAction::RoleOp { .. }
            | OutboundAction::EventOp { .. } => {}
            // Composite host-side role ops: planned by the platform duty hook, then every
            // add/remove runs through the normal pinned executor path.
            OutboundAction::RoleBulk { .. } => {
                return self
                    .execute_role_bulk(platform, community_id, action)
                    .await
                    .map_err(|e| exec_to_sender_error(platform, action.op(), &e));
            }
        }
        self.execute(&ExecRequest {
            platform,
            action,
            community_id,
        })
        .await
        .map(|o| o.result_id)
        .map_err(|e| exec_to_sender_error(platform, action.op(), &e))
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

/// The Twitch adapter. `chat.send` delegates to the existing IRC sender (behavior unchanged);
/// `chat.delete` / `dm.send` have no IRC implementation and run through the generic
/// [`ActionExecutor`] (the Helix templates in `sender_exec::policy::twitch`) when one is
/// attached with [`Self::with_executor`]. With no executor attached they fail loud as
/// [`SenderError::Unsupported`] -- there is no silent fallback.
pub struct TwitchSender<'a, S: IrcOutbound, X: ActionExecutor = NoExecutor> {
    irc: &'a S,
    community_id: Option<i64>,
    exec: Option<&'a X>,
}

impl<'a, S: IrcOutbound> TwitchSender<'a, S, NoExecutor> {
    /// Wraps the IRC chat sender for one entry's `community_id` (the
    /// community whose credential the send uses).
    #[must_use]
    pub fn new(irc: &'a S, community_id: Option<i64>) -> Self {
        Self {
            irc,
            community_id,
            exec: None,
        }
    }
}

impl<'a, S: IrcOutbound, X: ActionExecutor> TwitchSender<'a, S, X> {
    /// Attaches the generic executor that backs the ops IRC cannot do. The executor re-applies
    /// the destination pin, the flag gate and the template policy itself.
    #[must_use]
    pub fn with_executor<Y: ActionExecutor>(self, exec: &'a Y) -> TwitchSender<'a, S, Y> {
        TwitchSender {
            irc: self.irc,
            community_id: self.community_id,
            exec: Some(exec),
        }
    }

    /// Runs `action` through the attached executor, or fails loud when none is attached.
    async fn via_executor(&self, action: &OutboundAction) -> Result<(), SenderError> {
        match self.exec {
            Some(x) => x
                .execute_action("twitch", self.community_id, action)
                .await
                .map(|_| ()),
            None => Err(SenderError::Unsupported {
                platform: "twitch".to_string(),
                op: action.op(),
            }),
        }
    }
}

impl<S: IrcOutbound, X: ActionExecutor> PlatformSender for TwitchSender<'_, S, X> {
    fn platform(&self) -> &'static str {
        "twitch"
    }

    async fn send_chat(&self, channel: &str, text: &str) -> Result<(), SenderError> {
        self.irc
            .send(self.community_id, channel, text)
            .await
            .map_err(|e| match e {
                // Typed, content-free refusal: nothing was sent and there is
                // no fallback credential.
                IrcSendError::Credential(c @ CredError::Unavailable) => SenderError::Failed {
                    platform: "twitch",
                    op: "chat.send",
                    message: c.reason().to_string(),
                },
                IrcSendError::Credential(c) => SenderError::Rejected {
                    platform: "twitch",
                    op: "chat.send",
                    reason: c.reason(),
                },
                IrcSendError::Send(e) => SenderError::Failed {
                    platform: "twitch",
                    op: "chat.send",
                    message: e.to_string(),
                },
            })
    }

    async fn delete_chat(&self, channel: &str, message_id: &str) -> Result<(), SenderError> {
        // `channel` was already pinned against the stamp by `dispatch_to`, so it is its own
        // origin here; the executor re-proves it regardless.
        self.via_executor(&OutboundAction::ChatDelete {
            channel: channel.to_string(),
            message_id: message_id.to_string(),
            origin_channel: Some(channel.to_string()),
        })
        .await
    }

    async fn send_dm(
        &self,
        user_id: &str,
        text: &str,
        origin_channel: &str,
    ) -> Result<(), SenderError> {
        self.via_executor(&OutboundAction::DmSend {
            user_id: user_id.to_string(),
            text: text.to_string(),
            origin_channel: origin_channel.to_string(),
            tenant_id: None,
        })
        .await
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
    use std::sync::Mutex;

    #[derive(Default)]
    struct RecordingIrc {
        calls: Mutex<Vec<(String, String)>>,
    }

    impl IrcOutbound for RecordingIrc {
        async fn send(
            &self,
            _community_id: Option<i64>,
            channel: &str,
            text: &str,
        ) -> Result<(), IrcSendError> {
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
                text: "hi".into(),
                origin_channel: None,
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
                message_id: "m1".into(),
                origin_channel: None,
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
                origin_channel: "c1".into(),
                tenant_id: None,
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
        let twitch = TwitchSender::new(&irc, Some(7));
        let action = OutboundAction::ChatSend {
            channel: "#c".into(),
            text: "hi".into(),
            origin_channel: Some("#c".into()),
        };
        route("twitch", &action, &twitch, &DiscordSender::unconfigured())
            .await
            .unwrap();
        assert_eq!(
            irc.calls.lock().unwrap().as_slice(),
            &[("#c".to_string(), "hi".to_string())]
        );
    }

    /// regression: P0 relay-channel pin -- a queued chat.send whose channel
    /// differs from (or lacks) the host-pinned origin is refused with a typed
    /// `Rejected`, and nothing reaches the platform sender.
    #[tokio::test]
    async fn chat_send_to_unpinned_channel_is_refused_and_never_sent() {
        let irc = RecordingIrc::default();
        let twitch = TwitchSender::new(&irc, Some(7));
        let before =
            crate::outbound_pin::refusal_count("twitch", "chat.send", "channel_not_pinned");
        for (channel, origin, reason) in [
            ("#victim", Some("#mine"), "channel_not_pinned"),
            ("#a,#victim", Some("#a"), "invalid_channel"),
            ("#mine", None, "no_origin_pin"),
        ] {
            let action = OutboundAction::ChatSend {
                channel: channel.into(),
                text: "hi".into(),
                origin_channel: origin.map(Into::into),
            };
            let err = route("twitch", &action, &twitch, &DiscordSender::unconfigured())
                .await
                .unwrap_err();
            assert!(
                matches!(err, SenderError::Rejected { reason: r, .. } if r == reason),
                "{channel}: {err}"
            );
        }
        assert!(irc.calls.lock().unwrap().is_empty(), "nothing may be sent");
        assert!(
            crate::outbound_pin::refusal_count("twitch", "chat.send", "channel_not_pinned")
                > before
        );
    }

    #[tokio::test]
    async fn chat_delete_to_unpinned_discord_channel_is_refused() {
        let irc = RecordingIrc::default();
        let twitch = TwitchSender::new(&irc, Some(7));
        let action = OutboundAction::ChatDelete {
            channel: "2".into(),
            message_id: "9".into(),
            origin_channel: Some("1".into()),
        };
        let err = route("discord", &action, &twitch, &DiscordSender::unconfigured())
            .await
            .unwrap_err();
        assert!(matches!(
            err,
            SenderError::Rejected {
                reason: "channel_not_pinned",
                ..
            }
        ));
    }

    #[test]
    fn chat_ops_parse_the_stamped_origin_channel() {
        let a = parse_outbound(
            r##"{"v":1,"op":"chat.send","platform":"twitch","channel":"#c","text":"hi","origin_channel":"#c"}"##,
            "twitch",
        )
        .unwrap();
        assert_eq!(
            a,
            OutboundAction::ChatSend {
                channel: "#c".into(),
                text: "hi".into(),
                origin_channel: Some("#c".into()),
            }
        );
    }

    #[tokio::test]
    async fn new_ops_route_to_the_adapter_and_return_unsupported() {
        let irc = RecordingIrc::default();
        let twitch = TwitchSender::new(&irc, Some(7));
        let del = OutboundAction::ChatDelete {
            channel: "1".into(),
            message_id: "m".into(),
            origin_channel: Some("1".into()),
        };
        let del_tw = OutboundAction::ChatDelete {
            channel: "#c".into(),
            message_id: "m".into(),
            origin_channel: Some("#c".into()),
        };
        let dm = OutboundAction::DmSend {
            user_id: "u".into(),
            text: "t".into(),
            origin_channel: "c".into(),
            tenant_id: None,
        };
        for (platform, action, op) in [
            ("twitch", &del_tw, "chat.delete"),
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
        let twitch = TwitchSender::new(&irc, Some(7));
        let action = OutboundAction::ChatSend {
            channel: "c".into(),
            text: "t".into(),
            origin_channel: None,
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
        let twitch = TwitchSender::new(&irc, Some(7));
        let discord = DiscordSender::new(&client);
        for action in [
            OutboundAction::ChatSend {
                channel: "1".into(),
                text: "hi".into(),
                origin_channel: Some("1".into()),
            },
            OutboundAction::ChatDelete {
                channel: "1".into(),
                message_id: "2".into(),
                origin_channel: Some("1".into()),
            },
            OutboundAction::DmSend {
                user_id: "3".into(),
                text: "hi".into(),
                origin_channel: "1".into(),
                tenant_id: None,
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

    // ---- table-driven policy ops ---------------------------------------------------------

    #[test]
    fn registered_ops_parse_to_policy_ops_and_unknown_ones_are_rejected() {
        let raw = r##"{"v":1,"op":"mod.ban","platform":"twitch","channel":"#c","origin_channel":"#c","user_id":"123","text":"{}","tenant_id":4}"##;
        let OutboundAction::PolicyOp(p) = parse_outbound(raw, "twitch").unwrap() else {
            panic!("a registered op must parse to a PolicyOp");
        };
        assert_eq!(p.op, "mod.ban");
        assert_eq!(p.channel.as_deref(), Some("#c"));
        assert_eq!(p.user_id.as_deref(), Some("123"));
        assert_eq!(p.tenant_id, Some(4));
        // The same spelling is not registered on another platform / is not in the table.
        for (op, queue) in [
            ("mod.kick", "twitch"),
            ("eventsub.create", "youtube"),
            ("mod.ban", "kick"),
            ("mod.nuke", "discord"),
            ("MOD.BAN", "twitch"),
        ] {
            let raw = format!(r#"{{"v":1,"op":"{op}","platform":"{queue}"}}"#);
            assert_eq!(
                parse_outbound(&raw, queue),
                Err(OutboundParseError::UnknownOp(op.to_string())),
                "{queue}/{op}"
            );
        }
    }

    #[test]
    fn policy_op_fields_are_bounded_not_truncated() {
        let long = "x".repeat(MAX_POLICY_ID_LEN + 1);
        for field in ["channel", "message_id", "user_id", "origin_channel"] {
            let raw = format!(r#"{{"op":"mod.ban","{field}":"{long}"}}"#);
            assert!(
                matches!(
                    parse_outbound(&raw, "twitch"),
                    Err(OutboundParseError::Malformed(_))
                ),
                "{field}"
            );
        }
        let nul = r#"{"op":"mod.ban","text":"a\u0000b"}"#;
        assert!(matches!(
            parse_outbound(nul, "twitch"),
            Err(OutboundParseError::Malformed(_))
        ));
    }

    #[tokio::test]
    async fn a_policy_op_never_reaches_a_legacy_platform_sender() {
        let irc = RecordingIrc::default();
        let twitch = TwitchSender::new(&irc, Some(7));
        let action = parse_outbound(
            r##"{"op":"mod.ban","platform":"twitch","channel":"#c","origin_channel":"#c"}"##,
            "twitch",
        )
        .unwrap();
        match dispatch_to(&twitch, &action).await {
            Err(SenderError::Unsupported { platform, op }) => {
                assert_eq!((platform.as_str(), op), ("twitch", "mod.ban"));
            }
            other => panic!("expected Unsupported, got {other:?}"),
        }
    }

    /// Records the actions the executor was handed.
    #[derive(Default)]
    struct RecordingExec {
        seen: std::sync::Mutex<Vec<(Option<i64>, OutboundAction)>>,
    }

    impl ActionExecutor for RecordingExec {
        async fn execute_action(
            &self,
            _platform: &'static str,
            community_id: Option<i64>,
            action: &OutboundAction,
        ) -> Result<Option<String>, SenderError> {
            self.seen
                .lock()
                .unwrap()
                .push((community_id, action.clone()));
            Ok(None)
        }
    }

    /// Twitch `chat.delete` / `dm.send` are wired to the executor (no stub left): with one
    /// attached they reach it, pinned; with none they fail loud.
    #[tokio::test]
    async fn twitch_delete_and_dm_run_through_the_attached_executor() {
        let irc = RecordingIrc::default();
        let exec = RecordingExec::default();
        let twitch = TwitchSender::new(&irc, Some(7)).with_executor(&exec);
        let del = OutboundAction::ChatDelete {
            channel: "#c".into(),
            message_id: "m1".into(),
            origin_channel: Some("#c".into()),
        };
        dispatch_to(&twitch, &del).await.unwrap();
        let dm = OutboundAction::DmSend {
            user_id: "u1".into(),
            text: "hi".into(),
            origin_channel: "#c".into(),
            tenant_id: Some(3),
        };
        dispatch_to(&twitch, &dm).await.unwrap();
        {
            let seen = exec.seen.lock().unwrap();
            assert_eq!(seen.len(), 2);
            assert_eq!(seen[0].0, Some(7));
            assert!(matches!(
                &seen[0].1,
                OutboundAction::ChatDelete { channel, message_id, origin_channel }
                    if channel == "#c" && message_id == "m1" && origin_channel.as_deref() == Some("#c")
            ));
            assert!(
                matches!(&seen[1].1, OutboundAction::DmSend { user_id, .. } if user_id == "u1")
            );
        }
        // The pin is still enforced before the executor is consulted.
        let bad = OutboundAction::ChatDelete {
            channel: "#victim".into(),
            message_id: "m1".into(),
            origin_channel: Some("#c".into()),
        };
        assert!(matches!(
            dispatch_to(&twitch, &bad).await,
            Err(SenderError::Rejected { .. })
        ));
        assert_eq!(exec.seen.lock().unwrap().len(), 2);
    }
}
