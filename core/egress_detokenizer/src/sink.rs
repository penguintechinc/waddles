//! Per-sink escaping (spec S10.4: "each sink's own escaping rules...not one
//! shared escaper assumed safe everywhere"), plus the security-review
//! follow-ups on PR #427:
//!
//! - **HIGH**: Twitch/IRC client-side commands (`/ban`, `.mods`, `/me`,
//!   ...) are recognized only when a message/line literally starts with
//!   `/` or `.` -- neutralized at message start, after any newline, and
//!   (regardless of position, since a resolved name is less trusted than
//!   ordinary bundle text) at the start of every substituted name. See
//!   `crate::render`'s `twitch_neutralize_command_starts`, which needs
//!   cross-segment state this module doesn't have.
//! - **LOW**: stray (non-placeholder) `{`/`}` are left as plain literal
//!   text for the two chat sinks -- the single-pass scanner has already
//!   determined they're not a real placeholder, and neither Twitch nor
//!   Discord treats braces specially, so entity-escaping them there was
//!   unnecessary and visually confusing. Only the overlay/HTML sink still
//!   entity-escapes them (defense in depth for a DOM context).
//! - **LOW**: Discord's `<...>` custom-token opener (`<@id>`, `<@&id>`,
//!   `<#id>`, `<:name:id>`, `<t:...>`, etc.) is neutralized by inserting a
//!   zero-width space right after every literal `<` -- one fix for the
//!   whole token family rather than pattern-matching each variant.
//!
//! Escaping is atom-based ([`Atoms`]): every grapheme cluster of the raw
//! input maps to exactly one output atom (itself or an escaped/neutralized
//! replacement), so length-capping in `crate::render` can truncate at an
//! atom boundary and never split a grapheme cluster or an escape sequence
//! (security review MEDIUM finding).

use unicode_segmentation::UnicodeSegmentation;

/// One indivisible rendered unit -- a single grapheme cluster of the raw
/// input, rendered as itself or as a complete escape/neutralization
/// sequence. [`crate::render`] concatenates and length-caps a `Vec` of
/// these without ever splitting one apart.
pub(crate) type Atoms = Vec<String>;

/// The egress point a rendered payload is headed to. Each variant owns a
/// distinct escaping strategy -- see [`Sink::render_atoms`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Sink {
    /// Twitch IRC chat. No content escaping (IRC has no markup syntax) --
    /// `crate::render`'s command-start neutralization is the one Twitch-
    /// specific transform, layered on top of this sink's atoms.
    ChatTwitch,
    /// Discord chat, sent as the REST message `content` field. Discord
    /// interprets a rich markdown/mention/custom-token grammar there, so
    /// markdown formatting characters, raw `@` mentions, and the `<...>`
    /// custom-token opener are all neutralized.
    ChatDiscord,
    /// The browser-source overlay's DOM/HTML context. Standard HTML-entity
    /// escaping -- the sandboxed-iframe/CSP layer (spec S10.4 table) is a
    /// second, independent control, not a substitute for this one.
    Overlay,
}

/// Discord markdown formatting characters (backslash-escaped to render as
/// literal text rather than being interpreted as bold/italic/strike/code/
/// blockquote/spoiler markup).
const DISCORD_MARKDOWN_SPECIALS: &[char] = &['\\', '*', '_', '~', '`', '|', '>'];

/// A grapheme cluster that is exactly one `char` (every special character
/// this module checks for is ASCII, hence always a single-codepoint
/// cluster) -- `None` for a multi-codepoint cluster, which can never match
/// any of these rules and passes through verbatim.
fn single_char(grapheme: &str) -> Option<char> {
    let mut chars = grapheme.chars();
    let c = chars.next()?;
    if chars.next().is_none() {
        Some(c)
    } else {
        None
    }
}

impl Sink {
    /// This sink's default total-output length cap, in rendered `char`s
    /// (spec follow-up MEDIUM finding: Discord 2000, Twitch 500, overlay
    /// 500-but-configurable). Used by [`crate::render::Detokenizer::
    /// render`]; callers needing a different overlay budget use
    /// [`crate::render::Detokenizer::render_with_limit`] directly.
    pub(crate) fn default_max_len(self) -> usize {
        match self {
            Sink::ChatTwitch => 500,
            Sink::ChatDiscord => 2000,
            Sink::Overlay => 500,
        }
    }

    /// Renders `raw` (already-truncated to [`crate::limits::
    /// NAME_MAX_GRAPHEMES`] if it's a resolved name -- see `render.rs`) as
    /// one atom per grapheme cluster, applying this sink's escaping rules.
    pub(crate) fn render_atoms(self, raw: &str) -> Atoms {
        raw.graphemes(true)
            .map(|g| self.render_grapheme(g))
            .collect()
    }

