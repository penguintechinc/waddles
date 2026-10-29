//! Byte-exact re-implementation of Python's
//! `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)`.
//!
//! This is the serialization the tamper-evident audit chain hashes, so it must match CPython
//! to the byte: keys sorted by code point, no whitespace, every character outside
//! `0x20..=0x7e` escaped (a backslash-u escape with four lowercase hex digits, non-BMP as a
//! UTF-16 surrogate pair), and the
//! short escapes `\" \\ \n \r \t \b \f`. `serde_json::to_string` cannot be used: it emits raw
//! UTF-8 and escapes `0x7f`/U+2028 differently.
//!
//! Floats are refused (fail-closed): audited details are validated to bool/int/str/None/list,
//! and Python's float `repr` is deliberately not re-implemented.

use serde_json::Value;

/// Why a value could not be serialized canonically.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum PyJsonError {
    /// A number that is not an `i64`/`u64` (float or out-of-range integer).
    #[error("non-integer or out-of-range JSON number is not canonically serializable")]
    UnsupportedNumber,
}

/// Appends `s` as a JSON string literal with Python `ensure_ascii` escaping.
pub fn write_string(out: &mut String, s: &str) {
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            ' '..='~' => out.push(ch),
            _ => {
                let mut units = [0u16; 2];
                for unit in ch.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
        }
    }
    out.push('"');
}

fn write_value(out: &mut String, value: &Value) -> Result<(), PyJsonError> {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                out.push_str(&i.to_string());
            } else if let Some(u) = n.as_u64() {
                out.push_str(&u.to_string());
            } else {
                return Err(PyJsonError::UnsupportedNumber);
            }
        }
        Value::String(s) => write_string(out, s),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_value(out, item)?;
            }
            out.push(']');
        }
        Value::Object(map) => {
            // Sort explicitly (code-point order == UTF-8 byte order), never relying on the
            // map's own iteration order.
            let mut entries: Vec<(&String, &Value)> = map.iter().collect();
            entries.sort_by(|a, b| a.0.cmp(b.0));
            out.push('{');
            for (i, (k, v)) in entries.into_iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_string(out, k);
                out.push(':');
                write_value(out, v)?;
            }
            out.push('}');
        }
    }
    Ok(())
}

/// Serializes `value` exactly as Python's compact, sorted, ASCII-escaped `json.dumps`.
pub fn dumps_canonical(value: &Value) -> Result<String, PyJsonError> {
    let mut out = String::new();
    write_value(&mut out, value)?;
    Ok(out)
}
