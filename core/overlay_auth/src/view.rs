//! VIEW credential: the per-community, rotatable, unguessable key a
//! browser source (OBS) presents on the overlay GET routes
//! (`/overlay/{community}/{surface}`, `/overlay/{community}/{surface}/live`).
//!
//! OBS's browser source is a bare URL -- it cannot set custom headers --
//! so the credential travels as a `?key=` query parameter rather than an
//! `Authorization` header. `community_id` in the path is checked against
//! the credential's own `community_id` (not just "does this hash exist
//! anywhere"), so a leaked/guessed key for community A can never be
//! replayed against community B's path.
//!
//! Storage is hashed-at-rest (see [`crate::token::hash_token`]) and
//! supports one rotation grace window at a time, mirroring the legacy
//! `community_overlay_tokens` scheme's `previous_key`/`rotated_at` columns
//! (`core/browser_source_core_module/services/overlay_service.py`) --
//! rotating a key never hard-cuts an already-open OBS browser-source
//! session.

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;

use axum::extract::{Path, Query, Request, State};
use axum::middleware::Next;
use axum::response::Response;
use chrono::{DateTime, Duration, Utc};
use serde::Deserialize;

use crate::error::OverlayAuthError;

/// Grace period a just-rotated key stays valid for, matching the legacy
/// scheme's `KEY_GRACE_PERIOD_MINUTES` exactly -- rotation is a
/// transparent, non-disruptive operation for an already-connected OBS
/// browser source.
pub const ROTATION_GRACE_PERIOD: Duration = Duration::minutes(5);

/// A stored VIEW credential row (`overlay_view_credentials`,
/// `config/postgres/migrations/100_overlay_view_credentials.sql`). Field
/// names mirror the table's columns.
#[derive(Debug, Clone)]
pub struct ViewCredentialRecord {
    pub community_id: i64,
    pub key_hash: String,
    pub previous_key_hash: Option<String>,
    pub is_active: bool,
    pub rotated_at: Option<DateTime<Utc>>,
}

/// The successfully-validated credential, inserted as a request extension
/// by [`require_view_credential`] -- handlers read it via
/// `Extension<ViewCredential>` instead of re-validating.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ViewCredential {
    pub community_id: i64,
    /// `false` if this request validated against `previous_key_hash`
    /// during the rotation grace window rather than the current key --
    /// surfaced so a caller can log/metric rotation-grace usage, same as
    /// the legacy service's `grace_period: True` response field.
    pub is_current: bool,
}

/// Backing store for VIEW credential lookups. A future svc-presentation-
/// rust implements this (typically a thin SeaORM-backed wrapper around
/// `overlay_view_credentials`); tests use an in-memory fake (see this
/// module's test suite).
///
/// `async_trait`-free on purpose: hand-written `Pin<Box<dyn Future>>`
/// return types keep this trait object-safe (`&dyn ViewCredentialStore`)
/// without pulling `async-trait`'s proc-macro expansion into every
/// implementor, matching how a hot lookup-path trait is more idiomatically
/// written once AFIT isn't blocked by dyn-compatibility needs elsewhere in
/// this crate (contrast with `push.rs`, which reuses `service_auth`'s own
/// `async_trait`-based `TrustBundle` as-is rather than re-wrapping it).
pub trait ViewCredentialStore: Send + Sync {
    /// Look up the credential row for `community_id`, if any.
    fn find_by_community<'a>(
        &'a self,
        community_id: i64,
    ) -> Pin<
        Box<
            dyn Future<Output = Result<Option<ViewCredentialRecord>, OverlayAuthError>> + Send + 'a,
        >,
    >;

    /// Best-effort access-stat bump (`last_accessed`/`access_count` in the
    /// legacy schema's spirit). Failure here must never fail the request
    /// that triggered it -- callers log and continue, same precedent as
    /// `twitch_shoutout_action.py`'s "best-effort, never raises" adapters.
    fn record_access<'a>(
        &'a self,
        community_id: i64,
    ) -> Pin<Box<dyn Future<Output = Result<(), OverlayAuthError>> + Send + 'a>>;
}

