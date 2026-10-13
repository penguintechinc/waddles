//! Route guards for the overlay URL scheme `/{overlay_code}/{surface}[/...]`.
//!
//! Every guarded route carries the community's unguessable overlay code
//! ([`crate::overlay::code`]) in its first path segment instead of the
//! sequential integer id. A guard therefore does one more thing than
//! `overlay_auth`'s own numeric-path middleware: it resolves the code to the
//! real `community_id` **first**, then applies the unchanged `overlay_auth`
//! checks against that id -- [`overlay_auth::validate_view_token`] for VIEW
//! (`?key=`), [`overlay_auth::authorize_push`] for PUSH (machine JWT scoped to
//! the community). The guards then insert the same
//! `Extension<ViewCredential>` / `Extension<PushCredential>` the handlers
//! already trust, so nothing downstream sees (or keys by) the code.
//!
//! # Order of checks (and why)
//!
//! 1. **Resolve the code.** Malformed (not `[0-9a-f]{16}` -- this is what an
//!    old integer URL like `/42/media` looks like), or well-formed but unknown
//!    -> `404`, no credential is looked at. The route parameter cannot be
//!    regex-constrained in axum, so this check *is* the constraint: anything
//!    that is not a code never reaches a handler. A resolver/database failure
//!    is a `500` (logged, never echoed).
//! 2. **VIEW only: the `?key=` parameter must be present** (`400` if not).
//! 3. **The credential**, scoped to the resolved community. A VIEW key or PUSH
//!    token minted for community A therefore never works on community B's
//!    code (`403`).
//!
//! The code is defense in depth *beside* the credential, not instead of it: an
//! unguessable URL that leaks (referrer, screenshot, chat paste) still needs
//! the VIEW key / PUSH JWT.
//!
//! # Why `route_layer`, applied per already-populated router
//!
//! axum has no way to pre-attach a guard to a router with zero routes without
//! corrupting the router's own 404 handling:
//! - `Router::layer(...)` wraps the *whole* router's service, including its
//!   fallback -- attached to an empty router it intercepts every request that
//!   would otherwise 404 and runs the guard's extractors against it (observed
//!   directly: an empty guarded router turned `GET /does-not-exist` into a
//!   400).
//! - `Router::route_layer(...)` applies only to already-registered routes
//!   (never the fallback) -- exactly what is wanted -- but **panics** on a
//!   router with zero routes.
//!
//! So [`with_view_guard`]/[`with_push_guard`] are applied to a router that
//! already holds its routes, and a route added anywhere else on the app is
//! never affected.

use std::sync::Arc;

use axum::extract::{Path, Query, Request, State};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use axum::Router;
use overlay_auth::view::ViewKeyQuery;
use overlay_auth::{
    authorize_push, validate_view_token, OverlayAuthError, PushTrustSource, ViewCredentialStore,
};
use serde::Deserialize;

use crate::error::ApiError;
use crate::http::AppState;
use crate::overlay::code::{OverlayCodeError, OverlayCodeResolver};

/// Path params every code-keyed guarded route carries. `surface` is
/// `#[serde(default)]`: the caption/image PUSH routes spell the surface as a
/// literal segment (no `{surface}` capture), so axum supplies only
/// `overlay_code` there; the guards never read `surface`.
#[derive(Debug, Deserialize)]
pub struct CodePathParams {
    pub overlay_code: String,
    #[allow(dead_code)]
    #[serde(default)]
    pub surface: String,
}

/// State a code-keyed guard runs with: the code resolver plus the credential
/// backend (`S`: a [`ViewCredentialStore`] for VIEW, a [`PushTrustSource`] for
/// PUSH).
pub struct CodeGuardState<S> {
    codes: Arc<dyn OverlayCodeResolver>,
    auth: S,
}

impl<S: Clone> Clone for CodeGuardState<S> {
    fn clone(&self) -> Self {
        Self {
            codes: self.codes.clone(),
            auth: self.auth.clone(),
        }
    }
}

