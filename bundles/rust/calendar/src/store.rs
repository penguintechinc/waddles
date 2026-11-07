//! Typed persistence over the community kv. Keys use `.` only (no `:`, bundle kv rule).
//!
//! Layout (all community-scoped by the host):
//! `cal.seq` id counter | `cal.index` live ids | `cal.ev.<id>` event | `cal.rsvp.<id>` RSVP map |
//! `cal.sync.<id>.<target>` mapping | `cal.pendq` ids owing a sync op | `cal.idem.<hash>` applied
//! idempotency keys | `cal.cfg` config overrides.
//!
//! kv has no list/CAS, so index and RSVP updates are read-modify-write; a concurrent writer in
//! one community can lose an update (documented in the README, not hidden). Corrupt stored state
//! is a loud `Storage` error, never a silent default.

use std::collections::BTreeMap;

use serde::{Serialize, de::DeserializeOwned};

use crate::host::{CalError, Kv};
use crate::model::{Config, Event, Rsvp, SyncRecord, Target};

pub const MAX_EVENTS: usize = 400;
pub const MAX_RSVPS: usize = 500;
const SEQ: &str = "cal.seq";
const INDEX: &str = "cal.index";
const PENDQ: &str = "cal.pendq";
const CFG: &str = "cal.cfg";

fn load<T: DeserializeOwned>(kv: &dyn Kv, key: &str) -> Result<Option<T>, CalError> {
    match kv.get(key)? {
        None => Ok(None),
        Some(raw) => serde_json::from_slice(&raw)
            .map(Some)
            .map_err(|_| CalError::Storage(format!("corrupt stored value at {key}"))),
    }
}

fn save<T: Serialize>(kv: &dyn Kv, key: &str, v: &T) -> Result<(), CalError> {
    let raw = serde_json::to_vec(v).map_err(|e| CalError::Storage(e.to_string()))?;
    kv.set(key, &raw)
}

/// Allocates the next community-local event id.
pub fn next_id(kv: &dyn Kv) -> Result<u64, CalError> {
    let n = kv.increment(SEQ, 1)?;
    u64::try_from(n).map_err(|_| CalError::Storage("event id counter went negative".into()))
}

/// Ids of all stored events, ascending.
pub fn index(kv: &dyn Kv) -> Result<Vec<u64>, CalError> {
    Ok(load::<Vec<u64>>(kv, INDEX)?.unwrap_or_default())
}

/// Persists the id index.
pub fn set_index(kv: &dyn Kv, ids: &[u64]) -> Result<(), CalError> {
    save(kv, INDEX, &ids)
}

/// Loads one event.
pub fn event(kv: &dyn Kv, id: u64) -> Result<Option<Event>, CalError> {
    load(kv, &format!("cal.ev.{id}"))
}

/// Writes one event record (does not touch the index).
pub fn put_event(kv: &dyn Kv, e: &Event) -> Result<(), CalError> {
    save(kv, &format!("cal.ev.{}", e.id), e)
}

/// Removes an event, its RSVPs and sync mappings (index handled by the caller).
pub fn purge_event(kv: &dyn Kv, id: u64) -> Result<(), CalError> {
    kv.delete(&format!("cal.ev.{id}"))?;
    kv.delete(&format!("cal.rsvp.{id}"))?;
    for t in Target::ALL {
        kv.delete(&sync_key(id, t))?;
    }
    Ok(())
}

/// RSVP map keyed by user UUID (never a username).
pub fn rsvps(kv: &dyn Kv, id: u64) -> Result<BTreeMap<String, Rsvp>, CalError> {
    Ok(load(kv, &format!("cal.rsvp.{id}"))?.unwrap_or_default())
}

/// Persists the RSVP map.
pub fn put_rsvps(kv: &dyn Kv, id: u64, m: &BTreeMap<String, Rsvp>) -> Result<(), CalError> {
    save(kv, &format!("cal.rsvp.{id}"), m)
}

fn sync_key(id: u64, t: Target) -> String {
    format!("cal.sync.{id}.{}", t.name())
}

/// Loads the mapping for (event, target).
pub fn sync_record(kv: &dyn Kv, id: u64, t: Target) -> Result<Option<SyncRecord>, CalError> {
    load(kv, &sync_key(id, t))
}

/// Persists the mapping for (event, target).
pub fn put_sync_record(kv: &dyn Kv, id: u64, r: &SyncRecord) -> Result<(), CalError> {
    save(kv, &sync_key(id, r.target), r)
}

/// Event ids owing at least one sync op.
pub fn pending(kv: &dyn Kv) -> Result<Vec<u64>, CalError> {
    Ok(load::<Vec<u64>>(kv, PENDQ)?.unwrap_or_default())
}

/// Persists the pending-sync queue.
pub fn set_pending(kv: &dyn Kv, ids: &[u64]) -> Result<(), CalError> {
    save(kv, PENDQ, &ids)
}

fn idem_key(key_hash: &str) -> String {
    format!("cal.idem.{}", &key_hash[..key_hash.len().min(40)])
}

/// True if this idempotency key was already applied.
pub fn idem_applied(kv: &dyn Kv, key_hash: &str) -> Result<bool, CalError> {
    Ok(kv.get(&idem_key(key_hash))?.is_some())
}

/// Records an applied idempotency key.
pub fn mark_idem(kv: &dyn Kv, key_hash: &str) -> Result<(), CalError> {
    kv.set(&idem_key(key_hash), b"1")
}

/// kv-level config overrides (set by chat toggles); `None` fields fall back to hub config.
#[derive(Debug, Default, Clone, serde::Serialize, serde::Deserialize)]
#[serde(default)]
pub struct Overrides {
    pub approval_required: Option<bool>,
    pub sync_discord: Option<bool>,
    pub sync_twitch: Option<bool>,
    pub sync_wix: Option<bool>,
}

/// Loads the kv overrides.
pub fn overrides(kv: &dyn Kv) -> Result<Overrides, CalError> {
    Ok(load(kv, CFG)?.unwrap_or_default())
}

/// Persists the kv overrides.
pub fn put_overrides(kv: &dyn Kv, o: &Overrides) -> Result<(), CalError> {
    save(kv, CFG, o)
}

/// Effective config: hub-resolved `config_json` (strictly parsed) with kv overrides applied.
pub fn effective_config(kv: &dyn Kv, config_json: &str) -> Result<Config, CalError> {
    let mut cfg: Config = if config_json.trim().is_empty() {
        Config::default()
    } else {
        serde_json::from_str(config_json)
            .map_err(|_| CalError::Storage("bundle config is not valid".into()))?
    };
    let o = overrides(kv)?;
    if let Some(v) = o.approval_required {
        cfg.approval_required = v;
    }
    if let Some(v) = o.sync_discord {
        cfg.sync_discord = v;
    }
    if let Some(v) = o.sync_twitch {
        cfg.sync_twitch = v;
    }
    if let Some(v) = o.sync_wix {
        cfg.sync_wix = v;
    }
    Ok(cfg)
}
