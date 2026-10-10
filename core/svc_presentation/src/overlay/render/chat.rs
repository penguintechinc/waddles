//! `chat` renderer -- #458's new chat-box widget. No Python precedent;
//! validation requires `overlay_schema::ChatMessagePayload` to be present
//! with a non-empty `text` and a `user` that is actually a tenant-tokenized
//! UUID reference (`rules/critical-rules.md` PII Tokenization: "Reference
//! users by UUID, not username ... always outside the boundary") --
//! rejecting a non-UUID `user` here is defense-in-depth against whatever
//! upstream service builds this payload accidentally leaking a raw
//! username into the field documented to never carry one.
//!
//! **Output is detokenized and escaped, never the caller's word for it.**
//! The rendered `display_name` is the hub-api-resolved name for `user`
//! (`crate::overlay::detok`), HTML-escaped, or `Unknown User` when it did
//! not resolve -- the payload's own `display_name` is bundle-origin text
//! this service cannot verify carries no PII, so it is ignored. `text` and
//! `platform` go through [`super::shared::sanitize_text`] (HTML-escape,
//! `{user:<token>}` placeholders resolved). The `user` UUID itself is
//! deliberately **not** part of [`ChatContent`]: the overlay client needs
//! only the display name, and a raw UUID on the wire is exactly what this
//! pass exists to keep off it.

use overlay_schema::OverlayPush;
use overlay_schema::Surface;

use super::shared::{is_user_uuid, resolved_display_name, sanitize_text};
use super::{RenderError, RenderedContent, Renderer, ResolvedTheme};

const MAX_TEXT_LEN: usize = 2000;

/// `chat`'s rendered content: the resolved, escaped `display_name`, the
/// escaped `platform` and `text` -- `overlay_schema::ChatMessagePayload`
/// minus the `user` UUID (see this module's doc).
#[derive(Debug, Clone, serde::Serialize, PartialEq)]
pub struct ChatContent {
    pub display_name: String,
    pub platform: String,
    pub text: String,
}

pub struct ChatRenderer;

impl Renderer for ChatRenderer {
    fn surface(&self) -> Surface {
        Surface::Chat
    }

