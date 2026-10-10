//! PUSH credential: the service-to-service credential action-stage
//! adapters (e.g. `twitch_shoutout_action.py`'s `_push_video_overlay`)
//! present on `POST /overlay/{community}/{surface}/push`.
//!
//! Replaces the static `PRESENTATION_PUSH_TOKEN` bearer env var
//! (`core/svc_presentation/blueprints/overlay.py`) -- a long-lived shared
//! secret is exactly the anti-pattern `security.md` Service-to-Service
//! Auth calls out ("Long-lived static API keys/shared secrets"). This
//! module adds **zero new crypto**: it reuses `service_auth`'s existing
//! EdDSA machine-JWT verifier as-is and layers one thing on top --
//! **per-community scoping** -- so a credential minted for community 42
//! can never push to community 91. Scoping is encoded directly in the
//! OIDC `scope` claim (`push_scope()`, `presentation.overlay:push:<id>`)
//! rather than a new custom claim, so `service_auth::verify`'s existing
//! exact-match `required_scope` check enforces it with no changes to that
//! crate at all -- hub-api mints a token scoped to exactly the one
//! community/call an action adapter needs, short-lived (well under the
//! 1h ceiling; single-push use, mirrors `MAX_TOKEN_TTL_SECONDS`).

use axum::extract::{Path, Request, State};
use axum::http::header::AUTHORIZATION;
use axum::middleware::Next;
use axum::response::Response;
use serde::Deserialize;

use crate::error::OverlayAuthError;

/// Scope prefix every PUSH credential's `scope` claim must start with --
/// exported so hub-api's token-issuance side (Python, out of this crate)
/// can mint to the same convention: `f"{OVERLAY_PUSH_SCOPE_PREFIX}{cid}"`.
pub const OVERLAY_PUSH_SCOPE_PREFIX: &str = "presentation.overlay:push:";

/// The exact `scope` claim value a PUSH credential for `community_id`
/// must carry. `service_auth::verify`'s `required_scope` parameter is an
/// exact string match -- this is the one place that convention lives.
pub fn push_scope(community_id: i64) -> String {
    format!("{OVERLAY_PUSH_SCOPE_PREFIX}{community_id}")
}

/// Everything [`require_push_credential`] needs from axum state to verify
/// a PUSH credential -- a future svc-presentation-rust's `AppState`
/// implements this (typically backed by `service_auth::JwksTrustBundle`
/// pointed at hub-api's JWKS endpoint).
pub trait PushTrustSource: Send + Sync {
    fn trust_bundle(&self) -> &dyn service_auth::TrustBundle;
    fn expected_audience(&self) -> &str;
    /// Issuers trusted to mint overlay PUSH credentials -- in practice
    /// just `["hub-api"]`, kept as a list for the same reason
    /// `service_auth::verify` takes a slice (room for a future second
    /// trusted issuer without an API break).
    fn trusted_issuers(&self) -> Vec<&str>;
}

/// Blanket impl so `Arc<ConcreteTrustSource>` is itself a
/// [`PushTrustSource`] -- the idiomatic `Clone + Send + Sync + 'static`
/// shape for axum's `State<S>`, same precedent as
/// [`crate::view::ViewCredentialStore`]'s own `Arc<T>` blanket impl.
impl<T: PushTrustSource + ?Sized> PushTrustSource for std::sync::Arc<T> {
    fn trust_bundle(&self) -> &dyn service_auth::TrustBundle {
        (**self).trust_bundle()
    }
    fn expected_audience(&self) -> &str {
        (**self).expected_audience()
    }
    fn trusted_issuers(&self) -> Vec<&str> {
        (**self).trusted_issuers()
    }
}

/// The successfully-verified PUSH credential, inserted as a request
/// extension by [`require_push_credential`].
#[derive(Debug, Clone)]
pub struct PushCredential {
    pub claims: service_auth::ServiceClaims,
    pub community_id: i64,
}

/// Path params for the PUSH route (`/overlay/{community}/{surface}/push`).
///
/// `surface` is `#[serde(default)]`: a PUSH route may use a *literal*
/// segment in the `{surface}` position instead of a capture (svc-
/// presentation's `POST /overlay/{community}/image/push` upload route and
/// `POST /overlay/{community}/caption/push` ingest route), in which case
/// axum supplies only the `community` path param. Without the default,
/// serde fails the whole extraction with "missing field `surface`" and
/// the guard answers 400 to every request on such a route -- including
/// ones carrying a perfectly valid credential. The guard never reads
/// `surface` (the scope is keyed on `community_id` alone), so an empty
/// value is harmless.
#[derive(Debug, Deserialize)]
pub struct PushPathParams {
    pub community: String,
    #[allow(dead_code)]
    #[serde(default)]
    pub surface: String,
}

