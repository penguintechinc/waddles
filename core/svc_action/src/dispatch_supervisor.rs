//! The DB-driven multi-tenant dispatch-consumer supervisor: given a target
//! list of [`DispatchTarget`]s (one per active `(tenant_id, community_id,
//! app_id)` scope in `bundle_active_set`'s active set), maintains exactly
//! one dedicated `crate::dispatch::run` consumer task per scope, spawning
//! one for every newly-active scope and gracefully stopping one for every
//! scope that is removed or deactivated -- hot-swap, no pod restart.
//!
//! **Why this exists** (regression: svc-action had no multi-tenant dispatch
//! consumers; replies never sent after legacy env removal, alpha
//! 2026-10-03): `crate::changelog_consumer`'s DB-driven path only ever drove
//! bundle `Load`/`Unload` onto the executor via `crate::bundle_loader::
//! BundleSink` -- it never consumed the `:action` stream `svc-process`
//! writes process-stage outputs onto, so nothing in the multi-tenant path
//! ever dispatched a reply. `dispatch::run` was reachable only through the
//! single-app `ACTION_APP_ID` env path (`crate::lib::try_start_dispatch`),
//! which PR #538 retired from alpha -- leaving the DB-driven path
//! dispatching nothing at all. This module is the missing per-app
//! multi-tenant half, direct port of `core/svc_process/src/
//! source_supervisor.rs`'s mechanics (diff/spawn/stop, `ConsumerSupervisor`
//! test seam) adapted to one stream per scope (no platform/source_id axis --
//! an action stream is keyed purely by `(tenant, community, app_id)`,
//! `penguin_spine::Scope::action_stream`).
//!
//! **Stream key parity with the writer (spec §5.9):** `target_grant` below
//! builds the exact same key `core/svc_process/src/spine.rs::handle_delivered`
//! writes process-stage outputs onto -- `Scope::new(tenant_slug,
//! community_name).action_stream(app_id)`. `tests::
//! dispatch_grant_matches_the_svc_process_action_stream_writer_key` pins
//! this equality directly against that writer's own key-construction
//! arguments so the two can never silently drift apart.
//!
//! **Consumer group provisioning:** unlike `source_supervisor`'s ingest
//! bindings (hub-api provisions `app_source_bindings`' consumer groups),
//! nothing else provisions an action stream's `{app_id}` consumer group in
//! the multi-tenant path -- [`run_app_consumer`] calls `crate::dispatch::
//! ensure_consumer_group` (idempotent `XGROUP CREATE ... MKSTREAM`,
//! `BUSYGROUP` treated as success, PR #528's helper) before every connect
//! attempt, exactly like the legacy single-app path's own retry loop.

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use penguin_spine::{Grant, Scope, SpineClient, SpineConfig, SpineMetrics};
use sea_orm::DatabaseConnection;
use tokio::sync::oneshot;
use tokio::task::JoinHandle;

use crate::active_digests::{ActiveDigests, LoadedSessions};
use crate::dispatch::{self, DigestSource, DispatchDeps, RetryPolicy};
use crate::flags::FeatureFlag;
use crate::hop::KeyRing;
use crate::host_api::ConnectionRegistry;
use crate::retry::Jitter;
use crate::telemetry::DispatchSupervisorMetrics;
use crate::usage::UsageBatcher;
use crate::wiring::{DbAuditSink, DbTenantResolver};

/// One active `(tenant_id, community_id, app_id)` scope, fully resolved for
/// consumption: the tenant/community it belongs to (both the numeric scope
/// key AND the slug/name `penguin_spine::Scope::action_stream` needs) plus
/// the app id. `crate::changelog_consumer` builds these from its own
/// active-set scopes (`bundle_active_set::scoped_active_rows`) + a cached
/// `bundle_active_set::resolve_scope` result per scope -- never hardcoded,
/// never resolved from anything but the trusted DB (tenant isolation
/// invariant, `rules/security.md`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DispatchTarget {
    pub tenant_id: i32,
    pub community_id: i32,
    pub tenant_slug: String,
    pub community_name: Option<String>,
    pub app_id: String,
}

