//! The DB-driven source-binding supervisor: polls `app_source_bindings`
//! (`bundle_active_set::read_source_bindings`, scoped to currently ACTIVE
//! apps) and maintains exactly one dedicated `penguin_spine::GroupReader`
//! consumer task per `(app_id, platform, source_id)` binding, spawning one
//! for every newly-bound source and gracefully stopping one for every
//! binding that is removed or whose app is deactivated.
//!
//! Replaces `crate::lib::try_start_process_loop`'s single
//! `PROCESS_INGEST_PLATFORM`/`PROCESS_INGEST_SOURCE_ID`-configured consumer
//! as the primary source-consumption path; that env-driven path remains
//! only as the kill-switch/missing-config fallback (see
//! `crate::license::DISABLE_DB_BUNDLE_CONFIG_FLAG`'s doc). Enabled/disabled
//! per-tick by the same kill-switch gate as `crate::bundle_loader`
//! (`crate::license::FeatureGate`, already the negated "is the DB path
//! enabled" answer) -- while disabled, [`run_tick`] stops every running
//! consumer rather than merely refusing to spawn new ones, so a kill-switch
//! flip to ON during a live rollout actually falls back to the env path
//! rather than leaving stale DB-driven consumers running alongside it.
//!
//! Bundle load/unload onto the executor remains `crate::bundle_loader`'s
//! job, not this module's: each spawned consumer's [`crate::spine::
//! ProcessDeps::digest`] is left empty (see that field's own doc for why an
//! empty digest is a safe, already-handled "not loaded yet" state) and
//! relies entirely on `crate::bundle_loader::run` -- polling the same
//! Postgres reader connection -- to actually `Load`/`Unload` the bundle
//! this app_id's invokes need onto the shared executor connection.
//!
//! `penguin_spine::GroupReader::connect` never creates the Valkey consumer
//! group itself (hub-api provisions it, `XGROUP CREATE`, when it grants the
//! binding) -- a binding whose group hasn't been provisioned yet answers
//! `XREADGROUP` with a `NOGROUP` Redis error on every read attempt.
//! [`run_binding_consumer`] tolerates this: log at `WARN`, back off, retry
//! -- never treated as a reason to stop supervising that binding entirely.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::time::Duration;

use bundle_active_set::{SourceBinding, WatermarkTracker};
use penguin_spine::{Grant, Scope, SpineClient, SpineConfig, SpineError, SpineMetrics};
use sea_orm::DatabaseConnection;
use tokio::sync::oneshot;
use tokio::task::JoinHandle;

use crate::hop::KeyRing;
use crate::host_api::ConnectionRegistry;
use crate::license::FeatureGate;
use crate::spine::{LoadState, ProcessDeps};
use crate::telemetry::SourceBindingSupervisorMetrics;

/// Identifies one running consumer task: `(app_id, platform, source_id)`,
/// exactly the columns of `app_source_bindings`'s own composite key (minus
/// tenant/community, both fixed for one supervisor instance).
type BindingKey = (String, String, String);

fn binding_key(b: &SourceBinding) -> BindingKey {
    (b.app_id.clone(), b.platform.clone(), b.source_id.clone())
}

/// How long a per-binding consumer waits after `crate::spine::run` exits
/// (NOGROUP, a dropped Valkey connection, ...) before reconnecting --
/// applies uniformly regardless of the failure reason; see this module's
/// doc for the NOGROUP case specifically.
const CONSUMER_RETRY_BACKOFF: Duration = Duration::from_secs(5);

/// True when `err` is Valkey's `NOGROUP` reply -- the bound stream's
/// consumer group hasn't been provisioned yet (hub-api owns `XGROUP
/// CREATE` when it grants the binding). Matches on
/// [`redis::RedisError::code`] (the raw server-reported error code the
/// `redis` crate parses once from the `-NOGROUP ...` reply line) rather
/// than a substring match on the full `Display` text, so a wording change
/// in the trailing human-readable detail can never break this check.
fn is_nogroup_error(err: &SpineError) -> bool {
    matches!(err, SpineError::Redis(e) if e.code() == Some("NOGROUP"))
}

