//! `crawler` renderer -- bottom-screen scrolling text. Mirrors
//! `render.py::render_crawler`'s `on_message` handler (`data.text`,
//! falling back to empty when absent -- no `type: clear` special case
//! needed here, unlike `full_screen`/`media`).

use overlay_schema::OverlayPush;
use overlay_schema::Surface;

use super::shared::validate_text;
use super::{RenderError, RenderedContent, Renderer, ResolvedTheme};

pub struct CrawlerRenderer;

impl Renderer for CrawlerRenderer {
    fn surface(&self) -> Surface {
        Surface::Crawler
    }

    fn render(
        &self,
        push: &OverlayPush,
        _theme: &ResolvedTheme,
    ) -> Result<RenderedContent, RenderError> {
        let content = validate_text(self.surface(), push)?;
        Ok(RenderedContent::Crawler(content))
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
    fn renders_the_scrolling_text() {
        let push = OverlayPush {
            text: Some("breaking news".to_string()),
            ..Default::default()
        };
        let frame = CrawlerRenderer.render(&push, &theme()).unwrap();
        let RenderedContent::Crawler(content) = frame else {
            panic!("expected Crawler content");
        };
        assert_eq!(content.text.as_deref(), Some("breaking news"));
    }

    #[test]
    fn an_absent_text_renders_as_nothing_scrolling() {
        let frame = CrawlerRenderer
            .render(&OverlayPush::default(), &theme())
            .unwrap();
        let RenderedContent::Crawler(content) = frame else {
            panic!("expected Crawler content");
        };
        assert_eq!(content.text, None);
    }

    #[test]
    fn rejects_an_oversized_text_loudly() {
        let push = OverlayPush {
            text: Some("x".repeat(MAX_TEXT_LEN + 1)),
            ..Default::default()
        };
        let err = CrawlerRenderer.render(&push, &theme()).unwrap_err();
        assert_eq!(
            err,
            RenderError::InvalidField {
                surface: Surface::Crawler,
                field: "text",
                reason: "exceeds maximum length",
            }
        );
    }

    #[test]
    fn surface_reports_crawler() {
        assert_eq!(CrawlerRenderer.surface(), Surface::Crawler);
    }
}
