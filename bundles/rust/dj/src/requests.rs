//! Typed client for the shared queue service `waddles.core.service.music-requests`
//! (contract: `docs/music-requests-service.md`).
//!
//! Tools used: `requests.list`, `requests.add` (kind `dj`), `requests.advance_due`,
//! `requests.import` (one-shot legacy migration), `history.list` (read-only, no-replay),
//! `dj.get`, `dj.set`. Scopes are
//! enforced by the HOST from the invoking actor; this client never sends an actor.
//! Items carry no requester, and nothing here ever asks for one.

use serde::Deserialize;
use serde_json::{Value, json};

use crate::host::{Host, Level, ToolErr};
use crate::model::{Track, clean_text};

/// Service-bundle app id of the shared queue.
pub const TARGET: &str = "waddles.core.service.music-requests";

/// Failure talking to the queue service.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum ReqError {
    /// The host refused the call (no grant, or missing scope).
    #[error("not granted")]
    NotGranted,
    /// Timeout / target down / pool exhausted.
    #[error("unavailable")]
    Unavailable,
    /// The service answered with an error code (`queue_full`, `duplicate`, ...).
    #[error("refused: {0}")]
    Rejected(String),
    /// The reply did not match the contract.
    #[error("bad response")]
    BadResponse,
}

impl ReqError {
    /// Short chat-safe explanation (never includes raw service text beyond a clean code).
    pub fn user_msg(&self) -> String {
        match self {
            ReqError::NotGranted => {
                "the DJ is not allowed to use the music queue (an admin must grant it)".into()
            }
            ReqError::Unavailable => "the music queue is unavailable right now".into(),
            ReqError::Rejected(c) => format!("the music queue refused that ({c})"),
            ReqError::BadResponse => "the music queue sent an unexpected reply".into(),
        }
    }
}

/// One queue entry as the service returns it. NEVER carries a requester.
#[derive(Clone, Debug, Deserialize)]
pub struct QItem {
    pub id: u64,
    pub title: String,
    #[serde(default)]
    pub artist: String,
    #[serde(default)]
    pub source_url: Option<String>,
    #[serde(default)]
    pub duration_ms: u64,
    /// `viewer` | `dj` | `imported`.
    #[serde(default)]
    pub kind: String,
}

/// `requests.list` reply.
#[derive(Clone, Debug, Deserialize)]
pub struct Snapshot {
    #[serde(default)]
    pub now_playing: Option<QItem>,
    #[serde(default)]
    pub queue: Vec<QItem>,
    #[serde(default)]
    pub ai_dj_on: bool,
    /// The service's single DMCA-safe flag (per community). Kept raw so a missing or
    /// malformed value is NOT parsed as `false`: see [`Snapshot::safe_flag`].
    #[serde(default)]
    pub dmca_safe: Value,
}

impl Snapshot {
    /// `Some(b)` only for an explicit boolean; `None` (missing/unknown/garbage) must be
    /// treated as safe-mode ON by callers (fail closed).
    pub fn safe_flag(&self) -> Option<bool> {
        self.dmca_safe.as_bool()
    }
}

fn call(host: &dyn Host, tool: &str, args: Value, idem: Option<&str>) -> Result<Value, ReqError> {
    host.invoke(TARGET, tool, &args, idem).map_err(|e| {
        host.log(
            Level::Warn,
            "music-requests tool failed",
            json!({"tool": tool, "error": e.to_string()}),
        );
        match e {
            ToolErr::NotGranted | ToolErr::Forbidden => ReqError::NotGranted,
            ToolErr::InvalidArgs(m) => ReqError::Rejected(clean_text(&m, 40)),
            _ => ReqError::Unavailable,
        }
    })
}

/// `dj.get`: the single stored AI DJ setting.
pub fn dj_get(host: &dyn Host) -> Result<bool, ReqError> {
    let v = call(host, "dj.get", json!({}), None)?;
    v.get("enabled")
        .and_then(Value::as_bool)
        .ok_or(ReqError::BadResponse)
}

/// `dj.set` (scope `music:dj.manage`, host-enforced): flips the shared setting.
pub fn dj_set(host: &dyn Host, enabled: bool, idem: &str) -> Result<bool, ReqError> {
    let v = call(host, "dj.set", json!({"enabled": enabled}), Some(idem))?;
    v.get("enabled")
        .and_then(Value::as_bool)
        .ok_or(ReqError::BadResponse)
}

