//! The invocation seam between svc_ingest (the host that owns the socket) and
//! whatever runs the connector WASM -- the bundle executor.
//!
//! Production reaches the executor over the host-API wire
//! (`penguin_bundle_host::wire::ExportKind::Connector*`, 0.3.0):
//! [`super::WireConnectorInvoker`] implements this trait over that wire. Every
//! way a call can fail -- including "no executor attached" -- is a typed,
//! loud [`ConnectorInvokeError`], never a silent empty result. The real-path
//! harness (`core/connector_e2e`) drives the real executor through it.

use super::types::{ConnectionCtx, ConnectorEvent, ConnectorFrameError, Handshake, RawFrame};

/// Every way a connector invocation can fail, in the three classes the host
/// reacts to differently (spec S2.6).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum ConnectorInvokeError {
    /// The connector returned a `frame-error`: this frame is bad, the
    /// connection stays up.
    #[error("connector reported a bad frame ({})", .0.kind())]
    Guest(ConnectorFrameError),
    /// The sandbox contained a connector fault (trap, fuel, deadline, memory
    /// cap). The connection is torn down and backed off; the connector keeps
    /// serving other connections.
    #[error("connector sandbox trip: {kind}")]
    SandboxTrip {
        /// Stable low-cardinality kind (`trap`/`fuel_exhausted`/`deadline`/
        /// `memory_limit`).
        kind: &'static str,
        /// Bounded diagnostic for a debugger/test to read off the error value.
        /// NEVER logged by the host: the wasmtime error chain it can derive
        /// from may echo guest-supplied text (a URL, a token). Build it with
        /// [`ConnectorInvokeError::sandbox_trip`] so it is truncated.
        message: String,
    },
    /// The invocation could not be attempted or completed for a non-guest
    /// reason (executor unreachable, pool exhausted, capability unavailable).
    /// Treated like a transport failure: never silently skipped.
    #[error("connector invocation unavailable: {0}")]
    Unavailable(String),
}

/// Hard bound on a [`ConnectorInvokeError::SandboxTrip`] diagnostic, bytes.
pub const MAX_TRIP_DIAGNOSTIC_BYTES: usize = 160;

impl ConnectorInvokeError {
    /// Builds a [`Self::SandboxTrip`], truncating `message` to
    /// [`MAX_TRIP_DIAGNOSTIC_BYTES`] on a char boundary so a hostile guest
    /// cannot park an unbounded string in the error.
    #[must_use]
    pub fn sandbox_trip(kind: &'static str, message: impl Into<String>) -> Self {
        let mut message = message.into();
        if message.len() > MAX_TRIP_DIAGNOSTIC_BYTES {
            let mut end = MAX_TRIP_DIAGNOSTIC_BYTES;
            while !message.is_char_boundary(end) {
                end -= 1;
            }
            message.truncate(end);
        }
        Self::SandboxTrip { kind, message }
    }

    /// Stable, low-cardinality label for metrics.
    #[must_use]
    pub fn kind(&self) -> &'static str {
        match self {
            Self::Guest(_) => "guest_error",
            Self::SandboxTrip { kind, .. } => kind,
            Self::Unavailable(_) => "unavailable",
        }
    }

    /// True when the error must tear the connection down (everything except
    /// a plain per-frame `frame-error`).
    #[must_use]
    pub fn tears_down_connection(&self) -> bool {
        !matches!(self, Self::Guest(_))
    }
}

/// Calls a connector's `receiver` exports by platform. A generic bound (not
/// `dyn`) at every call site, like `crate::publish::EventAppender`.
///
/// `#[allow(async_fn_in_trait)]`: `pub` because it appears in the driver's
/// public signature; `svc-ingest` is a deployed binary, not a library other
/// crates implement this against blindly.
#[allow(async_fn_in_trait)]
pub trait ConnectorInvoker: Send + Sync {
    /// `receiver.on-connect`.
    async fn on_connect(
        &self,
        platform: &str,
        ctx: &ConnectionCtx,
    ) -> Result<Handshake, ConnectorInvokeError>;

    /// `receiver.on-frame`.
    async fn on_frame(
        &self,
        platform: &str,
        frame: RawFrame,
    ) -> Result<Vec<ConnectorEvent>, ConnectorInvokeError>;

    /// `receiver.on-heartbeat-due`. Scheduling it (a dedicated per-connection
    /// task, never the `on-frame` dispatch path, spec S3.5) belongs to the
    /// first heartbeat-bearing transport (IRC/Discord); B0 only carries the
    /// seam so the executor's export is reachable.
    async fn on_heartbeat_due(
        &self,
        platform: &str,
        ctx: &ConnectionCtx,
    ) -> Result<Option<Vec<u8>>, ConnectorInvokeError>;

    /// `receiver.on-disconnect`. Best-effort notification; the driver logs a
    /// failure and moves on (the connection is already gone).
    async fn on_disconnect(
        &self,
        platform: &str,
        ctx: &ConnectionCtx,
        reason: &str,
    ) -> Result<(), ConnectorInvokeError>;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_sandbox_trip_diagnostic_is_truncated_on_a_char_boundary() {
        let long = "é".repeat(500);
        match ConnectorInvokeError::sandbox_trip("trap", long) {
            ConnectorInvokeError::SandboxTrip { message, .. } => {
                assert!(message.len() <= MAX_TRIP_DIAGNOSTIC_BYTES);
                assert!(!message.is_empty());
            }
            other => panic!("{other:?}"),
        }
        // Display never carries the diagnostic.
        let e = ConnectorInvokeError::sandbox_trip("trap", "SECRET");
        assert!(!e.to_string().contains("SECRET"));
    }

    #[test]
    fn only_a_guest_frame_error_keeps_the_connection_up() {
        let guest = ConnectorInvokeError::Guest(ConnectorFrameError::Malformed("x".into()));
        assert!(!guest.tears_down_connection());
        assert_eq!(guest.kind(), "guest_error");
        let trip = ConnectorInvokeError::SandboxTrip {
            kind: "trap",
            message: String::new(),
        };
        assert!(trip.tears_down_connection());
        assert_eq!(trip.kind(), "trap");
    }
}
