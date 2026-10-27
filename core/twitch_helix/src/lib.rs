//! Typed async Twitch Helix client for moderation + messaging.
//!
//! Covers the three write endpoints the provider framework needs:
//!
//! | Method | Endpoint | Token / scope |
//! |---|---|---|
//! | [`HelixClient::delete_chat_message`] | `DELETE /moderation/chat` | moderator USER token, `moderator:manage:chat_messages` |
//! | [`HelixClient::send_chat_message`] | `POST /chat/messages` | USER token `user:write:chat` (or app token + `user:bot`/`channel:bot`) |
//! | [`HelixClient::send_whisper`] | `POST /whispers` | USER token, `user:manage:whispers` |
//!
//! # Auth
//! The caller supplies the token ([`AccessToken`], redacted in `Debug`) and the
//! app `Client-Id`. A 401 surfaces as [`HelixError::Unauthorized`]; refreshing
//! the token is the caller's concern (same boundary as the Python
//! `platform_moderation.py` classifier). No retry loop is hidden in here.
//!
//! # Rate limits
//! Every response's `Ratelimit-Limit/Remaining/Reset` headers are recorded and
//! readable through [`HelixClient::last_rate_limit`]. A 429 becomes
//! [`HelixError::RateLimited`] carrying the wait derived from `Ratelimit-Reset`;
//! [`HelixClient::with_wait_on_rate_limit`] opts in to ONE bounded sleep + retry.
//!
//! # Whisper restrictions (Twitch-imposed, verify against current docs)
//! - The SENDER account must have a verified phone number, or Twitch returns 400/403.
//! - The recipient must allow whispers from strangers, or have been whispered by/follow the sender.
//! - Limits: ~3 whispers/second and ~100/minute; a sender may whisper at most
//!   ~40 UNIQUE recipients per day, and new-recipient whispers are throttled further per minute.
//! - Text limit: 500 chars to a recipient that has not whispered you before, 10,000 otherwise.
//!   This client enforces the 10,000 ceiling locally; Twitch enforces the 500 case.
//! - Whispers cannot be sent to or from an app token; a user token is mandatory.
//!
//! # Logging
//! Only endpoint names and status codes are logged -- never tokens, message
//! text, or user/channel identifiers. Returned errors are URL-free too:
//! [`HelixError::Transport`] has its request URL (and query-string ids)
//! stripped, so a caller may log any [`HelixError`] safely.
//!
//! # Misconfiguration
//! Tokens and client ids are trimmed and validated on load. Anything that still
//! cannot be sent (builder errors) is [`HelixError::InvalidArgument`], which is
//! never [`HelixError::is_retryable`] -- a permanent config fault fails loud
//! instead of being retried forever.

mod error;

use std::fmt;
use std::sync::Mutex;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use reqwest::{Method, RequestBuilder, Response, StatusCode};
use serde::{Deserialize, Serialize};

pub use error::HelixError;

/// Production Helix base URL.
pub const DEFAULT_API_BASE: &str = "https://api.twitch.tv/helix";
/// Per-request timeout, matching the Python client's 10s.
const REQUEST_TIMEOUT: Duration = Duration::from_secs(10);
/// Upper bound for the opt-in rate-limit sleep.
const MAX_RATE_LIMIT_WAIT: Duration = Duration::from_secs(30);
/// Placeholder error message when a 4xx response body could not be read.
const UNREADABLE_BODY: &str = "<response body unreadable>";
/// Helix chat message length ceiling (characters).
pub const MAX_CHAT_MESSAGE_CHARS: usize = 500;
/// Helix whisper length ceiling (characters, for prior-correspondent recipients).
pub const MAX_WHISPER_CHARS: usize = 10_000;

/// A bearer token that never prints its value.
#[derive(Clone)]
pub struct AccessToken(String);

