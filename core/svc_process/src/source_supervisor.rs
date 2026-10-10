//! The DB-driven source-binding supervisor: given a target list of
//! [`ResolvedBinding`]s (one per `(tenant_id, community_id, app_id,
//! platform, source_id)` binding, already carrying the tenant slug/
//! community name its Valkey stream key needs), maintains exactly one
//! dedicated `penguin_spine::GroupReader` consumer task per binding,
//! spawning one for every newly-bound source and gracefully stopping one
//! for every binding that is removed or whose app is deactivated.
//!
//! **Multi-tenant rewrite (dataplane scale design rev 4, §8 step 2):**
//! this module used to own its own single-`(tenant_id, community_id)`
//! poll loop (`run_tick`/`run`, `bundle_active_set::WatermarkTracker`) and
//! carry one fixed tenant slug/community name per whole supervisor
//! instance (`SupervisorDeps::tenant`/`community`). Both are now owned by
//! `crate::changelog_consumer` instead: that module resolves EVERY
//! affected `(tenant_id, community_id)` scope's slug/name (cached, fail-
//! closed per scope -- a resolution failure skips + counts that scope
//! rather than aborting the whole tick) and calls [`reconcile`] directly
//! with the full multi-tenant [`ResolvedBinding`] target list. This module
//! keeps only the tenant-agnostic mechanics: which consumers are running,
//! diffing against a target list, and running one consumer's drain loop.
//!
//! Bundle load/unload onto the executor remains `crate::changelog_consumer`'s
//! job, not this module's: each spawned consumer's [`crate::spine::
//! ProcessDeps::digest`] is left empty (see that field's own doc for why an
//! empty digest is a safe, already-handled "not loaded yet" state).
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

use circuit_breaker::CircuitBreaker;
use penguin_spine::{Grant, Scope, SpineClient, SpineConfig, SpineError, SpineMetrics};
use tokio::sync::oneshot;
use tokio::task::JoinHandle;

use crate::active_digests::ActiveDigests;
use crate::hop::KeyRing;
use crate::host_api::ConnectionRegistry;
use crate::license::FeatureGate;
use crate::spine::{DigestSource, LoadState, ProcessDeps};
use crate::telemetry::SourceBindingSupervisorMetrics;

/// One `app_source_bindings` row, fully resolved for consumption: the
/// tenant/community it belongs to (both the numeric scope key AND the
/// slug/name `penguin_spine::Scope::source_stream` needs) plus the binding
/// itself. `crate::changelog_consumer` builds these from `bundle_active_set
/// ::read_source_bindings_all`'s per-scope map + a cached `bundle_active_set
/// ::scope::resolve_scope` result per scope -- never hardcoded, and never
/// resolved from anything but the trusted DB (tenant isolation invariant,
/// `rules/security.md`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ResolvedBinding {
    pub tenant_id: i32,
    pub community_id: i32,
    pub tenant_slug: String,
    pub community_name: Option<String>,
    pub app_id: String,
    pub platform: String,
    pub source_id: String,
}

/// Identifies one running consumer task: the full scope-qualified key.
/// Includes `(tenant_id, community_id)` (not just `(app_id, platform,
/// source_id)`, unlike the pre-multi-tenant version of this module) since
/// two different tenants could otherwise theoretically bind the same
/// `(app_id, platform, source_id)` triple and collide in `running`.
pub type BindingKey = (i32, i32, String, String, String);

/// The full set of currently-running consumers -- `crate::changelog_consumer`
/// holds this across ticks and passes it to [`reconcile`]/[`stop_all`] by
/// `&mut` reference every tick.
pub type RunningConsumers = HashMap<BindingKey, RunningConsumer>;

