//! Composite-scene wire types shared by svc-presentation's scene routes.
//!
//! Mirrors hub-api's scene API (`hub_api/services/overlay_scene_service.py`, alembic 0113) so the
//! data plane and control plane can never drift: kinds, canvas sizes and bounds are the SAME
//! integer codes and limits, and the canvas is always derived from `canvas_resolution_code`
//! (1 = 1080p, 2 = 2k, 3 = 4k), never from a stored string or width/height pair.

use serde::{Deserialize, Serialize};

/// Hard cap on placed components in one community scene; identical to hub-api's `MAX_COMPONENTS`.
pub const MAX_SCENE_COMPONENTS: usize = 128;

/// Largest sanitized-HTML widget fragment accepted, in bytes (16 KiB).
pub const MAX_WIDGET_HTML_BYTES: usize = 16_384;

/// Canvas size a community scene renders at, stored as `canvas_resolution_code`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum CanvasResolution {
    /// Code 1: 1920 x 1080.
    #[serde(rename = "1080p")]
    Res1080p,
    /// Code 2: 2560 x 1440.
    #[serde(rename = "2k")]
    Res2k,
    /// Code 3: 3840 x 2160.
    #[serde(rename = "4k")]
    Res4k,
}

impl CanvasResolution {
    /// Parses the stored integer code; `None` for anything outside 1..=3.
    pub const fn from_code(code: i16) -> Option<Self> {
        match code {
            1 => Some(Self::Res1080p),
            2 => Some(Self::Res2k),
            3 => Some(Self::Res4k),
            _ => None,
        }
    }

    /// The stored integer code.
    pub const fn to_code(self) -> i16 {
        match self {
            Self::Res1080p => 1,
            Self::Res2k => 2,
            Self::Res4k => 3,
        }
    }

    /// `(width, height)` in pixels.
    pub const fn dimensions(self) -> (i32, i32) {
        match self {
            Self::Res1080p => (1920, 1080),
            Self::Res2k => (2560, 1440),
            Self::Res4k => (3840, 2160),
        }
    }

    /// Wire label (`1080p`, `2k`, `4k`).
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Res1080p => "1080p",
            Self::Res2k => "2k",
            Self::Res4k => "4k",
        }
    }
}

/// What a widget emits (`output_kind_code`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum WidgetOutputKind {
    /// Plain text, rendered with `textContent`.
    Text,
    /// An https image URL.
    Image,
    /// A server-sanitized HTML/SVG fragment.
    SanitizedHtml,
    /// Structured state on a contract surface.
    Surface,
}

impl WidgetOutputKind {
    /// Parses the stored integer code.
    pub const fn from_code(code: i16) -> Option<Self> {
        match code {
            1 => Some(Self::Text),
            2 => Some(Self::Image),
            3 => Some(Self::SanitizedHtml),
            4 => Some(Self::Surface),
            _ => None,
        }
    }

    /// The stored integer code.
    pub const fn to_code(self) -> i16 {
        match self {
            Self::Text => 1,
            Self::Image => 2,
            Self::SanitizedHtml => 3,
            Self::Surface => 4,
        }
    }

    /// Wire label.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Text => "text",
            Self::Image => "image",
            Self::SanitizedHtml => "sanitized_html",
            Self::Surface => "surface",
        }
    }

    /// Parses a wire label.
    pub fn from_label(label: &str) -> Option<Self> {
        match label {
            "text" => Some(Self::Text),
            "image" => Some(Self::Image),
            "sanitized_html" => Some(Self::SanitizedHtml),
            "surface" => Some(Self::Surface),
            _ => None,
        }
    }
}

/// `status_code` of a placed component.
pub const STATUS_VISIBLE: i16 = 1;
/// `status_code` of a placed component the streamer has hidden.
pub const STATUS_HIDDEN: i16 = 2;

/// The scene's canvas as the page needs it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CanvasDto {
    /// Wire label (`1080p`, `2k`, `4k`).
    pub resolution: String,
    /// Width in pixels, derived from the code.
    pub width: i32,
    /// Height in pixels, derived from the code.
    pub height: i32,
}

