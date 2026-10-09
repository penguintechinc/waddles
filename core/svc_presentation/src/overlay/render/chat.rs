//! `chat` renderer -- #458's new chat-box widget. No Python precedent;
//! validation requires `overlay_schema::ChatMessagePayload` to be present
//! with a non-empty `text` and a `user` that is actually a tenant-tokenized
//! UUID reference (`rules/critical-rules.md` PII Tokenization: "Reference
//! users by UUID, not username ... always outside the boundary") --
//! rejecting a non-UUID `user` here is defense-in-depth against whatever
//! upstream service builds this payload accidentally leaking a raw
//! username into the field documented to never carry one.

use overlay_schema::OverlayPush;
use overlay_schema::Surface;

use super::{RenderError, RenderedContent, Renderer, ResolvedTheme};

const MAX_TEXT_LEN: usize = 2000;

/// `chat`'s rendered content -- mirrors `overlay_schema::ChatMessagePayload`
/// field-for-field.
#[derive(Debug, Clone, serde::Serialize, PartialEq)]
pub struct ChatContent {
    pub user: String,
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
        if uuid::Uuid::parse_str(&chat_message.user).is_err() {
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
            user: chat_message.user.clone(),
            display_name: chat_message.display_name.clone(),
            platform: chat_message.platform.clone(),
            text: chat_message.text.clone(),
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
}
