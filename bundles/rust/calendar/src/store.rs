//! Typed persistence over the community kv. Keys use `.` only (no `:`, bundle kv rule).
//!
//! Layout (all community-scoped by the host):
//! `cal.seq` id counter | `cal.index` live ids | `cal.ev.<id>` event |
//! `cal.rsvp.<id>.<uuid>` one RSVP per (event, user) | `cal.rsvpc.<id>.<answer>` atomic answer
//! counters | `cal.rsvpn.<id>` atomic voter count | `cal.rsvpt` atomic total voter keys |
//! `cal.sync.<id>.<target>` mapping | `cal.pendq` ids owing a sync op | `cal.idem.<hash>` applied
//! idempotency keys (30-day TTL).
//!
//! Concurrency, precisely: the host kv offers get/set/delete/increment only -- no compare-and-
//! swap, no multi-key transaction, no key listing, and the db import has no filtered query. So:
//! RSVPs use one key per (event, user) plus atomic `increment` counters (no shared read-modify-
//! write, safe under concurrent RSVPs); event records commit through a version check
//! (`put_event_cas`, loses nothing silently: a concurrent change is a loud `Conflict`, with a
//! microsecond window between the check and the write that only host CAS can close); the shared
//! index/queue lists use read-back verified updates with bounded retry. Corrupt stored state is
//! a loud `Storage` error, never a silent default.

use serde::{Serialize, de::DeserializeOwned};

use crate::host::{CalError, Kv};
use crate::model::{Config, Event, Rsvp, SyncRecord, Target};

/// Events kept per community. Bounded by the host's 64 kv ops per invocation: a list reads the
/// index plus every event once.
pub const MAX_EVENTS: usize = 40;
pub const MAX_RSVPS: usize = 500;
/// Budget of per-user RSVP keys per community (host cap is 10k keys per app, shared with
/// events, mappings and idempotency keys). Orphaned voter keys of purged events are not
/// enumerable and are not reclaimed; new voters are refused loudly at this budget.
pub const MAX_TOTAL_RSVP_KEYS: i64 = 6000;
const IDEM_TTL_SECONDS: u32 = 30 * 86_400;
const LIST_RETRIES: usize = 4;
const SEQ: &str = "cal.seq";
const INDEX: &str = "cal.index";
const PENDQ: &str = "cal.pendq";

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
    kv.set(key, &raw, 0)
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

/// Adds/removes one id in a shared list with a read-back check: after writing, the list is
/// re-read and the change re-applied if a concurrent writer overwrote it (bounded retries; a
/// persistent loss is a loud `Conflict`).
fn update_list(kv: &dyn Kv, key: &str, id: u64, add: bool) -> Result<(), CalError> {
    for _ in 0..LIST_RETRIES {
        let mut v = load::<Vec<u64>>(kv, key)?.unwrap_or_default();
        let has = v.contains(&id);
        if has != add {
            if add {
                v.push(id);
            } else {
                v.retain(|x| *x != id);
            }
            save(kv, key, &v)?;
        }
        let check = load::<Vec<u64>>(kv, key)?.unwrap_or_default();
        if check.contains(&id) == add {
            return Ok(());
        }
    }
    Err(CalError::Conflict("calendar is busy, try again".into()))
}

/// Adds an event id to the index (verified).
pub fn index_add(kv: &dyn Kv, id: u64) -> Result<(), CalError> {
    update_list(kv, INDEX, id, true)
}

/// Removes an event id from the index (verified).
pub fn index_remove(kv: &dyn Kv, id: u64) -> Result<(), CalError> {
    update_list(kv, INDEX, id, false)
}

/// Loads one event.
pub fn event(kv: &dyn Kv, id: u64) -> Result<Option<Event>, CalError> {
    load(kv, &format!("cal.ev.{id}"))
}

/// Writes a NEW event record (does not touch the index).
pub fn put_event(kv: &dyn Kv, e: &Event) -> Result<(), CalError> {
    save(kv, &format!("cal.ev.{}", e.id), e)
}

/// Commits a mutated event: `e.version` must be exactly one above the stored version. A
/// concurrent mutation (stored version moved) is a loud `Conflict`, never a silent overwrite.
pub fn put_event_cas(kv: &dyn Kv, e: &Event) -> Result<(), CalError> {
    let stored = event(kv, e.id)?.ok_or(CalError::NotFound)?;
    if stored.version + 1 != e.version {
        return Err(CalError::Conflict(
            "the event was changed by someone else; please retry".into(),
        ));
    }
    put_event(kv, e)
}

