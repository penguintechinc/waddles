//! Payload sanitization for bundle-published surface frames.
//!
//! Surfaces carry structured, plain-text data only. Before schema validation
//! every string is checked for control characters (rejected -- they have no
//! business in a title) and any `{user:<token>}` placeholder is replaced by
//! the neutral label: viewers of a surface never see a user reference, a
//! resolved name, or a UUID (PII-free by construction, no hub-api lookup).
//! Strings are NOT HTML-escaped: consumers render them as text.

use std::collections::HashMap;

use egress_detokenizer::{detokenize, Sink};
use serde_json::Value;

/// Why a payload was refused before validation.
#[derive(Debug, PartialEq, Eq, thiserror::Error)]
pub enum SanitizeError {
    /// A string held a control character at this JSON-pointer location.
    #[error("control character in string at {0}")]
    ControlCharacter(String),
}

/// Maximum nesting depth walked; deeper payloads are refused as malformed.
const MAX_DEPTH: usize = 16;

/// Sanitizes `value` in place of a copy; returns the cleaned value.
pub fn sanitize(value: Value) -> Result<Value, SanitizeError> {
    walk(value, String::new(), 0)
}

fn walk(value: Value, path: String, depth: usize) -> Result<Value, SanitizeError> {
    if depth > MAX_DEPTH {
        return Err(SanitizeError::ControlCharacter(path));
    }
    match value {
        Value::String(s) => {
            if s.chars().any(char::is_control) {
                return Err(SanitizeError::ControlCharacter(path));
            }
            if s.contains("{user:") {
                // Sink::Twitch strips control chars from the *substituted*
                // name only; with an empty name map every token becomes the
                // neutral label, so no name is ever involved.
                let out = detokenize(&s, &HashMap::new(), Sink::Twitch);
                Ok(Value::String(out.text))
            } else {
                Ok(Value::String(s))
            }
        }
        Value::Array(items) => items
            .into_iter()
            .enumerate()
            .map(|(i, v)| walk(v, format!("{path}/{i}"), depth + 1))
            .collect::<Result<Vec<_>, _>>()
            .map(Value::Array),
        Value::Object(map) => map
            .into_iter()
            .map(|(k, v)| {
                let child = format!("{path}/{k}");
                walk(v, child, depth + 1).map(|v| (k, v))
            })
            .collect::<Result<serde_json::Map<_, _>, _>>()
            .map(Value::Object),
        other => Ok(other),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn user_tokens_become_the_neutral_label() {
        let v = sanitize(json!({"t": "hi {user:11111111-1111-1111-1111-111111111111}!"})).unwrap();
        assert_eq!(v["t"], "hi Unknown User!");
    }

    #[test]
    fn markup_is_left_untouched_because_consumers_render_text() {
        let v = sanitize(json!({"t": "<script>x</script>"})).unwrap();
        assert_eq!(v["t"], "<script>x</script>");
    }

    #[test]
    fn control_characters_are_rejected_with_a_location() {
        let err = sanitize(json!({"a": ["ok", "bad\u{0007}"]})).unwrap_err();
        assert_eq!(err, SanitizeError::ControlCharacter("/a/1".into()));
        assert!(sanitize(json!({"k": "line\nbreak"})).is_err());
    }

    #[test]
    fn excessive_depth_is_refused() {
        let mut v = json!("x");
        for _ in 0..40 {
            v = json!([v]);
        }
        assert!(sanitize(v).is_err());
    }

    #[test]
    fn non_strings_pass_through() {
        let v = json!({"n": 1, "b": true, "z": null});
        assert_eq!(sanitize(v.clone()).unwrap(), v);
    }
}