/// `axum::middleware::from_fn_with_state` guard for the overlay PUSH
/// route. Extracts `Authorization: Bearer <jwt>`, verifies it against
/// `state`'s trust bundle with `required_scope = push_scope(community_id)`,
/// and on success inserts [`PushCredential`] as a request extension.
///
/// P4/P5 wiring: `Router::new().route("/{community}/{surface}/push",
/// post(push_handler)).layer(from_fn_with_state(state,
/// overlay_auth::require_push_credential))`.
pub async fn require_push_credential<S>(
    State(state): State<S>,
    Path(params): Path<PushPathParams>,
    request: Request,
    next: Next,
) -> Result<Response, OverlayAuthError>
where
    S: PushTrustSource + Clone + Send + Sync + 'static,
{
    let community_id: i64 = params
        .community
        .parse()
        .map_err(|_| OverlayAuthError::InvalidCommunityId)?;

    let token = request
        .headers()
        .get(AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .ok_or(OverlayAuthError::MissingBearer)?;

    let required_scope = push_scope(community_id);
    let claims = service_auth::verify(
        token,
        state.trust_bundle(),
        state.expected_audience(),
        &state.trusted_issuers(),
        &required_scope,
    )
    .await?;

    let (mut parts, body) = request.into_parts();
    parts.extensions.insert(PushCredential {
        claims,
        community_id,
    });
    let request = Request::from_parts(parts, body);
    Ok(next.run(request).await)
}

#[cfg(test)]
mod tests {
    use super::*;
    use jsonwebtoken::{Algorithm, DecodingKey, EncodingKey, Header};
    use std::collections::HashMap;
    use std::sync::Mutex;
    use std::time::{SystemTime, UNIX_EPOCH};

    // Test-only Ed25519 keypair (PKCS8/SPKI-DER), generated once with
    // `openssl genpkey -algorithm ed25519` / `openssl pkey -pubout`,
    // mirroring `core/service_auth/src/lib.rs`'s own test-key convention.
    // Never used outside this test module.
    const TEST_PRIV_DER: &[u8] = &[
        48, 46, 2, 1, 0, 48, 5, 6, 3, 43, 101, 112, 4, 34, 4, 32, 68, 3, 89, 171, 105, 151, 46,
        132, 159, 252, 253, 237, 160, 158, 76, 76, 117, 168, 49, 93, 237, 107, 129, 150, 211, 28,
        65, 232, 226, 2, 111, 69,
    ];
    const TEST_PUB_RAW: &[u8] = &[
        89, 221, 212, 205, 236, 61, 210, 204, 150, 160, 132, 29, 103, 16, 191, 115, 187, 222, 12,
        175, 169, 67, 5, 83, 51, 1, 220, 184, 65, 145, 95, 187,
    ];

    struct StaticTrustBundle(Mutex<HashMap<String, DecodingKey>>);

    #[async_trait::async_trait]
    impl service_auth::TrustBundle for StaticTrustBundle {
        async fn public_key(&self, kid: &str) -> Option<DecodingKey> {
            self.0.lock().unwrap().get(kid).cloned()
        }
    }

    struct FakeTrustSource {
        bundle: StaticTrustBundle,
        audience: String,
    }

    impl PushTrustSource for FakeTrustSource {
        fn trust_bundle(&self) -> &dyn service_auth::TrustBundle {
            &self.bundle
        }
        fn expected_audience(&self) -> &str {
            &self.audience
        }
        fn trusted_issuers(&self) -> Vec<&str> {
            vec!["hub-api"]
        }
    }

    fn now_secs() -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs()
    }

    fn sign(claims: &service_auth::ServiceClaims) -> String {
        let mut header = Header::new(Algorithm::EdDSA);
        header.kid = Some("k1".to_string());
        jsonwebtoken::encode(&header, claims, &EncodingKey::from_ed_der(TEST_PRIV_DER)).unwrap()
    }

    fn trust_source() -> FakeTrustSource {
        FakeTrustSource {
            bundle: StaticTrustBundle(Mutex::new(HashMap::from([(
                "k1".to_string(),
                DecodingKey::from_ed_der(TEST_PUB_RAW),
            )]))),
            audience: "waddlebot-internal".into(),
        }
    }

    fn claims_for(community_id: i64) -> service_auth::ServiceClaims {
        let now = now_secs();
        service_auth::ServiceClaims {
            iss: "hub-api".into(),
            aud: "waddlebot-internal".into(),
            sub: "spiffe://penguintech.io/alpha/svc-action".into(),
            scope: push_scope(community_id),
            iat: now,
            nbf: now,
            exp: now + 300,
            jti: "test-jti".into(),
        }
    }

    #[test]
    fn push_scope_is_community_specific() {
        assert_eq!(push_scope(42), "presentation.overlay:push:42");
        assert_ne!(push_scope(42), push_scope(91));
    }

    #[tokio::test]
    async fn token_scoped_to_the_right_community_verifies() {
        let source = trust_source();
        let token = sign(&claims_for(42));
        let claims = service_auth::verify(
            &token,
            source.trust_bundle(),
            source.expected_audience(),
            &source.trusted_issuers(),
            &push_scope(42),
        )
        .await
        .expect("correctly-scoped token verifies");
        assert_eq!(claims.scope, "presentation.overlay:push:42");
    }

    #[tokio::test]
    async fn token_scoped_to_another_community_is_rejected() {
        let source = trust_source();
        // Minted for community 42, presented against community 91's path.
        let token = sign(&claims_for(42));
        let err = service_auth::verify(
            &token,
            source.trust_bundle(),
            source.expected_audience(),
            &source.trusted_issuers(),
            &push_scope(91),
        )
        .await
        .unwrap_err();
        assert!(matches!(
            err,
            service_auth::ServiceAuthError::InvalidToken(_)
        ));
    }

    // -- `require_push_credential` middleware, end-to-end through a real
    // `Router` (not just the underlying `service_auth::verify` call) --

    fn test_router(source: std::sync::Arc<FakeTrustSource>) -> axum::Router {
        axum::Router::new()
            .route(
                "/overlay/{community}/{surface}/push",
                axum::routing::post(|| async { "pushed" }),
            )
            .layer(axum::middleware::from_fn_with_state(
                source,
                require_push_credential::<std::sync::Arc<FakeTrustSource>>,
            ))
    }

    #[tokio::test]
    async fn middleware_allows_a_correctly_scoped_token() {
        use tower::ServiceExt;

        let source = std::sync::Arc::new(trust_source());
        let token = sign(&claims_for(42));
        let request = axum::http::Request::builder()
            .method("POST")
            .uri("/overlay/42/media/push")
            .header(AUTHORIZATION, format!("Bearer {token}"))
            .body(axum::body::Body::empty())
            .unwrap();
        let response = test_router(source).oneshot(request).await.unwrap();
        assert_eq!(response.status(), axum::http::StatusCode::OK);
    }

    #[tokio::test]
    async fn middleware_rejects_a_token_scoped_to_another_community() {
        use tower::ServiceExt;

        let source = std::sync::Arc::new(trust_source());
        // Minted for community 42, presented against community 91's path.
        let token = sign(&claims_for(42));
        let request = axum::http::Request::builder()
            .method("POST")
            .uri("/overlay/91/media/push")
            .header(AUTHORIZATION, format!("Bearer {token}"))
            .body(axum::body::Body::empty())
            .unwrap();
        let response = test_router(source).oneshot(request).await.unwrap();
        assert_eq!(response.status(), axum::http::StatusCode::FORBIDDEN);
    }

    /// regression: p696-route-shadow -- a PUSH route that spells the
    /// surface as a literal segment (no `{surface}` capture) must still
    /// be guardable: the guard must verify the credential and let the
    /// request through, not 400 on a "missing field `surface`" path
    /// extraction failure.
    #[tokio::test]
    async fn middleware_allows_a_literal_surface_route_with_a_valid_token() {
        use tower::ServiceExt;

        let source = std::sync::Arc::new(trust_source());
        let router = axum::Router::new()
            .route(
                "/overlay/{community}/image/push",
                axum::routing::post(|| async { "uploaded" }),
            )
            .layer(axum::middleware::from_fn_with_state(
                source,
                require_push_credential::<std::sync::Arc<FakeTrustSource>>,
            ));
        let token = sign(&claims_for(42));
        let request = axum::http::Request::builder()
            .method("POST")
            .uri("/overlay/42/image/push")
            .header(AUTHORIZATION, format!("Bearer {token}"))
            .body(axum::body::Body::empty())
            .unwrap();
        let response = router.oneshot(request).await.unwrap();
        assert_eq!(response.status(), axum::http::StatusCode::OK);
    }

    /// The literal-surface route still enforces per-community scoping.
    #[tokio::test]
    async fn middleware_rejects_a_literal_surface_route_token_for_another_community() {
        use tower::ServiceExt;

        let source = std::sync::Arc::new(trust_source());
        let router = axum::Router::new()
            .route(
                "/overlay/{community}/image/push",
                axum::routing::post(|| async { "uploaded" }),
            )
            .layer(axum::middleware::from_fn_with_state(
                source,
                require_push_credential::<std::sync::Arc<FakeTrustSource>>,
            ));
        let token = sign(&claims_for(42));
        let request = axum::http::Request::builder()
            .method("POST")
            .uri("/overlay/91/image/push")
            .header(AUTHORIZATION, format!("Bearer {token}"))
            .body(axum::body::Body::empty())
            .unwrap();
        let response = router.oneshot(request).await.unwrap();
        assert_eq!(response.status(), axum::http::StatusCode::FORBIDDEN);
    }

    #[tokio::test]
    async fn middleware_rejects_a_missing_bearer_header() {
        use tower::ServiceExt;

        let source = std::sync::Arc::new(trust_source());
        let request = axum::http::Request::builder()
            .method("POST")
            .uri("/overlay/42/media/push")
            .body(axum::body::Body::empty())
            .unwrap();
        let response = test_router(source).oneshot(request).await.unwrap();
        assert_eq!(response.status(), axum::http::StatusCode::UNAUTHORIZED);
    }
}
