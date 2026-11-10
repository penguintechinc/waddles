//! Periodic reconcile for APP and SERVICE bundles.
//!
//! A bundle declares `schedule: {interval_s, jitter}` in its manifest. The host owns the clock:
//! for every `(tenant, community, app)` target it publishes a `hub`/`bundle.reconcile` event onto
//! that bundle's own `hub` source stream at the declared cadence, so the bundle's ordinary
//! process stage receives it like any other event.
//!
//! * **Replica-safe.** Every replica runs a loop per target, but a Valkey lease
//!   (`SET NX PX`, ttl = 90% of the interval) lets exactly one replica publish per interval.
//! * **Single-flight** per target inside a replica; an overrunning pass is skipped, not stacked.
//! * **Backoff.** A failed publish extends the lease to an exponentially growing, capped delay so
//!   no replica retries early; a success resets it.
//! * **Fail closed.** A lease backend error never publishes (a duplicate is worse than a delay).
//! * Telemetry: a tracing span plus the shared `svc_ingest_connector_reconcile_*` series
//!   (`platform="bundle"`). Logs carry numeric ids and fixed codes only.

use std::collections::{HashMap, HashSet};
use std::future::Future;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use hub_client::bundle_schedule::{BundleScheduleClient, ScheduleFilter};
use hub_client::pb;
use penguin_spine::{KeyRing, PlatformEvent, Source, SpineMetrics};
use tracing::Instrument;

use super::reconcile_hook::{record_pass, ReconcileError, ReconcileSpec};
use crate::identity_crypto::DekProvider;
use crate::publish::{
    app_id_slug, deterministic_workstream_id, publish_event_scoped, EventAppender,
};
use crate::scope_resolver::ResolvedScope;

/// Platform of the scheduler's events.
pub const SCHEDULE_PLATFORM: &str = "hub";
/// Event type delivered to the bundle on every scheduled pass.
pub const RECONCILE_EVENT_TYPE: &str = "bundle.reconcile";
/// Machine-JWT scope hub-api requires for `BundleScheduleService.ListBundleSchedules`.
pub const SCHEDULES_JWT_SCOPE: &str = hub_client::bundle_schedule::SCOPE;
/// Longest backoff after consecutive failures.
pub const MAX_BACKOFF: Duration = Duration::from_secs(3600);
const KEY_PREFIX: &str = "waddles:ingest:bundle-reconcile:";

/// One scheduled `(tenant, community, app)`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BundleScheduleTarget {
    /// Authoritative numeric scope plus the display strings the stream key needs.
    pub resolved: ResolvedScope,
    /// The bundle's `app_id`.
    pub app_id: String,
    /// Declared cadence.
    pub spec: ReconcileSpec,
}

