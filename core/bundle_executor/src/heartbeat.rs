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
//! frame arrived (any kind -- a `load`/`invoke`/`unload`/`ping` all count),
//! and [`run_monitor`] races that timestamp against the wall clock,
//! optionally sending this executor's own `ping` to generate traffic on an
//! otherwise-idle-but-healthy connection, and returns
//! [`crate::error::ExecutorError::SessionStale`] the moment the connection
//! goes quiet for too long -- `crate::wire::run_connection` treats that
//! exactly like any other fatal connection error: drop it, let
//! `crate::run`'s existing exponential-backoff loop reconnect.
//!
//! A real half-open socket is *also* caught at the OS level by
//! `crate::tls::dial_stage`'s TCP keepalive (belt and suspenders: keepalive
//! needs no wire-protocol cooperation from the stage at all, so it alone
//! already fixes the exact incident above; this module's frame-level check
//! is the second layer, and the one whose timeout is actually tunable per
//! spec's `HEARTBEAT_INTERVAL`).

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use penguin_bundle_host::wire::Message;
use tokio::time::Instant;
use tracing::{debug, error};

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
}

impl ActivityTracker {
    /// Starts "now" -- a freshly dialed connection hasn't gone silent yet,
    /// regardless of how long the dial itself took.
    pub fn new() -> Self {
        Self {
            last_activity: Mutex::new(Instant::now()),
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
) -> ExecutorError {
    let stale_after = cfg.interval * STALE_INTERVAL_MULTIPLIER;
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
        if let Err(e) = crate::probe::touch(&probe_file) {
            // Never fatal to the connection -- a probe-file write failure
            // (e.g. a misconfigured read-only mount) should surface loudly
            // in logs, not tear down an otherwise-healthy session.
            debug!(peer = %peer, error = %e, probe_file = ?probe_file, "failed to refresh liveness probe file");
        }
    }
}
