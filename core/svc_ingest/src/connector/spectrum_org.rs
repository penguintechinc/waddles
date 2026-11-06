//! Spectrum organization snapshot parsing and diffing (gh #486), the host-side
//! port of `receivers/spectrum_org.py` plus the member/event parsers of
//! `receivers/spectrum_poll.py`.
//!
//! A roster or event list is a SNAPSHOT (current state), not an append-only
//! stream, so the host turns "state now" into "what changed" before the pinned
//! connector sees anything: the connector is stateless and receives each change
//! as one raw frame.
//!
//! * **Parsing** is best effort per entry (`parse_members` / `parse_events`);
//!   entries without an id are dropped and COUNTED, never silently.
//! * **Diffing** compares the stored snapshot with the current one. With no
//!   stored snapshot the first run only PRIMES (no changes, so connecting never
//!   floods downstream with history).
//! * **Anomaly guard** -- a glitchy response (empty page, truncated list) must
//!   never read as "everyone left": a roster that empties or shrinks past the
//!   ratio, or a mass vanish of upcoming events, is an error and the snapshot
//!   is NOT advanced.
//!
//! PII boundary: stored snapshots hold opaque RSI ids and a role/content
//! fingerprint only; display names and handles are not parsed at all, so a
//! `left` change carries only the member id and no frame ever carries a name.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

/// Snapshot schema version -- bump (and handle) if the stored shape changes.
pub const SNAPSHOT_VERSION: u64 = 1;
/// Default shrink ratio past which a roster/event drop is an anomaly.
pub const DEFAULT_MAX_DEPARTURE_RATIO: f64 = 0.5;
/// Below this many previous members the ratio guard is skipped (tiny orgs
/// churn legitimately); an outright empty roster is still always an anomaly.
pub const DEFAULT_GUARD_MIN_SIZE: usize = 10;
/// Upcoming events needed before a mass-vanish counts as an anomaly.
pub const EVENT_GUARD_MIN_SIZE: usize = 5;
/// Longest org-event description carried downstream (chars).
pub const MAX_DESCRIPTION_CHARS: usize = 2000;

const STATUS_SCHEDULED: &str = "scheduled";
const STATUS_CANCELLED: &str = "cancelled";

/// One parsed roster entry; `roles` is `(role_id, role_name)` sorted by id.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Member {
    /// Opaque RSI member id.
    pub member_id: String,
    /// `(role_id, role_name)` pairs, sorted by id; the org rank is folded in as
    /// the pseudo-role `rank:<id>` so a promotion reads as a role change.
    pub roles: Vec<(String, Option<String>)>,
}

impl Member {
    fn role_ids(&self) -> Vec<String> {
        self.roles.iter().map(|(id, _)| id.clone()).collect()
    }

    fn role_names(&self) -> Vec<String> {
        self.roles.iter().filter_map(|(_, n)| n.clone()).collect()
    }

    /// Stable role signature stored in the snapshot (ids only, no names).
    fn fingerprint(&self) -> String {
        self.role_ids().join(",")
    }
}

/// One parsed org event; `rsvp_count` is the attending headcount.
#[derive(Debug, Clone, PartialEq)]
pub struct OrgEvent {
    /// Opaque RSI event id.
    pub event_id: String,
    /// Title.
    pub title: Option<String>,
    /// Description (bounded when framed).
    pub description: Option<String>,
    /// Start, epoch seconds.
    pub starts_epoch: Option<f64>,
    /// End, epoch seconds.
    pub ends_epoch: Option<f64>,
    /// Location text.
    pub location: Option<String>,
    /// Organizer's RSI member id (tokenized by the connector).
    pub organizer_id: Option<String>,
    /// `scheduled` | `cancelled`.
    pub status: String,
    /// Attending headcount.
    pub rsvp_count: Option<u64>,
}

impl OrgEvent {
    /// Content hash of everything except the RSVP count (which diffs apart).
    fn fingerprint(&self) -> String {
        let blob = json!([
            self.title,
            self.description,
            self.starts_epoch,
            self.ends_epoch,
            self.location,
            self.status
        ])
        .to_string();
        let digest = Sha256::digest(blob.as_bytes());
        digest.iter().take(8).map(|b| format!("{b:02x}")).collect()
    }
}

/// A detected roster delta.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RosterChange {
    /// `joined` | `left` | `roles_changed`.
    pub change: &'static str,
    /// Opaque RSI member id.
    pub member_id: String,
    /// Role ids newly held.
    pub roles_added: Vec<String>,
    /// Role ids no longer held.
    pub roles_removed: Vec<String>,
    /// Known role names after the change.
    pub role_names: Vec<String>,
}