/// Identifies one running consumer task: `(tenant_id, community_id,
/// app_id)` -- matches `bundle_active_set::AppScope` exactly (the same key
/// `crate::active_digests::ActiveDigests` and `ConsumerState::loaded` use),
/// so two different tenants independently activating the same `app_id`
/// always run as two independent consumers, never collapsed into one.
pub type DispatchKey = (i32, i32, String);

/// The full set of currently-running consumers -- `crate::changelog_consumer`
/// holds this across ticks and passes it to [`reconcile`]/[`stop_all`] by
/// `&mut` reference every tick.
pub type RunningConsumers = HashMap<DispatchKey, RunningConsumer>;

fn dispatch_key(t: &DispatchTarget) -> DispatchKey {
    (t.tenant_id, t.community_id, t.app_id.clone())
}

/// How long a per-app consumer waits after `dispatch::run` exits (NOGROUP, a
/// dropped Valkey connection, a spine-client connect failure, ...) before
/// reconnecting -- same constant/rationale as `core/svc_process/src/
/// source_supervisor.rs::CONSUMER_RETRY_BACKOFF`.
const CONSUMER_RETRY_BACKOFF: Duration = Duration::from_secs(5);

/// Builds the [`Grant`] for one dispatch target -- pulled out as a pure
/// function so stream-key parity with the writer (this module's own doc) is
/// directly unit-testable without a live Valkey connection.
fn target_grant(target: &DispatchTarget) -> (Grant, String) {
    let scope = Scope::new(target.tenant_slug.clone(), target.community_name.clone());
    let stream = scope.action_stream(&target.app_id);
    (
        Grant {
            stream: stream.clone(),
            platform: "internal".to_string(),
            source_id: target.app_id.clone(),
        },
        stream,
    )
}

/// Everything every per-app dispatch consumer task needs that does NOT vary
/// by target -- built once by `crate::changelog_consumer` (via `crate::lib`)
/// and shared (via `Arc`) across every spawned [`run_app_consumer`] task.
pub struct SupervisorDeps {
    pub spine_cfg: SpineConfig,
    pub key_ring: KeyRing,
    pub connections: Arc<ConnectionRegistry>,
    pub retry_policy: RetryPolicy,
    /// A dedicated, already-connected DB handle used to build each spawned
    /// consumer's own `DbAuditSink`/`DbTenantResolver` -- `sea_orm::
    /// DatabaseConnection` is itself a cheap, internally-pooled handle
    /// (`Clone`), matching `crate::lib::try_start_dispatch`'s own
    /// `db.clone()` convention.
    pub db: DatabaseConnection,
    pub usage: Arc<Mutex<UsageBatcher>>,
    pub metrics: Arc<dyn SpineMetrics>,
    pub rust_data_plane: Arc<dyn FeatureFlag>,
    /// The SAME `Arc<ActiveDigests>` instance `crate::changelog_consumer`
    /// writes to on every `load`/`unload` -- shared (never copied) into
    /// every spawned consumer's own `DispatchDeps::digest_source`
    /// (`DigestSource::Active`) so a hot-swap is visible to every
    /// already-running consumer's very next invoke. regression: svc-action
    /// had no multi-tenant dispatch consumers; replies never sent after
    /// legacy env removal (alpha 2026-10-03).
    pub active_digests: Arc<ActiveDigests>,
    /// The SAME `Arc<LoadedSessions>` instance `crate::changelog_consumer`
    /// writes to on every per-session `load`/`unload` -- shared (never
    /// copied) into every spawned consumer's own `DispatchDeps::
    /// digest_source` (`DigestSource::Active`) so `crate::dispatch::
    /// handle_delivered` can pick a live session that actually has the
    /// target digest loaded, never just whichever connection `
    /// ConnectionRegistry::active()` calls "newest". regression: bundles
    /// loaded only onto a terminating executor during rollout; live
    /// executor got none (alpha 2026-10-03).
    pub loaded_sessions: Arc<LoadedSessions>,
}

