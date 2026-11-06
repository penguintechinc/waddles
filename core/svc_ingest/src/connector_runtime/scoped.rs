//! Per-connection scope: the tenant refusal and the scoped event sink.
//!
//! The connector transports hand the sink an OPAQUE `source_id` (`ws-<hash>`),
//! so the process-wide per-event scope lookup (`publish::publish_event`) can
//! never resolve it. The runtime instead resolves each connection's scope ONCE
//! from the upstream resource the host-owned profile named (a Twitch channel,
//! a Discord guild), refuses the connection when the resolved community is not
//! the connection's community, and stamps every event of that connection with
//! exactly that scope.
//!
//! ```text
//!  DesiredConnection (feed tenant, community) ──> ScopeTenantResolve ──> tenant  (refuse: unresolved / mismatch)
//!                                                  ScopedSinkFactory  ──> ConnectionSink { ResolvedScope } per connection
//! ```
//!
//! Nothing here reads binding metadata or guest data for a scope.

use std::sync::Arc;

use penguin_spine::{KeyRing, SpineMetrics};

use super::feed::DesiredConnection;
use super::supervisor::{SinkBuildError, SinkFactory};
use super::tenant::{source_key_of, tenant_from_scope, TenantError, TenantResolve};
use crate::connector::webhook::BoxFut;
use crate::connector::{to_platform_event, ConnectionCtx, ConnectorEvent, EventSink, SinkError};
use crate::identity_crypto::DekProvider;
use crate::publish::{deterministic_workstream_id, publish_event_scoped, EventAppender};
use crate::scope_resolver::{HubScopeSource, ResolvedScope, ScopeResolver, Unresolved};

/// One upstream scope lookup, object-safe so the wiring can hold it behind an
/// `Arc<dyn ..>` (`ScopeResolver<S>::resolve` is an `async fn` in a trait, whose
/// future is only provably `Send` for a concrete source type).
pub trait ScopeLookup: Send + Sync {
    /// Resolves `(platform, source)` to its authoritative scope or says why not.
    fn lookup<'a>(
        &'a self,
        platform: &'a str,
        source: &'a str,
    ) -> BoxFut<'a, Result<ResolvedScope, Unresolved>>;
}

impl ScopeLookup for ScopeResolver<HubScopeSource> {
    fn lookup<'a>(
        &'a self,
        platform: &'a str,
        source: &'a str,
    ) -> BoxFut<'a, Result<ResolvedScope, Unresolved>> {
        Box::pin(self.resolve(platform, source))
    }
}

/// The upstream resource id hub-api's scope resolution knows for a connection,
/// derived from the profile's host-owned `source_key`. `None` for a platform
/// whose resource has no scope mapping (YouTube live chats): such a connection
/// is refused as unresolved, never defaulted a scope.
#[must_use]
pub fn scope_source(platform: &str, source_key: &str) -> Option<String> {
    let id = match platform {
        "twitch" => source_key.strip_prefix("twitch:"),
        "discord" => source_key.strip_prefix("discord:guild:"),
        _ => None,
    }?;
    (!id.is_empty()).then(|| id.to_owned())
}

/// [`TenantResolve`] over a [`ScopeLookup`]: the tenant is what hub-api says owns
/// the connection's upstream resource, and the resolved community must be the
/// connection's own. Unresolved / mismatch refuse (the runtime parks the
/// connection); a transient lookup failure retries after the cooldown.
pub struct ScopeTenantResolve {
    lookup: Arc<dyn ScopeLookup>,
}

impl ScopeTenantResolve {
    /// Wraps `lookup`.
    #[must_use]
    pub fn new(lookup: Arc<dyn ScopeLookup>) -> Self {
        Self { lookup }
    }
}

impl TenantResolve for ScopeTenantResolve {
    fn resolve<'a>(
        &'a self,
        desired: &'a DesiredConnection,
    ) -> BoxFut<'a, Result<i64, TenantError>> {
        Box::pin(async move {
            let platform = desired.key.platform;
            let Some(source) = source_key_of(desired).and_then(|k| scope_source(platform, k))
            else {
                tracing::warn!(
                    platform,
                    community_id = desired.key.community_id,
                    connection_id = desired.key.connection_id,
                    "connection has no scope-resolvable upstream resource; refusing it"
                );
                return Err(TenantError::Unresolved);
            };
            tenant_from_scope(
                self.lookup.lookup(platform, &source).await,
                desired.key.community_id,
            )
        })
    }
}