/// A detected event delta; `event` is `None` for `removed`.
#[derive(Debug, Clone, PartialEq)]
pub struct EventChange {
    /// `created` | `updated` | `cancelled` | `rsvp_changed` | `removed`.
    pub change: &'static str,
    /// Opaque RSI event id.
    pub event_id: String,
    /// The current event (absent for `removed`).
    pub event: Option<OrgEvent>,
    /// Attending headcount before an `rsvp_changed`.
    pub rsvp_previous: Option<u64>,
}

/// Why a diff was refused; the snapshot must NOT advance.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum DiffError {
    /// The new snapshot shrank implausibly (RSI glitch suspected).
    #[error("snapshot shrank implausibly: {0}")]
    Anomaly(&'static str),
}

/// Result of [`diff_roster`].
#[derive(Debug, Clone, PartialEq)]
pub struct RosterDiff {
    /// Deltas to emit.
    pub changes: Vec<RosterChange>,
    /// Snapshot to persist once the changes are published.
    pub snapshot: Value,
    /// True when there was no usable previous snapshot (this run primed).
    pub primed: bool,
    /// True when a stored snapshot was present but unusable (counted loudly).
    pub previous_discarded: bool,
}

/// Result of [`diff_events`].
#[derive(Debug, Clone, PartialEq)]
pub struct EventsDiff {
    /// Deltas to emit.
    pub changes: Vec<EventChange>,
    /// Snapshot to persist once the changes are published.
    pub snapshot: Value,
    /// True when there was no usable previous snapshot (this run primed).
    pub primed: bool,
    /// True when a stored snapshot was present but unusable.
    pub previous_discarded: bool,
    /// Events that ended (vanished after their start) and were pruned silently.
    pub pruned: usize,
}

/// Python `_s`: a non-empty string, or an integer coerced to a string.
pub(crate) fn s_of(v: Option<&Value>) -> Option<String> {
    match v {
        Some(Value::String(s)) if !s.is_empty() => Some(s.clone()),
        Some(Value::Number(n)) if n.is_i64() || n.is_u64() => Some(n.to_string()),
        _ => None,
    }
}

/// Python `_epoch`: int/float/numeric-string seconds; bool and junk are absent.
fn epoch_of(v: Option<&Value>) -> Option<f64> {
    match v {
        Some(Value::Number(n)) => n.as_f64(),
        Some(Value::String(s)) => s.trim().parse::<f64>().ok().filter(|f| f.is_finite()),
        _ => None,
    }
}

/// Python `a or b or c` over optional epochs: the first non-zero value, else
/// the last candidate as given.
fn first_epoch(vals: [Option<f64>; 3]) -> Option<f64> {
    vals.iter()
        .flatten()
        .find(|f| **f != 0.0)
        .copied()
        .or(vals[2])
}

/// Python `_count_or_none`: a non-negative integer count (numeric string ok).
pub(crate) fn count_of(v: Option<&Value>) -> Option<u64> {
    match v {
        Some(Value::Number(n)) => n.as_u64().or_else(|| {
            n.as_f64()
                .filter(|f| *f >= 0.0 && f.is_finite())
                .map(|f| f as u64)
        }),
        Some(Value::String(s)) if s.trim().bytes().all(|b| b.is_ascii_digit()) => {
            s.trim().parse::<u64>().ok()
        }
        _ => None,
    }
}

/// Epoch seconds -> UTC ISO-8601 (`+00:00`), or `None` when out of range.
#[must_use]
pub fn iso_of(epoch: f64) -> Option<String> {
    if !epoch.is_finite() {
        return None;
    }
    let secs = epoch.floor();
    let nanos = ((epoch - secs) * 1e9).round().clamp(0.0, 999_999_999.0) as u32;
    chrono::DateTime::from_timestamp(secs as i64, nanos).map(|d| d.to_rfc3339())
}

fn role_pair(raw: &Value) -> Option<(String, Option<String>)> {
    match raw {
        Value::Object(o) => s_of(o.get("id")).map(|id| (id, s_of(o.get("name")))),
        other => s_of(Some(other)).map(|id| (id, None)),
    }
}