/// Builds the [`Grant`] for one binding under `tenant`/`community` --
/// pulled out of [`run_binding_consumer`] as a pure function purely so the
/// tenant-isolation fix (`crate::lib::resolve_scope`'s resolved slug/name
/// actually reaching the stream key `penguin_spine::GroupReader` reads) is
/// directly unit-testable without a live Valkey connection.
fn binding_grant(
    tenant: &str,
    community: &Option<String>,
    platform: &str,
    source_id: &str,
) -> Grant {
    let scope = Scope::new(tenant.to_string(), community.clone());
    Grant {
        stream: scope.source_stream(platform, source_id),
        platform: platform.to_string(),
        source_id: source_id.to_string(),
    }
}

/// Everything every per-binding consumer task needs that does NOT vary by
/// binding -- built once by `crate::lib::try_start_db_bundle_loader` and
/// shared (via `Arc`) across every spawned [`run_binding_consumer`] task,
/// mirroring `crate::spine::ProcessDeps`'s own "bundle the dependencies"
/// rationale.
pub struct SupervisorDeps {
    pub spine_cfg: SpineConfig,
    pub key_ring: KeyRing,
    pub connections: Arc<ConnectionRegistry>,
    pub call_timeout_ms: u64,
    pub approved_targets: HashMap<String, String>,
    pub metrics: Arc<dyn SpineMetrics>,
    pub license: Arc<dyn FeatureGate>,
    /// Tenant slug / community name for `penguin_spine::Scope::
    /// source_stream` -- resolved from the numeric `BUNDLE_SCOPE_TENANT_ID`/
    /// `BUNDLE_SCOPE_COMMUNITY_ID` scope via `bundle_active_set::scope::
    /// resolve_scope` (`crate::lib::finish_supervisor_deps`), NEVER
    /// hardcoded: this module's only caller fails closed (does not build a
    /// `SupervisorDeps` at all, see `crate::lib::try_start_db_bundle_loader`)
    /// when resolution fails, rather than falling back to a guessed value.
    pub tenant: String,
    pub community: Option<String>,
    /// Connector spec SS0 condition 5: shared across every per-binding
    /// consumer task this supervisor runs, so a source's guest-fault history
    /// persists across `run_binding_consumer`'s reconnect loop and stays
    /// keyed independently per `(platform, source_id)` -- see
    /// `crate::spine::ProcessDeps::breaker`'s doc for the full rationale.
    pub breaker: Arc<circuit_breaker::CircuitBreaker>,
}

/// A running per-binding consumer: a shutdown signal plus the
/// [`JoinHandle`] this supervisor awaits when stopping it, so stopping a
/// consumer always waits for its task to actually exit (graceful drain --
/// the in-flight `drain_batch` call, if any, finishes acking/dead-lettering
/// before the task returns) rather than firing a shutdown signal and
/// moving on without confirmation.
pub struct RunningConsumer {
    shutdown: oneshot::Sender<()>,
    handle: JoinHandle<()>,
}

impl RunningConsumer {
    async fn stop(self) {
        // The receiver side may already be gone if the task exited on its
        // own between this supervisor's last tick and this stop() call
        // (e.g. it panicked) -- `send` returning `Err` in that case is not
        // itself an error worth logging; `self.handle.await` below is what
        // actually confirms the task is gone.
        let _ = self.shutdown.send(());
        if let Err(err) = self.handle.await {
            tracing::warn!(error = %err, "source-binding consumer task panicked while stopping");
        }
    }
}

/// Spawns a per-binding consumer task. Production wires
/// [`SpineConsumerSupervisor`] (a real `tokio::spawn` running
/// [`run_binding_consumer`] against a live Valkey/host-API connection);
/// tests wire a fake that records spawn calls without any live dependency,
/// mirroring `crate::bundle_loader::BundleSink`'s identical test-seam
/// rationale.
pub trait ConsumerSupervisor: Send + Sync {
    fn spawn(&self, app_id: &str, platform: &str, source_id: &str) -> RunningConsumer;
}

/// Production [`ConsumerSupervisor`]: spawns [`run_binding_consumer`] as a
/// real Tokio task against the shared [`SupervisorDeps`].
pub struct SpineConsumerSupervisor {
    pub deps: Arc<SupervisorDeps>,
}

impl ConsumerSupervisor for SpineConsumerSupervisor {
    fn spawn(&self, app_id: &str, platform: &str, source_id: &str) -> RunningConsumer {
        let (shutdown_tx, shutdown_rx) = oneshot::channel();
        let deps = Arc::clone(&self.deps);
        let handle = tokio::spawn(run_binding_consumer(
            app_id.to_string(),
            platform.to_string(),
            source_id.to_string(),
            deps,
            shutdown_rx,
        ));
        RunningConsumer {
            shutdown: shutdown_tx,
            handle,
        }
    }
}