/// Blanket impl so `Arc<ConcreteStore>` is itself a [`ViewCredentialStore`]
/// -- the idiomatic way to get a `Clone + Send + Sync + 'static` type for
/// axum's `State<S>` (P4/P5 holds `Arc<ConcreteStore>` in `AppState`,
/// cloning the `Arc`, not the store).
impl<T: ViewCredentialStore + ?Sized> ViewCredentialStore for Arc<T> {
    fn find_by_community<'a>(
        &'a self,
        community_id: i64,
    ) -> Pin<
        Box<
            dyn Future<Output = Result<Option<ViewCredentialRecord>, OverlayAuthError>> + Send + 'a,
        >,
    > {
        (**self).find_by_community(community_id)
    }

    fn record_access<'a>(
        &'a self,
        community_id: i64,
    ) -> Pin<Box<dyn Future<Output = Result<(), OverlayAuthError>> + Send + 'a>> {
        (**self).record_access(community_id)
    }
}

/// Validate a presented VIEW token against `store` for `community_id`.
///
/// Checks, in order: a record exists for `community_id`; it is active;
/// the presented token's hash matches either the current `key_hash`
/// (always valid) or `previous_key_hash` (valid only within
/// [`ROTATION_GRACE_PERIOD`] of `rotated_at`). A hash match against a
/// *different* `community_id`'s row can never happen here because the
/// lookup itself is scoped by `community_id`, not by hash -- this is the
/// structural fix for the legacy scheme, which looked up by hash alone
/// and only incidentally returned the right community.
pub async fn validate_view_token(
    store: &dyn ViewCredentialStore,
    community_id: i64,
    presented_token: &str,
) -> Result<ViewCredential, OverlayAuthError> {
    if presented_token.is_empty() {
        return Err(OverlayAuthError::MissingKey);
    }

    let record = store
        .find_by_community(community_id)
        .await?
        .ok_or(OverlayAuthError::InvalidKey)?;

    if !record.is_active {
        return Err(OverlayAuthError::InactiveCredential);
    }

    let presented_hash = crate::token::hash_token(presented_token);

    if presented_hash == record.key_hash {
        store.record_access(community_id).await.ok();
        return Ok(ViewCredential {
            community_id,
            is_current: true,
        });
    }

    if let (Some(previous_hash), Some(rotated_at)) =
        (record.previous_key_hash.as_deref(), record.rotated_at)
    {
        if presented_hash == previous_hash && Utc::now() <= rotated_at + ROTATION_GRACE_PERIOD {
            store.record_access(community_id).await.ok();
            return Ok(ViewCredential {
                community_id,
                is_current: false,
            });
        }
    }

    Err(OverlayAuthError::InvalidKey)
}

/// Path params shared by every VIEW-gated overlay route
/// (`/overlay/{community}/{surface}`, `.../{surface}/live`) -- axum's
/// `Path<T>` only captures named segments, so this deserializes correctly
/// regardless of trailing literal segments like `/live`.
#[derive(Debug, Deserialize)]
pub struct ViewPathParams {
    pub community: String,
    #[allow(dead_code)]
    pub surface: String,
}

/// OBS's browser source can only vary the URL -- the credential travels
/// as `?key=...`, never a header.
#[derive(Debug, Deserialize)]
pub struct ViewKeyQuery {
    pub key: String,
}

