//! `image` renderer -- deliberately unimplemented. #458's image-surface
//! widget is scoped to a later slice (P9); this module exists so
//! [`super::render`]'s dispatch is total over every
//! [`overlay_schema::Surface::ALL`] variant today, without pretending a
//! real renderer exists yet.
//!
//! Per `rules/general.md` Red Flags (no partial-feature placeholders)
//! and `rules/critical-rules.md` Fail-Loud Code Paths, this
//! is NOT a silent default/blank frame -- every call returns
//! [`RenderError::NotYetImplemented`] loudly, the same shape
//! `crate::error::ApiError::Unimplemented` already uses at the HTTP
//! boundary for exactly this reason. P9 replaces this module's `render`
//! body with a real implementation; it does not need to touch any other
//! surface's module or the dispatch table in [`super`].

use overlay_schema::OverlayPush;
use overlay_schema::Surface;

use super::{RenderError, RenderedContent, Renderer, ResolvedTheme};

pub struct ImageRenderer;

impl Renderer for ImageRenderer {
    fn surface(&self) -> Surface {
        Surface::Image
    }

    fn render(
        &self,
        _push: &OverlayPush,
        _theme: &ResolvedTheme,
    ) -> Result<RenderedContent, RenderError> {
        Err(RenderError::NotYetImplemented {
            surface: self.surface(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn always_fails_loudly_never_a_blank_frame() {
        let theme = ResolvedTheme {
            primary_color: "#000".to_string(),
            secondary_color: "#111".to_string(),
            font_family: "Arial".to_string(),
        };
        let err = ImageRenderer
            .render(&OverlayPush::default(), &theme)
            .unwrap_err();
        assert_eq!(
            err,
            RenderError::NotYetImplemented {
                surface: Surface::Image
            }
        );
    }

    #[test]
    fn fails_loudly_even_for_an_otherwise_well_formed_push() {
        let theme = ResolvedTheme {
            primary_color: "#000".to_string(),
            secondary_color: "#111".to_string(),
            font_family: "Arial".to_string(),
        };
        let push = OverlayPush {
            image_url: Some("https://example.com/a.png".to_string()),
            ..Default::default()
        };
        let err = ImageRenderer.render(&push, &theme).unwrap_err();
        assert!(matches!(err, RenderError::NotYetImplemented { .. }));
    }

    #[test]
    fn surface_reports_image() {
        assert_eq!(ImageRenderer.surface(), Surface::Image);
    }
}