/// Sleeps for `dur`, or returns early (reporting `true`) if `shutdown`
/// resolves first -- lets [`run_binding_consumer`]'s retry backoff still
/// respond promptly to a supervisor-initiated stop instead of sleeping out
/// the full backoff window first.
async fn wait_or_shutdown(shutdown: &mut oneshot::Receiver<()>, dur: Duration) -> bool {
    tokio::select! {
        _ = shutdown => true,
        _ = tokio::time::sleep(dur) => false,
    }
}

/// Runs one binding's dedicated `penguin_spine::GroupReader` consumer
/// (consumer group = `app_id`, exactly one granted stream) until
/// `shutdown` resolves, reconnecting with [`CONSUMER_RETRY_BACKOFF`]
/// between attempts on any error -- most notably `NOGROUP` (the bound
/// stream's consumer group not provisioned yet), logged at `WARN` and
/// retried rather than treated as fatal (see this module's doc).
///
/// Each attempt builds a fresh [`ProcessDeps`] with an empty `digest`
/// (`crate::bundle_loader::run`, not this consumer, is what actually
/// `Load`s the bundle onto the executor -- see this module's doc) and a
/// fresh [`LoadState`] (irrelevant with an empty digest, but required by
/// `ProcessDeps`'s shape).
async fn run_binding_consumer(
    app_id: String,
    platform: String,
    source_id: String,
    deps: Arc<SupervisorDeps>,
    mut shutdown: oneshot::Receiver<()>,
) {
    loop {
        let grant = binding_grant(&deps.tenant, &deps.community, &platform, &source_id);

        let spine_client =
            match SpineClient::connect(deps.spine_cfg.clone(), deps.metrics.clone()).await {
                Ok(c) => c,
                Err(err) => {
                    tracing::warn!(
                        app_id = %app_id, platform = %platform, source_id = %source_id,
                        error = %err,
                        "source-binding consumer: spine client connect failed, retrying"
                    );
                    if wait_or_shutdown(&mut shutdown, CONSUMER_RETRY_BACKOFF).await {
                        return;
                    }
                    continue;
                }
            };

        let process_deps = ProcessDeps {
            app_id: app_id.clone(),
            digest: String::new(),
            version: "1".to_string(),
            component_key: String::new(),
            sidecar_key: String::new(),
            key_ring: deps.key_ring.clone(),
            connections: Arc::clone(&deps.connections),
            call_timeout_ms: deps.call_timeout_ms,
            load_state: Arc::new(LoadState::new()),
            approved_targets: deps.approved_targets.clone(),
            consumer_id: deps.spine_cfg.consumer_id.clone(),
            spine: spine_client,
            metrics: deps.metrics.clone(),
            license: Arc::clone(&deps.license),
            breaker: Arc::clone(&deps.breaker),
        };

        let (inner_tx, inner_rx) = oneshot::channel();
        let run_fut =
            crate::spine::run(deps.spine_cfg.clone(), vec![grant], process_deps, inner_rx);
        tokio::pin!(run_fut);

        tokio::select! {
            _ = &mut shutdown => {
                // Forward the stop signal into the live drain loop and
                // wait for it to actually finish (graceful drain: any
                // in-flight `drain_batch` call finishes acking/dead-
                // lettering its current batch) before this task exits.
                let _ = inner_tx.send(());
                let _ = run_fut.await;
                return;
            }
            result = &mut run_fut => {
                match result {
                    // `crate::spine::run` only returns `Ok(())` when its
                    // own shutdown receiver resolves, which only happens
                    // via the branch above -- unreachable in practice, but
                    // handled the same way (return) rather than looping.
                    Ok(()) => return,
                    Err(err) if is_nogroup_error(&err) => {
                        tracing::warn!(
                            app_id = %app_id, platform = %platform, source_id = %source_id,
                            "source-binding consumer: consumer group not yet provisioned (NOGROUP), retrying"
                        );
                    }
                    Err(err) => {
                        tracing::error!(
                            app_id = %app_id, platform = %platform, source_id = %source_id,
                            error = %err,
                            "source-binding consumer exited, retrying"
                        );
                    }
                }
                if wait_or_shutdown(&mut shutdown, CONSUMER_RETRY_BACKOFF).await {
                    return;
                }
            }
        }
    }
}

