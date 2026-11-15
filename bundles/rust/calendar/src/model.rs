//! Domain types: events, RSVPs, sync mappings and config. Status/target values are int enum
//! codes (never strings) per the entity-typing rule; timestamps are stored UTC.

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

/// Lifecycle of an event. Numbering follows the calendar spec's SMALLINT enum (1 = draft is
/// reserved and unused in v3.0).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "u8", into = "u8")]
pub enum Status {
    /// Awaiting approval by a community admin/mod.
    Pending,
    /// Approved/visible; eligible for sync.
    Scheduled,
    /// Cancelled by owner or approver.
    Cancelled,
    /// Rejected at approval.
    Rejected,
}

impl From<Status> for u8 {
    fn from(s: Status) -> u8 {
        match s {
            Status::Pending => 2,
            Status::Scheduled => 3,
            Status::Cancelled => 4,
            Status::Rejected => 5,
        }
    }
}

impl TryFrom<u8> for Status {
    type Error = String;
    fn try_from(v: u8) -> Result<Self, String> {
        match v {
            2 => Ok(Status::Pending),
            3 => Ok(Status::Scheduled),
            4 => Ok(Status::Cancelled),
            5 => Ok(Status::Rejected),
            other => Err(format!("unknown status code {other}")),
        }
    }
}

impl Status {
    /// Short display label for chat/admin output.
    pub fn label(self) -> &'static str {
        match self {
            Status::Pending => "pending",
            Status::Scheduled => "scheduled",
            Status::Cancelled => "cancelled",
            Status::Rejected => "rejected",
        }
    }

    /// True for states that can never return to an active state.
    pub fn is_terminal(self) -> bool {
        matches!(self, Status::Cancelled | Status::Rejected)
    }
}

/// One calendar event, stored community-scoped in the bundle kv.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Event {
    /// Community-local numeric id (kv `increment`), the chat-facing handle.
    pub id: u64,
    /// Stable UUID-shaped reference used across boundaries (admin API, idempotency keys).
    pub public_id: String,
    pub title: String,
    pub description: String,
    /// UTC unix seconds.
    pub start: i64,
    /// UTC unix seconds.
    pub end: i64,
    /// Author's IANA zone, used for rendering.
    pub tz: String,
    pub location: Option<String>,
    pub channel: Option<String>,
    pub status: Status,
    /// Creator's community user UUID.
    pub created_by: String,
    pub approved_by: Option<String>,
    pub reject_reason: Option<String>,
    /// Bumped on every mutation; part of every sync idempotency key.
    pub version: u32,
}

/// RSVP answers (int codes on the wire).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "u8", into = "u8")]
pub enum Rsvp {
    Yes,
    No,
    Maybe,
}

impl From<Rsvp> for u8 {
    fn from(r: Rsvp) -> u8 {
        match r {
            Rsvp::Yes => 1,
            Rsvp::No => 2,
            Rsvp::Maybe => 3,
        }
    }
}

impl TryFrom<u8> for Rsvp {
    type Error = String;
    fn try_from(v: u8) -> Result<Self, String> {
        match v {
            1 => Ok(Rsvp::Yes),
            2 => Ok(Rsvp::No),
            3 => Ok(Rsvp::Maybe),
            other => Err(format!("unknown rsvp code {other}")),
        }
    }
}

impl Rsvp {
    /// Parses a user-typed answer.
    pub fn parse(s: &str) -> Option<Rsvp> {
        match s.to_ascii_lowercase().as_str() {
            "yes" | "y" | "going" => Some(Rsvp::Yes),
            "no" | "n" => Some(Rsvp::No),
            "maybe" | "m" => Some(Rsvp::Maybe),
            _ => None,
        }
    }

    /// Display label.
    pub fn label(self) -> &'static str {
        match self {
            Rsvp::Yes => "yes",
            Rsvp::No => "no",
            Rsvp::Maybe => "maybe",
        }
    }
}

/// External sync targets (int codes match the catalog spec: 1 discord, 2 twitch, 3 wix).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(try_from = "u8", into = "u8")]
pub enum Target {
    Discord,
    Twitch,
    Wix,
}

impl Target {
    /// All targets, in code order.
    pub const ALL: [Target; 3] = [Target::Discord, Target::Twitch, Target::Wix];

    /// Lower-case name used in config and chat.
    pub fn name(self) -> &'static str {
        match self {
            Target::Discord => "discord",
            Target::Twitch => "twitch",
            Target::Wix => "wix",
        }
    }

    /// Parses a config/chat target name.
    pub fn parse(s: &str) -> Option<Target> {
        Target::ALL.into_iter().find(|t| t.name() == s)
    }
}

impl From<Target> for u8 {
    fn from(t: Target) -> u8 {
        match t {
            Target::Discord => 1,
            Target::Twitch => 2,
            Target::Wix => 3,
        }
    }
}

impl TryFrom<u8> for Target {
    type Error = String;
    fn try_from(v: u8) -> Result<Self, String> {
        match v {
            1 => Ok(Target::Discord),
            2 => Ok(Target::Twitch),
            3 => Ok(Target::Wix),
            other => Err(format!("unknown target code {other}")),
        }
    }
}

