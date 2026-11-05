//! The host-side plumbing that drives a connector (spec S2, S2.6, S3.5): the
//! host owns the socket, reads a frame, enforces the size ceiling BEFORE any
//! guest runs, calls the connector's `on-frame`, caps and validates what comes
//! back, and publishes the normalized events. Reconnect/backoff is host code
//! (`crate::ingest::Backoff`, unchanged); a connector never sees a lease, a
//! partition or a reconnect attempt.
//!
//! # Failure classes (spec S2.6)
//!
//! | Outcome of `on-frame` | Reaction |
//! |---|---|
//! | `Ok(events)` | cap, validate each, publish; connection stays up |
//! | `Guest(frame-error)` | this frame is dropped + counted; connection stays up |
//! | `SandboxTrip` / `Unavailable` | connection torn down, counted, backoff, reconnect |
//! | transport `Fatal` | source PARKED: no reconnect, [`RunEnd::Parked`] |
//!
//! Frames are never silently skipped: every one lands in exactly one counter
//! outcome, and the transport is told what happened to it
//! ([`Transport::on_frame_outcome`]). Nothing frame-derived (bytes, actor,
//! payload, a guest-supplied message, a wasmtime error chain) is ever logged
//! or used as a metric label: logs carry error CLASSES only. Flood-prone WARNs
//! (oversize, spoofed platform, rejected/failed events) are sampled by
//! [`super::ratelimit`] -- every occurrence is still counted.

use std::future::Future;
use std::pin::Pin;
use std::sync::Arc;
use std::time::{Duration, Instant};

use prometheus::IntGauge;
use tokio::sync::{mpsc, oneshot, watch};
use tracing::{debug, info, warn, Instrument};

use super::invoker::{ConnectorInvokeError, ConnectorInvoker};
use super::metrics::ConnectorMetrics;
use super::ratelimit::{Admit, PumpLogs};
use super::registry::{ConnectorRegistry, PinnedConnector, RegistryError};
use super::sink::{check_event_limits, EventSink, SinkError};
use super::transport::{FrameDropReason, FrameOutcome, Transport};
use super::types::{ConnectionCtx, ConnectorEvent, RawFrame};
use crate::ingest::{Backoff, STABILITY_WINDOW};

/// A fatal driver error: a configuration/programming fault that retrying
/// cannot fix, so [`ConnectorDriver::run`] returns it instead of looping.
#[derive(Debug, thiserror::Error)]
pub enum DriverError {
    /// The transport's platform has no pinned connector.
    #[error(transparent)]
    Registry(#[from] RegistryError),
    /// The transport built a context for a different platform than it
    /// declares -- a transport bug that would route events into the wrong
    /// stream, so it is refused outright.
    #[error("transport for platform {declared:?} produced a context for {actual:?}")]
    ContextPlatformMismatch {
        /// What `Transport::platform` declared.
        declared: String,
        /// What `Transport::connect`'s context carried.
        actual: String,
    },
    /// The transport declared a heartbeat interval outside
    /// [`MIN_HEARTBEAT_INTERVAL`]..=[`MAX_HEARTBEAT_INTERVAL`]: a too-small
    /// one is a busy-loop (zero included), a too-large one is no heartbeat at
    /// all. A host/config fault, refused before connecting -- only checked
    /// when heartbeats are enabled.
    #[error(
        "transport for platform {platform:?} declared heartbeat interval {interval:?}, \
         outside the allowed {min:?}..={max:?}"
    )]
    HeartbeatIntervalOutOfRange {
        /// The offending transport's platform.
        platform: String,
        /// What the transport declared.
        interval: Duration,
        /// The inclusive lower bound.
        min: Duration,
        /// The inclusive upper bound.
        max: Duration,
    },
}

/// Smallest accepted `Transport::heartbeat_interval`. Below this the schedule
/// is a busy loop that floods the connector with `on-heartbeat-due` calls.
pub const MIN_HEARTBEAT_INTERVAL: Duration = Duration::from_millis(100);

/// Largest accepted `Transport::heartbeat_interval` (one hour): beyond it a
/// "heartbeat" cannot keep any real upstream connection alive.
pub const MAX_HEARTBEAT_INTERVAL: Duration = Duration::from_secs(3600);

/// Default deadline for one heartbeat write. A write that has not completed by
/// then means the socket is stalled; the connection is torn down (and
/// reconnected) rather than letting it freeze the pump.
pub const DEFAULT_HEARTBEAT_SEND_TIMEOUT: Duration = Duration::from_secs(5);

/// One `on-heartbeat-due` round's result, handed from the heartbeat loop to
/// the pump (which owns the transport and so is the only one that can send).
type HeartbeatResult = Result<Option<Vec<u8>>, ConnectorInvokeError>;

