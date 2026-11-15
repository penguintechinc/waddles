//! The composite full-screen overlay scene (VIEW-key protected, read-only):
//!
//! * `GET /{overlay_code}/scene` -- the static page an OBS browser source loads.
//! * `GET /{overlay_code}/scene/layout` -- the canvas (from `canvas_resolution_code`) and the
//!   placed components.
//! * `GET /{overlay_code}/scene/live` -- SSE of the frames for exactly the surfaces and widgets
//!   this community placed (or, with `?widget=<bundle_app_id>/<widget_id>`, that one registered
//!   widget). The keys are derived server-side from verified rows -- the browser never names them.
//!
//! The page is static, holds no community data, and is served with a hash-based CSP whose
//! `img-src` is `'self'` plus an explicit origin allowlist ([`crate::scene::build_csp`]).

use std::convert::Infallible;

use axum::extract::{Query, State};
use axum::http::{header, HeaderValue, StatusCode};
use axum::response::sse::{Event, Sse};
use axum::response::{IntoResponse, Response};
use axum::{Extension, Json};
use futures::stream::Stream;
use overlay_auth::ViewCredential;
use overlay_schema::{SceneLayoutDto, WidgetOutputKind, MAX_SCENE_COMPONENTS};
use serde::Deserialize;

use crate::error::ApiError;
use crate::http::AppState;
use crate::scene::{SceneError, WidgetBinding};
use crate::surfaces::widget_key;

/// The scene page, embedded at compile time.
pub const SCENE_PAGE: &str = include_str!("templates/scene.html");

/// Maps a scene lookup failure onto an API error; detail is logged, never returned.
pub fn scene_error(err: SceneError) -> ApiError {
    match err {
        SceneError::CommunityNotFound(_) => ApiError::NotFound("not found".to_string()),
        other => ApiError::Internal(anyhow::anyhow!(other)),
    }
}

/// Builds an HTML page response with the security headers every page carries. The page URL holds
/// the VIEW key, hence `no-store` + `no-referrer`.
pub fn html_response(
    page: &'static str,
    csp: Option<&str>,
    what: &str,
) -> Result<Response, ApiError> {
    let csp = csp.ok_or_else(|| {
        ApiError::Internal(anyhow::anyhow!(
            "malformed {what} template: exactly one inline script is required for the CSP hash"
        ))
    })?;
    let csp = HeaderValue::from_str(csp)
        .map_err(|e| ApiError::Internal(anyhow::anyhow!("invalid {what} CSP header value: {e}")))?;
    let mut response = (StatusCode::OK, page).into_response();
    let headers = response.headers_mut();
    headers.insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("text/html; charset=utf-8"),
    );
    headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    headers.insert(
        header::X_CONTENT_TYPE_OPTIONS,
        HeaderValue::from_static("nosniff"),
    );
    headers.insert(
        header::REFERRER_POLICY,
        HeaderValue::from_static("no-referrer"),
    );
    headers.insert(header::CONTENT_SECURITY_POLICY, csp);
    Ok(response)
}

/// `GET /{overlay_code}/scene` -- serves the composite scene HTML document.
#[tracing::instrument(name = "scene.page", skip_all)]
pub async fn page(
    State(state): State<AppState>,
    Extension(_credential): Extension<ViewCredential>,
) -> Result<Response, ApiError> {
    html_response(SCENE_PAGE, state.scene_csp.as_deref(), "scene")
}

/// `GET /{overlay_code}/scene/layout` -- the canvas and placed components for the credential's
/// community.
#[tracing::instrument(name = "scene.layout", skip_all, fields(community_id = credential.community_id))]
pub async fn layout(
    State(state): State<AppState>,
    Extension(credential): Extension<ViewCredential>,
) -> Result<Json<SceneLayoutDto>, ApiError> {
    let layout = state
        .scene
        .layout(credential.community_id)
        .await
        .map_err(scene_error)?;
    Ok(Json(layout))
}

/// Query of the live route.
#[derive(Debug, Deserialize)]
pub struct LiveQuery {
    /// `<bundle_app_id>/<widget_id>` to stream one registered widget instead of the whole scene.
    #[serde(default)]
    pub widget: Option<String>,
}

/// `true` for ids built from `[A-Za-z0-9._-]`, starting alphanumeric, 1..=128 bytes -- the shape
/// bundle and widget ids take, and nothing that could smuggle a path separator or `..` into a hub key.
pub fn valid_id(id: &str) -> bool {
    id.as_bytes().first().is_some_and(u8::is_ascii_alphanumeric)
        && id.len() <= 128
        && id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
}

/// The hub keys one widget's frames travel on: its own key, plus its contract surface when it is a
/// `Surface`-kind widget (a surface the contract does not declare is never subscribed).
pub fn keys_for_widget(
    state: &AppState,
    bundle_app_id: &str,
    widget_id: &str,
    kind: WidgetOutputKind,
    surface_id: Option<&str>,
) -> Vec<String> {
    if kind == WidgetOutputKind::Surface {
        surface_id
            .filter(|s| state.surfaces.contract.get(s).is_some())
            .map(|s| vec![s.to_string()])
            .unwrap_or_default()
    } else {
        vec![widget_key(bundle_app_id, widget_id)]
    }
}

