//! Standalone widget page and push API.
//!
//! * `GET /{overlay_code}/widget/{bundle_app_id}/{widget_id}?key=...` -- the OBS browser-source page
//!   for ONE widget (VIEW key). Refused unless the widget is registered/placed for the community and
//!   its registry row allows standalone output.
//! * `POST /{overlay_code}/widget/{bundle_app_id}/{widget_id}/push` -- PUSH-JWT route (external
//!   clients; in-cluster bundles use the gRPC surface path instead).
//!
//! A push is published to the widget's OWN channel: a `Surface`-kind widget goes through the
//! contract pipeline to its declared surface, every other kind to the widget's private hub key
//! (`widget/<bundle_app_id>/<widget_id>`). It never lands on a shared surface, so one widget can
//! never overwrite or leak into another's output. Before anything is published the widget must be
//! registered (or placed) for THIS community and its bundle enabled -- both checked against the
//! database for the credential's community, never taken from the request.

use std::time::Instant;

use axum::extract::{Path, State};
use axum::response::Response;
use axum::{Extension, Json};
use overlay_auth::{PushCredential, ViewCredential};
use overlay_schema::WidgetOutputKind;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use utoipa::ToSchema;

use crate::error::ApiError;
use crate::http::scene_page::{html_response, scene_error, valid_id};
use crate::http::AppState;
use crate::overlay::sanitize::{sanitize_widget_html, HtmlSanitizeError};
use crate::scene::WidgetBinding;
use crate::surfaces::ingress::publish_status;
use crate::surfaces::{widget_key, PublishError};

/// The widget page, embedded at compile time.
pub const WIDGET_PAGE: &str = include_str!("templates/widget.html");

/// Longest plain-text widget payload, in characters.
pub const MAX_TEXT_CHARS: usize = 1024;

/// Longest image URL accepted.
pub const MAX_IMAGE_URL_BYTES: usize = 512;

/// Raw request-body ceiling for the push route (axum enforces it before buffering).
pub const PUSH_BODY_LIMIT: usize = 32 * 1024;

/// Path params of the widget routes.
#[derive(Debug, Deserialize)]
pub struct WidgetRouteParams {
    /// The overlay code (consumed by the guards).
    #[allow(dead_code)]
    pub overlay_code: String,
    /// Owning bundle.
    pub bundle_app_id: String,
    /// Widget id, unique only within its bundle.
    pub widget_id: String,
}

/// Body of a widget push. Exactly the field matching `output_kind` is used.
#[derive(Debug, Deserialize, Serialize, ToSchema)]
pub struct WidgetPushPayload {
    /// `text`, `image`, `sanitized_html` or `surface`; must equal the widget's registered kind.
    pub output_kind: String,
    /// Plain text (`text`).
    #[serde(default)]
    pub text: Option<String>,
    /// https image URL (`image`).
    #[serde(default)]
    pub image_url: Option<String>,
    /// HTML fragment to sanitize (`sanitized_html`).
    #[serde(default)]
    pub html: Option<String>,
    /// Structured state for the widget's contract surface (`surface`).
    #[serde(default)]
    pub payload: Option<Value>,
}

/// Success body of a widget push.
#[derive(Debug, Serialize, ToSchema)]
pub struct WidgetPushResponse {
    /// Always `published`.
    pub status: &'static str,
    /// Sequence number of the fanned-out frame.
    pub seq: u64,
}

/// Looks the widget up for `community_id`; a missing/disabled/unregistered widget is one 404.
async fn resolve_widget(
    state: &AppState,
    community_id: i64,
    params: &WidgetRouteParams,
) -> Result<WidgetBinding, ApiError> {
    if !valid_id(&params.bundle_app_id) || !valid_id(&params.widget_id) {
        return Err(ApiError::NotFound("unknown widget".to_string()));
    }
    state
        .scene
        .find_widget(community_id, &params.bundle_app_id, &params.widget_id)
        .await
        .map_err(scene_error)?
        .ok_or_else(|| ApiError::NotFound("unknown widget".to_string()))
}

/// `GET /{overlay_code}/widget/{bundle_app_id}/{widget_id}` -- standalone widget page.
#[tracing::instrument(name = "widget.page", skip_all, fields(community_id = credential.community_id))]
pub async fn page(
    State(state): State<AppState>,
    Extension(credential): Extension<ViewCredential>,
    Path(params): Path<WidgetRouteParams>,
) -> Result<Response, ApiError> {
    let binding = resolve_widget(&state, credential.community_id, &params).await?;
    if !binding.allow_standalone_output {
        return Err(ApiError::NotFound("unknown widget".to_string()));
    }
    html_response(WIDGET_PAGE, state.widget_csp.as_deref(), "widget")
}