/// What to do with one classified heartbeat round.
enum BeatAction {
    /// Write this body (`None` = the platform-default heartbeat).
    Send(Option<Vec<u8>>),
    /// Nothing to write; the connection stays up.
    Skip,
    /// End the connection.
    End(ConnectionEnd),
}

/// How one heartbeat write ended.
enum BeatSend {
    Sent,
    Failed(super::transport::TransportError),
    TimedOut,
}

/// The verdict of processing one frame, applied by the pump once the
/// transport is free again.
struct FrameVerdict {
    outcome: FrameOutcome,
    /// `Some(kind)` = the connector was contained; the connection ends.
    fault: Option<&'static str>,
    /// The connector processed the frame successfully.
    ok: bool,
}

/// Why one connection's lifecycle ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ConnectionEnd {
    /// Shutdown was requested.
    Shutdown,
    /// The remote closed cleanly.
    RemoteClosed,
    /// `Transport::connect` failed.
    ConnectFailed,
    /// `on-connect` or the handshake send failed.
    HandshakeFailed,
    /// Reading a frame failed at the transport level.
    TransportError,
    /// The transport reported a terminal [`super::TransportError::Fatal`]
    /// (e.g. dead credentials): the source is parked, never reconnected.
    Parked,
    /// The connector was contained by the sandbox (or could not be invoked);
    /// the kind is the stable metric label.
    ConnectorFault(&'static str),
}

/// Why [`ConnectorDriver::run`] returned `Ok`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RunEnd {
    /// Shutdown was requested.
    Shutdown,
    /// The transport reported a terminal failure; the source was PARKED (not
    /// retried). The owner should surface "needs re-auth" and not restart it
    /// until credentials change.
    Parked,
}

/// What one [`ConnectorDriver::serve_connection`] call did.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ConnectionReport {
    /// Why it ended.
    pub end: ConnectionEnd,
    /// Whether it got past connect + handshake.
    pub established: bool,
    /// Frames the connector processed successfully on this connection.
    pub frames_ok: u64,
}

/// Whether a finished connection has earned a backoff reset.
///
/// Only a SUSTAINED connection ([`STABILITY_WINDOW`] established) that did not
/// end in a connector fault proves the upstream healthy. A frame-count test
/// (the original rule) let a poison frame at position >= 2 reset the backoff
/// on every cycle and hammer the upstream in a fast reconnect loop; a
/// connector fault says nothing good about the upstream either way.
pub(crate) fn should_reset_backoff(report: &ConnectionReport, connected_for: Duration) -> bool {
    report.established
        && connected_for >= STABILITY_WINDOW
        && !matches!(report.end, ConnectionEnd::ConnectorFault(_))
}

/// Decrements a gauge on drop, so a cancelled/aborted `serve_connection`
/// future cannot leak a permanently-"up" connection.
struct UpGuard(IntGauge);

impl UpGuard {
    fn new(gauge: IntGauge) -> Self {
        gauge.inc();
        Self(gauge)
    }
}

impl Drop for UpGuard {
    fn drop(&mut self) {
        self.0.dec();
    }
}

/// Drives one transport against its pinned connector.
pub struct ConnectorDriver<I: ConnectorInvoker, S: EventSink> {
    registry: Arc<ConnectorRegistry>,
    invoker: Arc<I>,
    sink: Arc<S>,
    metrics: ConnectorMetrics,
    backoff_cap: Duration,
    heartbeat_enabled: bool,
    heartbeat_send_timeout: Duration,
}

impl<I: ConnectorInvoker, S: EventSink> ConnectorDriver<I, S> {
    /// Builds a driver. The default reconnect backoff cap is 30s, matching
    /// the native receivers.
    pub fn new(
        registry: Arc<ConnectorRegistry>,
        invoker: Arc<I>,
        sink: Arc<S>,
        metrics: ConnectorMetrics,
    ) -> Self {
        Self {
            registry,
            invoker,
            sink,
            metrics,
            backoff_cap: Duration::from_secs(30),
            heartbeat_enabled: false,
            heartbeat_send_timeout: DEFAULT_HEARTBEAT_SEND_TIMEOUT,
        }
    }

    /// Opts the driver in to heartbeat scheduling. The caller resolves the
    /// PostHog flag `ingest.connector-heartbeat` (default OFF) and passes the
    /// result; with it OFF the driver behaves exactly as if heartbeats did not
    /// exist (no validation, no timer, no refusal; one DEBUG line per
    /// connection that a declared interval is being ignored).
    #[must_use]
    pub fn with_heartbeat_enabled(mut self, enabled: bool) -> Self {
        self.heartbeat_enabled = enabled;
        self
    }

    /// Overrides the per-heartbeat write deadline
    /// ([`DEFAULT_HEARTBEAT_SEND_TIMEOUT`]).
    #[must_use]
    pub fn with_heartbeat_send_timeout(mut self, timeout: Duration) -> Self {
        self.heartbeat_send_timeout = timeout;
        self
    }

