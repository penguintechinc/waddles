//! Server-side HTML/SVG sanitizer for custom presentation bundle widgets.
//!
//! Presentation app bundles may push rich visual fragments (`WidgetOutputKind::SanitizedHtml`):
//! animated avatars, status bars, floating badges. To prevent stored XSS, iframe injection and DOM
//! clobbering in OBS browser sources, every fragment is sanitized HERE, before it reaches the hub
//! cache, the live SSE stream or a DOM.
//!
//! The parsing and serialization is the [`ammonia`] crate (html5ever: the same tokenizer browsers
//! implement, so the sanitizer and the browser agree about what the markup *is* -- the property a
//! hand-rolled tag scanner can never guarantee). This module only supplies the policy:
//!
//! 1. Hard byte bound (`MAX_WIDGET_HTML_BYTES`) checked before parsing.
//! 2. Closed tag allowlist: layout, text, `img`, and SVG primitives. Everything else is removed;
//!    `script`, `style`, `iframe`, `object`, `embed`, `form`, ... are removed WITH their content.
//! 3. No event handlers, no `id`/`name` (DOM clobbering), no `href`/`xlink:href`, no `<a>`.
//! 4. `img src` is `https` only: no `javascript:`, `data:`, `http:` or scheme-relative URLs.
//! 5. `style` survives only as an allowlisted-property declaration list with plain values; any
//!    declaration carrying `url(`, `expression(`, `@`, `\`, quotes or comment syntax is dropped.
//! 6. `{user:<token>}` placeholders in the OUTPUT (checked last, so tokens assembled by stripping
//!    markup are caught too) become the neutral label: viewers never see a user reference.

use std::borrow::Cow;
use std::collections::{HashMap, HashSet};
use std::sync::LazyLock;

use egress_detokenizer::{detokenize, Sink};
use overlay_schema::MAX_WIDGET_HTML_BYTES;

/// Errors returned when widget HTML exceeds bounds.
#[derive(Debug, PartialEq, Eq, thiserror::Error)]
pub enum HtmlSanitizeError {
    /// The fragment is larger than the allowed number of bytes.
    #[error("HTML payload exceeds maximum allowed size of {0} bytes")]
    PayloadTooLarge(usize),
}

/// Allowed HTML/SVG tags.
const ALLOWED_TAGS: &[&str] = &[
    "div", "span", "p", "strong", "em", "b", "i", "small", "br", "ul", "ol", "li", "img", "svg",
    "g", "path", "circle", "rect", "line", "polygon", "polyline", "text",
];

/// Tags removed together with everything inside them.
const DROP_WITH_CONTENT: &[&str] = &[
    "script",
    "style",
    "iframe",
    "frame",
    "frameset",
    "object",
    "embed",
    "applet",
    "form",
    "textarea",
    "select",
    "button",
    "noscript",
    "template",
    "title",
    "math",
    "foreignobject",
    "link",
    "meta",
    "base",
];

/// Attributes allowed on every allowed tag.
const GENERIC_ATTRIBUTES: &[&str] = &["class", "style"];

/// Safe CSS properties permitted inside inline `style="..."` attributes.
const ALLOWED_CSS_PROPERTIES: &[&str] = &[
    "left",
    "top",
    "right",
    "bottom",
    "width",
    "height",
    "max-width",
    "max-height",
    "min-width",
    "min-height",
    "opacity",
    "color",
    "background-color",
    "border-color",
    "border-radius",
    "transform",
    "transition",
    "display",
    "flex",
    "flex-direction",
    "align-items",
    "justify-content",
    "gap",
    "font-size",
    "font-weight",
    "text-align",
    "z-index",
    "position",
    "overflow",
    "padding",
    "margin",
    "line-height",
];

