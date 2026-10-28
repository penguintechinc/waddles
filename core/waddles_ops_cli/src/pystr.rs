//! Python `str` semantics that differ from Rust's `str`, needed for byte-parity.

/// True for characters Python's `str.isspace()` accepts.
///
/// Rust's `char::is_whitespace` (Unicode `White_Space`) lacks U+001C..U+001F, which Python
/// treats as whitespace (bidi class B/S). Everything else is identical.
pub fn is_py_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// Python `str.strip()` with no arguments.
pub fn py_strip(s: &str) -> &str {
    s.trim_matches(is_py_space)
}

/// Python `int(text)` for the ASCII forms argparse's `type=int` accepts: surrounding
/// whitespace, an optional sign, digits with single underscores between digits.
pub fn py_int(text: &str) -> Option<i128> {
    let t = py_strip(text);
    let (neg, digits) = match t.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, t.strip_prefix('+').unwrap_or(t)),
    };
    if digits.is_empty()
        || digits.starts_with('_')
        || digits.ends_with('_')
        || digits.contains("__")
        || !digits.bytes().all(|b| b.is_ascii_digit() || b == b'_')
    {
        return None;
    }
    let cleaned: String = digits.chars().filter(|c| *c != '_').collect();
    let v: i128 = cleaned.parse().ok()?;
    Some(if neg { -v } else { v })
}
