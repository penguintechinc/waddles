//! End-to-end proof of the bundle -> presentation surface path and the widget/scene routes.
//!
//! * The stage-side `SurfaceHost` (real client) publishes over genuine gRPC to the real
//!   `SurfaceIngress` server, authenticated with real signed ES256 JWTs verified against a live
//!   JWKS -- the only in-cluster publish transport.
//! * The HTTP side (`http::router`) is driven through the real VIEW/PUSH guards: widget push,
//!   cross-surface bleed, unregistered/disabled widgets, the CSP header, canvas codes, live SSE.

mod common;

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use axum::body::Body;
use axum::http::{header, Request, StatusCode};
use bundle_host_surface::pb::surface_service_client::SurfaceServiceClient;
use bundle_host_surface::pb::PublishRequest;
use bundle_host_surface::{PushTokenSource, SurfaceError, SurfaceHost, SurfacePolicy};
use http_body_util::BodyExt;
use overlay_auth::{generate_view_token, hash_token, ViewCredentialRecord};
use overlay_schema::{
    CanvasDto, CanvasResolution, SceneComponentDto, SceneLayoutDto, Surface, WidgetOutputKind,
};
use serde_json::{json, Value};
use tower::ServiceExt;

use common::*;
use svc_presentation::http::{router, AppState};
use svc_presentation::scene::{SceneError, SceneStore, WidgetBinding};
use svc_presentation::surfaces::hub::Recv;
use svc_presentation::surfaces::widget_key;

const APP: &str = "waddles.core.dj";

// ---------------------------------------------------------------- fakes

/// Scene store answering from fixed tables, keyed by community so isolation is observable.
#[derive(Default)]
struct FakeScene {
    widgets: HashMap<(i64, String, String), WidgetBinding>,
    layouts: HashMap<i64, SceneLayoutDto>,
}

impl FakeScene {
    fn with_widget(mut self, community: i64, app: &str, id: &str, b: WidgetBinding) -> Self {
        self.widgets
            .insert((community, app.to_string(), id.to_string()), b);
        self
    }
}

#[async_trait]
impl SceneStore for FakeScene {
    async fn layout(&self, community_id: i64) -> Result<SceneLayoutDto, SceneError> {
        self.layouts
            .get(&community_id)
            .cloned()
            .ok_or(SceneError::CommunityNotFound(community_id))
    }

    async fn find_widget(
        &self,
        community_id: i64,
        bundle_app_id: &str,
        widget_id: &str,
    ) -> Result<Option<WidgetBinding>, SceneError> {
        Ok(self
            .widgets
            .get(&(community_id, bundle_app_id.to_string(), widget_id.to_string()))
            .cloned())
    }
}

fn binding(kind: WidgetOutputKind, surface: Option<&str>, standalone: bool) -> WidgetBinding {
    WidgetBinding {
        output_kind: kind,
        surface_id: surface.map(str::to_string),
        allow_standalone_output: standalone,
    }
}

fn component(id: &str, widget: &str, kind: WidgetOutputKind, surface: &str) -> SceneComponentDto {
    SceneComponentDto {
        instance_id: id.to_string(),
        bundle_app_id: APP.to_string(),
        widget_id: widget.to_string(),
        surface_id: surface.to_string(),
        output_kind: kind,
        display_name: widget.to_string(),
        x: 0,
        y: 0,
        width: 100,
        height: 100,
        z_index: 1,
        visible: true,
        props: json!({}),
    }
}

/// Token source minting a real signed JWT for exactly the requested community.
struct RealTokens;

#[async_trait]
impl PushTokenSource for RealTokens {
    async fn token_for(&self, community_id: i64) -> Result<Arc<str>, String> {
        Ok(Arc::from(sign_push_token(community_id)))
    }
}

/// Token source that always hands out a token for a FIXED other community.
struct WrongCommunityTokens(i64);

#[async_trait]
impl PushTokenSource for WrongCommunityTokens {
    async fn token_for(&self, _community_id: i64) -> Result<Arc<str>, String> {
        Ok(Arc::from(sign_push_token(self.0)))
    }
}

