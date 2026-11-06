//! Desired-set types and the binding feed (spec 1.1).
//!
//! The desired set is returned COMPLETE or not at all: any page failure aborts
//! the whole discovery, because a silently shorter set would make the
//! reconciler tear down healthy connections. Mirrors the legacy
//! `twitch_sessions::spec::discover` (paging, stuck-cursor check, fail-closed
//! on a resource claimed by several communities), generalized over a
//! platform [`ConnectorProfile`].

use std::collections::BTreeMap;
use std::fmt;
use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use connection_resolver::{ConnectionResolve, ResolveError};
use sha2::{Digest, Sha256};

use super::metrics::{saturating_u32, RuntimeMetrics, SkipReason};
use super::plan::{ConnectorProfile, RuntimeEnv};
use crate::connector::spectrum::SpectrumSpec;
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
    /// Polling with host-side dedupe/priming or snapshot diffing (Spectrum).
    Spectrum(Box<SpectrumPlan>),
}

impl TransportPlan {
    /// Fixed, PII-free variant name for logs.
    #[must_use]
    pub fn kind(&self) -> &'static str {
        match self {
            Self::Ws(_) => "ws",
            Self::Poll(_) => "poll",
            Self::Webhook(_) => "webhook",
            Self::Spectrum(_) => "spectrum",
        }
    }
}

