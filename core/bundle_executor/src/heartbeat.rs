//! Host-API session liveness.
//!
//! **Incident this closes:** after a svc-process/svc-action rollout, an
//! executor pod stayed dialed to the terminated pod's socket -- no FIN/RST
//! ever arrived (a classic half-open TCP connection: the old pod's network
//! namespace vanished without a clean close), so the process looked
//! perfectly healthy (`--healthcheck` only ever build-tests the wasmtime
//! engine, it has no opinion on the stage dial) while silently never
//! receiving another frame again. `crate::wire::run_connection` had no way
//! to notice "nothing has arrived in a while" and no way to act on it even
//! if it had.
//!
//! This module gives it both: [`ActivityTracker`] records when the last
//! frame arrived (any kind -- a `load`/`invoke`/`unload`/`ping` all count)
//! AND whether the stage has ever sent its OWN `ping` (proof the peer
//! actually heartbeats), and [`run_monitor`] races the activity timestamp
//! against the wall clock, optionally sending this executor's own `ping`
//! to generate traffic on an otherwise-idle-but-healthy connection, and
//! returns [`crate::error::ExecutorError::SessionStale`] the moment an
//! ARMED connection goes quiet for too long -- `crate::wire::run_connection`
//! treats that exactly like any other fatal connection error: drop it, let
//! `crate::run`'s existing exponential-backoff loop reconnect.
//!
//! **Arming (PR #529 review fix):** the frame-activity timeout only
//! activates once this session has observed at least one stage-originated
//! `ping`. Before the companion svc-side fix (`fix/executor-link-
//! heartbeat`) lands, today's stage sends no periodic `ping` at all, so an
//! idle-but-perfectly-healthy connection (no `load`/`invoke` traffic for a
//! while) would otherwise look identical to a genuinely stalled one and get
//! disconnected+reconnected roughly every `STALE_INTERVAL_MULTIPLIER *
//! HEARTBEAT_INTERVAL` seconds -- constant churn, with in-flight messages
//! dead-lettered on every reconnect window. Gating on "has the stage ever
//! heartbeated" means: unarmed, this module relies solely on
//! `crate::tls::dial_stage`'s TCP keepalive (OS-level, needs no
//! wire-protocol cooperation); the moment the svc-side fix starts sending
//! periodic `ping`, this session auto-arms and gets the full frame-level
//! check too -- no flag flip, no redeploy of this crate required.
//!
//! A real half-open socket is *also* caught at the OS level by
//! `crate::tls::dial_stage`'s TCP keepalive (belt and suspenders: keepalive
//! needs no wire-protocol cooperation from the stage at all, so it alone
//! already fixes the exact incident above; this module's frame-level check
//! is the second layer, and the one whose timeout is actually tunable per
//! spec's `HEARTBEAT_INTERVAL`).

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use penguin_bundle_host::wire::Message;
use tokio::time::Instant;
use tracing::{debug, error, info, warn};

use crate::config::CliConfig;
use crate::error::ExecutorError;
use crate::wire::Connection;

/// `EXECUTOR_HEARTBEAT_INTERVAL_SECS` default.
pub const DEFAULT_HEARTBEAT_INTERVAL_SECS: u64 = 5;

/// A connection is declared stale after this many consecutive silent
/// heartbeat intervals (spec: "On no frame for 3 intervals").
pub const STALE_INTERVAL_MULTIPLIER: u32 = 3;

/// Timestamp of the last frame this connection received from the stage,
/// shared between `crate::wire`'s read loop (the writer) and
/// [`run_monitor`] (the reader). `tokio::time::Instant`, deliberately NOT
/// `std::time::SystemTime`/`std::time::Instant`: this crate's own tests
/// drive `run_monitor` under `#[tokio::test(start_paused = true)]` (`src/
/// wire.rs`'s heartbeat tests) to deterministically exercise a multi-
/// second stale timeout in milliseconds of real wall-clock test time --
/// that only works if EVERY clock read in this path (both `sleep`'s and
/// this tracker's) goes through tokio's mockable clock. A `Mutex`, not an
/// atomic: `tokio::time::Instant` is not an integer this module controls
/// the bit-layout of; touches happen at most once per inbound frame
/// (nowhere near hot-path packet-rate frequency), so the lock is never a
/// contention concern.
#[derive(Debug)]
pub struct ActivityTracker {
    last_activity: Mutex<Instant>,
    /// Set once this connection has received a stage-originated `ping` --
    /// see this module's doc comment on "Arming". Never cleared: once a
    /// stage proves it heartbeats, this connection's stale-check stays
    /// armed for the rest of its life.
    server_ping_seen: AtomicBool,
}

