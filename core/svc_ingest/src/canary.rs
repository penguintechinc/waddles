//! Command-latency synthetic canary: the per-minute driver and the egress sink rule.
//!
//! See `command_latency::canary` for the full path diagram and safety properties. This module is the
//! svc-ingest half: [`CanaryRunner`] publishes one harmless registered command as a synthetic Discord
//! chat message into the REAL pipeline once a minute (flag `waddles.command-latency-canary`, default
//! OFF) and waits for svc-action to report the end-to-end result; [`sink_matches`] is the exact-match
//! predicate the Discord outbound drain uses to acknowledge the canary's reply WITHOUT calling Discord.
//!
//! Fail loud: an enabled canary that is misconfigured, cannot publish, or is not confirmed within
//! [`command_latency::canary::TIMEOUT`] logs at ERROR and bumps
//! `waddles_canary_failures_total{reason}`; it never silently does nothing.

use std::future::Future;
use std::time::Duration;

use command_latency::canary::{self, CanaryConfig, SinkMode};
use command_latency::metrics::metrics;
use command_latency::Outcome;
use penguin_spine::{PlatformEvent, Source};

use crate::outbound_ops::{OutboundAction, OutboundEntry};

/// The poll cadence while waiting for the result list.
const POLL: Duration = Duration::from_millis(100);

/// `true` only in `swallow` mode, for an outbound `chat.send` that is exactly the canary's: the
/// configured numeric tenant, numeric community and channel (all three, every time). Anything else
/// -- including an absent config and every `post`-mode send -- is a normal send. This is the ONLY
/// condition under which the drain skips the platform call.
pub fn sink_matches(entry: &OutboundEntry) -> bool {
    sink_matches_with(CanaryConfig::from_env_cached(), entry)
}

/// [`sink_matches`] over an explicit config (tests).
pub fn sink_matches_with(cfg: Option<&CanaryConfig>, entry: &OutboundEntry) -> bool {
    let Some(cfg) = cfg else {
        return false;
    };
    if cfg.sink_mode != SinkMode::Swallow {
        return false;
    }
    match &entry.action {
        OutboundAction::ChatSend { channel, .. } => {
            cfg.is_sink_target(entry.tenant_id, entry.community_id, channel)
        }
        _ => false,
    }
}

/// The synthetic event: a plain Discord chat message in the canary guild/channel invoking the
/// configured harmless command. No real user, no token: the author is the sentinel guild id (a
/// snowflake that is not a user) and the actor is a fixed non-human label.
pub fn canary_event(cfg: &CanaryConfig, now_rfc3339: &str) -> PlatformEvent {
    let mut payload = serde_json::Map::new();
    payload.insert("text".into(), cfg.command_text.clone().into());
    payload.insert("guild_id".into(), cfg.guild_id.clone().into());
    payload.insert("channel_id".into(), cfg.channel_id.clone().into());
    payload.insert("message_id".into(), cfg.guild_id.clone().into());
    payload.insert("author_id".into(), cfg.guild_id.clone().into());
    payload.insert("is_mod".into(), false.into());
    payload.insert("is_broadcaster".into(), false.into());
    PlatformEvent {
        platform: "discord".to_owned(),
        event_type: "message".to_owned(),
        actor: Some("waddles-latency-canary".to_owned()),
        payload,
        occurred_at: now_rfc3339.to_owned(),
        source: Some(Source {
            platform: "discord".to_owned(),
            account_id: cfg.guild_id.clone(),
            channel_id: Some(cfg.channel_id.clone()),
        }),
    }
}

/// The Valkey operations the runner needs (a seam for tests).
pub trait CanaryStore: Send + Sync {
    /// One driver per minute across replicas: `SET NX EX` on the lock key. `Ok(false)` = another
    /// replica holds this minute.
    fn try_lock(&self) -> impl Future<Output = Result<bool, String>> + Send;
    /// Pops one result pushed by svc-action (`RPOP`), `Ok(None)` when nothing is waiting.
    fn pop_result(&self) -> impl Future<Output = Result<Option<String>, String>> + Send;
    /// Drops stale results left by an earlier timed-out run, so they cannot satisfy this one.
    fn clear_results(&self) -> impl Future<Output = Result<(), String>> + Send;
}

