//! Credential resolution for generic outbound transports. OAuth access tokens
//! are accepted here, separately from webhook verification keys: accepting an
//! OAuth token as an HMAC signing key would confuse two different credentials.

use std::sync::Arc;

use connection_resolver::{
    ConnectionRef, ConnectionResolve, ConnectionSecret, ResolveError, SecretString,
};

use super::transport::TransportError;
use super::webhook::{BoxFut, SecretError};

/// Authentication credentials for outbound transports, distinct from inbound
/// verification keys so an OAuth source cannot be wired into webhook ingress.
pub trait TransportSecretSource: Send + Sync {
    /// Resolve the host-approved connection immediately before sending.
    fn resolve(&self, requested: ConnectionRef) -> BoxFut<'_, Result<SecretString, SecretError>>;
}

/// A fresh, community-bound connection resolve for every transport use.
/// This adapter retains only the resolver, never a resolved credential.
///
/// Transport authentication sources cannot be used for webhook verification:
/// ```compile_fail,E0277
/// use connection_resolver::ConnectionResolver;
/// use svc_ingest::connector::secrets::TransportSecrets;
/// use svc_ingest::connector::webhook::WebhookSecretSource;
/// fn verification_keys(_: impl WebhookSecretSource) {}
/// fn cannot_mix(source: TransportSecrets<ConnectionResolver>) {
///     verification_keys(source);
/// }
/// ```
pub struct TransportSecrets<R: ConnectionResolve> {
    resolver: Arc<R>,
    community_id: i64,
}

impl<R: ConnectionResolve> TransportSecrets<R> {
    /// Binds a source to its host-verified community before it can resolve
    /// credentials. Guest-supplied handles cannot change this scope.
    pub fn new(resolver: Arc<R>, community_id: i64) -> Result<Self, TransportError> {
        if community_id <= 0 {
            return Err(TransportError::Connect(
                "invalid credential community".into(),
            ));
        }
        Ok(Self {
            resolver,
            community_id,
        })
    }
}

