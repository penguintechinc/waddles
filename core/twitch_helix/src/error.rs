//! Typed errors for the Helix client -- one variant per caller-actionable failure class.

use std::time::Duration;

/// Maximum characters of a Twitch error body kept in an error message.
const MAX_BODY_CHARS: usize = 200;

/// Every way a Helix call can fail; callers match on the variant, never on message text.
#[derive(Debug, thiserror::Error)]
pub enum HelixError {
    /// A request argument or the client/credential configuration failed local
    /// validation (including reqwest *builder* errors such as an unusable header
    /// value); nothing was sent. Permanent misconfiguration -- never retryable.
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
    /// Connection/timeout/TLS failure before a response arrived. The wrapped
    /// error has had its request URL stripped (`reqwest::Error::without_url`),
    /// so neither `Display` nor `Debug` carries query-string user/channel ids.
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

    /// Classify a `reqwest::Error` raised while building or sending a request.
    ///
    /// The request URL is always stripped first: reqwest embeds it (query string
    /// included, e.g. `from_user_id=...&to_user_id=...`) in `Display`/`Debug`,
    /// which would leak user/channel ids into any caller's logs. A *builder*
    /// error (unusable header value, bad URL, unserialisable body) is permanent
    /// misconfiguration, so it becomes the non-retryable `InvalidArgument`
    /// instead of a `Transport` error a retry loop would spin on forever.
    pub(crate) fn from_reqwest(e: reqwest::Error) -> Self {
        let e = e.without_url();
        if e.is_builder() {
            let detail = std::error::Error::source(&e).map_or_else(
                || e.to_string(),
                |source| format!("{e}: {}", truncate(&source.to_string())),
            );
            return Self::InvalidArgument(format!(
                "request or client could not be built (check token, client id, base url): {detail}"
            ));
        }
        Self::Transport(e)
    }
}

/// Truncate a remote-supplied string to a bounded length on a char boundary.
pub(crate) fn truncate(s: &str) -> String {
    s.chars().take(MAX_BODY_CHARS).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A `reqwest` send error (connection refused) for a URL carrying query ids.
    async fn refused_send_error() -> reqwest::Error {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        drop(listener);
        reqwest::Client::new()
            .post(format!(
                "http://{addr}/whispers?from_user_id=uid-from-7771&to_user_id=uid-to-8882"
            ))
            .send()
            .await
            .unwrap_err()
    }

    #[tokio::test]
    async fn from_reqwest_strips_url_that_raw_reqwest_leaks() {
        let raw = refused_send_error().await;
        // Premise: a bare reqwest error DOES carry the full URL + ids.
        assert!(raw.to_string().contains("from_user_id=uid-from-7771"));
        assert!(format!("{raw:?}").contains("to_user_id=uid-to-8882"));

        let err = HelixError::from_reqwest(raw);
        assert!(matches!(err, HelixError::Transport(_)));
        assert!(err.is_retryable());
        for text in [err.to_string(), format!("{err:?}")] {
            for needle in [
                "from_user_id",
                "to_user_id",
                "uid-from-7771",
                "uid-to-8882",
                "/whispers",
            ] {
                assert!(!text.contains(needle), "{needle:?} leaked in {text:?}");
            }
        }
    }

    #[test]
    fn from_reqwest_maps_builder_error_to_non_retryable_invalid_argument() {
        // `build()` on an unparsable URL yields a reqwest *builder* error.
        let raw = reqwest::Client::new().get("not a url").build().unwrap_err();
        assert!(raw.is_builder());
        let err = HelixError::from_reqwest(raw);
        assert!(matches!(err, HelixError::InvalidArgument(_)), "{err:?}");
        assert!(!err.is_retryable());
    }
}
