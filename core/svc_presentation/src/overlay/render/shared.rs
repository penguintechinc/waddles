//! Content shapes and validation shared by more than one
//! [`super::Renderer`] -- `full_screen`/`media` share [`TextImageContent`]
//! (both mirror `render.py`'s identical `on_message` handler field-for-
//! field); `crawler`/`ticker` share [`TextContent`] (`OverlayPush::text`'s
//! own doc: "ticker is new ... and reuses the identical field rather than
//! inventing a second text key").
//!
//! This module is also where every renderer's output sanitization is
//! defined once ([`sanitize_text`], [`sanitize_opt`], [`sanitize_json`],
//! [`validate_image_url`]): bundle-authored text is HTML-escaped and its
//! `{user:<token>}` placeholders are replaced with hub-api-resolved display
//! names (or `Unknown User`), so no per-surface renderer ever emits a raw
//! token, a raw UUID, or unescaped markup. The resolution map is supplied
//! by `crate::overlay::detok` -- see that module's doc for why it reaches
//! the (synchronous, signature-frozen) renderers through a scoped slot.

use overlay_schema::{OverlayPush, PushKind, Surface};
use serde::Serialize;

use super::RenderError;

pub use crate::overlay::detok::{is_user_uuid, resolved_display_name, sanitize_text};

/// Defensive length caps -- hardening this crate adds that `render.py` never
/// had (no existing deployed limit to stay byte-for-byte compatible with);
/// generous enough never to reject a legitimate title/body/chat line, tight
/// enough to reject an obvious griefing-sized payload before it reaches a
/// browser source.
pub const MAX_TITLE_LEN: usize = 500;
pub const MAX_BODY_LEN: usize = 2000;
pub const MAX_TEXT_LEN: usize = 2000;

/// `full_screen`/`media`'s rendered content -- mirrors `render.py`'s
/// `title`/`body`/`image_url` fields; `cleared` is `true` only for an
/// explicit `type: clear` push (`render.py`'s `data.type === 'clear'`
/// branch).
#[derive(Debug, Clone, Default, Serialize, PartialEq)]
pub struct TextImageContent {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub body: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub image_url: Option<String>,
    pub cleared: bool,
}

/// `crawler`/`ticker`'s rendered content -- `render.py`'s `crawler` handler
/// treats a missing/empty `text` as "nothing scrolling", so there is no
/// separate `cleared` flag here: `text: None` already means that.
#[derive(Debug, Clone, Default, Serialize, PartialEq)]
pub struct TextContent {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub text: Option<String>,
}

fn is_http_url(value: &str) -> bool {
    value.starts_with("http://") || value.starts_with("https://")
}

/// [`sanitize_text`] over an optional field.
pub fn sanitize_opt(value: &Option<String>) -> Option<String> {
    value.as_deref().map(sanitize_text)
}

/// Sanitizes every string *value* inside `value` (recursively) with
/// [`sanitize_text`] and HTML-escapes every object *key* -- a legitimate
/// key (an identifier) is unchanged by escaping, a hostile one is
/// neutralized. Numbers, booleans and nulls pass through.
pub fn sanitize_json(value: &serde_json::Value) -> serde_json::Value {
    match value {
        serde_json::Value::String(text) => serde_json::Value::String(sanitize_text(text)),
        serde_json::Value::Array(items) => {
            serde_json::Value::Array(items.iter().map(sanitize_json).collect())
        }
        serde_json::Value::Object(map) => serde_json::Value::Object(
            map.iter()
                .map(|(key, item)| (crate::overlay::detok::escape_html(key), sanitize_json(item)))
                .collect(),
        ),
        other => other.clone(),
    }
}

/// Validates a pushed `image_url` and returns it unchanged.
///
/// Not HTML-escaped: the browser client assigns it to `img.src`, where an
/// escaped `&amp;` would corrupt every query string. Instead it is
/// rejected loudly unless it is a plain `http(s)://` URL with no
/// whitespace, control, quote, angle-bracket, backslash or backtick
/// characters (a conforming URL percent-encodes all of those) and embeds no
/// `{user:` token -- so it cannot break out of an attribute or smuggle a
/// token to the browser.
pub fn validate_image_url(surface: Surface, url: &str) -> Result<String, RenderError> {
    if !is_http_url(url) {
        return Err(RenderError::InvalidField {
            surface,
            field: "image_url",
            reason: "must start with http:// or https://",
        });
    }
    if url.contains("{user:") {
        return Err(RenderError::InvalidField {
            surface,
            field: "image_url",
            reason: "must not embed a user token",
        });
    }
    if url.chars().any(|c| {
        c.is_control() || c.is_whitespace() || matches!(c, '"' | '\'' | '<' | '>' | '\\' | '`')
    }) {
        return Err(RenderError::InvalidField {
            surface,
            field: "image_url",
            reason: "must be percent-encoded (no whitespace, quote, bracket or control characters)",
        });
    }
    Ok(url.to_string())
}

