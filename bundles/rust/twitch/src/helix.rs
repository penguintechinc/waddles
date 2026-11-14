//! Shared plumbing for the clip / schedule / live tools: schema-strict argument parsing,
//! the host-op shape, and the coded error mapping every tool of this extension uses.
//!
//! Error contract (all tools): failures are `ToolError::InvalidArgs("<code>[: detail]")` with
//! `code` in [`CODES`]; there is never an empty-success fallback.

use serde_json::{Value, json};
use waddle_sdk::tool::ToolError;

use crate::tools::HelixHost;

/// Stable error codes shared by the clip, schedule and live tools.
pub const CODES: &[&str] = &[
    "not_live",
    "rate_limited",
    "not_connected",
    "clips_disabled",
    "platform_unavailable",
];

/// One validated host-executed tool call, ready for the host to queue as an outbound op.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HostOp {
    /// Outbound kind (`clip.create`, `schedule.update`, ...).
    pub kind: &'static str,
    /// Producer-envelope fields: only `text` (JSON-object text of the parameters). Never a
    /// channel / broadcaster / moderator.
    pub payload: Value,
}

/// `InvalidArgs("<code>: <detail>")`.
pub fn coded(code: &str, detail: &str) -> ToolError {
    debug_assert!(CODES.contains(&code));
    ToolError::InvalidArgs(format!("{code}: {detail}"))
}

/// Plain schema violation (not one of the platform codes).
pub fn bad(msg: &str) -> ToolError {
    ToolError::InvalidArgs(msg.to_string())
}

/// Maps a non-2xx Helix status to its code: 401/403 `not_connected`, 429 `rate_limited`,
/// anything else `platform_unavailable`.
pub fn status_error(status: u16) -> ToolError {
    match status {
        401 | 403 => coded("not_connected", "twitch refused the connection credential"),
        429 => coded("rate_limited", "twitch rate limited the request"),
        s => coded("platform_unavailable", &format!("twitch answered {s}")),
    }
}

/// Guarded GET returning `(status, parsed JSON)`; a transport failure or a non-JSON 2xx is
/// `platform_unavailable`.
pub fn get_json(host: &dyn HelixHost, url: &str) -> Result<(u16, Value), ToolError> {
    let (status, body) = host.get(url).map_err(|e| match e {
        ToolError::PoolExhausted => coded("rate_limited", "request budget exhausted"),
        ToolError::NotGranted | ToolError::Forbidden => {
            coded("not_connected", "egress or credential not granted")
        }
        _ => coded("platform_unavailable", "twitch is unreachable"),
    })?;
    if (200..300).contains(&status) {
        let v = serde_json::from_slice(&body)
            .map_err(|_| coded("platform_unavailable", "twitch reply is not JSON"))?;
        Ok((status, v))
    } else {
        Ok((status, json!(null)))
    }
}

/// Requires `args` to be an object with only `allowed` keys; destination keys are refused by
/// name so the message says why.
pub fn object<'a>(
    args: &'a Value,
    allowed: &[&str],
) -> Result<&'a serde_json::Map<String, Value>, ToolError> {
    for k in crate::sender::IGNORED_TARGET_KEYS {
        if args.get(*k).is_some() {
            return Err(ToolError::InvalidArgs(format!(
                "{k} is host-pinned and may not be supplied"
            )));
        }
    }
    let obj = args.as_object().ok_or_else(|| bad("args must be an object"))?;
    if let Some(k) = obj.keys().find(|k| !allowed.contains(&k.as_str())) {
        return Err(ToolError::InvalidArgs(format!("unknown argument {k}")));
    }
    Ok(obj)
}

/// A string value of `1..=max` chars with no control/invisible characters or brace pairs.
pub fn text_arg(v: &Value, label: &str, max: usize) -> Result<String, ToolError> {
    let s = v
        .as_str()
        .ok_or_else(|| ToolError::InvalidArgs(format!("{label} must be a string")))?;
    if s.is_empty() || s.chars().count() > max {
        return Err(ToolError::InvalidArgs(format!(
            "{label} must be 1..={max} characters"
        )));
    }
    crate::sender::reject_unsafe(label, s)
        .map_err(|_| ToolError::InvalidArgs(format!("{label} contains unsafe characters")))?;
    Ok(s.to_string())
}

/// `[A-Za-z0-9_-]{1,100}` (a Twitch clip id).
pub fn is_clip_id(s: &str) -> bool {
    (1..=100).contains(&s.len())
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

/// Truncates to at most `max` characters (Helix titles may exceed the contract's bound).
pub fn clip_chars(s: &str, max: usize) -> String {
    s.chars().take(max).collect()
}

/// Visible-ASCII RFC 3339-ish timestamp characters, bounded; the platform validates the value.
pub fn is_timestamp(s: &str) -> bool {
    (10..=40).contains(&s.len())
        && s.bytes()
            .all(|b| b.is_ascii_digit() || matches!(b, b'T' | b'Z' | b':' | b'-' | b'+' | b'.'))
}
