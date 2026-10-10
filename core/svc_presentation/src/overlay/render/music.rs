//! `music` renderer -- the Music Station browser source
//! (`render.py::render_music`) is queue-driven: it polls
//! `/overlay/<community>/music/queue` (hub-api-backed) for now-playing/
//! up-next data, not the push/live channel. `overlay_schema::OverlayPush`
//! carries no music-specific field (its own `extra` escape hatch is
//! reserved for bundle-registered widgets, never these 9 built-in
//! surfaces), so there is nothing for a `music` push to render beyond the
//! one shape every surface shares: `type: clear`. Anything else is
//! rejected loudly rather than silently ignored -- a caller pushing track
//! data here would otherwise believe it worked.
//!
//! Detokenization/escaping (`crate::overlay::detok`) therefore has nothing
//! to act on here: [`MusicContent`] carries no push-derived text at all, so
//! no token, UUID or markup can reach the wire from this surface (pinned by
//! `output_carries_no_push_derived_text_at_all`).

use overlay_schema::OverlayPush;
use overlay_schema::PushKind;
use overlay_schema::Surface;

use super::{RenderError, RenderedContent, Renderer, ResolvedTheme};

/// `music`'s only renderable push shape -- `cleared` is the only field
/// because the queue/now-playing data itself is poll-driven, not
/// push-driven (unchanged from the Python alpha).
#[derive(Debug, Clone, Default, serde::Serialize, PartialEq)]
pub struct MusicContent {
    pub cleared: bool,
}

pub struct MusicRenderer;

impl Renderer for MusicRenderer {
    fn surface(&self) -> Surface {
        Surface::Music
    }

    fn render(
        &self,
        push: &OverlayPush,
        _theme: &ResolvedTheme,
    ) -> Result<RenderedContent, RenderError> {
        if push.kind == Some(PushKind::Clear) {
            return Ok(RenderedContent::Music(MusicContent { cleared: true }));
        }
        Err(RenderError::InvalidField {
            surface: self.surface(),
            field: "type",
            reason: "music is queue-driven (poll /music/queue); push only supports type: clear",
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn theme() -> ResolvedTheme {
        ResolvedTheme {
            primary_color: "#000".to_string(),
            secondary_color: "#111".to_string(),
            font_family: "Arial".to_string(),
        }
    }

    #[test]
    fn clear_push_is_the_only_accepted_shape() {
        let push = OverlayPush {
            kind: Some(PushKind::Clear),
            ..Default::default()
        };
        let frame = MusicRenderer.render(&push, &theme()).unwrap();
        let RenderedContent::Music(content) = frame else {
            panic!("expected Music content");
        };
        assert!(content.cleared);
    }

    #[test]
    fn any_other_push_shape_is_rejected_loudly() {
        let push = OverlayPush {
            title: Some("now playing".to_string()),
            ..Default::default()
        };
        let err = MusicRenderer.render(&push, &theme()).unwrap_err();
        assert_eq!(
            err,
            RenderError::InvalidField {
                surface: Surface::Music,
                field: "type",
                reason: "music is queue-driven (poll /music/queue); push only supports type: clear",
            }
        );
    }

    #[test]
    fn an_empty_push_is_also_rejected_not_silently_accepted() {
        let err = MusicRenderer
            .render(&OverlayPush::default(), &theme())
            .unwrap_err();
        assert!(matches!(err, RenderError::InvalidField { .. }));
    }

    #[test]
    fn surface_reports_music() {
        assert_eq!(MusicRenderer.surface(), Surface::Music);
    }

    /// `music` has no free-text field, so there is nothing to detokenize or
    /// escape: its rendered output is exactly `{"cleared": true}`, even for
    /// a push stuffed with hostile text in every other field -- the
    /// sanitization contract holds by construction, not by a missing call.
    #[test]
    fn output_carries_no_push_derived_text_at_all() {
        let push = OverlayPush {
            kind: Some(PushKind::Clear),
            title: Some("<script>x</script>".to_string()),
            body: Some("{user:11111111-1111-1111-1111-111111111111}".to_string()),
            text: Some("<img src=x onerror=alert(1)>".to_string()),
            ..Default::default()
        };
        let frame = MusicRenderer.render(&push, &theme()).unwrap();
        assert_eq!(
            serde_json::to_value(&frame).unwrap(),
            serde_json::json!({"content_type": "music", "cleared": true})
        );
    }
}