impl From<CanvasResolution> for CanvasDto {
    fn from(res: CanvasResolution) -> Self {
        let (width, height) = res.dimensions();
        Self {
            resolution: res.as_str().to_string(),
            width,
            height,
        }
    }
}

impl Default for CanvasDto {
    fn default() -> Self {
        CanvasResolution::Res1080p.into()
    }
}

/// One placed widget instance. `instance_id` is the row's public UUID; the numeric key never
/// leaves the database.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SceneComponentDto {
    /// Public UUID of the placement.
    pub instance_id: String,
    /// Bundle that owns the widget.
    pub bundle_app_id: String,
    /// Widget id, unique only within its bundle.
    pub widget_id: String,
    /// Surface the widget renders (`custom` for non-surface kinds).
    pub surface_id: String,
    /// What the widget emits.
    pub output_kind: WidgetOutputKind,
    /// Human label.
    pub display_name: String,
    /// Left edge in canvas pixels.
    pub x: i32,
    /// Top edge in canvas pixels.
    pub y: i32,
    /// Width in canvas pixels.
    pub width: i32,
    /// Height in canvas pixels.
    pub height: i32,
    /// Stacking order.
    pub z_index: i32,
    /// `false` when the streamer hid it.
    pub visible: bool,
    /// Bundle-defined props (never rendered as markup).
    pub props: serde_json::Value,
}

/// The canvas plus every placed component, back to front.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct SceneLayoutDto {
    /// Canvas size.
    pub canvas: CanvasDto,
    /// Placed components.
    pub components: Vec<SceneComponentDto>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canvas_codes_match_hub_api() {
        for (code, label, dims) in [
            (1, "1080p", (1920, 1080)),
            (2, "2k", (2560, 1440)),
            (3, "4k", (3840, 2160)),
        ] {
            let res = CanvasResolution::from_code(code).unwrap();
            assert_eq!(res.to_code(), code);
            assert_eq!(res.as_str(), label);
            assert_eq!(res.dimensions(), dims);
            let dto = CanvasDto::from(res);
            assert_eq!((dto.width, dto.height), dims);
        }
        assert_eq!(CanvasResolution::from_code(0), None);
        assert_eq!(CanvasResolution::from_code(4), None);
        assert_eq!(CanvasDto::default().resolution, "1080p");
    }

    #[test]
    fn output_kind_codes_and_labels_roundtrip() {
        for code in 1..=4 {
            let kind = WidgetOutputKind::from_code(code).unwrap();
            assert_eq!(kind.to_code(), code);
            assert_eq!(WidgetOutputKind::from_label(kind.as_str()), Some(kind));
        }
        assert_eq!(WidgetOutputKind::from_code(5), None);
        assert_eq!(WidgetOutputKind::from_label("nope"), None);
    }

    #[test]
    fn scene_limit_is_aligned_with_hub_api() {
        assert_eq!(MAX_SCENE_COMPONENTS, 128);
    }

    #[test]
    fn layout_serde_roundtrip() {
        let layout = SceneLayoutDto {
            canvas: CanvasResolution::Res2k.into(),
            components: vec![SceneComponentDto {
                instance_id: "0195f3a2-8c11-7000-8000-000000000001".to_string(),
                bundle_app_id: "waddles.core.dj".to_string(),
                widget_id: "now_playing".to_string(),
                surface_id: "music.now_playing".to_string(),
                output_kind: WidgetOutputKind::Surface,
                display_name: "Now Playing".to_string(),
                x: 48,
                y: 860,
                width: 460,
                height: 160,
                z_index: 20,
                visible: true,
                props: serde_json::json!({"show_artwork": true}),
            }],
        };
        let back: SceneLayoutDto =
            serde_json::from_str(&serde_json::to_string(&layout).unwrap()).unwrap();
        assert_eq!(layout, back);
    }
}