/// A target was malformed.
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
#[error("invalid schedule target: {0}")]
pub struct InvalidTarget(pub &'static str);

impl BundleScheduleTarget {
    /// Validated target: positive tenant, non-negative community, key-safe `app_id`.
    ///
    /// # Errors
    /// [`InvalidTarget`] naming the first violated rule.
    pub fn new(
        resolved: ResolvedScope,
        app_id: &str,
        spec: ReconcileSpec,
    ) -> Result<Self, InvalidTarget> {
        if resolved.tenant_id < 1 {
            return Err(InvalidTarget("tenant_id"));
        }
        if resolved.community_id < 0 {
            return Err(InvalidTarget("community_id"));
        }
        let ok = !app_id.is_empty()
            && app_id.len() <= 128
            && app_id.chars().all(|c| {
                c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(c, '.' | '_' | '-')
            });
        if !ok {
            return Err(InvalidTarget("app_id"));
        }
        Ok(Self {
            resolved,
            app_id: app_id.to_owned(),
            spec,
        })
    }

    /// Stable identity (also the lease key suffix and the single-flight key).
    #[must_use]
    pub fn key(&self) -> String {
        format!(
            "{}.{}.{}",
            self.resolved.tenant_id, self.resolved.community_id, self.app_id
        )
    }

    /// The `hub` source id whose stream only this bundle consumes.
    #[must_use]
    pub fn source_id(&self) -> String {
        app_id_slug(&self.app_id)
    }
}

/// A lease backend failure (type only; never echoes connection details).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("schedule lease unavailable: {0}")]
pub struct ScheduleLeaseError(pub String);

/// Cross-replica exclusivity for one scheduled slot.
pub trait ScheduleLease: Send + Sync {
    /// `true` iff this replica now holds `key` for `ttl`.
    fn try_acquire(
        &self,
        key: &str,
        ttl: Duration,
    ) -> impl Future<Output = Result<bool, ScheduleLeaseError>> + Send;
    /// Re-arms `key`'s ttl, only if this replica still holds it.
    fn extend(
        &self,
        key: &str,
        ttl: Duration,
    ) -> impl Future<Output = Result<bool, ScheduleLeaseError>> + Send;
}

const EXTEND_LUA: &str = "if redis.call('GET', KEYS[1]) == ARGV[1] then \
return redis.call('PEXPIRE', KEYS[1], ARGV[2]) else return 0 end";

/// Valkey [`ScheduleLease`] identified by a per-boot replica id.
pub struct ValkeyScheduleLease {
    conn: redis::aio::MultiplexedConnection,
    replica_id: String,
    extend: redis::Script,
}

impl ValkeyScheduleLease {
    /// Wraps a connection with an explicit replica id.
    #[must_use]
    pub fn with_replica_id(conn: redis::aio::MultiplexedConnection, replica_id: String) -> Self {
        Self {
            conn,
            replica_id,
            extend: redis::Script::new(EXTEND_LUA),
        }
    }

    /// Wraps a connection with a fresh random replica id.
    #[must_use]
    pub fn new(conn: redis::aio::MultiplexedConnection) -> Self {
        Self::with_replica_id(conn, uuid::Uuid::new_v4().to_string())
    }
}

fn ttl_ms(ttl: Duration) -> Result<u64, ScheduleLeaseError> {
    match u64::try_from(ttl.as_millis()) {
        Ok(ms) if ms > 0 => Ok(ms),
        _ => Err(ScheduleLeaseError("invalid ttl".to_owned())),
    }
}

async fn bounded<T>(
    fut: impl Future<Output = Result<T, redis::RedisError>>,
) -> Result<T, ScheduleLeaseError> {
    match tokio::time::timeout(Duration::from_secs(2), fut).await {
        Ok(Ok(v)) => Ok(v),
        // Kind only: a redis error Display can echo the URL and password.
        Ok(Err(e)) => Err(ScheduleLeaseError(format!("{:?}", e.kind()))),
        Err(_) => Err(ScheduleLeaseError("timeout".to_owned())),
    }
}

impl ScheduleLease for ValkeyScheduleLease {
    async fn try_acquire(&self, key: &str, ttl: Duration) -> Result<bool, ScheduleLeaseError> {
        let ms = ttl_ms(ttl)?;
        let mut conn = self.conn.clone();
        let res: Option<String> = bounded(
            redis::cmd("SET")
                .arg(format!("{KEY_PREFIX}{key}"))
                .arg(&self.replica_id)
                .arg("NX")
                .arg("PX")
                .arg(ms)
                .query_async(&mut conn),
        )
        .await?;
        Ok(res.is_some())
    }

    async fn extend(&self, key: &str, ttl: Duration) -> Result<bool, ScheduleLeaseError> {
        let ms = ttl_ms(ttl)?;
        let mut conn = self.conn.clone();
        let n: i64 = bounded(
            self.extend
                .key(format!("{KEY_PREFIX}{key}"))
                .arg(&self.replica_id)
                .arg(ms)
                .invoke_async(&mut conn),
        )
        .await?;
        Ok(n == 1)
    }
}

/// Where a reconcile event goes.
pub trait ReconcileSink: Send + Sync {
    /// Publishes `event` for `target`.
    fn deliver(
        &self,
        target: &BundleScheduleTarget,
        event: PlatformEvent,
    ) -> impl Future<Output = Result<(), ReconcileError>> + Send;
}

/// Publishes onto the spine under the target's own resolved scope (schema-v3 envelope).
pub struct SpineReconcileSink<A: EventAppender, D: DekProvider> {
    /// The `XADD` seam.
    pub appender: Arc<A>,
    /// Spine metrics.
    pub metrics: Arc<dyn SpineMetrics>,
    /// Binding-MAC keyring.
    pub keyring: Arc<KeyRing>,
    /// Active key version.
    pub active_kid: String,
    /// Identity DEK provider (the event carries no identity field, so it is never asked).
    pub dek: Arc<D>,
    /// Identity-encryption counter.
    pub identity_metrics: prometheus::IntCounterVec,
}

impl<A: EventAppender, D: DekProvider> ReconcileSink for SpineReconcileSink<A, D> {
    async fn deliver(
        &self,
        target: &BundleScheduleTarget,
        event: PlatformEvent,
    ) -> Result<(), ReconcileError> {
        let source_id = target.source_id();
        publish_event_scoped(
            &target.resolved,
            &*self.appender,
            &*self.metrics,
            &self.keyring,
            &self.active_kid,
            &source_id,
            &deterministic_workstream_id(&source_id),
            None,
            event,
            &*self.dek,
            &self.identity_metrics,
        )
        .await
        .map(|_| ())
        .map_err(|_| ReconcileError("publish_failed"))
    }
}

/// The event a bundle receives. Numeric ids and cadence only: no identity, no secret.
#[must_use]
pub fn reconcile_event(target: &BundleScheduleTarget, now_rfc3339: &str) -> PlatformEvent {
    let mut payload = serde_json::Map::new();
    payload.insert("app_id".into(), target.app_id.clone().into());
    payload.insert("community_id".into(), target.resolved.community_id.into());
    payload.insert("interval_s".into(), target.spec.interval().as_secs().into());
    payload.insert("scheduled_at".into(), now_rfc3339.into());
    PlatformEvent {
        platform: SCHEDULE_PLATFORM.to_owned(),
        event_type: RECONCILE_EVENT_TYPE.to_owned(),
        actor: None,
        payload,
        occurred_at: now_rfc3339.to_owned(),
        source: Some(Source {
            platform: SCHEDULE_PLATFORM.to_owned(),
            account_id: "scheduler".to_owned(),
            channel_id: None,
        }),
    }
}

/// What one scheduled attempt did.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TickOutcome {
    /// This replica won the slot and published.
    Fired,
    /// Another pass of the same target is running in this replica.
    SkippedInFlight,
    /// Another replica holds the slot.
    SkippedLease,
    /// The lease backend or the publish failed (fixed code).
    Failed(&'static str),
}

struct FlightGuard {
    set: Arc<Mutex<HashSet<String>>>,
    key: String,
}

impl Drop for FlightGuard {
    fn drop(&mut self) {
        let mut g = self.set.lock().unwrap_or_else(|p| p.into_inner());
        g.remove(&self.key);
    }
}

/// Drives scheduled reconcile passes for APP/SERVICE bundles.
pub struct ScheduleRunner<L, S> {
    lease: L,
    sink: S,
    in_flight: Arc<Mutex<HashSet<String>>>,
    failures: Mutex<HashMap<String, u32>>,
}

/// Exponential backoff after `fails` consecutive failures, capped at [`MAX_BACKOFF`].
#[must_use]
pub fn backoff(spec: &ReconcileSpec, fails: u32) -> Duration {
    let factor = 1u32 << fails.min(10);
    spec.interval().saturating_mul(factor).min(MAX_BACKOFF)
}

impl<L: ScheduleLease, S: ReconcileSink> ScheduleRunner<L, S> {
    /// A runner over `lease` and `sink`.
    #[must_use]
    pub fn new(lease: L, sink: S) -> Self {
        Self {
            lease,
            sink,
            in_flight: Arc::new(Mutex::new(HashSet::new())),
            failures: Mutex::new(HashMap::new()),
        }
    }

    fn fails(&self, key: &str) -> u32 {
        let g = self.failures.lock().unwrap_or_else(|p| p.into_inner());
        g.get(key).copied().unwrap_or(0)
    }

    fn set_fails(&self, key: &str, n: u32) {
        let mut g = self.failures.lock().unwrap_or_else(|p| p.into_inner());
        if n == 0 {
            g.remove(key);
        } else {
            g.insert(key.to_owned(), n);
        }
    }

    /// How long this replica should sleep before the next attempt for `target`.
    #[must_use]
    pub fn delay(&self, target: &BundleScheduleTarget, unit: f64) -> Duration {
        let base = target.spec.next_delay(unit);
        let fails = self.fails(&target.key());
        if fails == 0 {
            base
        } else {
            base.max(backoff(&target.spec, fails))
        }
    }

    /// One attempt: single-flight, win the lease, publish. Never panics, never double-fires.
    pub async fn tick(&self, target: &BundleScheduleTarget) -> TickOutcome {
        let key = target.key();
        let inserted = {
            let mut g = self.in_flight.lock().unwrap_or_else(|p| p.into_inner());
            g.insert(key.clone())
        };
        if !inserted {
            record_pass("bundle", "skipped_in_flight", None);
            return TickOutcome::SkippedInFlight;
        }
        let _guard = FlightGuard {
            set: Arc::clone(&self.in_flight),
            key: key.clone(),
        };
        let lease_ttl = target.spec.interval().mul_f64(0.9);
        match self.lease.try_acquire(&key, lease_ttl).await {
            Ok(true) => {}
            Ok(false) => {
                record_pass("bundle", "skipped_lease", None);
                return TickOutcome::SkippedLease;
            }
            Err(_) => {
                record_pass("bundle", "failed", None);
                tracing::warn!(
                    tenant_id = target.resolved.tenant_id,
                    community_id = target.resolved.community_id,
                    app_id = %target.app_id,
                    code = "lease_unavailable",
                    "bundle reconcile not fired"
                );
                return TickOutcome::Failed("lease_unavailable");
            }
        }
        let start = Instant::now();
        let span = tracing::info_span!(
            "bundle_reconcile",
            tenant_id = target.resolved.tenant_id,
            community_id = target.resolved.community_id,
            app_id = %target.app_id
        );
        let event = reconcile_event(
            target,
            &chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true),
        );
        let res = self.sink.deliver(target, event).instrument(span).await;
        match res {
            Ok(()) => {
                self.set_fails(&key, 0);
                record_pass("bundle", "ok", Some(start.elapsed()));
                TickOutcome::Fired
            }
            Err(ReconcileError(code)) => {
                let n = self.fails(&key).saturating_add(1);
                self.set_fails(&key, n);
                // Hold the slot for the backoff so no replica retries early.
                let _ = self.lease.extend(&key, backoff(&target.spec, n)).await;
                record_pass("bundle", "failed", Some(start.elapsed()));
                tracing::warn!(
                    tenant_id = target.resolved.tenant_id,
                    app_id = %target.app_id,
                    code,
                    fails = n,
                    "bundle reconcile publish failed"
                );
                TickOutcome::Failed(code)
            }
        }
    }
}

