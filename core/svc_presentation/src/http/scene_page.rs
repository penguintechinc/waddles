//! The composite full-screen overlay scene page: `GET /{overlay_code}/scene?key=...`.
//!
//! Serves the full-screen virtual canvas (1080p, 2K, 4K) and dynamic components placed
//! by presentation app bundles.

use std::sync::LazyLock;

use axum::extract::State;
use axum::http::{header, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::{Extension, Json};
use base64::Engine as _;
use overlay_auth::ViewCredential;
use overlay_schema::{
    CanvasResolution, CanvasThemeDto, ComponentStatusCode, SceneComponentDto, SceneLayoutDto,
    WidgetOutputKind,
};
use sea_orm::{ColumnTrait, EntityTrait, QueryFilter, QueryOrder};
use sha2::{Digest, Sha256};

use crate::db::entities::{overlay_scene_component, presentation_config};
use crate::error::ApiError;
use crate::http::AppState;

const SCENE_PAGE: &str = include_str!("templates/scene.html");

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

static SCENE_CSP: LazyLock<Option<String>> = LazyLock::new(|| build_csp(SCENE_PAGE));

/// `GET /{overlay_code}/scene` -- serves the composite scene HTML document.
#[tracing::instrument(name = "scene.page", skip_all)]
pub async fn page(
    Extension(_credential): Extension<ViewCredential>,
) -> Result<Response, ApiError> {
    let csp = SCENE_CSP
        .as_deref()
        .ok_or_else(|| ApiError::Internal(anyhow::anyhow!("malformed scene template: CSP generation failed")))?;

    let mut response = (StatusCode::OK, SCENE_PAGE).into_response();
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

/// `GET /{overlay_code}/scene/layout` -- returns the scene's canvas theme and active components.
#[tracing::instrument(name = "scene.layout", skip_all)]
pub async fn layout(
    State(state): State<AppState>,
    Extension(credential): Extension<ViewCredential>,
) -> Result<Json<SceneLayoutDto>, ApiError> {
    let community_id = credential.community_id;

    // Load components
    let rows = overlay_scene_component::Entity::find()
        .filter(overlay_scene_component::Column::CommunityId.eq(community_id))
        .order_by_asc(overlay_scene_component::Column::ZIndex)
        .order_by_asc(overlay_scene_component::Column::Id)
        .all(&state.db)
        .await
        .map_err(|e| ApiError::Internal(anyhow::anyhow!("database error fetching components: {e}")))?;

    let components: Vec<SceneComponentDto> = rows
        .into_iter()
        .map(|r| SceneComponentDto {
            instance_id: r.public_id.to_string(),
            bundle_app_id: r.bundle_app_id,
            widget_id: r.widget_id,
            surface_id: r.surface_id,
            output_kind: WidgetOutputKind::from_code(r.output_kind_code)
                .unwrap_or(WidgetOutputKind::Text),
            display_name: r.display_name,
            x: r.x_px,
            y: r.y_px,
            width: r.width_px,
            height: r.height_px,
            z_index: r.z_index,
            status: ComponentStatusCode::from_code(r.status_code)
                .unwrap_or(ComponentStatusCode::Active),
            props: r.props,
        })
        .collect();

    // Load theme / resolution from presentation_config if present
    let mut canvas = CanvasThemeDto::default();
    if let Ok(Some(cfg)) = presentation_config::Entity::find()
        .filter(presentation_config::Column::CommunityId.eq(community_id as i32))
        .one(&state.db)
        .await
    {
        canvas.theme = cfg.theme;
        if let Some(p) = cfg.primary_color {
            canvas.primary_color = p;
        }
        if let Some(s) = cfg.secondary_color {
            canvas.secondary_color = s;
        }
        if let Some(f) = cfg.font_family {
            canvas.font_family = f;
        }

        // Prefer dedicated canvas_resolution / canvas_width / canvas_height columns
        // (migration 105), falling back to JSON config["resolution"] if needed.
        if let Some(res) = CanvasResolution::from_str_name(&cfg.canvas_resolution) {
            canvas.resolution = res.as_str().to_string();
            let (def_w, def_h) = res.dimensions();
            canvas.width = if cfg.canvas_width > 0 { cfg.canvas_width } else { def_w };
            canvas.height = if cfg.canvas_height > 0 { cfg.canvas_height } else { def_h };
        } else if let Some(res_val) = cfg.config.get("resolution").and_then(|v| v.as_str()) {
            if let Some(res) = CanvasResolution::from_str_name(res_val) {
                canvas.resolution = res.as_str().to_string();
                let (w, h) = res.dimensions();
                canvas.width = w;
                canvas.height = h;
            }
        }
    }

    Ok(Json(SceneLayoutDto { canvas, components }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scene_page_has_exactly_one_script_and_valid_csp() {
        let csp = build_csp(SCENE_PAGE).expect("CSP generation failed");
        assert!(csp.contains("script-src 'sha256-"));
        assert!(csp.contains("connect-src 'self'"));
    }
}