impl AccessToken {
    /// Wrap a raw token, trimming surrounding whitespace (a trailing `\n` from a
    /// secret file/env is the usual culprit). Fails loud on an empty value or one
    /// that still has whitespace/control/non-ASCII characters inside, since such
    /// a token can never form a valid `Authorization` header.
    pub fn new(raw: impl Into<String>) -> Result<Self, HelixError> {
        let raw = raw.into();
        Ok(Self(clean_header_value("access token", &raw)?))
    }

    /// Read a token from the named environment variable (trimmed, see [`AccessToken::new`]).
    pub fn from_env(var: &str) -> Result<Self, HelixError> {
        let raw = std::env::var(var)
            .map_err(|_| HelixError::InvalidArgument(format!("env var {var} not set")))?;
        Self::new(raw)
    }
}

impl fmt::Debug for AccessToken {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("AccessToken(****)")
    }
}

/// Snapshot of the `Ratelimit-*` response headers.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RateLimit {
    /// Bucket size (`Ratelimit-Limit`).
    pub limit: Option<u32>,
    /// Points left (`Ratelimit-Remaining`).
    pub remaining: Option<u32>,
    /// Unix-epoch seconds the bucket refills (`Ratelimit-Reset`).
    pub reset_epoch_secs: Option<u64>,
}

/// Request body for `POST /chat/messages`.
#[derive(Debug, Serialize)]
struct SendChatRequest<'a> {
    broadcaster_id: &'a str,
    sender_id: &'a str,
    message: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    reply_parent_message_id: Option<&'a str>,
}

/// Reason Twitch dropped a chat message.
#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
pub struct DropReason {
    /// Machine code (e.g. `msg_duplicate`).
    pub code: String,
    /// Human text.
    pub message: String,
}

/// One entry of the `POST /chat/messages` response.
#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
pub struct SentChatMessage {
    /// Twitch message id.
    pub message_id: String,
    /// Whether the message was actually posted.
    pub is_sent: bool,
    /// Present when `is_sent` is false.
    #[serde(default)]
    pub drop_reason: Option<DropReason>,
}

/// Envelope of the `POST /chat/messages` response.
#[derive(Debug, Deserialize)]
struct SendChatResponse {
    data: Vec<SentChatMessage>,
}

/// Request body for `POST /whispers`.
#[derive(Debug, Serialize)]
struct WhisperRequest<'a> {
    message: &'a str,
}

/// Twitch's JSON error envelope (`{"error","status","message"}`).
#[derive(Debug, Deserialize)]
struct ErrorBody {
    #[serde(default)]
    message: String,
}

/// Helix client bound to one token + client id.
#[derive(Debug)]
pub struct HelixClient {
    http: reqwest::Client,
    api_base: String,
    client_id: String,
    token: AccessToken,
    wait_on_rate_limit: bool,
    last_rate_limit: Mutex<Option<RateLimit>>,
}

impl HelixClient {
    /// Build a client against production Helix (rustls TLS, 10s timeout).
    pub fn new(client_id: impl Into<String>, token: AccessToken) -> Result<Self, HelixError> {
        Self::with_base_url(client_id, token, DEFAULT_API_BASE)
    }

    /// Build a client against a custom base URL (tests / Twitch CLI mock server).
    pub fn with_base_url(
        client_id: impl Into<String>,
        token: AccessToken,
        api_base: impl Into<String>,
    ) -> Result<Self, HelixError> {
        let client_id = clean_header_value("client id", &client_id.into())?;
        let http = reqwest::Client::builder()
            .timeout(REQUEST_TIMEOUT)
            .use_rustls_tls()
            .build()
            .map_err(HelixError::from_reqwest)?;
        Ok(Self {
            http,
            api_base: api_base.into().trim_end_matches('/').to_owned(),
            client_id,
            token,
            wait_on_rate_limit: false,
            last_rate_limit: Mutex::new(None),
        })
    }

    /// Opt in to sleeping (bounded to 30s) and retrying ONCE after a 429.
    #[must_use]
    pub fn with_wait_on_rate_limit(mut self, enabled: bool) -> Self {
        self.wait_on_rate_limit = enabled;
        self
    }

