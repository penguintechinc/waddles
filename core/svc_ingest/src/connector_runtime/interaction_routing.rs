//! Boot wiring of [`ContextSink`]: every routed Discord interaction calls
//! `InteractionStore::set_routed`.
//!
//! The runtime's per-connection sink is wrapped here. A Discord connection gets a
//! [`ContextSink`] bound to ITS OWN application id (read from the connection's resolved
//! binding metadata, never from a payload); every other platform keeps the plain sink.
//! Enabled exactly when the Discord platform is (the same gate as the interaction tap, so
//! the slash feature defaults together): Discord ON with a missing dependency is a boot
//! error, and a Discord connection whose binding carries no valid application id is
//! refused loudly (the supervisor logs the sink-build failure) rather than run unrouted.

use std::sync::Arc;
use std::time::Duration;

use bundle_host_http::guarded::{GuardedClient, GuardedLimits};
use connection_resolver::{ConnectionRef, ConnectionResolve};

use super::feed::DesiredConnection;
use super::supervisor::{SinkBuildError, SinkFactory};
use crate::active_context::{
    register_routing_metrics, ContextError, ContextSink, ContextSource, HubContextSource,
    InteractionReplier, InvokerTokenizer, RoutingMetrics,
};
use crate::commands_reconcile::BoxFut;
use crate::config::CliConfig;
use crate::connector::host_calls::{CallScope, IdentityTokenizer};
use crate::connector::sink::{EventSink, SinkError};
use crate::connector::types::{ConnectionCtx, ConnectorEvent};
use crate::discord_rest::is_snowflake;
use crate::interaction_store::InteractionStore;

/// Metadata key carrying a Discord binding's application (client) id.
const META_APPLICATION_ID: &str = "application_id";
/// Bound on the binding resolve made while building a connection's sink.
const RESOLVE_TIMEOUT: Duration = Duration::from_secs(5);
/// Bound and size cap of the refusal-notice followup.
const REPLY_TIMEOUT: Duration = Duration::from_secs(5);

/// The shared dependencies every Discord connection's [`ContextSink`] is built from.
#[derive(Clone)]
pub struct InteractionRouting {
    /// hub-api active-context access.
    pub source: Arc<dyn ContextSource>,
    /// Host-held interaction store (same Valkey as the tap).
    pub store: InteractionStore,
    /// Refusal-notice replier over the same store.
    pub replier: Arc<InteractionReplier>,
    /// Registered routing instruments.
    pub metrics: RoutingMetrics,
    /// The hub-api identity tokenizer (the same one behind the host-call allowlist); each
    /// connection's sink binds it to its own tenant/community/connection scope.
    pub tokenizer: Arc<dyn IdentityTokenizer>,
}

/// [`InvokerTokenizer`] bound to ONE connection's host-derived scope: the invoker's raw
/// Discord id is minted under that connection's tenant, never one the guest names.
struct ConnectionTokenizer {
    inner: Arc<dyn IdentityTokenizer>,
    scope: CallScope,
}

impl InvokerTokenizer for ConnectionTokenizer {
    fn tokenize<'a>(
        &'a self,
        _source_id: &'a str,
        raw_user_id: &'a str,
    ) -> BoxFut<'a, Result<String, ContextError>> {
        Box::pin(async move {
            self.inner
                .tokenize(&self.scope, raw_user_id)
                .await
                .map_err(|_| ContextError::Unavailable)
        })
    }
}

impl InteractionRouting {
    /// Production wiring from the process configuration: a machine JWT scoped to the
    /// active-context endpoints, the lease Valkey store and a registered metric set.
    ///
    /// # Errors
    /// A missing JWT bootstrap, a plaintext hub-api outside alpha, an HTTP client build
    /// failure or a duplicate metric registration -- all fatal, never a fallback.
    pub async fn from_config(
        cli: &CliConfig,
        store: InteractionStore,
        registry: &prometheus::Registry,
        tokenizer: Arc<dyn IdentityTokenizer>,
    ) -> anyhow::Result<Self> {
        if cli.service_jwt_token_endpoint.is_empty() {
            anyhow::bail!(
                "Discord interactions are enabled but SERVICE_JWT_TOKEN_ENDPOINT is unset: \
                 no machine JWT for active-context routing"
            );
        }
        let metrics = register_routing_metrics(registry)
            .map_err(|_| anyhow::anyhow!("interaction routing metrics could not be registered"))?;
        let ca_file = cli.hub_api_grpc_ca_file.trim();
        let source = HubContextSource::connect(
            &cli.hub_api_grpc_endpoint,
            &cli.service_jwt_token_endpoint,
            &cli.service_jwt_sa_token_path,
            (!ca_file.is_empty()).then_some(ca_file),
        )
        .await
        .map_err(|e| anyhow::anyhow!("active-context hub-api gRPC connect failed: {e}"))?;
        let client = GuardedClient::new(
            GuardedLimits {
                max_redirects: 0,
                timeout: REPLY_TIMEOUT,
                max_response_bytes: 16 * 1024,
            },
            Vec::new(),
        );
        let replier = Arc::new(InteractionReplier::new(
            store.clone(),
            client,
            crate::interaction_tap::API_BASE.to_string(),
        ));
        Ok(Self {
            source: Arc::new(source),
            store,
            replier,
            metrics,
            tokenizer,
        })
    }
}