/// Serves the real gRPC ingress for `state`; returns the loopback endpoint.
async fn serve_grpc(state: AppState) -> String {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        svc_presentation::surfaces::ingress::serve(state, listener, None, std::future::pending())
            .await
            .unwrap();
    });
    format!("http://{addr}")
}

fn policy() -> SurfacePolicy {
    SurfacePolicy::from_permissions([
        "overlay.surface.waddles.core.example.music.now_playing",
        "overlay.surface.waddles.core.example.music.queue",
        "overlay.surface.alert",
        "overlay.surface.clip.alert",
        "overlay.surface.nope",
    ])
}

fn track() -> Value {
    json!({"id":1,"provider":"yt","track_id":"abc","title":"T","artist":"A","duration_s":200,"url":"https://example.com/x"})
}

// ------------------------------------------------------------ gRPC path

#[tokio::test]
async fn a_bundle_publish_travels_grpc_and_is_cached_and_isolated_by_community() {
    let state = state_with_real_push_trust(true).await;
    let endpoint = serve_grpc(state.clone()).await;
    let host = SurfaceHost::new(&endpoint, None, Arc::new(RealTokens)).unwrap();
    let body = json!({"now_playing": track(), "updated_ms": 5}).to_string();
    host.publish(&policy(), 42, "waddles.core.example.music.now_playing", &body)
        .await
        .unwrap();
    let cached = state.surfaces.hub.replay_for(42, "waddles.core.example.music.now_playing").unwrap();
    assert_eq!(cached.payload["now_playing"]["title"], "T");
    assert!(state.surfaces.hub.replay_for(43, "waddles.core.example.music.now_playing").is_none());
}

#[tokio::test]
async fn a_token_scoped_to_another_community_is_denied_and_nothing_is_published() {
    let state = state_with_real_push_trust(true).await;
    let endpoint = serve_grpc(state.clone()).await;
    let host = SurfaceHost::new(&endpoint, None, Arc::new(WrongCommunityTokens(99))).unwrap();
    let body = json!({"now_playing": null, "updated_ms": 1}).to_string();
    let err = host
        .publish(&policy(), 42, "waddles.core.example.music.now_playing", &body)
        .await
        .unwrap_err();
    assert_eq!(err.code(), "denied");
    assert_eq!(state.surfaces.hub.replay_len(), 0);
}

#[tokio::test]
async fn missing_credentials_wrong_version_and_bad_ids_are_refused_before_any_processing() {
    let state = state_with_real_push_trust(true).await;
    let endpoint = serve_grpc(state.clone()).await;
    let mut raw = SurfaceServiceClient::connect(endpoint).await.unwrap();
    let req = |version: &str, community: i64| PublishRequest {
        api_version: version.to_string(),
        community_id: community,
        surface_id: "alert".to_string(),
        payload_json: json!({"alert_type":"info","message":"m"}).to_string(),
    };
    // No bearer at all -> UNAUTHENTICATED.
    let status = raw.publish(req("v1", 42)).await.unwrap_err();
    assert_eq!(status.code(), tonic::Code::Unauthenticated);
    // A garbage bearer -> PERMISSION_DENIED (never says which check failed).
    let mut with_bad = tonic::Request::new(req("v1", 42));
    with_bad
        .metadata_mut()
        .insert("authorization", "Bearer not-a-jwt".parse().unwrap());
    assert_eq!(
        raw.publish(with_bad).await.unwrap_err().code(),
        tonic::Code::PermissionDenied
    );
    // Wrong api_version -> UNIMPLEMENTED; non-positive community -> INVALID_ARGUMENT.
    assert_eq!(
        raw.publish(req("v9", 42)).await.unwrap_err().code(),
        tonic::Code::Unimplemented
    );
    assert_eq!(
        raw.publish(req("v1", 0)).await.unwrap_err().code(),
        tonic::Code::InvalidArgument
    );
    assert_eq!(state.surfaces.hub.replay_len(), 0);
}

