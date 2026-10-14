//! The dynamic-connections supervisor: polls hub-api for the set of Twitch chat
//! bindings the data plane should run, diffs it against the running bots, and
//! spawns / updates / stops exactly one [`super::bot::run_bot`] task per bot
//! account.
//!
//! ```text
//!   hub-api list (cursor-paged, no tokens)
//!        |
//!        v   group_bindings(): validate, scope, drop refused
//!   BTreeMap<bot_login, BotSpec{ members[channel, scope, ids] }>
//!        |
//!        v   reconcile(): diff against running bots
//!   spawn new bot --- watch::send(spec) on membership change --- stop vanished bot
//!        |
//!        v   per bot: resolve each member (hub-api refreshes) -> ONE IRC socket,
//!             JOIN #a,#b,#c -> route PRIVMSG by channel -> publish under scope
//! ```
//!
//! **Safety properties**
//! - A failed or partial list never tears anything down: [`list_all`] returns
//!   the whole listing or an error, and an error leaves the running bots
//!   untouched (a hub-api outage must not silence every channel).
//! - A binding hub-api refused (409/404) is remembered with its `updatedAt` and
//!   not re-added until it changes or disappears from the listing.
//! - Bindings are validated against an allow-list before any value reaches an
//!   IRC line or a spine stream key; an unscoped binding is skipped and counted
//!   (`bindings_skipped_total{reason="no_scope"}`), never published under a
//!   guessed scope.
//!
//! See `ScopeMode` for how a binding maps to a spine `Scope`.

use std::collections::{BTreeMap, HashMap};
use std::sync::Arc;
use std::time::{Duration, Instant};

use penguin_spine::{Scope, SpineMetrics};
use tokio::sync::{mpsc, oneshot, watch};
use tokio::task::JoinHandle;

use super::bot::{self, BotEvent, BotSpec, IrcSettings, Member, PublishCtx};
use super::client::{list_all, ClientError, ConnectionsApi};
use super::dto::{DataPlaneBinding, CAPABILITY_CHAT_INGEST, PROVIDER_TWITCH};
use super::metrics::ConnectionsMetrics;
use crate::publish::EventAppender;

/// How long a stopping bot is given to exit before its task is aborted.
const STOP_GRACE: Duration = Duration::from_secs(5);

/// Longest tenant slug / community name accepted as a spine scope segment.
const MAX_SEGMENT_LEN: usize = 64;

/// How a hub-api binding maps to the spine `Scope` its events are minted under.
///
/// The spine stream key is `waddles:t:{tenant slug}:c:{community name|_tenant}
/// :src:...`, but hub-api's `DataPlaneBindingDTO` carries only *numeric*
/// `tenantId`/`communityId`. Guessing a slug from an id would publish a tenant's
/// chat into the wrong stream, so the mapping is explicit:
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ScopeMode {
    /// Use the binding's own `tenantSlug`/`communityName` (additive, optional
    /// fields on the DTO). A binding without them is **skipped and counted**
    /// (`no_scope`) -- fail closed. This is the multi-tenant-correct mode and
    /// the default; it needs hub-api to emit those two fields.
    Binding,
    /// Publish every binding under the runner's fixed scope
    /// (`RUNNER_TENANT_SLUG`/`RUNNER_COMMUNITY`), exactly as the static env path
    /// does. Correct only for a single-tenant deployment (alpha); an explicit
    /// operator opt-in (`INGEST_DYNAMIC_SCOPE_MODE=runner`), never a fallback.
    Runner(Scope),
}

impl ScopeMode {
    /// Parses the `INGEST_DYNAMIC_SCOPE_MODE` value (`binding` | `runner`).
    pub fn parse(mode: &str, runner_scope: Scope) -> Result<Self, String> {
        match mode.trim().to_ascii_lowercase().as_str() {
            "binding" => Ok(Self::Binding),
            "runner" => Ok(Self::Runner(runner_scope)),
            other => Err(format!(
                "INGEST_DYNAMIC_SCOPE_MODE must be `binding` or `runner`, got {other:?}"
            )),
        }
    }

