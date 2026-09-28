//! Per-sink escaping (spec S10.4: "each sink's own escaping rules...not one
//! shared escaper assumed safe everywhere"). Applied by
//! [`crate::render::Detokenizer::render`] to every [`crate::grammar::
//! Segment`] -- both literal bundle/guest text and resolved display names
//! go through the same sink's escaper, since either one can carry
//! attacker-influenced content (a crafted display name for the overlay
//! sink; markdown/mention injection in bundle-authored chat text for
//! Discord).

/// The egress point a rendered payload is headed to. Each variant owns a
/// distinct escaping strategy -- see [`Sink::escape`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Sink {
    /// Twitch IRC chat. Plaintext protocol with no markup/formatting
    /// syntax to escape; CRLF/control-character injection defense stays a
    /// separate, existing concern
    /// (`core/svc_action/src/capabilities.rs`'s `sanitize_irc_component`,
    /// applied by the caller AFTER this crate's `render` -- see that
    /// module's doc for the detokenize-then-sanitize ordering, spec
    /// S10.4's table).
    ChatTwitch,
    /// Discord chat, sent as the REST message `content` field. Discord
    /// interprets a rich markdown/mention grammar in that field, so both
    /// markdown formatting characters and raw `@` mentions must be
    /// neutralized -- an attacker-influenced string here could otherwise
    /// format arbitrary rich text or trigger an `@everyone`/`@here` mass
    /// mention in the shared bot's channel.
    ChatDiscord,
    /// The browser-source overlay's DOM/HTML context. Standard HTML-entity
    /// escaping -- the sandboxed-iframe/CSP layer (spec S10.4 table) is a
    /// second, independent control, not a substitute for this one.
    Overlay,
}

/// Escapes a lone, non-placeholder `{`/`}` byte (S10.4: "escaped before
/// tokenizing... braces entity-escaped") so that no downstream consumer --
/// regardless of sink -- can ever reinterpret it as placeholder syntax.
/// Applied uniformly before any sink-specific escaping, to literal segments
/// only (a resolved [`Segment::Placeholder`] never contains a literal brace
/// character; its substituted name goes through sink escaping alone).
fn escape_stray_braces(literal: &str) -> String {
    let mut out = String::with_capacity(literal.len());
    for c in literal.chars() {
        match c {
            '{' => out.push_str("&#123;"),
            '}' => out.push_str("&#125;"),
            other => out.push(other),
        }
    }
    out
}

fn escape_html(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#x27;"),
            other => out.push(other),
        }
    }
    out
}

/// Discord markdown formatting characters (backslash-escaped to render as
/// literal text rather than being interpreted as bold/italic/strike/code/
/// blockquote/spoiler markup). Backslash itself is escaped first so an
/// already-escaped sequence in guest text isn't double-unescaped by
/// Discord's own parser.
const DISCORD_MARKDOWN_SPECIALS: &[char] = &['\\', '*', '_', '~', '`', '|', '>'];

/// Escapes Discord markdown formatting and neutralizes mention syntax.
/// `@everyone`/`@here`/`<@id>`/`<@&role-id>` all key off a literal `@`
/// (spec: "Discord markdown/mention escaping") -- inserting a zero-width
/// space immediately after every `@` breaks Discord's mention tokenizer
/// while leaving the text visually unchanged to a human reader, the same
/// technique Discord bot authors commonly use (backslash-escaping does
/// NOT neutralize mention syntax, only markdown formatting, hence the two
/// separate mechanisms here).
fn escape_discord(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        if DISCORD_MARKDOWN_SPECIALS.contains(&c) {
            out.push('\\');
            out.push(c);
        } else if c == '@' {
            out.push('@');
            out.push('\u{200B}'); // zero-width space
        } else {
            out.push(c);
        }
    }
    out
}

impl Sink {
    /// Escapes one already-brace-safe string chunk for this sink. Callers
    /// (`render.rs`) pass literal text through [`escape_stray_braces`]
    /// first; a resolved display name has no stray braces to worry about
    /// (S10.4's self-referential-injection closure) and goes straight to
    /// this method.
    pub(crate) fn escape(self, s: &str) -> String {
        match self {
            Sink::ChatTwitch => s.to_string(),
            Sink::ChatDiscord => escape_discord(s),
            Sink::Overlay => escape_html(s),
        }
    }

    /// Escapes a literal (non-placeholder) segment: this sink's own
    /// escaping first, then the universal stray-brace escaping. This
    /// order matters for the overlay/HTML sink -- HTML-escaping never
    /// produces a literal `{`/`}` in its output, so applying brace
    /// escaping second cannot be re-mangled by it, whereas the reverse
    /// order would HTML-escape the `&` the brace escaper itself just
    /// introduced (`&#123;` -> `&amp;#123;`).
    pub(crate) fn escape_literal(self, s: &str) -> String {
        escape_stray_braces(&self.escape(s))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn twitch_passes_plain_text_through() {
        assert_eq!(Sink::ChatTwitch.escape("hello *world*"), "hello *world*");
    }

    #[test]
    fn discord_escapes_markdown_specials() {
        assert_eq!(
            Sink::ChatDiscord.escape("*bold* _italic_ ~~strike~~ `code` >quote"),
            "\\*bold\\* \\_italic\\_ \\~\\~strike\\~\\~ \\`code\\` \\>quote"
        );
    }

    #[test]
    fn discord_neutralizes_everyone_mention() {
        let escaped = Sink::ChatDiscord.escape("@everyone free stuff");
        assert!(!escaped.contains("@everyone"), "mention must be broken up");
        assert!(escaped.starts_with("@\u{200B}everyone"));
    }

    #[test]
    fn discord_neutralizes_raw_id_mention() {
        let escaped = Sink::ChatDiscord.escape("<@123456789012345678>");
        assert!(!escaped.contains("<@1"));
    }

    #[test]
    fn overlay_html_escapes_xss_payload() {
        let escaped = Sink::Overlay.escape("<script>alert(1)</script>");
        assert!(!escaped.contains('<'));
        assert!(!escaped.contains('>'));
        assert_eq!(escaped, "&lt;script&gt;alert(1)&lt;/script&gt;");
    }

    #[test]
    fn overlay_html_escapes_attribute_breakout() {
        let escaped = Sink::Overlay.escape(r#"" onmouseover="alert(1)"#);
        assert!(!escaped.contains('"'));
    }

    #[test]
    fn escape_literal_neutralizes_stray_braces_before_sink_escaping() {
        let escaped = Sink::Overlay.escape_literal("cost is {5} <b>bold</b>");
        assert_eq!(escaped, "cost is &#123;5&#125; &lt;b&gt;bold&lt;/b&gt;");
    }

    #[test]
    fn escape_literal_on_discord_neutralizes_both_braces_and_markdown() {
        let escaped = Sink::ChatDiscord.escape_literal("{not a uuid} *bold*");
        assert_eq!(escaped, "&#123;not a uuid&#125; \\*bold\\*");
    }
}
