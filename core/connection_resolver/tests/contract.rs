//! Contract test: the crate parses hub-api's golden resolve-v2 fixtures through the real
//! client path (a local fake hub serves each fixture byte-for-byte).

use std::sync::Arc;
use std::time::Duration;

use connection_resolver::{
    ConnectionKind, ConnectionRef, ConnectionResolver, ConnectionSecret, ResolveError,
    ResolverConfig, ResolverMetrics,
};
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

const OAUTH: &str = include_str!("../../../hub_api/tests/fixtures/resolve_v2_oauth.json");
const HTTP: &str = include_str!("../../../hub_api/tests/fixtures/resolve_v2_http.json");
const LAMBDA: &str = include_str!("../../../hub_api/tests/fixtures/resolve_v2_lambda.json");
const GCP: &str = include_str!("../../../hub_api/tests/fixtures/resolve_v2_gcp.json");

struct NoMetrics;
impl ResolverMetrics for NoMetrics {
    fn resolve(&self, _: ConnectionKind, _: &'static str, _: Duration) {}
    fn list(&self, _: &'static str) {}
}

async fn resolve_fixture(
    body: &str,
) -> Result<connection_resolver::ResolvedConnection, ResolveError> {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/internal/service-token"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_json(serde_json::json!({"token": "t", "expires_in": 3600})),
        )
        .mount(&server)
        .await;
    Mock::given(method("POST"))
        .and(path(
            "/api/v1/internal/communities/7/provider-connections/12/resolve",
        ))
        .respond_with(ResponseTemplate::new(200).set_body_raw(body.to_string(), "application/json"))
        .mount(&server)
        .await;
    let sa = std::env::temp_dir().join(format!("contract-sa-{}", server.address().port()));
    std::fs::write(&sa, "sa").unwrap();
    let cfg = ResolverConfig::new(
        server.uri(),
        "alpha",
        format!("{}/internal/service-token", server.uri()),
        sa.to_string_lossy().to_string(),
    );
    let r = ConnectionResolver::new(cfg, Arc::new(NoMetrics)).unwrap();
    r.resolve(ConnectionRef {
        community_id: 7,
        connection_id: 12,
    })
    .await
}

#[tokio::test]
async fn contract_parses_every_golden_fixture() {
    // OAuth (kind 1): accessToken, expiry parsed to epoch seconds, adapter provider.
    let c = resolve_fixture(OAUTH).await.unwrap();
    assert_eq!((c.community_id, c.connection_id), (7, 12));
    assert_eq!(c.kind, ConnectionKind::PlatformOauth);
    assert_eq!(c.provider, "twitch");
    assert_eq!(c.expires_at, Some(1_791_648_000));
    assert!(c.endpoint.url.is_none() && c.endpoint.region.is_none());
    match &c.secret {
        ConnectionSecret::OAuthAccessToken { access_token } => {
            assert_eq!(access_token.expose(), "FAKE-ACCESS-TOKEN");
        }
        other => panic!("{other:?}"),
    }

    // External HTTP (kind 2, scheme 1 bearer).
    let c = resolve_fixture(HTTP).await.unwrap();
    assert_eq!(c.kind, ConnectionKind::ExternalHttp);
    assert_eq!(
        c.endpoint.url.as_deref(),
        Some("https://hooks.example.com/waddles")
    );
    assert!(c.expires_at.is_none());
    match &c.secret {
        ConnectionSecret::Bearer { token } => {
            assert_eq!(token.expose(), "STATIC-SECRET-bearer-0123456789")
        }
        other => panic!("{other:?}"),
    }

    // AWS Lambda (kind 3, scheme 4); null sessionToken -> None.
    let c = resolve_fixture(LAMBDA).await.unwrap();
    assert_eq!(c.kind, ConnectionKind::AwsLambda);
    assert_eq!(c.endpoint.region.as_deref(), Some("us-east-1"));
    assert_eq!(c.endpoint.function.as_deref(), Some("my-function"));
    match &c.secret {
        ConnectionSecret::AwsAccessKey {
            access_key_id,
            secret_access_key,
            session_token,
        } => {
            assert_eq!(access_key_id.expose(), "FAKE-ACCESS-KEY-ID");
            assert_eq!(secret_access_key.expose(), "FAKE-SECRET-ACCESS-KEY");
            assert!(session_token.is_none());
        }
        other => panic!("{other:?}"),
    }

    // GCP function (kind 4, scheme 5).
    let c = resolve_fixture(GCP).await.unwrap();
    assert_eq!(c.kind, ConnectionKind::GcpFunction);
    assert_eq!(c.endpoint.project.as_deref(), Some("fake-project-1"));
    assert_eq!(c.endpoint.region.as_deref(), Some("us-central1"));
    assert_eq!(c.endpoint.function.as_deref(), Some("my-func"));
    match &c.secret {
        ConnectionSecret::GcpServiceAccount {
            client_email,
            private_key_pem,
            token_uri,
        } => {
            assert_eq!(client_email, "svc@fake-project.example");
            assert_eq!(private_key_pem.expose(), "FAKE-PRIVATE-KEY");
            assert!(token_uri.is_none());
        }
        other => panic!("{other:?}"),
    }
}

#[tokio::test]
async fn golden_fixtures_never_leak_secrets_through_debug() {
    for body in [OAUTH, HTTP, LAMBDA, GCP] {
        let c = resolve_fixture(body).await.unwrap();
        let out = format!("{c:?}");
        for needle in ["FAKE-", "STATIC-SECRET"] {
            assert!(!out.contains(needle), "{out}");
        }
    }
}