/// Why a code-keyed guard refused a request.
#[derive(Debug, thiserror::Error)]
pub enum GuardError {
    /// The code is malformed or names no community. Deliberately says nothing
    /// about which, nor echoes the code.
    #[error("not found")]
    NotFound,
    /// The code lookup itself failed (database down) -- an operational fault,
    /// not a rejected request.
    #[error(transparent)]
    Resolve(#[from] OverlayCodeError),
    /// VIEW route without a `?key=` query parameter.
    #[error("missing key query parameter")]
    MissingKey,
    /// The VIEW key / PUSH token was refused.
    #[error(transparent)]
    Auth(#[from] OverlayAuthError),
}

impl IntoResponse for GuardError {
    fn into_response(self) -> Response {
        match self {
            GuardError::NotFound => ApiError::NotFound("not found".to_string()).into_response(),
            // `OverlayAuthError::Store` logs the detail and answers a bare 500.
            GuardError::Resolve(err) => OverlayAuthError::Store(err.to_string()).into_response(),
            GuardError::MissingKey => {
                ApiError::BadRequest("the key query parameter is required".to_string())
                    .into_response()
            }
            GuardError::Auth(err) => err.into_response(),
        }
    }
}

/// Resolves `code` to its community id, or [`GuardError::NotFound`] when it is
/// malformed or unknown.
async fn resolve_community(codes: &dyn OverlayCodeResolver, code: &str) -> Result<i64, GuardError> {
    codes.resolve(code).await?.ok_or(GuardError::NotFound)
}

/// VIEW guard for `/{overlay_code}/{surface}`, `.../live`, `.../live/ws`:
/// resolve the code, require `?key=`, validate it for that community.
async fn require_view_by_code<S>(
    State(state): State<CodeGuardState<S>>,
    Path(params): Path<CodePathParams>,
    request: Request,
    next: Next,
) -> Result<Response, GuardError>
where
    S: ViewCredentialStore + Clone + Send + Sync + 'static,
{
    let community_id = resolve_community(state.codes.as_ref(), &params.overlay_code).await?;
    let Query(ViewKeyQuery { key }) =
        Query::try_from_uri(request.uri()).map_err(|_| GuardError::MissingKey)?;
    let credential = validate_view_token(&state.auth, community_id, &key).await?;
    tracing::debug!(
        community_id,
        current_key = credential.is_current,
        "overlay view credential accepted"
    );

    let (mut parts, body) = request.into_parts();
    parts.extensions.insert(credential);
    Ok(next.run(Request::from_parts(parts, body)).await)
}

/// PUSH guard for `/{overlay_code}/{surface}/push` (and the literal
/// `caption`/`image` variants): resolve the code, verify the bearer JWT is
/// scoped to that community.
async fn require_push_by_code<S>(
    State(state): State<CodeGuardState<S>>,
    Path(params): Path<CodePathParams>,
    request: Request,
    next: Next,
) -> Result<Response, GuardError>
where
    S: PushTrustSource + Clone + Send + Sync + 'static,
{
    let community_id = resolve_community(state.codes.as_ref(), &params.overlay_code).await?;
    let credential = authorize_push(&state.auth, request.headers(), community_id).await?;
    tracing::debug!(community_id, "overlay push credential accepted");

    let (mut parts, body) = request.into_parts();
    parts.extensions.insert(credential);
    Ok(next.run(Request::from_parts(parts, body)).await)
}

/// Wraps `router` -- already holding the VIEW-gated routes
/// (`GET /{overlay_code}/{surface}`, `.../live`, `.../live/ws`) -- with the
/// code-resolving VIEW guard, scoped to those routes via `route_layer`.
pub fn with_view_guard<S>(
    router: Router<AppState>,
    codes: Arc<dyn OverlayCodeResolver>,
    store: S,
) -> Router<AppState>
where
    S: ViewCredentialStore + Clone + Send + Sync + 'static,
{
    router.route_layer(axum::middleware::from_fn_with_state(
        CodeGuardState { codes, auth: store },
        require_view_by_code::<S>,
    ))
}

/// Same as [`with_view_guard`], but for the PUSH-gated routes
/// (`POST /{overlay_code}/{surface}/push`, `.../caption/push`,
/// `.../image/push`).
pub fn with_push_guard<S>(
    router: Router<AppState>,
    codes: Arc<dyn OverlayCodeResolver>,
    source: S,
) -> Router<AppState>
where
    S: PushTrustSource + Clone + Send + Sync + 'static,
{
    router.route_layer(axum::middleware::from_fn_with_state(
        CodeGuardState {
            codes,
            auth: source,
        },
        require_push_by_code::<S>,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{CliConfig, Config, Secret};
    use crate::overlay::code::StaticOverlayCodes;
    use crate::overlay::{AppPushTrustSource, SeaOrmViewCredentialStore};
    use axum::body::{to_bytes, Body};
    use axum::http::{Request as HttpRequest, StatusCode};
    use axum::routing::{get, post};
    use axum::Extension;
    use clap::Parser;
    use overlay_auth::{
        generate_view_token, hash_token, OverlayAuthError, ViewCredential, ViewCredentialRecord,
    };
    use sea_orm::{DatabaseBackend, MockDatabase};
    use std::collections::HashMap;
    use std::future::Future;
    use std::pin::Pin;
    use tower::ServiceExt;

    const CODE_A: &str = "a1b2c3d4e5f60718";
    const CODE_B: &str = "0123456789abcdef";

    fn dummy_state() -> AppState {
        let cli = CliConfig::parse_from(["svc-presentation"]);
        let config = Config {
            cli,
            db_password: Secret::new("x"),
            cache_password: None,
            image_bucket_access_key_id: None,
            image_bucket_secret_access_key: None,
        };
        let db = MockDatabase::new(DatabaseBackend::Postgres).into_connection();
        AppState::new(config, prometheus::Registry::new(), db)
    }

    /// In-memory VIEW store: one credential per community.
    #[derive(Clone)]
    struct FakeStore(Arc<HashMap<i64, ViewCredentialRecord>>);

    impl FakeStore {
        fn with(rows: &[(i64, &str)]) -> Self {
            Self(Arc::new(
                rows.iter()
                    .map(|(community_id, token)| {
                        (
                            *community_id,
                            ViewCredentialRecord {
                                community_id: *community_id,
                                key_hash: hash_token(token),
                                previous_key_hash: None,
                                is_active: true,
                                rotated_at: None,
                            },
                        )
                    })
                    .collect(),
            ))
        }
    }

    impl ViewCredentialStore for FakeStore {
        fn find_by_community<'a>(
            &'a self,
            community_id: i64,
        ) -> Pin<
            Box<
                dyn Future<Output = Result<Option<ViewCredentialRecord>, OverlayAuthError>>
                    + Send
                    + 'a,
            >,
        > {
            let row = self.0.get(&community_id).cloned();
            Box::pin(async move { Ok(row) })
        }

        fn record_access<'a>(
            &'a self,
            _community_id: i64,
        ) -> Pin<Box<dyn Future<Output = Result<(), OverlayAuthError>> + Send + 'a>> {
            Box::pin(async { Ok(()) })
        }
    }

    fn codes() -> Arc<dyn OverlayCodeResolver> {
        Arc::new(StaticOverlayCodes::new([(CODE_A, 42), (CODE_B, 43)]))
    }

    /// A VIEW-guarded `/{overlay_code}/{surface}` whose handler answers the
    /// community id the guard resolved (from `Extension<ViewCredential>`).
    fn view_router(store: FakeStore, resolver: Arc<dyn OverlayCodeResolver>) -> Router {
        with_view_guard(
            Router::new().route(
                "/{overlay_code}/{surface}",
                get(
                    |Extension(credential): Extension<ViewCredential>| async move {
                        credential.community_id.to_string()
                    },
                ),
            ),
            resolver,
            store,
        )
        .with_state(dummy_state())
    }

    async fn get_status(router: Router, uri: &str) -> StatusCode {
        router
            .oneshot(HttpRequest::builder().uri(uri).body(Body::empty()).unwrap())
            .await
            .unwrap()
            .status()
    }

    #[tokio::test]
    async fn the_code_resolves_to_the_correct_community_and_the_handler_sees_its_real_id() {
        let token = generate_view_token();
        let router = view_router(FakeStore::with(&[(42, &token), (43, "other")]), codes());
        let response = router
            .oneshot(
                HttpRequest::builder()
                    .uri(format!("/{CODE_A}/media?key={token}"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let body = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        assert_eq!(&body[..], b"42", "CODE_A must resolve to community 42");
    }

    #[tokio::test]
    async fn each_code_resolves_to_its_own_community() {
        let router = view_router(FakeStore::with(&[(42, "k42"), (43, "k43")]), codes());
        let response = router
            .oneshot(
                HttpRequest::builder()
                    .uri(format!("/{CODE_B}/media?key=k43"))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let body = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        assert_eq!(&body[..], b"43");
    }

    #[tokio::test]
    async fn the_view_key_is_still_enforced_a_wrong_key_is_403() {
        let router = view_router(FakeStore::with(&[(42, "the-real-token")]), codes());
        let status = get_status(router, &format!("/{CODE_A}/media?key=wrong-token")).await;
        assert_eq!(status, StatusCode::FORBIDDEN);
    }

    #[tokio::test]
    async fn a_key_minted_for_another_community_does_not_work_on_this_code() {
        // Community 43's valid key, presented against community 42's code.
        let router = view_router(FakeStore::with(&[(42, "k42"), (43, "k43")]), codes());
        let status = get_status(router, &format!("/{CODE_A}/media?key=k43")).await;
        assert_eq!(status, StatusCode::FORBIDDEN);
    }

    #[tokio::test]
    async fn a_missing_key_is_400_for_a_known_code() {
        let router = view_router(FakeStore::with(&[(42, "k42")]), codes());
        assert_eq!(
            get_status(router.clone(), &format!("/{CODE_A}/media")).await,
            StatusCode::BAD_REQUEST
        );
        // Present but empty is a missing credential (401), as before.
        assert_eq!(
            get_status(router, &format!("/{CODE_A}/media?key=")).await,
            StatusCode::UNAUTHORIZED
        );
    }

    #[tokio::test]
    async fn an_unknown_but_well_formed_code_is_404_even_with_a_valid_looking_key() {
        let router = view_router(FakeStore::with(&[(42, "k42")]), codes());
        let status = get_status(router, "/ffffffffffffffff/media?key=k42").await;
        assert_eq!(status, StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn an_old_integer_community_path_is_404() {
        let router = view_router(FakeStore::with(&[(42, "k42")]), codes());
        for uri in [
            "/42/media?key=k42",
            "/42/media",
            "/0000000000000042/media?key=k42", // 16 digits, but not a real code
            "/A1B2C3D4E5F60718/media?key=k42", // uppercase is not the canonical code
        ] {
            assert_eq!(
                get_status(router.clone(), uri).await,
                StatusCode::NOT_FOUND,
                "{uri}"
            );
        }
    }

    #[tokio::test]
    async fn a_resolver_failure_is_a_500_not_a_404() {
        let router = view_router(
            FakeStore::with(&[(42, "k42")]),
            Arc::new(StaticOverlayCodes::failing()),
        );
        let status = get_status(router, &format!("/{CODE_A}/media?key=k42")).await;
        assert_eq!(status, StatusCode::INTERNAL_SERVER_ERROR);
    }

    #[tokio::test]
    async fn the_not_found_body_never_echoes_the_code() {
        let router = view_router(FakeStore::with(&[]), codes());
        let response = router
            .oneshot(
                HttpRequest::builder()
                    .uri("/ffffffffffffffff/media?key=k")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let body = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        let text = String::from_utf8_lossy(&body);
        assert!(!text.contains("ffffffffffffffff"), "{text}");
    }

    /// Mounting the guard never affects a genuinely unrelated path's own 404
    /// -- the regression the module doc warns about (`Router::layer` on an
    /// empty router would have turned this into a 400 instead).
    #[tokio::test]
    async fn with_view_guard_never_affects_an_unrelated_unmatched_path() {
        let router = view_router(FakeStore::with(&[]), codes());
        assert_eq!(
            get_status(router, "/totally/unrelated/path/here").await,
            StatusCode::NOT_FOUND
        );
    }

    fn push_source_with_unreachable_jwks() -> Arc<AppPushTrustSource> {
        let cli = CliConfig::parse_from([
            "svc-presentation",
            // Port 0 on loopback is never listening -- a connection attempt
            // fails immediately (no DNS lookup, no timeout wait), unlike the
            // production default hostname.
            "--push-jwks-url",
            "http://127.0.0.1:0/.well-known/jwks.json",
        ]);
        let config = Config {
            cli,
            db_password: Secret::new("x"),
            cache_password: None,
            image_bucket_access_key_id: None,
            image_bucket_secret_access_key: None,
        };
        Arc::new(AppPushTrustSource::from_config(&config))
    }

    fn push_router(resolver: Arc<dyn OverlayCodeResolver>) -> Router {
        with_push_guard(
            Router::new().route(
                "/{overlay_code}/{surface}/push",
                post(|| async { "pushed" }),
            ),
            resolver,
            push_source_with_unreachable_jwks(),
        )
        .with_state(dummy_state())
    }

    async fn post_status(router: Router, uri: &str) -> StatusCode {
        router
            .oneshot(
                HttpRequest::builder()
                    .method("POST")
                    .uri(uri)
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap()
            .status()
    }

    /// The guard rejects a request with no `Authorization` header before ever
    /// attempting a JWKS fetch -- provable with no network dependency. (A
    /// valid-credential round trip needs a live JWKS endpoint and lives in
    /// `tests/push_guard.rs` / `tests/overlay_render.rs`.)
    #[tokio::test]
    async fn with_push_guard_rejects_a_missing_bearer_header_for_a_known_code() {
        let status = post_status(push_router(codes()), &format!("/{CODE_A}/media/push")).await;
        assert_eq!(status, StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn with_push_guard_404s_an_unknown_or_integer_path_before_any_auth() {
        for uri in [
            "/ffffffffffffffff/media/push",
            "/42/media/push",
            "/overlay/media/push",
        ] {
            assert_eq!(
                post_status(push_router(codes()), uri).await,
                StatusCode::NOT_FOUND,
                "{uri}"
            );
        }
    }

    #[tokio::test]
    async fn with_push_guard_resolver_failure_is_a_500() {
        let status = post_status(
            push_router(Arc::new(StaticOverlayCodes::failing())),
            &format!("/{CODE_A}/media/push"),
        )
        .await;
        assert_eq!(status, StatusCode::INTERNAL_SERVER_ERROR);
    }

    #[tokio::test]
    async fn with_push_guard_never_affects_an_unrelated_unmatched_path() {
        assert_eq!(
            get_status(push_router(codes()), "/totally-unrelated-path").await,
            StatusCode::NOT_FOUND
        );
    }

    /// The concrete store type production wires (`Arc<SeaOrmViewCredentialStore>`)
    /// satisfies the guard's bounds -- a compile-time pin, with a real
    /// round-trip through the mock database.
    #[tokio::test]
    async fn the_production_store_type_works_with_the_guard() {
        use crate::db::entities::overlay_view_credential::Model;
        let token = generate_view_token();
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![Model {
                id: 1,
                community_id: 42,
                key_hash: hash_token(&token),
                previous_key_hash: None,
                is_active: true,
                rotated_at: None,
            }]])
            .into_connection();
        let router = with_view_guard(
            Router::new().route("/{overlay_code}/{surface}", get(|| async { "ok" })),
            codes(),
            Arc::new(SeaOrmViewCredentialStore::new(db)),
        )
        .with_state(dummy_state());
        let status = get_status(router, &format!("/{CODE_A}/media?key={token}")).await;
        assert_eq!(status, StatusCode::OK);
    }
}