/// Publishes the synthetic event with the given first-door instant.
pub trait CanaryPublisher: Send + Sync {
    /// Publishes `event`, stamping `t0_ms` as the first door.
    fn publish(
        &self,
        event: PlatformEvent,
        t0_ms: u64,
    ) -> impl Future<Output = Result<(), String>> + Send;
}

/// The Valkey-backed [`CanaryStore`].
pub struct ValkeyCanaryStore {
    conn: redis::aio::MultiplexedConnection,
    lock_ttl_secs: u64,
}

impl ValkeyCanaryStore {
    /// Wraps an established connection. The one-driver lock expires after 80% of the configured
    /// interval, so the next (jittered, at least 90% of the interval) tick can always take it while
    /// two replicas never fire within the same interval.
    pub fn new(conn: redis::aio::MultiplexedConnection, interval: Duration) -> Self {
        Self {
            conn,
            lock_ttl_secs: (interval.as_secs() * 8 / 10).max(1),
        }
    }
}

impl CanaryStore for ValkeyCanaryStore {
    async fn try_lock(&self) -> Result<bool, String> {
        let mut c = self.conn.clone();
        let r: Option<String> = redis::cmd("SET")
            .arg(canary::LOCK_KEY)
            .arg("1")
            .arg("NX")
            .arg("EX")
            .arg(self.lock_ttl_secs)
            .query_async(&mut c)
            .await
            .map_err(|e| e.to_string())?;
        Ok(r.is_some())
    }

    async fn pop_result(&self) -> Result<Option<String>, String> {
        let mut c = self.conn.clone();
        redis::cmd("RPOP")
            .arg(canary::RESULT_KEY)
            .query_async(&mut c)
            .await
            .map_err(|e| e.to_string())
    }

    async fn clear_results(&self) -> Result<(), String> {
        let mut c = self.conn.clone();
        redis::cmd("DEL")
            .arg(canary::RESULT_KEY)
            .query_async::<()>(&mut c)
            .await
            .map_err(|e| e.to_string())
    }
}

/// What one canary tick did.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RunOutcome {
    /// Another replica owns this minute.
    NotLeader,
    /// Confirmed within the timeout; `e2e_ms` is svc-action's first-door -> confirm measurement.
    Confirmed {
        /// First door to outbound confirmed, ms.
        e2e_ms: u64,
        /// The outcome svc-action reported (`ok`, `refused`, ...).
        outcome: String,
    },
    /// A loud failure; the reason is also the `waddles_canary_failures_total` label.
    Failed(&'static str),
}

/// Fires one canary and waits for its result.
pub struct CanaryRunner<S: CanaryStore, P: CanaryPublisher> {
    cfg: CanaryConfig,
    store: S,
    publisher: P,
    timeout: Duration,
}

impl<S: CanaryStore, P: CanaryPublisher> CanaryRunner<S, P> {
    /// A runner with the production [`canary::TIMEOUT`].
    pub fn new(cfg: CanaryConfig, store: S, publisher: P) -> Self {
        Self {
            cfg,
            store,
            publisher,
            timeout: canary::TIMEOUT,
        }
    }

    /// The configured driver cadence (before jitter and backoff).
    pub fn interval(&self) -> Duration {
        self.cfg.interval
    }

    /// Overrides the timeout (tests).
    #[must_use]
    pub fn with_timeout(mut self, timeout: Duration) -> Self {
        self.timeout = timeout;
        self
    }

