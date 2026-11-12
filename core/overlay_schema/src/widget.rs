use serde::{Deserialize, Serialize};

/// Standard full-screen composite canvas width in pixels (1080p default).
pub const CANVAS_WIDTH: i32 = 1920;

/// Standard full-screen composite canvas height in pixels (1080p default).
pub const CANVAS_HEIGHT: i32 = 1080;

/// Maximum full-screen composite canvas width in pixels (4K).
pub const MAX_CANVAS_WIDTH: i32 = 3840;

/// Maximum full-screen composite canvas height in pixels (4K).
pub const MAX_CANVAS_HEIGHT: i32 = 2160;

/// Maximum number of placed components in a single community scene.
pub const MAX_SCENE_COMPONENTS: usize = 32;

/// Maximum allowed length for custom HTML widget output in bytes (16 KiB).
pub const MAX_WIDGET_HTML_BYTES: usize = 16_384;

/// Supported full-screen canvas resolutions (1080p, 2K, 4K).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum CanvasResolution {
    /// 1080p (FHD): 1920 x 1080
    #[serde(rename = "1080p", alias = "1080")]
    Res1080p,
    /// 2K (QHD / 1440p): 2560 x 1440
    #[serde(rename = "2k", alias = "1440p", alias = "2560x1440")]
    Res2k,
    /// 4K (UHD / 2160p): 3840 x 2160
    #[serde(rename = "4k", alias = "2160p", alias = "3840x2160")]
    Res4k,
}

impl CanvasResolution {
    pub const fn dimensions(self) -> (i32, i32) {
        match self {
            Self::Res1080p => (1920, 1080),
            Self::Res2k => (2560, 1440),
            Self::Res4k => (3840, 2160),
        }
    }

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Res1080p => "1080p",
            Self::Res2k => "2k",
            Self::Res4k => "4k",
        }
    }

    pub fn from_str_name(s: &str) -> Option<Self> {
        match s.trim().to_ascii_lowercase().as_str() {
            "1080" | "1080p" | "fhd" => Some(Self::Res1080p),
            "2k" | "1440" | "1440p" | "qhd" => Some(Self::Res2k),
            "4k" | "2160" | "2160p" | "uhd" => Some(Self::Res4k),
            _ => None,
        }
    }
}

/// Integer enum code for widget output kind (Entity Identity & Typing standard).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[repr(i16)]
#[serde(rename_all = "snake_case")]
pub enum WidgetOutputKind {
    /// Plain-text only (rendered via `textContent`).
    Text = 1,
    /// Image/media URL (https-verified).
    Image = 2,
    /// Server-side sanitized HTML/SVG fragment.
    SanitizedHtml = 3,
    /// Structured contract surface (music.now_playing, alert, etc.).
    Surface = 4,
}

impl WidgetOutputKind {
    /// Convert to database SMALLINT code.
    pub const fn to_code(self) -> i16 {
        self as i16
    }

    /// Parse from database SMALLINT code.
    pub fn from_code(code: i16) -> Option<Self> {
        match code {
            1 => Some(Self::Text),
            2 => Some(Self::Image),
            3 => Some(Self::SanitizedHtml),
            4 => Some(Self::Surface),
            _ => None,
        }
    }

    /// Wire name string.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Text => "text",
            Self::Image => "image",
            Self::SanitizedHtml => "sanitized_html",
            Self::Surface => "surface",
        }
    }

    /// Parse from wire name string.
    pub fn from_str_name(s: &str) -> Option<Self> {
        match s {
            "text" => Some(Self::Text),
            "image" => Some(Self::Image),
            "sanitized_html" => Some(Self::SanitizedHtml),
            "surface" => Some(Self::Surface),
            _ => None,
        }
    }
}

/// Integer enum code for placed component status.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[repr(i16)]
#[serde(rename_all = "snake_case")]
pub enum ComponentStatusCode {
    /// Placed and actively rendering.
    Active = 1,
    /// Placed but hidden by streamer.
    Hidden = 2,
    /// Providing bundle is disabled in community.
    BundleDisabled = 3,
}

impl ComponentStatusCode {
    pub const fn to_code(self) -> i16 {
        self as i16
    }

    pub fn from_code(code: i16) -> Option<Self> {
        match code {
            1 => Some(Self::Active),
            2 => Some(Self::Hidden),
            3 => Some(Self::BundleDisabled),
            _ => None,
        }
    }

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Active => "active",
            Self::Hidden => "hidden",
            Self::BundleDisabled => "bundle_disabled",
        }
    }
}