fn random_unit() -> f64 {
    let b = uuid::Uuid::new_v4().as_u128();
    // Top 53 bits -> [0, 1).
    #[allow(clippy::cast_precision_loss)]
    {
        ((b >> 75) as f64) / ((1u64 << 53) as f64)
    }
}

/// Why listing targets failed (fixed code; the previous target set stays in force).
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
#[error("schedule source failed ({0})")]
pub struct SourceError(pub &'static str);

/// The authoritative list of scheduled targets.
pub trait ScheduleSource: Send + Sync {
    /// The complete current target set.
    fn list(&self) -> impl Future<Output = Result<Vec<BundleScheduleTarget>, SourceError>> + Send;
}

/// Keeps one loop per target alive: spawns new ones, aborts removed or re-declared ones.
/// A failed listing leaves the running set untouched (a short list must not stop healthy loops).
pub async fn run_manager<L, S, T>(
    runner: Arc<ScheduleRunner<L, S>>,
    source: T,
    poll: Duration,
    shutdown: impl Future<Output = ()> + Send,
) where
    L: ScheduleLease + 'static,
    S: ReconcileSink + 'static,
    T: ScheduleSource,
{
    let mut running: HashMap<String, (BundleScheduleTarget, tokio::task::JoinHandle<()>)> =
        HashMap::new();
    tokio::pin!(shutdown);
    loop {
        match source.list().await {
            Ok(targets) => {
                let want: HashMap<String, BundleScheduleTarget> =
                    targets.into_iter().map(|t| (t.key(), t)).collect();
                running.retain(|k, (t, h)| {
                    let keep = want.get(k) == Some(t);
                    if !keep {
                        h.abort();
                    }
                    keep
                });
                for (k, t) in want {
                    if running.contains_key(&k) {
                        continue;
                    }
                    let r = Arc::clone(&runner);
                    let tt = t.clone();
                    let h = tokio::spawn(async move {
                        loop {
                            tokio::time::sleep(r.delay(&tt, random_unit())).await;
                            r.tick(&tt).await;
                        }
                    });
                    running.insert(k, (t, h));
                }
            }
            Err(SourceError(code)) => {
                tracing::warn!(code, "bundle schedule listing failed; keeping current set");
            }
        }
        tokio::select! {
            () = tokio::time::sleep(poll) => {}
            () = &mut shutdown => break,
        }
    }
    for (_, (_, h)) in running {
        h.abort();
    }
}

/// hub-api backed [`ScheduleSource`]: the `BundleScheduleService.ListBundleSchedules` gRPC call on
/// hub-api's internal listener (TLS, machine JWT with scope [`SCHEDULES_JWT_SCOPE`]) over one
/// long-lived channel. A malformed row is dropped and counted in the log, never fatal to the rest.
pub struct HubScheduleSource {
    client: BundleScheduleClient,
}

/// Why [`HubScheduleSource::connect`] refused to start.
#[derive(Debug, thiserror::Error)]
pub enum HubScheduleConnectError {
    /// The endpoint is not `https://` -- hub-api's internal gRPC listener is TLS-only.
    #[error("HUB_API_GRPC_ENDPOINT must be an https:// URL (hub-api's internal gRPC is TLS-only)")]
    NotTls,
    /// Dialing/authenticating the channel failed (CA unreadable, unreachable, untrusted).
    #[error("hub-api gRPC channel unavailable: {0}")]
    Client(#[from] hub_client::HubClientError),
}

/// Converts one wire target into a validated [`BundleScheduleTarget`] (`Err` = which field).
fn into_target(t: pb::BundleScheduleTarget) -> Result<BundleScheduleTarget, &'static str> {
    let spec = ReconcileSpec::new(Duration::from_secs(t.interval_s))
        .map_err(|_| "interval")?
        .with_jitter(t.jitter)
        .map_err(|_| "jitter")?;
    let resolved = ResolvedScope {
        tenant_id: t.tenant_id,
        community_id: i32::try_from(t.community_id).map_err(|_| "community")?,
        tenant_slug: t.tenant_slug,
        community_name: t.community_name,
    };
    BundleScheduleTarget::new(resolved, &t.app_id, spec).map_err(|e| e.0)
}

impl HubScheduleSource {
    /// Dials hub-api's internal gRPC `endpoint` (`HUB_API_GRPC_ENDPOINT`, `https://` only) once;
    /// the channel then lives as long as the source. `ca_cert_path` pins the CA that signed
    /// hub-api's certificate (`HUB_API_GRPC_CA_FILE`).
    ///
    /// # Errors
    /// [`HubScheduleConnectError`] -- fatal at boot, never a silent fallback.
    pub async fn connect(
        endpoint: &str,
        token_endpoint: &str,
        sa_token_path: &str,
        ca_cert_path: Option<&str>,
    ) -> Result<Self, HubScheduleConnectError> {
        if !endpoint.trim().to_ascii_lowercase().starts_with("https://") {
            return Err(HubScheduleConnectError::NotTls);
        }
        let client = BundleScheduleClient::connect(
            endpoint.trim(),
            token_endpoint,
            sa_token_path,
            ca_cert_path,
        )
        .await?;
        Ok(Self { client })
    }

    /// Wraps an already-built client (loopback servers in tests).
    #[must_use]
    pub fn from_client(client: BundleScheduleClient) -> Self {
        Self { client }
    }
}

impl ScheduleSource for HubScheduleSource {
    async fn list(&self) -> Result<Vec<BundleScheduleTarget>, SourceError> {
        let targets = self
            .client
            .list(&ScheduleFilter::default())
            .await
            .map_err(|e| {
                tracing::warn!(error = %e, "bundle schedule listing failed");
                SourceError(match e {
                    hub_client::HubClientError::Auth(_) => "token",
                    hub_client::HubClientError::CircuitOpen => "circuit_open",
                    hub_client::HubClientError::Grpc(_) => "status",
                    _ => "transport",
                })
            })?;
        let total = targets.len();
        let out: Vec<_> = targets
            .into_iter()
            .filter_map(|t| into_target(t).ok())
            .collect();
        if out.len() != total {
            tracing::warn!(total, valid = out.len(), "dropped malformed schedule rows");
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    #[derive(Default, Clone)]
    struct MemLease {
        held: Arc<Mutex<HashMap<String, String>>>,
        owner: String,
        fail: bool,
        extended: Arc<Mutex<Vec<(String, Duration)>>>,
    }

    impl MemLease {
        fn peer(&self, owner: &str) -> Self {
            Self {
                owner: owner.into(),
                ..self.clone()
            }
        }
    }

    impl ScheduleLease for MemLease {
        async fn try_acquire(&self, key: &str, _ttl: Duration) -> Result<bool, ScheduleLeaseError> {
            if self.fail {
                return Err(ScheduleLeaseError("down".into()));
            }
            let mut g = self.held.lock().unwrap();
            if g.contains_key(key) {
                return Ok(false);
            }
            g.insert(key.into(), self.owner.clone());
            Ok(true)
        }
        async fn extend(&self, key: &str, ttl: Duration) -> Result<bool, ScheduleLeaseError> {
            self.extended.lock().unwrap().push((key.into(), ttl));
            Ok(true)
        }
    }

    #[derive(Clone, Default)]
    struct Rec {
        events: Arc<Mutex<Vec<PlatformEvent>>>,
        fail: bool,
        calls: Arc<AtomicUsize>,
    }

    impl ReconcileSink for Rec {
        async fn deliver(
            &self,
            _t: &BundleScheduleTarget,
            e: PlatformEvent,
        ) -> Result<(), ReconcileError> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            if self.fail {
                return Err(ReconcileError("publish_failed"));
            }
            self.events.lock().unwrap().push(e);
            Ok(())
        }
    }

    fn target(app: &str) -> BundleScheduleTarget {
        BundleScheduleTarget::new(
            ResolvedScope {
                tenant_id: 4,
                community_id: 9,
                tenant_slug: "acme".into(),
                community_name: Some("main".into()),
            },
            app,
            ReconcileSpec::new(Duration::from_secs(60)).unwrap(),
        )
        .unwrap()
    }

    #[test]
    fn targets_are_validated() {
        let spec = ReconcileSpec::new(Duration::from_secs(30)).unwrap();
        let ok = ResolvedScope {
            tenant_id: 1,
            community_id: 0,
            tenant_slug: "t".into(),
            community_name: None,
        };
        assert!(BundleScheduleTarget::new(ok.clone(), "waddles.core.calendar", spec).is_ok());
        for bad in ["", "Has:Colon", "a b", &"x".repeat(129)] {
            assert_eq!(
                BundleScheduleTarget::new(ok.clone(), bad, spec).unwrap_err(),
                InvalidTarget("app_id")
            );
        }
        let mut z = ok.clone();
        z.tenant_id = 0;
        assert_eq!(
            BundleScheduleTarget::new(z, "a", spec).unwrap_err(),
            InvalidTarget("tenant_id")
        );
        assert_eq!(target("a.b").key(), "4.9.a.b");
    }

    #[test]
    fn event_is_pii_free_and_shaped() {
        let e = reconcile_event(&target("a.b"), "2026-11-07T00:00:00.000Z");
        assert_eq!(
            (e.platform.as_str(), e.event_type.as_str()),
            ("hub", "bundle.reconcile")
        );
        assert!(e.actor.is_none());
        let mut keys: Vec<_> = e.payload.keys().cloned().collect();
        keys.sort();
        assert_eq!(
            keys,
            ["app_id", "community_id", "interval_s", "scheduled_at"]
        );
        assert_eq!(e.payload["interval_s"], 60);
    }

    #[tokio::test]
    async fn exactly_one_replica_fires_per_slot() {
        let a = MemLease::default();
        let b = a.peer("b");
        let sink = Rec::default();
        let ra = ScheduleRunner::new(a, sink.clone());
        let rb = ScheduleRunner::new(b, sink.clone());
        let t = target("x.y");
        let (oa, ob) = tokio::join!(ra.tick(&t), rb.tick(&t));
        let mut outs = [oa, ob];
        outs.sort_by_key(|o| matches!(o, TickOutcome::SkippedLease));
        assert_eq!(outs, [TickOutcome::Fired, TickOutcome::SkippedLease]);
        assert_eq!(sink.events.lock().unwrap().len(), 1);
    }

    #[tokio::test]
    async fn lease_outage_fails_closed() {
        let l = MemLease {
            fail: true,
            ..MemLease::default()
        };
        let sink = Rec::default();
        let r = ScheduleRunner::new(l, sink.clone());
        assert_eq!(
            r.tick(&target("x.y")).await,
            TickOutcome::Failed("lease_unavailable")
        );
        assert_eq!(sink.calls.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn failure_backs_off_and_holds_the_slot_then_success_resets() {
        let l = MemLease::default();
        let ext = Arc::clone(&l.extended);
        let held = Arc::clone(&l.held);
        let t = target("x.y");
        let bad = Rec {
            fail: true,
            ..Rec::default()
        };
        let r = ScheduleRunner::new(l.clone(), bad);
        assert_eq!(r.tick(&t).await, TickOutcome::Failed("publish_failed"));
        assert_eq!(ext.lock().unwrap()[0].1, Duration::from_secs(120));
        assert!(r.delay(&t, 0.0) >= Duration::from_secs(120));
        held.lock().unwrap().clear();
        assert_eq!(r.tick(&t).await, TickOutcome::Failed("publish_failed"));
        assert_eq!(ext.lock().unwrap()[1].1, Duration::from_secs(240));
        // Cap.
        assert_eq!(backoff(&t.spec, 40), MAX_BACKOFF);
        // Success on a healthy sink resets the counter.
        let good = ScheduleRunner::new(l, Rec::default());
        good.set_fails(&t.key(), 3);
        held.lock().unwrap().clear();
        assert_eq!(good.tick(&t).await, TickOutcome::Fired);
        assert_eq!(good.fails(&t.key()), 0);
    }

    #[tokio::test]
    async fn same_target_is_single_flight() {
        struct Slow(Arc<tokio::sync::Semaphore>, Arc<AtomicUsize>);
        impl ReconcileSink for Slow {
            async fn deliver(
                &self,
                _t: &BundleScheduleTarget,
                _e: PlatformEvent,
            ) -> Result<(), ReconcileError> {
                self.1.fetch_add(1, Ordering::SeqCst);
                self.0.acquire().await.unwrap().forget();
                Ok(())
            }
        }
        let gate = Arc::new(tokio::sync::Semaphore::new(0));
        let calls = Arc::new(AtomicUsize::new(0));
        let r = Arc::new(ScheduleRunner::new(
            MemLease::default(),
            Slow(Arc::clone(&gate), Arc::clone(&calls)),
        ));
        let t = target("x.y");
        let first = {
            let (r, t) = (Arc::clone(&r), t.clone());
            tokio::spawn(async move { r.tick(&t).await })
        };
        while calls.load(Ordering::SeqCst) == 0 {
            tokio::task::yield_now().await;
        }
        assert_eq!(r.tick(&t).await, TickOutcome::SkippedInFlight);
        gate.add_permits(1);
        assert_eq!(first.await.unwrap(), TickOutcome::Fired);
    }

    fn wire(interval_s: u64, jitter: f64, community_id: i64) -> pb::BundleScheduleTarget {
        pb::BundleScheduleTarget {
            tenant_id: 1,
            community_id,
            tenant_slug: "t".into(),
            community_name: Some("c".into()),
            app_id: "a.b".into(),
            interval_s,
            jitter,
        }
    }

    #[test]
    fn hub_rows_are_validated() {
        let ok = into_target(wire(45, 0.1, 2)).unwrap();
        assert_eq!(ok.resolved.community_id, 2);
        assert_eq!(into_target(wire(5, 0.1, 2)).unwrap_err(), "interval");
        assert_eq!(into_target(wire(45, 7.0, 2)).unwrap_err(), "jitter");
        assert_eq!(
            into_target(wire(45, 0.1, i64::from(i32::MAX) + 1)).unwrap_err(),
            "community"
        );
    }

    #[tokio::test]
    async fn plaintext_or_non_url_endpoint_is_refused_before_dialing() {
        for bad in ["http://hub:50204", "hub:50204", ""] {
            let err = HubScheduleSource::connect(bad, "t", "s", None).await;
            assert!(matches!(err, Err(HubScheduleConnectError::NotTls)), "{bad}");
        }
    }

    /// Real tonic `BundleScheduleService` on loopback answering with `reply`; returns a
    /// [`HubScheduleSource`] wired (real machine-JWT bootstrap against an in-process gRPC mint)
    /// to it.
    async fn grpc_source(
        reply: Result<Vec<pb::BundleScheduleTarget>, tonic::Status>,
    ) -> (HubScheduleSource, String) {
        use hub_client::pb::bundle_schedule_service_server::{
            BundleScheduleService, BundleScheduleServiceServer,
        };
        struct Fake(Result<Vec<pb::BundleScheduleTarget>, tonic::Status>);
        #[tonic::async_trait]
        impl BundleScheduleService for Fake {
            async fn list_bundle_schedules(
                &self,
                request: tonic::Request<pb::ListBundleSchedulesRequest>,
            ) -> Result<tonic::Response<pb::ListBundleSchedulesResponse>, tonic::Status>
            {
                let bearer = request
                    .metadata()
                    .get("authorization")
                    .and_then(|v| v.to_str().ok())
                    .unwrap_or_default();
                assert_eq!(bearer, "Bearer machine.jwt.value");
                self.0.clone().map(|targets| {
                    tonic::Response::new(pb::ListBundleSchedulesResponse { targets })
                })
            }
        }
        // The machine-JWT mint is gRPC: a real in-process `ServiceAuthService`.
        let tokens = service_auth::test_support::MockHub::builder()
            .mint(vec![service_auth::test_support::MintReply::token(
                "machine.jwt.value",
                3600,
            )])
            .start_detached()
            .await;
        let sa = std::env::temp_dir().join(format!("svc-ingest-sched-sa-{}", uuid::Uuid::new_v4()));
        std::fs::write(&sa, "projected-sa-token").unwrap();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            tonic::transport::Server::builder()
                .add_service(BundleScheduleServiceServer::new(Fake(reply)))
                .serve_with_incoming(tonic::transport::server::TcpIncoming::from(listener))
                .await
                .unwrap();
        });
        let channel = tonic::transport::Endpoint::from_shared(format!("http://{addr}"))
            .unwrap()
            .connect()
            .await
            .unwrap();
        let client = BundleScheduleClient::with_channel(
            channel,
            Arc::new(service_auth::MachineJwtClient::new(
                tokens.clone(),
                sa.to_string_lossy().to_string(),
                SCHEDULES_JWT_SCOPE,
            )),
        );
        (HubScheduleSource::from_client(client), tokens)
    }

    #[tokio::test]
    async fn grpc_source_lists_valid_rows_and_drops_malformed_ones() {
        let (source, _tokens) = grpc_source(Ok(vec![
            wire(45, 0.1, 2),
            wire(5, 0.1, 2), // interval too short -> dropped, not fatal
        ]))
        .await;
        let listed = source.list().await.unwrap();
        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0].app_id, "a.b");
        assert_eq!(listed[0].resolved.tenant_id, 1);
        assert_eq!(listed[0].resolved.community_id, 2);
        assert_eq!(listed[0].resolved.community_name.as_deref(), Some("c"));
    }

    #[tokio::test]
    async fn grpc_source_surfaces_a_refusal_as_a_source_error() {
        let (source, _tokens) =
            grpc_source(Err(tonic::Status::permission_denied("forbidden"))).await;
        assert_eq!(source.list().await.unwrap_err().0, "status");
    }

    #[tokio::test]
    async fn manager_spawns_and_stops_loops_and_survives_a_failed_listing() {
        type Listing = Result<Vec<BundleScheduleTarget>, SourceError>;
        struct Src(Arc<Mutex<Vec<Listing>>>);
        impl ScheduleSource for Src {
            async fn list(&self) -> Result<Vec<BundleScheduleTarget>, SourceError> {
                let mut g = self.0.lock().unwrap();
                if g.len() > 1 {
                    g.remove(0)
                } else {
                    g[0].clone()
                }
            }
        }
        let sink = Rec::default();
        let runner = Arc::new(ScheduleRunner::new(MemLease::default(), sink.clone()));
        let mut t = target("x.y");
        t.spec = ReconcileSpec::new(Duration::from_secs(30)).unwrap();
        let src = Src(Arc::new(Mutex::new(vec![
            Ok(vec![t]),
            Err(SourceError("transport")),
            Ok(vec![]),
        ])));
        let (tx, rx) = tokio::sync::oneshot::channel::<()>();
        let h = tokio::spawn(run_manager(runner, src, Duration::from_millis(10), async {
            let _ = rx.await;
        }));
        tokio::time::sleep(Duration::from_millis(100)).await;
        tx.send(()).unwrap();
        tokio::time::timeout(Duration::from_secs(2), h)
            .await
            .unwrap()
            .unwrap();
    }
}