    /// One canary tick: lock, publish, wait for the action stage's result, account for it. Every
    /// failure is an ERROR log plus a failure counter.
    pub async fn run_once(&self) -> RunOutcome {
        match self.store.try_lock().await {
            Ok(true) => {}
            Ok(false) => return RunOutcome::NotLeader,
            Err(e) => {
                tracing::error!(error = %e, "canary: valkey unavailable (lock)");
                metrics().record_canary_failure("store_unavailable");
                return RunOutcome::Failed("store_unavailable");
            }
        }
        if let Err(e) = self.store.clear_results().await {
            tracing::error!(error = %e, "canary: could not clear stale results");
            metrics().record_canary_failure("store_unavailable");
            return RunOutcome::Failed("store_unavailable");
        }
        let t0 = command_latency::stamp::now_ms();
        let now_rfc3339 = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true);
        let event = canary_event(&self.cfg, &now_rfc3339);
        if let Err(e) = self.publisher.publish(event, t0).await {
            tracing::error!(error = %e, "canary: publish failed");
            metrics().record_canary_failure("publish_failed");
            return RunOutcome::Failed("publish_failed");
        }
        let deadline = tokio::time::Instant::now() + self.timeout;
        loop {
            match self.store.pop_result().await {
                Ok(Some(raw)) => {
                    return match canary::parse_result(&raw) {
                        Some((outcome, e2e_ms)) if outcome == "ok" => {
                            if e2e_ms
                                >= u64::try_from(command_latency::BUDGET.as_millis())
                                    .unwrap_or(1000)
                            {
                                tracing::warn!(e2e_ms, "canary: over the 1.0 s latency budget");
                            }
                            RunOutcome::Confirmed { e2e_ms, outcome }
                        }
                        Some((outcome, e2e_ms)) => {
                            tracing::error!(outcome = %outcome, e2e_ms, "canary: the command completed but not ok");
                            metrics().record_canary(Outcome::Error, Duration::from_millis(e2e_ms));
                            metrics().record_canary_failure("not_ok");
                            RunOutcome::Failed("not_ok")
                        }
                        None => {
                            tracing::error!("canary: result value unreadable");
                            metrics().record_canary_failure("result_unreadable");
                            RunOutcome::Failed("result_unreadable")
                        }
                    };
                }
                Ok(None) => {}
                Err(e) => {
                    tracing::error!(error = %e, "canary: valkey unavailable (result)");
                    metrics().record_canary_failure("store_unavailable");
                    return RunOutcome::Failed("store_unavailable");
                }
            }
            if tokio::time::Instant::now() >= deadline {
                let waited = command_latency::stamp::now_ms().saturating_sub(t0);
                tracing::error!(
                    waited_ms = waited,
                    timeout_ms = u64::try_from(self.timeout.as_millis()).unwrap_or(u64::MAX),
                    "canary: command not confirmed in time -- the pipeline is slow, wedged or down"
                );
                metrics().record_canary(Outcome::Timeout, Duration::from_millis(waited));
                metrics().record_canary_failure("timeout");
                return RunOutcome::Failed("timeout");
            }
            tokio::time::sleep(POLL).await;
        }
    }
}

/// The kill-switch flag resolver for the shared switch: `true` = telemetry DISABLED. Under the
/// license bypass domain `flag_enabled` reads ON for every flag, which for an opt-OUT flag would turn
/// the mechanism off -- so the bypass answer is the ENV baseline only (same inversion the other
/// kill switches use).
pub async fn kill_switch_wanted(
    client: Option<&std::sync::Arc<penguin_licensing::LicenseClient>>,
) -> bool {
    let env = command_latency::switch::env_baseline_disabled();
    match client {
        Some(c) if !c.bypass_active() => {
            env || c.flag_enabled(command_latency::switch::FLAG_KEY).await
        }
        _ => env,
    }
}

/// Whether the canary flag is ON: the ENV baseline, or the PostHog flag -- never the bypass domain
/// (a canary that drives the real pipeline must be switched on deliberately, default OFF).
pub async fn canary_enabled(
    client: Option<&std::sync::Arc<penguin_licensing::LicenseClient>>,
) -> bool {
    if canary::env_baseline_enabled() {
        return true;
    }
    match client {
        Some(c) if !c.bypass_active() => c.flag_enabled(canary::FLAG_KEY).await,
        _ => false,
    }
}

