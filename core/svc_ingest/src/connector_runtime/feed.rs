//! Desired-set types and the binding feed (spec 1.1).

use std::future::Future;
use std::sync::Arc;
use std::time::Duration;

use connection_resolver::{ConnectionResolve, ResolveError};

use super::plan::{OverlayProfile, RuntimeEnv};
use crate::connector::webhook::WebhookRoute;
use crate::connector::{PollConfig, WsConfig};

/// Bindings requested per page.
pub const PAGE_LIMIT: u32 = 200;
/// Hard cap on pages per discovery; exceeding it is a failure, not a truncation.
pub const MAX_PAGES: u32 = 100;

/// Identity of one supervised connection (numeric ids only, never handles).
#[derive(Clone, PartialEq, Eq, Hash, PartialOrd, Ord, Debug)]
pub struct ConnKey {
    /// Platform slug.
    pub platform: &'static str,
    /// Owning community (numeric).
    pub community_id: i64,
    /// Hub-api connection row (numeric).
    pub connection_id: i64,
}

/// How a desired connection receives frames.
pub enum TransportPlan {
    /// WebSocket (Twitch IRC, Discord gateway).
    Ws(Box<WsConfig>),
    /// Polling (YouTube live chat).
    Poll(Box<PollConfig>),
    /// Verified webhook route bound to a transport.
    Webhook(Box<WebhookPlan>),
}

/// A webhook route the runtime must register and bind.
pub struct WebhookPlan {
    /// The route as `WebhookIngress::register` takes it.
    pub route: WebhookRoute,
    /// Community the (trusted) control plane authorized for this route.
    pub authorized_community: i64,
}

/// One connection the runtime should be running.
pub struct DesiredConnection {
    /// Identity.
    pub key: ConnKey,
    /// Owning tenant (numeric), used for the per-connection sink scope.
    pub tenant_id: i64,
    /// Transport derivation.
    pub plan: TransportPlan,
    /// Hash of the derived config plus secret version; excludes secrets.
    pub fingerprint: u64,
}

/// Why the desired set could not be produced. There is no partial variant.
#[derive(Debug, thiserror::Error)]
pub enum FeedError {
    /// A binding-list call failed.
    #[error("binding list failed: {0}")]
    Resolve(ResolveError),
    /// More than [`MAX_PAGES`] pages.
    #[error("binding list exceeded the page cap")]
    PageCapExceeded,
    /// A binding-list call timed out.
    #[error("binding list call timed out")]
    Timeout,
    /// The cursor did not advance.
    #[error("binding list cursor did not advance")]
    StuckCursor,
    /// The feed does not serve this platform.
    #[error("feed does not serve platform {0}")]
    UnknownPlatform(String),
    /// Behavior pending in the skeleton commit.
    #[error("not implemented: {0}")]
    Unimplemented(&'static str),
}

/// Source of the full desired set for one platform. `Err` means "keep the
/// current set" -- never "stop everything".
pub trait BindingFeed: Send + Sync {
    /// Full desired set for `platform`: pages to completion (cap
    /// [`MAX_PAGES`]), dedups, fail-closed on cross-community claims.
    fn desired(
        &self,
        platform: &'static str,
    ) -> impl Future<Output = Result<Vec<DesiredConnection>, FeedError>> + Send;
}

/// [`BindingFeed`] over hub-api's binding list for one platform profile.
pub struct HubBindingFeed<R: ConnectionResolve, P: OverlayProfile> {
    resolver: Arc<R>,
    profile: P,
    env: RuntimeEnv,
    call_timeout: Duration,
}

impl<R: ConnectionResolve, P: OverlayProfile> HubBindingFeed<R, P> {
    /// Builds a feed; each binding-list call is bounded by `call_timeout`.
    pub fn new(resolver: Arc<R>, profile: P, env: RuntimeEnv, call_timeout: Duration) -> Self {
        Self {
            resolver,
            profile,
            env,
            call_timeout,
        }
    }
}

impl<R: ConnectionResolve, P: OverlayProfile> BindingFeed for HubBindingFeed<R, P> {
    async fn desired(&self, _platform: &'static str) -> Result<Vec<DesiredConnection>, FeedError> {
        let _ = (&self.resolver, &self.profile, &self.env, self.call_timeout);
        Err(FeedError::Unimplemented("HubBindingFeed::desired"))
    }
}
