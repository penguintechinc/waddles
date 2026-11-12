//! Standalone widget page and push API:
//! `GET /{overlay_code}/widget/{widget_id}?key=...`
//! `POST /{overlay_code}/widget/{widget_id}/push?key=...`

use std::sync::LazyLock;

use axum::extract::{Path, State};
use axum::http::{header, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::{Extension, Json};
use base64::Engine as _;
use overlay_auth::{PushCredential, ViewCredential};
use overlay_schema::{Surface, WidgetOutputKind};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use utoipa::ToSchema;

use crate::error::ApiError;
use crate::http::AppState;
use crate::overlay::render::{RenderedContent, RenderedFrame, ResolvedTheme, TextImageContent};
use crate::overlay::sanitize::sanitize_widget_html;

const WIDGET_PAGE: &str = include_str!("templates/widget.html");

fn inline_script(page: &str) -> Option<&str> {
    if page.matches("<script").count() != 1 || page.matches("</script>").count() != 1 {
        return None;
    }
    let open = page.find("<script>")? + "<script>".len();
    let close = page.find("</script>")?;
    page.get(open..close)
}

fn build_csp(page: &str) -> Option<String> {
    let script = inline_script(page)?;
    let digest = Sha256::digest(script.as_bytes());
    let hash = base64::engine::general_purpose::STANDARD.encode(digest);
    Some(format!(
        "default-src 'none'; script-src 'sha256-{hash}'; style-src 'unsafe-inline'; \
         img-src http: https: data:; connect-src 'self'; base-uri 'none'; form-action 'none'"
    ))
}

static WIDGET_CSP: LazyLock<Option<String>> = LazyLock::new(|| build_csp(WIDGET_PAGE));

#[derive(Debug, Deserialize)]
pub struct WidgetRouteParams {
    pub overlay_code: String,
    pub widget_id: String,
}

#[derive(Debug, Deserialize, Serialize, ToSchema)]
pub struct WidgetPushPayload {
    pub output_kind: String,
    #[serde(default)]
    pub html: Option<String>,
    #[serde(default)]
    pub image_url: Option<String>,
    #[serde(default)]
    pub title: Option<String>,
    #[serde(default)]
    pub text: Option<String>,
    #[serde(default)]
    pub surface_id: Option<String>,
    #[serde(default)]
    pub payload: Option<serde_json::Value>,
}

/// `GET /{overlay_code}/widget/{widget_id}` -- standalone widget page for OBS browser source.
#[tracing::instrument(name = "widget.page", skip_all)]
pub async fn page(
    Extension(_credential): Extension<ViewCredential>,
) -> Result<Response, ApiError> {
    let csp = WIDGET_CSP
        .as_deref()
        .ok_or_else(|| ApiError::Internal(anyhow::anyhow!("malformed widget template: CSP generation failed")))?;

    let mut response = (StatusCode::OK, WIDGET_PAGE).into_response();
    let headers = response.headers_mut();
    headers.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("text/html; charset=utf-8"),
    );
    headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    headers.insert(
        header::REFERRER_POLICY,
        HeaderValue::from_static("no-referrer"),
    );
    headers.insert(
        header::CONTENT_SECURITY_POLICY,
        HeaderValue::from_str(csp)
            .map_err(|e| ApiError::Internal(anyhow::anyhow!("invalid CSP header value: {e}")))?,
    );
    Ok(response)
}

/// `POST /{overlay_code}/widget/{widget_id}/push` -- push update to a specific widget.
#[tracing::instrument(name = "widget.push", skip_all)]
pub async fn push(
    State(state): State<AppState>,
    Path(params): Path<WidgetRouteParams>,
    Extension(credential): Extension<PushCredential>,
    Json(payload): Json<WidgetPushPayload>,
) -> Result<StatusCode, ApiError> {
    let _output_kind = WidgetOutputKind::from_str_name(&payload.output_kind).ok_or_else(|| {
        ApiError::BadRequest(format!("unsupported output_kind '{}'", payload.output_kind))
    })?;

    let sanitized_html = if let Some(ref raw_h) = payload.html {
        Some(sanitize_widget_html(raw_h).map_err(|e| ApiError::BadRequest(e.to_string()))?)
    } else {
        None
    };

    let frame = RenderedFrame {
        surface: Surface::FullScreen,
        theme: ResolvedTheme {
            primary_color: "#1db954".to_string(),
            secondary_color: "#1ed760".to_string(),
            font_family: "sans-serif".to_string(),
        },
        content: RenderedContent::FullScreen(TextImageContent {
            title: payload.title,
            body: payload.text,
            image_url: payload.image_url,
            cleared: false,
            widget_id: Some(params.widget_id),
            html: sanitized_html,
        }),
    };

    state
        .frame_hub
        .publish(credential.community_id, Surface::FullScreen, frame);

    Ok(StatusCode::OK)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn widget_page_has_valid_csp() {
        let csp = build_csp(WIDGET_PAGE).expect("CSP generation failed");
        assert!(csp.contains("script-src 'sha256-"));
    }
}