/// Shared `full_screen`/`media` validation: an explicit `type: clear` push
/// always renders as cleared; otherwise at least one of
/// `title`/`body`/`image_url` must be present, each within its length cap,
/// and a present `image_url` must pass [`validate_image_url`] -- re-validated
/// server-side now that a bundle-origin push is no longer a trusted
/// first-party caller (see `overlay_schema::OverlayPush::image_url`'s own
/// doc). The returned `title`/`body` are sanitized ([`sanitize_text`]).
pub fn validate_text_image(
    surface: Surface,
    push: &OverlayPush,
) -> Result<TextImageContent, RenderError> {
    if push.kind == Some(PushKind::Clear) {
        return Ok(TextImageContent {
            cleared: true,
            ..Default::default()
        });
    }
    if push.title.is_none() && push.body.is_none() && push.image_url.is_none() {
        return Err(RenderError::EmptyPush { surface });
    }
    if let Some(title) = &push.title {
        if title.chars().count() > MAX_TITLE_LEN {
            return Err(RenderError::InvalidField {
                surface,
                field: "title",
                reason: "exceeds maximum length",
            });
        }
    }
    if let Some(body) = &push.body {
        if body.chars().count() > MAX_BODY_LEN {
            return Err(RenderError::InvalidField {
                surface,
                field: "body",
                reason: "exceeds maximum length",
            });
        }
    }
    let image_url = push
        .image_url
        .as_deref()
        .map(|url| validate_image_url(surface, url))
        .transpose()?;
    Ok(TextImageContent {
        title: sanitize_opt(&push.title),
        body: sanitize_opt(&push.body),
        image_url,
        cleared: false,
    })
}

