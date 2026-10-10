//! `full_screen` renderer -- full-bleed overlay, pushed content replaces
//! the entire visible area. Validation mirrors `render.py::render_full_screen`'s
//! `on_message` handler (`title`/`body`/`image_url`, `type: clear` hides).
//!
//! `title`/`body` are bundle-authored free text: [`super::shared::
//! validate_text_image`] returns them HTML-escaped with their
//! `{user:<token>}` placeholders replaced by hub-api-resolved display names
//! (or `Unknown User`); `image_url` is validated, not escaped (see
//! [`super::shared::validate_image_url`]) -- see `crate::overlay::detok`.

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

    use crate::overlay::detok::test_support::{names, USER_A, USER_UNKNOWN};
    use crate::overlay::detok::with_names;
    use egress_detokenizer::NEUTRAL_LABEL;

    fn plain_theme() -> ResolvedTheme {
        ResolvedTheme {
            primary_color: "#000".to_string(),
            secondary_color: "#111".to_string(),
            font_family: "Arial".to_string(),
        }
    }

    #[test]
    fn title_and_body_are_escaped_and_user_tokens_resolved() {
        let push = OverlayPush {
            title: Some(format!("<script>x</script> {{user:{USER_A}}}")),
            body: Some(format!("{{user:{USER_UNKNOWN}}} & friends")),
            ..Default::default()
        };
        let frame = with_names(names(&[(USER_A, "Al<i>ce")]), || {
            FullScreenRenderer.render(&push, &plain_theme()).unwrap()
        });
        let RenderedContent::FullScreen(content) = frame else {
            panic!("expected FullScreen content");
        };
        assert_eq!(
            content.title.as_deref(),
            Some("&lt;script&gt;x&lt;/script&gt; Al&lt;i&gt;ce")
        );
        assert_eq!(
            content.body.as_deref(),
            Some(format!("{NEUTRAL_LABEL} &amp; friends").as_str())
        );
    }

    /// regression: a hostile `image_url` cannot break out of an attribute
    /// or smuggle a user token, and is rejected loudly rather than
    /// silently dropped.
    #[test]
    fn rejects_an_image_url_that_could_break_out_of_an_attribute() {
        for url in ["https://x/a\"onerror=\"alert(1)", "https://x/{user:abc}"] {
            let push = OverlayPush {
                image_url: Some(url.to_string()),
                ..Default::default()
            };
            let err = FullScreenRenderer
                .render(&push, &plain_theme())
                .unwrap_err();
            assert!(
                matches!(
                    err,
                    RenderError::InvalidField {
                        field: "image_url",
                        ..
                    }
                ),
                "{url}"
            );
        }
    }

    /// regression: a raw token/UUID/`<script>` never reaches the output,
    /// even when the renderer runs with no resolved-name scope at all.
    #[test]
    fn no_raw_token_uuid_or_markup_reaches_the_output_without_a_scope() {
        let push = OverlayPush {
            title: Some(format!("<script>x</script>{{user:{USER_A}}}")),
            ..Default::default()
        };
        let frame = FullScreenRenderer.render(&push, &plain_theme()).unwrap();
        let json = serde_json::to_string(&frame).unwrap();
        assert!(
            !json.contains("<script") && !json.contains(USER_A),
            "{json}"
        );
        assert!(json.contains(NEUTRAL_LABEL));
    }
}