#[tokio::test]
async fn contract_refusals_map_to_the_stable_guest_codes_over_grpc() {
    let state = state_with_real_push_trust(true).await;
    let endpoint = serve_grpc(state.clone()).await;
    let host = SurfaceHost::new(&endpoint, None, Arc::new(RealTokens)).unwrap();
    let p = policy();

    // Schema violation -> invalid, naming locations but never values.
    let e = host
        .publish(&p, 42, "clip.alert", &json!({"clip_id":"a b","url":"javascript:alert(1)","title":"t","created_ms":1}).to_string())
        .await
        .unwrap_err();
    assert_eq!(e.code(), "invalid");
    assert!(!e.to_string().contains("javascript"), "{e}");
    // Unknown surface (granted locally, not in the contract) -> invalid.
    assert_eq!(
        host.publish(&p, 42, "nope", "{}").await.unwrap_err().code(),
        "invalid"
    );
    // Over the surface's own cap (4096) but under the host's hard cap -> too_large.
    let big = json!({"now_playing": null, "updated_ms": 1, "pad": "x".repeat(5000)}).to_string();
    assert_eq!(
        host.publish(&p, 42, "waddles.core.example.music.now_playing", &big).await.unwrap_err().code(),
        "too_large"
    );
    // Control characters -> invalid.
    let ctl = json!({"alert_type":"info","message":"a\u{1b}[31m"}).to_string();
    assert_eq!(
        host.publish(&p, 42, "alert", &ctl).await.unwrap_err().code(),
        "invalid"
    );
    assert_eq!(state.surfaces.hub.replay_len(), 0, "nothing invalid may be cached");
}

#[tokio::test]
async fn the_rate_limit_is_enforced_per_community_with_a_retry_delay() {
    let state = state_with_real_push_trust(true).await;
    let endpoint = serve_grpc(state.clone()).await;
    let host = SurfaceHost::new(&endpoint, None, Arc::new(RealTokens)).unwrap();
    let p = policy();
    let body = json!({"alert_type":"info","message":"m"}).to_string();
    let mut limited = None;
    for _ in 0..100 {
        if let Err(e) = host.publish(&p, 42, "alert", &body).await {
            limited = Some(e);
            break;
        }
    }
    match limited.expect("a burst must eventually be limited") {
        SurfaceError::RateLimited { retry_after_ms } => assert!(retry_after_ms >= 1000),
        other => panic!("expected rate limit, got {other:?}"),
    }
    // Another community has its own bucket.
    host.publish(&p, 43, "alert", &body).await.unwrap();
}

#[tokio::test]
async fn an_alert_publish_is_bridged_into_the_alert_box_overlay_hub() {
    let state = state_with_real_push_trust(true).await;
    let mut sub = state.frame_hub.subscribe(42, Surface::AlertBox);
    let endpoint = serve_grpc(state.clone()).await;
    let host = SurfaceHost::new(&endpoint, None, Arc::new(RealTokens)).unwrap();
    host.publish(
        &policy(),
        42,
        "alert",
        &json!({"alert_type":"info","message":"<b>hi</b>"}).to_string(),
    )
    .await
    .unwrap();
    let frame = tokio::time::timeout(Duration::from_secs(2), sub.recv())
        .await
        .expect("bridged frame delivered");
    assert!(frame.is_some());
}

// ------------------------------------------------------- widget push

async fn send(
    app: axum::Router,
    method: &str,
    uri: &str,
    bearer: Option<String>,
    body: Value,
) -> (StatusCode, Value) {
    let mut request = Request::builder()
        .method(method)
        .uri(uri)
        .header(header::CONTENT_TYPE, "application/json");
    if let Some(token) = bearer {
        request = request.header(header::AUTHORIZATION, format!("Bearer {token}"));
    }
    let response = app
        .oneshot(request.body(Body::from(body.to_string())).unwrap())
        .await
        .unwrap();
    let status = response.status();
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    (status, serde_json::from_slice(&bytes).unwrap_or(Value::Null))
}

fn push_uri(widget: &str) -> String {
    format!("/{CODE_42}/widget/{APP}/{widget}/push")
}

async fn widget_state(scene: FakeScene) -> AppState {
    let mut state = state_with_real_push_trust(true).await;
    state.scene = Arc::new(scene);
    state
}

