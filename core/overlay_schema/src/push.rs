use serde::{Deserialize, Serialize};

/// The push payload body: `POST /overlay/<community>/<surface>/push`'s
/// existing JSON request body (`core/svc_presentation/blueprints/
/// overlay.py::push`), and the `payload-json` argument to the bundle
/// `overlay.push` WIT call (`wit/waddle-bundle/stage.wit`). One open,
/// surface-interpreted object -- a given [`crate::Surface`] only consumes
/// the subset of fields documented below; unused fields are simply absent
/// (`#[serde(skip_serializing_if = "Option::is_none")]` on every field, so
/// a `full_screen` push never serializes a `chat_message` key).
///
/// `title`/`body`/`image_url` mirror `render.py`'s `full_screen`/`media`
/// `on_message` handlers field-for-field; `text` mirrors `crawler`'s.
/// These four are therefore the fields that MUST NOT be renamed without
/// also updating the already-deployed `render.py` (or its eventual Rust
/// port) in the same change -- an already-running OBS browser source's
/// inline `<script>` reads these keys directly off `JSON.parse(event.
/// data)`.
#[derive(Debug, Clone, Default, Serialize, Deserialize, PartialEq)]
pub struct OverlayPush {
    /// `Some(Clear)` hides the surface's current content -- the only
    /// pre-existing special case (`render.py`'s `data.type === 'clear'`
    /// branch in every `on_message` handler). Wire key is `"type"`, not
    /// `"kind"`, to match that existing payload shape exactly.
    #[serde(rename = "type", skip_serializing_if = "Option::is_none", default)]
    pub kind: Option<PushKind>,

    /// `full_screen`/`media` -- mirrors `render.py` verbatim.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub title: Option<String>,

    /// `full_screen`/`media` -- mirrors `render.py` verbatim.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub body: Option<String>,

    /// `full_screen`/`media` -- must be `http(s)://` (enforced client-side
    /// today by `render.py`'s inline regex guard; re-validated server-side
    /// once svc-presentation-rust owns rendering, since a bundle-origin
    /// push is no longer a trusted first-party caller).
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub image_url: Option<String>,

    /// `crawler`/`ticker` -- the scrolling/ticking text payload. Mirrors
    /// `render.py`'s `crawler` handler (`data.text`) for `crawler`; `ticker`
    /// is new (#458) and reuses the identical field rather than inventing
    /// a second text key for what is visually the same "text runs across
    /// the screen" primitive.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub text: Option<String>,

    /// `alert_box` -- which alert template to play (follow/sub/raid/cheer/
    /// donation, #458's widget catalog) plus its display fields.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub alert: Option<AlertPayload>,

    /// `chat` -- one rendered chat line. `user` is a tenant-tokenized UUID
    /// reference ONLY, never a raw username/PII (`critical-rules.md` PII
    /// Tokenization, `backend-database.md`) -- `display_name` is already
    /// server-side-detokenized by the time this struct is constructed; no
    /// overlay client, bundle, or log ever sees the raw identity record.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub chat_message: Option<ChatMessagePayload>,

    /// `goals` -- current/target progress for a goal bar.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub goal: Option<GoalPayload>,

    /// `caption` -- one live closed-caption line (a chat message plus its
    /// optional translation). Same PII rule as [`Self::chat_message`]:
    /// `user` is a tenant-tokenized UUID reference only.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub caption: Option<CaptionPayload>,

    /// Escape hatch for a bundle-registered widget's own declared
    /// data-binding fields (#458 "Data binding") -- never populated by any
    /// of the 9 built-in surfaces above. The host validates size (and, once
    /// wired, the widget's own `descriptor-json` schema) but not shape;
    /// this crate makes no claim about what a bundle widget puts here.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub extra: Option<serde_json::Value>,
}

/// The one pre-existing sentinel value `render.py`'s `on_message` handlers
/// special-case. A closed enum (not a bare string) so a typo in a future
/// Rust caller (`"Clear"`/`"CLEAR"`) is a compile-time mismatch, not a
/// silently-ignored payload.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PushKind {
    Clear,
}

