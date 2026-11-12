//! Server-side HTML/SVG sanitizer for custom presentation bundle widgets.
//!
//! Presentation app bundles may push rich visual fragments (`WidgetOutputKind::SanitizedHtml`),
//! such as animated avatars, status bars, and floating badges.
//! To prevent Stored XSS, iframe injection, CSRF, and DOM clobbering in OBS browser sources,
//! all incoming HTML is sanitized server-side in Rust before reaching cache, live SSE streams,
//! or the DOM.
//!
//! # Invariants
//! 1. Hard byte bound: maximum 16 KiB (`MAX_WIDGET_HTML_BYTES`).
//! 2. Closed tag allowlist: only structural layout, text, img, and SVG primitives.
//! 3. All `<script>`, `<style>`, `<iframe>`, `<object>`, `<embed>`, `<form>`, `<input>`,
//!    `<button>`, `<link>`, and `<meta>` elements are strictly stripped along with any dangerous attributes.
//! 4. Zero event handlers: any attribute starting with `on` (`onload`, `onerror`, etc.) is removed.
//! 5. `img.src` is validated: only `https://` URLs are permitted (no `javascript:`, `data:`, or `http://`).
//! 6. Inline CSS `style` attributes are filtered against a strict property allowlist; any dangerous
//!    patterns (`url(`, `expression(`, `javascript:`, `@import`) cause the style to be stripped.
//! 7. Any `{user:<uuid>}` placeholder is detokenized via [`crate::overlay::detok::sanitize_text`].

use std::collections::HashSet;

use overlay_schema::MAX_WIDGET_HTML_BYTES;

use crate::overlay::detok::sanitize_text;

/// Errors returned when widget HTML cannot be sanitized or exceeds bounds.
#[derive(Debug, PartialEq, Eq, thiserror::Error)]
pub enum HtmlSanitizeError {
    #[error("HTML payload exceeds maximum allowed size of {0} bytes")]
    PayloadTooLarge(usize),
}

/// Allowed HTML/SVG tags.
const ALLOWED_TAGS: &[&str] = &[
    "div", "span", "p", "strong", "em", "b", "i", "small", "br", "ul", "ol", "li", "img",
    "svg", "g", "path", "circle", "rect", "line", "polygon", "polyline", "text",
];

/// Tags whose entire subtree (content between open and close tag) must be removed.
const DROP_SUBTREE_TAGS: &[&str] = &[
    "script", "style", "iframe", "frame", "object", "embed", "applet", "form", "textarea", "select",
    "button", "noscript",
];

/// Safe CSS properties permitted inside inline `style="..."` attributes.
const ALLOWED_CSS_PROPERTIES: &[&str] = &[
    "left", "top", "right", "bottom", "width", "height", "max-width", "max-height",
    "min-width", "min-height", "opacity", "color", "background-color", "border-color",
    "border-radius", "transform", "transition", "display", "flex", "flex-direction",
    "align-items", "justify-content", "gap", "font-size", "font-weight", "text-align",
    "z-index", "position", "overflow", "padding", "margin", "line-height",
];

/// Sanitizes a custom presentation widget HTML fragment.
pub fn sanitize_widget_html(input: &str) -> Result<String, HtmlSanitizeError> {
    if input.len() > MAX_WIDGET_HTML_BYTES {
        return Err(HtmlSanitizeError::PayloadTooLarge(MAX_WIDGET_HTML_BYTES));
    }

    // Step 1: Pre-detokenize any {user:<uuid>} placeholders into HTML-escaped names.
    let detokenized = if input.contains("{user:") {
        sanitize_text(input)
    } else {
        input.to_string()
    };

    // Step 2: Parse and sanitize HTML tokens.
    let sanitized = sanitize_tokens(&detokenized);
    Ok(sanitized)
}

