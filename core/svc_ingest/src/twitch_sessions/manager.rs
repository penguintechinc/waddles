//! Desired-state reconciliation of per-community Twitch sessions.
//!
//! Each pass diffs hub-api's complete binding list against the running
//! sessions (keyed by numeric `(community_id, connection_id)`, never by
//! channel) and makes start/stop/restart decisions. A failed discovery makes
//! NO decisions: a transient hub error must never tear down healthy sessions.
//! Ownership is a Valkey lease renewed every pass; the lease has no fencing
//! token, so a session is stopped as soon as its lease is known lost (or after
//! [`MAX_RENEW_FAILURES`] consecutive renew errors, well inside [`LEASE_TTL`]).
//! Logs and metrics carry numeric ids and counts only.

use std::collections::{BTreeMap, HashMap};
use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;

use connection_resolver::ConnectionResolve;
use penguin_spine::{KeyRing, Scope, SpineMetrics};
use tokio::sync::{oneshot, watch};
use tokio::task::{AbortHandle, JoinHandle};
use tokio::time::Instant;

use crate::ingest::twitch::{run_with_connector, SessionLabel};
use crate::publish::EventAppender;
use crate::twitch_sessions::connector::ResolvingConnector;
use crate::twitch_sessions::lease::SessionLease;
use crate::twitch_sessions::metrics::{DiscoveryOutcome, LeaseOutcome, SessionMetrics};
use crate::twitch_sessions::spec::{discover, DiscoverError, SessionSpec};

/// Time between reconcile passes (plus up to 10% jitter).
pub const RECONCILE_INTERVAL: Duration = Duration::from_secs(60);
/// Default lease lifetime: `(MAX_RENEW_FAILURES + 2) x RECONCILE_INTERVAL +
/// DISCOVERY_TIMEOUT` (window + one interval), so the failed renews the
/// manager tolerates never outrun the session's self-stop deadline
/// (see `SessionManager::lease_ttl`).
pub const LEASE_TTL: Duration = Duration::from_secs(250);
/// Wait after a session ends on its own before it is restarted.
pub const RESTART_COOLDOWN: Duration = Duration::from_secs(120);
/// Bound on one binding discovery (each page call is bounded by this too).
const DISCOVERY_TIMEOUT: Duration = Duration::from_secs(10);
/// Bound on a session task finishing after its shutdown signal.
const STOP_TIMEOUT: Duration = Duration::from_secs(10);
/// Consecutive renew errors after which a session is stopped (fail-closed).
const MAX_RENEW_FAILURES: u32 = 2;
/// Per-call bound on credential resolution for real sessions.
const RESOLVE_TIMEOUT: Duration = Duration::from_secs(10);

/// Why a session ended (int-coded).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum SessionEnd {
    /// Stopped by the manager (binding removed/changed, or process shutdown).
    Shutdown = 1,
    /// The session stopped itself on a non-retryable error.
    NonRetryable = 2,
    /// The lease was lost (or could not be renewed), so the session was stopped.
    LeaseLost = 3,
}

/// Counts from one reconcile pass.
#[derive(Clone, Copy, PartialEq, Eq, Debug, Default)]
pub struct ReconcileReport {
    /// Sessions in the desired set (0 when discovery failed).
    pub desired: u32,
    /// New sessions spawned.
    pub started: u32,
    /// Sessions stopped (binding removed or lease lost).
    pub stopped: u32,
    /// Sessions replaced because the spec changed or the cooldown elapsed.
    pub restarted: u32,
    /// Live sessions kept with a renewed lease.
    pub kept: u32,
    /// Sessions not started because the lease was held elsewhere or unavailable.
    pub lease_denied: u32,
    /// Discovery failed: no start/stop/restart decisions were made.
    pub discovery_failed: bool,
}

/// Spawns one session task; the seam that lets reconcile be tested without
/// sockets.
pub trait SessionRunner: Send + Sync {
    /// Starts the session for `spec`; it must stop when `shutdown` resolves
    /// (or its sender is dropped). The handle yields how it ended.
    fn spawn(&self, spec: SessionSpec, shutdown: oneshot::Receiver<()>) -> JoinHandle<SessionEnd>;
}

/// Everything the manager needs besides the resolver and lease.
pub struct SessionDeps {
    /// How sessions are spawned (real: [`RealRunner`]).
    pub runner: Arc<dyn SessionRunner>,
    /// Telemetry sink.
    pub metrics: Arc<dyn SessionMetrics>,
}

/// The production [`SessionRunner`]: runs `run_with_connector` over a
/// [`ResolvingConnector`] so every (re)connect re-resolves its credentials.
pub struct RealRunner<R, A, M, Dk> {
    resolver: Arc<R>,
    appender: Arc<A>,
    spine_metrics: Arc<M>,
    keyring: Arc<KeyRing>,
    active_kid: Arc<str>,
    scope: Arc<Scope>,
    host: String,
    port: u16,
    use_tls: bool,
    session_metrics: Option<Arc<dyn SessionMetrics>>,
    resolve_timeout: Duration,
    dek_provider: Arc<Dk>,
    identity_metrics: Arc<prometheus::IntCounterVec>,
}

impl<R, A, M, Dk> RealRunner<R, A, M, Dk> {
    /// Overrides the per-call credential-resolution bound (default 10s).
    #[must_use]
    pub fn with_resolve_timeout(mut self, timeout: Duration) -> Self {
        self.resolve_timeout = timeout;
        self
    }

    /// Attaches the telemetry sink that records resolve failures from every
    /// session's connector.
    #[must_use]
    pub fn with_session_metrics(mut self, metrics: Arc<dyn SessionMetrics>) -> Self {
        self.session_metrics = Some(metrics);
        self
    }

    /// Bundles the shared publish/IRC dependencies of every session.
    #[allow(clippy::too_many_arguments)] // one flat constructor for boot wiring
    pub fn new(
        resolver: Arc<R>,
        appender: Arc<A>,
        spine_metrics: Arc<M>,
        keyring: Arc<KeyRing>,
        active_kid: String,
        scope: Arc<Scope>,
        host: String,
        port: u16,
        use_tls: bool,
        dek_provider: Arc<Dk>,
        identity_metrics: Arc<prometheus::IntCounterVec>,
    ) -> Self {
        Self {
            resolver,
            appender,
            spine_metrics,
            keyring,
            active_kid: active_kid.into(),
            scope,
            host,
            port,
            use_tls,
            session_metrics: None,
            resolve_timeout: RESOLVE_TIMEOUT,
            dek_provider,
            identity_metrics,
        }
    }
}

