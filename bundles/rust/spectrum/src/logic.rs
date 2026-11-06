//! Pure Spectrum connector logic (gh #486): frame -> normalized events.
//!
//! Parity reference: `core/svc_ingest/builtin_handlers/spectrum_ingest.py`
//! `normalize()` and `core/svc_ingest/receivers/spectrum_poll.py`
//! `_item_from_mapping`. Intentional, documented divergences from Python (the
//! v3 PII boundary): the raw RSI handle/member id and display name NEVER appear
//! in `payload_json` -- the component tokenizes the author to a UUID `actor`
//! instead (`Event::actor` is the raw id only until the component resolves it).
//!
//! Spectrum is ONE-WAY INBOUND by design (#101): there is no request builder
//! here and none may be added.

use serde_json::{Map, Value, json};

/// Platform slug.
pub const PLATFORM: &str = "spectrum";
/// Longest org-event description carried downstream (chars), as in Python.
pub const MAX_DESCRIPTION_CHARS: usize = 2000;
/// Largest epoch (seconds) accepted: 9999-12-31T23:59:59Z (Python `datetime` max).
const MAX_EPOCH: f64 = 253_402_300_799.0;

/// Typed failure; mapped by the component onto the WIT `frame-error`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LogicError {
    /// Input is not what the platform contract allows.
    Malformed(String),
    /// Frame shape the connector deliberately does not handle (named, never silent).
    Unsupported(String),
}

/// One normalized event, field-for-field the WIT `normalized-event`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Event {
    /// Platform slug.
    pub platform: String,
    /// `message` | `thread` | `reply` | `member_*` | `org_event_*`.
    pub event_type: String,
    /// Raw RSI member id (tokenized by the component); `None` = anonymous.
    pub actor: Option<String>,
    /// JSON payload text (no raw handle, no display name).
    pub payload_json: String,
    /// RFC 3339 timestamp.
    pub occurred_at: String,
}

/// Result of parsing one frame.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Parsed {
    /// Normalized events, oldest first.
    pub events: Vec<Event>,
    /// Items dropped (no id / no text); a count only, never content.
    pub skipped: usize,
}

/// Mirrors Python `str.strip()` (Unicode whitespace plus 0x1c-0x1f).
fn py_strip(s: &str) -> &str {
    s.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}

/// Python `_s`: a non-empty string, or an integer coerced to a string; else `None`.
fn s_of(v: Option<&Value>) -> Option<String> {
    match v {
        Some(Value::String(s)) if !s.is_empty() => Some(s.clone()),
        Some(Value::Number(n)) if n.is_i64() || n.is_u64() => Some(n.to_string()),
        _ => None,
    }
}

/// Non-empty string or JSON `null` (Python `_as_str_or_none`).
fn str_or_null(v: Option<&Value>) -> Value {
    match v {
        Some(Value::String(s)) if !s.is_empty() => Value::String(s.clone()),
        _ => Value::Null,
    }
}

/// List of non-empty strings; anything else is empty (Python `_as_str_list`).
fn str_list(v: Option<&Value>) -> Value {
    match v {
        Some(Value::Array(a)) => Value::Array(
            a.iter()
                .filter(|x| matches!(x, Value::String(s) if !s.is_empty()))
                .cloned()
                .collect(),
        ),
        _ => Value::Array(Vec::new()),
    }
}

/// Python `_epoch`: int/float/numeric-string seconds; bool and junk are absent.
fn epoch_of(v: Option<&Value>) -> Option<f64> {
    match v {
        Some(Value::Number(n)) => n.as_f64(),
        // A non-numeric string is "no timestamp" (Python `_epoch`); the caller then
        // falls back to the host's `received_at`, which is a stated, tested fallback.
        Some(Value::String(s)) => match s.trim().parse::<f64>() {
            Ok(f) if f.is_finite() => Some(f),
            _ => None,
        },
        _ => None,
    }
}

/// Days since 1970-01-01 -> (year, month, day) (Howard Hinnant's civil algorithm).
fn civil_from_days(z: i64) -> (i64, i64, i64) {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + i64::from(m <= 2);
    (y, m, d)
}

