//! `full_screen` renderer -- full-bleed overlay, pushed content replaces
//! the entire visible area. Validation mirrors `render.py::render_full_screen`'s
//! `on_message` handler (`title`/`body`/`image_url`, `type: clear` hides).

use overlay_schema::OverlayPush;
use overlay_schema::Surface;

use super::shared::validate_text_image;
use super::{RenderError, RenderedContent, Renderer, ResolvedTheme};

pub struct FullScreenRenderer;

impl Renderer for FullScreenRenderer {
    fn surface(&self) -> Surface {
        Surface::FullScreen
    }

    fn render(
        &self,
        push: &OverlayPush,
        _theme: &ResolvedTheme,
    ) -> Result<RenderedContent, RenderError> {
        let content = validate_text_image(self.surface(), push)?;
        Ok(RenderedContent::FullScreen(content))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use overlay_schema::PushKind;

    #[test]
    fn renders_title_body_and_image_url() {
        let push = OverlayPush {
            title: Some("Now Live".to_string()),
            body: Some("Welcome in!".to_string()),
            image_url: Some("https://example.com/a.png".to_string()),
            ..Default::default()
        };
        let frame = FullScreenRenderer
            .render(
                &push,
                &ResolvedTheme {
                    primary_color: "#000".to_string(),
                    secondary_color: "#111".to_string(),
                    font_family: "Arial".to_string(),
                },
            )
            .unwrap();
        let RenderedContent::FullScreen(content) = frame else {
            panic!("expected FullScreen content");
        };
        assert_eq!(content.title.as_deref(), Some("Now Live"));
        assert_eq!(content.body.as_deref(), Some("Welcome in!"));
        assert!(!content.cleared);
    }

    #[test]
    fn clear_push_hides_the_surface() {
        let push = OverlayPush {
            kind: Some(PushKind::Clear),
            ..Default::default()
        };
        let theme = ResolvedTheme {
            primary_color: "#000".to_string(),
            secondary_color: "#111".to_string(),
            font_family: "Arial".to_string(),
        };
        let frame = FullScreenRenderer.render(&push, &theme).unwrap();
        let RenderedContent::FullScreen(content) = frame else {
            panic!("expected FullScreen content");
        };
        assert!(content.cleared);
    }

    #[test]
    fn rejects_an_empty_push_loudly() {
        let theme = ResolvedTheme {
            primary_color: "#000".to_string(),
            secondary_color: "#111".to_string(),
            font_family: "Arial".to_string(),
        };
        let err = FullScreenRenderer
            .render(&OverlayPush::default(), &theme)
            .unwrap_err();
        assert_eq!(
            err,
            RenderError::EmptyPush {
                surface: Surface::FullScreen
            }
        );
    }

    #[test]
    fn surface_reports_full_screen() {
        assert_eq!(FullScreenRenderer.surface(), Surface::FullScreen);
    }
}