/// Validates canvas coordinates and dimensions against maximum canvas bounds (4K ceiling).
pub fn validate_coordinates(
    x: i32,
    y: i32,
    width: i32,
    height: i32,
    z_index: i32,
) -> Result<(), &'static str> {
    validate_coordinates_for_canvas(x, y, width, height, z_index, MAX_CANVAS_WIDTH, MAX_CANVAS_HEIGHT)
}

/// Validates canvas coordinates and dimensions against a specific canvas size.
pub fn validate_coordinates_for_canvas(
    x: i32,
    y: i32,
    width: i32,
    height: i32,
    z_index: i32,
    canvas_width: i32,
    canvas_height: i32,
) -> Result<(), &'static str> {
    if x < 0 || x > canvas_width - 32 {
        return Err("x coordinate out of bounds for canvas");
    }
    if y < 0 || y > canvas_height - 32 {
        return Err("y coordinate out of bounds for canvas");
    }
    if width < 32 || width > canvas_width {
        return Err("width out of bounds for canvas (minimum 32px)");
    }
    if height < 32 || height > canvas_height {
        return Err("height out of bounds for canvas (minimum 32px)");
    }
    if z_index < -100 || z_index > 1000 {
        return Err("z_index must be between -100 and 1000");
    }
    Ok(())
}

/// A widget descriptor declared or registered by an app or presentation bundle.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct WidgetDescriptorDto {
    pub widget_id: String,
    pub bundle_app_id: String,
    pub display_name: String,
    #[serde(default)]
    pub description: String,
    #[serde(default = "default_category")]
    pub category: String,
    pub output_kind: WidgetOutputKind,
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub surface_id: Option<String>,
    #[serde(default = "default_true")]
    pub allow_standalone_output: bool,
    #[serde(default = "default_coord")]
    pub default_x: i32,
    #[serde(default = "default_coord")]
    pub default_y: i32,
    #[serde(default = "default_width")]
    pub default_width: i32,
    #[serde(default = "default_height")]
    pub default_height: i32,
    #[serde(default = "default_z")]
    pub default_z_index: i32,
    #[serde(default = "default_props_schema")]
    pub props_schema: serde_json::Value,
}

fn default_category() -> String {
    "custom".to_string()
}

fn default_true() -> bool {
    true
}

fn default_coord() -> i32 {
    48
}

fn default_width() -> i32 {
    400
}

fn default_height() -> i32 {
    200
}

fn default_z() -> i32 {
    10
}

fn default_props_schema() -> serde_json::Value {
    serde_json::json!({})
}

/// One placed component instance on a community's composite overlay canvas.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct SceneComponentDto {
    /// Public UUID string across boundaries (Entity Identity & Typing rule).
    pub instance_id: String,
    pub bundle_app_id: String,
    pub widget_id: String,
    #[serde(default = "default_surface_id")]
    pub surface_id: String,
    pub output_kind: WidgetOutputKind,
    pub display_name: String,
    pub x: i32,
    pub y: i32,
    pub width: i32,
    pub height: i32,
    pub z_index: i32,
    pub status: ComponentStatusCode,
    #[serde(default = "default_props_schema")]
    pub props: serde_json::Value,
}

fn default_surface_id() -> String {
    "custom".to_string()
}

/// Canvas background and styling configuration.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct CanvasThemeDto {
    #[serde(default = "default_theme")]
    pub theme: String,
    #[serde(default = "default_resolution")]
    pub resolution: String,
    #[serde(default = "default_primary_color")]
    pub primary_color: String,
    #[serde(default = "default_secondary_color")]
    pub secondary_color: String,
    #[serde(default = "default_font")]
    pub font_family: String,
    #[serde(default = "default_canvas_width")]
    pub width: i32,
    #[serde(default = "default_canvas_height")]
    pub height: i32,
}

impl Default for CanvasThemeDto {
    fn default() -> Self {
        Self {
            theme: default_theme(),
            resolution: default_resolution(),
            primary_color: default_primary_color(),
            secondary_color: default_secondary_color(),
            font_family: default_font(),
            width: CANVAS_WIDTH,
            height: CANVAS_HEIGHT,
        }
    }
}

fn default_theme() -> String {
    "dark".to_string()
}

fn default_resolution() -> String {
    "1080p".to_string()
}

fn default_primary_color() -> String {
    "#6366f1".to_string()
}

fn default_secondary_color() -> String {
    "#a855f7".to_string()
}

fn default_font() -> String {
    "Inter".to_string()
}

fn default_canvas_width() -> i32 {
    CANVAS_WIDTH
}