    /// Overrides the reconnect backoff cap (tests use a few milliseconds).
    #[must_use]
    pub fn with_backoff_cap(mut self, cap: Duration) -> Self {
        self.backoff_cap = cap;
        self
    }

    /// Connects, handshakes, pumps frames and reconnects under
    /// `crate::ingest::Backoff` until `shutdown` resolves or the transport
    /// reports a terminal failure ([`RunEnd::Parked`]). Returns `Err` only for
    /// a fatal [`DriverError`]. Backoff is reset ONLY once a connection has
    /// been sustained for [`STABILITY_WINDOW`] without a connector fault (see
    /// [`should_reset_backoff`]) -- never on a bare successful connect and
    /// never merely because a frame or two succeeded before a trap.
    pub async fn run<T: Transport>(
        &self,
        mut transport: T,
        mut shutdown: oneshot::Receiver<()>,
    ) -> Result<RunEnd, DriverError> {
        let mut backoff = Backoff::new(self.backoff_cap);
        loop {
            let started = Instant::now();
            let report = self.serve_connection(&mut transport, &mut shutdown).await?;
            match report.end {
                ConnectionEnd::Shutdown => return Ok(RunEnd::Shutdown),
                ConnectionEnd::Parked => return Ok(RunEnd::Parked),
                _ => {}
            }
            if should_reset_backoff(&report, started.elapsed()) {
                backoff.reset();
            }
            let delay = backoff.delay();
            tokio::select! {
                _ = &mut shutdown => return Ok(RunEnd::Shutdown),
                _ = tokio::time::sleep(delay) => {}
            }
        }
    }

    /// One connection lifecycle: connect, `on-connect`, handshake, frame pump,
    /// teardown. Exposed (not just `run`) so a caller or test can step a
    /// single lifecycle deterministically, with no backoff in the way.
    pub async fn serve_connection<T: Transport>(
        &self,
        transport: &mut T,
        shutdown: &mut oneshot::Receiver<()>,
    ) -> Result<ConnectionReport, DriverError> {
        let platform = transport.platform().to_string();
        let pinned = self.registry.resolve(&platform)?.clone();
        let heartbeat_every = match transport.heartbeat_interval() {
            // Flag OFF: heartbeats do not exist. Nothing is validated or
            // refused -- a bad interval cannot matter to a schedule that
            // never runs.
            Some(_) if !self.heartbeat_enabled => {
                debug!(platform = %platform, "heartbeat flag is OFF; ignoring the transport's heartbeat interval");
                None
            }
            Some(d) if !(MIN_HEARTBEAT_INTERVAL..=MAX_HEARTBEAT_INTERVAL).contains(&d) => {
                return Err(DriverError::HeartbeatIntervalOutOfRange {
                    platform,
                    interval: d,
                    min: MIN_HEARTBEAT_INTERVAL,
                    max: MAX_HEARTBEAT_INTERVAL,
                });
            }
            Some(d) => Some(d),
            None => None,
        };

        let ctx = tokio::select! {
            _ = &mut *shutdown => return Ok(report(ConnectionEnd::Shutdown, false, 0)),
            result = transport.connect() => match result {
                Ok(ctx) => ctx,
                Err(e) => {
                    let outcome = if e.is_fatal() { "fatal" } else { "failed" };
                    self.metrics.connects_total.with_label_values(&[&platform, outcome]).inc();
                    warn!(platform = %platform, kind = e.kind(), "connector transport connect failed");
                    debug!(platform = %platform, reason = %e, "connector transport connect failure reason");
                    if e.is_fatal() {
                        self.park(&platform);
                        return Ok(report(ConnectionEnd::Parked, false, 0));
                    }
                    return Ok(report(ConnectionEnd::ConnectFailed, false, 0));
                }
            },
        };
        if ctx.platform != platform {
            transport.close("context_platform_mismatch").await;
            return Err(DriverError::ContextPlatformMismatch {
                declared: platform,
                actual: ctx.platform,
            });
        }
        self.metrics
            .connects_total
            .with_label_values(&[&platform, "ok"])
            .inc();

        let handshake = match self.invoker.on_connect(&platform, &ctx).await {
            Ok(handshake) => handshake,
            Err(e) => {
                let end = self.connect_phase_failure(&platform, &e);
                transport.close("on_connect_failed").await;
                return Ok(report(end, false, 0));
            }
        };
        if let Err(e) = transport.send_handshake(handshake).await {
            warn!(platform = %platform, kind = e.kind(), "connector handshake send failed");
            debug!(platform = %platform, reason = %e, "connector handshake failure reason");
            if e.is_fatal() {
                self.park(&platform);
                transport.close("parked").await;
                return Ok(report(ConnectionEnd::Parked, false, 0));
            }
            transport.close("handshake_failed").await;
            return Ok(report(ConnectionEnd::HandshakeFailed, false, 0));
        }

        info!(platform = %platform, connection_id = %ctx.connection_id, "connector connection established");
        let up = UpGuard::new(self.metrics.connection_up.with_label_values(&[&platform]));
        // The heartbeat loop and the pump run concurrently in this one task:
        // the loop only borrows the invoker, the pump owns the transport, and
        // a result crosses a bounded (1) channel -- so a slow
        // `on-heartbeat-due` never delays `on-frame`, and at most one
        // heartbeat is in flight per connection. Dropping either side on
        // return cancels the other (shutdown/teardown safe).
        let (beat_tx, mut beat_rx) = mpsc::channel::<HeartbeatResult>(1);
        // The interval can change mid-connection (a transport that captures
        // it from a frame, e.g. a HELLO): the pump re-reads
        // `Transport::heartbeat_interval` after every frame and publishes a
        // change here; the loop restarts its schedule on it.
        let (interval_tx, interval_rx) = watch::channel(heartbeat_every);
        // Scoped so the heartbeat future (and any `on-heartbeat-due` still in
        // flight inside it) is DROPPED here, before `on-disconnect` runs --
        // never left suspended across teardown holding invoker resources.
        let (end, frames_ok) = {
            let beats = self.heartbeat_loop(&platform, &ctx, interval_rx, beat_tx);
            tokio::pin!(beats);
            let pump = self.pump(
                transport,
                shutdown,
                &platform,
                &pinned,
                &ctx,
                &mut beat_rx,
                &interval_tx,
            );
            tokio::select! {
                out = pump => out,
                () = &mut beats, if self.heartbeat_enabled => {
                    // Unreachable while the pump (which owns the receiver)
                    // lives; surfaced loudly rather than ignored.
                    warn!(platform = %platform, "heartbeat loop exited unexpectedly");
                    (ConnectionEnd::ConnectorFault("heartbeat_loop_exited"), 0)
                }
            }
        };
        drop(up);
        if end == ConnectionEnd::Parked {
            self.park(&platform);
        }

        let reason = end_reason(end);
        if let Err(e) = self.invoker.on_disconnect(&platform, &ctx, reason).await {
            // The connection is already gone; a failed notification is logged
            // (never swallowed silently) and the lifecycle still completes.
            debug!(platform = %platform, kind = e.kind(), "connector on-disconnect failed");
        }
        transport.close(reason).await;
        info!(platform = %platform, reason, frames_ok, "connector connection ended");
        Ok(report(end, true, frames_ok))
    }