/// Reconciles `running` against `target`: stops every running consumer
/// whose binding is no longer in `target` (removed binding, or its app
/// deactivated -- `bundle_active_set::read_source_bindings` already scopes
/// to currently ACTIVE apps, so a deactivation surfaces here as simply
/// "missing from `target`"), then spawns a consumer for every binding in
/// `target` not already running. Stops before spawns so a binding that
/// moves scope in the same tick (unlikely given the composite key, but not
/// prevented at the type level) never briefly runs two consumers.
pub async fn reconcile(
    running: &mut HashMap<BindingKey, RunningConsumer>,
    target: &[SourceBinding],
    spawner: &dyn ConsumerSupervisor,
    metrics: &SourceBindingSupervisorMetrics,
) {
    let target_keys: HashSet<BindingKey> = target.iter().map(binding_key).collect();

    let to_stop: Vec<BindingKey> = running
        .keys()
        .filter(|k| !target_keys.contains(*k))
        .cloned()
        .collect();
    for key in to_stop {
        if let Some(consumer) = running.remove(&key) {
            tracing::info!(
                app_id = %key.0, platform = %key.1, source_id = %key.2,
                "source-binding consumer: binding removed or app deactivated, stopping consumer"
            );
            consumer.stop().await;
            metrics
                .consumer_transitions_total
                .with_label_values(&["stop"])
                .inc();
            metrics.active_consumers.dec();
        }
    }

    for binding in target {
        let key = binding_key(binding);
        if let std::collections::hash_map::Entry::Vacant(entry) = running.entry(key) {
            tracing::info!(
                app_id = %binding.app_id, platform = %binding.platform, source_id = %binding.source_id,
                "source-binding consumer: new binding, spawning consumer"
            );
            let consumer = spawner.spawn(&binding.app_id, &binding.platform, &binding.source_id);
            entry.insert(consumer);
            metrics
                .consumer_transitions_total
                .with_label_values(&["spawn"])
                .inc();
            metrics.active_consumers.inc();
        }
    }
}

/// One poll tick, split out from [`run`] for direct testability against a
/// `MockDatabase`-backed connection, a fake [`FeatureGate`], and a fake
/// [`ConsumerSupervisor`] -- mirrors `crate::bundle_loader::run_tick`'s
/// identical split.
///
/// Order of short-circuits (cheapest first, matching
/// `crate::bundle_loader::run_tick`'s own documented rationale): kill-
/// switch on -- stop every running consumer, no DB call at all; watermark
/// read fails -- logged, retried next tick; watermark unchanged -- no
/// binding read; binding read fails -- logged, retried next tick,
/// `running` left untouched.
#[allow(clippy::too_many_arguments)]
pub async fn run_tick(
    db: &DatabaseConnection,
    tenant_id: i32,
    community_id: i32,
    gate: &dyn FeatureGate,
    tracker: &mut WatermarkTracker,
    running: &mut HashMap<BindingKey, RunningConsumer>,
    spawner: &dyn ConsumerSupervisor,
    metrics: &SourceBindingSupervisorMetrics,
) {
    if !gate.enabled().await {
        if !running.is_empty() {
            tracing::info!(
                "db-bundle-config disabled (kill-switch on / unavailable); stopping all \
                 DB-driven source-binding consumers, falling back to env selection"
            );
            reconcile(running, &[], spawner, metrics).await;
        }
        return;
    }

    let watermark = match bundle_active_set::read_watermark(db, tenant_id, community_id).await {
        Ok(w) => w,
        Err(err) => {
            tracing::warn!(error = %err, "source-binding supervisor: watermark read failed");
            return;
        }
    };
    if !tracker.observe(watermark) {
        return;
    }

    let bindings = match bundle_active_set::read_source_bindings(db, tenant_id, community_id).await
    {
        Ok(b) => b,
        Err(err) => {
            tracing::warn!(error = %err, "source-binding supervisor: binding read failed");
            return;
        }
    };

    reconcile(running, &bindings, spawner, metrics).await;
}

