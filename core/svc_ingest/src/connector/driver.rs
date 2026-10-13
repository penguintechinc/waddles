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
//!
//! Frames are never silently skipped: every one lands in exactly one counter
//! outcome. Nothing frame-derived (bytes, actor, payload, a guest-supplied
//! message) is ever logged or used as a metric label.

use std::sync::Arc;
use std::time::{Duration, Instant};

use tokio::sync::oneshot;
use tracing::{debug, info, warn, Instrument};

use super::invoker::{ConnectorInvokeError, ConnectorInvoker};
use super::metrics::ConnectorMetrics;
use super::registry::{ConnectorRegistry, PinnedConnector, RegistryError};
use super::sink::EventSink;
use super::transport::Transport;
use super::types::{ConnectionCtx, RawFrame};
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
    /// The connector was contained by the sandbox (or could not be invoked);
    /// the kind is the stable metric label.
    ConnectorFault(&'static str),
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

/// Drives one transport against its pinned connector.
pub struct ConnectorDriver<I: ConnectorInvoker, S: EventSink> {
    registry: Arc<ConnectorRegistry>,
    invoker: Arc<I>,
    sink: Arc<S>,
    metrics: ConnectorMetrics,
    backoff_cap: Duration,
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
        }
    }

    /// Overrides the reconnect backoff cap (tests use a few milliseconds).
    #[must_use]
    pub fn with_backoff_cap(mut self, cap: Duration) -> Self {
        self.backoff_cap = cap;
        self
    }

    /// Connects, handshakes, pumps frames and reconnects under
    /// `crate::ingest::Backoff` until `shutdown` resolves. Returns `Err` only
    /// for a fatal [`DriverError`]. Backoff is reset ONLY once a connection has
    /// proven itself (a frame processed, or [`STABILITY_WINDOW`] connected) --
    /// never on a bare successful connect (see `Backoff`'s incident doc).
    pub async fn run<T: Transport>(
        &self,
        mut transport: T,
        mut shutdown: oneshot::Receiver<()>,
    ) -> Result<(), DriverError> {
        let mut backoff = Backoff::new(self.backoff_cap);
        loop {
            let started = Instant::now();
            let report = self.serve_connection(&mut transport, &mut shutdown).await?;
            if report.end == ConnectionEnd::Shutdown {
                return Ok(());
            }
            if report.frames_ok > 0
                || (report.established && started.elapsed() >= STABILITY_WINDOW)
            {
                backoff.reset();
            }
            let delay = backoff.delay();
            tokio::select! {
                _ = &mut shutdown => return Ok(()),
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

        let ctx = tokio::select! {
            _ = &mut *shutdown => return Ok(report(ConnectionEnd::Shutdown, false, 0)),
            result = transport.connect() => match result {
                Ok(ctx) => ctx,
                Err(e) => {
                    self.metrics.connects_total.with_label_values(&[&platform, "failed"]).inc();
                    warn!(platform = %platform, error = %e, "connector transport connect failed");
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
        self.metrics.connects_total.with_label_values(&[&platform, "ok"]).inc();

        let handshake = match self.invoker.on_connect(&platform, &ctx).await {
            Ok(handshake) => handshake,
            Err(e) => {
                let end = self.connect_phase_failure(&platform, &e);
                transport.close("on_connect_failed").await;
                return Ok(report(end, false, 0));
            }
        };
        if let Err(e) = transport.send_handshake(handshake).await {
            warn!(platform = %platform, error = %e, "connector handshake send failed");
            transport.close("handshake_failed").await;
            return Ok(report(ConnectionEnd::HandshakeFailed, false, 0));
        }

        info!(platform = %platform, connection_id = %ctx.connection_id, "connector connection established");
        self.metrics.connection_up.with_label_values(&[&platform]).set(1);
        let (end, frames_ok) = self.pump(transport, shutdown, &platform, &pinned, &ctx).await;
        self.metrics.connection_up.with_label_values(&[&platform]).set(0);

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

    /// Classifies a failure during `on-connect`.
    fn connect_phase_failure(&self, platform: &str, err: &ConnectorInvokeError) -> ConnectionEnd {
        match err {
            ConnectorInvokeError::Guest(_) => {
                warn!(platform = %platform, kind = err.kind(), "connector refused to build a handshake");
                ConnectionEnd::HandshakeFailed
            }
            other => {
                self.metrics.trap(platform, other.kind());
                warn!(platform = %platform, kind = other.kind(), "connector on-connect could not run");
                debug!(platform = %platform, detail = %other, "connector on-connect failure detail");
                ConnectionEnd::ConnectorFault(other.kind())
            }
        }
    }

    /// The frame pump: returns why it ended and how many frames succeeded.
    async fn pump<T: Transport>(
        &self,
        transport: &mut T,
        shutdown: &mut oneshot::Receiver<()>,
        platform: &str,
        pinned: &PinnedConnector,
        ctx: &ConnectionCtx,
    ) -> (ConnectionEnd, u64) {
        let mut frames_ok = 0u64;
        loop {
            let next = tokio::select! {
                _ = &mut *shutdown => return (ConnectionEnd::Shutdown, frames_ok),
                next = transport.next_frame() => next,
            };
            let bytes = match next {
                Ok(Some(bytes)) => bytes,
                Ok(None) => return (ConnectionEnd::RemoteClosed, frames_ok),
                Err(e) => {
                    warn!(platform = %platform, error = %e, "connector transport read failed");
                    return (ConnectionEnd::TransportError, frames_ok);
                }
            };

            // Host-side ceiling, before any guest runs (spec S3.5).
            if bytes.len() > pinned.max_frame_bytes {
                self.metrics.frame(platform, "oversize");
                warn!(platform = %platform, len = bytes.len(), max = pinned.max_frame_bytes,
                    "connector frame exceeds the platform ceiling, dropped before invocation");
                continue;
            }

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
                    frames_ok += 1;
                    self.metrics.frame(platform, "ok");
                    self.publish_events(platform, pinned, ctx, events).await;
                }
                Err(ConnectorInvokeError::Guest(frame_error)) => {
                    self.metrics.frame(platform, "guest_error");
                    debug!(platform = %platform, kind = frame_error.kind(),
                        "connector reported a bad frame, dropped");
                }
                Err(other) => {
                    self.metrics.frame(platform, other.kind());
                    self.metrics.trap(platform, other.kind());
                    warn!(platform = %platform, kind = other.kind(),
                        "connector contained or unavailable, tearing the connection down");
                    debug!(platform = %platform, detail = %other, "connector failure detail");
                    return (ConnectionEnd::ConnectorFault(other.kind()), frames_ok);
                }
            }
        }
    }

    /// Caps, platform-checks and publishes one `on-frame`'s events.
    async fn publish_events(
        &self,
        platform: &str,
        pinned: &PinnedConnector,
        ctx: &ConnectionCtx,
        mut events: Vec<super::types::ConnectorEvent>,
    ) {
        let cap = pinned.max_events_per_frame;
        if events.len() > cap {
            let dropped = events.len() - cap;
            warn!(platform = %platform, returned = events.len(), cap,
                "connector returned more events than the per-frame cap, truncating");
            self.metrics
                .events_total
                .with_label_values(&[platform, "truncated"])
                .inc_by(dropped as u64);
            events.truncate(cap);
        }
        for event in events {
            // A connector must never write into another platform's stream: the
            // stream key derives from the event's platform.
            if event.platform != platform {
                self.metrics.event(platform, "platform_mismatch");
                warn!(platform = %platform, "connector event claims a different platform, dropped");
                continue;
            }
            match self.sink.publish(ctx, event).await {
                Ok(()) => self.metrics.event(platform, "published"),
                Err(e) => {
                    self.metrics.event(platform, e.kind());
                    warn!(platform = %platform, kind = e.kind(), "connector event not published");
                    debug!(platform = %platform, detail = %e, "connector event failure detail");
                }
            }
        }
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
        ConnectionEnd::ConnectorFault(_) => "connector_fault",
    }
}

/// RFC 3339, UTC, millisecond precision, `Z` suffix -- the arrival time the
/// host stamps on every frame.
fn now_rfc3339_millis() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true)
}