fn member_of(raw: &Map<String, Value>) -> Option<Member> {
    let member_id = s_of(raw.get("id")).or_else(|| s_of(raw.get("member_id")))?;
    let roles_raw = match raw.get("roles") {
        Some(Value::Array(a)) => Some(a),
        _ => match raw.get("role_ids") {
            Some(Value::Array(a)) => Some(a),
            _ => None,
        },
    };
    let mut pairs: BTreeMap<String, Option<String>> = BTreeMap::new();
    for entry in roles_raw.into_iter().flatten() {
        if let Some((id, name)) = role_pair(entry) {
            pairs.insert(id, name);
        }
    }
    if let Some(Value::Object(rank)) = raw.get("rank") {
        if let Some(rank_id) = s_of(rank.get("id")) {
            pairs.insert(format!("rank:{rank_id}"), s_of(rank.get("name")));
        }
    }
    Some(Member {
        member_id,
        roles: pairs.into_iter().collect(),
    })
}

fn event_of(raw: &Map<String, Value>) -> Option<OrgEvent> {
    let event_id = s_of(raw.get("id")).or_else(|| s_of(raw.get("event_id")))?;
    let rsvp = raw.get("rsvp").and_then(Value::as_object);
    let attending = rsvp
        .and_then(|r| count_of(r.get("count_attending")))
        .or_else(|| count_of(raw.get("rsvp_count")))
        .or_else(|| count_of(raw.get("attending_count")));
    let organizer = raw.get("member").and_then(Value::as_object);
    // Strict: only a real `true` / `1` cancels (a string "false" must not
    // fabricate an org_event_cancelled).
    let flagged = match raw.get("cancelled") {
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_i64() == Some(1),
        _ => false,
    };
    let cancelled = flagged || s_of(raw.get("status")).as_deref() == Some(STATUS_CANCELLED);
    let description = s_of(raw.get("description"))
        .or_else(|| s_of(raw.get("body")))
        .map(|d| d.trim().to_owned());
    Some(OrgEvent {
        event_id,
        title: s_of(raw.get("title")).or_else(|| s_of(raw.get("name"))),
        description,
        starts_epoch: first_epoch([
            epoch_of(raw.get("time_start")),
            epoch_of(raw.get("start_time")),
            epoch_of(raw.get("starts_at")),
        ]),
        ends_epoch: first_epoch([
            epoch_of(raw.get("time_end")),
            epoch_of(raw.get("end_time")),
            epoch_of(raw.get("ends_at")),
        ]),
        location: s_of(raw.get("location")),
        organizer_id: organizer
            .and_then(|o| s_of(o.get("id")))
            .or_else(|| s_of(raw.get("member_id"))),
        status: if cancelled {
            STATUS_CANCELLED.to_owned()
        } else {
            STATUS_SCHEDULED.to_owned()
        },
        rsvp_count: attending,
    })
}

/// Entries parsed from one snapshot page plus the count dropped (no id).
#[derive(Debug, Clone, PartialEq)]
pub struct Parsed<T> {
    /// Parsed entries, in response order.
    pub items: Vec<T>,
    /// Entries dropped as unparseable (a count only, never content).
    pub dropped: usize,
}

fn parse_list<T>(list: &[Value], f: impl Fn(&Map<String, Value>) -> Option<T>) -> Parsed<T> {
    let items: Vec<T> = list
        .iter()
        .filter_map(|v| v.as_object().and_then(&f))
        .collect();
    Parsed {
        dropped: list.len() - items.len(),
        items,
    }
}

/// Parses roster entries (`members` / `member_list` page arrays).
#[must_use]
pub fn parse_members(list: &[Value]) -> Parsed<Member> {
    parse_list(list, member_of)
}

/// Parses event entries.
#[must_use]
pub fn parse_events(list: &[Value]) -> Parsed<OrgEvent> {
    parse_list(list, event_of)
}

/// Serializes a roster to its stored form `{v, members: {id: fingerprint}}`.
fn roster_snapshot(members: &[Member]) -> Value {
    let map: Map<String, Value> = members
        .iter()
        .map(|m| (m.member_id.clone(), Value::String(m.fingerprint())))
        .collect();
    json!({"v": SNAPSHOT_VERSION, "members": map})
}

/// Serializes events to `{v, events: {id: {fp, rsvp, start, status}}}`.
fn events_snapshot(events: &[OrgEvent]) -> Value {
    let map: Map<String, Value> = events
        .iter()
        .map(|e| {
            (
                e.event_id.clone(),
                json!({
                    "fp": e.fingerprint(),
                    "rsvp": e.rsvp_count,
                    "start": e.starts_epoch,
                    "status": e.status,
                }),
            )
        })
        .collect();
    json!({"v": SNAPSHOT_VERSION, "events": map})
}

