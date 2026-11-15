//! Composite scene data access and page security policy.
//!
//! The Overlay Designer (hub-api, alembic 0113) writes `overlay_scene_components`,
//! `overlay_widget_registry` and `presentation_config.canvas_resolution_code`; this module is the
//! data plane's READ side: it turns those rows into the layout the scene page renders and answers
//! "may this community's bundle push to this widget?" for the widget routes.
//!
//! Invariants:
//! * Tenant is DERIVED from the `communities` row of the verified community id and applied to every
//!   query -- never read from a request.
//! * Widgets are keyed `(bundle_app_id, widget_id)`; two bundles may share a `widget_id`.
//! * A push is accepted only when the owning bundle is ENABLED for the community and the widget is
//!   registered for it (a registry row) or placed on its scene (a visible component).
//! * The canvas comes from `canvas_resolution_code` (1/2/3) only.
//! * Logs carry ids, counts and codes -- never widget props or HTML.

use async_trait::async_trait;
use base64::Engine as _;
use overlay_schema::{
    CanvasDto, CanvasResolution, SceneComponentDto, SceneLayoutDto, WidgetOutputKind,
    MAX_SCENE_COMPONENTS, STATUS_VISIBLE,
};
use sea_orm::{ColumnTrait, DatabaseConnection, DbErr, EntityTrait, QueryFilter, QueryOrder, QuerySelect};
use sha2::{Digest, Sha256};

use crate::db::entities::{
    app_activation, community, overlay_scene_component, overlay_widget_registry,
    presentation_canvas,
};

/// Image origins widget/scene pages may always load from: the artwork CDNs of the providers the
/// shipped bundles integrate. Extended (never replaced) by `WIDGET_IMG_SRC_ALLOWLIST`.
pub const DEFAULT_IMG_ORIGINS: &[&str] = &[
    "https://i.ytimg.com",
    "https://i.scdn.co",
    "https://static-cdn.jtvnw.net",
    "https://clips-media-assets2.twitch.tv",
];

/// Why a scene lookup failed.
#[derive(Debug, thiserror::Error)]
pub enum SceneError {
    /// The community does not exist (or does not fit the id range).
    #[error("community {0} not found")]
    CommunityNotFound(i64),
    /// The database failed.
    #[error("scene database error: {0}")]
    Db(#[from] DbErr),
    /// A stored code is outside its documented range (a CHECK constraint was bypassed).
    #[error("stored {column} code {code} is out of range")]
    BadCode {
        /// The column.
        column: &'static str,
        /// The offending value.
        code: i16,
    },
}

/// What the data plane needs to know about a registered widget.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WidgetBinding {
    /// What the widget emits.
    pub output_kind: WidgetOutputKind,
    /// The contract surface a `Surface`-kind widget renders; `None` otherwise.
    pub surface_id: Option<String>,
    /// Whether the standalone widget page may be served.
    pub allow_standalone_output: bool,
}

/// Read access to the composite scene.
#[async_trait]
pub trait SceneStore: Send + Sync {
    /// The canvas plus every placed component (hidden ones flagged), back to front.
    async fn layout(&self, community_id: i64) -> Result<SceneLayoutDto, SceneError>;

    /// The widget's binding when its bundle is enabled for the community and the widget is
    /// registered or placed there; `None` otherwise (indistinguishable to the caller).
    async fn find_widget(
        &self,
        community_id: i64,
        bundle_app_id: &str,
        widget_id: &str,
    ) -> Result<Option<WidgetBinding>, SceneError>;
}

/// [`SceneStore`] over the hub-owned tables.
pub struct SeaOrmSceneStore {
    db: DatabaseConnection,
}

impl SeaOrmSceneStore {
    /// Wraps a connection.
    pub fn new(db: DatabaseConnection) -> Self {
        Self { db }
    }