impl<R, A, M, Dk> SessionRunner for RealRunner<R, A, M, Dk>
where
    R: ConnectionResolve + 'static,
    A: EventAppender + 'static,
    M: SpineMetrics + 'static,
    Dk: crate::identity_crypto::DekProvider + 'static,
{
    fn spawn(
        &self,
        spec: SessionSpec,
        mut shutdown: oneshot::Receiver<()>,
    ) -> JoinHandle<SessionEnd> {
        let resolver = self.resolver.clone();
        let appender = self.appender.clone();
        let metrics = self.spine_metrics.clone();
        let keyring = self.keyring.clone();
        let kid = self.active_kid.clone();
        let scope = self.scope.clone();
        let (host, port, tls) = (self.host.clone(), self.port, self.use_tls);
        let session_metrics = self.session_metrics.clone();
        let resolve_timeout = self.resolve_timeout;
        let dek_provider = self.dek_provider.clone();
        let identity_metrics = self.identity_metrics.clone();
        tokio::spawn(async move {
            let label = SessionLabel {
                community_id: spec.community_id,
                connection_id: spec.connection_id,
            };
            let mut connector =
                ResolvingConnector::new(resolver, spec.clone(), host, port, tls, resolve_timeout);
            if let Some(m) = session_metrics {
                connector = connector.with_metrics(m);
            }
            let (stop_tx, stop_rx) = oneshot::channel();
            let session = run_with_connector(
                connector,
                &spec.bot_login,
                &spec.channel,
                label,
                &*appender,
                &*metrics,
                &keyring,
                &kid,
                &scope,
                &*dek_provider,
                &identity_metrics,
                stop_rx,
            );
            tokio::pin!(session);
            tokio::select! {
                _ = &mut shutdown => {
                    // Sender dropped counts as shutdown: the manager is gone.
                    let _ = stop_tx.send(());
                    session.await;
                    SessionEnd::Shutdown
                }
                () = &mut session => SessionEnd::NonRetryable,
            }
        })
    }
}

/// One tracked session.
struct RunningSession {
    spec: SessionSpec,
    /// Wrapper task: awaits the runner's handle and stamps the end time.
    handle: JoinHandle<SessionEnd>,
    /// Aborts the runner's own task if it ignores shutdown.
    inner_abort: AbortHandle,
    shutdown_tx: Option<oneshot::Sender<()>>,
    /// Session-local lease deadline; pushed forward on every successful renew.
    deadline_tx: watch::Sender<Instant>,
    /// Set by the wrapper the instant the session task finishes.
    ended: Arc<Mutex<Option<(Instant, SessionEnd)>>>,
    /// Consecutive lease-renew errors.
    renew_failures: u32,
    /// Whether this replica still holds the lease for the session.
    lease_held: bool,
}

impl RunningSession {
    /// When and how the session ended on its own, if it did.
    fn finished(&self) -> Option<(Instant, SessionEnd)> {
        *self.ended.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

/// Result of renewing one live session's lease.
#[derive(PartialEq, Eq)]
enum Renewal {
    Kept,
    Stopped,
}

/// Reconciles running Twitch sessions against hub-api's desired set.
pub struct SessionManager<R: ConnectionResolve + 'static, L: SessionLease + 'static> {
    resolver: Arc<R>,
    lease: Arc<L>,
    deps: SessionDeps,
    interval: Duration,
    discovery_timeout: Duration,
    sessions: HashMap<(i64, i64), RunningSession>,
}

/// Up to 10% of [`RECONCILE_INTERVAL`], randomized so replicas desynchronize.
fn jitter(interval: Duration) -> Duration {
    let max_ms = (interval.as_millis() / 10).min(u64::MAX as u128);
    let ms = uuid::Uuid::new_v4().as_u128() % (max_ms + 1);
    Duration::from_millis(u64::try_from(ms).unwrap_or(0))
}

/// Fixed class label for a discovery failure (never the error text).
fn discover_error_class(e: &DiscoverError) -> &'static str {
    match e {
        DiscoverError::Resolve(_) => "resolve",
        DiscoverError::PageCapExceeded => "page_cap",
        DiscoverError::Timeout => "timeout",
        DiscoverError::StuckCursor => "stuck_cursor",
    }
}

impl<R: ConnectionResolve + 'static, L: SessionLease + 'static> SessionManager<R, L> {
    /// Builds an idle manager; nothing runs until [`Self::reconcile`]/[`Self::run`].
    pub fn new(resolver: Arc<R>, lease: Arc<L>, deps: SessionDeps) -> Self {
        Self {
            resolver,
            lease,
            deps,
            interval: RECONCILE_INTERVAL,
            discovery_timeout: DISCOVERY_TIMEOUT,
            sessions: HashMap::new(),
        }
    }

    /// Overrides the reconcile interval and the per-call discovery bound
    /// (defaults: [`RECONCILE_INTERVAL`], 10s). The lease TTL and the
    /// self-stop window scale with the interval (4x and 3x, plus discovery) so the renew
    /// cadence always fits well inside the lease.
    #[must_use]
    pub fn with_timing(mut self, interval: Duration, discovery_timeout: Duration) -> Self {
        self.interval = interval;
        self.discovery_timeout = discovery_timeout;
        self
    }

    /// Lease lifetime: the session window plus one interval of margin, so the
    /// window is always strictly inside the TTL. Saturating: never panics.
    fn lease_ttl(&self) -> Duration {
        self.session_window().saturating_add(self.interval)
    }

    /// How long a session may outlive its last successful acquire/renew:
    /// `(MAX_RENEW_FAILURES + 1)` intervals plus one discovery bound. The
    /// manager tolerates `MAX_RENEW_FAILURES - 1` failed renews before it
    /// stops a session itself, so the deadline must outlast that many missed
    /// renews plus the one that finally succeeds (each gap = interval + 10%
    /// jitter + the discovery preceding it) -- otherwise a healthy session
    /// self-stops as LeaseLost after one transient renew error.
    fn session_window(&self) -> Duration {
        self.interval
            .saturating_mul(MAX_RENEW_FAILURES.saturating_add(1))
            .saturating_add(self.discovery_timeout)
    }

    /// Sessions whose task is still running.
    pub fn live_session_count(&self) -> usize {
        self.sessions
            .values()
            .filter(|s| s.finished().is_none())
            .count()
    }