impl ActivityTracker {
    /// Starts "now" -- a freshly dialed connection hasn't gone silent yet,
    /// regardless of how long the dial itself took. Unarmed until the
    /// first stage-originated `ping` arrives.
    pub fn new() -> Self {
        Self {
            last_activity: Mutex::new(Instant::now()),
            server_ping_seen: AtomicBool::new(false),
        }
    }

    /// Records that a frame just arrived.
    pub fn touch(&self) {
        *lock(&self.last_activity) = Instant::now();
    }

    /// How long it has been since the last recorded frame.
    pub fn age(&self) -> Duration {
        Instant::now().saturating_duration_since(*lock(&self.last_activity))
    }

    /// Records that the stage just sent its own `ping` -- proof it
    /// heartbeats, arming [`run_monitor`]'s stale-frame-activity check.
    pub fn mark_server_ping_seen(&self) {
        self.server_ping_seen.store(true, Ordering::Relaxed);
    }

    /// Whether the stale-frame-activity check is armed yet (see this
    /// module's doc comment on "Arming").
    pub fn is_armed(&self) -> bool {
        self.server_ping_seen.load(Ordering::Relaxed)
    }
}

impl Default for ActivityTracker {
    fn default() -> Self {
        Self::new()
    }
}

/// A panic while holding this lock (there is none in this module's own
/// code) would be a bug elsewhere; recovering the poisoned guard keeps the
/// tracker usable rather than wedging every future heartbeat check --
/// same rationale as `penguin_bundle_host::wire::CorrelationTable::lock`.
fn lock(m: &Mutex<Instant>) -> std::sync::MutexGuard<'_, Instant> {
    match m.lock() {
        Ok(guard) => guard,
        Err(poisoned) => poisoned.into_inner(),
    }
}

/// Heartbeat behavior for one connection, resolved once from [`CliConfig`]
/// (`crate::bucket::BucketConfig::from_cli` is the precedent for this
/// `from_cli` naming convention in this crate).
#[derive(Debug, Clone, Copy)]
pub struct HeartbeatConfig {
    pub enabled: bool,
    pub interval: Duration,
    pub self_ping_enabled: bool,
}

impl HeartbeatConfig {
    pub fn from_cli(cfg: &CliConfig) -> Self {
        Self {
            enabled: cfg.executor_heartbeat_enabled,
            interval: Duration::from_secs(cfg.executor_heartbeat_interval_secs.max(1)),
            self_ping_enabled: cfg.executor_self_ping_enabled,
        }
    }

    /// No monitor task is spawned at all -- used by `crate::wire`'s tests
    /// that exercise unrelated protocol behavior and don't want a
    /// heartbeat ticking in the background.
    pub fn disabled() -> Self {
        Self {
            enabled: false,
            interval: Duration::from_secs(DEFAULT_HEARTBEAT_INTERVAL_SECS),
            self_ping_enabled: false,
        }
    }
}

/// Reconnect/stale-session counters. No Prometheus/OTel registry exists in
/// this crate (deliberately -- see `Cargo.toml`'s doc comment on why
/// `penguin-logging`/`opentelemetry-otlp` are excluded here, same
/// dependency-minimalism argument that keeps `reqwest` out), so these are
/// plain atomics logged by `crate::run`/this module, same convention as
/// the pre-existing `Executor::orphaned_unload_total`. A future metrics
/// registry can read these directly rather than this module needing to
/// change.
#[derive(Debug, Default)]
pub struct HeartbeatMetrics {
    pub reconnects_total: AtomicU64,
    pub heartbeat_timeouts_total: AtomicU64,
}

impl HeartbeatMetrics {
    pub fn record_reconnect(&self) {
        self.reconnects_total.fetch_add(1, Ordering::Relaxed);
    }

    pub fn record_heartbeat_timeout(&self) {
        self.heartbeat_timeouts_total
            .fetch_add(1, Ordering::Relaxed);
    }
}

/// Bundles everything `crate::wire::run_connection` needs to run this
/// module's monitor for one connection, so adding heartbeat support only
/// grew that function's signature by one parameter instead of four.
pub struct Heartbeat {
    pub cfg: HeartbeatConfig,
    pub metrics: Arc<HeartbeatMetrics>,
    pub probe_file: std::path::PathBuf,
    /// How long a probe-file write may keep failing before `run_monitor`
    /// escalates its log from WARN to ERROR (`EXECUTOR_GRACE_SECONDS` --
    /// the same window `--healthcheck=session`'s default max age uses, so
    /// "the log escalates to ERROR" and "the liveness probe would now be
    /// failing too" line up).
    pub probe_grace: Duration,
    /// Fired once, right after the `hello`/`hello-ok` handshake completes
    /// -- `crate::run` uses it to log the connect event (with how long the
    /// prior outage lasted) and to refresh the probe file immediately,
    /// rather than waiting up to one full heartbeat interval for the first
    /// tick.
    pub on_connected: Option<Box<dyn FnOnce() + Send>>,
}