    /// The community's tenant (derived, never supplied).
    async fn tenant_of(&self, community_id: i64) -> Result<(i32, i32), SceneError> {
        let id = i32::try_from(community_id)
            .map_err(|_| SceneError::CommunityNotFound(community_id))?;
        let row = community::Entity::find_by_id(id)
            .one(&self.db)
            .await?
            .ok_or(SceneError::CommunityNotFound(community_id))?;
        Ok((id, row.tenant_id))
    }
}

#[async_trait]
impl SceneStore for SeaOrmSceneStore {
    #[tracing::instrument(name = "scene.layout_query", skip(self))]
    async fn layout(&self, community_id: i64) -> Result<SceneLayoutDto, SceneError> {
        let (id, tenant_id) = self.tenant_of(community_id).await?;

        let canvas = match presentation_canvas::Entity::find()
            .filter(presentation_canvas::Column::CommunityId.eq(id))
            .one(&self.db)
            .await?
        {
            Some(row) => CanvasResolution::from_code(row.canvas_resolution_code)
                .ok_or(SceneError::BadCode {
                    column: "canvas_resolution_code",
                    code: row.canvas_resolution_code,
                })?
                .into(),
            // No stored config row is the documented "not yet configured" state: 1080p.
            None => CanvasDto::default(),
        };

        let rows = overlay_scene_component::Entity::find()
            .filter(overlay_scene_component::Column::CommunityId.eq(community_id))
            .filter(overlay_scene_component::Column::TenantId.eq(i64::from(tenant_id)))
            .order_by_asc(overlay_scene_component::Column::ZIndex)
            .order_by_asc(overlay_scene_component::Column::Id)
            .limit(MAX_SCENE_COMPONENTS as u64 + 1)
            .all(&self.db)
            .await?;
        if rows.len() > MAX_SCENE_COMPONENTS {
            tracing::warn!(
                community_id,
                max = MAX_SCENE_COMPONENTS,
                "scene holds more components than the cap; extras are not rendered"
            );
        }
        let mut components = Vec::with_capacity(rows.len().min(MAX_SCENE_COMPONENTS));
        for r in rows.into_iter().take(MAX_SCENE_COMPONENTS) {
            let output_kind =
                WidgetOutputKind::from_code(r.output_kind_code).ok_or(SceneError::BadCode {
                    column: "output_kind_code",
                    code: r.output_kind_code,
                })?;
            components.push(SceneComponentDto {
                instance_id: r.public_id.to_string(),
                bundle_app_id: r.bundle_app_id,
                widget_id: r.widget_id,
                surface_id: r.surface_id,
                output_kind,
                display_name: r.display_name,
                x: r.x_px,
                y: r.y_px,
                width: r.width_px,
                height: r.height_px,
                z_index: r.z_index,
                visible: r.status_code == STATUS_VISIBLE,
                props: r.props,
            });
        }
        tracing::debug!(community_id, components = components.len(), "scene layout loaded");
        Ok(SceneLayoutDto { canvas, components })
    }

