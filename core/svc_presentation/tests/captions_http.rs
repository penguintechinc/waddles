//! HTTP-level tests for the caption routes (`http::captions`) against the
//! real `http::router` (page + websocket handshake: credential and flag
//! gating, headers, metrics) and against the ingest handler mounted behind a
//! directly-injected `PushCredential` (validation, publish, persist, failure
//! reporting). The real PUSH guard in front of the ingest route is covered
//! by `tests/push_guard.rs`; the live websocket by `tests/captions_ws.rs`.

mod common;

use axum::body::Body;
use axum::http::{header, Request, StatusCode};
use axum::routing::post;
use axum::{Extension, Router};
use http_body_util::BodyExt;
use overlay_auth::generate_view_token;
use overlay_schema::{OverlayPush, Surface};
use tower::ServiceExt;

use common::*;
use svc_presentation::flags::{boxed, StaticFlag};
use svc_presentation::http::captions::push_caption;
use svc_presentation::http::{router, AppState};
use svc_presentation::overlay::hub::RecvOutcome;

async fn body_text(response: axum::response::Response) -> String {
    let bytes = response.into_body().collect().await.unwrap().to_bytes();
    String::from_utf8_lossy(&bytes).into_owned()
}

async fn get(app: Router, uri: &str) -> axum::response::Response {
    app.oneshot(Request::builder().uri(uri).body(Body::empty()).unwrap())
        .await
        .unwrap()
}

fn counter(state: &AppState, series: &str, label: &str) -> u64 {
    let rendered = svc_presentation::telemetry::render_metrics(&state.metrics).unwrap();
    let needle = format!("{series}{{outcome=\"{label}\"}} ");
    rendered
        .lines()
        .find_map(|line| line.strip_prefix(&needle))
        .and_then(|value| value.trim().parse().ok())
        .unwrap_or(0)
}

// -- OBS page: GET /overlay/captions/{key}?community_id=N --

#[tokio::test]
async fn page_with_a_valid_key_serves_the_static_overlay_with_security_headers() {
    let token = generate_view_token();
    let app = router(state_with_view_credential(42, &token, true));
    let response = get(app, &format!("/overlay/captions/{token}?community_id=42")).await;

    assert_eq!(response.status(), StatusCode::OK);
    let headers = response.headers();
    assert_eq!(
        headers.get(header::CONTENT_TYPE).unwrap(),
        "text/html; charset=utf-8"
    );
    assert_eq!(headers.get(header::CACHE_CONTROL).unwrap(), "no-store");
    assert_eq!(headers.get(header::REFERRER_POLICY).unwrap(), "no-referrer");
    assert!(headers.get(header::CONTENT_SECURITY_POLICY).is_some());
    let body = body_text(response).await;
    assert!(body.contains("<title>Caption Overlay</title>"));
    // The page is static: it embeds neither the key nor the community.
    assert!(!body.contains(&token));
}

#[tokio::test]
async fn page_with_the_wrong_key_is_forbidden_and_serves_nothing() {
    let token = generate_view_token();
    let app = router(state_with_view_credential(42, &token, true));
    let response = get(app, "/overlay/captions/not-the-key?community_id=42").await;
    assert_eq!(response.status(), StatusCode::FORBIDDEN);
    assert!(!body_text(response).await.contains("Caption Overlay"));
}

#[tokio::test]
async fn page_key_for_one_community_never_opens_another() {
    let token = generate_view_token();
    // The DB only has a credential row for community 42; asking for 43
    // finds no row at all (the lookup is scoped by community, not by hash).
    let db = sea_orm::MockDatabase::new(sea_orm::DatabaseBackend::Postgres)
        .append_query_results([Vec::<
            svc_presentation::db::entities::overlay_view_credential::Model,
        >::new()])
        .into_connection();
    let mut state = AppState::new(config_with(&[]), prometheus::Registry::new(), db);
    state.captions_flag = boxed(StaticFlag(true));
    let response = get(
        router(state),
        &format!("/overlay/captions/{token}?community_id=43"),
    )
    .await;
    assert_eq!(response.status(), StatusCode::FORBIDDEN);
}