    /// The per-connection heartbeat loop: every interval (first beat one
    /// period after the interval is known, missed ticks delayed not burst) it
    /// calls `on-heartbeat-due` and hands the result to the pump. The
    /// interval arrives on a watch channel: `None` = idle until one is set,
    /// a change restarts the schedule. Returns only when the pump is gone.
    async fn heartbeat_loop(
        &self,
        platform: &str,
        ctx: &ConnectionCtx,
        mut interval: watch::Receiver<Option<Duration>>,
        tx: mpsc::Sender<HeartbeatResult>,
    ) {
        loop {
            let Some(every) = *interval.borrow_and_update() else {
                if interval.changed().await.is_err() {
                    return;
                }
                continue;
            };
            let mut ticker = tokio::time::interval_at(tokio::time::Instant::now() + every, every);
            ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
            loop {
                tokio::select! {
                    changed = interval.changed() => {
                        if changed.is_err() {
                            return;
                        }
                        break;
                    }
                    _ = ticker.tick() => {}
                }
                let started = Instant::now();
                let result = self
                    .invoker
                    .on_heartbeat_due(platform, ctx)
                    .instrument(
                        tracing::info_span!("connector.on_heartbeat_due", platform = %platform),
                    )
                    .await;
                self.metrics
                    .heartbeat_duration_seconds
                    .with_label_values(&[platform])
                    .observe(started.elapsed().as_secs_f64());
                // Bounded hand-off: waits (and so skips ticks) while the pump
                // has not yet consumed the previous result. A closed receiver
                // means the pump returned.
                if tx.send(result).await.is_err() {
                    return;
                }
            }
        }
    }

    /// Re-reads the transport's heartbeat interval and publishes a change to
    /// the loop. A value outside the driver bounds is a transport fault (the
    /// interval was captured from the wire): `Some(end)` ends the connection.
    fn sync_heartbeat<T: Transport>(
        &self,
        transport: &T,
        platform: &str,
        tx: &watch::Sender<Option<Duration>>,
    ) -> Option<ConnectionEnd> {
        let now = transport.heartbeat_interval();
        if now == *tx.borrow() {
            return None;
        }
        if let Some(d) = now {
            if !(MIN_HEARTBEAT_INTERVAL..=MAX_HEARTBEAT_INTERVAL).contains(&d) {
                warn!(platform = %platform, "transport heartbeat interval out of range, tearing the connection down");
                return Some(ConnectionEnd::TransportError);
            }
        }
        debug!(platform = %platform, "transport heartbeat interval changed");
        tx.send_replace(now);
        None
    }