    #[tracing::instrument(name = "scene.find_widget", skip(self))]
    async fn find_widget(
        &self,
        community_id: i64,
        bundle_app_id: &str,
        widget_id: &str,
    ) -> Result<Option<WidgetBinding>, SceneError> {
        let (id, tenant_id) = self.tenant_of(community_id).await?;

        // The bundle must be ENABLED for the community (hub-api's own `enabled == True` check).
        let enabled = app_activation::Entity::find()
            .filter(app_activation::Column::CommunityId.eq(id))
            .filter(app_activation::Column::TenantId.eq(tenant_id))
            .filter(app_activation::Column::AppId.eq(bundle_app_id))
            .filter(app_activation::Column::Enabled.eq(true))
            .one(&self.db)
            .await?;
        if enabled.is_none() {
            tracing::debug!(community_id, "widget lookup refused: bundle not enabled");
            return Ok(None);
        }

        if let Some(reg) = overlay_widget_registry::Entity::find()
            .filter(overlay_widget_registry::Column::CommunityId.eq(community_id))
            .filter(overlay_widget_registry::Column::TenantId.eq(i64::from(tenant_id)))
            .filter(overlay_widget_registry::Column::BundleAppId.eq(bundle_app_id))
            .filter(overlay_widget_registry::Column::WidgetId.eq(widget_id))
            .one(&self.db)
            .await?
        {
            let output_kind =
                WidgetOutputKind::from_code(reg.output_kind_code).ok_or(SceneError::BadCode {
                    column: "output_kind_code",
                    code: reg.output_kind_code,
                })?;
            return Ok(Some(WidgetBinding {
                output_kind,
                surface_id: reg.surface_id,
                allow_standalone_output: reg.allow_standalone_output,
            }));
        }

        // Manifest-declared widgets live in hub-api's catalog; the data plane knows them once a
        // community has placed (visible) one on its scene -- hub-api validated that placement.
        if let Some(comp) = overlay_scene_component::Entity::find()
            .filter(overlay_scene_component::Column::CommunityId.eq(community_id))
            .filter(overlay_scene_component::Column::TenantId.eq(i64::from(tenant_id)))
            .filter(overlay_scene_component::Column::BundleAppId.eq(bundle_app_id))
            .filter(overlay_scene_component::Column::WidgetId.eq(widget_id))
            .filter(overlay_scene_component::Column::StatusCode.eq(STATUS_VISIBLE))
            .one(&self.db)
            .await?
        {
            let output_kind =
                WidgetOutputKind::from_code(comp.output_kind_code).ok_or(SceneError::BadCode {
                    column: "output_kind_code",
                    code: comp.output_kind_code,
                })?;
            let surface_id = (output_kind == WidgetOutputKind::Surface).then_some(comp.surface_id);
            return Ok(Some(WidgetBinding {
                output_kind,
                surface_id,
                allow_standalone_output: true,
            }));
        }
        tracing::debug!(community_id, "widget lookup refused: not registered or placed");
        Ok(None)
    }
}

/// `true` for a CSP-safe `https` origin: scheme, then a hostname of `[a-z0-9.-]`, optional port --
/// no path, wildcard, quote, semicolon, comma or whitespace that could widen or break the header.
pub fn valid_img_origin(origin: &str) -> bool {
    let Some(rest) = origin.strip_prefix("https://") else {
        return false;
    };
    let (host, port) = match rest.split_once(':') {
        Some((h, p)) => (h, Some(p)),
        None => (rest, None),
    };
    let host_ok = !host.is_empty()
        && host.contains('.')
        && !host.starts_with(['.', '-'])
        && !host.ends_with(['.', '-'])
        && !host.contains("..")
        && host
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'.' || b == b'-');
    let port_ok = port.is_none_or(|p| !p.is_empty() && p.len() <= 5 && p.bytes().all(|b| b.is_ascii_digit()));
    host_ok && port_ok
}

/// The image origins pages may load from: the built-in artwork CDNs, the configured extras and
/// the image-bucket origin (signed upload URLs), deduplicated, invalid entries dropped loudly.
pub fn img_origins(extra_csv: &str, bucket_endpoint: &str) -> Vec<String> {
    let mut out: Vec<String> = DEFAULT_IMG_ORIGINS.iter().map(|s| (*s).to_string()).collect();
    let candidates = extra_csv
        .split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .chain(std::iter::once(bucket_endpoint.trim()).filter(|s| !s.is_empty()));
    for origin in candidates {
        let origin = origin.trim_end_matches('/');
        if !valid_img_origin(origin) {
            tracing::warn!(origin, "ignoring an image origin that is not a plain https origin");
            continue;
        }
        if !out.iter().any(|o| o == origin) {
            out.push(origin.to_string());
        }
    }
    out
}

/// Text between the single `<script>` and `</script>` (the CSP-hashed bytes); `None` unless there
/// is exactly one script block.
pub fn inline_script(page: &str) -> Option<&str> {
    if page.matches("<script").count() != 1 || page.matches("</script>").count() != 1 {
        return None;
    }
    let open = page.find("<script>")? + "<script>".len();
    let close = page.find("</script>")?;
    page.get(open..close)
}

