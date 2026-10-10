//! `caption` renderer -- the Rust port of the Python
//! `core/browser_source_core_module`'s closed-caption overlay. A caption is
//! one chat line plus its optional translation, shown by the OBS browser
//! source (`crate::http::captions`) and replayed from `caption_events` on
//! reconnect.
//!
//! Validation requires `overlay_schema::CaptionPayload` to be present with:
//! - `user` -- a tenant-tokenized UUID, never a raw username
//!   (`rules/critical-rules.md` PII Tokenization: "Reference users by UUID,
//!   not username ... always outside the boundary"). Rejecting a non-UUID
//!   here is defense-in-depth against an upstream service accidentally
//!   leaking a raw username into the field documented to never carry one;
//! - a non-empty `display_name` (already server-side-detokenized upstream),
//!   `platform`, and `original`;
//! - every string within a length cap and free of control characters. NUL
//!   in particular is rejected because Postgres `TEXT` cannot store it --
//!   without this check one hostile chat line would turn into a 500 at the
//!   persistence step instead of a clean 400 at the door;
//! - `confidence`, when present, finite and within `0.0..=1.0`.
//!
//! Output text is deliberately NOT HTML-escaped here: the only sink is the
//! overlay page's `textContent` (see `crate::http::captions`' template, which
//! never uses `innerHTML`), so escaping at this layer would double-encode
//! (`&amp;lt;`) rather than add safety. Escaping belongs at the sink.
//!
//! A `type: clear` push has no caption to render and is rejected as a
//! missing field (loud, like `chat`) rather than treated as a no-op.

use overlay_schema::{CaptionPayload, OverlayPush, Surface};

use super::{RenderError, RenderedContent, Renderer, ResolvedTheme};

/// Length caps -- hardening with no legacy equivalent (the Python path
/// accepted any size). Generous enough never to reject a real chat line or
/// translation, tight enough to reject a griefing-sized payload before it
/// is broadcast to every connected overlay.
pub const MAX_TEXT_LEN: usize = 2000;
pub const MAX_DISPLAY_NAME_LEN: usize = 255;
/// `caption_events.platform` is `VARCHAR(50)`.
pub const MAX_PLATFORM_LEN: usize = 50;
/// `caption_events.detected_language`/`target_language` are `VARCHAR(10)`.
pub const MAX_LANG_LEN: usize = 10;

/// `caption`'s rendered content -- mirrors `overlay_schema::CaptionPayload`
/// field-for-field; a distinct type (not a re-export) so this crate's wire
/// output never silently changes shape if the schema type is extended.
#[derive(Debug, Clone, serde::Serialize, PartialEq)]
pub struct CaptionContent {
    /// The author's tenant-tokenized UUID (parsed during validation, so no
    /// downstream consumer ever re-parses a string it must trust).
    pub user: uuid::Uuid,
    pub display_name: String,
    pub platform: String,
    pub original: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub translated: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detected_lang: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub target_lang: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub confidence: Option<f64>,
}

pub struct CaptionRenderer;

impl Renderer for CaptionRenderer {
    fn surface(&self) -> Surface {
        Surface::Caption
    }

    fn render(
        &self,
        push: &OverlayPush,
        _theme: &ResolvedTheme,
    ) -> Result<RenderedContent, RenderError> {
        render_caption(push).map(RenderedContent::Caption)
    }
}

/// Validates `push` as a caption and returns its content. The single
/// validation entry point -- [`CaptionRenderer`], the ingest handler and the
/// websocket replay path all go through it, so a frame is only ever
/// broadcast or stored if it passed exactly these checks.
pub fn render_caption(push: &OverlayPush) -> Result<CaptionContent, RenderError> {
    let surface = Surface::Caption;
    let caption = push.caption.as_ref().ok_or(RenderError::MissingField {
        surface,
        field: "caption",
    })?;
    let user = validate_caption(surface, caption)?;
    Ok(CaptionContent {
        user,
        display_name: caption.display_name.clone(),
        platform: caption.platform.clone(),
        original: caption.original.clone(),
        translated: caption.translated.clone(),
        detected_lang: caption.detected_lang.clone(),
        target_lang: caption.target_lang.clone(),
        confidence: caption.confidence,
    })
}