/// The live interval/shutdown loop `crate::lib::try_start_db_bundle_loader`
/// spawns alongside `crate::bundle_loader::run` (same `db`/`poll_interval`/
/// `gate`, a distinct concern). On shutdown, gracefully stops every
/// currently-running consumer before returning -- a pod termination must
/// never abandon in-flight consumer tasks.
#[allow(clippy::too_many_arguments)]
pub async fn run(
    db: DatabaseConnection,
    tenant_id: i32,
    community_id: i32,
    poll_interval: Duration,
    gate: Arc<dyn FeatureGate>,
    spawner: Arc<dyn ConsumerSupervisor>,
    metrics: SourceBindingSupervisorMetrics,
    mut shutdown: oneshot::Receiver<()>,
) {
    let mut tracker = WatermarkTracker::new();
    let mut running: HashMap<BindingKey, RunningConsumer> = HashMap::new();
    let mut interval = tokio::time::interval(poll_interval);
    interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    loop {
        tokio::select! {
            _ = &mut shutdown => {
                for (_, consumer) in running.drain() {
                    consumer.stop().await;
                }
                return;
            }
            _ = interval.tick() => {
                run_tick(
                    &db,
                    tenant_id,
                    community_id,
                    gate.as_ref(),
                    &mut tracker,
                    &mut running,
                    spawner.as_ref(),
                    &metrics,
                )
                .await;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::license::test_support::{FixedGate, ToggleGate};
    use sea_orm::{DatabaseBackend, MockDatabase};
    use std::sync::Mutex as StdMutex;

    /// Records every `spawn`/`stop` call it receives (as
    /// `"spawn:app:platform:source"`/`"stop:app:platform:source"`) without
    /// any live Valkey/host-API dependency -- the spawned task itself is a
    /// trivial `tokio::spawn` that just awaits its own shutdown signal and
    /// records the stop, proving [`reconcile`]/[`run_tick`]'s diff logic
    /// without needing `penguin_spine`/`crate::spine::run` at all.
    #[derive(Default)]
    struct RecordingSupervisor {
        calls: Arc<StdMutex<Vec<String>>>,
    }

    impl RecordingSupervisor {
        fn calls(&self) -> Vec<String> {
            self.calls.lock().unwrap().clone()
        }
    }

    impl ConsumerSupervisor for RecordingSupervisor {
        fn spawn(&self, app_id: &str, platform: &str, source_id: &str) -> RunningConsumer {
            let key = format!("{app_id}:{platform}:{source_id}");
            self.calls.lock().unwrap().push(format!("spawn:{key}"));
            let calls = Arc::clone(&self.calls);
            let (tx, rx) = oneshot::channel();
            let handle = tokio::spawn(async move {
                let _ = rx.await;
                calls.lock().unwrap().push(format!("stop:{key}"));
            });
            RunningConsumer {
                shutdown: tx,
                handle,
            }
        }
    }

    fn test_metrics() -> SourceBindingSupervisorMetrics {
        SourceBindingSupervisorMetrics {
            active_consumers: prometheus::IntGauge::new(
                "test_source_binding_consumers_active",
                "test",
            )
            .expect("valid metric definition"),
            consumer_transitions_total: prometheus::IntCounterVec::new(
                prometheus::Opts::new("test_source_binding_consumer_transitions_total", "test"),
                &["action"],
            )
            .expect("valid metric definition"),
        }
    }

    fn binding(app_id: &str, platform: &str, source_id: &str) -> SourceBinding {
        SourceBinding {
            app_id: app_id.to_string(),
            platform: platform.to_string(),
            source_id: source_id.to_string(),
        }
    }

    fn active_row(app_id: &str) -> bundle_active_set::entities::app_active_versions::Model {
        bundle_active_set::entities::app_active_versions::Model {
            app_id: app_id.to_string(),
            tenant_id: 1,
            community_id: 0,
            version_id: 10,
        }
    }

    fn binding_row(
        app_id: &str,
        platform: &str,
        source_id: &str,
    ) -> bundle_active_set::entities::app_source_bindings::Model {
        bundle_active_set::entities::app_source_bindings::Model {
            tenant_id: 1,
            community_id: 0,
            app_id: app_id.to_string(),
            platform: platform.to_string(),
            source_id: source_id.to_string(),
        }
    }

    /// The NOGROUP-detection regression test: a real `RedisError` carrying
    /// the server's own `"NOGROUP"` code (built via `redis::
    /// make_extension_error`, the same public constructor the `redis`
    /// crate's own parser uses internally for a `-NOGROUP ...` reply line)
    /// must be recognized regardless of the trailing detail text's exact
    /// wording.
    #[test]
    fn is_nogroup_error_matches_on_the_redis_error_code_not_message_wording() {
        let err = SpineError::Redis(redis::make_extension_error(
            "NOGROUP".to_string(),
            Some(
                "No such key 'waddles:t:acme:c:_tenant:src:twitch:tw-x:events' or consumer \
                 group 'waddles.bot.commands.default' in XREADGROUP with GROUP option"
                    .to_string(),
            ),
        ));
        assert!(is_nogroup_error(&err));

        // A totally different wording for the same code must still match --
        // proves this is a code check, not a disguised substring match.
        let err_different_wording = SpineError::Redis(redis::make_extension_error(
            "NOGROUP".to_string(),
            Some("some completely different detail text".to_string()),
        ));
        assert!(is_nogroup_error(&err_different_wording));
    }

    #[test]
    fn is_nogroup_error_rejects_a_different_redis_error_code() {
        let err = SpineError::Redis(redis::make_extension_error(
            "WRONGTYPE".to_string(),
            Some("Operation against a key holding the wrong kind of value".to_string()),
        ));
        assert!(!is_nogroup_error(&err));
    }

    #[test]
    fn is_nogroup_error_rejects_a_non_redis_spine_error() {
        let err = SpineError::Config("unrelated config error".to_string());
        assert!(!is_nogroup_error(&err));
    }

    /// Tenant-isolation regression test: the DB-resolved tenant slug/
    /// community name (`crate::lib::resolve_scope`, never a hardcoded
    /// scope) must be exactly what ends up in the Valkey stream key a
    /// consumer actually reads.
    #[test]
    fn binding_grant_uses_the_resolved_tenant_slug_and_community_name() {
        let grant = binding_grant("acme", &Some("main".to_string()), "twitch", "tw-channelA");
        assert_eq!(
            grant.stream,
            "waddles:t:acme:c:main:src:twitch:tw-channelA:events"
        );
        assert_eq!(grant.platform, "twitch");
        assert_eq!(grant.source_id, "tw-channelA");
    }

    #[test]
    fn binding_grant_renders_the_tenant_wide_segment_for_no_community() {
        let grant = binding_grant("acme", &None, "discord", "dg-x");
        assert_eq!(
            grant.stream,
            "waddles:t:acme:c:_tenant:src:discord:dg-x:events"
        );
    }

    #[tokio::test]
    async fn wait_or_shutdown_returns_true_when_shutdown_fires_first() {
        let (tx, mut rx) = oneshot::channel();
        tx.send(()).unwrap();
        assert!(wait_or_shutdown(&mut rx, Duration::from_secs(30)).await);
    }

    #[tokio::test]
    async fn wait_or_shutdown_returns_false_when_the_backoff_elapses_first() {
        let (_tx, mut rx) = oneshot::channel();
        assert!(!wait_or_shutdown(&mut rx, Duration::from_millis(1)).await);
    }

    /// `RunningConsumer::stop`'s panic-logging branch: a consumer task that
    /// panics instead of exiting cleanly must still let `stop()` return
    /// (never hang or propagate the panic to the caller) -- it only logs a
    /// warning.
    #[tokio::test]
    async fn running_consumer_stop_completes_even_if_the_task_panicked() {
        let (tx, rx) = oneshot::channel();
        let handle = tokio::spawn(async move {
            let _ = rx.await;
            panic!("simulated consumer task panic");
        });
        let consumer = RunningConsumer {
            shutdown: tx,
            handle,
        };
        consumer.stop().await;
    }

    #[tokio::test]
    async fn reconcile_spawns_a_consumer_for_a_new_binding() {
        let mut running = HashMap::new();
        let spawner = RecordingSupervisor::default();
        let metrics = test_metrics();
        reconcile(
            &mut running,
            &[binding("waddles.a", "twitch", "tw-channelA")],
            &spawner,
            &metrics,
        )
        .await;
        assert_eq!(running.len(), 1);
        assert_eq!(spawner.calls(), vec!["spawn:waddles.a:twitch:tw-channelA"]);
        assert_eq!(metrics.active_consumers.get(), 1);
        assert_eq!(
            metrics
                .consumer_transitions_total
                .with_label_values(&["spawn"])
                .get(),
            1
        );
    }

    #[tokio::test]
    async fn reconcile_is_a_noop_when_the_binding_is_already_running() {
        let mut running = HashMap::new();
        let spawner = RecordingSupervisor::default();
        let metrics = test_metrics();
        let target = [binding("waddles.a", "twitch", "tw-channelA")];
        reconcile(&mut running, &target, &spawner, &metrics).await;
        reconcile(&mut running, &target, &spawner, &metrics).await;
        assert_eq!(
            spawner.calls(),
            vec!["spawn:waddles.a:twitch:tw-channelA"],
            "an already-running binding must not be re-spawned"
        );
    }

    #[tokio::test]
    async fn reconcile_stops_a_consumer_for_a_removed_binding() {
        let mut running = HashMap::new();
        let spawner = RecordingSupervisor::default();
        let metrics = test_metrics();
        reconcile(
            &mut running,
            &[binding("waddles.a", "twitch", "tw-channelA")],
            &spawner,
            &metrics,
        )
        .await;
        reconcile(&mut running, &[], &spawner, &metrics).await;
        assert!(running.is_empty());
        assert_eq!(
            spawner.calls(),
            vec![
                "spawn:waddles.a:twitch:tw-channelA".to_string(),
                "stop:waddles.a:twitch:tw-channelA".to_string(),
            ]
        );
        assert_eq!(metrics.active_consumers.get(), 0);
        assert_eq!(
            metrics
                .consumer_transitions_total
                .with_label_values(&["stop"])
                .get(),
            1
        );
    }

    #[tokio::test]
    async fn reconcile_handles_a_mixed_add_and_remove_tick() {
        let mut running = HashMap::new();
        let spawner = RecordingSupervisor::default();
        let metrics = test_metrics();
        reconcile(
            &mut running,
            &[
                binding("waddles.unchanged", "twitch", "tw-a"),
                binding("waddles.removed", "discord", "dg-x"),
            ],
            &spawner,
            &metrics,
        )
        .await;
        reconcile(
            &mut running,
            &[
                binding("waddles.unchanged", "twitch", "tw-a"),
                binding("waddles.added", "discord", "dg-y"),
            ],
            &spawner,
            &metrics,
        )
        .await;
        let mut keys: Vec<&BindingKey> = running.keys().collect();
        keys.sort();
        assert_eq!(
            keys,
            vec![
                &(
                    "waddles.added".to_string(),
                    "discord".to_string(),
                    "dg-y".to_string()
                ),
                &(
                    "waddles.unchanged".to_string(),
                    "twitch".to_string(),
                    "tw-a".to_string()
                ),
            ]
        );
        // `calls()` records both spawns and stops: 2 initial spawns
        // ("unchanged", "removed") + 1 stop ("removed" dropped from
        // target) + 1 new spawn ("added") = 4.
        assert_eq!(
            spawner.calls().len(),
            4,
            "2 initial spawns + 1 stop + 1 added spawn"
        );
    }

    #[tokio::test]
    async fn run_tick_skips_all_db_work_when_the_gate_is_off() {
        let db = MockDatabase::new(DatabaseBackend::Postgres).into_connection();
        let mut tracker = WatermarkTracker::new();
        let mut running = HashMap::new();
        let spawner = RecordingSupervisor::default();
        let metrics = test_metrics();
        run_tick(
            &db,
            1,
            0,
            &FixedGate(false),
            &mut tracker,
            &mut running,
            &spawner,
            &metrics,
        )
        .await;
        assert!(running.is_empty());
        assert!(spawner.calls().is_empty());
    }

    #[tokio::test]
    async fn run_tick_stops_running_consumers_when_the_gate_flips_off() {
        // A full tick (gate on, watermark changed, an active app) issues
        // FOUR queries: `read_watermark`'s own `app_active_versions` +
        // `app_source_bindings` pair, then `read_source_bindings`'s own
        // independent `app_active_versions` + `app_source_bindings` pair
        // (it re-derives the active-app set itself rather than reusing
        // `read_watermark`'s, per `bundle_active_set::bindings`'s module
        // doc). Tick 2 (gate off) issues zero queries -- the gate check is
        // the very first thing `run_tick` does.
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([vec![binding_row("waddles.a", "twitch", "tw-channelA")]])
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([vec![binding_row("waddles.a", "twitch", "tw-channelA")]])
            .into_connection();
        let mut tracker = WatermarkTracker::new();
        let mut running = HashMap::new();
        let spawner = RecordingSupervisor::default();
        let metrics = test_metrics();
        let gate = ToggleGate::new(true);

        run_tick(
            &db,
            1,
            0,
            &gate,
            &mut tracker,
            &mut running,
            &spawner,
            &metrics,
        )
        .await;
        assert_eq!(running.len(), 1);

        gate.set(false);
        run_tick(
            &db,
            1,
            0,
            &gate,
            &mut tracker,
            &mut running,
            &spawner,
            &metrics,
        )
        .await;
        assert!(
            running.is_empty(),
            "kill-switch on must stop every running consumer"
        );
        assert_eq!(
            spawner.calls(),
            vec![
                "spawn:waddles.a:twitch:tw-channelA".to_string(),
                "stop:waddles.a:twitch:tw-channelA".to_string(),
            ]
        );
    }

    #[tokio::test]
    async fn run_tick_skips_the_binding_read_when_the_watermark_is_unchanged() {
        // Tick 1: a full tick (4 queries -- see
        // `run_tick_stops_running_consumers_when_the_gate_flips_off`'s
        // comment for why). Tick 2: `read_watermark`'s own 2 queries return
        // byte-identical rows to tick 1's, so the watermark is unchanged and
        // `read_source_bindings` must never run -- the trap 5th result is
        // only consumed if that guarantee breaks.
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([vec![binding_row("waddles.a", "twitch", "tw-channelA")]])
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([vec![binding_row("waddles.a", "twitch", "tw-channelA")]])
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([vec![binding_row("waddles.a", "twitch", "tw-channelA")]])
            // Trap: only consumed if tick 2 incorrectly re-reads bindings
            // despite the unchanged watermark above.
            .append_query_results([vec![binding_row("waddles.trap", "twitch", "tw-trap")]])
            .into_connection();
        let mut tracker = WatermarkTracker::new();
        let mut running = HashMap::new();
        let spawner = RecordingSupervisor::default();
        let metrics = test_metrics();
        let gate = FixedGate(true);

        run_tick(
            &db,
            1,
            0,
            &gate,
            &mut tracker,
            &mut running,
            &spawner,
            &metrics,
        )
        .await;
        run_tick(
            &db,
            1,
            0,
            &gate,
            &mut tracker,
            &mut running,
            &spawner,
            &metrics,
        )
        .await;

        assert_eq!(
            spawner.calls(),
            vec!["spawn:waddles.a:twitch:tw-channelA"],
            "tick 2's unchanged watermark must skip the binding read -- the trap must never spawn"
        );
    }

    #[tokio::test]
    async fn run_tick_stops_a_consumer_when_its_app_is_deactivated() {
        let db = MockDatabase::new(DatabaseBackend::Postgres)
            // Tick 1: full tick (4 queries).
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([vec![binding_row("waddles.a", "twitch", "tw-channelA")]])
            .append_query_results([vec![active_row("waddles.a")]])
            .append_query_results([vec![binding_row("waddles.a", "twitch", "tw-channelA")]])
            // Tick 2: the app is no longer active -- `read_watermark`'s own
            // `app_active_versions` query returns empty (still moving the
            // watermark even though the binding row itself is untouched),
            // then `read_source_bindings`' own ACTIVE-app filter
            // (`bundle_active_set::bindings`'s module doc) short-circuits
            // on the same empty active set without a second query.
            .append_query_results([
                Vec::<bundle_active_set::entities::app_active_versions::Model>::new(),
            ])
            .append_query_results([vec![binding_row("waddles.a", "twitch", "tw-channelA")]])
            .append_query_results([
                Vec::<bundle_active_set::entities::app_active_versions::Model>::new(),
            ])
            .into_connection();
        let mut tracker = WatermarkTracker::new();
        let mut running = HashMap::new();
        let spawner = RecordingSupervisor::default();
        let metrics = test_metrics();
        let gate = FixedGate(true);

        run_tick(
            &db,
            1,
            0,
            &gate,
            &mut tracker,
            &mut running,
            &spawner,
            &metrics,
        )
        .await;
        assert_eq!(running.len(), 1);

        run_tick(
            &db,
            1,
            0,
            &gate,
            &mut tracker,
            &mut running,
            &spawner,
            &metrics,
        )
        .await;
        assert!(
            running.is_empty(),
            "a deactivated app's consumer must be stopped"
        );
    }
}