/// Maps a contract-pipeline refusal for a `Surface`-kind widget onto the REST API.
fn publish_error_to_api(err: PublishError) -> ApiError {
    match err {
        PublishError::RateLimited { retry_after_secs } => {
            ApiError::TooManyRequests { retry_after_secs }
        }
        PublishError::Saturated => ApiError::TooManyRequests {
            retry_after_secs: 5,
        },
        PublishError::TooLarge { max } => {
            ApiError::PayloadTooLarge(format!("payload exceeds {max} bytes for this surface"))
        }
        PublishError::UnknownSurface => ApiError::Unprocessable(
            "the widget's surface is not declared by the surface contract".to_string(),
        ),
        other => ApiError::Unprocessable(publish_status(other).message().to_string()),
    }
}

/// `true` for a string without control characters.
fn no_controls(s: &str) -> bool {
    !s.chars().any(char::is_control)
}

/// Builds the sanitized frame payload for a non-surface widget kind.
fn widget_frame(kind: WidgetOutputKind, body: &WidgetPushPayload) -> Result<Value, ApiError> {
    match kind {
        WidgetOutputKind::Text => {
            let text = body
                .text
                .as_deref()
                .ok_or_else(|| ApiError::Unprocessable("text widget requires `text`".into()))?;
            if text.chars().count() > MAX_TEXT_CHARS || !no_controls(text) {
                return Err(ApiError::Unprocessable(
                    "`text` is too long or contains control characters".into(),
                ));
            }
            // Tokens become the neutral label; the page renders this with textContent.
            let cleaned = crate::surfaces::sanitize::sanitize(Value::String(text.to_string()))
                .map_err(|e| ApiError::Unprocessable(e.to_string()))?;
            Ok(json!({"output_kind": "text", "text": cleaned}))
        }
        WidgetOutputKind::Image => {
            let url = body.image_url.as_deref().ok_or_else(|| {
                ApiError::Unprocessable("image widget requires `image_url`".into())
            })?;
            let plain = url.len() <= MAX_IMAGE_URL_BYTES
                && url.starts_with("https://")
                && url.len() > "https://".len()
                && !url
                    .bytes()
                    .any(|b| b <= b' ' || matches!(b, b'<' | b'>' | b'"' | b'\'' | b'\\'));
            if !plain {
                return Err(ApiError::Unprocessable(
                    "`image_url` must be a plain https URL".into(),
                ));
            }
            Ok(json!({"output_kind": "image", "image_url": url}))
        }
        WidgetOutputKind::SanitizedHtml => {
            let html = body
                .html
                .as_deref()
                .ok_or_else(|| ApiError::Unprocessable("html widget requires `html`".into()))?;
            let cleaned = sanitize_widget_html(html).map_err(|e| match e {
                HtmlSanitizeError::PayloadTooLarge(_) => ApiError::PayloadTooLarge(e.to_string()),
            })?;
            Ok(json!({"output_kind": "sanitized_html", "html": cleaned}))
        }
        // Handled by the caller (contract pipeline); never reaches here.
        WidgetOutputKind::Surface => Err(ApiError::Internal(anyhow::anyhow!(
            "surface widgets do not build widget frames"
        ))),
    }
}

