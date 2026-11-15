//! Typed client for the shared queue service `waddles.core.service.music-requests`
//! (contract: `docs/music-requests-service.md`).
//!
//! Tools used: `requests.list`, `requests.add` (kind `dj`), `requests.advance_due`,
//! `requests.import` (one-shot legacy migration), `dj.get`, `dj.set`. Scopes are
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