/// Dedupes by id keeping the first position and the last value (dict semantics).
fn dedupe<T: Clone>(items: &[T], id: impl Fn(&T) -> &str) -> Vec<T> {
    let mut index: HashMap<String, usize> = HashMap::new();
    let mut out: Vec<T> = Vec::with_capacity(items.len());
    for it in items {
        match index.get(id(it)) {
            Some(i) => out[*i] = it.clone(),
            None => {
                index.insert(id(it).to_owned(), out.len());
                out.push(it.clone());
            }
        }
    }
    out
}

/// The stored section when `previous` carries a usable object under `section`
/// and the right version; `(None, discarded)` otherwise.
fn usable_section<'a>(
    previous: Option<&'a Value>,
    section: &str,
) -> (Option<&'a Map<String, Value>>, bool) {
    let Some(prev) = previous else {
        return (None, false);
    };
    let ok = prev.get("v").and_then(Value::as_u64) == Some(SNAPSHOT_VERSION);
    match prev.get(section).and_then(Value::as_object) {
        Some(m) if ok => (Some(m), false),
        _ => (None, true),
    }
}

fn split_roles(fingerprint: &str) -> BTreeSet<String> {
    fingerprint
        .split(',')
        .filter(|r| !r.is_empty())
        .map(str::to_owned)
        .collect()
}

/// Diffs the stored roster snapshot against `current`. `previous = None` (or an
/// unusable one) primes: no changes. Otherwise reports `joined`, `left` and
/// `roles_changed`; empties / past-ratio shrinks are [`DiffError::Anomaly`].
///
/// # Errors
/// [`DiffError::Anomaly`] -- the snapshot must not advance.
pub fn diff_roster(
    previous: Option<&Value>,
    current: &[Member],
    max_departure_ratio: f64,
    guard_min_size: usize,
) -> Result<RosterDiff, DiffError> {
    let now = dedupe(current, |m| &m.member_id);
    let snapshot = roster_snapshot(&now);
    let (prev, previous_discarded) = usable_section(previous, "members");
    let Some(prev) = prev else {
        return Ok(RosterDiff {
            changes: Vec::new(),
            snapshot,
            primed: true,
            previous_discarded,
        });
    };
    let now_ids: BTreeSet<&str> = now.iter().map(|m| m.member_id.as_str()).collect();
    let departed: Vec<&String> = prev
        .keys()
        .filter(|id| !now_ids.contains(id.as_str()))
        .collect();
    if !prev.is_empty() && now.is_empty() {
        return Err(DiffError::Anomaly("roster emptied"));
    }
    if prev.len() >= guard_min_size
        && departed.len() as f64 / prev.len() as f64 > max_departure_ratio
    {
        return Err(DiffError::Anomaly("roster shrank past the guard ratio"));
    }
    let mut changes = Vec::new();
    for m in &now {
        match prev.get(&m.member_id).and_then(Value::as_str) {
            None => changes.push(RosterChange {
                change: "joined",
                member_id: m.member_id.clone(),
                roles_added: m.role_ids(),
                roles_removed: Vec::new(),
                role_names: m.role_names(),
            }),
            Some(old) if old != m.fingerprint() => {
                let before = split_roles(old);
                let after: BTreeSet<String> = m.role_ids().into_iter().collect();
                changes.push(RosterChange {
                    change: "roles_changed",
                    member_id: m.member_id.clone(),
                    roles_added: after.difference(&before).cloned().collect(),
                    roles_removed: before.difference(&after).cloned().collect(),
                    role_names: m.role_names(),
                });
            }
            Some(_) => {}
        }
    }
    changes.extend(departed.into_iter().map(|id| RosterChange {
        change: "left",
        member_id: id.clone(),
        roles_added: Vec::new(),
        roles_removed: Vec::new(),
        role_names: Vec::new(),
    }));
    Ok(RosterDiff {
        changes,
        snapshot,
        primed: false,
        previous_discarded,
    })
}

/// True if a stored event had not started yet (unknown start counts upcoming).
fn is_upcoming(entry: &Value, now: f64) -> bool {
    match entry.get("start").and_then(Value::as_f64) {
        Some(start) => start > now,
        None => true,
    }
}

