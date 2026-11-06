//! Readiness reporting for the connector runtime (spec section 3).

use std::sync::Arc;

/// A platform's readiness as the runtime sees it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Readiness {
    /// The platform's overlay is not enabled; nothing to wait for.
    Disabled,
    /// Enabled but not serving; the reason is a fixed, PII-free label.
    NotReady(&'static str),
    /// Serving: `live` of `desired` connections are up.
    Ready {
        /// Connections currently supervised and live.
        live: u32,
        /// Connections in the last successful desired set.
        desired: u32,
    },
}

/// Read side of the runtime's readiness, shared with `/readyz`.
pub trait RuntimeReady: Send + Sync {
    /// Readiness of one platform.
    fn ready(&self, platform: &str) -> Readiness;
}

/// Skeleton readiness: always not ready until the runtime lands.
pub(crate) struct PendingReady;

impl RuntimeReady for PendingReady {
    fn ready(&self, _platform: &str) -> Readiness {
        Readiness::NotReady("runtime_unimplemented")
    }
}

pub(crate) fn pending() -> Arc<dyn RuntimeReady> {
    Arc::new(PendingReady)
}