    /// The scope `binding`'s events publish under, or `None` if it cannot be
    /// scoped safely.
    fn scope_for(&self, binding: &DataPlaneBinding) -> Option<Scope> {
        match self {
            Self::Runner(scope) => Some(scope.clone()),
            Self::Binding => {
                let tenant = binding.tenant_slug.as_deref().filter(|s| valid_segment(s))?;
                // `communityId == 0` is the tenant-wide sentinel; any real
                // community must name itself, or the event would silently be
                // published tenant-wide.
                let community = if binding.community_id == 0 {
                    None
                } else {
                    Some(
                        binding
                            .community_name
                            .as_deref()
                            .filter(|s| valid_segment(s))?
                            .to_string(),
                    )
                };
                Some(Scope::new(tenant, community))
            }
        }
    }
}

/// A scope segment may carry only `[A-Za-z0-9_-]`, 1..=64 chars -- never the
/// `:` Valkey key separator, whitespace, or control characters.
fn valid_segment(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= MAX_SEGMENT_LEN
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

/// Validates, scopes and groups `listed` into one [`BotSpec`] per bot login.
///
/// Pure apart from the skip counters. Skipped (never an error): a binding for
/// another provider or lacking `chat_ingest` (`wrong_provider`/
/// `missing_capability` -- the server filters, this verifies), one with
/// malformed `bot_login`/`channel_login` metadata (`invalid_metadata`), one that
/// cannot be scoped (`no_scope`), a repeated grant id, and any binding present
/// in `rejected` with an unchanged `updatedAt`.
pub fn group_bindings(
    listed: &[DataPlaneBinding],
    mode: &ScopeMode,
    rejected: &HashMap<i64, Option<String>>,
    metrics: &ConnectionsMetrics,
) -> BTreeMap<String, BotSpec> {
    let skip = |reason: &str| {
        metrics.bindings_skipped_total.with_label_values(&[reason]).inc();
    };
    let mut specs: BTreeMap<String, BotSpec> = BTreeMap::new();
    let mut seen_grants: Vec<i64> = Vec::new();

    for binding in listed {
        if seen_grants.contains(&binding.grant_id) {
            continue;
        }
        seen_grants.push(binding.grant_id);

        if rejected
            .get(&binding.grant_id)
            .is_some_and(|stamp| *stamp == binding.updated_at)
        {
            continue; // refused by hub-api and unchanged since -- do not retry
        }
        if binding.provider != PROVIDER_TWITCH {
            skip("wrong_provider");
            continue;
        }
        if !binding.capabilities.iter().any(|c| c == CAPABILITY_CHAT_INGEST) {
            skip("missing_capability");
            continue;
        }
        let (Some(bot_login), Some(channel)) = (binding.bot_login(), binding.channel_login())
        else {
            tracing::warn!(
                grant_id = binding.grant_id,
                connection_id = binding.connection_id,
                "binding has missing/invalid bot_login or channel_login metadata; skipped"
            );
            skip("invalid_metadata");
            continue;
        };
        let Some(scope) = mode.scope_for(binding) else {
            tracing::error!(
                grant_id = binding.grant_id,
                connection_id = binding.connection_id,
                community_id = binding.community_id,
                "binding cannot be scoped (hub-api sent no usable tenantSlug/communityName and \
                 INGEST_DYNAMIC_SCOPE_MODE=binding); skipped, NOT published under a guessed scope"
            );
            skip("no_scope");
            continue;
        };
        specs
            .entry(bot_login.clone())
            .or_insert_with(|| BotSpec {
                bot_login,
                members: Vec::new(),
            })
            .members
            .push(Member {
                grant_id: binding.grant_id,
                connection_id: binding.connection_id,
                community_id: binding.community_id,
                tenant_id: binding.tenant_id,
                channel,
                scope,
                updated_at: binding.updated_at.clone(),
            });
    }
    for spec in specs.values_mut() {
        spec.members.sort_by_key(|m| m.grant_id);
    }
    specs
}

/// Operational settings for the supervisor.
#[derive(Debug, Clone)]
pub struct SupervisorSettings {
    /// Interval between reconcile passes.
    pub poll_interval: Duration,
    /// Page size for the cursor-paged list (1..=1000).
    pub page_size: u32,
    /// IRC endpoint shared by every bot.
    pub irc: IrcSettings,
    /// Cap on each bot's reconnect backoff.
    pub reconnect_backoff_cap: Duration,
}

/// A running bot and the channel to steer it.
struct BotHandle {
    spec_tx: watch::Sender<BotSpec>,
    shutdown_tx: Option<oneshot::Sender<()>>,
    join: JoinHandle<()>,
}

/// What one reconcile pass did, for logs and tests.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct ReconcileReport {
    /// Bindings returned by the list.
    pub listed: usize,
    /// Bots newly spawned.
    pub spawned: usize,
    /// Running bots whose membership changed (their socket reconnects).
    pub updated: usize,
    /// Bots stopped because no binding remained.
    pub stopped: usize,
}