    /// Rate-limit headers from the most recent response, if any carried them.
    pub fn last_rate_limit(&self) -> Option<RateLimit> {
        self.last_rate_limit.lock().ok().and_then(|g| *g)
    }

    /// `DELETE /moderation/chat` -- remove one chat message (204 on success).
    ///
    /// Needs a token of `moderator_id` with `moderator:manage:chat_messages`;
    /// the moderator must moderate `broadcaster_id`'s channel. Twitch refuses
    /// to delete messages from the broadcaster or other moderators (400).
    pub async fn delete_chat_message(
        &self,
        broadcaster_id: &str,
        moderator_id: &str,
        message_id: &str,
    ) -> Result<(), HelixError> {
        require("broadcaster_id", broadcaster_id)?;
        require("moderator_id", moderator_id)?;
        require("message_id", message_id)?;
        let query = [
            ("broadcaster_id", broadcaster_id),
            ("moderator_id", moderator_id),
            ("message_id", message_id),
        ];
        self.send("moderation/chat", || {
            self.request(Method::DELETE, "/moderation/chat")
                .query(&query)
        })
        .await?;
        Ok(())
    }

    /// `POST /chat/messages` -- send a chat message as `sender_id`.
    ///
    /// Returns [`HelixError::MessageDropped`] when Twitch accepts the call but
    /// reports `is_sent: false` (never silently succeeds).
    pub async fn send_chat_message(
        &self,
        broadcaster_id: &str,
        sender_id: &str,
        message: &str,
        reply_parent_message_id: Option<&str>,
    ) -> Result<SentChatMessage, HelixError> {
        require("broadcaster_id", broadcaster_id)?;
        require("sender_id", sender_id)?;
        require("message", message)?;
        if message.chars().count() > MAX_CHAT_MESSAGE_CHARS {
            return Err(HelixError::InvalidArgument(format!(
                "message exceeds {MAX_CHAT_MESSAGE_CHARS} characters"
            )));
        }
        let body = SendChatRequest {
            broadcaster_id,
            sender_id,
            message,
            reply_parent_message_id,
        };
        let response = self
            .send("chat/messages", || {
                self.request(Method::POST, "/chat/messages").json(&body)
            })
            .await?;
        let parsed: SendChatResponse = response
            .json()
            .await
            .map_err(|e| HelixError::Malformed(e.without_url().to_string()))?;
        let sent = parsed
            .data
            .into_iter()
            .next()
            .ok_or_else(|| HelixError::Malformed("empty data array".into()))?;
        if !sent.is_sent {
            let reason = sent.drop_reason.unwrap_or(DropReason {
                code: "unknown".into(),
                message: String::new(),
            });
            return Err(HelixError::MessageDropped {
                code: error::truncate(&reason.code),
                message: error::truncate(&reason.message),
            });
        }
        Ok(sent)
    }

    /// `POST /whispers` -- whisper `to_user_id` from `from_user_id` (204 on success).
    ///
    /// Needs `user:manage:whispers` on `from_user_id`'s USER token. See the
    /// crate docs for Twitch's heavy sender/recipient/rate restrictions.
    pub async fn send_whisper(
        &self,
        from_user_id: &str,
        to_user_id: &str,
        message: &str,
    ) -> Result<(), HelixError> {
        require("from_user_id", from_user_id)?;
        require("to_user_id", to_user_id)?;
        require("message", message)?;
        if message.chars().count() > MAX_WHISPER_CHARS {
            return Err(HelixError::InvalidArgument(format!(
                "whisper exceeds {MAX_WHISPER_CHARS} characters"
            )));
        }
        let query = [("from_user_id", from_user_id), ("to_user_id", to_user_id)];
        let body = WhisperRequest { message };
        self.send("whispers", || {
            self.request(Method::POST, "/whispers")
                .query(&query)
                .json(&body)
        })
        .await?;
        Ok(())
    }

