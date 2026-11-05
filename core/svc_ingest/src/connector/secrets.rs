//! Credential resolution for generic outbound transports. OAuth access tokens
//! are accepted here, separately from webhook verification keys: accepting an
//! OAuth token as an HMAC signing key would confuse two different credentials.

use std::sync::Arc;

use connection_resolver::{
    ConnectionRef, ConnectionResolve, ConnectionSecret, ResolveError, SecretString,
};

use super::transport::TransportError;
use super::webhook::{BoxFut, SecretError};

/// Where a credential is about to be sent: the host of the transport's target
/// URL. The credential is only released if this host is approved for the
/// RESOLVED connection (never decided by overlay or guest input).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Destination {
    host: String,
    port: u16,
    secure: bool,
}

impl Destination {
    /// Builds from a validated target URL; `None` when it has no host or no
    /// known port. Scheme security (`https`/`wss`) and the effective port are
    /// part of the destination, not just the host.
    pub fn from_url(url: &reqwest::Url) -> Option<Self> {
        Some(Self {
            host: url.host_str()?.trim_end_matches('.').to_ascii_lowercase(),
            port: url.port_or_known_default()?,
            secure: matches!(url.scheme(), "https" | "wss"),
        })
    }
}

/// A resolved credential plus, for API-key rows, the STORED header name.
pub struct ResolvedAuth {
    /// The secret value (zeroizing, redacted `Debug`).
    pub secret: SecretString,
    /// Stored header name for `ApiKeyHeader` rows; overrides any overlay header.
    pub header: Option<String>,
}

/// Host-owned allowlist: exact upstream hosts approved per RESOLVED provider.
/// Exact match only (no suffixes): shared-suffix hosts such as
/// `*.googleapis.com` include user-controlled buckets. Unknown provider = none.
/// NOTE: hosts listed are the supported platforms' published API/gateway hosts;
/// only `www.googleapis.com` is exercised by an in-tree adapter today.
pub fn approved_hosts(provider: &str) -> &'static [&'static str] {
    match provider.to_ascii_lowercase().as_str() {
        "twitch" => &[
            "api.twitch.tv",
            "id.twitch.tv",
            "irc-ws.chat.twitch.tv",
            "eventsub.wss.twitch.tv",
        ],
        "discord" => &["discord.com", "gateway.discord.gg"],
        "youtube" => &["www.googleapis.com", "youtube.googleapis.com"],
        "slack" => &["slack.com", "www.slack.com"],
        // Test-only provider whose upstream is the loopback mock server.
        #[cfg(test)]
        "testprov" => &["127.0.0.1"],
        _ => &[],
    }
}

/// Authentication credentials for outbound transports, distinct from inbound
/// verification keys so an OAuth source cannot be wired into webhook ingress.
pub trait TransportSecretSource: Send + Sync {
    /// Resolve the host-approved connection immediately before sending; refuse
    /// (`Missing`) unless `dest` is approved for the resolved connection.
    fn resolve(
        &self,
        requested: ConnectionRef,
        dest: Destination,
    ) -> BoxFut<'_, Result<ResolvedAuth, SecretError>>;
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
    expected_provider: Option<String>,
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
            expected_provider: None,
        })
    }

    /// Additionally requires the resolved provider to equal this host-verified
    /// value (never the overlay's platform field).
    pub fn with_expected_provider(mut self, provider: impl Into<String>) -> Self {
        self.expected_provider = Some(provider.into().to_ascii_lowercase());
        self
    }
}

