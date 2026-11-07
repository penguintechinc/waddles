//! Calendar operations: create/update/cancel/approve/reject/rsvp/list. Pure logic over the host
//! seams; every mutation bumps the event version and queues sync, then flushes it inline.

use std::collections::BTreeMap;

use crate::host::{CalError, Caller, Ctx};
use crate::model::{Config, Event, Rsvp, Status, SyncOp, SyncRecord, Target, derive_public_id};
use crate::store::{self, MAX_EVENTS, MAX_RSVPS};
use crate::sync;
use crate::timeutil::{
    MAX_CHANNEL, MAX_LOCATION, MAX_TITLE, check_description, check_text, check_window, parse_tz,
    parse_when,
};

/// Input for creating an event; `start`/`end` are already parsed to UTC seconds.
#[derive(Debug, Clone)]
pub struct NewEvent {
    pub title: String,
    pub description: String,
    pub start: i64,
    pub end: Option<i64>,
    pub tz: Option<String>,
    pub location: Option<String>,
    pub channel: Option<String>,
}

/// Partial edit; `None` leaves a field unchanged. `location`/`channel` `Some("")` clears.
#[derive(Debug, Clone, Default)]
pub struct Patch {
    pub title: Option<String>,
    pub description: Option<String>,
    pub start: Option<i64>,
    pub end: Option<i64>,
    pub tz: Option<String>,
    pub location: Option<String>,
    pub channel: Option<String>,
}

fn opt_text(name: &str, v: &Option<String>, max: usize) -> Result<Option<String>, CalError> {
    match v {
        None => Ok(None),
        Some(s) if s.trim().is_empty() => Ok(None),
        Some(s) => check_text(name, s, max, true).map(Some),
    }
}

fn load(ctx: &Ctx, id: u64) -> Result<Event, CalError> {
    store::event(ctx.kv, id)?.ok_or(CalError::NotFound)
}

fn owns_or_approves(caller: &Caller, ev: &Event) -> bool {
    caller.approver || caller.actor.as_deref() == Some(ev.created_by.as_str())
}

/// Makes room by dropping the oldest terminal event with no sync work outstanding.
fn make_room(ctx: &Ctx, index: &mut Vec<u64>) -> Result<(), CalError> {
    if index.len() < MAX_EVENTS {
        return Ok(());
    }
    for (pos, id) in index.clone().into_iter().enumerate() {
        let Some(ev) = store::event(ctx.kv, id)? else {
            index.remove(pos);
            return Ok(());
        };
        let owes = Target::ALL.iter().any(
            |t| matches!(store::sync_record(ctx.kv, id, *t), Ok(Some(r)) if r.pending_op.is_some()),
        );
        if ev.status.is_terminal() && !owes {
            store::purge_event(ctx.kv, id)?;
            index.remove(pos);
            return Ok(());
        }
    }
    Err(CalError::Conflict(format!(
        "calendar is full ({MAX_EVENTS} active events); cancel or remove one first"
    )))
}

/// Creates an event. Approvers publish immediately; others need `approval_required` and land
/// in `Pending`.
pub fn create(ctx: &Ctx, cfg: &Config, caller: &Caller, n: NewEvent) -> Result<Event, CalError> {
    let creator = caller.require_actor()?.to_string();
    if !caller.approver && !cfg.approval_required {
        return Err(CalError::Forbidden);
    }
    let title = check_text("title", &n.title, MAX_TITLE, true)?;
    let description = check_description(&n.description)?;
    let tz_name = n.tz.clone().unwrap_or_else(|| cfg.default_tz.clone());
    parse_tz(&tz_name)?;
    let end = n
        .end
        .unwrap_or(n.start + i64::from(cfg.default_duration_minutes) * 60);
    check_window(n.start, end)?;
    let location = opt_text("location", &n.location, MAX_LOCATION)?;
    let channel = opt_text("channel", &n.channel, MAX_CHANNEL)?;

    let mut index = store::index(ctx.kv)?;
    make_room(ctx, &mut index)?;
    let id = store::next_id(ctx.kv)?;
    let status = if caller.approver || !cfg.approval_required {
        Status::Scheduled
    } else {
        Status::Pending
    };
    let ev = Event {
        id,
        public_id: derive_public_id(ctx.tenant, ctx.community, id),
        title,
        description,
        start: n.start,
        end,
        tz: tz_name,
        location,
        channel,
        status,
        created_by: creator.clone(),
        approved_by: (status == Status::Scheduled).then(|| creator.clone()),
        reject_reason: None,
        version: 1,
    };
    store::put_event(ctx.kv, &ev)?;
    index.push(id);
    store::set_index(ctx.kv, &index)?;
    if ev.status == Status::Scheduled {
        sync::enqueue(ctx, cfg, &ev, SyncOp::Create)?;
        sync::flush_event(ctx, cfg, &ev)?;
    }
    Ok(ev)
}

