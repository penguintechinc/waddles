//! Helpers P2 (render), P3 (live SSE/websocket), and P4 (push) call once
//! they've added the real overlay routes, so each route is auth-gated
//! from the moment it is added with zero additional wiring.
//!
//! **Deliberately NOT invoked by [`crate::http::router`] yet.** axum has
//! no way to pre-attach a guard to a router with zero routes that doesn't
//! also corrupt the router's own 404 handling:
//! - `Router::layer(...)` wraps the *whole* router's service, including
//!   its fallback -- attached to an empty router, it intercepts every
//!   request that would otherwise 404 and runs the guard's extractors
//!   against it instead (observed directly during this scaffold's own
//!   development: an empty VIEW-guarded router turned `GET
//!   /does-not-exist` into a 400, because the guard's `Path`/`Query`
//!   extractors fail against a request that matched no real route).
//! - `Router::route_layer(...)` applies only to already-registered routes
//!   (never the fallback) -- exactly what's wanted -- but
//!   **panics outright** on a router with zero routes
//!   (`axum::routing::path_router::PathRouter::route_layer`: "Adding a
//!   route_layer before any routes is a no-op. Add the routes you want
//!   the layer to apply to first.").
//!
//! So neither axum primitive can be pre-attached to nothing. Instead, P1
//! ships [`with_view_guard`]/[`with_push_guard`] as functions P2-P4 apply
//! to their *own* already-populated router (using `route_layer`, so a
//! route added anywhere else on the app is never affected) -- real,
//! tested wiring, just applied at the point a real route exists rather
//! than pre-attached to nothing.

use std::sync::Arc;

use axum::Router;
use overlay_auth::{require_push_credential, require_view_credential};

use crate::http::AppState;
use crate::overlay::{AppPushTrustSource, SeaOrmViewCredentialStore};

/// Wraps `router` -- expected to already have the real VIEW-gated route(s)
/// mounted (`GET /overlay/{community}/{surface}` (P2),
/// `.../{surface}/live` (P3)) -- with `overlay_auth::require_view_credential`,
/// scoped only to those routes via `route_layer`.
///
/// EXTENSION POINT (P2/P3): call this from [`crate::http::router`] once
/// the real route(s) exist, e.g.:
/// ```ignore
/// let overlay_view = with_view_guard(
///     Router::new().route("/overlay/{community}/{surface}", get(render::surface)),
///     state.view_store.clone(),
/// );
/// public.merge(overlay_view)
/// ```
pub fn with_view_guard(
    router: Router<AppState>,
    store: Arc<SeaOrmViewCredentialStore>,
) -> Router<AppState> {
    router.route_layer(axum::middleware::from_fn_with_state(
        store,
        require_view_credential::<Arc<SeaOrmViewCredentialStore>>,
    ))
}