/// Epoch seconds -> Python `datetime.fromtimestamp(x, UTC).isoformat()`
/// (`+00:00`, microseconds only when non-zero); `None` when out of range.
pub fn epoch_to_iso(x: f64) -> Option<String> {
    if !x.is_finite() || !(0.0..=MAX_EPOCH).contains(&x) {
        return None;
    }
    let mut secs = x.floor();
    let mut micros = ((x - secs) * 1_000_000.0).round_ties_even();
    if micros >= 1_000_000.0 {
        secs += 1.0;
        micros = 0.0;
    }
    let secs = secs as i64;
    let micros = micros as i64;
    let (y, mo, d) = civil_from_days(secs.div_euclid(86_400));
    let rem = secs.rem_euclid(86_400);
    let (h, mi, s) = (rem / 3_600, (rem % 3_600) / 60, rem % 60);
    let frac = if micros == 0 {
        String::new()
    } else {
        format!(".{micros:06}")
    };
    Some(format!(
        "{y:04}-{mo:02}-{d:02}T{h:02}:{mi:02}:{s:02}{frac}+00:00"
    ))
}

/// Strict truthiness for `is_reply`: only a real `true` or `1` counts (fail closed;
/// Python used bare `bool()`, under which the string `"false"` would be a reply).
fn strict_flag(v: Option<&Value>) -> bool {
    match v {
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_i64() == Some(1),
        _ => false,
    }
}

/// First non-empty string among `keys`, else the explicit `fallback` (the host's
/// `received_at`); a missing timestamp is a stated fallback, never a silent default.
fn first_str(raw: &Map<String, Value>, keys: &[&str], fallback: &str) -> String {
    for k in keys {
        if let Some(s) = s_of(raw.get(*k)) {
            return s;
        }
    }
    fallback.to_string()
}

fn malformed(msg: &str) -> LogicError {
    LogicError::Malformed(msg.to_string())
}

/// Requires a non-empty string field.
fn require_str<'a>(
    raw: &'a Map<String, Value>,
    key: &str,
    what: &str,
) -> Result<&'a str, LogicError> {
    match raw.get(key) {
        Some(Value::String(s)) if !s.is_empty() => Ok(s),
        _ => Err(LogicError::Malformed(format!(
            "raw Spectrum {what} missing required '{key}' string field"
        ))),
    }
}

/// Normalizes one forum/lobby raw event (the Python `normalize()` message path).
fn normalize_message(raw: &Map<String, Value>, received_at: &str) -> Result<Event, LogicError> {
    let text = match raw.get("text") {
        Some(Value::String(t)) if !py_strip(t).is_empty() => py_strip(t),
        _ => {
            return Err(malformed(
                "raw Spectrum event missing required 'text' string field",
            ));
        }
    };
    let source_id = require_str(raw, "source_id", "event")?;
    let kind = match raw.get("kind") {
        Some(Value::String(k)) if k == "forum" || k == "lobby" => k.as_str(),
        _ => {
            return Err(malformed(
                "raw Spectrum event 'kind' must be forum|lobby|roster|events",
            ));
        }
    };
    let event_type = if kind == "lobby" {
        "message"
    } else if strict_flag(raw.get("is_reply")) {
        "reply"
    } else {
        "thread"
    };
    let created_at = str_or_null(raw.get("created_at"));
    let occurred_at = first_str(raw, &["occurred_at", "created_at"], received_at);
    let payload = json!({
        "text": text,
        "kind": kind,
        "source_id": source_id,
        "thread_id": str_or_null(raw.get("thread_id")),
        "message_id": str_or_null(raw.get("message_id")),
        "created_at": created_at,
    });
    Ok(Event {
        platform: PLATFORM.into(),
        event_type: event_type.into(),
        actor: s_of(raw.get("author_id")),
        payload_json: payload.to_string(),
        occurred_at,
    })
}