/// The production publisher: the real `publish_event` funnel (identity encryption, v3 binding,
/// XADD), inside a first-door scope so the canary's `t0` is the driver's own instant.
pub struct SpineCanaryPublisher {
    appender: penguin_spine::SpineClient,
    metrics: std::sync::Arc<dyn penguin_spine::SpineMetrics>,
    keyring: penguin_spine::KeyRing,
    active_kid: String,
    dek: crate::identity_crypto::ConfiguredDekProvider,
    identity_metrics: prometheus::IntCounterVec,
    scope: penguin_spine::Scope,
}

impl CanaryPublisher for SpineCanaryPublisher {
    async fn publish(&self, event: PlatformEvent, t0_ms: u64) -> Result<(), String> {
        let sid =
            crate::ingest::discord::source_id(event.source.as_ref().map(|s| s.account_id.as_str()));
        let workstream = crate::publish::deterministic_workstream_id(&sid);
        command_latency::first_door::with_first_door(
            t0_ms,
            command_latency::Door::Publish,
            crate::publish::publish_event(
                &self.appender,
                &*self.metrics,
                &self.keyring,
                &self.active_kid,
                &self.scope,
                &sid,
                &workstream,
                None,
                event,
                &self.dek,
                &self.identity_metrics,
            ),
        )
        .await
        .map(|_| ())
        .map_err(|e| e.to_string())
    }
}

/// A cheap 0.0..=1.0 jitter sample from the clock's sub-second nanoseconds (no RNG dependency; the
/// jitter only has to de-align replicas, it is not security relevant).
fn jitter_frac() -> f64 {
    let ns = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.subsec_nanos());
    f64::from(ns % 1_000_000) / 1_000_000.0
}

/// Spawns the canary loop: one tick per configured interval (default 60 s) with +-10% jitter,
/// doubling backoff after consecutive failures, strictly sequential (single in flight). The
/// Valkey/spine machinery is built lazily on the first enabled tick, so a deployment with the flag
/// OFF pays nothing. Every configuration problem (missing/invalid ids, wrong environment) is an
/// ERROR + `waddles_canary_failures_total{misconfigured}` per tick -- never silent.
pub fn spawn(
    config: crate::config::Config,
    ingest_metrics: std::sync::Arc<crate::telemetry::IngestMetrics>,
    dek: crate::identity_crypto::ConfiguredDekProvider,
    license: Option<std::sync::Arc<penguin_licensing::LicenseClient>>,
) {
    tokio::spawn(async move {
        let mut runner: Option<CanaryRunner<ValkeyCanaryStore, SpineCanaryPublisher>> = None;
        let mut failures: u32 = 0;
        loop {
            let base = runner
                .as_ref()
                .map_or(canary::DEFAULT_INTERVAL, CanaryRunner::interval);
            tokio::time::sleep(canary::next_delay(base, failures, jitter_frac())).await;
            if !canary_enabled(license.as_ref()).await {
                failures = 0;
                continue;
            }
            if runner.is_none() {
                match build(&config, &ingest_metrics, &dek).await {
                    Ok(r) => runner = Some(r),
                    Err(why) => {
                        tracing::error!(reason = %why, "canary enabled but cannot start");
                        metrics().record_canary_failure("misconfigured");
                        failures = 0;
                        continue;
                    }
                }
            }
            if let Some(r) = &runner {
                let outcome = r.run_once().await;
                tracing::debug!(?outcome, "canary tick finished");
                match outcome {
                    RunOutcome::Failed(_) => failures = failures.saturating_add(1),
                    RunOutcome::Confirmed { .. } => failures = 0,
                    RunOutcome::NotLeader => {}
                }
            }
        }
    });
}