/// A running per-app dispatch consumer: a shutdown signal plus the
/// [`JoinHandle`] this supervisor awaits when stopping it -- graceful drain,
/// same contract as `core/svc_process/src/source_supervisor.rs::
/// RunningConsumer`.
pub struct RunningConsumer {
    pub(crate) shutdown: oneshot::Sender<()>,
    pub(crate) handle: JoinHandle<()>,
}

impl RunningConsumer {
    async fn stop(self) {
        let _ = self.shutdown.send(());
        if let Err(err) = self.handle.await {
            tracing::warn!(error = %err, "dispatch consumer task panicked while stopping");
        }
    }
}

/// Spawns a per-app dispatch consumer task. Production wires
/// [`SpineConsumerSupervisor`]; tests wire a fake that records spawn calls
/// without any live Valkey/host-API/DB dependency.
pub trait ConsumerSupervisor: Send + Sync {
    fn spawn(&self, target: &DispatchTarget) -> RunningConsumer;
}

/// Production [`ConsumerSupervisor`]: spawns [`run_app_consumer`] as a real
/// Tokio task against the shared [`SupervisorDeps`].
pub struct SpineConsumerSupervisor {
    pub deps: Arc<SupervisorDeps>,
}

impl ConsumerSupervisor for SpineConsumerSupervisor {
    fn spawn(&self, target: &DispatchTarget) -> RunningConsumer {
        let (shutdown_tx, shutdown_rx) = oneshot::channel();
        let deps = Arc::clone(&self.deps);
        let target = target.clone();
        let handle = tokio::spawn(run_app_consumer(target, deps, shutdown_rx));
        RunningConsumer {
            shutdown: shutdown_tx,
            handle,
        }
    }
}

/// Sleeps for `dur`, or returns early (reporting `true`) if `shutdown`
/// resolves first.
async fn wait_or_shutdown(shutdown: &mut oneshot::Receiver<()>, dur: Duration) -> bool {
    tokio::select! {
        _ = shutdown => true,
        _ = tokio::time::sleep(dur) => false,
    }
}