/// Normalizes one roster delta (`joined`/`left`/`roles_changed`).
fn normalize_roster(raw: &Map<String, Value>, received_at: &str) -> Result<Event, LogicError> {
    let event_type = match raw.get("change").and_then(Value::as_str) {
        Some("joined") => "member_joined",
        Some("left") => "member_left",
        Some("roles_changed") => "member_roles_changed",
        _ => {
            return Err(malformed(
                "raw Spectrum roster event 'change' must be joined|left|roles_changed",
            ));
        }
    };
    let member_id = require_str(raw, "member_id", "roster event")?;
    let source_id = require_str(raw, "source_id", "event")?;
    let payload = json!({
        "kind": "roster",
        "source_id": source_id,
        "roles_added": str_list(raw.get("roles_added")),
        "roles_removed": str_list(raw.get("roles_removed")),
        "role_names": str_list(raw.get("role_names")),
    });
    Ok(Event {
        platform: PLATFORM.into(),
        event_type: event_type.into(),
        actor: Some(member_id.to_string()),
        payload_json: payload.to_string(),
        occurred_at: first_str(raw, &["observed_at"], received_at),
    })
}

/// Integer-or-null (Python `isinstance(x, int)`; bools are not counts here).
fn int_or_null(v: Option<&Value>) -> Value {
    match v {
        Some(Value::Number(n)) if n.is_i64() || n.is_u64() => Value::Number(n.clone()),
        _ => Value::Null,
    }
}

/// Normalizes one org-event delta (`created`/`updated`/...).
fn normalize_org_event(raw: &Map<String, Value>, received_at: &str) -> Result<Event, LogicError> {
    let event_type = match raw.get("change").and_then(Value::as_str) {
        Some("created") => "org_event_created",
        Some("updated") => "org_event_updated",
        Some("cancelled") => "org_event_cancelled",
        Some("removed") => "org_event_removed",
        Some("rsvp_changed") => "org_event_rsvp_changed",
        _ => {
            return Err(malformed(
                "raw Spectrum event 'change' must be created|updated|cancelled|removed|rsvp_changed",
            ));
        }
    };
    let event_id = require_str(raw, "event_id", "org event")?;
    let source_id = require_str(raw, "source_id", "event")?;
    let payload = json!({
        "kind": "events",
        "source_id": source_id,
        "event_id": event_id,
        "title": str_or_null(raw.get("title")),
        "description": match raw.get("description") {
            Some(Value::String(d)) if !d.is_empty() => {
                Value::String(d.chars().take(MAX_DESCRIPTION_CHARS).collect())
            }
            _ => Value::Null,
        },
        "starts_at": str_or_null(raw.get("starts_at")),
        "ends_at": str_or_null(raw.get("ends_at")),
        "location": str_or_null(raw.get("location")),
        "status": str_or_null(raw.get("status")),
        "rsvp_count": int_or_null(raw.get("rsvp_count")),
        "rsvp_previous": int_or_null(raw.get("rsvp_previous")),
    });
    Ok(Event {
        platform: PLATFORM.into(),
        event_type: event_type.into(),
        actor: s_of(raw.get("organizer_id")),
        payload_json: payload.to_string(),
        occurred_at: first_str(raw, &["observed_at"], received_at),
    })
}

