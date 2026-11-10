//! The closed error taxonomy. Variants carry fixed reason codes only -- never a host,
//! address, port or credential -- so an error can be forwarded to the guest verbatim.

/// Why a `gameserver-net` call failed. Mirrors the WIT `error` variant one-to-one.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum NetError {
    /// Policy refusal; the payload is a fixed machine code (`not_registered`,
    /// `forbidden_address`, `rate_limited`, `too_many_connections`, `command_rejected`, ...).
    #[error("denied: {0}")]
    Denied(&'static str),
    /// The target could not be reached (resolve/connect/IO failure).
    #[error("unreachable")]
    Unreachable,
    /// A connect, login or read deadline elapsed.
    #[error("timeout")]
    Timeout,
    /// The server rejected the RCON login.
    #[error("auth_failed")]
    AuthFailed,
    /// A byte budget (in or out) would be exceeded.
    #[error("too_large")]
    TooLarge,
    /// Internal/directory failure; detail is logged host-side, never forwarded.
    #[error("backend")]
    Backend,
}

impl NetError {
    /// Stable lowercase label for metrics/audit (no cardinality blow-up).
    pub fn label(&self) -> &'static str {
        match self {
            Self::Denied(_) => "denied",
            Self::Unreachable => "unreachable",
            Self::Timeout => "timeout",
            Self::AuthFailed => "auth_failed",
            Self::TooLarge => "too_large",
            Self::Backend => "backend",
        }
    }
}
