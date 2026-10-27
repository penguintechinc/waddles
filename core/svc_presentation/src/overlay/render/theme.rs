//! Per-community theme resolution -- [`RenderTheme`] carries the raw
//! `presentation_config` overrides (any field may be unset), [`resolve`]
//! fills in the same defaults `core/svc_presentation/services/render.
//! py::_theme_style` has always used so every [`super::RenderedFrame`]
//! ships a fully-resolved theme rather than making the browser overlay
//! client re-implement that fallback itself.

use serde::Serialize;

use crate::db::entities::presentation_config;

/// `render.py::_theme_style`'s existing defaults, ported verbatim -- the
/// Music Station's dark-glass look this scaffold's renderers all share.
pub const DEFAULT_PRIMARY_COLOR: &str = "#1db954";
pub const DEFAULT_SECONDARY_COLOR: &str = "#1ed760";
pub const DEFAULT_FONT_FAMILY: &str = "'Segoe UI', Tahoma, Geneva, Verdana, sans-serif";

/// The community's raw theme overrides, straight off the
/// `presentation_config` row -- any field may be `None` (not yet
/// configured), in which case [`RenderTheme::resolve`] substitutes the
/// built-in default rather than omitting the field from the rendered
/// frame.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct RenderTheme {
    pub primary_color: Option<String>,
    pub secondary_color: Option<String>,
    pub font_family: Option<String>,
}

impl RenderTheme {
    /// Fills in every unset field with its built-in default, producing the
    /// theme block every [`super::RenderedFrame`] carries.
    pub fn resolve(&self) -> ResolvedTheme {
        ResolvedTheme {
            primary_color: self
                .primary_color
                .clone()
                .unwrap_or_else(|| DEFAULT_PRIMARY_COLOR.to_string()),
            secondary_color: self
                .secondary_color
                .clone()
                .unwrap_or_else(|| DEFAULT_SECONDARY_COLOR.to_string()),
            font_family: self
                .font_family
                .clone()
                .unwrap_or_else(|| DEFAULT_FONT_FAMILY.to_string()),
        }
    }
}

impl From<&presentation_config::Model> for RenderTheme {
    fn from(model: &presentation_config::Model) -> Self {
        Self {
            primary_color: model.primary_color.clone(),
            secondary_color: model.secondary_color.clone(),
            font_family: model.font_family.clone(),
        }
    }
}

/// A fully-resolved theme -- every field is always present, no
/// `Option`/default-fallback logic left for the browser overlay client to
/// reimplement.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct ResolvedTheme {
    pub primary_color: String,
    pub secondary_color: String,
    pub font_family: String,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resolve_substitutes_every_default_when_nothing_is_configured() {
        let theme = RenderTheme::default();
        let resolved = theme.resolve();
        assert_eq!(resolved.primary_color, DEFAULT_PRIMARY_COLOR);
        assert_eq!(resolved.secondary_color, DEFAULT_SECONDARY_COLOR);
        assert_eq!(resolved.font_family, DEFAULT_FONT_FAMILY);
    }

    #[test]
    fn resolve_keeps_configured_overrides_as_is() {
        let theme = RenderTheme {
            primary_color: Some("#112233".to_string()),
            secondary_color: Some("#445566".to_string()),
            font_family: Some("Comic Sans MS".to_string()),
        };
        let resolved = theme.resolve();
        assert_eq!(resolved.primary_color, "#112233");
        assert_eq!(resolved.secondary_color, "#445566");
        assert_eq!(resolved.font_family, "Comic Sans MS");
    }

    #[test]
    fn resolve_mixes_configured_and_default_fields_independently() {
        let theme = RenderTheme {
            primary_color: Some("#abcdef".to_string()),
            secondary_color: None,
            font_family: None,
        };
        let resolved = theme.resolve();
        assert_eq!(resolved.primary_color, "#abcdef");
        assert_eq!(resolved.secondary_color, DEFAULT_SECONDARY_COLOR);
        assert_eq!(resolved.font_family, DEFAULT_FONT_FAMILY);
    }

    #[test]
    fn from_presentation_config_model_copies_each_override_field() {
        let model = presentation_config::Model {
            id: 1,
            community_id: 42,
            theme: "dark".to_string(),
            primary_color: Some("#fedcba".to_string()),
            secondary_color: None,
            font_family: Some("Arial".to_string()),
            music_enabled: true,
            crawler_speed_seconds: 20,
            config: serde_json::json!({}),
        };
        let theme = RenderTheme::from(&model);
        assert_eq!(theme.primary_color.as_deref(), Some("#fedcba"));
        assert_eq!(theme.secondary_color, None);
        assert_eq!(theme.font_family.as_deref(), Some("Arial"));
    }
}