/// Maps one RSI forum thread / lobby message dict to a raw-event map, or `None`
/// when it has no id or no text (skipped, counted -- one bad item must not poison
/// the window). Python `_item_from_mapping`.
fn item_to_raw(
    item: &Value,
    kind: &str,
    source_id: &str,
) -> Option<(f64, String, Map<String, Value>)> {
    let obj = item.as_object()?;
    let id = s_of(obj.get("id"))?;
    let text = ["plaintext", "body", "subject"]
        .iter()
        .find_map(|k| s_of(obj.get(*k)))?;
    let member = obj.get("member").and_then(Value::as_object);
    let author = member
        .and_then(|m| s_of(m.get("id")))
        .or_else(|| s_of(obj.get("member_id")));
    let created = epoch_of(obj.get("time_created"));
    let is_reply = strict_flag(obj.get("is_reply")) || s_of(obj.get("parent_id")).is_some();
    let mut m = Map::new();
    m.insert("kind".into(), json!(kind));
    m.insert("source_id".into(), json!(source_id));
    m.insert("text".into(), json!(py_strip(&text)));
    m.insert("message_id".into(), json!(id));
    m.insert(
        "thread_id".into(),
        s_of(obj.get("thread_id")).map_or(Value::Null, Value::String),
    );
    m.insert("is_reply".into(), json!(is_reply));
    m.insert(
        "author_id".into(),
        author.map_or(Value::Null, Value::String),
    );
    m.insert(
        "created_at".into(),
        created
            .and_then(epoch_to_iso)
            .map_or(Value::Null, Value::String),
    );
    // Items with no timestamp sort first (Python `created_epoch or 0.0`).
    #[allow(clippy::manual_unwrap_or)] // explicit match: house rule bans unwrap_or* defaults
    let sort_key = match created {
        Some(c) => c,
        None => 0.0,
    };
    Some((sort_key, id, m))
}

/// Parses an RSI `{success, code, data}` poll response. `source_id` is the host's
/// opaque id for the polled source (the response does not carry it).
fn parse_envelope(
    obj: &Map<String, Value>,
    received_at: &str,
    source_id: &str,
) -> Result<Parsed, LogicError> {
    if obj.get("success").and_then(Value::as_i64) != Some(1) {
        // Only the (non-secret) code is named; the body is upstream content.
        let code = match s_of(obj.get("code")) {
            Some(c) => c,
            None => "unknown".to_string(),
        };
        return Err(LogicError::Malformed(format!(
            "spectrum envelope reported failure code={code}"
        )));
    }
    let data = obj
        .get("data")
        .and_then(Value::as_object)
        .ok_or_else(|| malformed("spectrum envelope has no 'data' object"))?;
    let (kind, list) = match (data.get("threads"), data.get("messages")) {
        (Some(Value::Array(t)), None) => ("forum", t),
        (None, Some(Value::Array(m))) => ("lobby", m),
        (Some(_), Some(_)) => {
            return Err(malformed(
                "spectrum envelope carries both 'threads' and 'messages'",
            ));
        }
        _ => {
            return Err(LogicError::Unsupported(
                "spectrum envelope has no 'threads'/'messages' list (roster/event snapshots \
                 need the host snapshot diff and arrive as raw change frames)"
                    .into(),
            ));
        }
    };
    let mut skipped = 0usize;
    let mut mapped = Vec::with_capacity(list.len());
    for item in list {
        match item_to_raw(item, kind, source_id) {
            None => skipped += 1,
            Some(t) => mapped.push(t),
        }
    }
    // Oldest first so downstream ordering matches Spectrum's own timeline.
    mapped.sort_by(|a, b| a.0.total_cmp(&b.0).then_with(|| a.1.cmp(&b.1)));
    let events = mapped
        .iter()
        .map(|(_, _, raw)| normalize_message(raw, received_at))
        .collect::<Result<Vec<_>, _>>()?;
    Ok(Parsed { events, skipped })
}

/// Parses one frame: an RSI poll response (`success`/`data` envelope -- what the
/// host poll transport yields) or a receiver-raw event/change dict (`kind` present).
pub fn parse_frame(bytes: &[u8], received_at: &str, source_id: &str) -> Result<Parsed, LogicError> {
    let v: Value = serde_json::from_slice(bytes).map_err(|_| malformed("frame is not JSON"))?;
    let obj = v
        .as_object()
        .ok_or_else(|| malformed("frame is not a JSON object"))?;
    if obj.contains_key("success") {
        return parse_envelope(obj, received_at, source_id);
    }
    let event = match obj.get("kind").and_then(Value::as_str) {
        Some("roster") => normalize_roster(obj, received_at)?,
        Some("events") => normalize_org_event(obj, received_at)?,
        Some(_) => normalize_message(obj, received_at)?,
        None => {
            return Err(malformed(
                "frame is neither a spectrum poll response nor a raw event",
            ));
        }
    };
    Ok(Parsed {
        events: vec![event],
        skipped: 0,
    })
}