/// Why a reconcile pass did not complete.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum ReconcileError {
    /// The hub-api listing failed; no running bot was touched.
    #[error("hub-api list failed: {0}")]
    List(ClientError),
}

/// Drives the dynamic Twitch receivers. See the module docs.
pub struct Supervisor<C, A, M> {
    api: Arc<C>,
    publish: Arc<PublishCtx<A, M>>,
    metrics: Arc<ConnectionsMetrics>,
    scope_mode: ScopeMode,
    settings: SupervisorSettings,
    bots: HashMap<String, BotHandle>,
    rejected: HashMap<i64, Option<String>>,
    events_tx: mpsc::UnboundedSender<BotEvent>,
    events_rx: mpsc::UnboundedReceiver<BotEvent>,
}

impl<C, A, M> Supervisor<C, A, M>
where
    C: ConnectionsApi,
    A: EventAppender + 'static,
    M: SpineMetrics + 'static,
{
    /// Builds an idle supervisor (no bot runs until the first reconcile).
    pub fn new(
        api: Arc<C>,
        publish: Arc<PublishCtx<A, M>>,
        metrics: Arc<ConnectionsMetrics>,
        scope_mode: ScopeMode,
        settings: SupervisorSettings,
    ) -> Self {
        let (events_tx, events_rx) = mpsc::unbounded_channel();
        Self {
            api,
            publish,
            metrics,
            scope_mode,
            settings,
            bots: HashMap::new(),
            rejected: HashMap::new(),
            events_tx,
            events_rx,
        }
    }

    /// Number of bot tasks currently tracked (tests and logs).
    #[must_use]
    pub fn running_bots(&self) -> usize {
        self.bots.len()
    }

    /// Applies bot -> supervisor events: refused members are remembered and
    /// removed (silently, without waking the bot -- it has already dropped
    /// them) from the bot's stored desired spec.
    fn drain_events(&mut self) {
        while let Ok(event) = self.events_rx.try_recv() {
            match event {
                BotEvent::MemberDropped {
                    grant_id,
                    updated_at,
                } => {
                    self.rejected.insert(grant_id, updated_at);
                    for handle in self.bots.values() {
                        handle.spec_tx.send_if_modified(|spec| {
                            spec.members.retain(|m| m.grant_id != grant_id);
                            false // no wake-up: the bot already dropped it
                        });
                    }
                }
            }
        }
    }

    /// One reconcile pass: list, group, then spawn / update / stop bots.
    #[tracing::instrument(name = "hubconn.reconcile", skip_all)]
    pub async fn reconcile_once(&mut self) -> Result<ReconcileReport, ReconcileError> {
        let started = Instant::now();
        self.drain_events();

        // Reap bots whose task ended on its own (all members refused, or a
        // panic) so a still-desired bot is respawned below.
        self.bots.retain(|_, handle| {
            let alive = !handle.join.is_finished();
            if !alive {
                self.metrics.bots_running.dec();
            }
            alive
        });

        let listed = match list_all(
            self.api.as_ref(),
            PROVIDER_TWITCH,
            CAPABILITY_CHAT_INGEST,
            self.settings.page_size,
        )
        .await
        {
            Ok(listed) => listed,
            Err(err) => {
                self.metrics.reconcile_total.with_label_values(&["list_failed"]).inc();
                self.metrics
                    .reconcile_duration_seconds
                    .with_label_values(&["list_failed"])
                    .observe(started.elapsed().as_secs_f64());
                return Err(ReconcileError::List(err));
            }
        };
        self.metrics.bindings_listed.set(i64::try_from(listed.len()).unwrap_or(i64::MAX));

        // A refusal sticks only while the same binding version is listed.
        self.rejected.retain(|grant_id, stamp| {
            listed
                .iter()
                .any(|b| b.grant_id == *grant_id && b.updated_at == *stamp)
        });

        let desired = group_bindings(&listed, &self.scope_mode, &self.rejected, &self.metrics);
        let mut report = ReconcileReport {
            listed: listed.len(),
            ..ReconcileReport::default()
        };

        // Stop bots with no remaining binding.
        let vanished: Vec<String> = self
            .bots
            .keys()
            .filter(|login| !desired.contains_key(*login))
            .cloned()
            .collect();
        for login in vanished {
            if let Some(handle) = self.bots.remove(&login) {
                tracing::info!(bot = %bot::bot_ref(&login), "no bindings remain; stopping dynamic bot");
                stop_bot(handle).await;
                self.metrics.bots_running.dec();
                report.stopped += 1;
            }
        }

        // Spawn new bots / push membership changes to running ones.
        for (login, spec) in desired {
            if let Some(handle) = self.bots.get(&login) {
                let changed = handle.spec_tx.send_if_modified(|current| {
                    if *current == spec {
                        false
                    } else {
                        *current = spec.clone();
                        true
                    }
                });
                if changed {
                    tracing::info!(
                        bot = %bot::bot_ref(&login),
                        channels = spec.members.len(),
                        "membership changed; bot will reconnect"
                    );
                    report.updated += 1;
                }
                continue;
            }
            tracing::info!(
                bot = %bot::bot_ref(&login),
                channels = spec.members.len(),
                "starting dynamic twitch bot"
            );
            let handle = self.spawn_bot(spec);
            self.bots.insert(login, handle);
            self.metrics.bots_running.inc();
            report.spawned += 1;
        }

        self.metrics.reconcile_total.with_label_values(&["ok"]).inc();
        self.metrics
            .reconcile_duration_seconds
            .with_label_values(&["ok"])
            .observe(started.elapsed().as_secs_f64());
        tracing::debug!(
            listed = report.listed,
            spawned = report.spawned,
            updated = report.updated,
            stopped = report.stopped,
            bots = self.bots.len(),
            "reconcile complete"
        );
        Ok(report)
    }

    fn spawn_bot(&self, spec: BotSpec) -> BotHandle {
        let (spec_tx, spec_rx) = watch::channel(spec);
        let (shutdown_tx, shutdown_rx) = oneshot::channel();
        let join = tokio::spawn(bot::run_bot(
            Arc::clone(&self.api),
            Arc::clone(&self.publish),
            Arc::clone(&self.metrics),
            self.settings.irc.clone(),
            self.events_tx.clone(),
            spec_rx,
            shutdown_rx,
            self.settings.reconnect_backoff_cap,
        ));
        BotHandle {
            spec_tx,
            shutdown_tx: Some(shutdown_tx),
            join,
        }
    }

    /// Stops every running bot (graceful, bounded).
    pub async fn stop_all(&mut self) {
        let handles: Vec<BotHandle> = self.bots.drain().map(|(_, h)| h).collect();
        for handle in handles {
            stop_bot(handle).await;
            self.metrics.bots_running.dec();
        }
    }

    /// Reconciles immediately, then every `poll_interval`, until `shutdown`
    /// fires; then stops every bot. A failed pass is logged and retried sooner
    /// than the poll interval (exponential, capped at the interval) but never
    /// touches running bots.
    pub async fn run(mut self, mut shutdown: oneshot::Receiver<()>) {
        let mut failures: u32 = 0;
        loop {
            let wait = match self.reconcile_once().await {
                Ok(_) => {
                    failures = 0;
                    self.settings.poll_interval
                }
                Err(err) => {
                    failures = failures.saturating_add(1);
                    let retry = Duration::from_secs(
                        1u64.checked_shl(failures.min(10)).unwrap_or(u64::MAX).saturating_mul(2),
                    )
                    .min(self.settings.poll_interval);
                    match &err {
                        ReconcileError::List(ClientError::Permanent { .. }) => {
                            tracing::error!(error = %err, ?retry, "hub-api rejected the connection list request; running bots untouched, retrying");
                        }
                        ReconcileError::List(_) => {
                            tracing::warn!(error = %err, ?retry, "hub-api connection list failed; running bots untouched, retrying");
                        }
                    }
                    retry
                }
            };
            tokio::select! {
                _ = &mut shutdown => break,
                () = tokio::time::sleep(wait) => {}
            }
        }
        tracing::info!("dynamic connections supervisor shutting down");
        self.stop_all().await;
    }
}

