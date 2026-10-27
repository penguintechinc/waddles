//! `media` renderer -- bottom-left lower-third card. Same validation as
//! `full_screen` (`render.py::render_media`'s `on_message` handler is
//! field-for-field identical to `render_full_screen`'s), so this module
//! reuses [`super::shared::validate_text_image`] rather than duplicating it.
//!
//! `title`/`body` are bundle-authored free text, returned HTML-escaped with
//! their `{user:<token>}` placeholders replaced by hub-api-resolved display
//! names (or `Unknown User`); `image_url` is validated, not escaped -- see
//! `crate::overlay::detok` and [`super::shared::validate_image_url`].

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

    use crate::overlay::detok::test_support::{names, USER_A, USER_UNKNOWN};
    use crate::overlay::detok::with_names;
    use egress_detokenizer::NEUTRAL_LABEL;

    #[test]
    fn title_and_body_are_escaped_and_user_tokens_resolved() {
        let push = OverlayPush {
            title: Some(format!("Donation from {{user:{USER_A}}}")),
            body: Some(format!(
                "<b>{{user:{USER_UNKNOWN}}}</b> says 'hi' & \"bye\""
            )),
            image_url: Some("https://example.com/a.png?x=1&y=2".to_string()),
            ..Default::default()
        };
        let frame = with_names(names(&[(USER_A, "<Al>")]), || {
            MediaRenderer.render(&push, &theme()).unwrap()
        });
        let RenderedContent::Media(content) = frame else {
            panic!("expected Media content");
        };
        assert_eq!(content.title.as_deref(), Some("Donation from &lt;Al&gt;"));
        assert_eq!(
            content.body.as_deref(),
            Some(
                format!(
                    "&lt;b&gt;{NEUTRAL_LABEL}&lt;/b&gt; says &#39;hi&#39; &amp; &quot;bye&quot;"
                )
                .as_str()
            )
        );
        assert_eq!(
            content.image_url.as_deref(),
            Some("https://example.com/a.png?x=1&y=2"),
            "a URL is validated, not HTML-escaped"
        );
    }

    /// regression: a raw token/UUID/`<script>` never reaches the output,
    /// even when the renderer runs with no resolved-name scope at all.
    #[test]
    fn no_raw_token_uuid_or_markup_reaches_the_output_without_a_scope() {
        let push = OverlayPush {
            body: Some(format!("<script>x</script>{{user:{USER_A}}}")),
            ..Default::default()
        };
        let frame = MediaRenderer.render(&push, &theme()).unwrap();
        let json = serde_json::to_string(&frame).unwrap();
        assert!(
            !json.contains("<script") && !json.contains(USER_A),
            "{json}"
        );
        assert!(json.contains(NEUTRAL_LABEL));
    }
}