/// Tokenizer-based sanitizer for HTML fragments.
fn sanitize_tokens(html: &str) -> String {
    let allowed_tag_set: HashSet<&'static str> = ALLOWED_TAGS.iter().copied().collect();
    let drop_tag_set: HashSet<&'static str> = DROP_SUBTREE_TAGS.iter().copied().collect();

    let mut output = String::with_capacity(html.len());
    let mut chars = html.char_indices().peekable();
    let mut open_tags: Vec<String> = Vec::new();
    let mut drop_depth: usize = 0;
    let mut active_drop_tag: Option<String> = None;

    while let Some((_i, c)) = chars.next() {
        if c == '<' {
            let mut tag_content = String::new();
            let mut in_quotes = None;
            let mut closed = false;

            for (_, next_c) in chars.by_ref() {
                tag_content.push(next_c);
                if let Some(q) = in_quotes {
                    if next_c == q {
                        in_quotes = None;
                    }
                } else if next_c == '"' || next_c == '\'' {
                    in_quotes = Some(next_c);
                } else if next_c == '>' {
                    closed = true;
                    break;
                }
            }

            if !closed {
                // Malformed/unclosed tag - escape the opening bracket and continue
                if drop_depth == 0 {
                    output.push_str("&lt;");
                }
                continue;
            }

            let raw_tag = tag_content.trim_end_matches('>').trim();
            if raw_tag.is_empty() {
                continue;
            }

            // Check if comment
            if raw_tag.starts_with("!--") {
                continue;
            }

            let is_closing = raw_tag.starts_with('/');
            let tag_body = if is_closing {
                raw_tag.trim_start_matches('/').trim()
            } else {
                raw_tag
            };

            let tag_name = tag_body
                .split_whitespace()
                .next()
                .unwrap_or("")
                .trim_end_matches('/')
                .to_ascii_lowercase();

            if drop_tag_set.contains(tag_name.as_str()) {
                if is_closing {
                    if let Some(ref current) = active_drop_tag {
                        if current == &tag_name {
                            drop_depth = drop_depth.saturating_sub(1);
                            if drop_depth == 0 {
                                active_drop_tag = None;
                            }
                        }
                    }
                } else if !tag_body.ends_with('/') {
                    drop_depth += 1;
                    if active_drop_tag.is_none() {
                        active_drop_tag = Some(tag_name.clone());
                    }
                }
                continue;
            }

            if drop_depth > 0 {
                continue;
            }

            if !allowed_tag_set.contains(tag_name.as_str()) {
                continue;
            }

            if is_closing {
                if let Some(pos) = open_tags.iter().rposition(|t| t == &tag_name) {
                    while open_tags.len() > pos {
                        let popped = open_tags.pop().unwrap();
                        output.push_str("</");
                        output.push_str(&popped);
                        output.push('>');
                    }
                }
                continue;
            }

            let is_self_closing = raw_tag.ends_with('/')
                || tag_name == "br"
                || tag_name == "img"
                || tag_name == "circle"
                || tag_name == "rect"
                || tag_name == "line"
                || tag_name == "path";

            let sanitized_attrs = sanitize_attributes(&tag_name, tag_body);

            output.push('<');
            output.push_str(&tag_name);
            if !sanitized_attrs.is_empty() {
                output.push(' ');
                output.push_str(&sanitized_attrs);
            }

            if is_self_closing {
                output.push_str(" />");
            } else {
                output.push('>');
                open_tags.push(tag_name);
            }
        } else if drop_depth == 0 {
            output.push(c);
        }
    }

    // Auto-close any unclosed tags at the end
    while let Some(tag) = open_tags.pop() {
        output.push_str("</");
        output.push_str(&tag);
        output.push('>');
    }

    output
}