    /// Classifies one heartbeat round's result. Non-send outcomes are counted
    /// and logged here; `Send(body)` is for the caller to write, where
    /// `None` means the connector asked for the platform-default heartbeat
    /// (WIT `on-heartbeat-due`: `none` = "host sends its platform-default
    /// heartbeat unchanged").
    fn classify_beat(&self, platform: &str, beat: Option<HeartbeatResult>) -> BeatAction {
        match beat {
            Some(Ok(body)) => BeatAction::Send(body),
            Some(Err(ConnectorInvokeError::Guest(e))) => {
                self.metrics.heartbeat(platform, "guest_error");
                debug!(platform = %platform, kind = e.kind(), "connector declined a heartbeat, skipped");
                BeatAction::Skip
            }
            Some(Err(other)) => {
                self.metrics.heartbeat(platform, other.kind());
                self.metrics.trap(platform, other.kind());
                warn!(platform = %platform, kind = other.kind(),
                    "connector contained or unavailable during heartbeat, tearing the connection down");
                BeatAction::End(ConnectionEnd::ConnectorFault(other.kind()))
            }
            None => {
                // The pump owns the receiver and the loop owns the sender for
                // the whole connection; a closed channel is a driver bug,
                // surfaced loudly.
                warn!(platform = %platform, "heartbeat channel closed unexpectedly");
                self.metrics.heartbeat(platform, "channel_closed");
                BeatAction::End(ConnectionEnd::ConnectorFault("heartbeat_channel_closed"))
            }
        }
    }

    /// Writes one heartbeat under the send deadline. Dropping the write on
    /// expiry cancels it; the caller then tears the connection down, so a
    /// half-written frame never reaches a reused stream.
    async fn send_beat<T: Transport>(&self, transport: &mut T, body: Option<Vec<u8>>) -> BeatSend {
        let write = async {
            match body {
                Some(bytes) => transport.send_heartbeat(bytes).await,
                None => transport.send_default_heartbeat().await,
            }
        };
        match tokio::time::timeout(self.heartbeat_send_timeout, write).await {
            Ok(Ok(())) => BeatSend::Sent,
            Ok(Err(e)) => BeatSend::Failed(e),
            Err(_) => BeatSend::TimedOut,
        }
    }

    /// Counts/logs one finished heartbeat write; `Some(end)` ends the
    /// connection.
    fn finish_beat(
        &self,
        platform: &str,
        defaulted: bool,
        sent: BeatSend,
    ) -> Option<ConnectionEnd> {
        match sent {
            BeatSend::Sent => {
                self.metrics
                    .heartbeat(platform, if defaulted { "default_sent" } else { "sent" });
                None
            }
            BeatSend::TimedOut => {
                self.metrics.heartbeat(platform, "send_timeout");
                warn!(platform = %platform, timeout_ms = self.heartbeat_send_timeout.as_millis() as u64,
                    "connector heartbeat send timed out, tearing the connection down");
                Some(ConnectionEnd::TransportError)
            }
            BeatSend::Failed(e) => {
                self.metrics.heartbeat(platform, "send_failed");
                warn!(platform = %platform, kind = e.kind(), "connector heartbeat send failed");
                debug!(platform = %platform, reason = %e, "connector heartbeat send failure reason");
                Some(if e.is_fatal() {
                    ConnectionEnd::Parked
                } else {
                    ConnectionEnd::TransportError
                })
            }
        }
    }

    /// Handles one due heartbeat while `work` (the in-flight frame processing,
    /// or a never-ready future when idle) keeps being polled: the send needs
    /// the transport, `work` does not, so a stalled write cannot freeze frame
    /// work already in flight. `work`'s output lands in `done`. Returns
    /// `Some(end)` when the connection must end (shutdown, send failure, a
    /// connector fault).
    async fn run_beat<T: Transport, W: Future>(
        &self,
        transport: &mut T,
        shutdown: &mut oneshot::Receiver<()>,
        platform: &str,
        beat: Option<HeartbeatResult>,
        mut work: Pin<&mut W>,
        done: &mut Option<W::Output>,
    ) -> Option<ConnectionEnd> {
        let body = match self.classify_beat(platform, beat) {
            BeatAction::Send(body) => body,
            BeatAction::Skip => return None,
            BeatAction::End(end) => return Some(end),
        };
        let defaulted = body.is_none();
        let send = self.send_beat(transport, body);
        tokio::pin!(send);
        let sent = loop {
            tokio::select! {
                _ = &mut *shutdown => return Some(ConnectionEnd::Shutdown),
                out = work.as_mut(), if done.is_none() => *done = Some(out),
                sent = &mut send => break sent,
            }
        };
        self.finish_beat(platform, defaulted, sent)
    }

