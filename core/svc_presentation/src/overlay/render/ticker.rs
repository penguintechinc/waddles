//! `ticker` renderer -- #458's new widget, visually the same "text runs
//! across the screen" primitive as `crawler`; reuses
//! [`super::shared::validate_text`]/[`super::shared::TextContent`] rather
//! than a second text key (see `overlay_schema::OverlayPush::text`'s own
//! doc for why the wire type already shares the field).
//!
//! `text` is bundle-authored free text: [`super::shared::validate_text`]
//! returns it HTML-escaped with its `{user:<token>}` placeholders replaced by
//! hub-api-resolved display names (or `Unknown User`) -- see
//! `crate::overlay::detok`.

use overlay_schema::OverlayPush;
use overlay_schema::Surface;

use super::shared::validate_text;
use super::{RenderError, RenderedContent, Renderer, ResolvedTheme};

pub struct TickerRenderer;

impl Renderer for TickerRenderer {
    fn surface(&self) -> Surface {
        Surface::Ticker
    }

    fn render(
        &self,
        push: &OverlayPush,
        _theme: &ResolvedTheme,
    ) -> Result<RenderedContent, RenderError> {
        let content = validate_text(self.surface(), push)?;
        Ok(RenderedContent::Ticker(content))
    }
}

#[cfg(test)]
mod tests {
    use super::super::shared::MAX_TEXT_LEN;
    use super::*;

    fn theme() -> ResolvedTheme {
        ResolvedTheme {
            primary_color: "#000".to_string(),
            secondary_color: "#111".to_string(),
            font_family: "Arial".to_string(),
        }
    }

    #[test]
    fn renders_the_ticking_text() {
        let push = OverlayPush {
            text: Some("New follower: ***".to_string()),
            ..Default::default()
        };
        let frame = TickerRenderer.render(&push, &theme()).unwrap();
        let RenderedContent::Ticker(content) = frame else {
            panic!("expected Ticker content");
        };
        assert_eq!(content.text.as_deref(), Some("New follower: ***"));
    }

    #[test]
    fn an_absent_text_renders_as_nothing_ticking() {
        let frame = TickerRenderer
            .render(&OverlayPush::default(), &theme())
            .unwrap();
        let RenderedContent::Ticker(content) = frame else {
            panic!("expected Ticker content");
        };
        assert_eq!(content.text, None);
    }

    #[test]
    fn rejects_an_oversized_text_loudly() {
        let push = OverlayPush {
            text: Some("x".repeat(MAX_TEXT_LEN + 1)),
            ..Default::default()
        };
        let err = TickerRenderer.render(&push, &theme()).unwrap_err();
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
    fn surface_reports_ticker() {
        assert_eq!(TickerRenderer.surface(), Surface::Ticker);
    }

    use crate::overlay::detok::test_support::{names, USER_A, USER_UNKNOWN};
    use crate::overlay::detok::with_names;
    use egress_detokenizer::NEUTRAL_LABEL;

    #[test]
    fn text_is_escaped_and_user_tokens_resolved() {
        let push = OverlayPush {
            text: Some(format!(
                "<img src=x onerror=alert(1)> {{user:{USER_A}}} {{user:{USER_UNKNOWN}}}"
            )),
            ..Default::default()
        };
        let frame = with_names(names(&[(USER_A, "A\"lice")]), || {
            TickerRenderer.render(&push, &theme()).unwrap()
        });
        let RenderedContent::Ticker(content) = frame else {
            panic!("expected Ticker content");
        };
        assert_eq!(
            content.text.as_deref(),
            Some(
                format!("&lt;img src=x onerror=alert(1)&gt; A&quot;lice {NEUTRAL_LABEL}").as_str()
            )
        );
    }

    /// regression: a raw token/UUID/`<script>` never reaches the output,
    /// even when the renderer runs with no resolved-name scope at all.
    #[test]
    fn no_raw_token_uuid_or_markup_reaches_the_output_without_a_scope() {
        let push = OverlayPush {
            text: Some(format!("<script>x</script>{{user:{USER_A}}}")),
            ..Default::default()
        };
        let frame = TickerRenderer.render(&push, &theme()).unwrap();
        let json = serde_json::to_string(&frame).unwrap();
        assert!(
            !json.contains("<script") && !json.contains(USER_A),
            "{json}"
        );
        assert!(json.contains(NEUTRAL_LABEL));
    }
}