impl Heartbeat {
    /// No monitor, no probe-file writes, no connect callback -- the
    /// pre-existing behavior every test in `crate::wire` that doesn't
    /// exercise this feature relies on.
    pub fn disabled() -> Self {
        Self {
            cfg: HeartbeatConfig::disabled(),
            metrics: Arc::new(HeartbeatMetrics::default()),
            probe_file: std::path::PathBuf::from(crate::probe::DEFAULT_PROBE_FILE_PATH),
            probe_grace: Duration::from_secs(crate::probe::DEFAULT_GRACE_SECS),
            on_connected: None,
        }
    }
}

/// Runs until the connection is declared stale, then returns the
/// [`ExecutorError::SessionStale`] that caused it -- never returns `Ok`.
/// `crate::wire::run_connection` races this against its read loop; the
/// first of the two to finish wins.
pub async fn run_monitor(
    connection: Arc<Connection>,
    activity: Arc<ActivityTracker>,
    cfg: HeartbeatConfig,
    metrics: Arc<HeartbeatMetrics>,
    peer: String,
    probe_file: std::path::PathBuf,
    probe_grace: Duration,
) -> ExecutorError {
    let stale_after = cfg.interval * STALE_INTERVAL_MULTIPLIER;
    let mut logged_unarmed_notice = false;
    let mut probe_failing_since: Option<Instant> = None;
    loop {
        tokio::time::sleep(cfg.interval).await;

        if cfg.self_ping_enabled {
            debug!(peer = %peer, "sending host-api heartbeat ping");
            match tokio::time::timeout(cfg.interval, connection.request(Message::Ping)).await {
                Ok(Ok(frame)) if matches!(frame.message, Message::Pong) => {
                    debug!(peer = %peer, "heartbeat pong received");
                }
                Ok(Ok(frame)) => {
                    debug!(peer = %peer, message = ?frame.message, "heartbeat ping answered with an unexpected frame kind");
                }
                Ok(Err(e)) => {
                    debug!(peer = %peer, error = %e, "heartbeat ping could not be sent");
                }
                Err(_timeout) => {
                    debug!(peer = %peer, "heartbeat ping timed out waiting for a pong");
                }
            }
        }

        // Arming gate (PR #529 review fix, this module's own doc comment
        // on "Arming"): only an armed connection -- one that has proven
        // the stage itself heartbeats -- can be declared stale on frame
        // silence. Unarmed, TCP keepalive (`crate::tls::dial_stage`) is
        // this connection's only stall detector; the probe file is still
        // refreshed below either way (requirement: liveness must not fail
        // on an idle-but-healthy link).
        if activity.is_armed() {
            let age = activity.age();
            if age >= stale_after {
                metrics.record_heartbeat_timeout();
                // regression: executor stuck on terminated svc pod after rollout (alpha 2026-10-02)
                error!(
                    peer = %peer,
                    last_seen_age_secs = age.as_secs(),
                    stale_after_secs = stale_after.as_secs(),
                    heartbeat_interval_secs = cfg.interval.as_secs(),
                    "host-api session stalled: no frame received from the stage within \
                     the stale threshold -- dropping this connection and reconnecting"
                );
                return ExecutorError::SessionStale {
                    peer,
                    age_secs: age.as_secs(),
                };
            }
            debug!(peer = %peer, last_seen_age_secs = age.as_secs(), "host-api session heartbeat OK");
        } else if !logged_unarmed_notice {
            logged_unarmed_notice = true;
            info!(
                peer = %peer,
                "stage does not heartbeat; stale detection relies on TCP keepalive"
            );
        }

        // Always refreshed while this loop is still running (i.e. the TCP
        // connection is up), armed or not -- an idle-but-healthy link must
        // never fail liveness just because the stale-frame-activity check
        // hasn't armed yet.
        match crate::probe::touch(&probe_file) {
            Ok(()) => {
                probe_failing_since = None;
            }
            Err(e) => {
                // Loud by design (user rule: failures must be loud) -- a
                // misconfigured mount silently degrading to WARN forever
                // would eventually cause a liveness restart with nothing
                // in the logs to explain it.
                let since = *probe_failing_since.get_or_insert(Instant::now());
                let elapsed = since.elapsed();
                if elapsed >= probe_grace {
                    error!(
                        peer = %peer,
                        error = %e,
                        probe_file = ?probe_file,
                        failing_for_secs = elapsed.as_secs(),
                        grace_secs = probe_grace.as_secs(),
                        "liveness probe file has failed to refresh past its grace period -- \
                         a liveness restart may follow with no other explanation"
                    );
                } else {
                    warn!(
                        peer = %peer,
                        error = %e,
                        probe_file = ?probe_file,
                        failing_for_secs = elapsed.as_secs(),
                        "failed to refresh liveness probe file"
                    );
                }
            }
        }
    }
}