/// Validates every field of `caption` and returns the parsed author UUID.
fn validate_caption(surface: Surface, caption: &CaptionPayload) -> Result<uuid::Uuid, RenderError> {
    let Ok(user) = uuid::Uuid::parse_str(&caption.user) else {
        return Err(RenderError::InvalidField {
            surface,
            field: "user",
            reason: "must be a tenant-tokenized UUID, not a raw username",
        });
    };
    check_text(
        surface,
        "display_name",
        &caption.display_name,
        MAX_DISPLAY_NAME_LEN,
    )?;
    check_token(surface, "platform", &caption.platform, MAX_PLATFORM_LEN)?;
    check_text(surface, "original", &caption.original, MAX_TEXT_LEN)?;
    if let Some(translated) = &caption.translated {
        check_text(surface, "translated", translated, MAX_TEXT_LEN)?;
    }
    if let Some(lang) = &caption.detected_lang {
        check_token(surface, "detected_lang", lang, MAX_LANG_LEN)?;
    }
    if let Some(lang) = &caption.target_lang {
        check_token(surface, "target_lang", lang, MAX_LANG_LEN)?;
    }
    if let Some(confidence) = caption.confidence {
        if !confidence.is_finite() || !(0.0..=1.0).contains(&confidence) {
            return Err(RenderError::InvalidField {
                surface,
                field: "confidence",
                reason: "must be a number between 0 and 1",
            });
        }
    }
    Ok(user)
}

/// Free text: non-blank, within `max` chars, no control characters other
/// than newline/carriage-return/tab.
fn check_text(
    surface: Surface,
    field: &'static str,
    value: &str,
    max: usize,
) -> Result<(), RenderError> {
    if value.trim().is_empty() {
        return Err(RenderError::InvalidField {
            surface,
            field,
            reason: "must not be empty",
        });
    }
    if value.chars().count() > max {
        return Err(RenderError::InvalidField {
            surface,
            field,
            reason: "exceeds maximum length",
        });
    }
    if value
        .chars()
        .any(|c| c.is_control() && !matches!(c, '\n' | '\r' | '\t'))
    {
        return Err(RenderError::InvalidField {
            surface,
            field,
            reason: "contains control characters",
        });
    }
    Ok(())
}