    /// Start a request with the auth + Client-Id headers attached.
    fn request(&self, method: Method, path: &str) -> RequestBuilder {
        self.http
            .request(method, format!("{}{path}", self.api_base))
            .header("Client-Id", &self.client_id)
            .bearer_auth(&self.token.0)
    }

    /// Execute a request, record rate-limit headers, classify errors, and
    /// optionally wait + retry once on 429.
    async fn send<F>(&self, endpoint: &'static str, build: F) -> Result<Response, HelixError>
    where
        F: Fn() -> RequestBuilder,
    {
        let mut waited = false;
        loop {
            let response = build().send().await.map_err(HelixError::from_reqwest)?;
            self.record_rate_limit(&response);
            let status = response.status();
            tracing::debug!(endpoint, status = status.as_u16(), "twitch helix response");
            match classify(response).await {
                Ok(ok) => return Ok(ok),
                Err(HelixError::RateLimited { retry_after })
                    if self.wait_on_rate_limit && !waited && retry_after <= MAX_RATE_LIMIT_WAIT =>
                {
                    waited = true;
                    tracing::debug!(endpoint, "waiting out twitch rate limit once");
                    tokio::time::sleep(retry_after).await;
                }
                Err(e) => return Err(e),
            }
        }
    }

    /// Store the latest `Ratelimit-*` header snapshot.
    fn record_rate_limit(&self, response: &Response) {
        let snapshot = parse_rate_limit(response);
        if snapshot.limit.is_none()
            && snapshot.remaining.is_none()
            && snapshot.reset_epoch_secs.is_none()
        {
            return;
        }
        if let Ok(mut guard) = self.last_rate_limit.lock() {
            *guard = Some(snapshot);
        }
    }
}

/// Trim a credential-like value (token, client id) and reject anything that
/// cannot be a valid HTTP header value, so a bad secret fails loud at load time
/// instead of surfacing later as a request-builder error. The value is never
/// echoed into the error.
fn clean_header_value(name: &str, raw: &str) -> Result<String, HelixError> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Err(HelixError::InvalidArgument(format!("{name} is empty")));
    }
    if !trimmed.chars().all(|c| c.is_ascii_graphic()) {
        return Err(HelixError::InvalidArgument(format!(
            "{name} contains whitespace, control, or non-ASCII characters"
        )));
    }
    Ok(trimmed.to_owned())
}

/// Reject empty required string arguments before any network I/O.
fn require(name: &str, value: &str) -> Result<(), HelixError> {
    if value.trim().is_empty() {
        return Err(HelixError::InvalidArgument(format!("{name} is empty")));
    }
    Ok(())
}

/// Parse one numeric response header.
fn header_num<T: std::str::FromStr>(response: &Response, name: &str) -> Option<T> {
    response
        .headers()
        .get(name)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.trim().parse().ok())
}

/// Extract the `Ratelimit-*` headers.
fn parse_rate_limit(response: &Response) -> RateLimit {
    RateLimit {
        limit: header_num(response, "Ratelimit-Limit"),
        remaining: header_num(response, "Ratelimit-Remaining"),
        reset_epoch_secs: header_num(response, "Ratelimit-Reset"),
    }
}

/// Seconds to wait given a reset epoch; floors at 1s (also when the header is absent).
fn retry_after_from(reset_epoch_secs: Option<u64>, now: SystemTime) -> Duration {
    let now_secs = now.duration_since(UNIX_EPOCH).map_or(0, |d| d.as_secs());
    match reset_epoch_secs {
        Some(reset) if reset > now_secs => Duration::from_secs(reset - now_secs),
        _ => Duration::from_secs(1),
    }
}