/// `POST /{overlay_code}/widget/{bundle_app_id}/{widget_id}/push` -- push to ONE widget.
#[tracing::instrument(name = "widget.push", skip_all, fields(community_id = credential.community_id))]
pub async fn push(
    State(state): State<AppState>,
    Path(params): Path<WidgetRouteParams>,
    Extension(credential): Extension<PushCredential>,
    Json(body): Json<WidgetPushPayload>,
) -> Result<Json<WidgetPushResponse>, ApiError> {
    let community_id = credential.community_id;
    // 1. The widget must be registered/placed for THIS community with its bundle enabled.
    let binding = resolve_widget(&state, community_id, &params).await?;
    // 2. The pushed kind must be the registered one: no html into a text widget, no re-typing.
    let wire_kind = WidgetOutputKind::from_label(&body.output_kind).ok_or_else(|| {
        ApiError::BadRequest(format!("unsupported output_kind {:?}", body.output_kind))
    })?;
    if wire_kind != binding.output_kind {
        return Err(ApiError::Unprocessable(
            "output_kind does not match the widget's registered kind".to_string(),
        ));
    }
    let now = Instant::now();
    // 3. Publish on the widget's own channel.
    let frame = if binding.output_kind == WidgetOutputKind::Surface {
        let surface_id = binding
            .surface_id
            .as_deref()
            .ok_or_else(|| ApiError::Unprocessable("the widget declares no surface".into()))?;
        let payload = body
            .payload
            .as_ref()
            .ok_or_else(|| ApiError::Unprocessable("surface widget requires `payload`".into()))?;
        let bytes = serde_json::to_vec(payload)
            .map_err(|e| ApiError::Internal(anyhow::anyhow!("re-serializing payload: {e}")))?;
        state
            .surfaces
            .process(community_id, surface_id, &bytes, now)
            .map_err(publish_error_to_api)?
    } else {
        let payload = widget_frame(binding.output_kind, &body)?;
        let key = widget_key(&params.bundle_app_id, &params.widget_id);
        state
            .surfaces
            .publish_widget(community_id, &key, payload, now)
            .map_err(publish_error_to_api)?
    };
    tracing::debug!(community_id, seq = frame.seq, "widget frame published");
    Ok(Json(WidgetPushResponse {
        status: "published",
        seq: frame.seq,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn body(kind: &str) -> WidgetPushPayload {
        WidgetPushPayload {
            output_kind: kind.to_string(),
            text: None,
            image_url: None,
            html: None,
            payload: None,
        }
    }

    #[test]
    fn widget_page_has_exactly_one_script_and_a_tight_csp() {
        let csp = crate::scene::build_csp(WIDGET_PAGE, &crate::scene::img_origins("", ""))
            .expect("exactly one script");
        let img = csp
            .split(';')
            .find(|d| d.trim().starts_with("img-src"))
            .unwrap();
        assert!(img.contains("'self'"));
        for forbidden in ["http:", "data:", "*"] {
            assert!(!img.contains(forbidden), "{img}");
        }
        let script = crate::scene::inline_script(WIDGET_PAGE).unwrap();
        assert_eq!(script.matches("innerHTML").count(), 1);
    }

    #[test]
    fn text_frames_are_bounded_and_tokens_neutralised() {
        let mut b = body("text");
        b.text = Some("hi {user:11111111-1111-1111-1111-111111111111}".into());
        let v = widget_frame(WidgetOutputKind::Text, &b).unwrap();
        assert_eq!(v["text"], "hi Unknown User");
        for bad in ["a\u{1b}b".to_string(), "x".repeat(MAX_TEXT_CHARS + 1)] {
            b.text = Some(bad);
            assert!(matches!(
                widget_frame(WidgetOutputKind::Text, &b),
                Err(ApiError::Unprocessable(_))
            ));
        }
        b.text = None;
        assert!(widget_frame(WidgetOutputKind::Text, &b).is_err());
    }

    #[test]
    fn image_frames_accept_only_plain_https_urls() {
        let mut b = body("image");
        for ok in [
            "https://i.ytimg.com/vi/x/hq.jpg",
            "https://a.example/b.png?x=1",
        ] {
            b.image_url = Some(ok.into());
            assert_eq!(
                widget_frame(WidgetOutputKind::Image, &b).unwrap()["image_url"],
                ok
            );
        }
        for bad in [
            "http://a.example/b.png",
            "javascript:alert(1)",
            "data:image/png;base64,AAAA",
            "https://",
            "https://a.example/ b",
            "https://a.example/\"onerror=x",
            "https://a.example/<x>",
            "//a.example/b.png",
        ] {
            b.image_url = Some(bad.into());
            assert!(
                matches!(
                    widget_frame(WidgetOutputKind::Image, &b),
                    Err(ApiError::Unprocessable(_))
                ),
                "{bad}"
            );
        }
        b.image_url = Some(format!(
            "https://a.example/{}",
            "x".repeat(MAX_IMAGE_URL_BYTES)
        ));
        assert!(widget_frame(WidgetOutputKind::Image, &b).is_err());
        b.image_url = None;
        assert!(widget_frame(WidgetOutputKind::Image, &b).is_err());
    }

    #[test]
    fn html_frames_are_sanitized_and_bounded() {
        let mut b = body("sanitized_html");
        b.html = Some(r#"<div onclick="x()">ok<script>alert(1)</script></div>"#.into());
        let v = widget_frame(WidgetOutputKind::SanitizedHtml, &b).unwrap();
        assert_eq!(v["html"], "<div>ok</div>");
        b.html = Some("a".repeat(overlay_schema::MAX_WIDGET_HTML_BYTES + 1));
        assert!(matches!(
            widget_frame(WidgetOutputKind::SanitizedHtml, &b),
            Err(ApiError::PayloadTooLarge(_))
        ));
        b.html = None;
        assert!(widget_frame(WidgetOutputKind::SanitizedHtml, &b).is_err());
        assert!(matches!(
            widget_frame(WidgetOutputKind::Surface, &b),
            Err(ApiError::Internal(_))
        ));
    }

    #[test]
    fn publish_errors_map_to_rest_statuses() {
        assert!(matches!(
            publish_error_to_api(PublishError::RateLimited {
                retry_after_secs: 3
            }),
            ApiError::TooManyRequests {
                retry_after_secs: 3
            }
        ));
        assert!(matches!(
            publish_error_to_api(PublishError::Saturated),
            ApiError::TooManyRequests { .. }
        ));
        assert!(matches!(
            publish_error_to_api(PublishError::TooLarge { max: 10 }),
            ApiError::PayloadTooLarge(_)
        ));
        assert!(matches!(
            publish_error_to_api(PublishError::UnknownSurface),
            ApiError::Unprocessable(_)
        ));
        assert!(matches!(
            publish_error_to_api(PublishError::NotJson),
            ApiError::Unprocessable(_)
        ));
    }
}