/// Builds one [`ConnectionSink`] per connection over the shared spine appender.
pub struct ScopedSinkFactory<A: EventAppender, D: DekProvider> {
    appender: Arc<A>,
    metrics: Arc<dyn SpineMetrics>,
    keyring: Arc<KeyRing>,
    active_kid: String,
    dek_provider: Arc<D>,
    identity_metrics: prometheus::IntCounterVec,
    lookup: Arc<dyn ScopeLookup>,
}

impl<A: EventAppender, D: DekProvider> ScopedSinkFactory<A, D> {
    /// Builds the factory. `lookup` MUST be the same authority the runtime's
    /// `TenantResolve` uses, so the sink scope and the refusal agree.
    #[must_use]
    pub fn new(
        appender: Arc<A>,
        metrics: Arc<dyn SpineMetrics>,
        keyring: Arc<KeyRing>,
        active_kid: String,
        dek_provider: Arc<D>,
        identity_metrics: prometheus::IntCounterVec,
        lookup: Arc<dyn ScopeLookup>,
    ) -> Self {
        Self {
            appender,
            metrics,
            keyring,
            active_kid,
            dek_provider,
            identity_metrics,
            lookup,
        }
    }
}

impl<A: EventAppender + 'static, D: DekProvider + 'static> SinkFactory<ConnectionSink<A, D>>
    for ScopedSinkFactory<A, D>
{
    fn build<'a>(
        &'a self,
        desired: &'a DesiredConnection,
        tenant_id: i64,
    ) -> BoxFut<'a, Result<ConnectionSink<A, D>, SinkBuildError>> {
        Box::pin(async move {
            let platform = desired.key.platform;
            let source = source_key_of(desired)
                .and_then(|k| scope_source(platform, k))
                .ok_or(SinkBuildError("no scope-resolvable upstream resource"))?;
            let resolved = self
                .lookup
                .lookup(platform, &source)
                .await
                .map_err(|_| SinkBuildError("scope is not resolvable"))?;
            // The sink scope is the authority; it must be the connection's own.
            if i64::from(resolved.community_id) != desired.key.community_id
                || resolved.tenant_id != tenant_id
            {
                return Err(SinkBuildError(
                    "resolved scope disagrees with the connection",
                ));
            }
            Ok(ConnectionSink {
                resolved,
                appender: Arc::clone(&self.appender),
                metrics: Arc::clone(&self.metrics),
                keyring: Arc::clone(&self.keyring),
                active_kid: self.active_kid.clone(),
                dek_provider: Arc::clone(&self.dek_provider),
                identity_metrics: self.identity_metrics.clone(),
            })
        })
    }
}

/// Publishes one connection's events onto the spine under the connection's own
/// resolved scope. Holds no handle or secret.
pub struct ConnectionSink<A: EventAppender, D: DekProvider> {
    resolved: ResolvedScope,
    appender: Arc<A>,
    metrics: Arc<dyn SpineMetrics>,
    keyring: Arc<KeyRing>,
    active_kid: String,
    dek_provider: Arc<D>,
    identity_metrics: prometheus::IntCounterVec,
}