/// `axum::middleware::from_fn_with_state` guard for the overlay GET/`/live`
/// routes. On success, inserts [`ViewCredential`] as a request extension
/// and runs `next`; on failure, short-circuits with the mapped
/// [`OverlayAuthError`] response (400/401/403/500 -- see that type's
/// `IntoResponse`).
///
/// P4/P5 wiring: `Router::new().route("/{community}/{surface}", get(...))
/// .layer(from_fn_with_state(store, overlay_auth::require_view_credential))`
/// where `store: S` is the concrete `ViewCredentialStore` implementation
/// (typically `Arc<ConcreteStore>`, see the blanket impl above) held in
/// axum state.
pub async fn require_view_credential<S>(
    State(store): State<S>,
    Path(params): Path<ViewPathParams>,
    Query(query): Query<ViewKeyQuery>,
    request: Request,
    next: Next,
) -> Result<Response, OverlayAuthError>
where
    S: ViewCredentialStore + Clone + Send + Sync + 'static,
{
    let community_id: i64 = params
        .community
        .parse()
        .map_err(|_| OverlayAuthError::InvalidCommunityId)?;

    let credential = validate_view_token(&store, community_id, &query.key).await?;

    let (mut parts, body) = request.into_parts();
    parts.extensions.insert(credential);
    let request = Request::from_parts(parts, body);
    Ok(next.run(request).await)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::token::{generate_view_token, hash_token};
    use std::collections::HashMap;
    use std::sync::Mutex;

    struct FakeStore {
        rows: Mutex<HashMap<i64, ViewCredentialRecord>>,
    }

    impl FakeStore {
        fn new(rows: Vec<ViewCredentialRecord>) -> Self {
            Self {
                rows: Mutex::new(rows.into_iter().map(|r| (r.community_id, r)).collect()),
            }
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
            let row = self.rows.lock().unwrap().get(&community_id).cloned();
            Box::pin(async move { Ok(row) })
        }

        fn record_access<'a>(
            &'a self,
            _community_id: i64,
        ) -> Pin<Box<dyn Future<Output = Result<(), OverlayAuthError>> + Send + 'a>> {
            Box::pin(async move { Ok(()) })
        }
    }

    fn active_record(community_id: i64, current_token: &str) -> ViewCredentialRecord {
        ViewCredentialRecord {
            community_id,
            key_hash: hash_token(current_token),
            previous_key_hash: None,
            is_active: true,
            rotated_at: None,
        }
    }

    #[tokio::test]
    async fn current_key_validates() {
        let token = generate_view_token();
        let store = FakeStore::new(vec![active_record(42, &token)]);
        let result = validate_view_token(&store, 42, &token).await.unwrap();
        assert_eq!(result.community_id, 42);
        assert!(result.is_current);
    }

    #[tokio::test]
    async fn empty_key_is_missing_key() {
        let store = FakeStore::new(vec![active_record(42, "irrelevant")]);
        let err = validate_view_token(&store, 42, "").await.unwrap_err();
        assert!(matches!(err, OverlayAuthError::MissingKey));
    }

    #[tokio::test]
    async fn unknown_community_is_invalid_key() {
        let store = FakeStore::new(vec![]);
        let err = validate_view_token(&store, 999, "whatever")
            .await
            .unwrap_err();
        assert!(matches!(err, OverlayAuthError::InvalidKey));
    }

    #[tokio::test]
    async fn wrong_key_is_invalid_key() {
        let token = generate_view_token();
        let store = FakeStore::new(vec![active_record(42, &token)]);
        let err = validate_view_token(&store, 42, "not-the-real-token")
            .await
            .unwrap_err();
        assert!(matches!(err, OverlayAuthError::InvalidKey));
    }

    #[tokio::test]
    async fn key_for_a_different_community_never_matches() {
        let token = generate_view_token();
        let store = FakeStore::new(vec![active_record(1, &token), active_record(2, "other")]);
        // Community 1's valid key presented against community 2's path.
        let err = validate_view_token(&store, 2, &token).await.unwrap_err();
        assert!(matches!(err, OverlayAuthError::InvalidKey));
    }

    #[tokio::test]
    async fn inactive_credential_is_rejected() {
        let token = generate_view_token();
        let mut record = active_record(42, &token);
        record.is_active = false;
        let store = FakeStore::new(vec![record]);
        let err = validate_view_token(&store, 42, &token).await.unwrap_err();
        assert!(matches!(err, OverlayAuthError::InactiveCredential));
    }

    #[tokio::test]
    async fn previous_key_validates_within_grace_period() {
        let old_token = generate_view_token();
        let new_token = generate_view_token();
        let mut record = active_record(42, &new_token);
        record.previous_key_hash = Some(hash_token(&old_token));
        record.rotated_at = Some(Utc::now() - Duration::minutes(2));
        let store = FakeStore::new(vec![record]);
        let result = validate_view_token(&store, 42, &old_token).await.unwrap();
        assert!(!result.is_current);
    }

    #[tokio::test]
    async fn previous_key_rejected_after_grace_period_expires() {
        let old_token = generate_view_token();
        let new_token = generate_view_token();
        let mut record = active_record(42, &new_token);
        record.previous_key_hash = Some(hash_token(&old_token));
        record.rotated_at = Some(Utc::now() - Duration::minutes(10));
        let store = FakeStore::new(vec![record]);
        let err = validate_view_token(&store, 42, &old_token)
            .await
            .unwrap_err();
        assert!(matches!(err, OverlayAuthError::InvalidKey));
    }

    // -- `require_view_credential` middleware, end-to-end through a real
    // `Router` (not just the underlying `validate_view_token`) --

    fn test_router(store: Arc<FakeStore>) -> axum::Router {
        axum::Router::new()
            .route(
                "/overlay/{community}/{surface}",
                axum::routing::get(|| async { "ok" }),
            )
            .layer(axum::middleware::from_fn_with_state(
                store,
                require_view_credential::<Arc<FakeStore>>,
            ))
    }

    #[tokio::test]
    async fn middleware_allows_a_request_with_the_current_key() {
        use tower::ServiceExt;

        let token = generate_view_token();
        let store = Arc::new(FakeStore::new(vec![active_record(42, &token)]));
        let request = axum::http::Request::builder()
            .uri(format!("/overlay/42/media?key={token}"))
            .body(axum::body::Body::empty())
            .unwrap();
        let response = test_router(store).oneshot(request).await.unwrap();
        assert_eq!(response.status(), axum::http::StatusCode::OK);
    }

    #[tokio::test]
    async fn middleware_rejects_a_request_with_the_wrong_key() {
        use tower::ServiceExt;

        let store = Arc::new(FakeStore::new(vec![active_record(42, "the-real-token")]));
        let request = axum::http::Request::builder()
            .uri("/overlay/42/media?key=wrong-token")
            .body(axum::body::Body::empty())
            .unwrap();
        let response = test_router(store).oneshot(request).await.unwrap();
        assert_eq!(response.status(), axum::http::StatusCode::FORBIDDEN);
    }

    #[tokio::test]
    async fn middleware_rejects_a_request_missing_the_key_query_param() {
        use tower::ServiceExt;

        let store = Arc::new(FakeStore::new(vec![active_record(42, "irrelevant")]));
        let request = axum::http::Request::builder()
            .uri("/overlay/42/media")
            .body(axum::body::Body::empty())
            .unwrap();
        let response = test_router(store).oneshot(request).await.unwrap();
        // Missing the required `key` query param fails axum's `Query`
        // extractor itself (400), before this crate's own validation runs.
        assert_eq!(response.status(), axum::http::StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn middleware_rejects_a_non_numeric_community_path_segment() {
        use tower::ServiceExt;

        let store = Arc::new(FakeStore::new(vec![]));
        let request = axum::http::Request::builder()
            .uri("/overlay/not-a-number/media?key=anything")
            .body(axum::body::Body::empty())
            .unwrap();
        let response = test_router(store).oneshot(request).await.unwrap();
        assert_eq!(response.status(), axum::http::StatusCode::BAD_REQUEST);
    }
}
