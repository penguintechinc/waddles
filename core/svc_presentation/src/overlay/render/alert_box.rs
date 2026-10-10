//! `alert_box` renderer -- #458's new alert widget (follow/sub/raid/cheer/
//! donation). No Python precedent (this surface didn't exist in
//! `render.py`); validation requires `overlay_schema::AlertPayload` to be
//! present with a non-empty `alert_type`, and -- like `chat` -- a `user`,
//! when present, that is a tenant-tokenized UUID, not a raw username.
//!
//! **Output is detokenized and escaped, never the caller's word for it.**
//! When `user` is present the rendered `display_name` is the hub-api-
//! resolved name for it (`crate::overlay::detok`), HTML-escaped, or
//! `Unknown User` when it did not resolve; the payload's own `display_name`
//! is then ignored (bundle-origin text this service cannot verify carries
//! no PII). With no `user` (e.g. an anonymous donor), `display_name` is
//! plain bundle-authored text and is sanitized like `message`:
//! HTML-escaped, `{user:<token>}` placeholders resolved. `alert_type`,
//! `message` and every string inside `amount` are sanitized the same way.
//! The `user` UUID itself is deliberately **not** part of [`AlertContent`]
//! -- the overlay client needs only the display name.

use overlay_schema::OverlayPush;
use overlay_schema::Surface;

use super::shared::{
    is_user_uuid, resolved_display_name, sanitize_json, sanitize_opt, sanitize_text,
};
use super::{RenderError, RenderedContent, Renderer, ResolvedTheme};

/// `alert_box`'s rendered content -- `overlay_schema::AlertPayload` minus
/// the `user` UUID, every remaining string sanitized (see this module's
/// doc); this is a distinct type (not a re-export) so this crate's wire
/// output never silently changes shape if `AlertPayload` itself is ever
/// extended for `extra`-only (bundle widget) use.
#[derive(Debug, Clone, serde::Serialize, PartialEq)]
pub struct AlertContent {
    pub alert_type: String,
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
        let display_name = match alert.user.as_deref() {
            Some(user) if !is_user_uuid(user) => {
                return Err(RenderError::InvalidField {
                    surface,
                    field: "user",
                    reason: "must be a tenant-tokenized UUID, not a raw username",
                });
            }
            Some(user) => Some(resolved_display_name(user)),
            None => sanitize_opt(&alert.display_name),
        };
        Ok(RenderedContent::AlertBox(AlertContent {
            alert_type: sanitize_text(&alert.alert_type),
            display_name,
            amount: alert.amount.as_ref().map(sanitize_json),
            message: sanitize_opt(&alert.message),
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
        // The display name now comes from hub-api resolution of `user`, not
        // from the payload's own (unverified) `display_name`.
        let frame = with_names(names(&[(USER_A, "Display Name")]), || {
            AlertBoxRenderer.render(&push, &theme()).unwrap()
        });
        let RenderedContent::AlertBox(content) = frame else {
            panic!("expected AlertBox content");
        };
        assert_eq!(content.alert_type, "follow");
        assert_eq!(content.display_name.as_deref(), Some("Display Name"));
        assert_eq!(content.message.as_deref(), Some("thanks for following!"));
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

    use crate::overlay::detok::test_support::{names, USER_A, USER_B, USER_UNKNOWN};
    use crate::overlay::detok::with_names;
    use egress_detokenizer::NEUTRAL_LABEL;

    fn render_in_scope(
        resolved: &[(&str, &str)],
        alert: AlertPayload,
    ) -> Result<AlertContent, RenderError> {
        let push = OverlayPush {
            alert: Some(alert),
            ..Default::default()
        };
        with_names(names(resolved), || AlertBoxRenderer.render(&push, &theme())).map(|content| {
            let RenderedContent::AlertBox(content) = content else {
                panic!("expected AlertBox content");
            };
            content
        })
    }

    fn alert(user: Option<&str>) -> AlertPayload {
        AlertPayload {
            alert_type: "sub".to_string(),
            user: user.map(str::to_string),
            display_name: Some("raw-username-from-a-bundle".to_string()),
            amount: None,
            message: None,
        }
    }

    #[test]
    fn display_name_is_the_resolved_name_when_a_user_is_present() {
        let content = render_in_scope(&[(USER_A, "A<l>ice")], alert(Some(USER_A))).unwrap();
        assert_eq!(content.display_name.as_deref(), Some("A&lt;l&gt;ice"));
    }

    #[test]
    fn an_unresolved_user_renders_the_neutral_label_never_the_callers_display_name() {
        let content = render_in_scope(&[(USER_B, "Bob")], alert(Some(USER_UNKNOWN))).unwrap();
        assert_eq!(content.display_name.as_deref(), Some(NEUTRAL_LABEL));
    }

    #[test]
    fn without_a_user_the_display_name_is_sanitized_bundle_text() {
        let content = render_in_scope(
            &[(USER_A, "Alice")],
            AlertPayload {
                display_name: Some(format!("<b>Anonymous</b> via {{user:{USER_A}}}")),
                ..alert(None)
            },
        )
        .unwrap();
        assert_eq!(
            content.display_name.as_deref(),
            Some("&lt;b&gt;Anonymous&lt;/b&gt; via Alice")
        );
    }

    #[test]
    fn without_a_user_or_display_name_there_is_no_display_name() {
        let content = render_in_scope(
            &[],
            AlertPayload {
                display_name: None,
                ..alert(None)
            },
        )
        .unwrap();
        assert_eq!(content.display_name, None);
    }

    #[test]
    fn alert_type_message_and_amount_strings_are_sanitized() {
        let content = render_in_scope(
            &[(USER_A, "Alice")],
            AlertPayload {
                alert_type: "su<b>".to_string(),
                message: Some(format!("<script>x</script> thanks {{user:{USER_A}}}")),
                amount: Some(serde_json::json!({
                    "tier": "<1>",
                    "from": format!("{{user:{USER_A}}}"),
                    "n": 5,
                })),
                ..alert(None)
            },
        )
        .unwrap();
        assert_eq!(content.alert_type, "su&lt;b&gt;");
        assert_eq!(
            content.message.as_deref(),
            Some("&lt;script&gt;x&lt;/script&gt; thanks Alice")
        );
        assert_eq!(
            content.amount,
            Some(serde_json::json!({"tier": "&lt;1&gt;", "from": "Alice", "n": 5}))
        );
    }

    #[test]
    fn the_user_uuid_never_reaches_the_serialized_output() {
        let content = render_in_scope(&[(USER_A, "Alice")], alert(Some(USER_A))).unwrap();
        let json = serde_json::to_string(&content).unwrap();
        assert!(!json.contains(USER_A), "{json}");
        assert!(!json.contains("\"user\""), "{json}");
        assert!(!json.contains("raw-username-from-a-bundle"), "{json}");
    }

    #[test]
    fn rejects_a_non_uuid_user_loudly() {
        let err = render_in_scope(&[], alert(Some("some_raw_username"))).unwrap_err();
        assert_eq!(
            err,
            RenderError::InvalidField {
                surface: Surface::AlertBox,
                field: "user",
                reason: "must be a tenant-tokenized UUID, not a raw username",
            }
        );
    }
}