    /// One reconcile pass. Never panics and never returns an error: failures
    /// are isolated per session and surface in the report and metrics.
    #[tracing::instrument(
        name = "twitch.reconcile",
        skip_all,
        fields(desired, started, stopped, discovery_failed)
    )]
    pub async fn reconcile(&mut self) -> ReconcileReport {
        let began = Instant::now();
        let mut rep = ReconcileReport::default();
        self.reap_finished().await;
        match discover(&*self.resolver, self.discovery_timeout).await {
            Err(e) => {
                rep.discovery_failed = true;
                self.deps.metrics.discovery(DiscoveryOutcome::Failed);
                if let DiscoverError::Resolve(re) = &e {
                    self.deps.metrics.resolve_failed(re.outcome());
                } else if matches!(e, DiscoverError::Timeout) {
                    self.deps.metrics.resolve_failed("timeout");
                }
                tracing::warn!(
                    class = discover_error_class(&e),
                    "twitch binding discovery failed; keeping all sessions"
                );
                let keys: Vec<_> = self.live_keys();
                for key in keys {
                    match self.renew_one(key).await {
                        Renewal::Kept => rep.kept += 1,
                        Renewal::Stopped => rep.stopped += 1,
                    }
                }
            }
            Ok(d) => {
                self.deps.metrics.discovery(DiscoveryOutcome::Ok);
                self.deps
                    .metrics
                    .discovery_skipped("malformed", d.skipped_malformed);
                self.deps
                    .metrics
                    .discovery_skipped("duplicate", d.skipped_duplicate);
                self.apply(d.specs, &mut rep).await;
            }
        }
        let live = self.live_session_count();
        self.deps
            .metrics
            .reconcile_done(&rep, began.elapsed(), live);
        let span = tracing::Span::current();
        span.record("desired", rep.desired);
        span.record("started", rep.started);
        span.record("stopped", rep.stopped);
        span.record("discovery_failed", rep.discovery_failed);
        tracing::debug!(
            desired = rep.desired,
            started = rep.started,
            stopped = rep.stopped,
            restarted = rep.restarted,
            kept = rep.kept,
            lease_denied = rep.lease_denied,
            live,
            "twitch reconcile pass done"
        );
        rep
    }

    /// Reconciles every [`RECONCILE_INTERVAL`] (+ jitter) until `shutdown`
    /// resolves, then stops every session and releases its lease.
    pub async fn run(mut self, mut shutdown: oneshot::Receiver<()>) {
        loop {
            self.reconcile().await;
            tokio::select! {
                _ = &mut shutdown => break,
                () = tokio::time::sleep(self.interval.saturating_add(jitter(self.interval))) => {}
            }
        }
        self.stop_all().await;
    }

    /// Keys of sessions still running.
    fn live_keys(&self) -> Vec<(i64, i64)> {
        self.sessions
            .iter()
            .filter(|(_, s)| s.finished().is_none())
            .map(|(k, _)| *k)
            .collect()
    }

    /// Releases the lease and records the end of sessions that stopped on
    /// their own; their entries stay (for the restart cooldown).
    async fn reap_finished(&mut self) {
        let keys: Vec<_> = self
            .sessions
            .iter()
            .filter(|(_, s)| s.lease_held && s.finished().is_some())
            .map(|(k, _)| *k)
            .collect();
        for key in keys {
            let Some(s) = self.sessions.get_mut(&key) else {
                continue;
            };
            s.lease_held = false;
            let end = s.finished().map_or(SessionEnd::NonRetryable, |(_, e)| e);
            self.deps.metrics.session_ended(end);
            tracing::info!(
                community_id = key.0,
                connection_id = key.1,
                end = end.label(),
                "twitch session ended on its own"
            );
            if let Err(e) = self.lease.release(key).await {
                tracing::warn!(community_id = key.0, connection_id = key.1, error = %e, "lease release failed");
            }
        }
    }

    /// Diffs `specs` (the complete desired set) against running sessions.
    async fn apply(&mut self, specs: Vec<SessionSpec>, rep: &mut ReconcileReport) {
        let desired: BTreeMap<(i64, i64), SessionSpec> =
            specs.into_iter().map(|s| (s.key(), s)).collect();
        rep.desired = u32::try_from(desired.len()).unwrap_or(u32::MAX);

        let gone: Vec<_> = self
            .sessions
            .keys()
            .filter(|k| !desired.contains_key(k))
            .copied()
            .collect();
        for key in gone {
            if let Some(rs) = self.sessions.remove(&key) {
                self.stop_session(key, rs, SessionEnd::Shutdown).await;
                rep.stopped += 1;
            }
        }

        for (key, spec) in desired {
            let Some(rs) = self.sessions.get(&key) else {
                if self.try_start(spec, rep).await {
                    rep.started += 1;
                }
                continue;
            };
            let changed = rs.spec != spec;
            if let Some((at, _)) = rs.finished() {
                if !changed && at.elapsed() < RESTART_COOLDOWN {
                    continue;
                }
                self.sessions.remove(&key);
                if self.try_start(spec, rep).await {
                    rep.restarted += 1;
                }
            } else if changed {
                if let Some(old) = self.sessions.remove(&key) {
                    self.stop_session(key, old, SessionEnd::Shutdown).await;
                }
                if self.try_start(spec, rep).await {
                    rep.restarted += 1;
                } else {
                    rep.stopped += 1;
                }
            } else {
                match self.renew_one(key).await {
                    Renewal::Kept => rep.kept += 1,
                    Renewal::Stopped => rep.stopped += 1,
                }
            }
        }
    }

    /// Acquires the lease and spawns the session; `false` (counted in
    /// `lease_denied`) when the lease is held elsewhere or Valkey failed.
    async fn try_start(&mut self, spec: SessionSpec, rep: &mut ReconcileReport) -> bool {
        let key = spec.key();
        match self.lease.acquire(key, self.lease_ttl()).await {
            Ok(true) => {
                self.deps.metrics.lease_event(LeaseOutcome::Acquired);
                let rs = self.launch(spec);
                self.sessions.insert(key, rs);
                tracing::info!(
                    community_id = key.0,
                    connection_id = key.1,
                    "twitch session started"
                );
                true
            }
            Ok(false) => {
                self.deps.metrics.lease_event(LeaseOutcome::Denied);
                rep.lease_denied += 1;
                false
            }
            Err(e) => {
                self.deps.metrics.lease_event(LeaseOutcome::Error);
                rep.lease_denied += 1;
                tracing::warn!(community_id = key.0, connection_id = key.1, error = %e, "lease acquire failed; session not started");
                false
            }
        }
    }

    /// Spawns via the runner and wraps the handle: the wrapper stamps the end
    /// time and stops the session itself once its lease deadline passes
    /// without a renew (no fencing token, so this closes the window in which a
    /// lost lease could keep publishing next to another replica's session).
    fn launch(&self, spec: SessionSpec) -> RunningSession {
        let (shutdown_tx, mut outer_rx) = oneshot::channel::<()>();
        let (inner_tx, inner_rx) = oneshot::channel();
        let mut inner = self.deps.runner.spawn(spec.clone(), inner_rx);
        let inner_abort = inner.abort_handle();
        let window = self.session_window();
        let (deadline_tx, mut deadline_rx) = watch::channel(Instant::now() + window);
        let ended = Arc::new(Mutex::new(None));
        let stamp = ended.clone();
        let label = spec.key();
        let handle = tokio::spawn(async move {
            let mut inner_tx = Some(inner_tx);
            let end = loop {
                let dl = *deadline_rx.borrow();
                let stop = tokio::select! {
                    r = &mut inner => break r.unwrap_or(SessionEnd::NonRetryable),
                    _ = &mut outer_rx => SessionEnd::Shutdown,
                    () = tokio::time::sleep_until(dl) => {
                        if *deadline_rx.borrow() > dl {
                            continue;
                        }
                        tracing::warn!(
                            community_id = label.0,
                            connection_id = label.1,
                            "lease deadline passed without renew; stopping session"
                        );
                        SessionEnd::LeaseLost
                    }
                    changed = deadline_rx.changed() => {
                        if changed.is_ok() {
                            continue;
                        }
                        SessionEnd::Shutdown
                    }
                };
                if let Some(tx) = inner_tx.take() {
                    let _ = tx.send(());
                }
                if tokio::time::timeout(STOP_TIMEOUT, &mut inner)
                    .await
                    .is_err()
                {
                    inner.abort();
                }
                break stop;
            };
            *stamp.lock().unwrap_or_else(PoisonError::into_inner) = Some((Instant::now(), end));
            end
        });
        RunningSession {
            spec,
            handle,
            inner_abort,
            shutdown_tx: Some(shutdown_tx),
            deadline_tx,
            ended,
            renew_failures: 0,
            lease_held: true,
        }
    }

    /// Renews one live session's lease; stops it when the lease is lost or
    /// could not be renewed [`MAX_RENEW_FAILURES`] times in a row.
    async fn renew_one(&mut self, key: (i64, i64)) -> Renewal {
        let before = Instant::now();
        let window = self.session_window();
        let outcome = self.lease.renew(key, self.lease_ttl()).await;
        let lost = match outcome {
            Ok(true) => {
                self.deps.metrics.lease_event(LeaseOutcome::Renewed);
                if let Some(s) = self.sessions.get_mut(&key) {
                    s.renew_failures = 0;
                    s.deadline_tx.send_replace(before + window);
                }
                false
            }
            Ok(false) => {
                self.deps.metrics.lease_event(LeaseOutcome::Lost);
                tracing::warn!(
                    community_id = key.0,
                    connection_id = key.1,
                    "lease lost; stopping session"
                );
                true
            }
            Err(e) => {
                self.deps.metrics.lease_event(LeaseOutcome::Error);
                let failures = self.sessions.get_mut(&key).map_or(0, |s| {
                    s.renew_failures += 1;
                    s.renew_failures
                });
                tracing::warn!(community_id = key.0, connection_id = key.1, error = %e, failures, "lease renew failed");
                failures >= MAX_RENEW_FAILURES
            }
        };
        if !lost {
            return Renewal::Kept;
        }
        if let Some(rs) = self.sessions.remove(&key) {
            self.stop_session(key, rs, SessionEnd::LeaseLost).await;
        }
        Renewal::Stopped
    }

    /// Signals shutdown, waits (bounded) for the task, releases the lease and
    /// records the end.
    async fn stop_session(&self, key: (i64, i64), mut rs: RunningSession, reason: SessionEnd) {
        let already = rs.finished();
        if let Some(tx) = rs.shutdown_tx.take() {
            let _ = tx.send(());
        }
        let mut handle = rs.handle;
        if tokio::time::timeout(STOP_TIMEOUT, &mut handle)
            .await
            .is_err()
        {
            tracing::warn!(
                community_id = key.0,
                connection_id = key.1,
                "session ignored shutdown; aborting"
            );
            rs.inner_abort.abort();
            handle.abort();
        }
        if rs.lease_held {
            if let Err(e) = self.lease.release(key).await {
                tracing::warn!(community_id = key.0, connection_id = key.1, error = %e, "lease release failed");
            }
        }
        // A session that already ended on its own had its end recorded when
        // reaped; only an unreaped one still owes its (real) end metric.
        let recorded = match already {
            Some((_, end)) if rs.lease_held => Some(end),
            Some(_) => None,
            None => Some(reason),
        };
        if let Some(end) = recorded {
            self.deps.metrics.session_ended(end);
        }
        tracing::info!(
            community_id = key.0,
            connection_id = key.1,
            end = reason.label(),
            "twitch session stopped"
        );
    }

    /// Stops every session: signals all first so they wind down in parallel.
    async fn stop_all(&mut self) {
        for rs in self.sessions.values_mut() {
            if let Some(tx) = rs.shutdown_tx.take() {
                let _ = tx.send(());
            }
        }
        let all: Vec<_> = self.sessions.drain().collect();
        for (key, rs) in all {
            if rs.finished().is_some() && !rs.lease_held {
                continue;
            }
            self.stop_session(key, rs, SessionEnd::Shutdown).await;
        }
        self.deps.metrics.set_live_sessions(0);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::twitch_sessions::lease::LeaseError;
    use crate::twitch_sessions::metrics::{DiscoveryOutcome, LeaseOutcome, SessionMetrics};
    use connection_resolver::{
        BindingPage, BindingSummary, ConnectionKind, ConnectionRef, ResolveError,
        ResolvedConnection,
    };
    use std::collections::{BTreeMap, HashSet};
    use std::sync::Mutex;

    /// Resolver whose binding list is scripted: `None` => transient failure.
    struct FakeResolver {
        bindings: Mutex<Option<Vec<BindingSummary>>>,
        resolve_transient: bool,
    }

    impl ConnectionResolve for FakeResolver {
        async fn resolve(&self, _r: ConnectionRef) -> Result<ResolvedConnection, ResolveError> {
            if self.resolve_transient {
                Err(ResolveError::Transient("boom"))
            } else {
                Err(ResolveError::NotFound)
            }
        }

        async fn list_bindings(
            &self,
            _p: &str,
            _c: Option<&str>,
            _a: i64,
            _l: u32,
        ) -> Result<BindingPage, ResolveError> {
            match self.bindings.lock().unwrap().clone() {
                Some(items) => Ok(BindingPage {
                    items,
                    next_after: None,
                }),
                None => Err(ResolveError::Transient("list down")),
            }
        }
    }

    fn binding(community: i64, conn: i64, channel: &str) -> BindingSummary {
        let mut metadata = BTreeMap::new();
        metadata.insert("channel".to_owned(), channel.to_owned());
        metadata.insert("bot_login".to_owned(), "botnick".to_owned());
        BindingSummary {
            community_id: community,
            connection_id: conn,
            provider: "twitch".to_owned(),
            kind: ConnectionKind::PlatformOauth,
            metadata,
        }
    }

    #[derive(Default)]
    struct LeaseState {
        held: HashSet<(i64, i64)>,
        deny: HashSet<(i64, i64)>,
        err: HashSet<(i64, i64)>,
        renew_false: HashSet<(i64, i64)>,
        renew_err: HashSet<(i64, i64)>,
        acquires: Vec<(i64, i64)>,
        released: Vec<(i64, i64)>,
    }

    #[derive(Default)]
    struct FakeLease(Mutex<LeaseState>);

    impl SessionLease for FakeLease {
        async fn acquire(&self, key: (i64, i64), _ttl: Duration) -> Result<bool, LeaseError> {
            let mut s = self.0.lock().unwrap();
            if s.err.contains(&key) {
                return Err(LeaseError("down".into()));
            }
            if s.deny.contains(&key) {
                return Ok(false);
            }
            s.acquires.push(key);
            s.held.insert(key);
            Ok(true)
        }

        async fn renew(&self, key: (i64, i64), _ttl: Duration) -> Result<bool, LeaseError> {
            let s = self.0.lock().unwrap();
            if s.renew_err.contains(&key) {
                return Err(LeaseError("down".into()));
            }
            Ok(!s.renew_false.contains(&key))
        }

        async fn release(&self, key: (i64, i64)) -> Result<(), LeaseError> {
            let mut s = self.0.lock().unwrap();
            s.held.remove(&key);
            s.released.push(key);
            Ok(())
        }
    }

    /// Runner recording every spawn; sessions live until shutdown unless
    /// `end_with` scripts an immediate self-end.
    #[derive(Default)]
    struct RecRunner {
        starts: Mutex<Vec<SessionSpec>>,
        end_with: Mutex<Option<SessionEnd>>,
    }

    impl SessionRunner for RecRunner {
        fn spawn(
            &self,
            spec: SessionSpec,
            shutdown: oneshot::Receiver<()>,
        ) -> JoinHandle<SessionEnd> {
            self.starts.lock().unwrap().push(spec);
            let end = *self.end_with.lock().unwrap();
            tokio::spawn(async move {
                match end {
                    Some(e) => e,
                    None => {
                        let _ = shutdown.await;
                        SessionEnd::Shutdown
                    }
                }
            })
        }
    }

    #[derive(Default)]
    struct RecMetrics {
        started: Mutex<usize>,
        ends: Mutex<Vec<SessionEnd>>,
        leases: Mutex<Vec<LeaseOutcome>>,
        discoveries: Mutex<Vec<DiscoveryOutcome>>,
        reconciles: Mutex<usize>,
    }

    impl SessionMetrics for RecMetrics {
        fn session_ended(&self, reason: SessionEnd) {
            self.ends.lock().unwrap().push(reason);
        }
        fn lease_event(&self, outcome: LeaseOutcome) {
            self.leases.lock().unwrap().push(outcome);
        }
        fn discovery(&self, outcome: DiscoveryOutcome) {
            self.discoveries.lock().unwrap().push(outcome);
        }
        fn reconcile_done(&self, r: &ReconcileReport, _e: Duration, _live: usize) {
            *self.started.lock().unwrap() += r.started as usize;
            *self.reconciles.lock().unwrap() += 1;
        }
        fn set_live_sessions(&self, _n: usize) {}
        fn resolve_failed(&self, _class: &'static str) {}
        fn discovery_skipped(&self, _why: &'static str, _n: u32) {}
    }

    struct H {
        resolver: Arc<FakeResolver>,
        lease: Arc<FakeLease>,
        runner: Arc<RecRunner>,
        metrics: Arc<RecMetrics>,
        mgr: SessionManager<FakeResolver, FakeLease>,
    }

    fn harness(initial: Vec<BindingSummary>) -> H {
        let resolver = Arc::new(FakeResolver {
            bindings: Mutex::new(Some(initial)),
            resolve_transient: false,
        });
        let lease = Arc::new(FakeLease::default());
        let runner = Arc::new(RecRunner::default());
        let metrics = Arc::new(RecMetrics::default());
        let mgr = SessionManager::new(
            resolver.clone(),
            lease.clone(),
            SessionDeps {
                runner: runner.clone(),
                metrics: metrics.clone(),
            },
        );
        H {
            resolver,
            lease,
            runner,
            metrics,
            mgr,
        }
    }

    /// Lets spawned session/wrapper tasks run to their next await.
    async fn settle() {
        for _ in 0..20 {
            tokio::task::yield_now().await;
        }
    }

    #[tokio::test]
    async fn manager_updates_gauge_on_start_and_stop() {
        let registry = prometheus::Registry::new();
        let metrics = Arc::new(crate::telemetry::register_ingest_metrics(&registry));
        let resolver = Arc::new(FakeResolver {
            bindings: Mutex::new(Some(vec![
                binding(1, 10, "chan_a"),
                binding(2, 20, "chan_b"),
            ])),
            resolve_transient: false,
        });
        let mut mgr = SessionManager::new(
            resolver.clone(),
            Arc::new(FakeLease::default()),
            SessionDeps {
                runner: Arc::new(RecRunner::default()),
                metrics: metrics.clone(),
            },
        );
        mgr.reconcile().await;
        let text = crate::telemetry::render_metrics(&registry).unwrap();
        assert!(text.contains("svc_ingest_twitch_sessions_live 2"), "{text}");
        assert!(text.contains(r#"svc_ingest_twitch_session_events_total{event="started"} 2"#));

        *resolver.bindings.lock().unwrap() = Some(vec![binding(2, 20, "chan_b")]);
        mgr.reconcile().await;
        let text = crate::telemetry::render_metrics(&registry).unwrap();
        assert!(text.contains("svc_ingest_twitch_sessions_live 1"), "{text}");
        assert!(text.contains(r#"svc_ingest_twitch_session_events_total{event="stopped"} 1"#));

        mgr.stop_all().await;
        let text = crate::telemetry::render_metrics(&registry).unwrap();
        assert!(text.contains("svc_ingest_twitch_sessions_live 0"), "{text}");
        assert_eq!(
            metrics
                .receiver_connection_healthy
                .with_label_values(&["twitch_irc"])
                .get(),
            0
        );
    }

    #[tokio::test]
    async fn discovery_failure_records_resolve_class_and_failed_outcome() {
        let registry = prometheus::Registry::new();
        let metrics = Arc::new(crate::telemetry::register_ingest_metrics(&registry));
        let resolver = Arc::new(FakeResolver {
            bindings: Mutex::new(None),
            resolve_transient: false,
        });
        let mut mgr = SessionManager::new(
            resolver,
            Arc::new(FakeLease::default()),
            SessionDeps {
                runner: Arc::new(RecRunner::default()),
                metrics: metrics.clone(),
            },
        );
        mgr.reconcile().await;
        let text = crate::telemetry::render_metrics(&registry).unwrap();
        assert!(text.contains(r#"svc_ingest_twitch_resolve_errors_total{class="transient"} 1"#));
        assert!(text.contains(r#"svc_ingest_twitch_reconcile_total{outcome="discovery_failed"} 1"#));
    }

    #[tokio::test]
    async fn repeated_passes_over_a_bad_binding_do_not_inflate_skip_metric() {
        let registry = prometheus::Registry::new();
        let metrics = Arc::new(crate::telemetry::register_ingest_metrics(&registry));
        let mut bad = binding(3, 30, "chan_c");
        bad.metadata.clear();
        let resolver = Arc::new(FakeResolver {
            bindings: Mutex::new(Some(vec![bad])),
            resolve_transient: false,
        });
        let mut mgr = SessionManager::new(
            resolver.clone(),
            Arc::new(FakeLease::default()),
            SessionDeps {
                runner: Arc::new(RecRunner::default()),
                metrics: metrics.clone(),
            },
        );
        for _ in 0..3 {
            mgr.reconcile().await;
        }
        let text = crate::telemetry::render_metrics(&registry).unwrap();
        assert!(
            text.contains(r#"svc_ingest_twitch_discovery_skipped{why="malformed"} 1"#),
            "{text}"
        );
        *resolver.bindings.lock().unwrap() = Some(vec![]);
        mgr.reconcile().await;
        let text = crate::telemetry::render_metrics(&registry).unwrap();
        assert!(text.contains(r#"svc_ingest_twitch_discovery_skipped{why="malformed"} 0"#));
    }

    #[tokio::test]
    async fn new_binding_starts_a_session() {
        let mut h = harness(vec![binding(1, 10, "chan_a")]);
        let r = h.mgr.reconcile().await;
        assert_eq!((r.desired, r.started, r.stopped, r.kept), (1, 1, 0, 0));
        assert!(!r.discovery_failed);
        assert_eq!(h.mgr.live_session_count(), 1);
        assert_eq!(h.runner.starts.lock().unwrap().len(), 1);
        assert_eq!(h.lease.0.lock().unwrap().acquires, vec![(1, 10)]);
        assert_eq!(*h.metrics.started.lock().unwrap(), 1);
        assert_eq!(*h.metrics.reconciles.lock().unwrap(), 1);
    }

    #[tokio::test]
    async fn removed_binding_stops_and_releases_its_session() {
        let mut h = harness(vec![binding(1, 10, "chan_a"), binding(2, 20, "chan_b")]);
        h.mgr.reconcile().await;
        *h.resolver.bindings.lock().unwrap() = Some(vec![binding(2, 20, "chan_b")]);
        let r = h.mgr.reconcile().await;
        assert_eq!((r.stopped, r.kept, r.started), (1, 1, 0));
        assert_eq!(h.mgr.live_session_count(), 1);
        assert_eq!(h.lease.0.lock().unwrap().released, vec![(1, 10)]);
        assert_eq!(*h.metrics.ends.lock().unwrap(), vec![SessionEnd::Shutdown]);
    }

    #[tokio::test]
    async fn transient_list_error_keeps_all_sessions() {
        let mut h = harness(vec![binding(1, 10, "chan_a"), binding(2, 20, "chan_b")]);
        h.mgr.reconcile().await;
        *h.resolver.bindings.lock().unwrap() = None;
        let r = h.mgr.reconcile().await;
        assert!(r.discovery_failed);
        assert_eq!((r.started, r.stopped, r.restarted), (0, 0, 0));
        assert_eq!(r.kept, 2, "renewals still attempted");
        assert_eq!(h.mgr.live_session_count(), 2);
        assert!(h.lease.0.lock().unwrap().released.is_empty());
        assert_eq!(h.runner.starts.lock().unwrap().len(), 2);
        assert_eq!(
            h.metrics.discoveries.lock().unwrap().last().copied(),
            Some(DiscoveryOutcome::Failed)
        );
    }

    #[tokio::test]
    async fn discovery_failure_makes_no_decisions_even_for_finished_sessions() {
        let mut h = harness(vec![binding(1, 10, "chan_a")]);
        *h.runner.end_with.lock().unwrap() = Some(SessionEnd::NonRetryable);
        h.mgr.reconcile().await;
        settle().await;
        tokio::time::sleep(Duration::from_millis(1)).await;
        *h.resolver.bindings.lock().unwrap() = None;
        let r = h.mgr.reconcile().await;
        assert!(r.discovery_failed);
        assert_eq!((r.started, r.stopped, r.restarted), (0, 0, 0));
        assert_eq!(h.runner.starts.lock().unwrap().len(), 1, "no restart");
    }

    #[tokio::test]
    async fn empty_complete_discovery_stops_all_sessions() {
        let mut h = harness(vec![binding(1, 10, "chan_a"), binding(2, 20, "chan_b")]);
        h.mgr.reconcile().await;
        *h.resolver.bindings.lock().unwrap() = Some(vec![]);
        let r = h.mgr.reconcile().await;
        assert!(!r.discovery_failed);
        assert_eq!((r.desired, r.stopped), (0, 2));
        assert_eq!(h.mgr.live_session_count(), 0);
        assert_eq!(h.lease.0.lock().unwrap().released.len(), 2);
    }

    #[tokio::test]
    async fn channel_change_restarts_session() {
        let mut h = harness(vec![binding(1, 10, "chan_a")]);
        h.mgr.reconcile().await;
        *h.resolver.bindings.lock().unwrap() = Some(vec![binding(1, 10, "chan_b")]);
        let r = h.mgr.reconcile().await;
        assert_eq!((r.restarted, r.started, r.stopped), (1, 0, 0));
        assert_eq!(h.mgr.live_session_count(), 1);
        let starts = h.runner.starts.lock().unwrap();
        assert_eq!(starts.len(), 2);
        assert_eq!(starts[1].channel, "chan_b");
        assert_eq!(h.lease.0.lock().unwrap().released, vec![(1, 10)]);
    }

    #[tokio::test(start_paused = true)]
    async fn finished_session_is_restarted_after_cooldown_with_fresh_resolve() {
        let mut h = harness(vec![binding(1, 10, "chan_a")]);
        *h.runner.end_with.lock().unwrap() = Some(SessionEnd::NonRetryable);
        h.mgr.reconcile().await;
        settle().await;
        assert_eq!(h.mgr.live_session_count(), 0, "session ended on its own");

        tokio::time::advance(Duration::from_secs(60)).await;
        let r = h.mgr.reconcile().await;
        assert_eq!((r.started, r.restarted), (0, 0), "still cooling down");
        assert_eq!(h.runner.starts.lock().unwrap().len(), 1);
        assert_eq!(
            *h.metrics.ends.lock().unwrap(),
            vec![SessionEnd::NonRetryable]
        );
        assert!(h.lease.0.lock().unwrap().released.contains(&(1, 10)));

        tokio::time::advance(Duration::from_secs(61)).await;
        let r = h.mgr.reconcile().await;
        assert_eq!(r.restarted, 1);
        assert_eq!(
            h.runner.starts.lock().unwrap().len(),
            2,
            "a new spawn means a fresh connector, hence a fresh resolve"
        );
    }

    #[tokio::test(start_paused = true)]
    async fn unrenewed_session_stops_itself_before_the_lease_can_lapse() {
        let mut h = harness(vec![binding(1, 10, "chan_a")]);
        h.mgr.reconcile().await;
        // No further reconcile fires: the manager "stopped renewing".
        tokio::time::advance(Duration::from_secs(189)).await;
        settle().await;
        assert_eq!(h.mgr.live_session_count(), 1, "still inside the window");
        tokio::time::advance(Duration::from_secs(2)).await;
        settle().await;
        assert_eq!(
            h.mgr.live_session_count(),
            0,
            "stopped itself at ~190s < TTL (250s)"
        );
        assert!(LEASE_TTL > Duration::from_secs(191));
        let r = h.mgr.reconcile().await;
        assert_eq!(r.restarted, 0, "cooldown still applies");
        assert_eq!(*h.metrics.ends.lock().unwrap(), vec![SessionEnd::LeaseLost]);
    }

    #[tokio::test(start_paused = true)]
    async fn successful_renews_push_the_session_deadline_forward() {
        let mut h = harness(vec![binding(1, 10, "chan_a")]);
        for _ in 0..4 {
            h.mgr.reconcile().await;
            tokio::time::advance(Duration::from_secs(60)).await;
            settle().await;
        }
        // Last renew at t=180; deadline t=370; now t=240.
        assert_eq!(h.mgr.live_session_count(), 1);
        tokio::time::advance(Duration::from_secs(129)).await;
        settle().await;
        assert_eq!(h.mgr.live_session_count(), 1);
        tokio::time::advance(Duration::from_secs(2)).await;
        settle().await;
        assert_eq!(h.mgr.live_session_count(), 0);
    }

    #[tokio::test]
    async fn naturally_ended_session_removed_by_binding_change_is_recorded_once() {
        let mut h = harness(vec![binding(1, 10, "chan_a")]);
        *h.runner.end_with.lock().unwrap() = Some(SessionEnd::NonRetryable);
        h.mgr.reconcile().await;
        settle().await;
        h.mgr.reconcile().await; // reaps: records the real end
        *h.resolver.bindings.lock().unwrap() = Some(vec![]);
        h.mgr.reconcile().await; // removes the dead entry
        assert_eq!(
            *h.metrics.ends.lock().unwrap(),
            vec![SessionEnd::NonRetryable]
        );
        assert_eq!(h.lease.0.lock().unwrap().released.len(), 1, "released once");
    }

    #[tokio::test]
    async fn lease_denied_does_not_start_and_is_counted() {
        let mut h = harness(vec![binding(1, 10, "chan_a")]);
        h.lease.0.lock().unwrap().deny.insert((1, 10));
        let r = h.mgr.reconcile().await;
        assert_eq!((r.lease_denied, r.started), (1, 0));
        assert_eq!(h.mgr.live_session_count(), 0);
        assert!(h.runner.starts.lock().unwrap().is_empty());
        assert_eq!(
            *h.metrics.leases.lock().unwrap(),
            vec![LeaseOutcome::Denied]
        );
    }

    #[tokio::test]
    async fn lease_renew_false_stops_the_session() {
        let mut h = harness(vec![binding(1, 10, "chan_a")]);
        h.mgr.reconcile().await;
        h.lease.0.lock().unwrap().renew_false.insert((1, 10));
        let r = h.mgr.reconcile().await;
        assert_eq!((r.stopped, r.kept), (1, 0));
        assert_eq!(h.mgr.live_session_count(), 0);
        assert_eq!(*h.metrics.ends.lock().unwrap(), vec![SessionEnd::LeaseLost]);
        assert!(h
            .metrics
            .leases
            .lock()
            .unwrap()
            .contains(&LeaseOutcome::Lost));
    }

    #[tokio::test]
    async fn lease_renew_false_stops_session_even_when_discovery_failed() {
        let mut h = harness(vec![binding(1, 10, "chan_a")]);
        h.mgr.reconcile().await;
        *h.resolver.bindings.lock().unwrap() = None;
        h.lease.0.lock().unwrap().renew_false.insert((1, 10));
        let r = h.mgr.reconcile().await;
        assert!(r.discovery_failed);
        assert_eq!(h.mgr.live_session_count(), 0);
    }

    #[tokio::test]
    async fn renew_errors_are_tolerated_once_but_stop_the_session_on_the_second() {
        let mut h = harness(vec![binding(1, 10, "chan_a")]);
        h.mgr.reconcile().await;
        h.lease.0.lock().unwrap().renew_err.insert((1, 10));
        let r = h.mgr.reconcile().await;
        assert_eq!((r.kept, r.stopped), (1, 0), "one missed renew is tolerated");
        let r = h.mgr.reconcile().await;
        assert_eq!((r.kept, r.stopped), (0, 1), "second consecutive miss stops");
        assert_eq!(*h.metrics.ends.lock().unwrap(), vec![SessionEnd::LeaseLost]);
    }

    // regression: the self-stop window must outlast the renew errors the
    // manager tolerates -- a healthy session that survives one transient renew
    // error is NOT self-stopped by the deadline (and never hits the cooldown).
    #[tokio::test(start_paused = true)]
    async fn session_surviving_tolerated_renew_errors_is_not_self_stopped() {
        let mut h = harness(vec![binding(1, 10, "chan_a")]);
        assert!(h.mgr.session_window() < h.mgr.lease_ttl());
        assert!(
            h.mgr.session_window()
                >= (h.mgr.interval + h.mgr.interval / 10 + h.mgr.discovery_timeout)
                    * MAX_RENEW_FAILURES,
            "window covers every tolerated missed renew at worst-case gap"
        );
        h.mgr.reconcile().await; // acquire at t=0; deadline t=190
        tokio::time::advance(Duration::from_secs(66)).await; // interval + jitter
        h.lease.0.lock().unwrap().renew_err.insert((1, 10));
        let r = h.mgr.reconcile().await; // one tolerated failure; deadline unchanged
        assert_eq!((r.kept, r.stopped), (1, 0));
        tokio::time::advance(Duration::from_secs(76)).await; // t=142 + disc gap
        settle().await;
        assert_eq!(h.mgr.live_session_count(), 1, "alive before next renew");
        h.lease.0.lock().unwrap().renew_err.clear();
        let r = h.mgr.reconcile().await; // recovers at t=142 < 190
        assert_eq!((r.kept, r.stopped), (1, 0));
        assert_eq!(h.mgr.live_session_count(), 1);
        assert!(h.metrics.ends.lock().unwrap().is_empty(), "no LeaseLost");
    }

    #[tokio::test]
    async fn a_successful_renew_resets_the_failure_count() {
        let mut h = harness(vec![binding(1, 10, "chan_a")]);
        h.mgr.reconcile().await;
        h.lease.0.lock().unwrap().renew_err.insert((1, 10));
        h.mgr.reconcile().await;
        h.lease.0.lock().unwrap().renew_err.clear();
        h.mgr.reconcile().await;
        h.lease.0.lock().unwrap().renew_err.insert((1, 10));
        let r = h.mgr.reconcile().await;
        assert_eq!((r.kept, r.stopped), (1, 0));
    }

    #[tokio::test]
    async fn one_community_failure_does_not_block_others() {
        let mut h = harness(vec![binding(1, 10, "chan_a"), binding(2, 20, "chan_b")]);
        h.lease.0.lock().unwrap().err.insert((1, 10));
        let r = h.mgr.reconcile().await;
        assert_eq!((r.started, r.lease_denied), (1, 1));
        assert_eq!(h.mgr.live_session_count(), 1);
        assert_eq!(h.runner.starts.lock().unwrap()[0].community_id, 2);
        assert!(h
            .metrics
            .leases
            .lock()
            .unwrap()
            .contains(&LeaseOutcome::Error));
    }

    #[tokio::test]
    async fn run_shutdown_stops_every_session_and_releases_leases() {
        let h = harness(vec![binding(1, 10, "chan_a"), binding(2, 20, "chan_b")]);
        let (tx, rx) = oneshot::channel();
        let task = tokio::spawn(h.mgr.run(rx));
        for _ in 0..200 {
            if h.runner.starts.lock().unwrap().len() == 2 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        assert_eq!(h.runner.starts.lock().unwrap().len(), 2);
        tx.send(()).unwrap();
        tokio::time::timeout(Duration::from_secs(5), task)
            .await
            .expect("run returns after shutdown")
            .unwrap();
        let mut released = h.lease.0.lock().unwrap().released.clone();
        released.sort_unstable();
        assert_eq!(released, vec![(1, 10), (2, 20)]);
        assert_eq!(h.metrics.ends.lock().unwrap().len(), 2);
    }

    #[tokio::test]
    async fn reconcile_is_idempotent_when_nothing_changed() {
        let mut h = harness(vec![binding(1, 10, "chan_a"), binding(2, 20, "chan_b")]);
        h.mgr.reconcile().await;
        let r = h.mgr.reconcile().await;
        assert_eq!((r.started, r.stopped, r.restarted, r.kept), (0, 0, 0, 2));
        assert_eq!(h.runner.starts.lock().unwrap().len(), 2);
    }

    #[tokio::test]
    async fn sessions_keyed_by_numeric_ids_not_channel() {
        let mut h = harness(vec![binding(1, 10, "chan_a"), binding(1, 11, "chan_b")]);
        let r = h.mgr.reconcile().await;
        assert_eq!((r.desired, r.started), (2, 2));
        assert_eq!(h.mgr.live_session_count(), 2);
        let mut keys: Vec<_> = h.mgr.sessions.keys().copied().collect();
        keys.sort_unstable();
        assert_eq!(keys, vec![(1, 10), (1, 11)]);
    }

    #[test]
    fn session_end_codes_are_stable_ints() {
        assert_eq!(SessionEnd::Shutdown as i32, 1);
        assert_eq!(SessionEnd::NonRetryable as i32, 2);
        assert_eq!(SessionEnd::LeaseLost as i32, 3);
        assert_eq!(SessionEnd::LeaseLost.label(), "lease_lost");
        assert_eq!(LeaseOutcome::Acquired.label(), "acquired");
        assert_eq!(DiscoveryOutcome::Failed.label(), "failed");
        for o in [
            LeaseOutcome::Denied,
            LeaseOutcome::Error,
            LeaseOutcome::Renewed,
            LeaseOutcome::Lost,
        ] {
            assert!(!o.label().is_empty());
        }
        assert_eq!(DiscoveryOutcome::Ok.label(), "ok");
        assert_eq!(SessionEnd::Shutdown.label(), "shutdown");
        assert_eq!(SessionEnd::NonRetryable.label(), "non_retryable");
    }

    #[tokio::test(start_paused = true)]
    async fn healthy_session_survives_worst_case_renew_gap_at_min_interval() {
        // Minimum interval 10s, 10s discovery bound: the worst healthy gap
        // between renews is 10s + 1s jitter + 10s discovery = 21s.
        let h = harness(vec![binding(1, 10, "chan_a")]);
        let mut mgr = h
            .mgr
            .with_timing(Duration::from_secs(10), Duration::from_secs(10));
        assert!(mgr.session_window() < mgr.lease_ttl());
        assert!(mgr.lease_ttl() - mgr.session_window() > Duration::ZERO);
        mgr.reconcile().await;
        for _ in 0..3 {
            tokio::time::advance(Duration::from_secs(21)).await;
            settle().await;
            assert_eq!(mgr.live_session_count(), 1, "not self-stopped before renew");
            let r = mgr.reconcile().await;
            assert_eq!(r.kept, 1, "renewed in place");
        }
    }

    #[test]
    fn extreme_interval_never_overflows() {
        let huge = Duration::MAX;
        let j = jitter(huge);
        assert_eq!(j.min(Duration::MAX), j);
        let sum = huge.saturating_add(j);
        assert_eq!(sum, Duration::MAX);
    }

    #[tokio::test]
    async fn with_timing_scales_lease_ttl_and_session_window() {
        let h = harness(vec![]);
        assert_eq!(h.mgr.lease_ttl(), LEASE_TTL, "defaults match the consts");
        assert_eq!(h.mgr.session_window(), Duration::from_secs(190));
        let mgr = h
            .mgr
            .with_timing(Duration::from_secs(20), Duration::from_secs(3));
        assert_eq!(mgr.session_window(), Duration::from_secs(63));
        assert_eq!(mgr.lease_ttl(), Duration::from_secs(83));
        assert_eq!(mgr.discovery_timeout, Duration::from_secs(3));
    }

    #[test]
    fn jitter_is_bounded_to_ten_percent() {
        for _ in 0..50 {
            assert!(jitter(RECONCILE_INTERVAL) <= RECONCILE_INTERVAL / 10);
        }
    }

    // ---- RealRunner against fake resolver (no sockets) ----

    #[derive(Default)]
    struct NullAppender;
    impl EventAppender for NullAppender {
        async fn append(
            &self,
            _stream: &str,
            _env: &penguin_spine::StageEnvelope,
        ) -> Result<String, penguin_spine::SpineError> {
            Ok("1-0".to_owned())
        }
    }

    struct FakeDekProvider;

    impl crate::identity_crypto::DekProvider for FakeDekProvider {
        async fn get_dek(
            &self,
            _tenant_id: &str,
        ) -> Result<(crate::identity_crypto::Dek, u32), crate::identity_crypto::DekUnavailableError>
        {
            Ok((zeroize::Zeroizing::new([9u8; 32]), 1))
        }
    }

    fn test_identity_metrics() -> prometheus::IntCounterVec {
        prometheus::IntCounterVec::new(
            prometheus::Opts::new("test_identity_encryption_total", "test-only"),
            &["tenant", "status"],
        )
        .unwrap()
    }

    fn real_runner(
        transient: bool,
    ) -> RealRunner<FakeResolver, NullAppender, penguin_spine::NoopMetrics, FakeDekProvider> {
        RealRunner::new(
            Arc::new(FakeResolver {
                bindings: Mutex::new(Some(vec![])),
                resolve_transient: transient,
            }),
            Arc::new(NullAppender),
            Arc::new(penguin_spine::NoopMetrics),
            Arc::new(penguin_spine::KeyRing::new(vec![(
                "k1".to_owned(),
                vec![9u8; 32],
            )])),
            "k1".to_owned(),
            Arc::new(penguin_spine::Scope::new("acme", None)),
            "127.0.0.1".to_owned(),
            1,
            false,
            Arc::new(FakeDekProvider),
            Arc::new(test_identity_metrics()),
        )
    }

    fn spec() -> SessionSpec {
        SessionSpec {
            community_id: 1,
            connection_id: 10,
            channel: "chan_a".into(),
            bot_login: "botnick".into(),
        }
    }

    #[tokio::test]
    async fn real_runner_reports_non_retryable_when_hub_refuses_the_connection() {
        let (_tx, rx) = oneshot::channel();
        let end =
            tokio::time::timeout(Duration::from_secs(5), real_runner(false).spawn(spec(), rx))
                .await
                .expect("session ends by itself")
                .unwrap();
        assert_eq!(end, SessionEnd::NonRetryable);
    }

    #[tokio::test]
    async fn real_runner_reports_shutdown_when_signalled_during_retries() {
        let (tx, rx) = oneshot::channel();
        let handle = real_runner(true).spawn(spec(), rx);
        tokio::time::sleep(Duration::from_millis(50)).await;
        assert!(
            !handle.is_finished(),
            "retryable failures keep the session alive"
        );
        tx.send(()).unwrap();
        let end = tokio::time::timeout(Duration::from_secs(5), handle)
            .await
            .expect("session stops on shutdown")
            .unwrap();
        assert_eq!(end, SessionEnd::Shutdown);
    }

    #[tokio::test]
    async fn dropped_shutdown_sender_also_stops_a_real_session() {
        let (tx, rx) = oneshot::channel();
        let handle = real_runner(true).spawn(spec(), rx);
        drop(tx);
        let end = tokio::time::timeout(Duration::from_secs(5), handle)
            .await
            .expect("session stops when the manager goes away")
            .unwrap();
        assert_eq!(end, SessionEnd::Shutdown);
    }
}
