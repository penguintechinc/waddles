//! Every forbidden character is rejected IN ISOLATION (no `{{`/`}}` present), so no
//! other guard can mask a missing entry in the character table (mutation-checked).

use serde_json::json;
use waddle_bundle_youtube_service::logic::{LogicError, build_chat_send};

#[test]
fn every_forbidden_char_is_rejected_in_isolation() {
    let mut chars: Vec<char> = (0u32..0x20).filter_map(char::from_u32).collect();
    chars.extend([
        '\u{7f}', '\u{80}', '\u{85}', '\u{9f}', '\u{ad}', '\u{61c}', '\u{180e}',
    ]);
    chars.extend(('\u{200b}'..='\u{200f}').chain('\u{2028}'..='\u{202e}'));
    chars.extend(('\u{2060}'..='\u{2069}').chain(['\u{feff}']));
    chars.extend(['\u{fff9}', '\u{fffa}', '\u{fffb}']);
    chars.extend(['\u{ff5b}', '\u{ff5d}', '\u{2774}', '\u{2775}']);
    chars.extend(['\u{fe5b}', '\u{fe5c}', '\u{2983}', '\u{2984}']);
    for c in chars {
        let text = format!("a{c}b");
        let r = build_chat_send("chat.send", "c", &json!({"text": text}).to_string());
        assert!(
            matches!(r, Err(LogicError::Malformed(_))),
            "U+{:04X} must be rejected",
            u32::from(c)
        );
    }
}