/// Map a response to `Ok` (2xx) or the matching [`HelixError`] (mirrors
/// `platform_moderation._classify_twitch_response`).
async fn classify(response: Response) -> Result<Response, HelixError> {
    let status = response.status();
    if status.is_success() {
        return Ok(response);
    }
    if status == StatusCode::TOO_MANY_REQUESTS {
        let reset = parse_rate_limit(&response).reset_epoch_secs;
        return Err(HelixError::RateLimited {
            retry_after: retry_after_from(reset, SystemTime::now()),
        });
    }
    if status == StatusCode::UNAUTHORIZED {
        return Err(HelixError::Unauthorized);
    }
    if status.is_server_error() {
        return Err(HelixError::Server {
            status: status.as_u16(),
        });
    }
    let text = match response.text().await {
        Ok(text) => text,
        Err(e) => {
            // The status code still classifies the failure; surface (not hide)
            // that Twitch's explanation could not be read.
            tracing::warn!(
                status = status.as_u16(),
                error = %e.without_url(),
                "twitch error response body unreadable; reporting status only"
            );
            UNREADABLE_BODY.to_owned()
        }
    };
    let message = serde_json::from_str::<ErrorBody>(&text)
        .map(|b| b.message)
        .unwrap_or(text);
    let message = error::truncate(&message);
    if status == StatusCode::FORBIDDEN {
        return Err(HelixError::Forbidden { message });
    }
    Err(HelixError::Client {
        status: status.as_u16(),
        message,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn token_debug_is_masked() {
        let t = AccessToken::new("supersecret").unwrap();
        assert_eq!(format!("{t:?}"), "AccessToken(****)");
    }

    #[test]
    fn token_rejects_empty() {
        assert!(matches!(
            AccessToken::new("  "),
            Err(HelixError::InvalidArgument(_))
        ));
    }

    #[test]
    fn token_is_trimmed_on_load() {
        assert_eq!(AccessToken::new("tok\n").unwrap().0, "tok");
        assert_eq!(AccessToken::new("  tok \r\n").unwrap().0, "tok");
    }

    #[test]
    fn token_with_unusable_characters_fails_loud_non_retryable() {
        for bad in [
            "",
            "  ",
            "\n",
            "sekrit\ntok",
            "sekrit tok",
            "s\u{e9}krit",
            "sekrit\0",
        ] {
            let err = AccessToken::new(bad).unwrap_err();
            assert!(matches!(err, HelixError::InvalidArgument(_)), "{bad:?}");
            assert!(!err.is_retryable(), "{bad:?}");
            assert!(!err.to_string().contains("sekrit"), "token echoed: {err}");
        }
    }

    #[tokio::test]
    async fn unsendable_header_value_is_non_retryable_not_transport() {
        // Bypass `AccessToken::new` (which now rejects this) to prove the second
        // line of defence: a builder error must not become a retryable Transport.
        let client = HelixClient::with_base_url(
            "cid",
            AccessToken("sekrit-tok\n".into()),
            "http://127.0.0.1:1",
        )
        .unwrap();
        let err = client.delete_chat_message("b", "m", "x").await.unwrap_err();
        assert!(matches!(err, HelixError::InvalidArgument(_)), "{err:?}");
        assert!(!err.is_retryable());
        assert!(!err.to_string().contains("sekrit"), "token echoed: {err}");
    }

    #[test]
    fn token_from_env_missing_fails_loud() {
        assert!(matches!(
            AccessToken::from_env("TWITCH_HELIX_TEST_DEFINITELY_UNSET_VAR"),
            Err(HelixError::InvalidArgument(_))
        ));
    }

    #[test]
    fn retry_after_computation() {
        let now = UNIX_EPOCH + Duration::from_secs(1000);
        assert_eq!(retry_after_from(Some(1007), now), Duration::from_secs(7));
        assert_eq!(retry_after_from(Some(900), now), Duration::from_secs(1));
        assert_eq!(retry_after_from(None, now), Duration::from_secs(1));
    }

    #[test]
    fn retryable_classification() {
        assert!(HelixError::Server { status: 502 }.is_retryable());
        assert!(HelixError::RateLimited {
            retry_after: Duration::from_secs(1)
        }
        .is_retryable());
        assert!(!HelixError::Unauthorized.is_retryable());
        assert!(!HelixError::Client {
            status: 400,
            message: String::new()
        }
        .is_retryable());
    }
}
