//! The invocation seam between svc_ingest (the host that owns the socket) and
//! whatever runs the connector WASM -- the bundle executor.
//!
//! Production reaches the executor over the host-API wire, which today has no
//! connector export kinds (`penguin_bundle_host::wire::ExportKind` is
//! `Transform`/`Dispatch` only, an external `penguin-libs` type). Until that
//! lands, [`UnwiredInvoker`] is the production default: it fails every call
//! with a typed, loud [`ConnectorInvokeError::Unavailable`], never a silent
//! empty result. The real-path harness (`core/connector_e2e`) implements this
//! trait over the executor's in-process `ConnectorHost`.

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
        /// Diagnostic detail; may be logged, never contains frame data.
        message: String,
    },
    /// The invocation could not be attempted or completed for a non-guest
    /// reason (executor unreachable, pool exhausted, capability unavailable).
    /// Treated like a transport failure: never silently skipped.
    #[error("connector invocation unavailable: {0}")]
    Unavailable(String),
}

impl ConnectorInvokeError {
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

/// The production default until the executor wire exposes connector exports:
/// every call fails [`ConnectorInvokeError::Unavailable`], loudly. A driver
/// built on this never publishes anything and never pretends to.
#[derive(Debug, Clone, Copy, Default)]
pub struct UnwiredInvoker;

const UNWIRED: &str = "connector execution is unavailable: the executor host-API wire has no \
connector export kinds yet (penguin-libs ExportKind::Transform/Dispatch only)";

impl ConnectorInvoker for UnwiredInvoker {
    async fn on_connect(
        &self,
        _platform: &str,
        _ctx: &ConnectionCtx,
    ) -> Result<Handshake, ConnectorInvokeError> {
        Err(ConnectorInvokeError::Unavailable(UNWIRED.to_string()))
    }

    async fn on_frame(
        &self,
        _platform: &str,
        _frame: RawFrame,
    ) -> Result<Vec<ConnectorEvent>, ConnectorInvokeError> {
        Err(ConnectorInvokeError::Unavailable(UNWIRED.to_string()))
    }

    async fn on_heartbeat_due(
        &self,
        _platform: &str,
        _ctx: &ConnectionCtx,
    ) -> Result<Option<Vec<u8>>, ConnectorInvokeError> {
        Err(ConnectorInvokeError::Unavailable(UNWIRED.to_string()))
    }

    async fn on_disconnect(
        &self,
        _platform: &str,
        _ctx: &ConnectionCtx,
        _reason: &str,
    ) -> Result<(), ConnectorInvokeError> {
        Err(ConnectorInvokeError::Unavailable(UNWIRED.to_string()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ctx() -> ConnectionCtx {
        ConnectionCtx {
            platform: "echo".to_string(),
            source_id: "s".to_string(),
            connection_id: "c".to_string(),
            session_token: None,
        }
    }

    #[tokio::test]
    async fn the_unwired_invoker_fails_every_export_with_a_typed_unavailable_error() {
        let inv = UnwiredInvoker;
        let frame = RawFrame {
            ctx: ctx(),
            bytes: vec![1],
            received_at: "2026-10-12T00:00:00.000Z".to_string(),
        };
        let results = [
            inv.on_connect("echo", &ctx()).await.err(),
            inv.on_frame("echo", frame).await.err(),
            inv.on_heartbeat_due("echo", &ctx()).await.err(),
            inv.on_disconnect("echo", &ctx(), "r").await.err(),
        ];
        assert_eq!(results.len(), 4);
        for err in results {
            let err = err.expect("every export must fail loudly");
            assert!(
                matches!(err, ConnectorInvokeError::Unavailable(ref m) if m.contains("unavailable"))
            );
            assert!(err.tears_down_connection());
            assert_eq!(err.kind(), "unavailable");
        }
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
