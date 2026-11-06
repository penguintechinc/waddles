//! The pre-handshake admission hook the WS driver awaits before an on-connect
//! handshake that may burn a scarce upstream budget (Discord IDENTIFY).
//!
//! The hook is deliberately tiny and platform-agnostic: the driver builds a
//! [`GateCtx`] from the host-owned [`super::ConnectionCtx`] and awaits
//! [`HandshakeGate::acquire`]. The default [`NoopGate`] admits instantly, so
//! every platform without a gate (Twitch IRC, ...) behaves exactly as before.
//! The trait is dyn-compatible (boxed future) so the driver holds an
//! `Arc<dyn HandshakeGate>` without gaining a generic parameter.

use std::time::Duration;

use super::types::ConnectionCtx;
use super::webhook::BoxFut;

/// What the gate may look at. Built by the driver from host-owned state only
/// (never frame or guest content).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct GateCtx<'a> {
    /// Platform slug.
    pub platform: &'a str,
    /// Opaque source id.
    pub source_id: &'a str,
    /// Host-assigned connection id.
    pub connection_id: &'a str,
    /// The host-maintained session token (opaque JSON for gateway platforms).
    /// `None` = no resumable session exists.
    pub session_token: Option<&'a str>,
    /// The handshake bytes the guest rendered (handles still in place) -- the
    /// exact frame that leaves the process once the gate admits it. A gate
    /// must decide from THESE bytes, never from the session token alone.
    pub handshake: &'a [u8],
}

impl<'a> GateCtx<'a> {
    /// Builds the gate context from the connection context.
    #[must_use]
    pub fn from_connection(ctx: &'a ConnectionCtx, handshake: &'a [u8]) -> Self {
        Self {
            platform: &ctx.platform,
            source_id: &ctx.source_id,
            connection_id: &ctx.connection_id,
            session_token: ctx.session_token.as_deref(),
            handshake,
        }
    }
}

/// Proof the gate admitted this handshake.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct GatePermit {
    /// `true` when a budget slot was actually consumed (an IDENTIFY);
    /// `false` when none was needed (a RESUME, or no gate).
    pub consumed: bool,
    /// How long the gate made the connection wait.
    pub waited: Duration,
}

impl GatePermit {
    /// A permit for a handshake that needs no budget.
    #[must_use]
    pub fn not_required() -> Self {
        Self {
            consumed: false,
            waited: Duration::ZERO,
        }
    }
}

/// Why the gate refused. The driver never sends the handshake on any of
/// these; it tears the connection down (never hammering the upstream).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum GateError {
    /// The budget is spent until `retry_after`; the connection backs off at
    /// least that long before the next attempt.
    #[error("handshake budget exhausted; retry after {retry_after:?}")]
    Exhausted {
        /// Earliest sensible retry.
        retry_after: Duration,
    },
    /// A transient failure (budget store outage, limit fetch failed). The
    /// handshake is NOT sent unchecked: fail closed, retry under backoff.
    #[error("handshake gate unavailable: {reason}")]
    Retryable {
        /// Stable class label (never a value or credential).
        reason: &'static str,
    },
    /// A terminal failure reconnecting cannot fix (dead credential): the
    /// source is parked.
    #[error("handshake gate fatal: {reason}")]
    Fatal {
        /// Stable class label.
        reason: &'static str,
    },
}

impl GateError {
    /// Stable low-cardinality label for metrics/logs.
    #[must_use]
    pub fn kind(&self) -> &'static str {
        match self {
            Self::Exhausted { .. } => "exhausted",
            Self::Retryable { .. } => "retryable",
            Self::Fatal { .. } => "fatal",
        }
    }
}

/// Admission control awaited by the driver before the handshake is sent.
pub trait HandshakeGate: Send + Sync {
    /// Admits (possibly after a bounded wait) or refuses one handshake.
    /// Must be cancel-safe: the driver races it against shutdown.
    fn acquire<'a>(&'a self, ctx: &'a GateCtx<'a>) -> BoxFut<'a, Result<GatePermit, GateError>>;
}

/// The default gate: admits immediately, consuming nothing.
#[derive(Debug, Default, Clone, Copy)]
pub struct NoopGate;

impl HandshakeGate for NoopGate {
    fn acquire<'a>(&'a self, _ctx: &'a GateCtx<'a>) -> BoxFut<'a, Result<GatePermit, GateError>> {
        Box::pin(async { Ok(GatePermit::not_required()) })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn conn(token: Option<&str>) -> ConnectionCtx {
        ConnectionCtx {
            platform: "p".into(),
            source_id: "s".into(),
            connection_id: "c".into(),
            session_token: token.map(str::to_string),
        }
    }

    #[tokio::test]
    async fn noop_gate_admits_without_consuming() {
        let c = conn(None);
        let g = GateCtx::from_connection(&c, b"");
        let permit = NoopGate.acquire(&g).await.unwrap();
        assert_eq!(permit, GatePermit::not_required());
        assert!(!permit.consumed);
    }

    #[test]
    fn gate_ctx_mirrors_the_connection() {
        let c = conn(Some("{}"));
        let g = GateCtx::from_connection(&c, b"");
        assert_eq!(
            (g.platform, g.source_id, g.connection_id, g.session_token),
            ("p", "s", "c", Some("{}"))
        );
    }

    #[test]
    fn error_kinds_are_stable() {
        assert_eq!(
            GateError::Exhausted {
                retry_after: Duration::ZERO
            }
            .kind(),
            "exhausted"
        );
        assert_eq!(GateError::Retryable { reason: "x" }.kind(), "retryable");
        assert_eq!(GateError::Fatal { reason: "x" }.kind(), "fatal");
    }
}
