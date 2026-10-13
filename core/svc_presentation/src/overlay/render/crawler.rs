//! `crawler` renderer -- bottom-screen scrolling text. Mirrors
//! `render.py::render_crawler`'s `on_message` handler (`data.text`,
//! falling back to empty when absent -- no `type: clear` special case
//! needed here, unlike `full_screen`/`media`).
//!
//! `text` is bundle-authored free text: [`super::shared::validate_text`]
//! returns it HTML-escaped with its `{user:<token>}` placeholders replaced by
//! hub-api-resolved display names (or `Unknown User`) -- see
//! `crate::overlay::detok`.

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

    use crate::overlay::detok::test_support::{names, USER_A, USER_UNKNOWN};
    use crate::overlay::detok::with_names;
    use egress_detokenizer::NEUTRAL_LABEL;

    #[test]
    fn text_is_escaped_and_user_tokens_resolved() {
        let push = OverlayPush {
            text: Some(format!(
                "<script>alert(1)</script> {{user:{USER_A}}} & {{user:{USER_UNKNOWN}}}"
            )),
            ..Default::default()
        };
        let frame = with_names(names(&[(USER_A, "Al<i>ce")]), || {
            CrawlerRenderer.render(&push, &theme()).unwrap()
        });
        let RenderedContent::Crawler(content) = frame else {
            panic!("expected Crawler content");
        };
        assert_eq!(
            content.text.as_deref(),
            Some(
                format!(
                    "&lt;script&gt;alert(1)&lt;/script&gt; Al&lt;i&gt;ce &amp; {NEUTRAL_LABEL}"
                )
                .as_str()
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
        let frame = CrawlerRenderer.render(&push, &theme()).unwrap();
        let json = serde_json::to_string(&frame).unwrap();
        assert!(
            !json.contains("<script") && !json.contains(USER_A),
            "{json}"
        );
        assert!(json.contains(NEUTRAL_LABEL));
    }
}
