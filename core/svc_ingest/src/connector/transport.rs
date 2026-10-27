//! The host-owned transport abstraction (spec S2). A `Transport` owns
//! everything about getting raw frames off a platform -- connect, reconnect
//! discipline is the driver's, the socket / poll / webhook is the
//! transport's -- and nothing about what a frame *means*. The connector
//! bundle only ever sees `receiver` calls.
//!
//! One impl per transport family (WebSocket, IRC, webhook, polling, Pusher).
//! B0 ships the trait and the registration seam only: no real platform
//! transport is implemented here (those are later phases).

use super::types::{ConnectionCtx, Handshake};

/// A transport-level failure. Always a reason string for diagnostics, never
/// frame content or a credential.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum TransportError {
    /// Opening the connection failed (DNS, TCP/TLS, auth rejected).
    #[error("connect failed: {0}")]
    Connect(String),
    /// Sending the handshake failed, including a failed secret-handle
    /// substitution (the transport owns that last step, spec S2.1).
    #[error("handshake failed: {0}")]
    Handshake(String),
    /// Reading the next frame failed (reset, protocol error, timeout).
    #[error("read failed: {0}")]
    Read(String),
    /// A TERMINAL failure that reconnecting cannot fix -- e.g. the upstream
    /// answered 401/403 and the credential is dead. The driver PARKS the
    /// source on this (stops, never backoff-retries) so a revoked token is
    /// not hammered forever; the transport records `needs_reauth` (or
    /// equivalent) itself when it returns it. May come from
    /// [`Transport::connect`], [`Transport::send_handshake`] or
    /// [`Transport::next_frame`].
    #[error("fatal: {0}")]
    Fatal(String),
}

impl TransportError {
    /// Stable, low-cardinality class label for logs/metrics (never the
    /// reason text).
    #[must_use]
    pub fn kind(&self) -> &'static str {
        match self {
            Self::Connect(_) => "connect",
            Self::Handshake(_) => "handshake",
            Self::Read(_) => "read",
            Self::Fatal(_) => "fatal",
        }
    }

    /// True for the terminal class that parks the source instead of retrying.
    #[must_use]
    pub fn is_fatal(&self) -> bool {
        matches!(self, Self::Fatal(_))
    }
}

/// Why a yielded frame did not end up fully published.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FrameDropReason {
    /// Over the platform's size ceiling; dropped before any guest ran.
    Oversize,
    /// The connector reported a bad frame (`frame-error`).
    GuestError,
    /// The sandbox contained a connector fault (trap/fuel/deadline/memory) or
    /// the connector could not be invoked; the connection is being torn down.
    ConnectorFault,
    /// The guest succeeded but at least one returned event was dropped
    /// (invalid, oversize, platform-spoofed, over a cap, or the spine write
    /// failed).
    PublishIncomplete,
}

/// What the driver did with a frame the transport yielded, reported back via
/// [`Transport::on_frame_outcome`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FrameOutcome {
    /// The connector accepted the frame and every event it returned was
    /// published (an empty event list counts: nothing was lost).
    Published,
    /// The frame (or part of its events) was dropped; a stateful transport
    /// must NOT treat its data as delivered.
    Dropped(FrameDropReason),
}

/// A host-owned connection to one platform source.
///
/// `#[allow(async_fn_in_trait)]`: a generic bound (not `dyn`) at every call
/// site, matching `crate::publish::EventAppender`'s precedent.
#[allow(async_fn_in_trait)]
pub trait Transport: Send {
    /// The platform slug this transport serves; must have a pinned connector
    /// in the [`super::ConnectorRegistry`] or the driver refuses to start.
    fn platform(&self) -> &str;

    /// Opens a fresh connection and returns the host-built context for it:
    /// a new `connection_id` per logical session (stable across a transport
    /// RESUME), the opaque `source_id`, the optional RESUME handle. Called
    /// again by the driver after every teardown, under backoff.
    async fn connect(&mut self) -> Result<ConnectionCtx, TransportError>;

    /// Sends the connector-built handshake. A non-empty `secret_refs` is
    /// resolved **here**, in the outbound send path, as the literal last step
    /// before the bytes leave the process; the resolved value never goes back
    /// into a guest call.
    async fn send_handshake(&mut self, handshake: Handshake) -> Result<(), TransportError>;

    /// The next raw frame: `Ok(Some(bytes))`, `Ok(None)` on a clean remote
    /// close. Must be cancel-safe (the driver races it against shutdown).
    async fn next_frame(&mut self) -> Result<Option<Vec<u8>>, TransportError>;

    /// Closes the connection; best effort, called on every teardown path.
    async fn close(&mut self, reason: &str);

    /// Told, after every frame this transport yielded from
    /// [`Self::next_frame`], whether it was processed AND published. A
    /// STATEFUL transport (a poll/snapshot cursor) uses it to avoid advancing
    /// past a frame a guest error, sandbox trip or publish failure dropped --
    /// it can hold its cursor and re-yield instead. Called exactly once per
    /// yielded frame, in order, before the next `next_frame`. The default is a
    /// no-op, correct for stateless transports (socket/IRC/webhook) where a
    /// dropped frame is simply lost.
    async fn on_frame_outcome(&mut self, _outcome: FrameOutcome) {}
}