/// Operations pushed to a remote target.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "u8", into = "u8")]
pub enum SyncOp {
    Create,
    Update,
    Cancel,
    Delete,
}

impl SyncOp {
    /// Name used inside idempotency keys and logs.
    pub fn name(self) -> &'static str {
        match self {
            SyncOp::Create => "create",
            SyncOp::Update => "update",
            SyncOp::Cancel => "cancel",
            SyncOp::Delete => "delete",
        }
    }
}

impl From<SyncOp> for u8 {
    fn from(o: SyncOp) -> u8 {
        match o {
            SyncOp::Create => 1,
            SyncOp::Update => 2,
            SyncOp::Cancel => 3,
            SyncOp::Delete => 4,
        }
    }
}

impl TryFrom<u8> for SyncOp {
    type Error = String;
    fn try_from(v: u8) -> Result<Self, String> {
        match v {
            1 => Ok(SyncOp::Create),
            2 => Ok(SyncOp::Update),
            3 => Ok(SyncOp::Cancel),
            4 => Ok(SyncOp::Delete),
            other => Err(format!("unknown sync op code {other}")),
        }
    }
}

/// Progress of the remote mapping.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(try_from = "u8", into = "u8")]
pub enum SyncStatus {
    /// An operation is queued and will be retried on the next touch/reconcile.
    Pending,
    Synced,
    /// Permanent failure; needs an operator (shown in admin views), never auto-retried.
    Error,
}

impl From<SyncStatus> for u8 {
    fn from(s: SyncStatus) -> u8 {
        match s {
            SyncStatus::Pending => 1,
            SyncStatus::Synced => 2,
            SyncStatus::Error => 3,
        }
    }
}

impl TryFrom<u8> for SyncStatus {
    type Error = String;
    fn try_from(v: u8) -> Result<Self, String> {
        match v {
            1 => Ok(SyncStatus::Pending),
            2 => Ok(SyncStatus::Synced),
            3 => Ok(SyncStatus::Error),
            other => Err(format!("unknown sync status code {other}")),
        }
    }
}

/// Per (event, target) mapping to the remote object (replaces legacy `calendar_event_discord_syncs`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SyncRecord {
    pub target: Target,
    pub remote_id: Option<String>,
    /// Last optimistic-concurrency revision the remote reported (Wix); absent for other targets
    /// and on records written before revisions were tracked.
    #[serde(default)]
    pub remote_revision: Option<i64>,
    pub status: SyncStatus,
    pub attempts: u32,
    pub last_error_code: Option<String>,
    /// Hash of the last payload the remote accepted; identical payloads skip the call.
    pub last_synced_hash: Option<String>,
    /// Operation still owed to the remote, if any.
    pub pending_op: Option<SyncOp>,
}

impl SyncRecord {
    /// A fresh mapping with nothing synced yet.
    pub fn new(target: Target) -> Self {
        SyncRecord {
            target,
            remote_id: None,
            remote_revision: None,
            status: SyncStatus::Pending,
            attempts: 0,
            last_error_code: None,
            last_synced_hash: None,
            pending_op: None,
        }
    }
}

/// Effective per-community configuration (hub-resolved `config_json` plus kv overrides).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct Config {
    pub approval_required: bool,
    pub default_tz: String,
    pub default_duration_minutes: u32,
    pub default_location: String,
    pub sync_discord: bool,
    pub sync_twitch: bool,
    pub sync_wix: bool,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            approval_required: false,
            default_tz: "UTC".to_string(),
            default_duration_minutes: 60,
            default_location: "Online".to_string(),
            sync_discord: false,
            sync_twitch: false,
            sync_wix: false,
        }
    }
}

impl Config {
    /// Whether sync to `t` is enabled.
    pub fn sync_enabled(&self, t: Target) -> bool {
        match t {
            Target::Discord => self.sync_discord,
            Target::Twitch => self.sync_twitch,
            Target::Wix => self.sync_wix,
        }
    }

    /// Sets the sync toggle for `t`.
    pub fn set_sync(&mut self, t: Target, on: bool) {
        match t {
            Target::Discord => self.sync_discord = on,
            Target::Twitch => self.sync_twitch = on,
            Target::Wix => self.sync_wix = on,
        }
    }
}

/// Lower-case hex SHA-256 of `input`.
pub fn sha256_hex(input: &[u8]) -> String {
    let digest = Sha256::digest(input);
    digest.iter().map(|b| format!("{b:02x}")).collect()
}

/// Deterministic UUID-shaped (v5-style) reference derived from tenant/community/id. It is a
/// stable cross-boundary handle, not a secret: access control is the host's community scoping.
pub fn derive_public_id(tenant: &str, community: &str, id: u64) -> String {
    let h = sha256_hex(format!("calendar.event|{tenant}|{community}|{id}").as_bytes());
    let b = h.as_bytes();
    let part = |r: std::ops::Range<usize>| std::str::from_utf8(&b[r]).unwrap_or("0");
    // Version nibble 5, variant nibble 8 (RFC 4122 shape).
    format!(
        "{}-{}-5{}-8{}-{}",
        part(0..8),
        part(8..12),
        part(13..16),
        part(17..20),
        part(20..32)
    )
}