async fn build(
    config: &crate::config::Config,
    ingest_metrics: &std::sync::Arc<crate::telemetry::IngestMetrics>,
    dek: &crate::identity_crypto::ConfiguredDekProvider,
) -> Result<CanaryRunner<ValkeyCanaryStore, SpineCanaryPublisher>, String> {
    let cfg = CanaryConfig::from_env().map_err(|e| e.0)?;
    let interval = cfg.interval;
    tracing::info!(
        tenant_id = cfg.tenant_id,
        community_id = cfg.community_id,
        sink_mode = ?cfg.sink_mode,
        interval_secs = interval.as_secs(),
        "canary starting (alpha only)"
    );
    if !crate::publish::scope_resolver_installed() {
        return Err(
            "the dataplane scope resolver is not installed (the canary needs v3 scope resolution)"
                .into(),
        );
    }
    let cli = &config.cli;
    let keyring = crate::resolve_binding_keyring(config, "command-latency canary")
        .ok_or("no valid ENVELOPE_BINDING_KEYS / ENVELOPE_BINDING_ACTIVE_KID")?;
    let spine_cfg =
        penguin_spine::SpineConfig::from_env().map_err(|e| format!("spine config: {e}"))?;
    let redis_client = crate::outbound::build_redis_client(&spine_cfg)
        .map_err(|e| format!("valkey client build failed ({:?})", e.kind()))?;
    let spine_metrics: std::sync::Arc<dyn penguin_spine::SpineMetrics> = ingest_metrics.clone();
    let appender = crate::connect_spine_with_retry(
        spine_cfg,
        std::sync::Arc::clone(&spine_metrics),
        ingest_metrics.as_ref(),
        "command_latency_canary",
        &crate::ReceiverFlag::default(),
        Duration::from_secs(cli.spine_connect_backoff_max_secs),
        std::time::Instant::now() + Duration::from_secs(cli.spine_connect_grace_secs),
    )
    .await
    .ok_or("spine did not connect within the grace period")?;
    let conn = redis_client
        .get_multiplexed_async_connection()
        .await
        .map_err(|e| format!("valkey connect failed ({:?})", e.kind()))?;
    let publisher = SpineCanaryPublisher {
        appender,
        metrics: spine_metrics,
        keyring,
        active_kid: cli.binding_active_kid.clone(),
        dek: dek.clone(),
        identity_metrics: ingest_metrics.identity_encryption_total.clone(),
        // Ignored when the v3 resolver is installed (checked above): the resolved canary scope wins.
        scope: penguin_spine::Scope::new("canary".to_string(), None),
    };
    Ok(CanaryRunner::new(
        cfg,
        ValkeyCanaryStore::new(conn, interval),
        publisher,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;
    use std::sync::Mutex;

    fn cfg() -> CanaryConfig {
        CanaryConfig::from_lookup(|k| {
            Some(
                match k {
                    "CANARY_TENANT_ID" => "900",
                    "CANARY_COMMUNITY_ID" => "901",
                    "CANARY_GUILD_ID" => "1000000000000000001",
                    "CANARY_CHANNEL_ID" => "1000000000000000002",
                    "CANARY_ENVIRONMENT" => "alpha",
                    "CANARY_SINK_MODE" => "swallow",
                    _ => return None,
                }
                .to_string(),
            )
        })
        .unwrap()
    }

    #[derive(Default)]
    struct FakeStore {
        lock: Mutex<Option<Result<bool, String>>>,
        results: Mutex<VecDeque<String>>,
        pop_err: Mutex<bool>,
        cleared: Mutex<u32>,
    }

    impl CanaryStore for FakeStore {
        async fn try_lock(&self) -> Result<bool, String> {
            self.lock.lock().unwrap().clone().unwrap_or(Ok(true))
        }
        async fn pop_result(&self) -> Result<Option<String>, String> {
            if *self.pop_err.lock().unwrap() {
                return Err("down".into());
            }
            Ok(self.results.lock().unwrap().pop_front())
        }
        async fn clear_results(&self) -> Result<(), String> {
            *self.cleared.lock().unwrap() += 1;
            self.results.lock().unwrap().clear();
            Ok(())
        }
    }

    #[derive(Default)]
    struct FakePublisher {
        published: Mutex<Vec<(PlatformEvent, u64)>>,
        fail: bool,
        /// Result to enqueue when published (simulates svc-action answering).
        answer: Mutex<Option<(String, std::sync::Arc<FakeStore>)>>,
    }

    impl CanaryPublisher for FakePublisher {
        async fn publish(&self, event: PlatformEvent, t0_ms: u64) -> Result<(), String> {
            if self.fail {
                return Err("spine down".into());
            }
            self.published.lock().unwrap().push((event, t0_ms));
            if let Some((v, store)) = &*self.answer.lock().unwrap() {
                store.results.lock().unwrap().push_back(v.clone());
            }
            Ok(())
        }
    }

    // `CanaryRunner` owns its store; share it through an Arc newtype for the answering publisher.
    struct Shared(std::sync::Arc<FakeStore>);
    impl CanaryStore for Shared {
        async fn try_lock(&self) -> Result<bool, String> {
            self.0.try_lock().await
        }
        async fn pop_result(&self) -> Result<Option<String>, String> {
            self.0.pop_result().await
        }
        async fn clear_results(&self) -> Result<(), String> {
            self.0.clear_results().await
        }
    }

    fn runner(
        answer: Option<&str>,
        publisher_fails: bool,
    ) -> (
        CanaryRunner<Shared, FakePublisher>,
        std::sync::Arc<FakeStore>,
    ) {
        let store = std::sync::Arc::new(FakeStore::default());
        let publisher = FakePublisher {
            fail: publisher_fails,
            ..FakePublisher::default()
        };
        if let Some(a) = answer {
            *publisher.answer.lock().unwrap() = Some((a.to_string(), store.clone()));
        }
        (
            CanaryRunner::new(cfg(), Shared(store.clone()), publisher)
                .with_timeout(Duration::from_secs(5)),
            store,
        )
    }

    #[tokio::test(start_paused = true)]
    async fn confirmed_run_reports_the_action_stage_measurement_and_clears_stale_results() {
        let (r, store) = runner(Some("ok:412"), false);
        store.results.lock().unwrap().push_back("ok:99999".into());
        let out = r.run_once().await;
        assert_eq!(
            out,
            RunOutcome::Confirmed {
                e2e_ms: 412,
                outcome: "ok".into()
            }
        );
        assert_eq!(*store.cleared.lock().unwrap(), 1);
        let published = r.publisher.published.lock().unwrap();
        assert_eq!(published.len(), 1);
        assert_eq!(published[0].0.payload["text"], "!ping");
        assert_eq!(published[0].0.platform, "discord");
    }

    #[tokio::test(start_paused = true)]
    async fn unanswered_canary_fails_loud_after_the_timeout() {
        let (r, _store) = runner(None, false);
        let start = tokio::time::Instant::now();
        let out = r.run_once().await;
        assert_eq!(out, RunOutcome::Failed("timeout"));
        assert!(start.elapsed() >= Duration::from_secs(5));
        assert!(start.elapsed() < Duration::from_secs(6));
    }

    #[tokio::test(start_paused = true)]
    async fn publish_failure_and_not_ok_and_garbage_each_fail_loud() {
        let (r, _s) = runner(None, true);
        assert_eq!(r.run_once().await, RunOutcome::Failed("publish_failed"));
        let (r, _s) = runner(Some("refused:300"), false);
        assert_eq!(r.run_once().await, RunOutcome::Failed("not_ok"));
        let (r, _s) = runner(Some("garbage"), false);
        assert_eq!(r.run_once().await, RunOutcome::Failed("result_unreadable"));
    }

    #[tokio::test(start_paused = true)]
    async fn follower_replicas_do_nothing_and_store_errors_are_loud() {
        let (r, store) = runner(Some("ok:1"), false);
        *store.lock.lock().unwrap() = Some(Ok(false));
        assert_eq!(r.run_once().await, RunOutcome::NotLeader);
        assert!(r.publisher.published.lock().unwrap().is_empty());
        *store.lock.lock().unwrap() = Some(Err("down".into()));
        assert_eq!(r.run_once().await, RunOutcome::Failed("store_unavailable"));
        *store.lock.lock().unwrap() = None;
        *store.pop_err.lock().unwrap() = true;
        let (r2, s2) = runner(None, false);
        *s2.pop_err.lock().unwrap() = true;
        assert_eq!(r2.run_once().await, RunOutcome::Failed("store_unavailable"));
    }

    fn chat_send(tenant: Option<i64>, community: Option<i64>, channel: &str) -> OutboundEntry {
        OutboundEntry {
            action: OutboundAction::ChatSend {
                channel: channel.to_string(),
                text: "pong".into(),
                origin_channel: Some(channel.to_string()),
                tenant_id: tenant,
            },
            op_id: Some("op-1".into()),
            exp_ms: None,
            community_id: community,
            tenant_id: tenant,
        }
    }

    #[test]
    fn the_sink_swallows_only_the_exact_canary_triple() {
        let c = cfg();
        let ch = "1000000000000000002";
        assert!(sink_matches_with(
            Some(&c),
            &chat_send(Some(900), Some(901), ch)
        ));
        for e in [
            chat_send(Some(900), Some(901), "1000000000000000099"),
            chat_send(Some(1), Some(901), ch),
            chat_send(Some(900), Some(2), ch),
            chat_send(None, Some(901), ch),
            chat_send(Some(900), None, ch),
        ] {
            assert!(!sink_matches_with(Some(&c), &e), "{e:?}");
        }
        assert!(
            !sink_matches_with(None, &chat_send(Some(900), Some(901), ch)),
            "no config -> never a sink"
        );
        let delete = OutboundEntry {
            action: OutboundAction::ChatDelete {
                channel: ch.into(),
                message_id: "1".into(),
                origin_channel: None,
            },
            op_id: None,
            exp_ms: None,
            community_id: Some(901),
            tenant_id: Some(900),
        };
        assert!(
            !sink_matches_with(Some(&c), &delete),
            "only chat.send is sunk"
        );
    }

    #[test]
    fn post_mode_never_swallows_so_the_reply_really_goes_to_discord() {
        let c = CanaryConfig::from_lookup(|k| {
            Some(
                match k {
                    "CANARY_TENANT_ID" => "1",
                    "CANARY_COMMUNITY_ID" => "5",
                    "CANARY_GUILD_ID" => "1000000000000000001",
                    "CANARY_CHANNEL_ID" => "1000000000000000002",
                    "CANARY_ENVIRONMENT" => "alpha",
                    _ => return None,
                }
                .to_string(),
            )
        })
        .unwrap();
        assert_eq!(c.sink_mode, SinkMode::Post);
        assert!(
            !sink_matches_with(
                Some(&c),
                &chat_send(Some(1), Some(5), "1000000000000000002")
            ),
            "default post mode: the exact canary triple is sent for real"
        );
    }

    #[test]
    fn jitter_sample_is_a_unit_fraction() {
        for _ in 0..50 {
            let j = jitter_frac();
            assert!((0.0..1.0).contains(&j), "{j}");
        }
    }

    #[test]
    fn the_synthetic_event_has_no_real_identity_or_token() {
        let c = cfg();
        let e = canary_event(&c, "2026-11-14T00:00:00.000Z");
        assert_eq!(e.actor.as_deref(), Some("waddles-latency-canary"));
        assert_eq!(e.payload["author_id"], c.guild_id);
        assert_eq!(e.payload["channel_id"], c.channel_id);
        assert_eq!(e.source.as_ref().unwrap().account_id, c.guild_id);
        assert!(!serde_json::to_string(&e.payload)
            .unwrap()
            .to_lowercase()
            .contains("token"));
    }
}
