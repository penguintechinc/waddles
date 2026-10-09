//! `media` renderer -- bottom-left lower-third card. Same validation as
//! `full_screen` (`render.py::render_media`'s `on_message` handler is
//! field-for-field identical to `render_full_screen`'s), so this module
//! reuses [`super::shared::validate_text_image`] rather than duplicating it.

use overlay_schema::OverlayPush;
use overlay_schema::Surface;

use super::shared::validate_text_image;
use super::{RenderError, RenderedContent, Renderer, ResolvedTheme};

pub struct MediaRenderer;

impl Renderer for MediaRenderer {
    fn surface(&self) -> Surface {
        Surface::Media
    }

    fn render(
        &self,
        push: &OverlayPush,
        _theme: &ResolvedTheme,
    ) -> Result<RenderedContent, RenderError> {
        let content = validate_text_image(self.surface(), push)?;
        Ok(RenderedContent::Media(content))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use overlay_schema::PushKind;

    fn theme() -> ResolvedTheme {
        ResolvedTheme {
            primary_color: "#000".to_string(),
            secondary_color: "#111".to_string(),
            font_family: "Arial".to_string(),
        }
    }

    #[test]
    fn renders_a_lower_third_card() {
        let push = OverlayPush {
            title: Some("Donation!".to_string()),
            body: Some("Thanks for the support".to_string()),
            ..Default::default()
        };
        let frame = MediaRenderer.render(&push, &theme()).unwrap();
        let RenderedContent::Media(content) = frame else {
            panic!("expected Media content");
        };
        assert_eq!(content.title.as_deref(), Some("Donation!"));
        assert!(!content.cleared);
    }

    #[test]
    fn clear_push_hides_the_card() {
        let push = OverlayPush {
            kind: Some(PushKind::Clear),
            ..Default::default()
        };
        let frame = MediaRenderer.render(&push, &theme()).unwrap();
        let RenderedContent::Media(content) = frame else {
            panic!("expected Media content");
        };
        assert!(content.cleared);
    }

    #[test]
    fn rejects_a_malformed_image_url_loudly() {
        let push = OverlayPush {
            image_url: Some("ftp://example.com/a.png".to_string()),
            ..Default::default()
        };
        let err = MediaRenderer.render(&push, &theme()).unwrap_err();
        assert_eq!(
            err,
            RenderError::InvalidField {
                surface: Surface::Media,
                field: "image_url",
                reason: "must start with http:// or https://",
            }
        );
    }

    #[test]
    fn surface_reports_media() {
        assert_eq!(MediaRenderer.surface(), Surface::Media);
    }
}