/// `alert_box`'s payload -- one alert_box widget event (#458's "Widgets"
/// table: "alerts (follow/sub/raid/cheer/donation)").
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct AlertPayload {
    /// `"follow" | "sub" | "raid" | "cheer" | "donation"` -- an open string
    /// rather than a closed enum: the alert-type catalog is widget-template
    /// data (#458), not a wire-protocol invariant this crate should freeze.
    pub alert_type: String,
    /// Tenant-tokenized UUID reference ONLY -- never a raw username/PII
    /// (see [`ChatMessagePayload::user`]'s doc for the full rule).
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub user: Option<String>,
    /// Already server-side-detokenized display name, HTML-escaped at
    /// render time by the consuming surface -- never raw user input passed
    /// through unescaped.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub display_name: Option<String>,
    /// E.g. cheer bits count, sub tier, donation amount -- alert-type
    /// specific, so carried as an open JSON value rather than one field per
    /// alert type.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub amount: Option<serde_json::Value>,
    pub message: Option<String>,
}

/// `chat`'s payload -- one chat-box widget line (#458's "Widgets": "chat
/// box").
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ChatMessagePayload {
    /// Tenant-tokenized UUID reference ONLY -- raw PII (name/email/
    /// username) never crosses this boundary (`critical-rules.md` PII
    /// Tokenization: "Reference users by UUID, not username ... always
    /// outside the boundary"). svc-presentation-rust/svc-ingest resolve
    /// this to a display-safe name server-side before constructing this
    /// struct, not the overlay client.
    pub user: String,
    /// Already server-side-detokenized, HTML-escaped display name.
    pub display_name: String,
    /// The platform the message originated on (`twitch`/`discord`/...).
    pub platform: String,
    /// Already HTML-escaped at construction time -- an overlay client
    /// renders this directly, never re-escapes or trusts it un-escaped.
    pub text: String,
}

/// `caption`'s payload -- one closed-caption line, the Rust port of the
/// Python `browser_source_core_module`'s `POST /api/v1/internal/captions`
/// body (`username`/`original_message`/`translated_message`/
/// `detected_language`/`target_language`/`confidence`).
///
/// Field mapping from the legacy body: `username` is replaced by the
/// `user`/`display_name` pair (a raw username never crosses this boundary
/// -- `critical-rules.md` PII Tokenization); `original_message`/
/// `translated_message`/`detected_language`/`target_language` are renamed
/// `original`/`translated`/`detected_lang`/`target_lang` to match the
/// overlay client's existing wire keys; `community_id` is gone (the
/// verified credential, never the body, names the community).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct CaptionPayload {
    /// Tenant-tokenized UUID reference ONLY -- see
    /// [`ChatMessagePayload::user`]'s doc for the full rule.
    pub user: String,
    /// Already server-side-detokenized display name (never a raw
    /// username); rendered by the overlay client as text, never markup.
    pub display_name: String,
    /// The platform the message originated on (`twitch`/`discord`/...).
    pub platform: String,
    /// The message as originally written.
    pub original: String,
    /// The translated message, if translation ran.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub translated: Option<String>,
    /// ISO 639-1-style code of the detected source language.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub detected_lang: Option<String>,
    /// ISO 639-1-style code of the target (translation) language.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub target_lang: Option<String>,
    /// Language-detection confidence, `0.0..=1.0`.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub confidence: Option<f64>,
}