/// The Content-Security-Policy for a scene/widget page: no network but this origin, scripts only
/// by hash, and images ONLY from this origin plus the explicit `img_origins` -- never `http:`,
/// `data:` or a bare `https:` wildcard. `None` unless the page has exactly one inline script.
pub fn build_csp(page: &str, img_origins: &[String]) -> Option<String> {
    let script = inline_script(page)?;
    let hash = base64::engine::general_purpose::STANDARD.encode(Sha256::digest(script.as_bytes()));
    let mut img_src = String::from("'self'");
    for origin in img_origins.iter().filter(|o| valid_img_origin(o)) {
        img_src.push(' ');
        img_src.push_str(origin);
    }
    Some(format!(
        "default-src 'none'; script-src 'sha256-{hash}'; style-src 'unsafe-inline'; \
         img-src {img_src}; connect-src 'self'; base-uri 'none'; form-action 'none'"
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use sea_orm::{DatabaseBackend, MockDatabase};

    fn community_row() -> community::Model {
        community::Model {
            id: 7,
            tenant_id: 3,
        }
    }

    fn component(widget: &str, kind: i16, status: i16) -> overlay_scene_component::Model {
        overlay_scene_component::Model {
            id: 1,
            public_id: uuid::Uuid::nil(),
            tenant_id: 3,
            community_id: 7,
            bundle_app_id: "waddles.core.dj".into(),
            widget_id: widget.into(),
            surface_id: "waddles.core.example.music.now_playing".into(),
            output_kind_code: kind,
            display_name: "Now Playing".into(),
            x_px: 10,
            y_px: 20,
            width_px: 300,
            height_px: 100,
            z_index: 5,
            status_code: status,
            props: serde_json::json!({}),
        }
    }

    fn activation() -> app_activation::Model {
        app_activation::Model {
            id: 1,
            community_id: 7,
            tenant_id: 3,
            app_id: "waddles.core.dj".into(),
            enabled: Some(true),
        }
    }

    fn registry(kind: i16, standalone: bool) -> overlay_widget_registry::Model {
        overlay_widget_registry::Model {
            id: 1,
            tenant_id: 3,
            community_id: 7,
            bundle_app_id: "waddles.core.dj".into(),
            widget_id: "w".into(),
            output_kind_code: kind,
            surface_id: Some("waddles.core.example.music.queue".into()),
            allow_standalone_output: standalone,
        }
    }

    fn canvas(code: i16) -> presentation_canvas::Model {
        presentation_canvas::Model {
            id: 1,
            community_id: 7,
            canvas_resolution_code: code,
        }
    }

    #[tokio::test]
    async fn layout_reads_the_canvas_code_and_derives_dimensions() {
        for (code, label, w, h) in [(1, "1080p", 1920, 1080), (2, "2k", 2560, 1440), (3, "4k", 3840, 2160)] {
            let db = MockDatabase::new(DatabaseBackend::Postgres)
                .append_query_results([vec![community_row()]])
                .append_query_results([vec![canvas(code)]])
                .append_query_results([vec![component("w", 4, 1), component("hidden", 1, 2)]])
                .into_connection();
            let layout = SeaOrmSceneStore::new(db).layout(7).await.unwrap();
            assert_eq!(layout.canvas.resolution, label);
            assert_eq!((layout.canvas.width, layout.canvas.height), (w, h));
            assert_eq!(layout.components.len(), 2);
            assert!(layout.components[0].visible);
            assert!(!layout.components[1].visible);
            assert_eq!(layout.components[0].output_kind, WidgetOutputKind::Surface);
        }
    }

    #[tokio::test]
    async fn layout_defaults_to_1080p_without_a_config_row_and_rejects_bad_codes() {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![community_row()]])
            .append_query_results([Vec::<presentation_canvas::Model>::new()])
            .append_query_results([Vec::<overlay_scene_component::Model>::new()])
            .into_connection();
        let layout = SeaOrmSceneStore::new(db).layout(7).await.unwrap();
        assert_eq!(layout.canvas, CanvasDto::default());
        assert!(layout.components.is_empty());

        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![community_row()]])
            .append_query_results([vec![canvas(9)]])
            .into_connection();
        assert!(matches!(
            SeaOrmSceneStore::new(db).layout(7).await,
            Err(SceneError::BadCode { column: "canvas_resolution_code", code: 9 })
        ));

        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![community_row()]])
            .append_query_results([vec![canvas(1)]])
            .append_query_results([vec![component("w", 99, 1)]])
            .into_connection();
        assert!(matches!(
            SeaOrmSceneStore::new(db).layout(7).await,
            Err(SceneError::BadCode { column: "output_kind_code", code: 99 })
        ));
    }

    #[tokio::test]
    async fn layout_caps_components_and_unknown_communities_fail() {
        let many: Vec<_> = (0..=MAX_SCENE_COMPONENTS).map(|_| component("w", 1, 1)).collect();
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![community_row()]])
            .append_query_results([vec![canvas(1)]])
            .append_query_results([many])
            .into_connection();
        let layout = SeaOrmSceneStore::new(db).layout(7).await.unwrap();
        assert_eq!(layout.components.len(), MAX_SCENE_COMPONENTS);

        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([Vec::<community::Model>::new()])
            .into_connection();
        assert!(matches!(
            SeaOrmSceneStore::new(db).layout(7).await,
            Err(SceneError::CommunityNotFound(7))
        ));
        let db = MockDatabase::new(DatabaseBackend::Postgres).into_connection();
        assert!(matches!(
            SeaOrmSceneStore::new(db).layout(i64::from(i32::MAX) + 1).await,
            Err(SceneError::CommunityNotFound(_))
        ));
    }

    #[tokio::test]
    async fn find_widget_requires_an_enabled_bundle() {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![community_row()]])
            .append_query_results([Vec::<app_activation::Model>::new()])
            .into_connection();
        assert_eq!(
            SeaOrmSceneStore::new(db).find_widget(7, "waddles.core.dj", "w").await.unwrap(),
            None
        );
    }

    #[tokio::test]
    async fn find_widget_prefers_the_registry_then_a_visible_placement() {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![community_row()]])
            .append_query_results([vec![activation()]])
            .append_query_results([vec![registry(3, false)]])
            .into_connection();
        let b = SeaOrmSceneStore::new(db)
            .find_widget(7, "waddles.core.dj", "w")
            .await
            .unwrap()
            .unwrap();
        assert_eq!(b.output_kind, WidgetOutputKind::SanitizedHtml);
        assert!(!b.allow_standalone_output);
        assert_eq!(b.surface_id.as_deref(), Some("waddles.core.example.music.queue"));

        // Not in the registry, but visibly placed: a surface widget keeps its surface.
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![community_row()]])
            .append_query_results([vec![activation()]])
            .append_query_results([Vec::<overlay_widget_registry::Model>::new()])
            .append_query_results([vec![component("w", 4, 1)]])
            .into_connection();
        let b = SeaOrmSceneStore::new(db)
            .find_widget(7, "waddles.core.dj", "w")
            .await
            .unwrap()
            .unwrap();
        assert_eq!(b.output_kind, WidgetOutputKind::Surface);
        assert_eq!(b.surface_id.as_deref(), Some("waddles.core.example.music.now_playing"));
        assert!(b.allow_standalone_output);

        // A placed non-surface widget has no surface.
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![community_row()]])
            .append_query_results([vec![activation()]])
            .append_query_results([Vec::<overlay_widget_registry::Model>::new()])
            .append_query_results([vec![component("w", 1, 1)]])
            .into_connection();
        let b = SeaOrmSceneStore::new(db)
            .find_widget(7, "waddles.core.dj", "w")
            .await
            .unwrap()
            .unwrap();
        assert_eq!(b.surface_id, None);
    }

    #[tokio::test]
    async fn find_widget_refuses_the_unregistered_and_unplaced() {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![community_row()]])
            .append_query_results([vec![activation()]])
            .append_query_results([Vec::<overlay_widget_registry::Model>::new()])
            .append_query_results([Vec::<overlay_scene_component::Model>::new()])
            .into_connection();
        assert_eq!(
            SeaOrmSceneStore::new(db).find_widget(7, "waddles.core.dj", "ghost").await.unwrap(),
            None
        );
    }

    #[tokio::test]
    async fn find_widget_surfaces_bad_codes() {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![community_row()]])
            .append_query_results([vec![activation()]])
            .append_query_results([vec![registry(77, true)]])
            .into_connection();
        assert!(matches!(
            SeaOrmSceneStore::new(db).find_widget(7, "waddles.core.dj", "w").await,
            Err(SceneError::BadCode { column: "output_kind_code", code: 77 })
        ));
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![community_row()]])
            .append_query_results([vec![activation()]])
            .append_query_results([Vec::<overlay_widget_registry::Model>::new()])
            .append_query_results([vec![component("w", 77, 1)]])
            .into_connection();
        assert!(matches!(
            SeaOrmSceneStore::new(db).find_widget(7, "waddles.core.dj", "w").await,
            Err(SceneError::BadCode { column: "output_kind_code", code: 77 })
        ));
    }

    #[test]
    fn img_origin_grammar() {
        for ok in ["https://i.ytimg.com", "https://cdn.example.com:8443", "https://a-b.example.org"] {
            assert!(valid_img_origin(ok), "{ok}");
        }
        for bad in [
            "http://i.ytimg.com",
            "https://*.example.com",
            "https://",
            "https://localhost",
            "https://a.example.com/path",
            "https://a.example.com; script-src *",
            "https://a.example.com b.example.com",
            "https://A.example.com",
            "https://.example.com",
            "https://example..com",
            "https://example.com:",
            "https://example.com:99999x",
            "data:",
            "https:",
            "'self'",
        ] {
            assert!(!valid_img_origin(bad), "{bad}");
        }
    }

    #[test]
    fn img_origins_extend_the_defaults_and_drop_garbage() {
        let o = img_origins(
            "https://cdn.example.com, https://i.ytimg.com,http://bad.example.com,https://*.x.com",
            "https://assets.example.net/",
        );
        for d in DEFAULT_IMG_ORIGINS {
            assert!(o.iter().any(|x| x == d), "{d}");
        }
        assert!(o.contains(&"https://cdn.example.com".to_string()));
        assert!(o.contains(&"https://assets.example.net".to_string()));
        assert_eq!(o.iter().filter(|x| *x == "https://i.ytimg.com").count(), 1);
        assert!(!o.iter().any(|x| x.contains("bad") || x.contains('*')));
        assert_eq!(img_origins("", "").len(), DEFAULT_IMG_ORIGINS.len());
    }

    #[test]
    fn csp_is_tight() {
        let page = "<html><script>var a=1;</script></html>";
        let csp = build_csp(page, &img_origins("", "")).unwrap();
        assert!(csp.contains("script-src 'sha256-"));
        assert!(csp.contains("default-src 'none'"));
        assert!(csp.contains("connect-src 'self'"));
        assert!(csp.contains("img-src 'self' https://i.ytimg.com"));
        let img = csp.split(';').find(|d| d.trim().starts_with("img-src")).unwrap();
        for forbidden in ["http:", "data:", " https: ", "*", "blob:"] {
            assert!(!img.contains(forbidden), "{forbidden} in {img}");
        }
        assert!(!csp.contains("unsafe-eval"));
        assert!(!csp.contains("script-src 'unsafe-inline'"));
        // Invalid origins handed in directly never reach the header.
        let csp = build_csp(page, &["https://*.evil.com".to_string(), "https://ok.example.com".to_string()]).unwrap();
        assert!(!csp.contains("evil"));
        assert!(csp.contains("https://ok.example.com"));
        // Not exactly one script -> no CSP (fail loud at the handler).
        assert!(build_csp("<html></html>", &[]).is_none());
        assert!(build_csp("<script>a</script><script>b</script>", &[]).is_none());
    }
}