/// Signals a bot to stop and waits (bounded) for it; aborts it past
/// [`STOP_GRACE`].
async fn stop_bot(mut handle: BotHandle) {
    if let Some(tx) = handle.shutdown_tx.take() {
        let _ = tx.send(());
    }
    if tokio::time::timeout(STOP_GRACE, &mut handle.join).await.is_err() {
        tracing::warn!("dynamic bot did not stop within the grace period; aborting its task");
        handle.join.abort();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn metrics() -> ConnectionsMetrics {
        ConnectionsMetrics::register(&prometheus::Registry::new()).unwrap()
    }

    fn binding(
        grant_id: i64,
        bot: &str,
        channel: &str,
        community_id: i64,
        extra: serde_json::Value,
    ) -> DataPlaneBinding {
        let mut value = json!({
            "grantId": grant_id, "connectionId": grant_id * 10, "communityId": community_id,
            "tenantId": 5, "provider": "twitch", "resourceType": "channel",
            "resourceId": "1", "capabilities": ["chat_ingest"],
            "updatedAt": "v1",
            "metadata": {"bot_login": bot, "channel_login": channel}
        });
        if let (Some(obj), Some(more)) = (value.as_object_mut(), extra.as_object()) {
            for (k, v) in more {
                obj.insert(k.clone(), v.clone());
            }
        }
        serde_json::from_value(value).unwrap()
    }

    fn runner() -> ScopeMode {
        ScopeMode::Runner(Scope::new("global", None))
    }

    #[test]
    fn groups_all_channels_of_one_bot_into_a_single_spec() {
        let m = metrics();
        let listed = vec![
            binding(3, "WaddleBots", "chan_c", 1, json!({})),
            binding(1, "waddlebots", "chan_a", 1, json!({})),
            binding(2, "otherbot", "chan_b", 2, json!({})),
            binding(4, "waddlebots", "chan_d", 3, json!({})),
        ];
        let specs = group_bindings(&listed, &runner(), &HashMap::new(), &m);
        assert_eq!(specs.len(), 2, "two distinct bot accounts -> two sockets");
        let wb = &specs["waddlebots"];
        assert_eq!(
            wb.members.iter().map(|m| (m.grant_id, m.channel.as_str())).collect::<Vec<_>>(),
            vec![(1, "chan_a"), (3, "chan_c"), (4, "chan_d")],
            "login case-insensitive, members sorted by grant id"
        );
        assert_eq!(specs["otherbot"].members.len(), 1);
    }

    #[test]
    fn skips_wrong_provider_missing_capability_and_invalid_metadata_with_counters() {
        let m = metrics();
        let listed = vec![
            binding(1, "bot", "chan", 1, json!({"provider": "discord"})),
            binding(2, "bot", "chan", 1, json!({"capabilities": ["action_relay"]})),
            binding(3, "bot", "bad chan\r\nQUIT", 1, json!({})),
            binding(4, "bot,other", "chan", 1, json!({})),
            binding(5, "bot", "good", 1, json!({})),
        ];
        let specs = group_bindings(&listed, &runner(), &HashMap::new(), &m);
        assert_eq!(specs["bot"].members.len(), 1);
        assert_eq!(specs["bot"].members[0].channel, "good");
        let count = |reason: &str| m.bindings_skipped_total.with_label_values(&[reason]).get();
        assert_eq!(count("wrong_provider"), 1);
        assert_eq!(count("missing_capability"), 1);
        assert_eq!(count("invalid_metadata"), 2);
    }

    #[test]
    fn binding_mode_requires_a_slug_and_never_guesses_a_scope() {
        let m = metrics();
        let mode = ScopeMode::Binding;
        let listed = vec![
            // No slug at all -> skipped (fail closed).
            binding(1, "bot", "a_chan", 0, json!({})),
            // Slug present, tenant-wide sentinel community -> tenant scope.
            binding(2, "bot", "b_chan", 0, json!({"tenantSlug": "acme"})),
            // Real community but no community name -> skipped, not tenant-wide.
            binding(3, "bot", "c_chan", 9, json!({"tenantSlug": "acme"})),
            // Fully scoped.
            binding(4, "bot", "d_chan", 9, json!({"tenantSlug": "acme", "communityName": "main"})),
            // Hostile segment (key separator) -> skipped.
            binding(5, "bot", "e_chan", 9, json!({"tenantSlug": "a:b", "communityName": "main"})),
        ];
        let specs = group_bindings(&listed, &mode, &HashMap::new(), &m);
        let members = &specs["bot"].members;
        assert_eq!(members.len(), 2);
        assert_eq!(members[0].scope, Scope::new("acme", None));
        assert_eq!(members[1].scope, Scope::new("acme", Some("main".to_string())));
        assert_eq!(m.bindings_skipped_total.with_label_values(&["no_scope"]).get(), 3);
    }

    #[test]
    fn runner_mode_uses_the_fixed_scope_for_every_binding() {
        let m = metrics();
        let listed = vec![
            binding(1, "bot", "a_chan", 0, json!({"tenantSlug": "ignored"})),
            binding(2, "bot", "b_chan", 7, json!({})),
        ];
        let specs = group_bindings(&listed, &runner(), &HashMap::new(), &m);
        assert!(specs["bot"].members.iter().all(|m| m.scope == Scope::new("global", None)));
    }

    #[test]
    fn rejected_bindings_are_excluded_until_updated_at_changes() {
        let m = metrics();
        let listed = vec![binding(1, "bot", "a_chan", 1, json!({})), binding(2, "bot", "b_chan", 1, json!({}))];
        let mut rejected = HashMap::new();
        rejected.insert(1, Some("v1".to_string()));
        let specs = group_bindings(&listed, &runner(), &rejected, &m);
        assert_eq!(specs["bot"].members.len(), 1);
        assert_eq!(specs["bot"].members[0].grant_id, 2);

        // The binding was edited (new updatedAt): eligible again.
        let edited = vec![binding(1, "bot", "a_chan", 1, json!({"updatedAt": "v2"}))];
        let specs = group_bindings(&edited, &runner(), &rejected, &m);
        assert_eq!(specs["bot"].members.len(), 1);
    }

    #[test]
    fn duplicate_grant_ids_are_collapsed() {
        let m = metrics();
        let listed = vec![binding(1, "bot", "a_chan", 1, json!({})), binding(1, "bot", "a_chan", 1, json!({}))];
        let specs = group_bindings(&listed, &runner(), &HashMap::new(), &m);
        assert_eq!(specs["bot"].members.len(), 1);
    }

    #[test]
    fn scope_mode_parses_and_rejects_unknown_values() {
        let scope = Scope::new("global", None);
        assert_eq!(ScopeMode::parse("binding", scope.clone()), Ok(ScopeMode::Binding));
        assert_eq!(ScopeMode::parse(" Runner ", scope.clone()), Ok(ScopeMode::Runner(scope.clone())));
        assert!(ScopeMode::parse("auto", scope).is_err());
    }

    #[test]
    fn segment_validation_is_a_strict_allow_list() {
        assert!(valid_segment("acme-1_x"));
        for bad in ["", "a:b", "a b", "a\n", &"x".repeat(65)] {
            assert!(!valid_segment(bad), "{bad:?}");
        }
    }
}