/// Edits an event (owner or approver). Scheduled events re-sync.
pub fn update(
    ctx: &Ctx,
    cfg: &Config,
    caller: &Caller,
    id: u64,
    p: Patch,
) -> Result<Event, CalError> {
    let mut ev = load(ctx, id)?;
    if !owns_or_approves(caller, &ev) {
        return Err(CalError::Forbidden);
    }
    if ev.status.is_terminal() {
        return Err(CalError::Conflict(format!(
            "event is {}",
            ev.status.label()
        )));
    }
    if let Some(t) = &p.title {
        ev.title = check_text("title", t, MAX_TITLE, true)?;
    }
    if let Some(d) = &p.description {
        ev.description = check_description(d)?;
    }
    if let Some(tz) = &p.tz {
        parse_tz(tz)?;
        ev.tz = tz.clone();
    }
    if let Some(loc) = &p.location {
        ev.location = opt_text("location", &Some(loc.clone()), MAX_LOCATION)?;
    }
    if let Some(ch) = &p.channel {
        ev.channel = opt_text("channel", &Some(ch.clone()), MAX_CHANNEL)?;
    }
    match (p.start, p.end) {
        (Some(s), Some(e)) => {
            ev.start = s;
            ev.end = e;
        }
        (Some(s), None) => {
            let dur = ev.end - ev.start;
            ev.start = s;
            ev.end = s + dur;
        }
        (None, Some(e)) => ev.end = e,
        (None, None) => {}
    }
    check_window(ev.start, ev.end)?;
    // Approval gate: a non-approver's edit of an approved event sends it back to Pending (no
    // sync; any remote copy is left unchanged) until an approver re-approves it.
    if cfg.approval_required && !caller.approver && ev.status == Status::Scheduled {
        ev.status = Status::Pending;
        ev.approved_by = None;
    }
    ev.version += 1;
    store::put_event(ctx.kv, &ev)?;
    if ev.status == Status::Scheduled {
        sync::enqueue(ctx, cfg, &ev, SyncOp::Update)?;
        sync::flush_event(ctx, cfg, &ev)?;
    }
    Ok(ev)
}

/// Cancels a pending/scheduled event (owner or approver); remote copies are cancelled.
pub fn cancel(ctx: &Ctx, cfg: &Config, caller: &Caller, id: u64) -> Result<Event, CalError> {
    let mut ev = load(ctx, id)?;
    if !owns_or_approves(caller, &ev) {
        return Err(CalError::Forbidden);
    }
    if ev.status.is_terminal() {
        return Err(CalError::Conflict(format!(
            "event is already {}",
            ev.status.label()
        )));
    }
    ev.status = Status::Cancelled;
    ev.version += 1;
    store::put_event(ctx.kv, &ev)?;
    sync::enqueue(ctx, cfg, &ev, SyncOp::Cancel)?;
    sync::flush_event(ctx, cfg, &ev)?;
    Ok(ev)
}

/// Hard delete (approver only): removes remote copies, then the local record.
pub fn delete(ctx: &Ctx, cfg: &Config, caller: &Caller, id: u64) -> Result<(), CalError> {
    if !caller.approver {
        return Err(CalError::Forbidden);
    }
    let mut ev = load(ctx, id)?;
    ev.version += 1;
    ev.status = Status::Cancelled;
    store::put_event(ctx.kv, &ev)?;
    sync::enqueue(ctx, cfg, &ev, SyncOp::Delete)?;
    sync::flush_event(ctx, cfg, &ev)?;
    // Keep the record while any enabled target still owes the removal (retry on next touch).
    for t in Target::ALL {
        if let Some(SyncRecord {
            pending_op: Some(_),
            ..
        }) = store::sync_record(ctx.kv, id, t)?
            && cfg.sync_enabled(t)
        {
            return Err(CalError::Conflict(
                "remote removal still pending; delete will complete on retry".into(),
            ));
        }
    }
    store::purge_event(ctx.kv, id)?;
    let mut index = store::index(ctx.kv)?;
    index.retain(|x| *x != id);
    store::set_index(ctx.kv, &index)?;
    let mut q = store::pending(ctx.kv)?;
    q.retain(|x| *x != id);
    store::set_pending(ctx.kv, &q)
}