/// Sanitizes element attributes against tag-specific rules.
fn sanitize_attributes(tag_name: &str, tag_body: &str) -> String {
    let mut parts = Vec::new();
    let attrs_str = tag_body
        .split_whitespace()
        .skip(1)
        .collect::<Vec<_>>()
        .join(" ");
    let raw_attrs = parse_raw_attributes(&attrs_str);

    for (name, val) in raw_attrs {
        let name_lower = name.to_ascii_lowercase();

        // Strip all event handlers unconditionally
        if name_lower.starts_with("on") {
            continue;
        }

        if name_lower == "style" {
            if let Some(sanitized_style) = sanitize_style(&val) {
                if !sanitized_style.is_empty() {
                    parts.push(format!("style=\"{}\"", escape_attr_val(&sanitized_style)));
                }
            }
            continue;
        }

        if tag_name == "img" && name_lower == "src" {
            if is_safe_https_url(&val) {
                parts.push(format!("src=\"{}\"", escape_attr_val(&val)));
            }
            continue;
        }

        if is_allowed_generic_attr(&name_lower) {
            parts.push(format!("{}=\"{}\"", name_lower, escape_attr_val(&val)));
        } else if is_allowed_svg_attr(tag_name, &name_lower) {
            parts.push(format!("{}=\"{}\"", name_lower, escape_attr_val(&val)));
        }
    }

    parts.join(" ")
}

fn is_allowed_generic_attr(attr: &str) -> bool {
    attr == "class"
        || attr == "id"
        || attr == "title"
        || attr == "alt"
        || attr == "role"
        || attr.starts_with("aria-")
        || attr.starts_with("data-")
}

fn is_allowed_svg_attr(tag: &str, attr: &str) -> bool {
    let is_svg_elem = matches!(
        tag,
        "svg" | "g" | "path" | "circle" | "rect" | "line" | "polygon" | "polyline" | "text"
    );
    if !is_svg_elem {
        return false;
    }
    matches!(
        attr,
        "viewbox"
            | "width"
            | "height"
            | "x"
            | "y"
            | "cx"
            | "cy"
            | "r"
            | "rx"
            | "ry"
            | "d"
            | "fill"
            | "stroke"
            | "stroke-width"
            | "transform"
            | "opacity"
            | "font-size"
            | "font-family"
            | "text-anchor"
    )
}

/// Enforces https:// scheme and basic URL safety on image sources.
fn is_safe_https_url(url: &str) -> bool {
    if !url.starts_with("https://") {
        return false;
    }
    if url.chars().any(|c| c.is_control() || c == '"' || c == '\'' || c == '<' || c == '>') {
        return false;
    }
    true
}

/// Sanitizes inline CSS styles to safe properties only.
fn sanitize_style(style_str: &str) -> Option<String> {
    let lower = style_str.to_ascii_lowercase();
    // Reject dangerous CSS constructs immediately
    if lower.contains("url(")
        || lower.contains("expression(")
        || lower.contains("javascript:")
        || lower.contains("@import")
        || lower.contains("-moz-binding")
    {
        return None;
    }

    let allowed_props: HashSet<&'static str> = ALLOWED_CSS_PROPERTIES.iter().copied().collect();
    let mut safe_declarations = Vec::new();

    for decl in style_str.split(';') {
        let decl = decl.trim();
        if decl.is_empty() {
            continue;
        }
        if let Some((prop, val)) = decl.split_once(':') {
            let prop_trim = prop.trim().to_ascii_lowercase();
            let val_trim = val.trim();
            if allowed_props.contains(prop_trim.as_str()) && is_safe_css_value(val_trim) {
                safe_declarations.push(format!("{}: {}", prop_trim, val_trim));
            }
        }
    }

    Some(safe_declarations.join("; "))
}

fn is_safe_css_value(val: &str) -> bool {
    !val.chars().any(|c| c.is_control() || c == '<' || c == '>')
}