/// Short identifier-like value (platform name, language code): 1..=`max`
/// ASCII alphanumerics, `-` or `_` (so region subtags like `zh-CN` pass).
fn check_token(
    surface: Surface,
    field: &'static str,
    value: &str,
    max: usize,
) -> Result<(), RenderError> {
    if value.is_empty() {
        return Err(RenderError::InvalidField {
            surface,
            field,
            reason: "must not be empty",
        });
    }
    if value.chars().count() > max {
        return Err(RenderError::InvalidField {
            surface,
            field,
            reason: "exceeds maximum length",
        });
    }
    if !value
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
    {
        return Err(RenderError::InvalidField {
            surface,
            field,
            reason: "may contain only letters, digits, '-' and '_'",
        });
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use overlay_schema::PushKind;

    const UUID: &str = "11111111-1111-1111-1111-111111111111";

    fn theme() -> ResolvedTheme {
        ResolvedTheme {
            primary_color: "#000".to_string(),
            secondary_color: "#111".to_string(),
            font_family: "Arial".to_string(),
        }
    }

    fn valid() -> CaptionPayload {
        CaptionPayload {
            user: UUID.to_string(),
            display_name: "Display Name".to_string(),
            platform: "twitch".to_string(),
            original: "hola amigos".to_string(),
            translated: Some("hello friends".to_string()),
            detected_lang: Some("es".to_string()),
            target_lang: Some("en".to_string()),
            confidence: Some(0.93),
        }
    }

    fn push_with(caption: CaptionPayload) -> OverlayPush {
        OverlayPush {
            caption: Some(caption),
            ..Default::default()
        }
    }

    fn invalid_field(push: &OverlayPush) -> (&'static str, &'static str) {
        match render_caption(push).unwrap_err() {
            RenderError::InvalidField { field, reason, .. } => (field, reason),
            other => panic!("expected InvalidField, got {other:?}"),
        }
    }

    #[test]
    fn surface_is_caption() {
        assert_eq!(CaptionRenderer.surface(), Surface::Caption);
    }

    #[test]
    fn renders_a_fully_populated_caption() {
        let content = render_caption(&push_with(valid())).unwrap();
        assert_eq!(content.user.to_string(), UUID);
        assert_eq!(content.display_name, "Display Name");
        assert_eq!(content.original, "hola amigos");
        assert_eq!(content.translated.as_deref(), Some("hello friends"));
        assert_eq!(content.confidence, Some(0.93));
    }

    #[test]
    fn renders_a_caption_with_only_the_required_fields() {
        let mut caption = valid();
        caption.translated = None;
        caption.detected_lang = None;
        caption.target_lang = None;
        caption.confidence = None;
        let content = render_caption(&push_with(caption)).unwrap();
        assert!(content.translated.is_none());
        assert!(content.confidence.is_none());
    }

    #[test]
    fn renderer_trait_wraps_the_content_in_the_caption_variant() {
        let rendered = CaptionRenderer
            .render(&push_with(valid()), &theme())
            .unwrap();
        let RenderedContent::Caption(content) = rendered else {
            panic!("expected Caption content");
        };
        assert_eq!(content.platform, "twitch");
    }

    #[test]
    fn markup_in_text_is_carried_verbatim_not_escaped() {
        // Escaping is the sink's job (textContent); escaping here would
        // double-encode. This pins that the renderer is a validator, not a
        // sanitizer.
        let mut caption = valid();
        caption.original = "<b>hi</b> & bye".to_string();
        let content = render_caption(&push_with(caption)).unwrap();
        assert_eq!(content.original, "<b>hi</b> & bye");
    }

    #[test]
    fn newlines_and_tabs_are_allowed_in_text() {
        let mut caption = valid();
        caption.original = "line one\nline two\tend".to_string();
        assert!(render_caption(&push_with(caption)).is_ok());
    }

    #[test]
    fn rejects_a_missing_caption_loudly() {
        let err = render_caption(&OverlayPush::default()).unwrap_err();
        assert_eq!(
            err,
            RenderError::MissingField {
                surface: Surface::Caption,
                field: "caption"
            }
        );
    }

    #[test]
    fn a_clear_push_has_no_caption_and_is_rejected() {
        let push = OverlayPush {
            kind: Some(PushKind::Clear),
            ..Default::default()
        };
        assert!(matches!(
            render_caption(&push).unwrap_err(),
            RenderError::MissingField {
                field: "caption",
                ..
            }
        ));
    }

    #[test]
    fn rejects_a_non_uuid_user_as_a_raw_username() {
        let mut caption = valid();
        caption.user = "some_raw_username".to_string();
        assert_eq!(
            invalid_field(&push_with(caption)),
            (
                "user",
                "must be a tenant-tokenized UUID, not a raw username"
            )
        );
    }

    #[test]
    fn rejection_messages_never_echo_push_content() {
        let mut caption = valid();
        caption.user = "leaky_raw_username".to_string();
        let message = render_caption(&push_with(caption)).unwrap_err().to_string();
        assert!(!message.contains("leaky_raw_username"));
    }

    #[test]
    fn rejects_blank_required_text_fields() {
        for field in ["display_name", "original"] {
            let mut caption = valid();
            match field {
                "display_name" => caption.display_name = "   ".to_string(),
                _ => caption.original = String::new(),
            }
            assert_eq!(
                invalid_field(&push_with(caption)),
                (field, "must not be empty"),
                "{field}"
            );
        }
    }

    #[test]
    fn rejects_a_blank_translation_rather_than_treating_it_as_none() {
        let mut caption = valid();
        caption.translated = Some(" ".to_string());
        assert_eq!(
            invalid_field(&push_with(caption)),
            ("translated", "must not be empty")
        );
    }

    #[test]
    fn rejects_over_length_text_fields() {
        let mut caption = valid();
        caption.original = "x".repeat(MAX_TEXT_LEN + 1);
        assert_eq!(
            invalid_field(&push_with(caption)),
            ("original", "exceeds maximum length")
        );

        let mut caption = valid();
        caption.translated = Some("x".repeat(MAX_TEXT_LEN + 1));
        assert_eq!(
            invalid_field(&push_with(caption)),
            ("translated", "exceeds maximum length")
        );

        let mut caption = valid();
        caption.display_name = "x".repeat(MAX_DISPLAY_NAME_LEN + 1);
        assert_eq!(
            invalid_field(&push_with(caption)),
            ("display_name", "exceeds maximum length")
        );
    }

    #[test]
    fn accepts_text_exactly_at_the_length_cap() {
        let mut caption = valid();
        caption.original = "x".repeat(MAX_TEXT_LEN);
        assert!(render_caption(&push_with(caption)).is_ok());
    }

    #[test]
    fn length_cap_counts_chars_not_bytes() {
        let mut caption = valid();
        // 2000 four-byte chars: 8000 bytes but exactly at the char cap.
        caption.original = "\u{1F600}".repeat(MAX_TEXT_LEN);
        assert!(render_caption(&push_with(caption)).is_ok());
    }

    #[test]
    fn rejects_nul_and_other_control_characters() {
        for bad in ["a\0b", "a\u{7}b", "a\u{1b}[31mb", "a\u{85}b"] {
            let mut caption = valid();
            caption.original = bad.to_string();
            assert_eq!(
                invalid_field(&push_with(caption)),
                ("original", "contains control characters"),
                "{bad:?}"
            );
        }
    }

    #[test]
    fn rejects_bad_platform_values() {
        for (value, reason) in [
            ("", "must not be empty"),
            ("tw itch", "may contain only letters, digits, '-' and '_'"),
            (
                "twitch<script>",
                "may contain only letters, digits, '-' and '_'",
            ),
        ] {
            let mut caption = valid();
            caption.platform = value.to_string();
            assert_eq!(
                invalid_field(&push_with(caption)),
                ("platform", reason),
                "{value:?}"
            );
        }
        let mut caption = valid();
        caption.platform = "p".repeat(MAX_PLATFORM_LEN + 1);
        assert_eq!(
            invalid_field(&push_with(caption)),
            ("platform", "exceeds maximum length")
        );
    }

    #[test]
    fn language_codes_allow_region_subtags_but_reject_junk() {
        let mut caption = valid();
        caption.detected_lang = Some("zh-CN".to_string());
        caption.target_lang = Some("pt_BR".to_string());
        assert!(render_caption(&push_with(caption)).is_ok());

        let mut caption = valid();
        caption.detected_lang = Some(String::new());
        assert_eq!(
            invalid_field(&push_with(caption)),
            ("detected_lang", "must not be empty")
        );

        let mut caption = valid();
        caption.target_lang = Some("en;drop".to_string());
        assert_eq!(
            invalid_field(&push_with(caption)),
            (
                "target_lang",
                "may contain only letters, digits, '-' and '_'"
            )
        );

        let mut caption = valid();
        caption.target_lang = Some("e".repeat(MAX_LANG_LEN + 1));
        assert_eq!(
            invalid_field(&push_with(caption)),
            ("target_lang", "exceeds maximum length")
        );
    }

    #[test]
    fn confidence_must_be_finite_and_between_zero_and_one() {
        for good in [0.0, 0.5, 1.0] {
            let mut caption = valid();
            caption.confidence = Some(good);
            assert!(render_caption(&push_with(caption)).is_ok(), "{good}");
        }
        for bad in [-0.01, 1.01, f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            let mut caption = valid();
            caption.confidence = Some(bad);
            assert_eq!(
                invalid_field(&push_with(caption)),
                ("confidence", "must be a number between 0 and 1"),
                "{bad}"
            );
        }
    }

    #[test]
    fn content_serializes_without_unset_optionals() {
        let mut caption = valid();
        caption.translated = None;
        caption.confidence = None;
        let content = render_caption(&push_with(caption)).unwrap();
        let json = serde_json::to_value(&content).unwrap();
        assert!(json.get("translated").is_none());
        assert!(json.get("confidence").is_none());
        assert_eq!(json["display_name"], "Display Name");
        assert_eq!(json["user"], UUID);
        assert!(json.get("username").is_none());
    }
}