fn binding_key(b: &ResolvedBinding) -> BindingKey {
    (
        b.tenant_id,
        b.community_id,
        b.app_id.clone(),
        b.platform.clone(),
        b.source_id.clone(),
    )
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

/// Builds the [`Grant`] for one resolved binding -- pulled out as a pure
/// function so the tenant-isolation fix (the resolved slug/name actually
/// reaching the stream key `penguin_spine::GroupReader` reads) is directly
/// unit-testable without a live Valkey connection.
fn binding_grant(binding: &ResolvedBinding) -> Grant {
    let scope = Scope::new(binding.tenant_slug.clone(), binding.community_name.clone());
    Grant {
        stream: scope.source_stream(&binding.platform, &binding.source_id),
        platform: binding.platform.clone(),
        source_id: binding.source_id.clone(),
    }
}

/// Everything every per-binding consumer task needs that does NOT vary by
/// binding -- built once by `crate::changelog_consumer` and shared (via
/// `Arc`) across every spawned [`run_binding_consumer`] task, mirroring
/// `crate::spine::ProcessDeps`'s own "bundle the dependencies" rationale.
/// **No `tenant`/`community` fields** (unlike the pre-multi-tenant
/// version) -- those now live per-binding on [`ResolvedBinding`], not
/// fixed for the whole supervisor.
pub struct SupervisorDeps {
    pub spine_cfg: SpineConfig,
    pub key_ring: KeyRing,
    pub connections: Arc<ConnectionRegistry>,
    pub call_timeout_ms: u64,
    pub approved_targets: HashMap<String, String>,
    pub metrics: Arc<dyn SpineMetrics>,
    pub license: Arc<dyn FeatureGate>,
    /// See `spine::ProcessDeps::kv_conn`'s doc -- opened once by
    /// `crate::lib::try_start_changelog_consumer` (inside its spawned task,
    /// since opening it is async) and cloned into every binding consumer's
    /// own `ProcessDeps` in [`run_binding_consumer`] below, rather than
    /// reopened per consumer or per reconnect attempt.
    pub kv_conn: Option<redis::aio::MultiplexedConnection>,
    /// See `spine::ProcessDeps::kv_capabilities`'s doc -- the same shared
    /// snapshot `crate::lib::try_start_changelog_consumer`'s
    /// `changelog_consumer::run` poll writes to, cloned (the `Arc`, not the
    /// snapshot) into every binding consumer's own `ProcessDeps`.
    pub kv_capabilities: Arc<bundle_host_kv::CapabilitySnapshot>,
    /// See `spine::ProcessDeps::gate`'s doc -- cloned into every binding
    /// consumer's own `ProcessDeps` in [`run_binding_consumer`] below.
    pub gate: Arc<bundle_capability_gate::CapabilityGate>,
    /// The live, poll-refreshed `app_id -> (digest, app_versions.id)`
    /// snapshot -- cloned into every binding consumer's own `ProcessDeps`
    /// in [`run_binding_consumer`] below. `spine::handle_delivered`
    /// resolves this PER DELIVERY, never once per connect: a bundle hot
    /// swap must be reflected on the very next delivery (spec SS4/SS5.1),
    /// not just the next reconnect. Fed by `crate::changelog_consumer`'s
    /// own apply step (mirrors `core/svc_action::changelog_consumer`'s
    /// identical `app_version_snapshot` feed) -- unlike `tenant`/
    /// `community`, this is NOT per-binding: `bundle_active_set::
    /// ActiveVersionSnapshot` is keyed by `app_id` alone across every
    /// scope this supervisor's tenant serves.
    pub app_version_snapshot: bundle_active_set::ActiveVersionSnapshot,
    /// Cloned into every spawned binding consumer's own `ProcessDeps` --
    /// see `crate::spine::ProcessDeps::egress`'s doc.
    pub egress: Arc<bundle_host_http::egress::EgressGuard>,
    /// Cloned into every spawned binding consumer's own `ProcessDeps` --
    /// see `crate::spine::ProcessDeps::pii_gate`'s doc.
    pub pii_gate: Arc<dyn FeatureGate>,
    /// Cloned into every spawned binding consumer's own `ProcessDeps` --
    /// see `crate::spine::ProcessDeps::pii_minter`'s doc.
    pub pii_minter: Option<Arc<dyn crate::pii_tokenize::IdentityMinter>>,
    /// The SAME `Arc<ActiveDigests>` instance `crate::changelog_consumer`
    /// writes to on every `load`/`unload` (`ConsumerState::active_digests`)
    /// -- shared (never copied) into every spawned binding consumer's own
    /// `ProcessDeps::digest_source` (`DigestSource::Active`) so a hot-swap
    /// is visible to every already-running consumer's very next invoke.
    /// regression: multi-tenant consumers invoked with empty legacy digest,
    /// UnknownBundle (alpha 2026-10-03).
    pub active_digests: Arc<ActiveDigests>,
    /// Cloned into every spawned binding consumer's own `ProcessDeps` --
    /// see `crate::spine::ProcessDeps::db_wiring`'s doc.
    pub db_wiring: Option<crate::capabilities::DbWiring>,
    /// ONE shared instance across every binding this supervisor runs
    /// (never one per binding) -- cloned into every spawned binding
    /// consumer's own `ProcessDeps`, see `crate::spine::ProcessDeps::
    /// breaker`'s doc for why pod-wide sharing is the correct scope.
    pub breaker: Arc<CircuitBreaker>,
}

/// A running per-binding consumer: a shutdown signal plus the
/// [`JoinHandle`] this supervisor awaits when stopping it, so stopping a
/// consumer always waits for its task to actually exit (graceful drain --
/// the in-flight `drain_batch` call, if any, finishes acking/dead-lettering
/// before the task returns) rather than firing a shutdown signal and
/// moving on without confirmation.
pub struct RunningConsumer {
    // `pub(crate)`, not private: `crate::changelog_consumer`'s own tests
    // construct a `RunningConsumer` directly around a trivial stand-in task
    // (no live Valkey/host-API dependency needed there either) rather than
    // duplicating this module's `ConsumerSupervisor` machinery a third time.
    pub(crate) shutdown: oneshot::Sender<()>,
    pub(crate) handle: JoinHandle<()>,
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
    fn spawn(&self, binding: &ResolvedBinding) -> RunningConsumer;
}

/// Production [`ConsumerSupervisor`]: spawns [`run_binding_consumer`] as a
/// real Tokio task against the shared [`SupervisorDeps`].
pub struct SpineConsumerSupervisor {
    pub deps: Arc<SupervisorDeps>,
}

impl ConsumerSupervisor for SpineConsumerSupervisor {
    fn spawn(&self, binding: &ResolvedBinding) -> RunningConsumer {
        let (shutdown_tx, shutdown_rx) = oneshot::channel();
        let deps = Arc::clone(&self.deps);
        let binding = binding.clone();
        let handle = tokio::spawn(run_binding_consumer(binding, deps, shutdown_rx));
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
/// Each attempt builds a fresh [`ProcessDeps`] with a
/// [`DigestSource::Active`] scoped to this binding's own `(tenant_id,
/// community_id, app_id)` -- `crate::changelog_consumer` is still what
/// actually `Load`s the bundle onto the executor (see this module's doc),
/// but this consumer's own `invoke`s now resolve the CURRENT canonical
/// digest for that same scope on every single message, from the shared
/// [`ActiveDigests`] map that consumer writes to -- never a value captured
/// once at spawn time (regression: multi-tenant consumers invoked with
/// empty legacy digest, UnknownBundle, alpha 2026-10-03) -- plus a fresh
/// [`LoadState`] per attempt.
async fn run_binding_consumer(
    binding: ResolvedBinding,
    deps: Arc<SupervisorDeps>,
    mut shutdown: oneshot::Receiver<()>,
) {
    loop {
        let grant = binding_grant(&binding);

        let spine_client = match SpineClient::connect(deps.spine_cfg.clone(), deps.metrics.clone())
            .await
        {
            Ok(c) => c,
            Err(err) => {
                tracing::warn!(
                    tenant_id = binding.tenant_id, community_id = binding.community_id,
                    app_id = %binding.app_id, platform = %binding.platform, source_id = %binding.source_id,
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
            app_id: binding.app_id.clone(),
            digest_source: DigestSource::Active {
                scope: (
                    binding.tenant_id,
                    binding.community_id,
                    binding.app_id.clone(),
                ),
                digests: Arc::clone(&deps.active_digests),
            },
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
            kv_conn: deps.kv_conn.clone(),
            kv_capabilities: Arc::clone(&deps.kv_capabilities),
            gate: Arc::clone(&deps.gate),
            tenant_id: binding.tenant_id,
            community_id: binding.community_id,
            // Shared, poll-refreshed handle -- `spine::handle_delivered`
            // resolves this PER DELIVERY (`SupervisorDeps::
            // app_version_snapshot`'s doc), never once here at connect time.
            app_version_snapshot: deps.app_version_snapshot.clone(),
            egress: Arc::clone(&deps.egress),
            pii_gate: Arc::clone(&deps.pii_gate),
            pii_minter: deps.pii_minter.clone(),
            db_wiring: deps.db_wiring.clone(),
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
                            tenant_id = binding.tenant_id, community_id = binding.community_id,
                            app_id = %binding.app_id, platform = %binding.platform, source_id = %binding.source_id,
                            "source-binding consumer: consumer group not yet provisioned (NOGROUP), retrying"
                        );
                    }
                    Err(err) => {
                        tracing::error!(
                            tenant_id = binding.tenant_id, community_id = binding.community_id,
                            app_id = %binding.app_id, platform = %binding.platform, source_id = %binding.source_id,
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
/// whose binding is no longer in `target` (removed binding, app
/// deactivated, or its whole scope skipped this tick due to a fail-closed
/// resolution/read error -- `crate::changelog_consumer` never includes a
/// skipped scope's bindings in `target`, so they surface here as simply
/// "missing"), then spawns a consumer for every binding in `target` not
/// already running. Stops before spawns so a binding that moves scope in
/// the same tick never briefly runs two consumers.
pub async fn reconcile(
    running: &mut HashMap<BindingKey, RunningConsumer>,
    target: &[ResolvedBinding],
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
                tenant_id = key.0, community_id = key.1,
                app_id = %key.2, platform = %key.3, source_id = %key.4,
                "source-binding consumer: binding removed, app deactivated, or scope skipped this tick; stopping consumer"
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
                tenant_id = binding.tenant_id, community_id = binding.community_id,
                app_id = %binding.app_id, platform = %binding.platform, source_id = %binding.source_id,
                "source-binding consumer: new binding, spawning consumer"
            );
            let consumer = spawner.spawn(binding);
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
pub async fn stop_all(
    running: &mut HashMap<BindingKey, RunningConsumer>,
    metrics: &SourceBindingSupervisorMetrics,
) {
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

    /// Records every `spawn`/`stop` call it receives (as
    /// `"spawn:tenant:community:app:platform:source"`/`"stop:..."`)
    /// without any live Valkey/host-API dependency -- the spawned task
    /// itself is a trivial `tokio::spawn` that just awaits its own
    /// shutdown signal and records the stop, proving [`reconcile`]'s diff
    /// logic without needing `penguin_spine`/`crate::spine::run` at all.
    #[derive(Default)]
    struct RecordingSupervisor {
        calls: Arc<StdMutex<Vec<String>>>,
    }

    impl RecordingSupervisor {
        fn calls(&self) -> Vec<String> {
            self.calls.lock().unwrap().clone()
        }
    }

    fn call_key(b: &ResolvedBinding) -> String {
        format!(
            "{}:{}:{}:{}:{}",
            b.tenant_id, b.community_id, b.app_id, b.platform, b.source_id
        )
    }

    impl ConsumerSupervisor for RecordingSupervisor {
        fn spawn(&self, binding: &ResolvedBinding) -> RunningConsumer {
            let key = call_key(binding);
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

    fn binding(
        tenant_id: i32,
        community_id: i32,
        app_id: &str,
        platform: &str,
        source_id: &str,
    ) -> ResolvedBinding {
        ResolvedBinding {
            tenant_id,
            community_id,
            tenant_slug: format!("tenant{tenant_id}"),
            community_name: None,
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
    /// community name must be exactly what ends up in the Valkey stream
    /// key a consumer actually reads.
    #[test]
    fn binding_grant_uses_the_resolved_tenant_slug_and_community_name() {
        let mut b = binding(7, 3, "waddles.a", "twitch", "tw-channelA");
        b.tenant_slug = "acme".to_string();
        b.community_name = Some("main".to_string());
        let grant = binding_grant(&b);
        assert_eq!(
            grant.stream,
            "waddles:t:acme:c:main:src:twitch:tw-channelA:events"
        );
        assert_eq!(grant.platform, "twitch");
        assert_eq!(grant.source_id, "tw-channelA");
    }

    #[test]
    fn binding_grant_renders_the_tenant_wide_segment_for_no_community() {
        let mut b = binding(7, 0, "waddles.a", "discord", "dg-x");
        b.tenant_slug = "acme".to_string();
        let grant = binding_grant(&b);
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
            &[binding(1, 0, "waddles.a", "twitch", "tw-channelA")],
            &spawner,
            &metrics,
        )
        .await;
        assert_eq!(running.len(), 1);
        assert_eq!(
            spawner.calls(),
            vec!["spawn:1:0:waddles.a:twitch:tw-channelA"]
        );
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
        let target = [binding(1, 0, "waddles.a", "twitch", "tw-channelA")];
        reconcile(&mut running, &target, &spawner, &metrics).await;
        reconcile(&mut running, &target, &spawner, &metrics).await;
        assert_eq!(
            spawner.calls(),
            vec!["spawn:1:0:waddles.a:twitch:tw-channelA"],
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
            &[binding(1, 0, "waddles.a", "twitch", "tw-channelA")],
            &spawner,
            &metrics,
        )
        .await;
        reconcile(&mut running, &[], &spawner, &metrics).await;
        assert!(running.is_empty());
        assert_eq!(
            spawner.calls(),
            vec![
                "spawn:1:0:waddles.a:twitch:tw-channelA".to_string(),
                "stop:1:0:waddles.a:twitch:tw-channelA".to_string(),
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

    /// Multi-tenant regression: two DIFFERENT tenants binding the exact
    /// same `(app_id, platform, source_id)` triple must run as two
    /// independent consumers, never collapsed into one -- proves
    /// `BindingKey` genuinely includes `(tenant_id, community_id)`, not
    /// just the old single-tenant triple.
    #[tokio::test]
    async fn reconcile_runs_independent_consumers_for_the_same_triple_across_tenants() {
        let mut running = HashMap::new();
        let spawner = RecordingSupervisor::default();
        let metrics = test_metrics();
        reconcile(
            &mut running,
            &[
                binding(1, 0, "waddles.a", "twitch", "tw-shared"),
                binding(2, 0, "waddles.a", "twitch", "tw-shared"),
            ],
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
                binding(1, 0, "waddles.unchanged", "twitch", "tw-a"),
                binding(1, 0, "waddles.removed", "discord", "dg-x"),
            ],
            &spawner,
            &metrics,
        )
        .await;
        reconcile(
            &mut running,
            &[
                binding(1, 0, "waddles.unchanged", "twitch", "tw-a"),
                binding(1, 0, "waddles.added", "discord", "dg-y"),
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
            &[binding(1, 0, "waddles.a", "twitch", "tw-channelA")],
            &spawner,
            &metrics,
        )
        .await;
        assert_eq!(running.len(), 1);
        stop_all(&mut running, &metrics).await;
        assert!(running.is_empty());
    }
}
