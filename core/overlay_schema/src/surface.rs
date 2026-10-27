use serde::{Deserialize, Serialize};
use std::fmt;

/// The unified overlay surface id set every consumer renders/fans out
/// against -- svc-presentation-rust's render routes, svc-streaming-rust's
/// stream-lifecycle-triggered pushes, the `overlay` WIT interface
/// (`wit/waddle-bundle/stage.wit`, kept in lockstep by doc comment there),
/// and the webui overlay designer's palette (#458).
///
/// Wire form is `snake_case` (`"full_screen"`, not `"full-screen"`) --
/// matches `core/svc_presentation/services/surfaces.py::KNOWN_SURFACES`'s
/// existing route-path literals exactly, so svc-presentation-rust's port
/// doesn't change any already-deployed OBS browser-source URL
/// (`/overlay/<community>/<surface>`).
///
/// `full_screen`/`media`/`crawler`/`music` are the four surfaces the
/// existing Python scaffold already renders (`render.py::RENDERERS` +
/// `render_music`); `alert_box`/`chat`/`goals`/`ticker`/`image` are new,
/// added for #458's widget palette. `caption` is the live closed-caption
/// (translated chat) overlay ported from `core/browser_source_core_module`'s
/// `/overlay/captions/<key>` + `/ws/captions/<community_id>` pair. The
/// pre-existing `live` (HLS livestream) surface is intentionally NOT a
/// member of this enum -- it is svc-streaming-rust's own poll-driven
/// status surface (`render_live`/`blueprints/live_stream.py`), never
/// pushed to via the `overlay`/`push` path this crate's
/// [`OverlayPush`](crate::OverlayPush) describes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Surface {
    FullScreen,
    Media,
    Crawler,
    Music,
    AlertBox,
    Chat,
    Goals,
    Ticker,
    Image,
    Caption,
}

impl Surface {
    /// Every surface, in the same order `wit/waddle-bundle/stage.wit`'s
    /// `overlay.surface` enum declares them -- a surface added to one must
    /// be added to both (and to this array) in the same PR.
    pub const ALL: &'static [Surface] = &[
        Surface::FullScreen,
        Surface::Media,
        Surface::Crawler,
        Surface::Music,
        Surface::AlertBox,
        Surface::Chat,
        Surface::Goals,
        Surface::Ticker,
        Surface::Image,
        Surface::Caption,
    ];

    /// The route-path / URL-segment form (`"full_screen"`, `"alert_box"`)
    /// -- identical to this type's own `serde` wire form, exposed as a
    /// plain `&'static str` for callers building a URL/log line rather
    /// than a JSON document.
    pub const fn as_str(self) -> &'static str {
        match self {
            Surface::FullScreen => "full_screen",
            Surface::Media => "media",
            Surface::Crawler => "crawler",
            Surface::Music => "music",
            Surface::AlertBox => "alert_box",
            Surface::Chat => "chat",
            Surface::Goals => "goals",
            Surface::Ticker => "ticker",
            Surface::Image => "image",
            Surface::Caption => "caption",
        }
    }
}

impl fmt::Display for Surface {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn as_str_matches_the_serde_snake_case_wire_form_for_every_surface() {
        for surface in Surface::ALL {
            let json = serde_json::to_string(surface).unwrap();
            let quoted = format!("\"{}\"", surface.as_str());
            assert_eq!(
                json, quoted,
                "{surface:?} as_str()/serde wire form diverged"
            );
        }
    }

    #[test]
    fn all_lists_every_variant_exactly_once() {
        let mut seen = std::collections::HashSet::new();
        for surface in Surface::ALL {
            assert!(
                seen.insert(*surface),
                "{surface:?} listed more than once in ALL"
            );
        }
        assert_eq!(
            seen.len(),
            10,
            "Surface::ALL must enumerate all 10 surfaces"
        );
    }

    #[test]
    fn round_trips_through_json_for_every_surface() {
        for surface in Surface::ALL {
            let json = serde_json::to_string(surface).unwrap();
            let back: Surface = serde_json::from_str(&json).unwrap();
            assert_eq!(back, *surface);
        }
    }

    #[test]
    fn full_screen_serializes_with_an_underscore_not_a_hyphen() {
        // The one surface name where snake_case vs. kebab-case
        // (`wit/waddle-bundle/stage.wit`'s `full-screen`) is visibly
        // different -- pin the wire form explicitly.
        assert_eq!(
            serde_json::to_string(&Surface::FullScreen).unwrap(),
            "\"full_screen\""
        );
    }

    #[test]
    fn display_matches_as_str() {
        for surface in Surface::ALL {
            assert_eq!(surface.to_string(), surface.as_str());
        }
    }
}