#[tokio::test]
async fn page_without_community_id_is_a_clear_400() {
    let app = router(state_with_no_queries(true));
    let response = get(app, "/overlay/captions/some-key").await;
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    assert!(body_text(response).await.contains("community_id"));
}

#[tokio::test]
async fn page_with_a_non_numeric_community_id_is_a_400() {
    let app = router(state_with_no_queries(true));
    let response = get(app, "/overlay/captions/some-key?community_id=abc").await;
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn page_is_forbidden_when_the_flag_is_off_without_touching_the_database() {
    let app = router(state_with_no_queries(false));
    let response = get(app, "/overlay/captions/some-key?community_id=42").await;
    assert_eq!(response.status(), StatusCode::FORBIDDEN);
    assert!(body_text(response).await.contains("not enabled"));
}

#[tokio::test]
async fn page_route_does_not_leak_the_key_into_metrics_labels() {
    let token = generate_view_token();
    let state = state_with_view_credential(42, &token, true);
    let app = router(state.clone());
    let uri = format!("/overlay/captions/{token}?community_id=42");
    assert_eq!(get(app, &uri).await.status(), StatusCode::OK);

    let rendered = svc_presentation::telemetry::render_metrics(&state.metrics).unwrap();
    assert!(!rendered.contains(&token), "VIEW key leaked into /metrics");
    assert!(
        rendered.contains("path=\"/overlay/captions/{key}\""),
        "expected the route template as the path label, got:\n{rendered}"
    );
}

#[tokio::test]
async fn unmatched_paths_are_labelled_unmatched_not_by_raw_uri() {
    let state = state_with_no_queries(true);
    let app = router(state.clone());
    assert_eq!(
        get(app, "/definitely/not/a/route/secret-looking-segment")
            .await
            .status(),
        StatusCode::NOT_FOUND
    );
    let rendered = svc_presentation::telemetry::render_metrics(&state.metrics).unwrap();
    assert!(!rendered.contains("secret-looking-segment"));
    assert!(rendered.contains("path=\"unmatched\""));
}

// -- websocket handshake: GET /ws/captions/{community_id}?key= (no upgrade) --

#[tokio::test]
async fn ws_handshake_with_the_wrong_key_is_forbidden_before_any_upgrade() {
    let token = generate_view_token();
    let state = state_with_view_credential(42, &token, true);
    let app = router(state.clone());
    let response = get(app, "/ws/captions/42?key=wrong").await;
    assert_eq!(response.status(), StatusCode::FORBIDDEN);
    assert_eq!(
        counter(
            &state,
            "svc_presentation_caption_ws_connections_total",
            "denied"
        ),
        1
    );
}

#[tokio::test]
async fn ws_handshake_without_a_key_is_401() {
    let state = state_with_no_queries(true);
    let app = router(state.clone());
    let response = get(app, "/ws/captions/42").await;
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
    assert_eq!(
        counter(
            &state,
            "svc_presentation_caption_ws_connections_total",
            "denied"
        ),
        1
    );
}

#[tokio::test]
async fn ws_handshake_with_an_empty_key_is_401() {
    let app = router(state_with_no_queries(true));
    let response = get(app, "/ws/captions/42?key=").await;
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn ws_handshake_is_forbidden_when_the_flag_is_off() {
    let state = state_with_no_queries(false);
    let app = router(state.clone());
    let response = get(app, "/ws/captions/42?key=anything").await;
    assert_eq!(response.status(), StatusCode::FORBIDDEN);
    assert_eq!(
        counter(
            &state,
            "svc_presentation_caption_ws_connections_total",
            "disabled"
        ),
        1
    );
}

#[tokio::test]
async fn ws_handshake_with_a_valid_key_but_no_upgrade_is_the_extractors_own_400() {
    let token = generate_view_token();
    let app = router(state_with_view_credential(42, &token, true));
    let response = get(app, &format!("/ws/captions/42?key={token}")).await;
    // Authenticated, but a plain GET is not a websocket upgrade.
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn ws_handshake_with_a_non_numeric_community_is_a_400() {
    let app = router(state_with_no_queries(true));
    let response = get(app, "/ws/captions/not-a-number?key=x").await;
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
}

// -- ingest handler (behind an injected PushCredential) --

fn ingest_router(state: AppState, community_id: i64) -> Router {
    Router::new()
        .route("/{overlay_code}/caption/push", post(push_caption))
        .layer(Extension(push_credential(community_id)))
        .with_state(state)
}

async fn post_json(app: Router, uri: &str, body: &OverlayPush) -> axum::response::Response {
    app.oneshot(
        Request::builder()
            .method("POST")
            .uri(uri)
            .header(header::CONTENT_TYPE, "application/json")
            .body(Body::from(serde_json::to_vec(body).unwrap()))
            .unwrap(),
    )
    .await
    .unwrap()
}

#[tokio::test]
async fn ingest_validates_publishes_and_persists_a_caption() {
    let store = arc_store(FakeCaptionStore::default());
    let mut state = state_with_no_queries(true);
    state.caption_store = store.clone();
    let mut subscription = state.hub.subscribe(42, Surface::Caption);

    let response = post_json(
        ingest_router(state.clone(), 42),
        &format!("/{CODE_42}/caption/push"),
        &caption_push("hola amigos"),
    )
    .await;

    assert_eq!(response.status(), StatusCode::OK);
    let json: serde_json::Value = serde_json::from_str(&body_text(response).await).unwrap();
    assert_eq!(json["status"], "published");
    assert_eq!(json["surface"], "caption");
    assert_eq!(json["overlay_code"], CODE_42);
    assert!(json.get("community").is_none());
    assert_eq!(json["persisted"], true);

    // Broadcast to the community's live viewers...
    match subscription.recv().await {
        Some(RecvOutcome::Push(push)) => {
            assert_eq!(push.caption.as_ref().unwrap().original, "hola amigos");
        }
        _ => panic!("expected the published caption"),
    }
    // ...and persisted, scoped to the credential's community, with only the
    // tokenized author reference.
    let inserted = store.inserted.lock().unwrap();
    assert_eq!(inserted.len(), 1);
    assert_eq!(inserted[0].community_id, 42);
    assert_eq!(inserted[0].user_ref.to_string(), AUTHOR);
    assert_eq!(inserted[0].original, "hola amigos");
    assert_eq!(
        counter(&state, "svc_presentation_caption_ingest_total", "ok"),
        1
    );
}

#[tokio::test]
async fn ingest_rejects_an_invalid_caption_without_publishing_or_persisting() {
    let store = arc_store(FakeCaptionStore::default());
    let mut state = state_with_no_queries(true);
    state.caption_store = store.clone();
    let mut subscription = state.hub.subscribe(42, Surface::Caption);

    // A raw username where the tokenized UUID must be.
    let mut push = caption_push("hola");
    push.caption.as_mut().unwrap().user = "raw_username".to_string();
    let response = post_json(
        ingest_router(state.clone(), 42),
        &format!("/{CODE_42}/caption/push"),
        &push,
    )
    .await;

    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    let body = body_text(response).await;
    assert!(body.contains("tenant-tokenized UUID"), "{body}");
    // The rejection never echoes the offending value back.
    assert!(!body.contains("raw_username"), "{body}");
    assert!(store.inserted.lock().unwrap().is_empty());
    assert_eq!(
        counter(&state, "svc_presentation_caption_ingest_total", "rejected"),
        1
    );

    // Nothing reached the hub: a valid push afterwards is the first frame.
    let ok = post_json(
        ingest_router(state, 42),
        &format!("/{CODE_42}/caption/push"),
        &caption_push("second"),
    )
    .await;
    assert_eq!(ok.status(), StatusCode::OK);
    match subscription.recv().await {
        Some(RecvOutcome::Push(push)) => {
            assert_eq!(push.caption.as_ref().unwrap().original, "second");
        }
        _ => panic!("expected the valid caption as the first frame"),
    }
}

#[tokio::test]
async fn ingest_rejects_a_push_with_no_caption_payload() {
    let state = state_with_no_queries(true);
    let response = post_json(
        ingest_router(state, 42),
        &format!("/{CODE_42}/caption/push"),
        &OverlayPush::default(),
    )
    .await;
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    assert!(body_text(response).await.contains("missing required field"));
}

#[tokio::test]
async fn ingest_rejects_malformed_json_with_a_client_error() {
    let state = state_with_no_queries(true);
    let response = ingest_router(state, 42)
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("/{CODE_42}/caption/push"))
                .header(header::CONTENT_TYPE, "application/json")
                .body(Body::from("{not json"))
                .unwrap(),
        )
        .await
        .unwrap();
    assert!(response.status().is_client_error());
}

#[tokio::test]
async fn ingest_reports_a_persist_failure_but_still_broadcasts() {
    let store = arc_store(FakeCaptionStore {
        fail_insert: true,
        ..Default::default()
    });
    let mut state = state_with_no_queries(true);
    state.caption_store = store;
    let mut subscription = state.hub.subscribe(42, Surface::Caption);

    let response = post_json(
        ingest_router(state.clone(), 42),
        &format!("/{CODE_42}/caption/push"),
        &caption_push("live only"),
    )
    .await;

    // Viewers saw it, so the push itself succeeded -- but the response says
    // plainly that history missed it.
    assert_eq!(response.status(), StatusCode::OK);
    let json: serde_json::Value = serde_json::from_str(&body_text(response).await).unwrap();
    assert_eq!(json["persisted"], false);
    assert!(matches!(
        subscription.recv().await,
        Some(RecvOutcome::Push(_))
    ));
    assert_eq!(
        counter(
            &state,
            "svc_presentation_caption_ingest_total",
            "ok_not_persisted"
        ),
        1
    );
}

#[tokio::test]
async fn ingest_is_forbidden_when_the_flag_is_off() {
    let store = arc_store(FakeCaptionStore::default());
    let mut state = state_with_no_queries(false);
    state.caption_store = store.clone();
    let response = post_json(
        ingest_router(state.clone(), 42),
        &format!("/{CODE_42}/caption/push"),
        &caption_push("hola"),
    )
    .await;
    assert_eq!(response.status(), StatusCode::FORBIDDEN);
    assert!(body_text(response).await.contains("not enabled"));
    assert!(store.inserted.lock().unwrap().is_empty());
    assert_eq!(
        counter(&state, "svc_presentation_caption_ingest_total", "disabled"),
        1
    );
}

#[tokio::test]
async fn ingest_scopes_persistence_to_the_credential_community_only() {
    let store = arc_store(FakeCaptionStore::default());
    let mut state = state_with_no_queries(true);
    state.caption_store = store.clone();
    let mut sub_a = state.hub.subscribe(7, Surface::Caption);
    let mut sub_b = state.hub.subscribe(8, Surface::Caption);

    let response = post_json(
        // `.clone()`: the hub lives in `state`; dropping the last handle
        // would close every subscriber's channel mid-test.
        ingest_router(state.clone(), 7),
        &format!("/{CODE_7}/caption/push"),
        &caption_push("only for seven"),
    )
    .await;
    assert_eq!(response.status(), StatusCode::OK);
    assert!(matches!(sub_a.recv().await, Some(RecvOutcome::Push(_))));
    assert!(
        tokio::time::timeout(std::time::Duration::from_millis(100), sub_b.recv())
            .await
            .is_err()
    );
    assert_eq!(store.inserted.lock().unwrap()[0].community_id, 7);
}

#[tokio::test]
async fn app_state_defaults_to_a_sea_orm_backed_store_and_a_real_flag() {
    // `AppState::new` wires production defaults: the store is exercised via
    // the mock DB (an INSERT that the mock has no result for errors), and the
    // flag resolves through the real licensing client (PenguinTech-owned
    // deployment domain => bypass => on).
    let db = sea_orm::MockDatabase::new(sea_orm::DatabaseBackend::Postgres).into_connection();
    let state = AppState::new(config_with(&[]), prometheus::Registry::new(), db);
    assert!(state.captions_flag.enabled().await);
    let result = state
        .caption_store
        .insert(svc_presentation::overlay::caption_store::NewCaptionEvent {
            community_id: 1,
            user_ref: uuid::Uuid::nil(),
            platform: "twitch".into(),
            original: "x".into(),
            translated: None,
            detected_lang: None,
            target_lang: None,
            confidence: None,
        })
        .await;
    assert!(result.is_err(), "mock DB with no exec result must error");
}