    fn render(
        &self,
        push: &OverlayPush,
        _theme: &ResolvedTheme,
    ) -> Result<RenderedContent, RenderError> {
        let surface = self.surface();
        let chat_message = push
            .chat_message
            .as_ref()
            .ok_or(RenderError::MissingField {
                surface,
                field: "chat_message",
            })?;
        if !is_user_uuid(&chat_message.user) {
            return Err(RenderError::InvalidField {
                surface,
                field: "user",
                reason: "must be a tenant-tokenized UUID, not a raw username",
            });
        }
        if chat_message.text.trim().is_empty() {
            return Err(RenderError::InvalidField {
                surface,
                field: "text",
                reason: "must not be empty",
            });
        }
        if chat_message.text.chars().count() > MAX_TEXT_LEN {
            return Err(RenderError::InvalidField {
                surface,
                field: "text",
                reason: "exceeds maximum length",
            });
        }
        Ok(RenderedContent::Chat(ChatContent {
            display_name: resolved_display_name(&chat_message.user),
            platform: sanitize_text(&chat_message.platform),
            text: sanitize_text(&chat_message.text),
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use overlay_schema::ChatMessagePayload;

    fn theme() -> ResolvedTheme {
        ResolvedTheme {
            primary_color: "#000".to_string(),
            secondary_color: "#111".to_string(),
            font_family: "Arial".to_string(),
        }
    }

    fn valid_chat_message() -> ChatMessagePayload {
        ChatMessagePayload {
            user: "11111111-1111-1111-1111-111111111111".to_string(),
            display_name: "Display Name".to_string(),
            platform: "twitch".to_string(),
            text: "hello chat".to_string(),
        }
    }

    #[test]
    fn renders_a_valid_chat_line() {
        let push = OverlayPush {
            chat_message: Some(valid_chat_message()),
            ..Default::default()
        };
        let frame = ChatRenderer.render(&push, &theme()).unwrap();
        let RenderedContent::Chat(content) = frame else {
            panic!("expected Chat content");
        };
        assert_eq!(content.text, "hello chat");
        assert_eq!(content.platform, "twitch");
    }

    #[test]
    fn rejects_a_missing_chat_message_loudly() {
        let err = ChatRenderer
            .render(&OverlayPush::default(), &theme())
            .unwrap_err();
        assert_eq!(
            err,
            RenderError::MissingField {
                surface: Surface::Chat,
                field: "chat_message",
            }
        );
    }

    #[test]
    fn rejects_a_non_uuid_user_loudly() {
        let push = OverlayPush {
            chat_message: Some(ChatMessagePayload {
                user: "some_raw_username".to_string(),
                ..valid_chat_message()
            }),
            ..Default::default()
        };
        let err = ChatRenderer.render(&push, &theme()).unwrap_err();
        assert_eq!(
            err,
            RenderError::InvalidField {
                surface: Surface::Chat,
                field: "user",
                reason: "must be a tenant-tokenized UUID, not a raw username",
            }
        );
    }

    #[test]
    fn rejects_an_empty_text_loudly() {
        let push = OverlayPush {
            chat_message: Some(ChatMessagePayload {
                text: "   ".to_string(),
                ..valid_chat_message()
            }),
            ..Default::default()
        };
        let err = ChatRenderer.render(&push, &theme()).unwrap_err();
        assert_eq!(
            err,
            RenderError::InvalidField {
                surface: Surface::Chat,
                field: "text",
                reason: "must not be empty",
            }
        );
    }

    #[test]
    fn rejects_an_oversized_text_loudly() {
        let push = OverlayPush {
            chat_message: Some(ChatMessagePayload {
                text: "x".repeat(MAX_TEXT_LEN + 1),
                ..valid_chat_message()
            }),
            ..Default::default()
        };
        let err = ChatRenderer.render(&push, &theme()).unwrap_err();
        assert_eq!(
            err,
            RenderError::InvalidField {
                surface: Surface::Chat,
                field: "text",
                reason: "exceeds maximum length",
            }
        );
    }

    #[test]
    fn surface_reports_chat() {
        assert_eq!(ChatRenderer.surface(), Surface::Chat);
    }

    use crate::overlay::detok::test_support::{names, USER_A, USER_B, USER_UNKNOWN};
    use crate::overlay::detok::with_names;
    use egress_detokenizer::NEUTRAL_LABEL;

    fn render_in_scope(
        resolved: &[(&str, &str)],
        message: ChatMessagePayload,
    ) -> Result<RenderedContent, RenderError> {
        let push = OverlayPush {
            chat_message: Some(message),
            ..Default::default()
        };
        with_names(names(resolved), || ChatRenderer.render(&push, &theme()))
    }

    #[test]
    fn display_name_is_the_resolved_name_not_the_callers_display_name() {
        let content = render_in_scope(
            &[(USER_A, "Alice")],
            ChatMessagePayload {
                user: USER_A.to_string(),
                display_name: "raw-username-from-a-bundle".to_string(),
                ..valid_chat_message()
            },
        )
        .unwrap();
        let RenderedContent::Chat(content) = content else {
            panic!("expected Chat content");
        };
        assert_eq!(content.display_name, "Alice");
    }

    #[test]
    fn an_unresolved_user_renders_the_neutral_label_never_the_uuid_or_callers_name() {
        let content = render_in_scope(
            &[(USER_B, "Bob")],
            ChatMessagePayload {
                display_name: "raw-username-from-a-bundle".to_string(),
                ..valid_chat_message()
            },
        )
        .unwrap();
        let RenderedContent::Chat(content) = content else {
            panic!("expected Chat content");
        };
        assert_eq!(content.display_name, NEUTRAL_LABEL);
    }

    #[test]
    fn text_and_platform_are_html_escaped_and_user_tokens_resolved() {
        let content = render_in_scope(
            &[(USER_A, "Alice"), (USER_B, "B<o>b")],
            ChatMessagePayload {
                text: format!(
                    "<script>alert(1)</script> hi {{user:{USER_B}}} {{user:{USER_UNKNOWN}}}"
                ),
                platform: "tw<itch>".to_string(),
                ..valid_chat_message()
            },
        )
        .unwrap();
        let RenderedContent::Chat(content) = content else {
            panic!("expected Chat content");
        };
        assert_eq!(
            content.text,
            format!("&lt;script&gt;alert(1)&lt;/script&gt; hi B&lt;o&gt;b {NEUTRAL_LABEL}")
        );
        assert_eq!(content.platform, "tw&lt;itch&gt;");
    }

    #[test]
    fn the_user_uuid_never_reaches_the_serialized_output() {
        let content = render_in_scope(&[(USER_A, "Alice")], valid_chat_message()).unwrap();
        let json = serde_json::to_string(&content).unwrap();
        assert!(!json.contains(USER_A), "{json}");
        assert!(!json.contains("\"user\""), "{json}");
        assert!(json.contains("Alice"));
    }

    #[test]
    fn rejects_a_non_canonical_uuid_form_loudly() {
        for user in [
            "{11111111-1111-1111-1111-111111111111}",
            "urn:uuid:11111111-1111-1111-1111-111111111111",
            "11111111111111111111111111111111",
        ] {
            let err = render_in_scope(
                &[],
                ChatMessagePayload {
                    user: user.to_string(),
                    ..valid_chat_message()
                },
            )
            .unwrap_err();
            assert!(
                matches!(err, RenderError::InvalidField { field: "user", .. }),
                "{user}"
            );
        }
    }
}