    /// Counts and logs one source being parked on a terminal failure.
    fn park(&self, platform: &str) {
        self.metrics
            .parked_total
            .with_label_values(&[platform])
            .inc();
        warn!(platform = %platform, "connector source parked on a terminal transport failure; not reconnecting");
    }

    /// Classifies a failure during `on-connect`.
    fn connect_phase_failure(&self, platform: &str, err: &ConnectorInvokeError) -> ConnectionEnd {
        match err {
            ConnectorInvokeError::Guest(_) => {
                warn!(platform = %platform, kind = err.kind(), "connector refused to build a handshake");
                ConnectionEnd::HandshakeFailed
            }
            other => {
                self.metrics.trap(platform, other.kind());
                // Class only: the error's payload can derive from guest text.
                warn!(platform = %platform, kind = other.kind(), "connector on-connect could not run");
                ConnectionEnd::ConnectorFault(other.kind())
            }
        }
    }

    /// The frame pump: returns why it ended and how many frames succeeded.
    /// Every yielded frame is reported back to the transport exactly once via
    /// [`Transport::on_frame_outcome`] before the next read.
    ///
    /// Heartbeats are serviced in both phases: while waiting for a frame, and
    /// while a frame is being processed (`on-frame` + publish only borrow the
    /// invoker/sink, not the transport, so a due heartbeat is written without
    /// waiting for a slow guest or sink). The one structural limit: the
    /// transport is a single `&mut`, so reading the NEXT frame pauses while a
    /// heartbeat write is in progress -- bounded by the send deadline, after
    /// which the connection is torn down.
    #[allow(clippy::too_many_arguments)]
    async fn pump<T: Transport>(
        &self,
        transport: &mut T,
        shutdown: &mut oneshot::Receiver<()>,
        platform: &str,
        pinned: &PinnedConnector,
        ctx: &ConnectionCtx,
        beat_rx: &mut mpsc::Receiver<HeartbeatResult>,
        interval_tx: &watch::Sender<Option<Duration>>,
    ) -> (ConnectionEnd, u64) {
        let heartbeat_on = self.heartbeat_enabled;
        let mut frames_ok = 0u64;
        let mut logs = PumpLogs::new();
        loop {
            // Unbiased: frames and heartbeat results get fair turns, so
            // neither starves the other.
            let next = tokio::select! {
                _ = &mut *shutdown => return (ConnectionEnd::Shutdown, frames_ok),
                beat = beat_rx.recv(), if heartbeat_on => {
                    let idle = std::future::pending::<()>();
                    tokio::pin!(idle);
                    let mut never = None;
                    if let Some(end) = self
                        .run_beat(transport, shutdown, platform, beat, idle, &mut never)
                        .await
                    {
                        return (end, frames_ok);
                    }
                    continue;
                }
                next = transport.next_frame() => next,
            };
            let bytes = match next {
                Ok(Some(bytes)) => bytes,
                Ok(None) => return (ConnectionEnd::RemoteClosed, frames_ok),
                Err(e) => {
                    warn!(platform = %platform, kind = e.kind(), "connector transport read failed");
                    debug!(platform = %platform, reason = %e, "connector transport read failure reason");
                    let end = if e.is_fatal() {
                        ConnectionEnd::Parked
                    } else {
                        ConnectionEnd::TransportError
                    };
                    return (end, frames_ok);
                }
            };

            // The frame may have carried a new heartbeat interval (HELLO).
            if heartbeat_on {
                if let Some(end) = self.sync_heartbeat(transport, platform, interval_tx) {
                    return (end, frames_ok);
                }
            }

            // Host-side ceiling, before any guest runs (spec S3.5).
            if bytes.len() > pinned.max_frame_bytes {
                self.metrics.frame(platform, "oversize");
                if let Admit::Emit { suppressed_before } = logs.oversize.admit() {
                    warn!(platform = %platform, len = bytes.len(), max = pinned.max_frame_bytes,
                        suppressed_before,
                        "connector frame exceeds the platform ceiling, dropped before invocation");
                }
                transport
                    .on_frame_outcome(FrameOutcome::Dropped(FrameDropReason::Oversize))
                    .await;
                continue;
            }

            let work = self.process_frame(platform, pinned, ctx, bytes, &mut logs);
            tokio::pin!(work);
            let mut done: Option<FrameVerdict> = None;
            let mut early_end: Option<ConnectionEnd> = None;
            let verdict = loop {
                if let Some(v) = done.take() {
                    break Some(v);
                }
                tokio::select! {
                    _ = &mut *shutdown => {
                        early_end = Some(ConnectionEnd::Shutdown);
                        break None;
                    }
                    v = work.as_mut() => break Some(v),
                    beat = beat_rx.recv(), if heartbeat_on => {
                        if let Some(end) = self
                            .run_beat(transport, shutdown, platform, beat, work.as_mut(), &mut done)
                            .await
                        {
                            early_end = Some(end);
                            // A frame that finished during the beat keeps its
                            // real verdict; one still in flight is dropped
                            // with the connection.
                            break done.take();
                        }
                    }
                }
            };
            // Report the frame exactly once. In flight at teardown = not
            // delivered: a stateful transport holds its cursor and re-yields.
            let verdict = verdict.unwrap_or(FrameVerdict {
                outcome: FrameOutcome::Dropped(FrameDropReason::ConnectorFault),
                fault: None,
                ok: false,
            });
            if verdict.ok {
                frames_ok += 1;
            }
            transport.on_frame_outcome(verdict.outcome).await;
            if let Some(end) = early_end {
                return (end, frames_ok);
            }
            if let Some(kind) = verdict.fault {
                return (ConnectionEnd::ConnectorFault(kind), frames_ok);
            }
        }
    }

