use serde::{Deserialize, Serialize};

use crate::{OverlayPush, Surface};

/// Sent once, immediately on connect -- mirrors `core/svc_presentation/
/// blueprints/overlay.py::live`'s existing first SSE frame
/// (`{"type": "connected", "community": ..., "surface": ...}`) field for
/// field. `community` stays the URL-path slug (`services/surfaces.py`'s
/// `SLUG_RE`-validated segment), not the internally-resolved numeric
/// `community_id` (`core/bundle_capability_gate::resource::
/// ResolvedResource::Overlay`) -- that resolution is a server-internal
/// detail the wire protocol never exposes.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ConnectedFrame {
    pub community: String,
    pub surface: Surface,
}

/// One frame sent down the live overlay channel (`GET /overlay/
/// <community>/<surface>/live`, SSE today; the same JSON is the intended
/// websocket text-frame shape too -- transport-agnostic by design, not an
/// SSE-specific type). SSE encodes a frame as `data: <json>\n\n`.
///
/// `#[serde(untagged)]`: the real wire protocol has no unifying envelope
/// discriminator today (`core/svc_presentation/services/presentation_hub.
/// py` forwards a push payload as-is, with no wrapper key) -- `Connected`
/// is the only frame with its own literal `"type": "connected"` marker;
/// every other frame is an [`OverlayPush`] (itself optionally carrying
/// `"type": "clear"`, see [`crate::PushKind`]). Untagged serialization
/// reproduces that exact shape instead of introducing a new wrapper
/// (`{"kind": "push", "payload": {...}}`) no existing OBS browser source
/// or bundle expects.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(untagged)]
pub enum OverlayEnvelope {
    Connected(ConnectedFrame),
    /// Boxed: `OverlayPush` is >400 bytes (several optional nested
    /// structs) against `ConnectedFrame`'s ~32 -- boxing keeps this enum
    /// itself small regardless of how many `Connected` frames are ever
    /// queued alongside pushes (`clippy::large_enum_variant`).
    Push(Box<OverlayPush>),
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::PushKind;

    #[test]
    fn connected_frame_matches_the_pre_existing_overlay_py_wire_shape() {
        let frame = OverlayEnvelope::Connected(ConnectedFrame {
            community: "my-community".to_string(),
            surface: Surface::FullScreen,
        });
        let json: serde_json::Value = serde_json::to_value(&frame).unwrap();
        assert_eq!(json["community"], "my-community");
        assert_eq!(json["surface"], "full_screen");
    }

    #[test]
    fn push_variant_serializes_with_no_wrapper_key() {
        let frame = OverlayEnvelope::Push(Box::new(OverlayPush {
            kind: Some(PushKind::Clear),
            ..Default::default()
        }));
        assert_eq!(
            serde_json::to_string(&frame).unwrap(),
            r#"{"type":"clear"}"#
        );
    }

    #[test]
    fn connected_frame_round_trips() {
        let frame = OverlayEnvelope::Connected(ConnectedFrame {
            community: "abc".to_string(),
            surface: Surface::Chat,
        });
        let json = serde_json::to_string(&frame).unwrap();
        let back: OverlayEnvelope = serde_json::from_str(&json).unwrap();
        assert_eq!(back, frame);
    }

    #[test]
    fn push_envelope_round_trips() {
        let frame = OverlayEnvelope::Push(Box::new(OverlayPush {
            title: Some("hi".to_string()),
            ..Default::default()
        }));
        let json = serde_json::to_string(&frame).unwrap();
        let back: OverlayEnvelope = serde_json::from_str(&json).unwrap();
        assert_eq!(back, frame);
    }
}
