//! Length bounds for rendered output (security review follow-up on PR
//! #427, MEDIUM: an unbounded resolved name or unbounded total payload is
//! itself a risk against a downstream sink with its own hard limit --
//! e.g. Discord silently truncating/rejecting an over-length message, or a
//! crafted PII-store display name used to visually flood a chat/overlay).
//! All truncation here is grapheme-cluster-safe (`unicode-segmentation`);
//! [`crate::render`] layers atom-boundary safety on top so the *escaped*
//! output is never cut mid-escape-sequence either.

use unicode_segmentation::UnicodeSegmentation;

/// Maximum length, in grapheme clusters, of a single resolved display name
/// before [`truncate_graphemes`] shortens it. Applied to the RAW name,
/// before any sink escaping -- so this truncation itself can never land
/// mid-escape-sequence (there isn't one yet at this point).
pub const NAME_MAX_GRAPHEMES: usize = 64;

/// Appended once when truncation actually occurs (name-level or
/// total-output-level) -- a single `char`, so it never itself needs
/// grapheme-splitting logic.
pub const TRUNCATION_INDICATOR: char = '\u{2026}'; // "…"

/// Truncates `s` to at most `max_graphemes` grapheme clusters, appending
/// [`TRUNCATION_INDICATOR`] (counted within the budget, so the visible
/// result never exceeds `max_graphemes`) if truncation actually occurred.
/// A no-op, byte-for-byte, when `s` already fits.
pub fn truncate_graphemes(s: &str, max_graphemes: usize) -> String {
    let graphemes: Vec<&str> = s.graphemes(true).collect();
    if graphemes.len() <= max_graphemes {
        return s.to_string();
    }
    let keep = max_graphemes.saturating_sub(1);
    let mut out: String = graphemes[..keep].concat();
    out.push(TRUNCATION_INDICATOR);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn short_name_is_unchanged() {
        assert_eq!(truncate_graphemes("Alice", 64), "Alice");
    }

    #[test]
    fn exact_length_is_unchanged() {
        let s = "a".repeat(64);
        assert_eq!(truncate_graphemes(&s, 64), s);
    }

    #[test]
    fn over_length_is_truncated_with_indicator() {
        let s = "a".repeat(100);
        let out = truncate_graphemes(&s, 64);
        assert_eq!(out.chars().count(), 64);
        assert!(out.ends_with('\u{2026}'));
        assert_eq!(&out[..63], &"a".repeat(63));
    }

    #[test]
    fn never_splits_a_multi_codepoint_grapheme_cluster() {
        // "family: man, woman, girl, boy" emoji -- one grapheme cluster
        // built from multiple codepoints joined by ZWJ. 64 plain graphemes
        // followed by this one pushes the total to 65, forcing truncation
        // right at the cluster's boundary -- a naive char-count truncation
        // would slice partway through its codepoints and produce a broken/
        // invalid rendering (e.g. an orphaned ZWJ or a lone base emoji).
        let family = "\u{1F468}\u{200D}\u{1F469}\u{200D}\u{1F467}\u{200D}\u{1F466}";
        let s = format!("{}{family}", "a".repeat(64));
        let out = truncate_graphemes(&s, 64);
        // The whole cluster is dropped for the indicator -- never a
        // partial codepoint sequence -- and no lone surrogate/ZWJ residue
        // from `family` appears anywhere in the output.
        assert_eq!(out, format!("{}\u{2026}", "a".repeat(63)));
        assert!(!out.contains('\u{200D}'), "no orphaned ZWJ may survive");
    }
}
