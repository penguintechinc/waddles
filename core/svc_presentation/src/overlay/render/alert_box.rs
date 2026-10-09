//! `alert_box` renderer -- #458's new alert widget (follow/sub/raid/cheer/
//! donation). No Python precedent (this surface didn't exist in
//! `render.py`); validation requires `overlay_schema::AlertPayload` to be
//! present with a non-empty `alert_type`, everything else carried through
//! as-is (already server-side-detokenized/escaped per `AlertPayload`'s own
//! doc).

use overlay_schema::OverlayPush;
use overlay_schema::Surface;

use super::{RenderError, RenderedContent, Renderer, ResolvedTheme};

/// `alert_box`'s rendered content -- mirrors `overlay_schema::AlertPayload`
/// field-for-field; this is a distinct type (not a re-export) so this
/// crate's wire output never silently changes shape if `AlertPayload`
/// itself is ever extended for `extra`-only (bundle widget) use.
#[derive(Debug, Clone, serde::Serialize, PartialEq)]
pub struct AlertContent {
    pub alert_type: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub user: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub display_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub amount: Option<serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub message: Option<String>,
}

pub struct AlertBoxRenderer;

impl Renderer for AlertBoxRenderer {
    fn surface(&self) -> Surface {
        Surface::AlertBox
    }

    fn render(
        &self,
        push: &OverlayPush,
        _theme: &ResolvedTheme,
    ) -> Result<RenderedContent, RenderError> {
        let surface = self.surface();
        let alert = push.alert.as_ref().ok_or(RenderError::MissingField {
            surface,
            field: "alert",
        })?;
        if alert.alert_type.trim().is_empty() {
            return Err(RenderError::InvalidField {
                surface,
                field: "alert_type",
                reason: "must not be empty",
            });
        }
        Ok(RenderedContent::AlertBox(AlertContent {
            alert_type: alert.alert_type.clone(),
            user: alert.user.clone(),
            display_name: alert.display_name.clone(),
            amount: alert.amount.clone(),
            message: alert.message.clone(),
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use overlay_schema::AlertPayload;

    fn theme() -> ResolvedTheme {
        ResolvedTheme {
            primary_color: "#000".to_string(),
            secondary_color: "#111".to_string(),
            font_family: "Arial".to_string(),
        }
    }

    #[test]
    fn renders_a_follow_alert() {
        let push = OverlayPush {
            alert: Some(AlertPayload {
                alert_type: "follow".to_string(),
                user: Some("11111111-1111-1111-1111-111111111111".to_string()),
                display_name: Some("Display Name".to_string()),
                amount: None,
                message: Some("thanks for following!".to_string()),
            }),
            ..Default::default()
        };
        let frame = AlertBoxRenderer.render(&push, &theme()).unwrap();
        let RenderedContent::AlertBox(content) = frame else {
            panic!("expected AlertBox content");
        };
        assert_eq!(content.alert_type, "follow");
        assert_eq!(content.display_name.as_deref(), Some("Display Name"));
    }

    #[test]
    fn rejects_a_missing_alert_loudly() {
        let err = AlertBoxRenderer
            .render(&OverlayPush::default(), &theme())
            .unwrap_err();
        assert_eq!(
            err,
            RenderError::MissingField {
                surface: Surface::AlertBox,
                field: "alert",
            }
        );
    }

    #[test]
    fn rejects_an_empty_alert_type_loudly() {
        let push = OverlayPush {
            alert: Some(AlertPayload {
                alert_type: "   ".to_string(),
                user: None,
                display_name: None,
                amount: None,
                message: None,
            }),
            ..Default::default()
        };
        let err = AlertBoxRenderer.render(&push, &theme()).unwrap_err();
        assert_eq!(
            err,
            RenderError::InvalidField {
                surface: Surface::AlertBox,
                field: "alert_type",
                reason: "must not be empty",
            }
        );
    }

    #[test]
    fn surface_reports_alert_box() {
        assert_eq!(AlertBoxRenderer.surface(), Surface::AlertBox);
    }
}
