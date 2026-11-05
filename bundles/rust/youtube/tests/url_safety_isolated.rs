//! Each URL-significant character rejected IN ISOLATION by the id/token allowlists, so a
//! second hostile character in a fixture can never mask a widened allowlist.

use waddle_bundle_youtube_service::logic::{is_valid_id, is_valid_page_token};

const HOSTILE: [char; 22] = [
    '&', '=', '#', '%', '/', '?', '+', ' ', '"', '\'', '<', '>', '\\', ';', ':', '@', '!', '{',
    '}', '\u{e9}', '\u{4e16}', '\u{0}',
];

#[test]
fn ids_reject_each_hostile_char_alone() {
    for c in HOSTILE {
        assert!(!is_valid_id(&format!("a{c}b")), "U+{:04X}", u32::from(c));
    }
    assert!(!is_valid_id("a.b"), "dot is not an id char");
    assert!(!is_valid_id("a=b"), "equals is not an id char");
}

#[test]
fn tokens_reject_each_hostile_char_alone() {
    for c in HOSTILE.iter().filter(|c| **c != '=') {
        assert!(
            !is_valid_page_token(&format!("a{c}b")),
            "U+{:04X}",
            u32::from(*c)
        );
    }
    assert!(is_valid_page_token("a=b.c"));
}