/// The per-tag attribute allowlist.
fn tag_attributes() -> HashMap<&'static str, HashSet<&'static str>> {
    let svg_paint: &[&str] = &["fill", "stroke", "stroke-width", "opacity"];
    let with_paint = |extra: &[&'static str]| -> HashSet<&'static str> {
        svg_paint.iter().chain(extra.iter()).copied().collect()
    };
    HashMap::from([
        ("img", HashSet::from(["src", "alt", "width", "height"])),
        ("svg", with_paint(&["viewBox", "width", "height"])),
        ("g", with_paint(&["transform"])),
        ("path", with_paint(&["d"])),
        ("circle", with_paint(&["cx", "cy", "r"])),
        (
            "rect",
            with_paint(&["x", "y", "width", "height", "rx", "ry"]),
        ),
        ("line", with_paint(&["x1", "y1", "x2", "y2"])),
        ("polygon", with_paint(&["points"])),
        ("polyline", with_paint(&["points"])),
        ("text", with_paint(&["x", "y", "font-size", "text-anchor"])),
    ])
}

/// `true` when a CSS value is plain data: no functions that load or run anything, no escapes,
/// quotes, comments or markup characters.
fn is_safe_css_value(value: &str) -> bool {
    let lower = value.to_ascii_lowercase();
    if lower.contains("url(")
        || lower.contains("expression")
        || lower.contains("javascript")
        || lower.contains("import")
        || lower.contains("var(")
        || lower.contains("attr(")
    {
        return false;
    }
    !value.is_empty()
        && value.chars().all(|c| {
            c.is_ascii_alphanumeric()
                || matches!(c, ' ' | '#' | '%' | '.' | ',' | '(' | ')' | '-' | '+' | '/')
        })
}

/// Filters an inline `style` attribute down to allowlisted `property: value` declarations;
/// `None` when nothing survives.
fn sanitize_style(style: &str) -> Option<String> {
    let kept: Vec<String> = style
        .split(';')
        .filter_map(|decl| {
            let (prop, value) = decl.split_once(':')?;
            let prop = prop.trim().to_ascii_lowercase();
            let value = value.trim();
            (ALLOWED_CSS_PROPERTIES.contains(&prop.as_str()) && is_safe_css_value(value))
                .then(|| format!("{prop}: {value}"))
        })
        .collect();
    (!kept.is_empty()).then(|| kept.join("; "))
}

/// The configured sanitizer, built once.
static SANITIZER: LazyLock<ammonia::Builder<'static>> = LazyLock::new(|| {
    let mut builder = ammonia::Builder::empty();
    builder
        .tags(ALLOWED_TAGS.iter().copied().collect())
        .clean_content_tags(DROP_WITH_CONTENT.iter().copied().collect())
        .generic_attributes(GENERIC_ATTRIBUTES.iter().copied().collect())
        .tag_attributes(tag_attributes())
        .url_schemes(HashSet::from(["https"]))
        .url_relative(ammonia::UrlRelative::Deny)
        .link_rel(None)
        .strip_comments(true)
        .attribute_filter(|_element, attribute, value| {
            if attribute == "style" {
                sanitize_style(value).map(Cow::Owned)
            } else {
                Some(Cow::Borrowed(value))
            }
        });
    builder
});