/// True when `dest` is approved for the resolved connection: in the provider
/// allowlist, and (static rows) also the host of the stored `endpoint.url`.
/// A static row WITHOUT a valid matching endpoint is refused; only OAuth rows
/// may omit it (the provider allowlist is their floor).
fn destination_approved(
    resolved_provider: &str,
    endpoint_url: Option<&str>,
    is_static: bool,
    dest: &Destination,
) -> bool {
    if !approved_hosts(resolved_provider).contains(&dest.host.as_str()) {
        return false;
    }
    let loopback_exception = insecure_loopback_allowed(resolved_provider);
    // Credentials only travel over TLS to the provider's standard port; the
    // sole exception is the explicit test-only loopback provider.
    if !loopback_exception && !(dest.secure && dest.port == 443) {
        return false;
    }
    match endpoint_url {
        None => !is_static,
        Some(u) => reqwest::Url::parse(u)
            .ok()
            .and_then(|u| Destination::from_url(&u))
            .is_some_and(|d| {
                if loopback_exception {
                    d.host == dest.host
                } else {
                    d == *dest
                }
            }),
    }
}

/// Test-only escape hatch: the loopback mock provider may use plain `http`/`ws`
/// and arbitrary ports. Always false in non-test builds.
fn insecure_loopback_allowed(provider: &str) -> bool {
    #[cfg(test)]
    {
        provider.eq_ignore_ascii_case("testprov")
    }
    #[cfg(not(test))]
    {
        let _ = provider;
        false
    }
}