/// `goals`' payload -- one goal bar's current state (#458's "Widgets":
/// "goal bars").
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct GoalPayload {
    pub label: String,
    pub current: f64,
    pub target: f64,
    /// E.g. `"followers"`, `"subs"`, `"$"` -- display-only, never parsed.
    #[serde(skip_serializing_if = "Option::is_none", default)]
    pub unit: Option<String>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_push_serializes_as_an_empty_object() {
        let push = OverlayPush::default();
        assert_eq!(serde_json::to_string(&push).unwrap(), "{}");
    }

    #[test]
    fn clear_push_matches_the_pre_existing_render_py_wire_shape() {
        let push = OverlayPush {
            kind: Some(PushKind::Clear),
            ..Default::default()
        };
        assert_eq!(serde_json::to_string(&push).unwrap(), r#"{"type":"clear"}"#);
    }

    #[test]
    fn full_screen_media_push_matches_the_pre_existing_render_py_field_names() {
        let push = OverlayPush {
            title: Some("Now Live".to_string()),
            body: Some("Welcome in!".to_string()),
            image_url: Some("https://example.com/a.png".to_string()),
            ..Default::default()
        };
        let json: serde_json::Value = serde_json::to_value(&push).unwrap();
        assert_eq!(json["title"], "Now Live");
        assert_eq!(json["body"], "Welcome in!");
        assert_eq!(json["image_url"], "https://example.com/a.png");
        assert!(json.get("type").is_none());
    }

    #[test]
    fn crawler_push_uses_the_pre_existing_text_field() {
        let push = OverlayPush {
            text: Some("breaking news".to_string()),
            ..Default::default()
        };
        assert_eq!(
            serde_json::to_string(&push).unwrap(),
            r#"{"text":"breaking news"}"#
        );
    }

    #[test]
    fn chat_message_never_serializes_a_raw_username_field() {
        let push = OverlayPush {
            chat_message: Some(ChatMessagePayload {
                user: "11111111-1111-1111-1111-111111111111".to_string(),
                display_name: "Display Name".to_string(),
                platform: "twitch".to_string(),
                text: "hello".to_string(),
            }),
            ..Default::default()
        };
        let json = serde_json::to_string(&push).unwrap();
        assert!(!json.contains("\"username\""));
        assert!(json.contains("\"user\":\"11111111-1111-1111-1111-111111111111\""));
    }

    #[test]
    fn round_trips_every_field_through_json() {
        let push = OverlayPush {
            kind: None,
            title: Some("t".to_string()),
            body: Some("b".to_string()),
            image_url: Some("https://x/y.png".to_string()),
            text: Some("txt".to_string()),
            alert: Some(AlertPayload {
                alert_type: "follow".to_string(),
                user: Some("uuid".to_string()),
                display_name: Some("Name".to_string()),
                amount: None,
                message: Some("thanks!".to_string()),
            }),
            chat_message: Some(ChatMessagePayload {
                user: "uuid".to_string(),
                display_name: "Name".to_string(),
                platform: "discord".to_string(),
                text: "hi".to_string(),
            }),
            goal: Some(GoalPayload {
                label: "Followers".to_string(),
                current: 10.0,
                target: 100.0,
                unit: Some("followers".to_string()),
            }),
            caption: Some(CaptionPayload {
                user: "uuid".to_string(),
                display_name: "Name".to_string(),
                platform: "twitch".to_string(),
                original: "hola".to_string(),
                translated: Some("hello".to_string()),
                detected_lang: Some("es".to_string()),
                target_lang: Some("en".to_string()),
                confidence: Some(0.93),
            }),
            extra: Some(serde_json::json!({"custom": true})),
        };
        let json = serde_json::to_string(&push).unwrap();
        let back: OverlayPush = serde_json::from_str(&json).unwrap();
        assert_eq!(back, push);
    }

    #[test]
    fn caption_never_serializes_a_raw_username_field() {
        let push = OverlayPush {
            caption: Some(CaptionPayload {
                user: "11111111-1111-1111-1111-111111111111".to_string(),
                display_name: "Display Name".to_string(),
                platform: "twitch".to_string(),
                original: "hello".to_string(),
                translated: None,
                detected_lang: None,
                target_lang: None,
                confidence: None,
            }),
            ..Default::default()
        };
        let json = serde_json::to_string(&push).unwrap();
        assert!(!json.contains("\"username\""));
        assert!(json.contains("\"user\":\"11111111-1111-1111-1111-111111111111\""));
        // Unset optionals are omitted, not serialized as null.
        assert!(!json.contains("translated"));
        assert!(!json.contains("confidence"));
    }

    #[test]
    fn caption_deserializes_with_only_the_required_fields() {
        let push: OverlayPush = serde_json::from_str(
            r#"{"caption":{"user":"u","display_name":"n","platform":"discord","original":"hi"}}"#,
        )
        .unwrap();
        let caption = push.caption.unwrap();
        assert_eq!(caption.original, "hi");
        assert!(caption.translated.is_none());
        assert!(caption.confidence.is_none());
    }
}