/// Shared `crawler`/`ticker` validation: no `type: clear` special case
/// (parity with `render.py`'s `crawler` handler, which has none -- a
/// missing/empty `text` already renders as nothing scrolling), just a
/// length cap on a present `text`, which is returned sanitized
/// ([`sanitize_text`]).
pub fn validate_text(surface: Surface, push: &OverlayPush) -> Result<TextContent, RenderError> {
    if let Some(text) = &push.text {
        if text.chars().count() > MAX_TEXT_LEN {
            return Err(RenderError::InvalidField {
                surface,
                field: "text",
                reason: "exceeds maximum length",
            });
        }
    }
    Ok(TextContent {
        text: sanitize_opt(&push.text),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validate_text_image_clear_overrides_any_other_field() {
        let push = OverlayPush {
            kind: Some(PushKind::Clear),
            title: Some("ignored".to_string()),
            ..Default::default()
        };
        let content = validate_text_image(Surface::FullScreen, &push).unwrap();
        assert!(content.cleared);
        assert_eq!(content.title, None);
    }

    #[test]
    fn validate_text_image_rejects_a_completely_empty_push() {
        let err = validate_text_image(Surface::Media, &OverlayPush::default()).unwrap_err();
        assert_eq!(
            err,
            RenderError::EmptyPush {
                surface: Surface::Media
            }
        );
    }

    #[test]
    fn validate_text_image_rejects_a_non_http_image_url() {
        let push = OverlayPush {
            image_url: Some("javascript:alert(1)".to_string()),
            ..Default::default()
        };
        let err = validate_text_image(Surface::FullScreen, &push).unwrap_err();
        assert_eq!(
            err,
            RenderError::InvalidField {
                surface: Surface::FullScreen,
                field: "image_url",
                reason: "must start with http:// or https://",
            }
        );
    }

    #[test]
    fn validate_text_image_rejects_an_oversized_title() {
        let push = OverlayPush {
            title: Some("x".repeat(MAX_TITLE_LEN + 1)),
            ..Default::default()
        };
        let err = validate_text_image(Surface::Media, &push).unwrap_err();
        assert_eq!(
            err,
            RenderError::InvalidField {
                surface: Surface::Media,
                field: "title",
                reason: "exceeds maximum length",
            }
        );
    }

    #[test]
    fn validate_text_image_accepts_an_https_image_url() {
        let push = OverlayPush {
            image_url: Some("https://example.com/a.png".to_string()),
            ..Default::default()
        };
        let content = validate_text_image(Surface::FullScreen, &push).unwrap();
        assert_eq!(
            content.image_url.as_deref(),
            Some("https://example.com/a.png")
        );
        assert!(!content.cleared);
    }

    #[test]
    fn validate_text_accepts_an_absent_text_as_a_clear() {
        let content = validate_text(Surface::Crawler, &OverlayPush::default()).unwrap();
        assert_eq!(content.text, None);
    }

    #[test]
    fn validate_text_rejects_an_oversized_text() {
        let push = OverlayPush {
            text: Some("x".repeat(MAX_TEXT_LEN + 1)),
            ..Default::default()
        };
        let err = validate_text(Surface::Ticker, &push).unwrap_err();
        assert_eq!(
            err,
            RenderError::InvalidField {
                surface: Surface::Ticker,
                field: "text",
                reason: "exceeds maximum length",
            }
        );
    }

    #[test]
    fn validate_text_passes_through_a_valid_text() {
        let push = OverlayPush {
            text: Some("breaking news".to_string()),
            ..Default::default()
        };
        let content = validate_text(Surface::Crawler, &push).unwrap();
        assert_eq!(content.text.as_deref(), Some("breaking news"));
    }

    // -- output sanitization (detok + HTML-escape) -------------------

    use crate::overlay::detok::test_support::{names, USER_A, USER_UNKNOWN};
    use crate::overlay::detok::with_names;
    use egress_detokenizer::NEUTRAL_LABEL;

    fn token(user: &str) -> String {
        format!("{{user:{user}}}")
    }

    #[test]
    fn sanitize_opt_maps_none_to_none_and_some_through_sanitize_text() {
        assert_eq!(sanitize_opt(&None), None);
        let out = with_names(names(&[(USER_A, "Alice")]), || {
            sanitize_opt(&Some(format!("<i>{}</i>", token(USER_A))))
        });
        assert_eq!(out.as_deref(), Some("&lt;i&gt;Alice&lt;/i&gt;"));
    }

    #[test]
    fn sanitize_json_recurses_into_values_and_escapes_keys() {
        let value = serde_json::json!({
            "<k>": [format!("<b>{}</b>", token(USER_A)), 3, true, null],
            "n": 1.5,
        });
        let out = with_names(names(&[(USER_A, "Alice")]), || sanitize_json(&value));
        assert_eq!(
            out,
            serde_json::json!({
                "&lt;k&gt;": ["&lt;b&gt;Alice&lt;/b&gt;", 3, true, null],
                "n": 1.5,
            })
        );
    }

    #[test]
    fn validate_text_image_sanitizes_title_and_body_but_not_the_image_url() {
        let push = OverlayPush {
            title: Some(format!("<script>x</script> {}", token(USER_A))),
            body: Some(format!("{} & {}", token(USER_UNKNOWN), token(USER_A))),
            image_url: Some("https://example.com/a.png?x=1&y=2".to_string()),
            ..Default::default()
        };
        let content = with_names(names(&[(USER_A, "Al<i>ce")]), || {
            validate_text_image(Surface::FullScreen, &push).unwrap()
        });
        assert_eq!(
            content.title.as_deref(),
            Some("&lt;script&gt;x&lt;/script&gt; Al&lt;i&gt;ce")
        );
        assert_eq!(
            content.body.as_deref(),
            Some(format!("{NEUTRAL_LABEL} &amp; Al&lt;i&gt;ce").as_str())
        );
        // `&` in a URL must survive: the client assigns it to `img.src`.
        assert_eq!(
            content.image_url.as_deref(),
            Some("https://example.com/a.png?x=1&y=2")
        );
    }

    #[test]
    fn validate_text_sanitizes_the_text() {
        let push = OverlayPush {
            text: Some(format!("<marquee>{}</marquee>", token(USER_A))),
            ..Default::default()
        };
        let content = with_names(names(&[(USER_A, "Alice")]), || {
            validate_text(Surface::Crawler, &push).unwrap()
        });
        assert_eq!(
            content.text.as_deref(),
            Some("&lt;marquee&gt;Alice&lt;/marquee&gt;")
        );
    }

    #[test]
    fn validate_image_url_accepts_a_plain_http_or_https_url() {
        for url in [
            "http://example.com/a.png",
            "https://example.com/a%20b.png?x=1&y=2#frag",
        ] {
            assert_eq!(validate_image_url(Surface::Media, url).unwrap(), url);
        }
    }

    #[test]
    fn validate_image_url_rejects_attribute_breakout_and_token_smuggling() {
        let cases: [(&str, &str); 9] = [
            ("javascript:alert(1)", "must start with http:// or https://"),
            (
                "data:text/html,<script>",
                "must start with http:// or https://",
            ),
            ("https://x/{user:abc}", "must not embed a user token"),
            (
                "https://x/a\"onerror=\"alert(1)",
                "must be percent-encoded (no whitespace, quote, bracket or control characters)",
            ),
            (
                "https://x/a'b",
                "must be percent-encoded (no whitespace, quote, bracket or control characters)",
            ),
            (
                "https://x/<b>",
                "must be percent-encoded (no whitespace, quote, bracket or control characters)",
            ),
            (
                "https://x/a b",
                "must be percent-encoded (no whitespace, quote, bracket or control characters)",
            ),
            (
                "https://x/a\nb",
                "must be percent-encoded (no whitespace, quote, bracket or control characters)",
            ),
            (
                "https://x/a\\b`c",
                "must be percent-encoded (no whitespace, quote, bracket or control characters)",
            ),
        ];
        for (url, reason) in cases {
            assert_eq!(
                validate_image_url(Surface::FullScreen, url).unwrap_err(),
                RenderError::InvalidField {
                    surface: Surface::FullScreen,
                    field: "image_url",
                    reason,
                },
                "{url:?}"
            );
        }
    }
}