impl<R: ConnectionResolve + 'static> TransportSecretSource for TransportSecrets<R> {
    /// Select one supported authentication secret after checking the resolved
    /// connection identity; multi-field authentication must use its own adapter.
    fn resolve(
        &self,
        requested: ConnectionRef,
        dest: Destination,
    ) -> BoxFut<'_, Result<ResolvedAuth, SecretError>> {
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
            if let Some(expected) = &self.expected_provider {
                if !resolved.provider.eq_ignore_ascii_case(expected) {
                    return Err(SecretError::Missing);
                }
            }
            let is_static = matches!(
                resolved.secret,
                ConnectionSecret::Bearer { .. } | ConnectionSecret::ApiKeyHeader { .. }
            );
            if !destination_approved(
                &resolved.provider,
                resolved.endpoint.url.as_deref(),
                is_static,
                &dest,
            ) {
                return Err(SecretError::Missing);
            }
            let (secret, header) = match resolved.secret {
                ConnectionSecret::OAuthAccessToken { access_token } => (access_token, None),
                ConnectionSecret::Bearer { token } => (token, None),
                ConnectionSecret::ApiKeyHeader { header, value } => (value, Some(header)),
                _ => return Err(SecretError::Missing),
            };
            if secret.expose().is_empty() {
                return Err(SecretError::Missing);
            }
            Ok(ResolvedAuth { secret, header })
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

    /// Destination host the loopback mock "upstream" presents (approved for `testprov`).
    fn dest() -> Destination {
        Destination::from_url(&reqwest::Url::parse("https://127.0.0.1/x").unwrap()).unwrap()
    }

    fn dest_of(url: &str) -> Destination {
        Destination::from_url(&reqwest::Url::parse(url).unwrap()).unwrap()
    }

    /// regression: p982-B -- host-only matching let a plaintext or off-port URL on
    /// an approved host receive the credential.
    #[test]
    fn destination_requires_tls_and_standard_port() {
        for (url, ok) in [
            ("https://api.twitch.tv/x", true),
            ("wss://irc-ws.chat.twitch.tv/", true),
            ("http://api.twitch.tv/x", false),
            ("ws://irc-ws.chat.twitch.tv/", false),
            ("https://api.twitch.tv:8443/x", false),
            ("wss://irc-ws.chat.twitch.tv:9999/", false),
        ] {
            assert_eq!(
                destination_approved("twitch", None, false, &dest_of(url)),
                ok,
                "{url}"
            );
        }
        let d = dest_of("https://api.twitch.tv/x");
        assert!(destination_approved(
            "twitch",
            Some("https://api.twitch.tv/h"),
            true,
            &d
        ));
        assert!(!destination_approved(
            "twitch",
            Some("http://api.twitch.tv/h"),
            true,
            &d
        ));
        assert!(!destination_approved(
            "twitch",
            Some("https://api.twitch.tv:444/h"),
            true,
            &d
        ));
    }

    /// Mounts one resolve answer with an arbitrary body.
    async fn mount_resolve(server: &MockServer, body: serde_json::Value) {
        Mock::given(method("POST"))
            .and(path(
                "/api/v1/internal/communities/7/provider-connections/12/resolve",
            ))
            .respond_with(ResponseTemplate::new(200).set_body_json(body))
            .mount(server)
            .await;
    }

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
                "communityId": 7, "connectionId": 12, "provider": "testprov",
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
                platform: "overlay-claims-anything".into(),
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
                lifecycle: Default::default(),
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
            &[b"\"token\":\"".to_vec()],
            &dest(),
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
                source.resolve(requested, dest()).await.err(),
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
                serde_json::json!({"communityId": 8, "connectionId": 12, "provider": "testprov", "kind": 1, "accessToken": "foreign", "expiresAt": null}),
                SecretError::Missing,
            ),
            (
                200,
                serde_json::json!({"communityId": 7, "connectionId": 12, "provider": "testprov", "kind": 2, "secret": {"scheme": 2, "fields": {"username": "user", "password": "pass"}}, "expiresAt": null}),
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
            assert_eq!(
                source.resolve(CONNECTION, dest()).await.err(),
                Some(expected)
            );
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
                .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"communityId": 7, "connectionId": 12, "provider": "testprov", "kind": 2, "secret": secret, "endpoint": {"url": "https://127.0.0.1/x"}, "expiresAt": null})))
                .expect(1).mount(&server).await;
            let source = TransportSecrets::new(Arc::new(resolver), 7).unwrap();
            let result = source.resolve(CONNECTION, dest()).await;
            match expected {
                Some(value) => assert_eq!(result.unwrap().secret.expose(), value),
                None => assert_eq!(result.err(), Some(SecretError::Missing)),
            }
        }
    }

    /// Off-allowlist hosts never receive any credential type; the allowlist
    /// follows the RESOLVED provider, and an unknown provider has no hosts.
    #[tokio::test]
    async fn destination_outside_resolved_provider_allowlist_is_refused() {
        let _env = env_read().await;
        for (provider, body_extra, host) in [
            (
                "testprov",
                serde_json::json!({"kind": 1, "accessToken": "oauth-secret"}),
                "https://attacker.example/x",
            ),
            (
                "twitch",
                serde_json::json!({"kind": 1, "accessToken": "oauth-secret"}),
                "https://attacker.example/x",
            ),
            (
                "twitch",
                serde_json::json!({"kind": 1, "accessToken": "oauth-secret"}),
                "https://127.0.0.1/x",
            ),
            (
                "unknown-provider",
                serde_json::json!({"kind": 1, "accessToken": "oauth-secret"}),
                "https://api.twitch.tv/x",
            ),
            (
                "twitch",
                serde_json::json!({"kind": 2, "secret": {"scheme": 1, "fields": {"token": "bearer-secret"}}}),
                "https://evil.twitch.tv.attacker.example/x",
            ),
        ] {
            let server = MockServer::start().await;
            let (resolver, _sa) = resolver(&server).await;
            let mut body = serde_json::json!({"communityId": 7, "connectionId": 12, "provider": provider, "expiresAt": null});
            body.as_object_mut()
                .unwrap()
                .extend(body_extra.as_object().unwrap().clone());
            mount_resolve(&server, body).await;
            let source = TransportSecrets::new(Arc::new(resolver), 7).unwrap();
            assert_eq!(
                source.resolve(CONNECTION, dest_of(host)).await.err(),
                Some(SecretError::Missing),
                "{provider} -> {host}"
            );
        }
        // Approved host for the resolved provider is accepted.
        let server = MockServer::start().await;
        let (resolver, _sa) = resolver(&server).await;
        mount_resolve(&server, serde_json::json!({"communityId": 7, "connectionId": 12, "provider": "twitch", "kind": 1, "accessToken": "ok", "expiresAt": null})).await;
        let source = TransportSecrets::new(Arc::new(resolver), 7).unwrap();
        let ok = source
            .resolve(CONNECTION, dest_of("https://irc-ws.chat.twitch.tv/"))
            .await
            .unwrap();
        assert_eq!(ok.secret.expose(), "ok");
    }

    /// The overlay cannot point a registered handle at an off-allowlist
    /// endpoint to harvest the credential: zero requests reach that host.
    #[tokio::test]
    async fn poll_to_unapproved_host_sends_nothing_and_cannot_echo() {
        let _env = env_read().await;
        let hub = MockServer::start().await;
        let (resolver, _sa) = resolver(&hub).await;
        // Provider approves only 127.0.0.1 hosts for "twitch"? No: "twitch" hosts
        // are real names, so the loopback "attacker" is off-allowlist.
        mount_resolve(&hub, serde_json::json!({"communityId": 7, "connectionId": 12, "provider": "twitch", "kind": 1, "accessToken": "leak-me", "expiresAt": null})).await;
        let attacker = MockServer::start().await;
        Mock::given(method("GET"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({})))
            .mount(&attacker)
            .await;
        let source = TransportSecrets::new(Arc::new(resolver), 7).unwrap();
        let mut poll = PollTransport::new(
            PollConfig {
                platform: "twitch".into(),
                url: format!("{}/poll", attacker.uri()),
                source_key: "opaque-source".into(),
                handles: HashMap::from([("auth".into(), CONNECTION)]),
                auth: Some(PollAuth {
                    header: "Authorization".into(),
                    prefix: "Bearer ".into(),
                    handle: "auth".into(),
                }),
                cursor_param: None,
                cursor_pointer: None,
                initial_cursor: None,
                interval: Duration::from_millis(1),
                request_timeout: Duration::from_secs(2),
                max_body_bytes: 1024,
                max_drop_attempts: 3,
                egress_allow: vec![attacker.address().ip()],
                allow_loopback_insecure: true,
                lifecycle: Default::default(),
            },
            source,
        )
        .unwrap();
        poll.connect().await.unwrap();
        assert!(poll.next_frame().await.is_err());
        assert!(attacker.received_requests().await.unwrap().is_empty());
    }

    /// Provider binding: a host-supplied expected provider must equal the
    /// RESOLVED provider, whatever the overlay platform string claims.
    #[tokio::test]
    async fn expected_provider_mismatch_is_rejected() {
        let _env = env_read().await;
        for (expected, ok) in [("twitch", true), ("discord", false)] {
            let server = MockServer::start().await;
            let (resolver, _sa) = resolver(&server).await;
            mount_resolve(&server, serde_json::json!({"communityId": 7, "connectionId": 12, "provider": "twitch", "kind": 1, "accessToken": "t", "expiresAt": null})).await;
            let source = TransportSecrets::new(Arc::new(resolver), 7)
                .unwrap()
                .with_expected_provider(expected);
            let r = source
                .resolve(CONNECTION, dest_of("https://api.twitch.tv/"))
                .await;
            assert_eq!(r.is_ok(), ok, "{expected}");
        }
    }

    /// API-key rows return the STORED header name, and a static row's
    /// `endpoint.url` host must also match the destination.
    #[tokio::test]
    async fn api_key_uses_stored_header_and_static_endpoint_is_enforced() {
        let _env = env_read().await;
        let server = MockServer::start().await;
        let (resolver, _sa) = resolver(&server).await;
        mount_resolve(&server, serde_json::json!({
            "communityId": 7, "connectionId": 12, "provider": "twitch", "kind": 2, "expiresAt": null,
            "endpoint": {"url": "https://api.twitch.tv/helix"},
            "secret": {"scheme": 3, "fields": {"header": "X-Stored", "value": "k"}}
        })).await;
        let source = TransportSecrets::new(Arc::new(resolver), 7).unwrap();
        let got = source
            .resolve(CONNECTION, dest_of("https://api.twitch.tv/"))
            .await
            .unwrap();
        assert_eq!(got.header.as_deref(), Some("X-Stored"));
        // In the provider allowlist but not the row's own endpoint host.
        assert_eq!(
            source
                .resolve(CONNECTION, dest_of("https://id.twitch.tv/"))
                .await
                .err(),
            Some(SecretError::Missing)
        );
    }

    /// End to end: an API-key row is sent under the stored header name with a
    /// raw value, ignoring the overlay's header/prefix.
    #[tokio::test]
    async fn poll_sends_api_key_under_stored_header() {
        let _env = env_read().await;
        let server = MockServer::start().await;
        let (resolver, _sa) = resolver(&server).await;
        mount_resolve(&server, serde_json::json!({
            "communityId": 7, "connectionId": 12, "provider": "testprov", "kind": 2, "expiresAt": null,
            "endpoint": {"url": "https://127.0.0.1/x"},
            "secret": {"scheme": 3, "fields": {"header": "X-Stored", "value": "api-val"}}
        })).await;
        Mock::given(method("GET"))
            .and(path("/poll"))
            .and(header("X-Stored", "api-val"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({})))
            .expect(1)
            .mount(&server)
            .await;
        let source = TransportSecrets::new(Arc::new(resolver), 7).unwrap();
        let mut poll = PollTransport::new(
            PollConfig {
                platform: "overlay-claims-anything".into(),
                url: format!("{}/poll", server.uri()),
                source_key: "opaque-source".into(),
                handles: HashMap::from([("auth".into(), CONNECTION)]),
                auth: Some(PollAuth {
                    header: "Authorization".into(),
                    prefix: "Bearer ".into(),
                    handle: "auth".into(),
                }),
                cursor_param: None,
                cursor_pointer: None,
                initial_cursor: None,
                interval: Duration::from_millis(1),
                request_timeout: Duration::from_secs(2),
                max_body_bytes: 1024,
                max_drop_attempts: 3,
                egress_allow: vec![server.address().ip()],
                allow_loopback_insecure: true,
                lifecycle: Default::default(),
            },
            source,
        )
        .unwrap();
        poll.connect().await.unwrap();
        poll.next_frame().await.unwrap().unwrap();
    }

    /// Static rows must carry an endpoint matching the destination; a missing
    /// endpoint refuses (secret not released) while OAuth may omit it.
    #[tokio::test]
    async fn static_rows_require_endpoint_but_oauth_does_not() {
        let _env = env_read().await;
        let twitch = dest_of("https://api.twitch.tv/");
        for (secret, endpoint, ok) in [
            (
                serde_json::json!({"scheme": 1, "fields": {"token": "b"}}),
                None,
                false,
            ),
            (
                serde_json::json!({"scheme": 3, "fields": {"header": "X-K", "value": "v"}}),
                None,
                false,
            ),
            (
                serde_json::json!({"scheme": 1, "fields": {"token": "b"}}),
                Some("https://api.twitch.tv/x"),
                true,
            ),
            (
                serde_json::json!({"scheme": 1, "fields": {"token": "b"}}),
                Some("not a url"),
                false,
            ),
        ] {
            let server = MockServer::start().await;
            let (resolver, _sa) = resolver(&server).await;
            let mut body = serde_json::json!({"communityId": 7, "connectionId": 12, "provider": "twitch", "kind": 2, "secret": secret, "expiresAt": null});
            if let Some(u) = endpoint {
                body["endpoint"] = serde_json::json!({"url": u});
            }
            mount_resolve(&server, body).await;
            let source = TransportSecrets::new(Arc::new(resolver), 7).unwrap();
            assert_eq!(
                source.resolve(CONNECTION, twitch.clone()).await.is_ok(),
                ok,
                "{endpoint:?}"
            );
        }
        let server = MockServer::start().await;
        let (resolver, _sa) = resolver(&server).await;
        mount_resolve(&server, serde_json::json!({"communityId": 7, "connectionId": 12, "provider": "twitch", "kind": 1, "accessToken": "t", "expiresAt": null})).await;
        let source = TransportSecrets::new(Arc::new(resolver), 7).unwrap();
        assert!(source.resolve(CONNECTION, twitch).await.is_ok());
    }
}