/// `GET /{overlay_code}/scene/live` -- SSE for the scene or one widget.
#[tracing::instrument(name = "scene.live", skip_all, fields(community_id = credential.community_id))]
pub async fn live(
    State(state): State<AppState>,
    Extension(credential): Extension<ViewCredential>,
    Query(query): Query<LiveQuery>,
) -> Result<Sse<impl Stream<Item = Result<Event, Infallible>>>, ApiError> {
    let community_id = credential.community_id;
    let mut keys: Vec<String> = Vec::new();

    if let Some(spec) = query.widget.as_deref() {
        let (app, widget) = spec
            .split_once('/')
            .filter(|(a, w)| valid_id(a) && valid_id(w))
            .ok_or_else(|| {
                ApiError::BadRequest("widget must be <bundle_app_id>/<widget_id>".into())
            })?;
        let binding: WidgetBinding = state
            .scene
            .find_widget(community_id, app, widget)
            .await
            .map_err(scene_error)?
            .filter(|b| b.allow_standalone_output)
            .ok_or_else(|| ApiError::NotFound("unknown widget".to_string()))?;
        keys = keys_for_widget(
            &state,
            app,
            widget,
            binding.output_kind,
            binding.surface_id.as_deref(),
        );
    } else {
        let layout = state
            .scene
            .layout(community_id)
            .await
            .map_err(scene_error)?;
        for c in layout.components.iter().filter(|c| c.visible) {
            let surface =
                (c.output_kind == WidgetOutputKind::Surface).then_some(c.surface_id.as_str());
            for key in keys_for_widget(
                &state,
                &c.bundle_app_id,
                &c.widget_id,
                c.output_kind,
                surface,
            ) {
                if !keys.contains(&key) {
                    keys.push(key);
                }
            }
            if keys.len() >= MAX_SCENE_COMPONENTS {
                break;
            }
        }
    }
    crate::surfaces::http::sse_for_keys(&state, community_id, &keys)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scene_page_has_exactly_one_script_and_a_tight_csp() {
        let csp = crate::scene::build_csp(SCENE_PAGE, &crate::scene::img_origins("", ""))
            .expect("exactly one script");
        assert!(csp.contains("script-src 'sha256-"));
        assert!(csp.contains("connect-src 'self'"));
        let img = csp
            .split(';')
            .find(|d| d.trim().starts_with("img-src"))
            .unwrap();
        assert!(img.contains("'self'"));
        for forbidden in ["http:", "data:", "*"] {
            assert!(!img.contains(forbidden), "{img}");
        }
    }

    #[test]
    fn page_script_has_a_single_markup_sink_and_no_code_execution() {
        let script = crate::scene::inline_script(SCENE_PAGE).unwrap();
        assert_eq!(
            script.matches("innerHTML").count(),
            1,
            "one sanitized-html sink only"
        );
        for banned in [
            "outerHTML",
            "insertAdjacentHTML",
            "document.write",
            "eval(",
            "new Function",
            "srcdoc",
            "createContextualFragment",
            "location.href",
        ] {
            assert!(!script.contains(banned), "page script uses `{banned}`");
        }
    }

    #[test]
    fn id_grammar_rejects_separators_and_oversize() {
        assert!(valid_id("waddles.core.dj"));
        assert!(valid_id("now_playing-2"));
        for bad in [
            "",
            "a/b",
            "a b",
            "a?b",
            "../x",
            "..",
            ".hidden",
            "-x",
            "é",
            &"a".repeat(129),
        ] {
            assert!(!valid_id(bad), "{bad:?}");
        }
    }

    #[test]
    fn html_response_fails_loud_without_a_csp() {
        assert!(html_response(SCENE_PAGE, None, "scene").is_err());
        let r = html_response(SCENE_PAGE, Some("default-src 'none'"), "scene").unwrap();
        assert_eq!(r.status(), StatusCode::OK);
        assert_eq!(r.headers().get(header::CACHE_CONTROL).unwrap(), "no-store");
        assert_eq!(
            r.headers().get(header::X_CONTENT_TYPE_OPTIONS).unwrap(),
            "nosniff"
        );
        assert!(r.headers().contains_key(header::CONTENT_SECURITY_POLICY));
        assert!(html_response(SCENE_PAGE, Some("bad\nvalue"), "scene").is_err());
    }

    #[test]
    fn scene_error_hides_detail() {
        assert!(matches!(
            scene_error(SceneError::CommunityNotFound(1)),
            ApiError::NotFound(_)
        ));
        assert!(matches!(
            scene_error(SceneError::BadCode {
                column: "x",
                code: 1
            }),
            ApiError::Internal(_)
        ));
    }
}