    /// Runs one frame through the connector and publishes its events. Touches
    /// only the invoker/sink (never the transport), so the pump can write a
    /// heartbeat while this is in flight; the transport is told the verdict
    /// afterwards.
    async fn process_frame(
        &self,
        platform: &str,
        pinned: &PinnedConnector,
        ctx: &ConnectionCtx,
        bytes: Vec<u8>,
        logs: &mut PumpLogs,
    ) -> FrameVerdict {
        let frame = RawFrame {
            ctx: ctx.clone(),
            bytes,
            received_at: now_rfc3339_millis(),
        };
        let started = Instant::now();
        let result = self
            .invoker
            .on_frame(platform, frame)
            .instrument(tracing::info_span!("connector.on_frame", platform = %platform))
            .await;
        self.metrics
            .on_frame_duration_seconds
            .with_label_values(&[platform])
            .observe(started.elapsed().as_secs_f64());

        match result {
            Ok(events) => {
                self.metrics.frame(platform, "ok");
                let all_published = self
                    .publish_events(platform, pinned, ctx, events, logs)
                    .await;
                FrameVerdict {
                    outcome: if all_published {
                        FrameOutcome::Published
                    } else {
                        FrameOutcome::Dropped(FrameDropReason::PublishIncomplete)
                    },
                    fault: None,
                    ok: true,
                }
            }
            Err(ConnectorInvokeError::Guest(frame_error)) => {
                self.metrics.frame(platform, "guest_error");
                debug!(platform = %platform, kind = frame_error.kind(),
                    "connector reported a bad frame, dropped");
                FrameVerdict {
                    outcome: FrameOutcome::Dropped(FrameDropReason::GuestError),
                    fault: None,
                    ok: false,
                }
            }
            Err(other) => {
                self.metrics.frame(platform, other.kind());
                self.metrics.trap(platform, other.kind());
                // Class only -- never the error payload (guest-derived).
                warn!(platform = %platform, kind = other.kind(),
                    "connector contained or unavailable, tearing the connection down");
                FrameVerdict {
                    outcome: FrameOutcome::Dropped(FrameDropReason::ConnectorFault),
                    fault: Some(other.kind()),
                    ok: false,
                }
            }
        }
    }

