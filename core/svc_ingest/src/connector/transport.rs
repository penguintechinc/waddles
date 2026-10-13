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
}
