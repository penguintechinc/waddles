//! `ticker` renderer -- #458's new widget, visually the same "text runs
//! across the screen" primitive as `crawler`; reuses
//! [`super::shared::validate_text`]/[`super::shared::TextContent`] rather
//! than a second text key (see `overlay_schema::OverlayPush::text`'s own
//! doc for why the wire type already shares the field).

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
}