#[tokio::test]
async fn an_unregistered_widget_is_refused_and_publishes_nothing() {
    let state = widget_state(FakeScene::default()).await;
    let (status, _) = send(
        router(state.clone()),
        "POST",
        &push_uri("ghost"),
        Some(sign_push_token(42)),
        json!({"output_kind":"text","text":"boo"}),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(state.surfaces.hub.replay_len(), 0);
    assert_eq!(state.surfaces.hub.channel_count(), 0);
}

#[tokio::test]
async fn a_widget_registered_for_another_community_is_refused() {
    // Registered for community 43 only; community 42's credential must not reach it.
    let scene = FakeScene::default().with_widget(
        43,
        APP,
        "banner",
        binding(WidgetOutputKind::Text, None, true),
    );
    let state = widget_state(scene).await;
    let (status, _) = send(
        router(state.clone()),
        "POST",
        &push_uri("banner"),
        Some(sign_push_token(42)),
        json!({"output_kind":"text","text":"x"}),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(state.surfaces.hub.replay_len(), 0);
}

#[tokio::test]
async fn a_text_push_lands_on_the_widgets_own_channel_only() {
    let scene = FakeScene::default()
        .with_widget(42, APP, "banner", binding(WidgetOutputKind::Text, None, true))
        .with_widget(42, APP, "other", binding(WidgetOutputKind::Text, None, true))
        .with_widget(43, APP, "banner", binding(WidgetOutputKind::Text, None, true));
    let state = widget_state(scene).await;
    let mut own = state.surfaces.hub.subscribe(42, &widget_key(APP, "banner"));
    let mut sibling = state.surfaces.hub.subscribe(42, &widget_key(APP, "other"));
    let mut other_community = state.surfaces.hub.subscribe(43, &widget_key(APP, "banner"));
    let mut shared_surface = state.surfaces.hub.subscribe(42, "alert");

    let (status, body) = send(
        router(state.clone()),
        "POST",
        &push_uri("banner"),
        Some(sign_push_token(42)),
        json!({"output_kind":"text","text":"hi {user:11111111-1111-1111-1111-111111111111}"}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["status"], "published");

    match tokio::time::timeout(Duration::from_secs(2), own.recv()).await {
        Ok(Some(Recv::Frame(f))) => {
            assert_eq!(f.surface, widget_key(APP, "banner"));
            assert_eq!(f.payload["output_kind"], "text");
            assert_eq!(f.payload["text"], "hi Unknown User");
        }
        _ => panic!("the widget's own subscriber must receive the frame"),
    }
    for (name, sub) in [
        ("sibling widget", &mut sibling),
        ("other community", &mut other_community),
        ("shared alert surface", &mut shared_surface),
    ] {
        let got = tokio::time::timeout(Duration::from_millis(80), sub.recv()).await;
        assert!(got.is_err(), "bleed into {name}");
    }
}

#[tokio::test]
async fn a_surface_widget_publishes_to_its_own_declared_surface_not_a_neighbours() {
    let scene = FakeScene::default().with_widget(
        42,
        APP,
        "now_playing",
        binding(WidgetOutputKind::Surface, Some("waddles.core.example.music.now_playing"), true),
    );
    let state = widget_state(scene).await;
    let mut queue_sub = state.surfaces.hub.subscribe(42, "waddles.core.example.music.queue");
    let mut own_sub = state.surfaces.hub.subscribe(42, "waddles.core.example.music.now_playing");

    let (status, body) = send(
        router(state.clone()),
        "POST",
        &push_uri("now_playing"),
        Some(sign_push_token(42)),
        json!({"output_kind":"surface","payload":{"now_playing": track(), "updated_ms": 5}}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert!(matches!(
        tokio::time::timeout(Duration::from_secs(2), own_sub.recv()).await,
        Ok(Some(Recv::Frame(_)))
    ));
    assert!(
        tokio::time::timeout(Duration::from_millis(80), queue_sub.recv())
            .await
            .is_err(),
        "a now_playing widget must not touch waddles.core.example.music.queue"
    );
    // A payload that violates the surface schema is refused with locations only.
    let (status, body) = send(
        router(state.clone()),
        "POST",
        &push_uri("now_playing"),
        Some(sign_push_token(42)),
        json!({"output_kind":"surface","payload":{"now_playing": "javascript:alert(1)", "updated_ms": 5}}),
    )
    .await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    assert!(!body.to_string().contains("javascript"), "{body}");
}

#[tokio::test]
async fn pushes_must_match_the_registered_kind_and_html_is_sanitized() {
    let scene = FakeScene::default()
        .with_widget(42, APP, "t", binding(WidgetOutputKind::Text, None, true))
        .with_widget(42, APP, "h", binding(WidgetOutputKind::SanitizedHtml, None, true))
        .with_widget(42, APP, "i", binding(WidgetOutputKind::Image, None, true))
        .with_widget(42, APP, "s", binding(WidgetOutputKind::Surface, None, true));
    let state = widget_state(scene).await;
    let tok = || Some(sign_push_token(42));
    // HTML into a text widget: refused.
    let (status, _) = send(
        router(state.clone()),
        "POST",
        &push_uri("t"),
        tok(),
        json!({"output_kind":"sanitized_html","html":"<b>x</b>"}),
    )
    .await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    // Unknown kind label: 400.
    let (status, _) = send(
        router(state.clone()),
        "POST",
        &push_uri("t"),
        tok(),
        json!({"output_kind":"banana"}),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    // Surface widget with no declared surface: 422.
    let (status, _) = send(
        router(state.clone()),
        "POST",
        &push_uri("s"),
        tok(),
        json!({"output_kind":"surface","payload":{}}),
    )
    .await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    // Non-https image: refused.
    let (status, _) = send(
        router(state.clone()),
        "POST",
        &push_uri("i"),
        tok(),
        json!({"output_kind":"image","image_url":"http://x.example/a.png"}),
    )
    .await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY);
    // Hostile HTML is sanitized before fan-out.
    let mut sub = state.surfaces.hub.subscribe(42, &widget_key(APP, "h"));
    let (status, _) = send(
        router(state.clone()),
        "POST",
        &push_uri("h"),
        tok(),
        json!({"output_kind":"sanitized_html","html":"<div onclick=\"x()\">ok<script>alert(1)</script><img src=x onerror=alert(1)></div>"}),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    match tokio::time::timeout(Duration::from_secs(2), sub.recv()).await {
        Ok(Some(Recv::Frame(f))) => {
            let html = f.payload["html"].as_str().unwrap();
            assert_eq!(html, "<div>ok<img></div>");
        }
        _ => panic!("frame expected"),
    }
}

#[tokio::test]
async fn widget_push_requires_a_credential_for_the_codes_community() {
    let scene = FakeScene::default().with_widget(
        42,
        APP,
        "banner",
        binding(WidgetOutputKind::Text, None, true),
    );
    let state = widget_state(scene).await;
    let body = json!({"output_kind":"text","text":"x"});
    let (status, _) = send(router(state.clone()), "POST", &push_uri("banner"), None, body.clone()).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
    let (status, _) = send(
        router(state.clone()),
        "POST",
        &push_uri("banner"),
        Some(sign_push_token(99)),
        body,
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN);
    assert_eq!(state.surfaces.hub.replay_len(), 0);
}

#[tokio::test]
async fn the_widget_push_publish_rate_is_limited() {
    let scene = FakeScene::default().with_widget(
        42,
        APP,
        "banner",
        binding(WidgetOutputKind::Text, None, true),
    );
    let state = widget_state(scene).await;
    let mut limited = false;
    for _ in 0..200 {
        let (status, _) = send(
            router(state.clone()),
            "POST",
            &push_uri("banner"),
            Some(sign_push_token(42)),
            json!({"output_kind":"text","text":"x"}),
        )
        .await;
        if status == StatusCode::TOO_MANY_REQUESTS {
            limited = true;
            break;
        }
    }
    assert!(limited);
}

// ------------------------------------------------- pages, CSP, live

async fn get(app: axum::Router, uri: &str) -> axum::response::Response {
    app.oneshot(Request::builder().uri(uri).body(Body::empty()).unwrap())
        .await
        .unwrap()
}

fn viewed_state(scene: FakeScene) -> (AppState, String) {
    let token = generate_view_token();
    let mut state = state_with_view_credential(42, &token, true);
    state.scene = Arc::new(scene);
    (state, token)
}

fn assert_img_src_is_tight(csp: &str) {
    let img = csp
        .split(';')
        .map(str::trim)
        .find(|d| d.starts_with("img-src"))
        .unwrap_or_else(|| panic!("no img-src in {csp}"));
    assert!(img.starts_with("img-src 'self'"), "{img}");
    for forbidden in ["http:", "data:", "blob:", "*"] {
        assert!(!img.contains(forbidden), "{forbidden} in {img}");
    }
    assert!(
        !img.split_whitespace().any(|t| t == "https:"),
        "bare https: wildcard in {img}"
    );
    assert!(csp.contains("default-src 'none'"));
    assert!(csp.contains("connect-src 'self'"));
    assert!(!csp.contains("unsafe-eval"));
}

#[tokio::test]
async fn the_scene_page_carries_a_tight_csp_and_security_headers() {
    let (state, token) = viewed_state(FakeScene::default());
    let r = get(router(state), &format!("/{CODE_42}/scene?key={token}")).await;
    assert_eq!(r.status(), StatusCode::OK);
    let csp = r
        .headers()
        .get(header::CONTENT_SECURITY_POLICY)
        .expect("CSP header")
        .to_str()
        .unwrap()
        .to_string();
    assert_img_src_is_tight(&csp);
    assert!(csp.contains("script-src 'sha256-"));
    assert_eq!(r.headers().get(header::CACHE_CONTROL).unwrap(), "no-store");
    assert_eq!(r.headers().get(header::REFERRER_POLICY).unwrap(), "no-referrer");
    assert_eq!(r.headers().get(header::X_CONTENT_TYPE_OPTIONS).unwrap(), "nosniff");
}

#[tokio::test]
async fn the_widget_page_has_the_same_csp_and_refuses_unknown_or_non_standalone_widgets() {
    let scene = FakeScene::default()
        .with_widget(42, APP, "badge", binding(WidgetOutputKind::SanitizedHtml, None, true))
        .with_widget(42, APP, "hidden", binding(WidgetOutputKind::Text, None, false));
    let (state, token) = viewed_state(scene);
    let ok = get(
        router(state.clone()),
        &format!("/{CODE_42}/widget/{APP}/badge?key={token}"),
    )
    .await;
    assert_eq!(ok.status(), StatusCode::OK);
    assert_img_src_is_tight(
        ok.headers()
            .get(header::CONTENT_SECURITY_POLICY)
            .unwrap()
            .to_str()
            .unwrap(),
    );
}

#[tokio::test]
async fn unknown_and_non_standalone_widget_pages_are_404() {
    for widget in ["ghost", "hidden"] {
        let scene = FakeScene::default().with_widget(
            42,
            APP,
            "hidden",
            binding(WidgetOutputKind::Text, None, false),
        );
        let (state, token) = viewed_state(scene);
        let r = get(
            router(state),
            &format!("/{CODE_42}/widget/{APP}/{widget}?key={token}"),
        )
        .await;
        assert_eq!(r.status(), StatusCode::NOT_FOUND, "{widget}");
    }
}

#[tokio::test]
async fn the_scene_layout_route_serves_the_canvas_for_every_resolution_code() {
    for (res, label, w, h) in [
        (CanvasResolution::Res1080p, "1080p", 1920, 1080),
        (CanvasResolution::Res2k, "2k", 2560, 1440),
        (CanvasResolution::Res4k, "4k", 3840, 2160),
    ] {
        let mut scene = FakeScene::default();
        scene.layouts.insert(
            42,
            SceneLayoutDto {
                canvas: CanvasDto::from(res),
                components: vec![component(
                    "00000000-0000-0000-0000-000000000001",
                    "now_playing",
                    WidgetOutputKind::Surface,
                    "waddles.core.example.music.now_playing",
                )],
            },
        );
        let (state, token) = viewed_state(scene);
        let r = get(router(state), &format!("/{CODE_42}/scene/layout?key={token}")).await;
        assert_eq!(r.status(), StatusCode::OK);
        let v: Value =
            serde_json::from_slice(&r.into_body().collect().await.unwrap().to_bytes()).unwrap();
        assert_eq!(v["canvas"]["resolution"], label);
        assert_eq!(v["canvas"]["width"], w);
        assert_eq!(v["canvas"]["height"], h);
        assert_eq!(v["components"][0]["visible"], true);
        // The numeric key never leaves the service.
        assert!(v["components"][0].get("id").is_none());
    }
}

#[tokio::test]
async fn the_scene_layout_reads_the_stored_canvas_code_from_the_database() {
    use sea_orm::{DatabaseBackend, MockDatabase};
    use svc_presentation::db::entities::{community, overlay_scene_component, presentation_canvas};
    let token = generate_view_token();
    let db = MockDatabase::new(DatabaseBackend::Postgres)
        .append_query_results([vec![overlay_view_row(&token)]])
        .append_query_results([vec![community::Model { id: 42, tenant_id: 3 }]])
        .append_query_results([vec![presentation_canvas::Model {
            id: 1,
            community_id: 42,
            canvas_resolution_code: 3,
        }]])
        .append_query_results([Vec::<overlay_scene_component::Model>::new()])
        .into_connection();
    let mut state = AppState::new(config_with(&[]), prometheus::Registry::new(), db);
    state.overlay_codes = test_overlay_codes();
    let r = get(router(state), &format!("/{CODE_42}/scene/layout?key={token}")).await;
    assert_eq!(r.status(), StatusCode::OK);
    let v: Value =
        serde_json::from_slice(&r.into_body().collect().await.unwrap().to_bytes()).unwrap();
    assert_eq!(v["canvas"]["resolution"], "4k");
    assert_eq!((v["canvas"]["width"].as_i64(), v["canvas"]["height"].as_i64()), (Some(3840), Some(2160)));
}

fn overlay_view_row(token: &str) -> svc_presentation::db::entities::overlay_view_credential::Model {
    svc_presentation::db::entities::overlay_view_credential::Model {
        id: 1,
        community_id: 42,
        key_hash: hash_token(token),
        previous_key_hash: None,
        is_active: true,
        rotated_at: None,
    }
}

/// Reads SSE `data:` payloads from `body` until `want` frames arrive or `deadline` passes.
async fn read_events(body: Body, want: usize, deadline: Duration) -> Vec<Value> {
    let mut body = body;
    let mut text = String::new();
    let _ = tokio::time::timeout(deadline, async {
        loop {
            match body.frame().await {
                Some(Ok(frame)) => {
                    if let Some(d) = frame.data_ref() {
                        text.push_str(&String::from_utf8_lossy(d));
                        if text.matches("data:").count() >= want {
                            break;
                        }
                    }
                }
                _ => break,
            }
        }
    })
    .await;
    text.lines()
        .filter_map(|l| l.strip_prefix("data:"))
        .filter_map(|d| serde_json::from_str(d.trim()).ok())
        .collect()
}

#[tokio::test]
async fn scene_live_replays_and_streams_only_this_scenes_widgets() {
    let mut scene = FakeScene::default();
    scene.layouts.insert(
        42,
        SceneLayoutDto {
            canvas: CanvasDto::default(),
            components: vec![
                component("00000000-0000-0000-0000-000000000001", "banner", WidgetOutputKind::Text, "custom"),
                component("00000000-0000-0000-0000-000000000002", "np", WidgetOutputKind::Surface, "waddles.core.example.music.now_playing"),
            ],
        },
    );
    let (state, token) = viewed_state(scene);
    // Last state replays on connect; an unplaced widget's frame and another community's must not.
    state.surfaces.hub.publish(42, &widget_key(APP, "banner"), true, json!({"output_kind":"text","text":"replayed"}));
    state.surfaces.hub.publish(42, &widget_key(APP, "unplaced"), true, json!({"output_kind":"text","text":"SECRET"}));
    state.surfaces.hub.publish(43, &widget_key(APP, "banner"), true, json!({"output_kind":"text","text":"OTHER"}));
    let r = get(router(state.clone()), &format!("/{CODE_42}/scene/live?key={token}")).await;
    assert_eq!(r.status(), StatusCode::OK);
    let publisher = state.clone();
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(150)).await;
        publisher.surfaces.hub.publish(42, "waddles.core.example.music.now_playing", true, json!({"now_playing":null,"updated_ms":1}));
        publisher.surfaces.hub.publish(42, "waddles.core.example.music.queue", true, json!({"queue":[],"updated_ms":1}));
    });
    let events = read_events(r.into_body(), 3, Duration::from_secs(3)).await;
    let rendered = serde_json::to_string(&events).unwrap();
    assert!(rendered.contains("replayed"), "{rendered}");
    assert!(rendered.contains("waddles.core.example.music.now_playing"), "{rendered}");
    assert!(!rendered.contains("SECRET"), "{rendered}");
    assert!(!rendered.contains("OTHER"), "{rendered}");
    assert!(!rendered.contains("waddles.core.example.music.queue"), "unplaced surface leaked: {rendered}");
}

#[tokio::test]
async fn widget_live_streams_exactly_one_widget_and_validates_the_spec() {
    let scene = FakeScene::default()
        .with_widget(42, APP, "banner", binding(WidgetOutputKind::Text, None, true))
        .with_widget(42, APP, "closed", binding(WidgetOutputKind::Text, None, false));
    let (state, token) = viewed_state(scene);
    state.surfaces.hub.publish(42, &widget_key(APP, "banner"), true, json!({"output_kind":"text","text":"mine"}));
    state.surfaces.hub.publish(42, &widget_key(APP, "closed"), true, json!({"output_kind":"text","text":"NOT-MINE"}));
    let r = get(
        router(state.clone()),
        &format!("/{CODE_42}/scene/live?key={token}&widget={APP}/banner"),
    )
    .await;
    assert_eq!(r.status(), StatusCode::OK);
    let events = read_events(r.into_body(), 2, Duration::from_secs(2)).await;
    let rendered = serde_json::to_string(&events).unwrap();
    assert!(rendered.contains("mine") && !rendered.contains("NOT-MINE"), "{rendered}");

    for (spec, want) in [
        (format!("{APP}/closed"), StatusCode::NOT_FOUND),
        (format!("{APP}/ghost"), StatusCode::NOT_FOUND),
        ("no-slash".to_string(), StatusCode::BAD_REQUEST),
        ("a/b/c".to_string(), StatusCode::BAD_REQUEST),
        ("..%2F..".to_string(), StatusCode::BAD_REQUEST),
    ] {
        let (state, token) = viewed_state(
            FakeScene::default()
                .with_widget(42, APP, "closed", binding(WidgetOutputKind::Text, None, false)),
        );
        let r = get(
            router(state),
            &format!("/{CODE_42}/scene/live?key={token}&widget={spec}"),
        )
        .await;
        assert_eq!(r.status(), want, "{spec}");
    }
}

#[tokio::test]
async fn scene_routes_require_the_view_key() {
    let (state, _token) = viewed_state(FakeScene::default());
    for path in ["scene", "scene/layout", "scene/live"] {
        let r = get(router(state.clone()), &format!("/{CODE_42}/{path}")).await;
        assert_eq!(r.status(), StatusCode::BAD_REQUEST, "{path}: missing key");
    }
}

#[tokio::test]
async fn the_surface_viewer_routes_stay_but_there_is_no_http_publish_route() {
    let (state, token) = viewed_state(FakeScene::default());
    // The legacy REST publish route must not exist: no handler, so the generic router answers.
    let r = router(state.clone())
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/internal/surfaces/42/alert/publish")
                .header(header::AUTHORIZATION, format!("Bearer {}", sign_push_token(42)))
                .body(Body::from(r#"{"alert_type":"info","message":"m"}"#))
                .unwrap(),
        )
        .await
        .unwrap();
    assert!(
        matches!(r.status(), StatusCode::NOT_FOUND | StatusCode::METHOD_NOT_ALLOWED),
        "{}",
        r.status()
    );
    assert_eq!(state.surfaces.hub.replay_len(), 0);
    let page = get(router(state), &format!("/surfaces/{CODE_42}/view?key={token}")).await;
    assert_eq!(page.status(), StatusCode::OK);
}

#[test]
fn record_type_is_used() {
    // Keeps the import honest if the credential record type changes shape.
    let _ = std::mem::size_of::<ViewCredentialRecord>();
}