fn escape_attr_val(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('"', "&quot;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}

fn parse_raw_attributes(input: &str) -> Vec<(String, String)> {
    let mut attrs = Vec::new();
    let mut chars = input.char_indices().peekable();

    while let Some((_, c)) = chars.peek() {
        if c.is_whitespace() || *c == '/' {
            chars.next();
            continue;
        }

        let mut name = String::new();
        while let Some((_, c)) = chars.peek() {
            if c.is_whitespace() || *c == '=' || *c == '/' || *c == '>' {
                break;
            }
            name.push(*c);
            chars.next();
        }

        if name.is_empty() {
            break;
        }

        while let Some((_, c)) = chars.peek() {
            if c.is_whitespace() {
                chars.next();
            } else {
                break;
            }
        }

        let mut value = String::new();
        if let Some((_, '=')) = chars.peek() {
            chars.next();
            while let Some((_, c)) = chars.peek() {
                if c.is_whitespace() {
                    chars.next();
                } else {
                    break;
                }
            }

            if let Some((_, quote @ ('"' | '\''))) = chars.peek() {
                let quote = *quote;
                chars.next();
                while let Some((_, c)) = chars.next() {
                    if c == quote {
                        break;
                    }
                    value.push(c);
                }
            } else {
                while let Some((_, c)) = chars.peek() {
                    if c.is_whitespace() || *c == '/' || *c == '>' {
                        break;
                    }
                    value.push(*c);
                    chars.next();
                }
            }
        }

        attrs.push((name, value));
    }

    attrs
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn allows_safe_avatar_markup() {
        let input = r#"<div class="sw-avatar" style="left: 20px; top: 10px;"><img src="https://cdn.example.com/jedi.png" alt="jedi" /><span>Master</span></div>"#;
        let sanitized = sanitize_widget_html(input).unwrap();
        assert!(sanitized.contains("<div class=\"sw-avatar\""));
        assert!(sanitized.contains("style=\"left: 20px; top: 10px\""));
        assert!(sanitized.contains("<img src=\"https://cdn.example.com/jedi.png\" alt=\"jedi\" />"));
        assert!(sanitized.contains("<span>Master</span>"));
    }

    #[test]
    fn strips_script_tags_and_subtree() {
        let input = r#"<div>Hello<script>alert(1)</script>World</div>"#;
        let sanitized = sanitize_widget_html(input).unwrap();
        assert_eq!(sanitized, "<div>HelloWorld</div>");
    }

    #[test]
    fn strips_event_handlers() {
        let input = r#"<img src="https://example.com/a.png" onload="alert(1)" onerror="alert(2)" />"#;
        let sanitized = sanitize_widget_html(input).unwrap();
        assert_eq!(sanitized, r#"<img src="https://example.com/a.png" />"#);
    }

    #[test]
    fn rejects_non_https_image_sources() {
        let input = r#"<img src="http://insecure.com/a.png" /><img src="javascript:alert(1)" />"#;
        let sanitized = sanitize_widget_html(input).unwrap();
        assert_eq!(sanitized, "<img /><img />");
    }

    #[test]
    fn strips_dangerous_css_urls_and_javascript() {
        let input = r#"<div style="background-color: red; background-image: url('javascript:alert(1)'); color: blue;">Text</div>"#;
        let sanitized = sanitize_widget_html(input).unwrap();
        // url( triggers complete style strip
        assert_eq!(sanitized, "<div>Text</div>");
    }

    #[test]
    fn auto_closes_unclosed_tags() {
        let input = r#"<div><span><b>bold</div>"#;
        let sanitized = sanitize_widget_html(input).unwrap();
        assert_eq!(sanitized, "<div><span><b>bold</b></span></div>");
    }

    #[test]
    fn payload_too_large_is_refused() {
        let big = "a".repeat(MAX_WIDGET_HTML_BYTES + 1);
        let err = sanitize_widget_html(&big).unwrap_err();
        assert_eq!(
            err,
            HtmlSanitizeError::PayloadTooLarge(MAX_WIDGET_HTML_BYTES)
        );
    }
}