/// A stateful poll: the poll config plus the host-owned dedupe/diff spec.
pub struct SpectrumPlan {
    /// The poll transport configuration.
    pub poll: PollConfig,
    /// Dedupe / snapshot behaviour and the community-scoped store key.
    pub spec: SpectrumSpec,
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

/// `Debug` shows identity and plan kind only: the plan's `source_key` is a
/// handle (PII) and must never reach a log line.
impl fmt::Debug for DesiredConnection {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("DesiredConnection")
            .field("key", &self.key)
            .field("tenant_id", &self.tenant_id)
            .field("plan", &self.plan.kind())
            .field("fingerprint", &self.fingerprint)
            .finish()
    }
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

/// What a connection claims upstream, for cross-community conflict checks.
#[derive(PartialEq, Eq, PartialOrd, Ord)]
enum Claim {
    /// A Ws/Poll `source_key` (channel, guild, live chat, ...).
    Resource(String),
    /// A webhook `route_key`.
    Route(String),
}

fn claim_of(d: &DesiredConnection) -> Claim {
    match &d.plan {
        TransportPlan::Ws(c) => Claim::Resource(c.source_key.clone()),
        TransportPlan::Poll(c) => Claim::Resource(c.source_key.clone()),
        TransportPlan::Webhook(w) => Claim::Route(w.route.route_key.clone()),
        TransportPlan::Spectrum(s) => Claim::Resource(s.poll.source_key.clone()),
    }
}

/// Short SHA-256 fingerprint of a claim, safe to log (the claim may be a handle).
fn claim_fingerprint(c: &Claim) -> String {
    let (Claim::Resource(s) | Claim::Route(s)) = c;
    Sha256::digest(s.as_bytes())
        .iter()
        .take(6)
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// Counts of what the dedup pass dropped.
#[derive(Debug, Default, PartialEq, Eq)]
struct DedupReport {
    duplicate: u32,
    conflict: u32,
}

/// Groups by claim: a claim held by several communities is dropped for ALL of
/// them (fail closed, alerts with a fingerprint); within one community the
/// lowest `connection_id` wins. Output is sorted by key for determinism.
fn dedup(all: Vec<DesiredConnection>) -> (Vec<DesiredConnection>, DedupReport) {
    let mut by_claim: BTreeMap<Claim, Vec<DesiredConnection>> = BTreeMap::new();
    for d in all {
        by_claim.entry(claim_of(&d)).or_default().push(d);
    }
    let mut out = Vec::new();
    let mut report = DedupReport::default();
    for (claim, mut group) in by_claim {
        let mut communities: Vec<i64> = group.iter().map(|g| g.key.community_id).collect();
        communities.sort_unstable();
        communities.dedup();
        if communities.len() > 1 {
            tracing::warn!(
                platform = group[0].key.platform,
                communities = ?communities,
                claim_hash = %claim_fingerprint(&claim),
                "resource claimed by multiple communities; skipping all claimants"
            );
            report.conflict += 1;
            continue;
        }
        group.sort_by_key(|g| g.key.connection_id);
        report.duplicate += saturating_u32(group.len() - 1);
        out.extend(group.into_iter().take(1));
    }
    out.sort_by(|a, b| a.key.cmp(&b.key));
    (out, report)
}

/// [`BindingFeed`] over hub-api's binding list for one platform profile.
pub struct HubBindingFeed<R: ConnectionResolve, P: ConnectorProfile> {
    resolver: Arc<R>,
    profile: P,
    env: RuntimeEnv,
    call_timeout: Duration,
    metrics: Option<RuntimeMetrics>,
}

impl<R: ConnectionResolve, P: ConnectorProfile> HubBindingFeed<R, P> {
    /// Builds a feed; each binding-list call is bounded by `call_timeout`.
    pub fn new(resolver: Arc<R>, profile: P, env: RuntimeEnv, call_timeout: Duration) -> Self {
        Self {
            resolver,
            profile,
            env,
            call_timeout,
            metrics: None,
        }
    }

    /// Counts skipped bindings on `metrics` (bounded labels only).
    #[must_use]
    pub fn with_metrics(mut self, metrics: RuntimeMetrics) -> Self {
        self.metrics = Some(metrics);
        self
    }

    /// Pages the binding list to completion and plans each binding.
    async fn collect(&self) -> Result<(Vec<DesiredConnection>, u32), FeedError> {
        let platform = self.profile.platform();
        let mut planned = Vec::new();
        let mut malformed = 0u32;
        let mut after = 0i64;
        let mut pages = 0u32;
        loop {
            if pages >= MAX_PAGES {
                return Err(FeedError::PageCapExceeded);
            }
            pages += 1;
            let page = tokio::time::timeout(
                self.call_timeout,
                self.resolver.list_bindings(
                    self.profile.provider(),
                    Some(self.profile.capability()),
                    after,
                    PAGE_LIMIT,
                ),
            )
            .await
            .map_err(|_| FeedError::Timeout)?
            .map_err(FeedError::Resolve)?;
            for item in &page.items {
                match self.profile.plan(item, &self.env) {
                    Ok(list) => {
                        for d in list {
                            // A profile bug must never route one binding's
                            // connection under another identity.
                            if d.key.platform != platform
                                || d.key.community_id != item.community_id
                                || d.key.connection_id != item.connection_id
                            {
                                tracing::warn!(
                                    platform,
                                    community_id = item.community_id,
                                    connection_id = item.connection_id,
                                    "profile produced a connection that does not match its binding"
                                );
                                malformed += 1;
                                continue;
                            }
                            planned.push(d);
                        }
                    }
                    Err(e) => {
                        tracing::warn!(
                            platform,
                            community_id = item.community_id,
                            connection_id = item.connection_id,
                            error = %e,
                            "skipping malformed binding"
                        );
                        malformed += 1;
                    }
                }
            }
            match page.next_after {
                Some(next) if next <= after => return Err(FeedError::StuckCursor),
                Some(next) => after = next,
                None => break,
            }
        }
        Ok((planned, malformed))
    }
}

impl<R: ConnectionResolve, P: ConnectorProfile> BindingFeed for HubBindingFeed<R, P> {
    async fn desired(&self, platform: &'static str) -> Result<Vec<DesiredConnection>, FeedError> {
        if platform != self.profile.platform() {
            return Err(FeedError::UnknownPlatform(platform.to_owned()));
        }
        let (planned, malformed) = self.collect().await?;
        let (out, report) = dedup(planned);
        if let Some(m) = &self.metrics {
            m.skipped(platform, SkipReason::Malformed, u64::from(malformed));
            m.skipped(platform, SkipReason::Duplicate, u64::from(report.duplicate));
            m.skipped(platform, SkipReason::Conflict, u64::from(report.conflict));
        }
        Ok(out)
    }
}

type BoxedDesired<'a> =
    Pin<Box<dyn Future<Output = Result<Vec<DesiredConnection>, FeedError>> + Send + 'a>>;

/// Object-safe view of a [`BindingFeed`] so one runtime can serve several
/// platforms, each with its own profile and feed.
pub trait DynFeed: Send + Sync {
    /// Boxed [`BindingFeed::desired`].
    fn desired_boxed(&self, platform: &'static str) -> BoxedDesired<'_>;
}

impl<T: BindingFeed> DynFeed for T {
    fn desired_boxed(&self, platform: &'static str) -> BoxedDesired<'_> {
        Box::pin(self.desired(platform))
    }
}

/// Routes `desired(platform)` to the feed registered for that platform.
#[derive(Default)]
pub struct FeedSet {
    feeds: BTreeMap<&'static str, Arc<dyn DynFeed>>,
}

impl FeedSet {
    /// An empty set; every platform is unknown until registered.
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Registers the feed serving `platform` (replaces a previous one).
    #[must_use]
    pub fn with(mut self, platform: &'static str, feed: Arc<dyn DynFeed>) -> Self {
        self.feeds.insert(platform, feed);
        self
    }
}

impl BindingFeed for FeedSet {
    async fn desired(&self, platform: &'static str) -> Result<Vec<DesiredConnection>, FeedError> {
        match self.feeds.get(platform) {
            Some(f) => f.desired_boxed(platform).await,
            None => Err(FeedError::UnknownPlatform(platform.to_owned())),
        }
    }
}

#[cfg(test)]
mod tests;