fn default_canvas_height() -> i32 {
    CANVAS_HEIGHT
}

/// Full composite scene layout containing canvas settings and all placed component instances.
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
pub struct SceneLayoutDto {
    pub canvas: CanvasThemeDto,
    pub components: Vec<SceneComponentDto>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn output_kind_codes_roundtrip() {
        for (kind, code, name) in [
            (WidgetOutputKind::Text, 1, "text"),
            (WidgetOutputKind::Image, 2, "image"),
            (WidgetOutputKind::SanitizedHtml, 3, "sanitized_html"),
            (WidgetOutputKind::Surface, 4, "surface"),
        ] {
            assert_eq!(kind.to_code(), code);
            assert_eq!(WidgetOutputKind::from_code(code), Some(kind));
            assert_eq!(kind.as_str(), name);
            assert_eq!(WidgetOutputKind::from_str_name(name), Some(kind));
        }
        assert_eq!(WidgetOutputKind::from_code(99), None);
        assert_eq!(WidgetOutputKind::from_str_name("invalid"), None);
    }

    #[test]
    fn component_status_codes_roundtrip() {
        for (status, code, name) in [
            (ComponentStatusCode::Active, 1, "active"),
            (ComponentStatusCode::Hidden, 2, "hidden"),
            (ComponentStatusCode::BundleDisabled, 3, "bundle_disabled"),
        ] {
            assert_eq!(status.to_code(), code);
            assert_eq!(ComponentStatusCode::from_code(code), Some(status));
            assert_eq!(status.as_str(), name);
        }
        assert_eq!(ComponentStatusCode::from_code(99), None);
    }

    #[test]
    fn resolutions_roundtrip() {
        for (res, name, (w, h)) in [
            (CanvasResolution::Res1080p, "1080p", (1920, 1080)),
            (CanvasResolution::Res2k, "2k", (2560, 1440)),
            (CanvasResolution::Res4k, "4k", (3840, 2160)),
        ] {
            assert_eq!(res.as_str(), name);
            assert_eq!(res.dimensions(), (w, h));
            assert_eq!(CanvasResolution::from_str_name(name), Some(res));
        }
        assert_eq!(CanvasResolution::from_str_name("1080"), Some(CanvasResolution::Res1080p));
        assert_eq!(CanvasResolution::from_str_name("1440p"), Some(CanvasResolution::Res2k));
        assert_eq!(CanvasResolution::from_str_name("2160p"), Some(CanvasResolution::Res4k));
    }

    #[test]
    fn coordinate_validation_bounds() {
        assert!(validate_coordinates(0, 0, 400, 200, 10).is_ok());
        assert!(validate_coordinates(1888, 1048, 1920, 1080, 1000).is_ok());
        assert!(validate_coordinates(3800, 2100, 3840, 2160, 1000).is_ok());
        assert!(validate_coordinates(-1, 0, 400, 200, 10).is_err());
        assert!(validate_coordinates(0, 2129, 400, 200, 10).is_err());
        assert!(validate_coordinates(0, 0, 31, 200, 10).is_err());
        assert!(validate_coordinates(0, 0, 400, 2161, 10).is_err());
        assert!(validate_coordinates(0, 0, 400, 200, -101).is_err());
        assert!(validate_coordinates(0, 0, 400, 200, 1001).is_err());

        // Specific canvas bounds
        assert!(validate_coordinates_for_canvas(1888, 1048, 1920, 1080, 10, 1920, 1080).is_ok());
        assert!(validate_coordinates_for_canvas(1889, 0, 400, 200, 10, 1920, 1080).is_err());
    }

    #[test]
    fn scene_layout_dto_serde_roundtrip() {
        let layout = SceneLayoutDto {
            canvas: CanvasThemeDto::default(),
            components: vec![SceneComponentDto {
                instance_id: "0195f3a2-8c11-7000-8000-000000000001".to_string(),
                bundle_app_id: "waddles.core.example.dj".to_string(),
                widget_id: "dj.music.album_now_playing".to_string(),
                surface_id: "music.now_playing".to_string(),
                output_kind: WidgetOutputKind::Surface,
                display_name: "Now Playing (Album Cover)".to_string(),
                x: 48,
                y: 860,
                width: 460,
                height: 160,
                z_index: 20,
                status: ComponentStatusCode::Active,
                props: serde_json::json!({"show_artwork": true}),
            }],
        };
        let json = serde_json::to_string(&layout).unwrap();
        let back: SceneLayoutDto = serde_json::from_str(&json).unwrap();
        assert_eq!(layout, back);
    }
}