/// Removes an event, its RSVPs and sync mappings (index handled by the caller).
pub fn purge_event(kv: &dyn Kv, id: u64) -> Result<(), CalError> {
    kv.delete(&format!("cal.ev.{id}"))?;
    kv.delete(&format!("cal.rsvpn.{id}"))?;
    for r in [Rsvp::Yes, Rsvp::No, Rsvp::Maybe] {
        kv.delete(&format!("cal.rsvpc.{id}.{}", r.label()))?;
    }
    for t in Target::ALL {
        kv.delete(&sync_key(id, t))?;
    }
    Ok(())
}

fn rsvp_key(id: u64, uid: &str) -> String {
    format!("cal.rsvp.{id}.{uid}")
}

fn read_counter(kv: &dyn Kv, key: &str) -> Result<u64, CalError> {
    match kv.get(key)? {
        None => Ok(0),
        Some(raw) => std::str::from_utf8(&raw)
            .ok()
            .and_then(|t| t.parse::<i64>().ok())
            .map(|n| u64::try_from(n).unwrap_or(0))
            .ok_or_else(|| CalError::Storage(format!("corrupt counter at {key}"))),
    }
}

/// The caller's own RSVP for an event, if any.
pub fn rsvp_get(kv: &dyn Kv, id: u64, uid: &str) -> Result<Option<Rsvp>, CalError> {
    load(kv, &rsvp_key(id, uid))
}

/// Records one user's RSVP without any shared read-modify-write: a per-(event, user) key plus
/// atomic counters, so concurrent RSVPs from different users can never lose each other.
pub fn rsvp_set(kv: &dyn Kv, id: u64, uid: &str, answer: Rsvp) -> Result<(), CalError> {
    let prev = rsvp_get(kv, id, uid)?;
    if prev == Some(answer) {
        return Ok(());
    }
    if prev.is_none() {
        let n = kv.increment(&format!("cal.rsvpn.{id}"), 1)?;
        let total = kv.increment("cal.rsvpt", 1)?;
        if n > i64::try_from(MAX_RSVPS).unwrap_or(i64::MAX) || total > MAX_TOTAL_RSVP_KEYS {
            kv.increment(&format!("cal.rsvpn.{id}"), -1)?;
            kv.increment("cal.rsvpt", -1)?;
            return Err(CalError::Conflict(format!(
                "RSVPs are full (max {MAX_RSVPS} per event)"
            )));
        }
    }
    save(kv, &rsvp_key(id, uid), &answer)?;
    if let Some(p) = prev {
        kv.increment(&format!("cal.rsvpc.{id}.{}", p.label()), -1)?;
    }
    kv.increment(&format!("cal.rsvpc.{id}.{}", answer.label()), 1)?;
    Ok(())
}

/// RSVP counts (yes, no, maybe) for an event, read from the atomic counters.
pub fn rsvp_counts(kv: &dyn Kv, id: u64) -> Result<(u64, u64, u64), CalError> {
    let c = |r: Rsvp| read_counter(kv, &format!("cal.rsvpc.{id}.{}", r.label()));
    Ok((c(Rsvp::Yes)?, c(Rsvp::No)?, c(Rsvp::Maybe)?))
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

/// Adds an event id to the pending-sync queue (verified).
pub fn pending_add(kv: &dyn Kv, id: u64) -> Result<(), CalError> {
    update_list(kv, PENDQ, id, true)
}

/// Removes an event id from the pending-sync queue (verified).
pub fn pending_remove(kv: &dyn Kv, id: u64) -> Result<(), CalError> {
    update_list(kv, PENDQ, id, false)
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
    kv.set(&idem_key(key_hash), b"1", IDEM_TTL_SECONDS)
}

/// Effective config: the hub-resolved `config_json` (strictly parsed) and nothing else.
/// Security toggles (`approval_required`, `sync_*`) are set only through the hub config /
/// bundle-admin gateway; nothing a chat user can write may loosen them.
pub fn effective_config(config_json: &str) -> Result<Config, CalError> {
    if config_json.trim().is_empty() {
        return Ok(Config::default());
    }
    serde_json::from_str(config_json)
        .map_err(|_| CalError::Storage("bundle config is not valid".into()))
}
