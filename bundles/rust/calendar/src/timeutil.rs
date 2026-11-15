//! Input validation and time handling: RFC 3339 + IANA zones (DST-correct via `chrono-tz`).

use chrono::{DateTime, LocalResult, NaiveDateTime, TimeZone, Utc};
use chrono_tz::Tz;

use crate::host::CalError;

pub const MAX_TITLE: usize = 100;
pub const MAX_DESCRIPTION: usize = 1000;
pub const MAX_LOCATION: usize = 100;
pub const MAX_CHANNEL: usize = 64;

fn bad(msg: impl Into<String>) -> CalError {
    CalError::Validation(msg.into())
}

/// True if `s` holds any ASCII control character (newline, tab, NUL, ...).
pub fn has_control(s: &str) -> bool {
    s.chars().any(|c| c.is_control())
}

/// Validates a single-line text field: non-empty (when `required`), bounded, no control chars.
pub fn check_text(name: &str, s: &str, max: usize, required: bool) -> Result<String, CalError> {
    let t = s.trim();
    if required && t.is_empty() {
        return Err(bad(format!("{name} is required")));
    }
    if t.chars().count() > max {
        return Err(bad(format!("{name} is too long (max {max})")));
    }
    if has_control(t) {
        return Err(bad(format!("{name} contains control characters")));
    }
    Ok(t.to_string())
}

/// Description may contain newlines but no other control characters.
pub fn check_description(s: &str) -> Result<String, CalError> {
    let t = s.trim();
    if t.chars().count() > MAX_DESCRIPTION {
        return Err(bad(format!(
            "description is too long (max {MAX_DESCRIPTION})"
        )));
    }
    if t.chars().any(|c| c.is_control() && c != '\n') {
        return Err(bad("description contains control characters"));
    }
    Ok(t.to_string())
}

/// Parses an IANA zone name (allowlist = the bundled IANA database).
pub fn parse_tz(name: &str) -> Result<Tz, CalError> {
    name.parse::<Tz>()
        .map_err(|_| bad(format!("unknown timezone `{}`", truncate(name, 40))))
}

/// Parses a `when`: RFC 3339 (own offset honoured) or local `YYYY-MM-DD HH:MM` / `...THH:MM`
/// interpreted in `tz`. Ambiguous or non-existent local times (DST) are rejected, not guessed.
pub fn parse_when(s: &str, tz: &Tz) -> Result<i64, CalError> {
    let s = s.trim();
    if let Ok(dt) = DateTime::parse_from_rfc3339(s) {
        return Ok(dt.timestamp());
    }
    for fmt in ["%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"] {
        if let Ok(naive) = NaiveDateTime::parse_from_str(s, fmt) {
            return match tz.from_local_datetime(&naive) {
                LocalResult::Single(dt) => Ok(dt.timestamp()),
                LocalResult::Ambiguous(..) => Err(bad(
                    "that local time is ambiguous (DST); give an RFC 3339 time with offset",
                )),
                LocalResult::None => Err(bad("that local time does not exist (DST gap)")),
            };
        }
    }
    Err(bad(
        "time must be RFC 3339 (2026-12-01T19:00:00Z) or `YYYY-MM-DD HH:MM` in the event timezone",
    ))
}

/// Unix seconds -> RFC 3339 UTC (`Z`).
pub fn to_rfc3339(secs: i64) -> Result<String, CalError> {
    DateTime::<Utc>::from_timestamp(secs, 0)
        .map(|d| d.to_rfc3339_opts(chrono::SecondsFormat::Secs, true))
        .ok_or_else(|| bad("timestamp out of range"))
}

/// Renders `secs` in `tz` for chat (`2026-12-01 19:00 CET`).
pub fn render_local(secs: i64, tz: &Tz) -> String {
    match DateTime::<Utc>::from_timestamp(secs, 0) {
        Some(d) => d.with_timezone(tz).format("%Y-%m-%d %H:%M %Z").to_string(),
        None => "?".to_string(),
    }
}

/// Truncates to at most `max` chars.
pub fn truncate(s: &str, max: usize) -> String {
    s.chars().take(max).collect()
}

/// Neutralizes mass-mention / formatting abuse when user text is echoed back into chat.
pub fn chat_safe(s: &str) -> String {
    s.replace('@', "@\u{200b}")
}

/// Bounds for a sane event window (year 2000 .. 2100).
pub fn check_window(start: i64, end: i64) -> Result<(), CalError> {
    const MIN: i64 = 946_684_800;
    const MAX: i64 = 4_102_444_800;
    if !(MIN..=MAX).contains(&start) || !(MIN..=MAX).contains(&end) {
        return Err(bad("event time is out of range"));
    }
    if end <= start {
        return Err(bad("event must end after it starts"));
    }
    if end - start > 366 * 86_400 {
        return Err(bad("event is longer than a year"));
    }
    Ok(())
}
