//! Host-owned target rediscovery for a poll source whose upstream resource
//! ended (a YouTube live chat after the stream stops). The poll transport
//! classifies the ending response ([`super::lifecycle::HttpAction::Rediscover`]),
//! asks the upstream for the source's CURRENT target with the connection's own
//! credential through the same guarded, address-pinned client as every poll,
//! and hands the response body to a [`Rediscover`] hook. The hook (platform
//! code in `connector_runtime/profiles`) validates it and records the new
//! target host-side; the connection then ends so the next reconcile derives a
//! new plan whose fingerprint differs and restarts it cleanly.
//!
//! The hook never receives a credential, a URL or a client: it is a pure
//! parser plus a host-side record, so no new secret or egress path exists.

use std::fmt::Debug;
use std::sync::Arc;

/// What a rediscovery attempt concluded.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RediscoverOutcome {
    /// A new target was found and recorded; the connection restarts on it.
    Rediscovered,
    /// No target is live right now; the connection waits and re-checks.
    WaitingForLive,
}

impl RediscoverOutcome {
    /// Stable, low-cardinality metric label.
    #[must_use]
    pub fn label(self) -> &'static str {
        match self {
            Self::Rediscovered => "rediscovered",
            Self::WaitingForLive => "waiting_for_live",
        }
    }
}

/// Why a rediscovery response was not usable. Fixed text, never body content.
#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum RediscoverError {
    /// The response was not the expected shape (transient: retried under the
    /// driver's backoff).
    #[error("rediscovery response malformed")]
    Malformed,
    /// The host refused to record the target (terminal: parks the source).
    #[error("rediscovery target refused")]
    Refused,
}

/// Platform hook that turns a rediscovery response into a recorded target.
pub trait Rediscover: Send + Sync + Debug {
    /// Parses `body`, validates the candidate against this connection's own
    /// identity, records it host-side and remembers the outcome for
    /// [`Self::take_outcome`].
    ///
    /// # Errors
    /// [`RediscoverError`] when the body is unusable or the target refused.
    fn apply(&self, body: &[u8]) -> Result<RediscoverOutcome, RediscoverError>;

    /// The outcome of the last successful [`Self::apply`], cleared on read;
    /// the supervisor reads it when the connection ends.
    fn take_outcome(&self) -> Option<RediscoverOutcome>;
}

/// Rediscovery request target plus its hook, carried in the poll config.
#[derive(Clone)]
pub struct RediscoverConfig {
    /// `https://` request URL (a host constant; same host as the poll URL).
    pub url: String,
    /// The platform hook.
    pub hook: Arc<dyn Rediscover>,
}

impl Debug for RediscoverConfig {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("RediscoverConfig")
            .field("url", &"<host constant>")
            .field("hook", &self.hook)
            .finish()
    }
}