/// Diffs the stored event snapshot against `current` at `now` (epoch seconds).
/// Reports `created`, `updated`, `cancelled`, `rsvp_changed` and `removed`; an
/// event that vanishes AFTER its start simply ended and is pruned silently.
///
/// # Errors
/// [`DiffError::Anomaly`] -- a mass vanish of upcoming events.
pub fn diff_events(
    previous: Option<&Value>,
    current: &[OrgEvent],
    now: f64,
    max_departure_ratio: f64,
) -> Result<EventsDiff, DiffError> {
    let events = dedupe(current, |e| &e.event_id);
    let snapshot = events_snapshot(&events);
    let (prev, previous_discarded) = usable_section(previous, "events");
    let Some(prev) = prev else {
        return Ok(EventsDiff {
            changes: Vec::new(),
            snapshot,
            primed: true,
            previous_discarded,
            pruned: 0,
        });
    };
    let now_ids: BTreeSet<&str> = events.iter().map(|e| e.event_id.as_str()).collect();
    let vanished: Vec<&String> = prev
        .keys()
        .filter(|id| !now_ids.contains(id.as_str()))
        .collect();
    let upcoming_vanished: Vec<&String> = vanished
        .iter()
        .copied()
        .filter(|id| prev.get(*id).is_some_and(|e| is_upcoming(e, now)))
        .collect();
    let upcoming_before = prev.values().filter(|e| is_upcoming(e, now)).count();
    if upcoming_before >= EVENT_GUARD_MIN_SIZE
        && upcoming_vanished.len() as f64 / upcoming_before as f64 > max_departure_ratio
    {
        return Err(DiffError::Anomaly("upcoming events vanished in one poll"));
    }
    let mut changes = Vec::new();
    for e in &events {
        let Some(old) = prev.get(&e.event_id) else {
            changes.push(EventChange {
                change: "created",
                event_id: e.event_id.clone(),
                event: Some(e.clone()),
                rsvp_previous: None,
            });
            continue;
        };
        let was_cancelled = old.get("status").and_then(Value::as_str) == Some(STATUS_CANCELLED);
        if e.status == STATUS_CANCELLED && !was_cancelled {
            changes.push(EventChange {
                change: "cancelled",
                event_id: e.event_id.clone(),
                event: Some(e.clone()),
                rsvp_previous: None,
            });
        } else if old.get("fp").and_then(Value::as_str) != Some(e.fingerprint().as_str()) {
            changes.push(EventChange {
                change: "updated",
                event_id: e.event_id.clone(),
                event: Some(e.clone()),
                rsvp_previous: None,
            });
        }
        let old_rsvp = old.get("rsvp").and_then(Value::as_u64);
        if e.rsvp_count.is_some() && e.rsvp_count != old_rsvp {
            changes.push(EventChange {
                change: "rsvp_changed",
                event_id: e.event_id.clone(),
                event: Some(e.clone()),
                rsvp_previous: old_rsvp,
            });
        }
    }
    changes.extend(upcoming_vanished.iter().map(|id| EventChange {
        change: "removed",
        event_id: (*id).clone(),
        event: None,
        rsvp_previous: None,
    }));
    Ok(EventsDiff {
        pruned: vanished.len() - upcoming_vanished.len(),
        changes,
        snapshot,
        primed: false,
        previous_discarded,
    })
}

/// The raw roster-change frame the connector's `parse_frame` consumes. No
/// display name is ever included (PII boundary).
#[must_use]
pub fn roster_frame(change: &RosterChange, source_id: &str, observed_at: &str) -> Vec<u8> {
    json!({
        "kind": "roster",
        "source_id": source_id,
        "change": change.change,
        "member_id": change.member_id,
        "roles_added": change.roles_added,
        "roles_removed": change.roles_removed,
        "role_names": change.role_names,
        "observed_at": observed_at,
    })
    .to_string()
    .into_bytes()
}

/// The raw org-event-change frame the connector's `parse_frame` consumes.
#[must_use]
pub fn event_frame(change: &EventChange, source_id: &str, observed_at: &str) -> Vec<u8> {
    let ev = change.event.as_ref();
    let description = ev
        .and_then(|e| e.description.as_ref())
        .map(|d| d.chars().take(MAX_DESCRIPTION_CHARS).collect::<String>());
    json!({
        "kind": "events",
        "source_id": source_id,
        "change": change.change,
        "event_id": change.event_id,
        "title": ev.and_then(|e| e.title.clone()),
        "description": description,
        "starts_at": ev.and_then(|e| e.starts_epoch).and_then(iso_of),
        "ends_at": ev.and_then(|e| e.ends_epoch).and_then(iso_of),
        "location": ev.and_then(|e| e.location.clone()),
        "organizer_id": ev.and_then(|e| e.organizer_id.clone()),
        "status": ev.map(|e| e.status.clone()),
        "rsvp_count": ev.and_then(|e| e.rsvp_count),
        "rsvp_previous": change.rsvp_previous,
        "observed_at": observed_at,
    })
    .to_string()
    .into_bytes()
}

#[cfg(test)]
mod tests;