/// `requests.advance_due`: idempotent, advances only when the playing track has elapsed.
pub fn advance_due(host: &dyn Host) -> Result<(), ReqError> {
    call(host, "requests.advance_due", json!({}), None).map(|_| ())
}

/// `requests.list`: now playing + queued items (limit 1..100).
pub fn list(host: &dyn Host, limit: u32) -> Result<Snapshot, ReqError> {
    let v = call(
        host,
        "requests.list",
        json!({"limit": limit.clamp(1, 100)}),
        None,
    )?;
    serde_json::from_value(v).map_err(|_| ReqError::BadResponse)
}

/// `requests.add` with `kind: "dj"`; returns the 1-based queue position.
pub fn add_dj(host: &dyn Host, t: &Track, idem: &str) -> Result<u64, ReqError> {
    let mut args = json!({
        "title": clean_text(&t.title, 200),
        "artist": clean_text(&t.artist, 200),
        "duration_ms": u64::from(t.duration_s) * 1000,
        "kind": "dj",
    });
    if let Some(u) = t.url.as_deref().filter(|u| u.len() <= 500) {
        args["source_url"] = json!(u);
    }
    let v = call(host, "requests.add", args, Some(idem))?;
    v.get("position")
        .and_then(Value::as_u64)
        .ok_or(ReqError::BadResponse)
}

/// One `music.stats.top` row: aggregate play counts only, never a requester.
#[derive(Clone, Debug, Deserialize)]
pub struct StatRow {
    #[serde(default)]
    pub title: Option<String>,
    pub artist: String,
    #[serde(default)]
    pub plays: u64,
}

/// `music.stats.top` (scope `music:stats.read`): most played `artist`s or `song`s.
pub fn stats_top(host: &dyn Host, kind: &str, limit: u32) -> Result<Vec<StatRow>, ReqError> {
    let v = call(
        host,
        "music.stats.top",
        json!({"kind": kind, "limit": limit.clamp(1, 25)}),
        None,
    )?;
    serde_json::from_value(v.get("items").cloned().unwrap_or(Value::Null))
        .map_err(|_| ReqError::BadResponse)
}

/// `settings.set {dmca_safe}` (scope `music:settings.write`, host-enforced): used only by the
/// one-shot carry-over of the old local DMCA-safe switch.
pub fn set_dmca_safe(host: &dyn Host, on: bool, idem: &str) -> Result<(), ReqError> {
    call(host, "settings.set", json!({"dmca_safe": on}), Some(idem)).map(|_| ())
}

/// True when an add was refused by the service's DMCA-safe mode.
pub fn is_not_dmca_safe(e: &ReqError) -> bool {
    matches!(e, ReqError::Rejected(c) if c.starts_with("not_dmca_safe"))
}

/// `requests.import`: one-shot legacy queue import (at most 100 items).
pub fn import(host: &dyn Host, items: &[Value], idem: &str) -> Result<u64, ReqError> {
    let v = call(host, "requests.import", json!({"items": items}), Some(idem))?;
    v.get("imported")
        .and_then(Value::as_u64)
        .ok_or(ReqError::BadResponse)
}

/// True for service refusals that mean "the one-shot import already happened / is moot".
pub fn import_already_done(e: &ReqError) -> bool {
    matches!(e, ReqError::Rejected(c)
        if c.contains("already") || c.contains("imported") || c.contains("not_empty"))
}

/// One `history.list` row: what was played, never who requested it.
#[derive(Clone, Debug, Deserialize)]
pub struct HistItem {
    pub title: String,
    #[serde(default)]
    pub artist: String,
}

/// `history.list` (scope `music:history.read`, host-enforced): the newest `limit` played
/// songs (1..50), used so the DJ never replays a recent song.
pub fn history_list(host: &dyn Host, limit: u32) -> Result<Vec<HistItem>, ReqError> {
    let v = call(
        host,
        "history.list",
        json!({"limit": limit.clamp(1, 50)}),
        None,
    )?;
    serde_json::from_value(v.get("items").cloned().unwrap_or(Value::Null))
        .map_err(|_| ReqError::BadResponse)
}