    fn render_grapheme(self, g: &str) -> String {
        match self {
            Sink::ChatTwitch => g.to_string(),
            Sink::ChatDiscord => Self::render_discord_grapheme(g),
            Sink::Overlay => Self::render_overlay_grapheme(g),
        }
    }

    fn render_discord_grapheme(g: &str) -> String {
        if let Some(c) = single_char(g) {
            if DISCORD_MARKDOWN_SPECIALS.contains(&c) {
                return format!("\\{c}");
            }
            if c == '@' {
                // Zero-width space immediately after '@' breaks Discord's
                // mention tokenizer (`@everyone`/`@here`/`<@id>`'s leading
                // `@`) while leaving the text visually unchanged.
                return format!("@{}", '\u{200B}');
            }
            if c == '<' {
                // Every Discord custom-token opener (`<@id>`, `<@&id>`,
                // `<#id>`, `<:name:id>`, `<t:...>`, ...) begins with a
                // literal '<' -- neutralizing that one character closes
                // the whole family at once (module doc).
                return format!("<{}", '\u{200B}');
            }
        }
        g.to_string()
    }

    fn render_overlay_grapheme(g: &str) -> String {
        if let Some(c) = single_char(g) {
            match c {
                '&' => return "&amp;".to_string(),
                '<' => return "&lt;".to_string(),
                '>' => return "&gt;".to_string(),
                '"' => return "&quot;".to_string(),
                '\'' => return "&#x27;".to_string(),
                // Stray (non-placeholder) braces: entity-escaped only for
                // the overlay/DOM sink (module doc's LOW-severity fix) --
                // not that braces are HTML-dangerous, but as the same
                // belt-and-suspenders defense the sandboxed-iframe/CSP
                // layer represents for this sink specifically.
                '{' => return "&#123;".to_string(),
                '}' => return "&#125;".to_string(),
                _ => {}
            }
        }
        g.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn render(sink: Sink, raw: &str) -> String {
        sink.render_atoms(raw).concat()
    }

    #[test]
    fn twitch_passes_plain_text_through_including_braces() {
        assert_eq!(
            render(Sink::ChatTwitch, "hello *world* {5}"),
            "hello *world* {5}"
        );
    }

    #[test]
    fn discord_escapes_markdown_specials() {
        assert_eq!(
            render(
                Sink::ChatDiscord,
                "*bold* _italic_ ~~strike~~ `code` >quote"
            ),
            "\\*bold\\* \\_italic\\_ \\~\\~strike\\~\\~ \\`code\\` \\>quote"
        );
    }

    #[test]
    fn discord_neutralizes_everyone_mention() {
        let escaped = render(Sink::ChatDiscord, "@everyone free stuff");
        assert!(!escaped.contains("@everyone"), "mention must be broken up");
        assert!(escaped.starts_with("@\u{200B}everyone"));
    }

    #[test]
    fn discord_neutralizes_raw_id_mention() {
        let escaped = render(Sink::ChatDiscord, "<@123456789012345678>");
        assert!(!escaped.contains("<@1"));
    }

    #[test]
    fn discord_neutralizes_role_mention() {
        let escaped = render(Sink::ChatDiscord, "<@&123456789012345678>");
        assert!(!escaped.contains("<@&"));
    }

    #[test]
    fn discord_neutralizes_channel_reference() {
        let escaped = render(Sink::ChatDiscord, "<#123456789012345678>");
        assert!(!escaped.contains("<#1"));
    }

    #[test]
    fn discord_leaves_stray_braces_literal() {
        assert_eq!(
            render(Sink::ChatDiscord, "{not a uuid} *bold*"),
            "{not a uuid} \\*bold\\*"
        );
    }

    #[test]
    fn overlay_html_escapes_xss_payload() {
        let escaped = render(Sink::Overlay, "<script>alert(1)</script>");
        assert!(!escaped.contains('<'));
        assert!(!escaped.contains('>'));
        assert_eq!(escaped, "&lt;script&gt;alert(1)&lt;/script&gt;");
    }

    #[test]
    fn overlay_html_escapes_attribute_breakout() {
        let escaped = render(Sink::Overlay, r#"" onmouseover="alert(1)"#);
        assert!(!escaped.contains('"'));
    }

    #[test]
    fn overlay_entity_escapes_stray_braces() {
        assert_eq!(
            render(Sink::Overlay, "cost is {5} <b>bold</b>"),
            "cost is &#123;5&#125; &lt;b&gt;bold&lt;/b&gt;"
        );
    }

    #[test]
    fn render_atoms_never_splits_a_multi_codepoint_grapheme() {
        let family = "\u{1F468}\u{200D}\u{1F469}\u{200D}\u{1F467}\u{200D}\u{1F466}";
        let atoms = Sink::Overlay.render_atoms(family);
        assert_eq!(atoms.len(), 1, "the whole ZWJ sequence must be one atom");
        assert_eq!(atoms[0], family);
    }
}