impl<A: EventAppender, D: DekProvider> EventSink for ConnectionSink<A, D> {
    async fn publish(&self, ctx: &ConnectionCtx, event: ConnectorEvent) -> Result<(), SinkError> {
        let platform_event = to_platform_event(ctx, event)?;
        let workstream_id = deterministic_workstream_id(&ctx.source_id);
        publish_event_scoped(
            &self.resolved,
            &*self.appender,
            &*self.metrics,
            &self.keyring,
            &self.active_kid,
            &ctx.source_id,
            &workstream_id,
            Some(&ctx.connection_id),
            platform_event,
            &*self.dek_provider,
            &self.identity_metrics,
        )
        .await?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use std::sync::Mutex;

    use penguin_spine::{SpineError, StageEnvelope};

    use super::*;
    use crate::connector_runtime::testkit::{desired_webhook, desired_ws};
    use crate::identity_crypto::DekUnavailableError;

    /// Answers by `(platform, source)` from a script and records every ask.
    #[derive(Default)]
    struct Script {
        answers: Mutex<Vec<((String, String), Result<ResolvedScope, Unresolved>)>>,
        asked: Mutex<Vec<(String, String)>>,
    }

    impl Script {
        fn with(self, platform: &str, source: &str, r: Result<ResolvedScope, Unresolved>) -> Self {
            self.answers
                .lock()
                .unwrap()
                .push(((platform.into(), source.into()), r));
            self
        }
    }

    impl ScopeLookup for Script {
        fn lookup<'a>(
            &'a self,
            platform: &'a str,
            source: &'a str,
        ) -> BoxFut<'a, Result<ResolvedScope, Unresolved>> {
            self.asked
                .lock()
                .unwrap()
                .push((platform.into(), source.into()));
            let hit = self
                .answers
                .lock()
                .unwrap()
                .iter()
                .find(|((p, s), _)| p == platform && s == source)
                .map(|(_, r)| r.clone());
            Box::pin(async move { hit.unwrap_or(Err(Unresolved::NotFound)) })
        }
    }

    fn scope(tenant: i64, community: i64) -> ResolvedScope {
        ResolvedScope::validated(tenant, community, "acme", Some("main")).unwrap()
    }

    #[test]
    fn the_scope_source_comes_from_the_host_owned_source_key_only() {
        assert_eq!(
            scope_source("twitch", "twitch:somechan").as_deref(),
            Some("somechan")
        );
        assert_eq!(
            scope_source("discord", "discord:guild:123456789012345678").as_deref(),
            Some("123456789012345678")
        );
        // Wrong prefix for the platform, empty id, and a platform with no mapping.
        for (p, k) in [
            ("twitch", "discord:guild:1"),
            ("twitch", "twitch:"),
            ("discord", "discord:guild:"),
            ("youtube", "youtube:livechat:abc"),
            ("slack", "slack:x"),
        ] {
            assert_eq!(scope_source(p, k), None, "{p} {k}");
        }
    }

    /// Mutation target: the refusal. An unresolved resource, a resolved
    /// community that is not the connection's, and a platform with no scope
    /// mapping are all refused; only the matching community yields a tenant.
    #[tokio::test]
    async fn the_tenant_is_refused_unless_the_resolved_community_is_the_connections() {
        let lookup = Arc::new(
            Script::default()
                .with("twitch", "good", Ok(scope(5, 7)))
                .with("twitch", "other", Ok(scope(5, 8)))
                .with("twitch", "flaky", Err(Unresolved::Transient)),
        );
        let r = ScopeTenantResolve::new(lookup.clone());
        // (community 7, resource "good") -> tenant 5.
        let d = desired_ws("twitch", 7, 9, 1, "twitch:good", 1);
        assert_eq!(r.resolve(&d).await, Ok(5));
        // The resource resolves to ANOTHER community: mismatch, never a tenant.
        let d = desired_ws("twitch", 7, 9, 1, "twitch:other", 1);
        assert_eq!(r.resolve(&d).await, Err(TenantError::Mismatch));
        // Not found / ambiguous: unresolved.
        let d = desired_ws("twitch", 7, 9, 1, "twitch:unknown", 1);
        assert_eq!(r.resolve(&d).await, Err(TenantError::Unresolved));
        // A transient failure retries, it is not a refusal.
        let d = desired_ws("twitch", 7, 9, 1, "twitch:flaky", 1);
        assert_eq!(r.resolve(&d).await, Err(TenantError::Transient));
        // No scope mapping (youtube) and a webhook plan: unresolved, no lookup.
        let asked_before = lookup.asked.lock().unwrap().len();
        let d = desired_ws("youtube", 7, 9, 1, "youtube:livechat:abc", 1);
        assert_eq!(r.resolve(&d).await, Err(TenantError::Unresolved));
        let d = desired_webhook(7, 9, "route-key-1", 1);
        assert_eq!(r.resolve(&d).await, Err(TenantError::Unresolved));
        assert_eq!(lookup.asked.lock().unwrap().len(), asked_before);
    }

    struct Rec(Mutex<Vec<(String, StageEnvelope)>>);

    impl EventAppender for Rec {
        async fn append(&self, stream: &str, env: &StageEnvelope) -> Result<String, SpineError> {
            self.0.lock().unwrap().push((stream.into(), env.clone()));
            Ok("1-0".into())
        }
    }

    struct Dek;

    impl DekProvider for Dek {
        async fn get_dek(
            &self,
            _tenant_id: &str,
        ) -> Result<(crate::identity_crypto::Dek, u32), DekUnavailableError> {
            Ok((zeroize::Zeroizing::new([9u8; 32]), 1))
        }
    }

    fn factory(lookup: Arc<Script>, appender: Arc<Rec>) -> ScopedSinkFactory<Rec, Dek> {
        ScopedSinkFactory::new(
            appender,
            Arc::new(penguin_spine::NoopMetrics),
            Arc::new(KeyRing::new(vec![("k1".to_string(), vec![7u8; 32])])),
            "k1".into(),
            Arc::new(Dek),
            prometheus::IntCounterVec::new(
                prometheus::Opts::new("scoped_sink_identity_total", "test"),
                &["tenant", "status"],
            )
            .unwrap(),
            lookup,
        )
    }

    fn ctx(source_id: &str) -> ConnectionCtx {
        ConnectionCtx {
            platform: "twitch".into(),
            source_id: source_id.into(),
            connection_id: "conn-1".into(),
            session_token: None,
        }
    }

    fn event() -> ConnectorEvent {
        ConnectorEvent {
            platform: "twitch".into(),
            event_type: "message".into(),
            actor: Some("0b9f6a2c-aaaa-bbbb-cccc-111122223333".into()),
            payload_json: r#"{"text":"hi"}"#.into(),
            occurred_at: "2026-11-05T12:00:00.000Z".into(),
        }
    }

    /// The sink stamps the connection's OWN numeric scope even though the
    /// transport's `source_id` is an opaque hash no resolver could look up.
    #[tokio::test]
    async fn events_publish_under_the_connections_resolved_numeric_scope() {
        let rec = Arc::new(Rec(Mutex::new(Vec::new())));
        let lookup = Arc::new(Script::default().with("twitch", "good", Ok(scope(5, 7))));
        let f = factory(lookup, rec.clone());
        let d = desired_ws("twitch", 7, 9, 1, "twitch:good", 1);
        let sink = f.build(&d, 5).await.unwrap();
        sink.publish(&ctx("ws-0123abcd"), event()).await.unwrap();
        let got = rec.0.lock().unwrap();
        assert_eq!(got.len(), 1, "one event published");
        let (stream, env) = &got[0];
        assert_eq!((env.tenant_id, env.community_id), (Some(5), Some(7)));
        assert_eq!(env.schema_version, penguin_spine::ENVELOPE_SCHEMA_VERSION);
        assert!(stream.contains("ws-0123abcd"), "{stream}");
        assert!(stream.contains("acme"), "tenant slug segment: {stream}");
    }

    #[tokio::test]
    async fn the_sink_refuses_to_build_when_the_scope_disagrees_or_is_missing() {
        let rec = Arc::new(Rec(Mutex::new(Vec::new())));
        let lookup = Arc::new(
            Script::default()
                .with("twitch", "good", Ok(scope(5, 7)))
                .with("twitch", "other", Ok(scope(5, 8))),
        );
        let f = factory(lookup, rec);
        let ok = desired_ws("twitch", 7, 9, 1, "twitch:good", 1);
        // Tenant passed by the supervisor differs from the resolved tenant.
        assert!(f.build(&ok, 6).await.is_err());
        // Resolved community is not the connection's.
        let other = desired_ws("twitch", 7, 9, 1, "twitch:other", 1);
        assert!(f.build(&other, 5).await.is_err());
        // Unresolvable and unmapped resources.
        let missing = desired_ws("twitch", 7, 9, 1, "twitch:nope", 1);
        assert!(f.build(&missing, 5).await.is_err());
        let yt = desired_ws("youtube", 7, 9, 1, "youtube:livechat:abc", 1);
        assert!(f.build(&yt, 5).await.is_err());
    }
}