    /// Caps, platform-checks, size-checks and publishes one `on-frame`'s
    /// events. Returns true only when EVERY returned event was published (the
    /// signal a stateful transport needs before advancing its cursor).
    async fn publish_events(
        &self,
        platform: &str,
        pinned: &PinnedConnector,
        ctx: &ConnectionCtx,
        mut events: Vec<ConnectorEvent>,
        logs: &mut PumpLogs,
    ) -> bool {
        let mut all_published = true;
        let cap = pinned.max_events_per_frame;
        if events.len() > cap {
            all_published = false;
            let dropped = events.len() - cap;
            if let Admit::Emit { suppressed_before } = logs.truncated.admit() {
                warn!(platform = %platform, returned = events.len(), cap, suppressed_before,
                    "connector returned more events than the per-frame cap, truncating");
            }
            self.metrics
                .events_total
                .with_label_values(&[platform, "truncated"])
                .inc_by(dropped as u64);
            events.truncate(cap);
        }
        let limits = &pinned.event_limits;
        let mut frame_bytes = 0usize;
        for event in events {
            // A connector must never write into another platform's stream: the
            // stream key derives from the event's platform.
            if event.platform != platform {
                all_published = false;
                self.metrics.event(platform, "platform_mismatch");
                if let Admit::Emit { suppressed_before } = logs.mismatch.admit() {
                    warn!(platform = %platform, suppressed_before,
                        "connector event claims a different platform, dropped");
                }
                continue;
            }
            // Host-side size caps + event_type shape (typed, loud, never a
            // silent truncation), then the per-frame aggregate bound.
            frame_bytes = frame_bytes.saturating_add(event.payload_json.len());
            let rejected = check_event_limits(&event, limits).err().or_else(|| {
                (frame_bytes > limits.max_frame_events_bytes).then_some(SinkError::Oversize {
                    field: "frame_events_bytes",
                    len: frame_bytes,
                    max: limits.max_frame_events_bytes,
                })
            });
            if let Some(e) = rejected {
                all_published = false;
                self.metrics.event(platform, e.kind());
                if let Admit::Emit { suppressed_before } = logs.rejected.admit() {
                    // `SinkError`'s Display carries only a field name and two
                    // lengths / a fixed description -- never a guest value.
                    warn!(platform = %platform, kind = e.kind(), reason = %e, suppressed_before,
                        "connector event rejected by the host, dropped");
                }
                continue;
            }
            match self.sink.publish(ctx, event).await {
                Ok(()) => self.metrics.event(platform, "published"),
                Err(e) => {
                    all_published = false;
                    self.metrics.event(platform, e.kind());
                    if let Admit::Emit { suppressed_before } = logs.publish.admit() {
                        warn!(platform = %platform, kind = e.kind(), suppressed_before,
                            "connector event not published");
                    }
                }
            }
        }
        all_published
    }
}

fn report(end: ConnectionEnd, established: bool, frames_ok: u64) -> ConnectionReport {
    ConnectionReport {
        end,
        established,
        frames_ok,
    }
}

/// A stable, PII-free reason string for `on-disconnect`/`Transport::close`.
fn end_reason(end: ConnectionEnd) -> &'static str {
    match end {
        ConnectionEnd::Shutdown => "shutdown",
        ConnectionEnd::RemoteClosed => "remote_closed",
        ConnectionEnd::ConnectFailed => "connect_failed",
        ConnectionEnd::HandshakeFailed => "handshake_failed",
        ConnectionEnd::TransportError => "transport_error",
        ConnectionEnd::Parked => "parked",
        ConnectionEnd::ConnectorFault(_) => "connector_fault",
    }
}

/// RFC 3339, UTC, millisecond precision, `Z` suffix -- the arrival time the
/// host stamps on every frame.
fn now_rfc3339_millis() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// regression: a poison frame at position >= 2 used to reset the backoff
    /// (`frames_ok > 0`) every cycle, giving a fast reconnect loop.
    #[test]
    fn frames_before_a_trap_do_not_reset_the_backoff() {
        let trapped_after_frames = report(ConnectionEnd::ConnectorFault("trap"), true, 3);
        assert!(!should_reset_backoff(
            &trapped_after_frames,
            Duration::from_millis(50)
        ));
        // Not even a long-lived connection that ended in a connector fault.
        assert!(!should_reset_backoff(
            &trapped_after_frames,
            STABILITY_WINDOW * 2
        ));
        // Nor frames-then-remote-close inside the stability window.
        assert!(!should_reset_backoff(
            &report(ConnectionEnd::RemoteClosed, true, 10),
            Duration::from_secs(1)
        ));
    }

    #[test]
    fn only_a_sustained_non_faulting_connection_resets_the_backoff() {
        assert!(should_reset_backoff(
            &report(ConnectionEnd::RemoteClosed, true, 0),
            STABILITY_WINDOW
        ));
        assert!(should_reset_backoff(
            &report(ConnectionEnd::TransportError, true, 5),
            STABILITY_WINDOW + Duration::from_secs(1)
        ));
        assert!(!should_reset_backoff(
            &report(ConnectionEnd::ConnectFailed, false, 0),
            STABILITY_WINDOW * 3
        ));
    }

    /// The loop-level effect: repeated trap-after-frames cycles keep growing
    /// the backoff ceiling; only a sustained connection brings it back down.
    #[test]
    fn repeated_poison_cycles_grow_the_backoff_until_a_sustained_connection() {
        let mut backoff = Backoff::seeded(Duration::from_secs(30), 7);
        let poison = report(ConnectionEnd::ConnectorFault("trap"), true, 4);
        for _ in 0..5 {
            if should_reset_backoff(&poison, Duration::from_millis(20)) {
                backoff.reset();
            }
            let _ = backoff.delay();
        }
        assert_eq!(backoff.ceiling(), Duration::from_secs(30));
        let healthy = report(ConnectionEnd::RemoteClosed, true, 4);
        if should_reset_backoff(&healthy, STABILITY_WINDOW) {
            backoff.reset();
        }
        assert_eq!(backoff.ceiling(), Duration::from_secs(1));
    }
}