impl<R: ConnectionResolve + 'static> TransportSecretSource for TransportSecrets<R> {
    /// Select one supported authentication secret after checking the resolved
    /// connection identity; multi-field authentication must use its own adapter.
    fn resolve(&self, requested: ConnectionRef) -> BoxFut<'_, Result<SecretString, SecretError>> {
        Box::pin(async move {
            if requested.community_id != self.community_id || requested.connection_id <= 0 {
                return Err(SecretError::Missing);
            }
            let resolved = self
                .resolver
                .resolve(requested)
                .await
                .map_err(|error| match error {
                    ResolveError::Transient(_) | ResolveError::CircuitOpen => {
                        SecretError::Unavailable
                    }
                    _ => SecretError::Missing,
                })?;
            if resolved.community_id != requested.community_id
                || resolved.connection_id != requested.connection_id
            {
                return Err(SecretError::Missing);
            }
            let secret = match resolved.secret {
                ConnectionSecret::OAuthAccessToken { access_token } => access_token,
                ConnectionSecret::Bearer { token } => token,
                ConnectionSecret::ApiKeyHeader { value, .. } => value,
                _ => return Err(SecretError::Missing),
            };
            if secret.expose().is_empty() {
                return Err(SecretError::Missing);
            }
            Ok(secret)
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    use std::path::PathBuf;
    use std::time::Duration;

    use connection_resolver::{
        ConnectionKind, ConnectionResolver, ResolverConfig, ResolverMetrics,
    };
    use wiremock::matchers::{body_partial_json, header, method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    use crate::connector::poll::{PollAuth, PollConfig, PollTransport};
    use crate::connector::transport::{FrameOutcome, Transport};
    use crate::connector::transport_common::substitute_slots;
    use crate::connector::webhook::{ConnectionResolverSecrets, WebhookSecretSource};

    const CONNECTION: ConnectionRef = ConnectionRef {
        community_id: 7,
        connection_id: 12,
    };

    /// Resolver clients read ambient `HTTP(S)_PROXY` at build time; poll's proxy
    /// test mutates it process-wide, so every resolver test holds the read side.
    async fn env_read() -> tokio::sync::RwLockReadGuard<'static, ()> {
        crate::connector::transport_common::PROXY_ENV.read().await
    }

    /// Resolver telemetry is outside this authentication contract test.
    struct NoMetrics;
    impl ResolverMetrics for NoMetrics {
        fn resolve(&self, _: ConnectionKind, _: &'static str, _: Duration) {}
        fn list(&self, _: &'static str) {}
    }

    /// Removes the synthetic projected service-account token on every exit path.
    struct TestSa(PathBuf);
    impl Drop for TestSa {
        fn drop(&mut self) {
            if let Err(error) = std::fs::remove_file(&self.0) {
                eprintln!("synthetic service-account test file cleanup failed: {error}");
            }
        }
    }

    /// Starts the real resolver against a loopback hub HTTP contract server.
    /// The synthetic projected token lives until its test guard is dropped.
    async fn resolver(server: &MockServer) -> (ConnectionResolver, TestSa) {
        Mock::given(method("POST"))
            .and(path("/internal/service-token"))
            .and(body_partial_json(serde_json::json!({"community_id": 7})))
            .respond_with(ResponseTemplate::new(200).set_body_json(
                serde_json::json!({"token": "synthetic-machine-token", "expires_in": 3600}),
            ))
            .mount(server)
            .await;
        let sa = std::env::temp_dir().join(format!("transport-sa-{}", uuid::Uuid::new_v4()));
        std::fs::write(&sa, "synthetic-service-account").unwrap();
        let resolver = ConnectionResolver::new(
            ResolverConfig::new(
                server.uri(),
                "alpha",
                format!("{}/internal/service-token", server.uri()),
                sa.to_string_lossy().to_string(),
            ),
            Arc::new(NoMetrics),
        )
        .unwrap();
        (resolver, TestSa(sa))
    }

    /// Each request returns the selected credential, as the real hub does.
    async fn credential(server: &MockServer, token: &str) {
        Mock::given(method("POST"))
            .and(path(
                "/api/v1/internal/communities/7/provider-connections/12/resolve",
            ))
            .and(header("Authorization", "Bearer synthetic-machine-token"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "communityId": 7, "connectionId": 12, "provider": "test-platform",
                "kind": 1, "accessToken": token, "expiresAt": null,
            })))
            .up_to_n_times(1)
            .mount(server)
            .await;
    }

    /// The complete resolver-to-poll path must fetch rotated tokens on every
    /// request and send each one only to the host-configured upstream.
    #[tokio::test]
    async fn poll_resolves_rotated_oauth_credentials_per_request() {
        let _env = env_read().await;
        let server = MockServer::start().await;
        let (resolver, _sa) = resolver(&server).await;
        let source = TransportSecrets::new(Arc::new(resolver), 7).unwrap();
        let mut poll = PollTransport::new(
            PollConfig {
                platform: "test-platform".into(),
                url: format!("{}/poll", server.uri()),
                source_key: "opaque-source".into(),
                handles: HashMap::from([("auth".into(), CONNECTION)]),
                auth: Some(PollAuth {
                    header: "Authorization".into(),
                    prefix: "Bearer ".into(),
                    handle: "auth".into(),
                }),
                cursor_param: Some("after".into()),
                cursor_pointer: Some("/next".into()),
                initial_cursor: None,
                interval: Duration::from_millis(1),
                request_timeout: Duration::from_secs(2),
                max_body_bytes: 1024,
                max_drop_attempts: 3,
                egress_allow: vec![server.address().ip()],
                allow_loopback_insecure: true,
            },
            source,
        )
        .unwrap();
        poll.connect().await.unwrap();
        for (index, token) in ["first-token", "rotated-token"].iter().enumerate() {
            credential(&server, token).await;
            Mock::given(method("GET"))
                .and(path("/poll"))
                .and(header("Authorization", format!("Bearer {token}")))
                .respond_with(
                    ResponseTemplate::new(200)
                        .set_body_json(serde_json::json!({"next": format!("c{index}")})),
                )
                .expect(1)
                .mount(&server)
                .await;
            poll.next_frame().await.unwrap().unwrap();
            poll.on_frame_outcome(FrameOutcome::Published).await;
        }
        let requests = server.received_requests().await.unwrap();
        assert_eq!(
            requests
                .iter()
                .filter(|r| r.url.path().ends_with("/resolve"))
                .count(),
            2
        );
        assert_eq!(
            requests
                .iter()
                .filter(|r| r.url.path() == "/internal/service-token")
                .count(),
            1
        );
    }

    /// Authentication tokens are expanded only in the transport-owned buffer;
    /// webhook verification continues to reject OAuth credentials.
    #[tokio::test]
    async fn handshake_substitution_keeps_oauth_out_of_verifier() {
        let _env = env_read().await;
        let server = MockServer::start().await;
        let (resolver, _sa) = resolver(&server).await;
        let resolver = Arc::new(resolver);
        credential(&server, "oauth-token").await;
        let template = b"{\"token\":\"{{AUTH}}\"}";
        let output = substitute_slots(
            &TransportSecrets::new(resolver.clone(), 7).unwrap(),
            &HashMap::from([("auth".into(), CONNECTION)]),
            template,
            &[("{{AUTH}}".into(), "auth".into())],
        )
        .await
        .unwrap();
        assert_eq!(&output[..], b"{\"token\":\"oauth-token\"}");
        credential(&server, "oauth-token").await;
        assert_eq!(
            ConnectionResolverSecrets(resolver)
                .resolve(CONNECTION)
                .await
                .err(),
            Some(SecretError::Missing)
        );
    }

    /// A foreign community handle is rejected before a machine JWT is minted,
    /// not merely after a foreign credential has already entered the process.
    #[tokio::test]
    async fn foreign_community_and_invalid_connection_never_reach_hub() {
        let _env = env_read().await;
        let server = MockServer::start().await;
        let (resolver, _sa) = resolver(&server).await;
        let resolver = Arc::new(resolver);
        assert!(TransportSecrets::new(resolver.clone(), 0).is_err());
        let source = TransportSecrets::new(resolver, 7).unwrap();
        for requested in [
            ConnectionRef {
                community_id: 8,
                connection_id: 12,
            },
            ConnectionRef {
                community_id: 7,
                connection_id: 0,
            },
        ] {
            assert_eq!(
                source.resolve(requested).await.err(),
                Some(SecretError::Missing)
            );
        }
        assert!(server.received_requests().await.unwrap().is_empty());
    }

    /// Hub failures and unsupported credential schemes must fail closed,
    /// retaining the transient/unusable distinction used by transport retries.
    #[tokio::test]
    async fn resolve_failures_and_wrong_identity_are_rejected() {
        let _env = env_read().await;
        for (status, body, expected) in [
            (503, serde_json::json!({}), SecretError::Unavailable),
            (404, serde_json::json!({}), SecretError::Missing),
            (
                200,
                serde_json::json!({"communityId": 8, "connectionId": 12, "provider": "test", "kind": 1, "accessToken": "foreign", "expiresAt": null}),
                SecretError::Missing,
            ),
            (
                200,
                serde_json::json!({"communityId": 7, "connectionId": 12, "provider": "test", "kind": 2, "secret": {"scheme": 2, "fields": {"username": "user", "password": "pass"}}, "expiresAt": null}),
                SecretError::Missing,
            ),
        ] {
            let server = MockServer::start().await;
            let (resolver, _sa) = resolver(&server).await;
            Mock::given(method("POST"))
                .and(path(
                    "/api/v1/internal/communities/7/provider-connections/12/resolve",
                ))
                .respond_with(ResponseTemplate::new(status).set_body_json(body))
                .expect(1)
                .mount(&server)
                .await;
            let source = TransportSecrets::new(Arc::new(resolver), 7).unwrap();
            assert_eq!(source.resolve(CONNECTION).await.err(), Some(expected));
        }
    }

    /// Static bearer/API-key values remain supported without a cache, while
    /// an empty credential never causes an unauthenticated upstream request.
    #[tokio::test]
    async fn static_variants_and_empty_credentials() {
        let _env = env_read().await;
        for (secret, expected) in [
            (
                serde_json::json!({"scheme": 1, "fields": {"token": "bearer-key"}}),
                Some("bearer-key"),
            ),
            (
                serde_json::json!({"scheme": 3, "fields": {"header": "X-Key", "value": "api-key"}}),
                Some("api-key"),
            ),
            (
                serde_json::json!({"scheme": 1, "fields": {"token": ""}}),
                None,
            ),
        ] {
            let server = MockServer::start().await;
            let (resolver, _sa) = resolver(&server).await;
            Mock::given(method("POST"))
                .and(path("/api/v1/internal/communities/7/provider-connections/12/resolve"))
                .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"communityId": 7, "connectionId": 12, "provider": "test", "kind": 2, "secret": secret, "expiresAt": null})))
                .expect(1).mount(&server).await;
            let source = TransportSecrets::new(Arc::new(resolver), 7).unwrap();
            let result = source.resolve(CONNECTION).await;
            match expected {
                Some(value) => assert_eq!(result.unwrap().expose(), value),
                None => assert_eq!(result.err(), Some(SecretError::Missing)),
            }
        }
    }
}