/// Sanitizes a custom presentation widget HTML fragment.
pub fn sanitize_widget_html(input: &str) -> Result<String, HtmlSanitizeError> {
    if input.len() > MAX_WIDGET_HTML_BYTES {
        return Err(HtmlSanitizeError::PayloadTooLarge(MAX_WIDGET_HTML_BYTES));
    }
    let cleaned = SANITIZER.clean(input).to_string();
    if cleaned.contains("{user:") {
        // Empty name map: every token becomes the neutral label, no name is ever involved.
        return Ok(detokenize(&cleaned, &HashMap::new(), Sink::Overlay).text);
    }
    Ok(cleaned)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Substrings that must never survive in sanitized output (lowercased comparison).
    const FORBIDDEN: &[&str] = &[
        "<script",
        "</script",
        "<iframe",
        "<object",
        "<embed",
        "<form",
        "<style",
        "<link",
        "<meta",
        "<base",
        "<math",
        "<a ",
        "<a>",
        "onerror",
        "onload",
        "onclick",
        "onmouseover",
        "onfocus",
        "onanimationstart",
        "javascript:",
        "vbscript:",
        "data:",
        "expression(",
        "url(",
        "srcdoc",
        "xlink:href",
        "href=",
        "<foreignobject",
        "id=",
        "name=",
    ];

    fn assert_clean(input: &str) -> String {
        let out = sanitize_widget_html(input).unwrap();
        let lower = out.to_ascii_lowercase();
        for bad in FORBIDDEN {
            assert!(
                !lower.contains(bad),
                "input {input:?} produced {out:?} containing {bad:?}"
            );
        }
        // A sanitizer must be a fixed point: re-sanitizing its own output changes nothing.
        assert_eq!(
            sanitize_widget_html(&out).unwrap(),
            out,
            "not idempotent for {input:?}"
        );
        out
    }

    #[test]
    fn allows_safe_avatar_markup() {
        let out = assert_clean(
            r#"<div class="sw-avatar" style="left: 20px; top: 10px;"><img src="https://cdn.example.com/jedi.png" alt="jedi"><span>Master</span></div>"#,
        );
        assert!(out.contains("class=\"sw-avatar\""), "{out}");
        assert!(out.contains("style=\"left: 20px; top: 10px\""), "{out}");
        assert!(
            out.contains("src=\"https://cdn.example.com/jedi.png\""),
            "{out}"
        );
        assert!(out.contains("<span>Master</span>"), "{out}");
    }

    #[test]
    fn allows_svg_primitives() {
        let out = assert_clean(
            r##"<svg viewBox="0 0 10 10" width="40" height="40"><circle cx="5" cy="5" r="4" fill="#fff"/><path d="M0 0L10 10" stroke="red"/><text x="1" y="2">hi</text></svg>"##,
        );
        assert!(out.contains("<svg"), "{out}");
        assert!(out.contains("viewBox=\"0 0 10 10\""), "{out}");
        assert!(out.contains("<circle"), "{out}");
        assert!(out.contains("d=\"M0 0L10 10\""), "{out}");
    }

    /// The XSS corpus: classic and parser-differential vectors. Each must come out free of every
    /// forbidden construct and idempotent.
    #[test]
    fn xss_corpus_is_neutralised() {
        let corpus = [
            r#"<script>alert(1)</script>"#,
            r#"<SCRIPT SRC=//evil.example/x.js></SCRIPT>"#,
            r#"<scr<script>ipt>alert(1)</scr</script>ipt>"#,
            r#"<img src=x onerror=alert(1)>"#,
            r#"<img src="https://a.example/x.png" onload="alert(1)" onerror='alert(2)'>"#,
            r#"<IMG SRC=JaVaScRiPt:alert('XSS')>"#,
            r#"<img src="javascript:alert(1)">"#,
            r#"<img src="&#106;avascript:alert(1)">"#,
            r#"<img src="  javascript:alert(1)">"#,
            "<img src=\"java\tscript:alert(1)\">",
            r#"<img src="data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==">"#,
            r#"<img src="http://insecure.example/a.png">"#,
            r#"<img src="//evil.example/a.png">"#,
            r#"<iframe src="https://evil.example"></iframe>"#,
            r#"<iframe srcdoc="<script>alert(1)</script>"></iframe>"#,
            r#"<object data="https://evil.example/x.swf"></object>"#,
            r#"<embed src="https://evil.example/x.swf">"#,
            r#"<form action="https://evil.example"><input name="x"><button>go</button></form>"#,
            r#"<a href="javascript:alert(1)">click</a>"#,
            r#"<a href="https://evil.example">click</a>"#,
            r#"<svg onload=alert(1)>"#,
            r#"<svg><script>alert(1)</script></svg>"#,
            r#"<svg><a xlink:href="javascript:alert(1)"><text>x</text></a></svg>"#,
            r#"<svg><foreignObject><iframe src="https://evil.example"></iframe></foreignObject></svg>"#,
            r#"<svg><style>*{background:url(javascript:alert(1))}</style></svg>"#,
            r#"<math><mi xlink:href="javascript:alert(1)">x</mi></math>"#,
            r#"<math><mtext><table><mglyph><style><img src=x onerror=alert(1)>"#,
            r#"<svg></p><style><a id="</style><img src=1 onerror=alert(1)>">"#,
            r#"<noscript><p title="</noscript><img src=x onerror=alert(1)>">"#,
            r#"<style>@import 'https://evil.example/x.css';</style>"#,
            r#"<div style="background:url(javascript:alert(1))">x</div>"#,
            r#"<div style="width: expression(alert(1))">x</div>"#,
            r#"<div style="behavior:url(x.htc)">x</div>"#,
            r#"<div style="color:red;background-image:url('https://evil.example/t.gif')">x</div>"#,
            r#"<div style="position:fixed;left:0;top:0;width:100%;height:100%;background:url(https://evil.example/p.png)">x</div>"#,
            r#"<div onmouseover="alert(1)">x</div>"#,
            r#"<div id="x" name="y">clobber</div>"#,
            r#"<div id="location"></div><div name="cookie"></div>"#,
            r#"<link rel="stylesheet" href="https://evil.example/x.css">"#,
            r#"<meta http-equiv="refresh" content="0;url=https://evil.example">"#,
            r#"<base href="https://evil.example/">"#,
            r#"<template><script>alert(1)</script></template>"#,
            r#"<textarea><script>alert(1)</script></textarea>"#,
            r#"<!--<script>alert(1)</script>-->"#,
            r#"<div><!--[if IE]><script>alert(1)</script><![endif]--></div>"#,
            r#"<![CDATA[<script>alert(1)</script>]]>"#,
            r#"<?xml version="1.0"?><script>alert(1)</script>"#,
            "<div\u{0}onclick=alert(1)>x</div>",
            "<scr\u{0}ipt>alert(1)</scr\u{0}ipt>",
            r#"<div/onclick=alert(1)>x</div>"#,
            r#"<div """><script>alert(1)</script>">x</div>"#,
            r#"<img src="https://a.example/x.png" srcset="javascript:alert(1)">"#,
            r#"<svg><animate onbegin=alert(1) attributeName=x dur=1s>"#,
            r#"<svg><set attributeName="onmouseover" to="alert(1)"/></svg>"#,
            r#"<svg><use href="data:image/svg+xml;base64,PHN2Zz48L3N2Zz4="/></svg>"#,
            r#"<video src=x onerror=alert(1)><source src=x onerror=alert(1)></video>"#,
            r#"<details open ontoggle=alert(1)>"#,
            r#"<input autofocus onfocus=alert(1)>"#,
            r#"<body onload=alert(1)>"#,
            r#"<marquee onstart=alert(1)>x</marquee>"#,
            // RUSTSEC-2026-0533: mutation XSS across the SVG/MathML namespace boundary.
            r#"<svg><foreignObject><math><iframe><!--</iframe><img src onerror=alert(1)>-->"#,
            r#"<math><mi><svg><iframe><!--</iframe><img src onerror=alert(1)>-->"#,
            r#"<svg><foreignObject><math><mi><svg><style><!--</style><img src onerror=alert(1)>-->"#,
        ];
        for input in corpus {
            assert_clean(input);
        }
    }

    #[test]
    fn script_and_style_subtrees_are_removed_with_their_content() {
        assert_eq!(
            sanitize_widget_html("<div>Hello<script>alert(1)</script>World</div>").unwrap(),
            "<div>HelloWorld</div>"
        );
        let out = sanitize_widget_html("<div>a<style>b{color:red}</style>c</div>").unwrap();
        assert_eq!(out, "<div>ac</div>");
    }

    #[test]
    fn only_https_image_sources_survive() {
        let out = sanitize_widget_html(r#"<img src="https://ok.example/a.png">"#).unwrap();
        assert!(out.contains("src=\"https://ok.example/a.png\""), "{out}");
        for bad in [
            r#"<img src="http://x.example/a.png">"#,
            r#"<img src="javascript:alert(1)">"#,
            r#"<img src="data:image/png;base64,AAAA">"#,
            r#"<img src="//x.example/a.png">"#,
            r#"<img src="/relative.png">"#,
            r#"<img src="relative.png">"#,
        ] {
            let out = sanitize_widget_html(bad).unwrap();
            assert!(!out.contains("src="), "{bad} -> {out}");
        }
    }

    #[test]
    fn style_keeps_safe_declarations_and_drops_the_rest() {
        let out = sanitize_widget_html(
            r#"<div style="color: blue; background-image: url('javascript:alert(1)'); z-index: 5; behavior: x">T</div>"#,
        )
        .unwrap();
        assert_eq!(out, "<div style=\"color: blue; z-index: 5\">T</div>");
        // No safe declaration at all: the attribute disappears.
        assert_eq!(
            sanitize_widget_html(r#"<div style="background: url(x)">T</div>"#).unwrap(),
            "<div>T</div>"
        );
        for (value, ok) in [
            ("10px", true),
            ("rgb(1, 2, 3)", true),
            ("#fff", true),
            ("translate(10px, 5px)", true),
            ("url(x)", false),
            ("expression(1)", false),
            ("a\\62 c", false),
            ("'quoted'", false),
            ("x/*c*/", false),
            ("var(--x)", false),
            ("", false),
            ("a;b", false),
        ] {
            assert_eq!(is_safe_css_value(value), ok, "{value:?}");
        }
    }

    #[test]
    fn unclosed_tags_are_closed_by_the_parser() {
        assert_eq!(
            sanitize_widget_html("<div><span><b>bold</div>").unwrap(),
            "<div><span><b>bold</b></span></div>"
        );
    }

    #[test]
    fn user_tokens_become_the_neutral_label_even_when_assembled_by_stripping() {
        let token = "{user:11111111-1111-1111-1111-111111111111}";
        let direct = sanitize_widget_html(&format!("<p>hi {token}</p>")).unwrap();
        assert!(!direct.contains("{user:"), "{direct}");
        assert!(direct.contains("Unknown User"), "{direct}");
        assert!(
            direct.starts_with("<p>") && direct.ends_with("</p>"),
            "{direct}"
        );
        // Markup that is stripped can splice a token together: it must still be caught.
        let spliced = sanitize_widget_html(
            "<p>{us<blink></blink>er:11111111-1111-1111-1111-111111111111}</p>",
        )
        .unwrap();
        assert!(!spliced.contains("{user:"), "{spliced}");
        assert!(spliced.contains("Unknown User"), "{spliced}");
    }

    #[test]
    fn payload_too_large_is_refused_before_parsing() {
        let big = "a".repeat(MAX_WIDGET_HTML_BYTES + 1);
        assert_eq!(
            sanitize_widget_html(&big).unwrap_err(),
            HtmlSanitizeError::PayloadTooLarge(MAX_WIDGET_HTML_BYTES)
        );
        assert!(sanitize_widget_html(&"a".repeat(MAX_WIDGET_HTML_BYTES)).is_ok());
    }

    #[test]
    fn plain_text_is_escaped_not_interpreted() {
        let out = sanitize_widget_html("1 < 2 & 3 > 2").unwrap();
        assert!(!out.contains("< 2"), "{out}");
        assert!(out.contains("&amp;") && out.contains("&lt;"), "{out}");
    }
}