/// A connection's sink: the plain one, or the plain one behind the routing wrapper.
pub enum RoutedSink<S: EventSink> {
    /// Non-Discord connections.
    Plain(S),
    /// Discord connections: interaction events are routed before they are published.
    Context(ContextSink<S>),
}

impl<S: EventSink> EventSink for RoutedSink<S> {
    async fn publish(&self, ctx: &ConnectionCtx, event: ConnectorEvent) -> Result<(), SinkError> {
        match self {
            Self::Plain(s) => s.publish(ctx, event).await,
            Self::Context(s) => s.publish(ctx, event).await,
        }
    }
}

/// Wraps an inner factory so Discord connections get a [`ContextSink`].
pub struct RoutingSinkFactory<S, R: ConnectionResolve + 'static> {
    inner: Arc<dyn SinkFactory<S>>,
    resolver: Arc<R>,
    routing: Option<InteractionRouting>,
}

impl<S, R: ConnectionResolve + 'static> RoutingSinkFactory<S, R> {
    /// `routing = None` is only legal without Discord (assembly enforces it).
    #[must_use]
    pub fn new(
        inner: Arc<dyn SinkFactory<S>>,
        resolver: Arc<R>,
        routing: Option<InteractionRouting>,
    ) -> Self {
        Self {
            inner,
            resolver,
            routing,
        }
    }

    /// This connection's own application id from its resolved binding.
    async fn application_id(&self, desired: &DesiredConnection) -> Result<String, SinkBuildError> {
        let resolved = tokio::time::timeout(
            RESOLVE_TIMEOUT,
            self.resolver.resolve(ConnectionRef {
                community_id: desired.key.community_id,
                connection_id: desired.key.connection_id,
            }),
        )
        .await
        .map_err(|_| SinkBuildError("discord binding resolve timed out"))?
        .map_err(|_| SinkBuildError("discord binding could not be resolved"))?;
        if resolved.community_id != desired.key.community_id
            || resolved.connection_id != desired.key.connection_id
        {
            return Err(SinkBuildError("resolved binding is not the connection's"));
        }
        match resolved.metadata.get(META_APPLICATION_ID).map(|a| a.trim()) {
            Some(a) if is_snowflake(a) => Ok(a.to_string()),
            _ => Err(SinkBuildError(
                "discord binding has no valid application_id; interactions cannot be routed",
            )),
        }
    }
}

impl<S, R> SinkFactory<RoutedSink<S>> for RoutingSinkFactory<S, R>
where
    S: EventSink + Send + Sync + 'static,
    R: ConnectionResolve + 'static,
{
    fn build<'a>(
        &'a self,
        desired: &'a DesiredConnection,
        tenant_id: i64,
    ) -> BoxFut<'a, Result<RoutedSink<S>, SinkBuildError>> {
        Box::pin(async move {
            let inner = self.inner.build(desired, tenant_id).await?;
            if desired.key.platform != "discord" {
                return Ok(RoutedSink::Plain(inner));
            }
            let routing = self.routing.as_ref().ok_or(SinkBuildError(
                "discord interaction routing is not configured",
            ))?;
            let application_id = self.application_id(desired).await?;
            Ok(RoutedSink::Context(ContextSink::new(
                inner,
                Arc::clone(&routing.source),
                Arc::clone(&routing.replier),
                routing.store.clone(),
                application_id,
                routing.metrics.clone(),
                Arc::new(ConnectionTokenizer {
                    inner: Arc::clone(&routing.tokenizer),
                    scope: CallScope {
                        platform: "discord",
                        community_id: desired.key.community_id,
                        tenant_id,
                        connection_id: desired.key.connection_id,
                    },
                }),
            )))
        })
    }
}