/// Approves a pending event (approver only) and starts sync.
pub fn approve(ctx: &Ctx, cfg: &Config, caller: &Caller, id: u64) -> Result<Event, CalError> {
    if !caller.approver {
        return Err(CalError::Forbidden);
    }
    let approver = caller.require_actor()?.to_string();
    let mut ev = load(ctx, id)?;
    if ev.status != Status::Pending {
        return Err(CalError::Conflict(format!(
            "event is {}, not pending",
            ev.status.label()
        )));
    }
    ev.status = Status::Scheduled;
    ev.approved_by = Some(approver);
    ev.version += 1;
    store::put_event(ctx.kv, &ev)?;
    sync::enqueue(ctx, cfg, &ev, SyncOp::Create)?;
    sync::flush_event(ctx, cfg, &ev)?;
    Ok(ev)
}

/// Rejects a pending event (approver only).
pub fn reject(
    ctx: &Ctx,
    caller: &Caller,
    id: u64,
    reason: Option<String>,
) -> Result<Event, CalError> {
    if !caller.approver {
        return Err(CalError::Forbidden);
    }
    let mut ev = load(ctx, id)?;
    if ev.status != Status::Pending {
        return Err(CalError::Conflict(format!(
            "event is {}, not pending",
            ev.status.label()
        )));
    }
    ev.reject_reason = opt_text("reason", &reason, 200)?;
    ev.status = Status::Rejected;
    ev.version += 1;
    store::put_event(ctx.kv, &ev)?;
    Ok(ev)
}

/// Records the caller's RSVP (visible, scheduled events only; keyed by user UUID).
pub fn rsvp(ctx: &Ctx, caller: &Caller, id: u64, answer: Rsvp) -> Result<Event, CalError> {
    let uid = caller.require_actor()?.to_string();
    let ev = load(ctx, id)?;
    if ev.status != Status::Scheduled {
        return Err(CalError::NotFound);
    }
    let mut m = store::rsvps(ctx.kv, id)?;
    if !m.contains_key(&uid) && m.len() >= MAX_RSVPS {
        return Err(CalError::Conflict(format!(
            "event is full ({MAX_RSVPS} RSVPs)"
        )));
    }
    m.insert(uid, answer);
    store::put_rsvps(ctx.kv, id, &m)?;
    Ok(ev)
}

/// RSVP counts (yes, no, maybe) for an event.
pub fn rsvp_counts(m: &BTreeMap<String, Rsvp>) -> (usize, usize, usize) {
    let c = |r: Rsvp| m.values().filter(|v| **v == r).count();
    (c(Rsvp::Yes), c(Rsvp::No), c(Rsvp::Maybe))
}

/// One event the caller may see: scheduled ones for everyone, anything for approvers/owners.
pub fn get(ctx: &Ctx, caller: &Caller, id: u64) -> Result<Event, CalError> {
    let ev = load(ctx, id)?;
    if ev.status == Status::Scheduled || owns_or_approves(caller, &ev) {
        Ok(ev)
    } else {
        Err(CalError::NotFound)
    }
}

/// Upcoming scheduled events (end not passed), soonest first, at most `limit`. With
/// `include_pending`, approvers also see pending events.
pub fn list_upcoming(
    ctx: &Ctx,
    include_pending: bool,
    limit: usize,
) -> Result<Vec<Event>, CalError> {
    let now = ctx.clock.now_secs();
    let mut out = Vec::new();
    for id in store::index(ctx.kv)? {
        let Some(ev) = store::event(ctx.kv, id)? else {
            continue;
        };
        let visible =
            ev.status == Status::Scheduled || (include_pending && ev.status == Status::Pending);
        if visible && ev.end >= now {
            out.push(ev);
        }
    }
    out.sort_by_key(|e| (e.start, e.id));
    out.truncate(limit);
    Ok(out)
}

/// Admin listing: every stored event (optionally filtered by status), newest id first.
pub fn list_all(ctx: &Ctx, status: Option<Status>, limit: usize) -> Result<Vec<Event>, CalError> {
    let mut out = Vec::new();
    for id in store::index(ctx.kv)?.into_iter().rev() {
        let Some(ev) = store::event(ctx.kv, id)? else {
            continue;
        };
        if status.is_none_or(|s| s == ev.status) {
            out.push(ev);
            if out.len() >= limit {
                break;
            }
        }
    }
    Ok(out)
}

/// Parses `when` text in the event's zone (helper shared by chat and admin paths).
pub fn parse_time(text: &str, tz: &str) -> Result<i64, CalError> {
    let z = parse_tz(tz)?;
    parse_when(text, &z)
}
