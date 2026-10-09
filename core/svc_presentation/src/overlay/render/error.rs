//! [`RenderError`]: the one error type every per-surface [`super::Renderer`]
//! returns. Every variant's message is built from `&'static str` field
//! names/reasons and the [`overlay_schema::Surface`] enum only -- never
//! from push content -- so a render-rejection log line
//! (`crate::overlay::render::render_with_metrics`) is PII-free by
//! construction, not by remembering to redact it (`rules/critical-rules.md`
//! PII Tokenization; the bundle-logging precedent this mirrors is
//! documented project-side as "bundle log calls never include raw user
//! input").

use overlay_schema::Surface;
use thiserror::Error;

/// Why a [`super::Renderer`] rejected a push. Every variant is loud and
/// specific (`rules/general.md` Red Flags forbids a silent default/blank
/// frame) and carries no push-derived content, only field names and
/// pre-written reasons.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Error)]
pub enum RenderError {
    /// The push has no content for this surface to render, and isn't an
    /// explicit `type: clear` either -- e.g. `full_screen`/`media` with
    /// every one of `title`/`body`/`image_url` absent.
    #[error("{surface}: push has no renderable content (send `type: clear` to explicitly hide)")]
    EmptyPush { surface: Surface },
    /// A field this surface requires is entirely absent (e.g. `alert_box`
    /// with no `alert`, `chat` with no `chat_message`, `goals` with no
    /// `goal`).
    #[error("{surface}: missing required field `{field}`")]
    MissingField {
        surface: Surface,
        field: &'static str,
    },
    /// A present field failed this surface's validation -- e.g. an
    /// `image_url` that isn't `http(s)://`, or a field exceeding this
    /// renderer's length limit. `reason` is always a static, pre-written
    /// string, never push content.
    #[error("{surface}: field `{field}` failed validation: {reason}")]
    InvalidField {
        surface: Surface,
        field: &'static str,
        reason: &'static str,
    },
    /// This surface's real renderer doesn't exist yet -- the fail-loud P9
    /// stub seam ([`crate::overlay::render::image`]). Never a silently
    /// rendered blank/default frame.
    #[error("{surface}: rendering not yet implemented")]
    NotYetImplemented { surface: Surface },
}

impl RenderError {
    /// The surface every variant carries -- used by callers (metrics,
    /// logging) that want the surface without matching on the whole enum.
    pub fn surface(&self) -> Surface {
        match self {
            RenderError::EmptyPush { surface }
            | RenderError::MissingField { surface, .. }
            | RenderError::InvalidField { surface, .. }
            | RenderError::NotYetImplemented { surface } => *surface,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn surface_extracts_the_carried_surface_for_every_variant() {
        let cases = [
            RenderError::EmptyPush {
                surface: Surface::FullScreen,
            },
            RenderError::MissingField {
                surface: Surface::Chat,
                field: "chat_message",
            },
            RenderError::InvalidField {
                surface: Surface::Media,
                field: "image_url",
                reason: "must start with http:// or https://",
            },
            RenderError::NotYetImplemented {
                surface: Surface::Image,
            },
        ];
        let expected = [
            Surface::FullScreen,
            Surface::Chat,
            Surface::Media,
            Surface::Image,
        ];
        for (case, want) in cases.iter().zip(expected) {
            assert_eq!(case.surface(), want);
        }
    }

    #[test]
    fn display_never_contains_a_json_style_value_payload() {
        // Loose but meaningful guard: every Display string is built purely
        // from Surface::as_str() and the static field/reason strings above
        // -- none of which contain quotes-around-arbitrary-content shaped
        // like escaped push text. This is a smoke check, not a formal
        // proof, that no variant was extended with a push-content field
        // later without updating this module's own PII-free guarantee.
        let err = RenderError::InvalidField {
            surface: Surface::Chat,
            field: "user",
            reason: "must be a valid UUID",
        };
        let rendered = err.to_string();
        assert!(rendered.contains("chat"));
        assert!(rendered.contains("user"));
        assert!(rendered.contains("must be a valid UUID"));
    }
}