/// Same as [`with_view_guard`], but for the PUSH-gated route (P4):
/// `POST /overlay/{community}/{surface}/push`, guarded by
/// `overlay_auth::require_push_credential`.
pub fn with_push_guard(
    router: Router<AppState>,
    source: Arc<AppPushTrustSource>,
) -> Router<AppState> {
    router.route_layer(axum::middleware::from_fn_with_state(
        source,
        require_push_credential::<Arc<AppPushTrustSource>>,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{CliConfig, Config, Secret};
    use crate::db::entities::overlay_view_credential::Model;
    use crate::overlay::view_store::SeaOrmViewCredentialStore as Store;
    use axum::body::Body;
    use axum::http::{Request, StatusCode};
    use axum::routing::{get, post};
    use clap::Parser;
    use overlay_auth::{generate_view_token, hash_token};
    use sea_orm::{DatabaseBackend, MockDatabase};
    use tower::ServiceExt;

    fn dummy_state() -> AppState {
        let cli = CliConfig::parse_from(["svc-presentation"]);
        let config = Config {
            cli,
            db_password: Secret::new("x"),
            cache_password: None,
        };
        let db = MockDatabase::new(DatabaseBackend::Postgres).into_connection();
        AppState::new(config, prometheus::Registry::new(), db)
    }

    fn store_with(rows: Vec<Model>) -> Arc<Store> {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([rows])
            .into_connection();
        Arc::new(Store::new(db))
    }

    /// Full round-trip through a real route wrapped by [`with_view_guard`]
    /// -- the current VIEW key validates and the handler runs.
    #[tokio::test]
    async fn with_view_guard_allows_a_request_bearing_the_current_key() {
        let token = generate_view_token();
        let store = store_with(vec![Model {
            id: 1,
            community_id: 42,
            key_hash: hash_token(&token),
            previous_key_hash: None,
            is_active: true,
            rotated_at: None,
        }]);
        let router = with_view_guard(
            Router::new().route("/overlay/{community}/{surface}", get(|| async { "ok" })),
            store,
        );
        let response = router
            .with_state(dummy_state())
            .oneshot(
                Request::builder()
                    .uri(format!("/overlay/42/media?key={token}"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
    }

    /// The guard rejects a wrong key with 403 -- the handler never runs.
    #[tokio::test]
    async fn with_view_guard_rejects_the_wrong_key() {
        let store = store_with(vec![Model {
            id: 1,
            community_id: 42,
            key_hash: hash_token("the-real-token"),
            previous_key_hash: None,
            is_active: true,
            rotated_at: None,
        }]);
        let router = with_view_guard(
            Router::new().route("/overlay/{community}/{surface}", get(|| async { "ok" })),
            store,
        );
        let response = router
            .with_state(dummy_state())
            .oneshot(
                Request::builder()
                    .uri("/overlay/42/media?key=wrong-token")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::FORBIDDEN);
    }

    /// Mounting the guard never affects a genuinely unrelated path's own
    /// 404 -- the regression this module doc warns about
    /// (`Router::layer` on an empty router would have turned this into a
    /// 400 instead).
    #[tokio::test]
    async fn with_view_guard_never_affects_an_unrelated_unmatched_path() {
        let store = store_with(vec![]);
        let router = with_view_guard(
            Router::new().route("/overlay/{community}/{surface}", get(|| async { "ok" })),
            store,
        );
        let response = router
            .with_state(dummy_state())
            .oneshot(
                Request::builder()
                    .uri("/totally-unrelated-path")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::NOT_FOUND);
    }

    fn push_source_with_unreachable_jwks() -> Arc<AppPushTrustSource> {
        let cli = CliConfig::parse_from([
            "svc-presentation",
            // Port 0 on loopback is never listening -- a connection
            // attempt fails immediately (no DNS lookup, no timeout wait),
            // unlike the production default hostname.
            "--push-jwks-url",
            "http://127.0.0.1:0/.well-known/jwks.json",
        ]);
        let config = Config {
            cli,
            db_password: Secret::new("x"),
            cache_password: None,
        };
        Arc::new(AppPushTrustSource::from_config(&config))
    }

    /// The guard rejects a request with no `Authorization` header before
    /// ever attempting a JWKS fetch -- provable with no network
    /// dependency, unlike a "valid credential" round-trip (which needs a
    /// live/mock JWKS endpoint and is deferred to P4 alongside the real
    /// push route).
    #[tokio::test]
    async fn with_push_guard_rejects_a_missing_bearer_header() {
        let router = with_push_guard(
            Router::new().route(
                "/overlay/{community}/{surface}/push",
                post(|| async { "pushed" }),
            ),
            push_source_with_unreachable_jwks(),
        );
        let response = router
            .with_state(dummy_state())
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/overlay/42/media/push")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn with_push_guard_never_affects_an_unrelated_unmatched_path() {
        let router = with_push_guard(
            Router::new().route(
                "/overlay/{community}/{surface}/push",
                post(|| async { "pushed" }),
            ),
            push_source_with_unreachable_jwks(),
        );
        let response = router
            .with_state(dummy_state())
            .oneshot(
                Request::builder()
                    .uri("/totally-unrelated-path")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::NOT_FOUND);
    }
}
