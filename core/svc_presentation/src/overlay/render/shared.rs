//! Content shapes and validation shared by more than one
//! [`super::Renderer`] -- `full_screen`/`media` share [`TextImageContent`]
//! (both mirror `render.py`'s identical `on_message` handler field-for-
//! field); `crawler`/`ticker` share [`TextContent`] (`OverlayPush::text`'s
//! own doc: "ticker is new ... and reuses the identical field rather than
//! inventing a second text key").

use overlay_schema::{OverlayPush, PushKind, Surface};
use serde::Serialize;

use super::RenderError;

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

/// Shared `full_screen`/`media` validation: an explicit `type: clear` push
/// always renders as cleared; otherwise at least one of
/// `title`/`body`/`image_url` must be present, each within its length cap,
/// and a present `image_url` must be `http(s)://` -- re-validated
/// server-side now that a bundle-origin push is no longer a trusted
/// first-party caller (see `overlay_schema::OverlayPush::image_url`'s own
/// doc).
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
    if let Some(image_url) = &push.image_url {
        if !is_http_url(image_url) {
            return Err(RenderError::InvalidField {
                surface,
                field: "image_url",
                reason: "must start with http:// or https://",
            });
        }
    }
    Ok(TextImageContent {
        title: push.title.clone(),
        body: push.body.clone(),
        image_url: push.image_url.clone(),
        cleared: false,
    })
}

/// Shared `crawler`/`ticker` validation: no `type: clear` special case
/// (parity with `render.py`'s `crawler` handler, which has none -- a
/// missing/empty `text` already renders as nothing scrolling), just a
/// length cap on a present `text`.
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
        text: push.text.clone(),
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
}
