//! Typed errors for the Helix client -- one variant per caller-actionable failure class.

use std::time::Duration;

/// Maximum characters of a Twitch error body kept in an error message.
const MAX_BODY_CHARS: usize = 200;

/// Every way a Helix call can fail; callers match on the variant, never on message text.
#[derive(Debug, thiserror::Error)]
pub enum HelixError {
    /// A request argument failed local validation; nothing was sent.
    #[error("invalid argument: {0}")]
    InvalidArgument(String),
    /// HTTP 401 -- token invalid/expired/revoked. Refresh is the CALLER's concern.
    #[error("twitch oauth token didn't work (401); caller must refresh the token")]
    Unauthorized,
    /// HTTP 403 -- token valid but lacks a required scope/role (e.g. not a moderator).
    #[error("twitch token lacks required scope or permission (403): {message}")]
    Forbidden {
        /// Twitch's own explanation (truncated).
        message: String,
    },
    /// HTTP 429 -- rate limited; wait `retry_after` before retrying.
    #[error("twitch api rate limited (429); retry after {retry_after:?}")]
    RateLimited {
        /// Time until the bucket resets (from `Ratelimit-Reset`), floor 1s when absent.
        retry_after: Duration,
    },
    /// Any other 4xx -- the request itself is wrong; retrying will not help.
    #[error("twitch api client error: HTTP {status}: {message}")]
    Client {
        /// HTTP status code.
        status: u16,
        /// Twitch's own explanation (truncated).
        message: String,
    },
    /// 5xx -- Twitch-side failure; safe for the caller to retry with backoff.
    #[error("twitch api server error: HTTP {status}")]
    Server {
        /// HTTP status code.
        status: u16,
    },
    /// Connection/timeout/TLS failure before a response arrived.
    #[error("twitch api request failed: {0}")]
    Transport(#[source] reqwest::Error),
    /// A 2xx response whose body didn't match the documented shape.
    #[error("twitch api response malformed: {0}")]
    Malformed(String),
    /// Twitch accepted the chat request but dropped the message (`is_sent: false`).
    #[error("twitch dropped the chat message: {code}: {message}")]
    MessageDropped {
        /// Twitch drop-reason code (e.g. `msg_duplicate`).
        code: String,
        /// Twitch drop-reason text (truncated).
        message: String,
    },
}

impl HelixError {
    /// True when retrying the identical request later may succeed (429, 5xx, transport).
    pub fn is_retryable(&self) -> bool {
        matches!(
            self,
            Self::RateLimited { .. } | Self::Server { .. } | Self::Transport(_)
        )
    }
}

/// Truncate a remote-supplied string to a bounded length on a char boundary.
pub(crate) fn truncate(s: &str) -> String {
    s.chars().take(MAX_BODY_CHARS).collect()
}
