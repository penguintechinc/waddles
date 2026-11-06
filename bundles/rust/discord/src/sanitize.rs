//! Input guard for every string that can reach an `http-request-tpl` or a
//! handshake payload (message text, target ids, user placeholders).
//!
//! The host substitutes `{{SLOT}}` credential slots into outbound bytes. A
//! guard that strips control characters and THEN looks for `{{` is bypassable
//! (`{<ctrl>{SECRET}}` becomes a live slot after cleaning). This guard never
//! strips or normalizes: it REJECTS control / zero-width / format / bidi /
//! lookalike-brace characters with a typed error, then rejects `{{` and `}}`
//! on both the raw string and a whitespace-collapsed copy (so `{\n{` and
//! `{ {` cannot hide a slot from a downstream that trims).

use crate::error::{ConnectorError, Result};

/// True for zero-width, soft-hyphen, bidi-control, invisible-operator and BOM
/// characters, and for brace lookalikes (fullwidth, ornamental, math).
fn is_forbidden_format(c: char) -> bool {
    matches!(
        c,
        '\u{00AD}'
            | '\u{180E}'
            | '\u{200B}'..='\u{200F}'
            | '\u{202A}'..='\u{202E}'
            | '\u{2060}'..='\u{2064}'
            | '\u{2066}'..='\u{2069}'
            | '\u{FEFF}'
            | '\u{FF5B}' | '\u{FF5D}'
            | '\u{2774}' | '\u{2775}'
            | '\u{2983}' | '\u{2984}'
            | '\u{FE5B}' | '\u{FE5C}'
    )
}

/// Validates `s` for use in an outbound template. `allow_newlines` permits
/// `\n` and `\t` (multi-line chat text only); every other control character is
/// rejected.
///
/// # Errors
/// [`ConnectorError::Malformed`] naming the field and the rule (never the value).
pub fn guard_text(field: &str, s: &str, allow_newlines: bool) -> Result<()> {
    for c in s.chars() {
        let newline_ok = allow_newlines && (c == '\n' || c == '\t');
        if (c.is_control() && !newline_ok) || is_forbidden_format(c) {
            return Err(ConnectorError::Malformed(format!(
                "`{field}` contains a control, invisible or lookalike character"
            )));
        }
    }
    let collapsed: String = s.chars().filter(|c| !c.is_whitespace()).collect();
    for candidate in [s, collapsed.as_str()] {
        if candidate.contains("{{") || candidate.contains("}}") {
            return Err(ConnectorError::Malformed(format!(
                "`{field}` contains a credential-slot delimiter"
            )));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bad(s: &str) -> bool {
        guard_text("f", s, true).is_err()
    }

    #[test]
    fn plain_and_multiline_text_pass() {
        assert!(guard_text("f", "hello { world } é 😀\nline2\ttab", true).is_ok());
        assert!(!bad("single { brace } fine"));
    }

    #[test]
    fn a_control_char_between_braces_is_rejected_not_stripped() {
        // regression: the Twitch connector HIGH -- `{<ctrl>{SECRET}}` after strip-then-check.
        for c in [
            '\u{0}', '\u{1}', '\u{7}', '\u{1B}', '\u{7F}', '\u{85}', '\r',
        ] {
            assert!(bad(&format!("{{{c}{{SECRET}}}}")), "{c:?}");
            assert!(guard_text("f", &format!("a{c}b"), false).is_err(), "{c:?}");
        }
    }

    #[test]
    fn zero_width_bidi_and_lookalike_braces_are_rejected() {
        for c in [
            '\u{200B}', '\u{200D}', '\u{200E}', '\u{202E}', '\u{2060}', '\u{2066}', '\u{FEFF}',
            '\u{00AD}', '\u{FF5B}', '\u{FF5D}', '\u{2774}', '\u{2775}', '\u{2983}', '\u{FE5B}',
        ] {
            assert!(bad(&format!("{{{c}{{SECRET}}}}")), "{c:?}");
            assert!(bad(&format!("x{c}y")), "{c:?}");
        }
    }

    #[test]
    fn slot_delimiters_are_rejected_raw_and_whitespace_collapsed() {
        for s in [
            "{{X}}",
            "{{",
            "}}",
            "{\n{X}\n}",
            "{ {X} }",
            "{\t{X}}",
            "ok }\n} ok",
        ] {
            assert!(bad(s), "{s:?}");
        }
    }

    #[test]
    fn newlines_are_only_allowed_when_asked() {
        assert!(guard_text("f", "a\nb", false).is_err());
        assert!(guard_text("f", "a\nb", true).is_ok());
    }
}