/// Runs one app's dedicated `crate::dispatch::run` consumer (consumer group
/// = `app_id`, exactly one granted stream -- this scope's own action
/// stream) until `shutdown` resolves, reconnecting with
/// [`CONSUMER_RETRY_BACKOFF`] between attempts on any error. Idempotently
/// (re)provisions the consumer group before every attempt
/// (`dispatch::ensure_consumer_group`, PR #528) -- nothing else in the
/// multi-tenant path ever creates it (this module's own doc).
///
/// Each attempt builds a fresh [`DispatchDeps`] with a
/// [`DigestSource::Active`] scoped to this target's own `(tenant_id,
/// community_id, app_id)` -- `crate::changelog_consumer` is still what
/// actually `Load`s the bundle onto the executor; this consumer's own
/// `invoke`s resolve the CURRENT canonical digest for that same scope on
/// every single message, from the shared [`ActiveDigests`] map that
/// consumer writes to.
async fn run_app_consumer(
    target: DispatchTarget,
    deps: Arc<SupervisorDeps>,
    mut shutdown: oneshot::Receiver<()>,
) {
    loop {
        let (grant, stream_key) = target_grant(&target);

        if let Err(err) =
            dispatch::ensure_consumer_group(&deps.spine_cfg, &stream_key, &target.app_id).await
        {
            tracing::warn!(
                tenant_id = target.tenant_id, community_id = target.community_id,
                app_id = %target.app_id, stream = %stream_key, error = %err,
                "dispatch consumer: ensure consumer group failed, retrying"
            );
            if wait_or_shutdown(&mut shutdown, CONSUMER_RETRY_BACKOFF).await {
                return;
            }
            continue;
        }

        let spine_client =
            match SpineClient::connect(deps.spine_cfg.clone(), deps.metrics.clone()).await {
                Ok(c) => c,
                Err(err) => {
                    tracing::warn!(
                        tenant_id = target.tenant_id, community_id = target.community_id,
                        app_id = %target.app_id, stream = %stream_key, error = %err,
                        "dispatch consumer: spine client connect failed, retrying"
                    );
                    if wait_or_shutdown(&mut shutdown, CONSUMER_RETRY_BACKOFF).await {
                        return;
                    }
                    continue;
                }
            };

        let dispatch_deps = DispatchDeps {
            app_id: target.app_id.clone(),
            digest_source: DigestSource::Active {
                scope: (target.tenant_id, target.community_id, target.app_id.clone()),
                digests: Arc::clone(&deps.active_digests),
                sessions: Arc::clone(&deps.loaded_sessions),
            },
            // No DB-sourced per-bundle config exists yet for this stage
            // (`crate::lib::resolve_initial_bundle`'s identical legacy
            // default) -- a documented seam, not a regression introduced
            // here.
            config_json: "{}".to_string(),
            key_ring: deps.key_ring.clone(),
            connections: Arc::clone(&deps.connections),
            retry_policy: deps.retry_policy.clone(),
            jitter: Jitter::from_entropy(),
            audit: DbAuditSink::new(deps.db.clone()),
            tenants: DbTenantResolver::new(deps.db.clone()),
            usage: Arc::clone(&deps.usage),
            consumer_id: deps.spine_cfg.consumer_id.clone(),
            spine: spine_client,
            metrics: deps.metrics.clone(),
        };

        let (inner_tx, inner_rx) = oneshot::channel();
        let run_fut = dispatch::run(
            deps.spine_cfg.clone(),
            vec![grant],
            stream_key.clone(),
            dispatch_deps,
            Arc::clone(&deps.rust_data_plane),
            inner_rx,
        );
        tokio::pin!(run_fut);

        tokio::select! {
            _ = &mut shutdown => {
                let _ = inner_tx.send(());
                let _ = run_fut.await;
                return;
            }
            result = &mut run_fut => {
                match result {
                    Ok(()) => return,
                    Err(err) if dispatch::is_nogroup_error(&err) => {
                        tracing::warn!(
                            tenant_id = target.tenant_id, community_id = target.community_id,
                            app_id = %target.app_id, stream = %stream_key,
                            "dispatch consumer: consumer group not yet provisioned (NOGROUP), retrying"
                        );
                    }
                    Err(err) => {
                        tracing::error!(
                            tenant_id = target.tenant_id, community_id = target.community_id,
                            app_id = %target.app_id, stream = %stream_key, error = %err,
                            "dispatch consumer exited, retrying"
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
/// whose scope is no longer in `target` (deactivated, or its whole scope
/// skipped this tick due to a fail-closed resolution/read error), then
/// spawns a consumer for every scope in `target` not already running. Stops
/// before spawns so a scope that moves in the same tick never briefly runs
/// two consumers.
pub async fn reconcile(
    running: &mut RunningConsumers,
    target: &[DispatchTarget],
    spawner: &dyn ConsumerSupervisor,
    metrics: &DispatchSupervisorMetrics,
) {
    let target_keys: HashSet<DispatchKey> = target.iter().map(dispatch_key).collect();

    let to_stop: Vec<DispatchKey> = running
        .keys()
        .filter(|k| !target_keys.contains(*k))
        .cloned()
        .collect();
    for key in to_stop {
        if let Some(consumer) = running.remove(&key) {
            tracing::info!(
                tenant_id = key.0, community_id = key.1, app_id = %key.2,
                "dispatch consumer: app deactivated or scope skipped this tick; stopping consumer"
            );
            consumer.stop().await;
            metrics
                .consumer_transitions_total
                .with_label_values(&["stop"])
                .inc();
            metrics.active_consumers.dec();
        }
    }

    for t in target {
        let key = dispatch_key(t);
        if let std::collections::hash_map::Entry::Vacant(entry) = running.entry(key.clone()) {
            let (_, stream_key) = target_grant(t);
            tracing::info!(
                tenant_id = t.tenant_id, community_id = t.community_id, app_id = %t.app_id,
                stream = %stream_key,
                "dispatch consumer: new active scope, spawning consumer"
            );
            let consumer = spawner.spawn(t);
            entry.insert(consumer);
            metrics
                .consumer_transitions_total
                .with_label_values(&["spawn"])
                .inc();
            metrics.active_consumers.inc();
        }
    }
}

/// Stops every currently-running consumer -- used by
/// `crate::changelog_consumer` on shutdown, and when the multi-tenant path
/// itself is disabled mid-run (kill-switch flip).
pub async fn stop_all(running: &mut RunningConsumers, metrics: &DispatchSupervisorMetrics) {
    for (_, consumer) in running.drain() {
        consumer.stop().await;
        metrics
            .consumer_transitions_total
            .with_label_values(&["stop"])
            .inc();
        metrics.active_consumers.dec();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex as StdMutex;

    /// Records every `spawn`/`stop` call it receives
    /// (`"spawn:tenant:community:app"`/`"stop:..."`) without any live
    /// Valkey/host-API/DB dependency.
    #[derive(Default)]
    struct RecordingSupervisor {
        calls: Arc<StdMutex<Vec<String>>>,
    }

    impl RecordingSupervisor {
        fn calls(&self) -> Vec<String> {
            self.calls.lock().unwrap().clone()
        }
    }

    fn call_key(t: &DispatchTarget) -> String {
        format!("{}:{}:{}", t.tenant_id, t.community_id, t.app_id)
    }

    impl ConsumerSupervisor for RecordingSupervisor {
        fn spawn(&self, target: &DispatchTarget) -> RunningConsumer {
            let key = call_key(target);
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

    fn test_metrics() -> DispatchSupervisorMetrics {
        crate::telemetry::register_dispatch_supervisor_metrics(&prometheus::Registry::new())
    }

    fn target(tenant_id: i32, community_id: i32, app_id: &str) -> DispatchTarget {
        DispatchTarget {
            tenant_id,
            community_id,
            tenant_slug: format!("tenant{tenant_id}"),
            community_name: None,
            app_id: app_id.to_string(),
        }
    }

    /// Stream-key parity regression: `target_grant` must build EXACTLY the
    /// key `core/svc_process/src/spine.rs::handle_delivered` writes
    /// process-stage outputs onto -- `Scope::new(tenant, community).
    /// action_stream(app_id)`, same literal `_tenant` sentinel for a
    /// tenant-wide (no-community) scope. Pinned against the same literal
    /// fixture values `core/svc_process/src/spine.rs`'s own
    /// `handle_delivered_reply_enqueues_onto_the_same_apps_action_stream_and_acks`
    /// test uses, so the two can never silently drift apart.
    #[test]
    fn dispatch_grant_matches_the_svc_process_action_stream_writer_key() {
        let t = DispatchTarget {
            tenant_id: 7,
            community_id: 3,
            tenant_slug: "acme".to_string(),
            community_name: Some("main".to_string()),
            app_id: "waddles.bot.commands.default".to_string(),
        };
        let (grant, stream) = target_grant(&t);

        // The exact key `bundle_active_set`/`penguin_spine::scope`'s own
        // fixtures use for this (tenant, community, app_id) triple.
        let writer_key = penguin_spine::Scope::new("acme".to_string(), Some("main".to_string()))
            .action_stream("waddles.bot.commands.default");
        assert_eq!(stream, writer_key);
        assert_eq!(
            stream,
            "waddles:t:acme:c:main:app:waddles.bot.commands.default:action"
        );
        assert_eq!(grant.stream, stream);
    }

    #[test]
    fn dispatch_grant_renders_the_tenant_wide_segment_for_no_community() {
        let t = target(7, 0, "waddles.a");
        let mut t = t;
        t.tenant_slug = "acme".to_string();
        let (grant, stream) = target_grant(&t);
        assert_eq!(stream, "waddles:t:acme:c:_tenant:app:waddles.a:action");
        let writer_key =
            penguin_spine::Scope::new("acme".to_string(), None).action_stream("waddles.a");
        assert_eq!(stream, writer_key);
        assert_eq!(grant.platform, "internal");
        assert_eq!(grant.source_id, "waddles.a");
    }

    #[tokio::test]
    async fn reconcile_spawns_a_consumer_for_a_newly_active_scope() {
        let mut running = HashMap::new();
        let spawner = RecordingSupervisor::default();
        let metrics = test_metrics();
        reconcile(
            &mut running,
            &[target(1, 0, "waddles.a")],
            &spawner,
            &metrics,
        )
        .await;
        assert_eq!(running.len(), 1);
        assert_eq!(spawner.calls(), vec!["spawn:1:0:waddles.a"]);
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
    async fn reconcile_is_a_noop_when_the_scope_is_already_running() {
        let mut running = HashMap::new();
        let spawner = RecordingSupervisor::default();
        let metrics = test_metrics();
        let targets = [target(1, 0, "waddles.a")];
        reconcile(&mut running, &targets, &spawner, &metrics).await;
        reconcile(&mut running, &targets, &spawner, &metrics).await;
        assert_eq!(
            spawner.calls(),
            vec!["spawn:1:0:waddles.a"],
            "an already-running scope must not be re-spawned"
        );
    }

    #[tokio::test]
    async fn reconcile_stops_a_consumer_for_a_deactivated_scope() {
        let mut running = HashMap::new();
        let spawner = RecordingSupervisor::default();
        let metrics = test_metrics();
        reconcile(
            &mut running,
            &[target(1, 0, "waddles.a")],
            &spawner,
            &metrics,
        )
        .await;
        reconcile(&mut running, &[], &spawner, &metrics).await;
        assert!(running.is_empty());
        assert_eq!(
            spawner.calls(),
            vec![
                "spawn:1:0:waddles.a".to_string(),
                "stop:1:0:waddles.a".to_string()
            ]
        );
        assert_eq!(metrics.active_consumers.get(), 0);
    }

    /// Multi-tenant regression: two DIFFERENT tenants activating the exact
    /// same `app_id` must run as two independent consumers, never collapsed
    /// into one -- proves `DispatchKey` genuinely includes `(tenant_id,
    /// community_id)`, not just `app_id`.
    #[tokio::test]
    async fn reconcile_runs_independent_consumers_for_the_same_app_id_across_tenants() {
        let mut running = HashMap::new();
        let spawner = RecordingSupervisor::default();
        let metrics = test_metrics();
        reconcile(
            &mut running,
            &[target(1, 0, "waddles.a"), target(2, 0, "waddles.a")],
            &spawner,
            &metrics,
        )
        .await;
        assert_eq!(running.len(), 2, "each tenant gets its own consumer");
        assert_eq!(metrics.active_consumers.get(), 2);
    }

    #[tokio::test]
    async fn reconcile_handles_a_mixed_add_and_remove_tick() {
        let mut running = HashMap::new();
        let spawner = RecordingSupervisor::default();
        let metrics = test_metrics();
        reconcile(
            &mut running,
            &[
                target(1, 0, "waddles.unchanged"),
                target(1, 0, "waddles.removed"),
            ],
            &spawner,
            &metrics,
        )
        .await;
        reconcile(
            &mut running,
            &[
                target(1, 0, "waddles.unchanged"),
                target(1, 0, "waddles.added"),
            ],
            &spawner,
            &metrics,
        )
        .await;
        assert_eq!(running.len(), 2);
        assert_eq!(
            spawner.calls().len(),
            4,
            "2 initial spawns + 1 stop + 1 added spawn"
        );
    }

    #[tokio::test]
    async fn stop_all_stops_every_running_consumer() {
        let mut running = HashMap::new();
        let spawner = RecordingSupervisor::default();
        let metrics = test_metrics();
        reconcile(
            &mut running,
            &[target(1, 0, "waddles.a")],
            &spawner,
            &metrics,
        )
        .await;
        assert_eq!(running.len(), 1);
        stop_all(&mut running, &metrics).await;
        assert!(running.is_empty());
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
}
