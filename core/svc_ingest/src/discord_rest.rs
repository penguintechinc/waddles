//! Discord bot-token REST client: the real implementation behind the
//! Discord [`crate::outbound_ops::PlatformSender`] (provider-connection-
//! framework, issue #719).
//!
//! Endpoints (Discord REST API v10, `Authorization: Bot <token>`):
//!
//! | op            | calls                                                                 |
//! |---------------|-----------------------------------------------------------------------|
//! | `chat.send`   | `POST /channels/{channel_id}/messages`                                |
//! | `chat.delete` | `DELETE /channels/{channel_id}/messages/{message_id}`                 |
//! | `dm.send`     | `GET /channels/{origin}` -> `guild_id`, `GET /guilds/{guild}/members/{user}` (membership gate), then `POST /users/@me/channels {recipient_id}` and `POST` to that channel |
//!
//! **DM target binding.** A bundle names the DM recipient, so on its own it
//! could DM any user the shared bot account can reach, across tenants. The
//! community-bound send ([`DiscordRestClient::send_dm_in_community`]) only
//! delivers to a user who is a member of the guild the triggering event's
//! channel belongs to; a non-member, a channel with no guild, or a failed
//! membership lookup is refused (fail closed) before any DM channel is opened.
//!
//! **Token handling.** The token is a [`Secret`] (redacted `Debug`), is only
//! ever placed in the `Authorization` header, and never appears in a log
//! line or an error string.
//!
//! **PII-free logging.** Log fields are limited to platform/op/channel id/
//! HTTP status/attempt counts -- never message content, usernames, or user
//! ids (a DM recipient id is a user identifier and is deliberately omitted).
//!
//! **Rate limits.** A `429` is retried after the server-provided
//! `Retry-After` (header first, JSON body `retry_after` fallback), at most
//! [`MAX_RATE_LIMIT_RETRIES`] times and never sleeping longer than
//! [`MAX_RETRY_WAIT`] -- beyond either bound the call fails loudly with
//! [`DiscordRestError::RateLimited`]. Per-route buckets are honored
//! proactively: when a response reports `X-RateLimit-Remaining: 0`, the next
//! request on the same route waits out `X-RateLimit-Reset-After` (same
//! bound). Every other non-2xx status fails loudly, no retry.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use reqwest::{Method, StatusCode};

use crate::config::Secret;

/// Discord REST API base this client targets.
pub const DEFAULT_API_BASE: &str = "https://discord.com/api/v10";

/// Maximum `429` retries per call before failing loudly.
pub const MAX_RATE_LIMIT_RETRIES: u32 = 3;

/// Longest single rate-limit wait this client will sleep through. A longer
/// server-requested wait fails the call instead of stalling the drain loop.
pub const MAX_RETRY_WAIT: Duration = Duration::from_secs(10);

/// Per-request timeout.
const REQUEST_TIMEOUT: Duration = Duration::from_secs(10);

/// Discord error code: "Cannot send messages to this user" (DMs closed or
/// no shared guild / blocked).
const CODE_CANNOT_DM_USER: u64 = 50007;

/// Discord error codes for "Unknown Member" / "Unknown User" -- the
/// membership lookup's "this user is not in the guild" answer.
const CODE_UNKNOWN_MEMBER: u64 = 10007;
const CODE_UNKNOWN_USER: u64 = 10013;

/// How long a channel -> guild resolution is cached. A channel never moves
/// between guilds, so this only bounds staleness after a channel deletion.
const GUILD_CACHE_TTL: Duration = Duration::from_secs(600);

/// Upper bound on cached channel -> guild entries; the cache is cleared
/// wholesale when full (it is a latency optimisation, never a source of truth).
const GUILD_CACHE_MAX: usize = 1024;

/// A Discord REST call failure. `Display` never includes the token or any
/// message content.
#[derive(Debug, thiserror::Error)]
pub enum DiscordRestError {
    /// An id was not a valid Discord snowflake (rejected before any request
    /// -- prevents path injection into the REST URL).
    #[error("invalid discord {field} (not a snowflake)")]
    InvalidId { field: &'static str },
    /// Empty message content (Discord rejects it; fail before the request).
    #[error("discord message content must be non-empty")]
    EmptyContent,
    /// The recipient cannot be DM'd (closed DMs, blocked, no shared guild).
    #[error("discord user cannot be direct-messaged (status {status}, code {code:?})")]
    CannotDm { status: u16, code: Option<u64> },
    /// The DM target is not a member of the guild the triggering event's
    /// channel belongs to (or that channel has no guild at all). Policy
    /// refusal, raised before any DM channel is opened.
    #[error("discord dm target is not a member of the triggering community")]
    NotInCommunity,
    /// Rate limited and the bounded retry budget/wait was exhausted.
    #[error("discord rate limited (retry_after {retry_after_ms}ms, {attempts} attempts)")]
    RateLimited { retry_after_ms: u64, attempts: u32 },
    /// Any other non-2xx status.
    #[error("discord API returned status {status} (code {code:?})")]
    Status { status: u16, code: Option<u64> },
    /// Network/TLS/timeout failure (no response).
    #[error("discord request transport error: {0}")]
    Transport(String),
    /// A 2xx response whose body was not the expected shape.
    #[error("discord response malformed: {0}")]
    BadResponse(&'static str),
}

/// `true` for a Discord snowflake: 1..=20 ASCII digits (u64 decimal).
#[must_use]
pub fn is_snowflake(id: &str) -> bool {
    !id.is_empty() && id.len() <= 20 && id.bytes().all(|b| b.is_ascii_digit())
}

/// Discord bot-token REST client. Cheap to share by reference.
pub struct DiscordRestClient {
    client: reqwest::Client,
    token: Secret,
    api_base: String,
    max_retries: u32,
    max_wait: Duration,
    /// Route key -> instant before which the bucket is exhausted.
    buckets: Mutex<HashMap<String, Instant>>,
    /// Channel id -> (guild id, cached-at); see [`GUILD_CACHE_TTL`].
    guilds: Mutex<HashMap<String, (String, Instant)>>,
}

impl DiscordRestClient {
    /// Builds a client against `api_base` (normally [`DEFAULT_API_BASE`]).
    ///
    /// # Errors
    /// [`DiscordRestError::Transport`] if the HTTP client cannot be built.
    pub fn new(token: Secret, api_base: impl Into<String>) -> Result<Self, DiscordRestError> {
        crate::crypto::ensure_installed();
        let client = reqwest::Client::builder()
            .timeout(REQUEST_TIMEOUT)
            .user_agent("DiscordBot (https://penguintech.io, 1)")
            .build()
            .map_err(|e| DiscordRestError::Transport(e.to_string()))?;
        Ok(Self {
            client,
            token,
            api_base: api_base.into().trim_end_matches('/').to_string(),
            max_retries: MAX_RATE_LIMIT_RETRIES,
            max_wait: MAX_RETRY_WAIT,
            buckets: Mutex::new(HashMap::new()),
            guilds: Mutex::new(HashMap::new()),
        })
    }

    /// Overrides the retry bounds (tests; production uses the defaults).
    #[must_use]
    pub fn with_limits(mut self, max_retries: u32, max_wait: Duration) -> Self {
        self.max_retries = max_retries;
        self.max_wait = max_wait;
        self
    }

    /// `chat.send`: `POST /channels/{channel_id}/messages`.
    ///
    /// # Errors
    /// [`DiscordRestError`] -- invalid id, empty content, rate limit
    /// exhaustion, non-2xx status, or transport failure.
    pub async fn send_message(&self, channel_id: &str, text: &str) -> Result<(), DiscordRestError> {
        require_snowflake(channel_id, "channel_id")?;
        require_content(text)?;
        let resp = self
            .execute(
                "chat.send",
                Method::POST,
                format!("/channels/{channel_id}/messages"),
                format!("POST /channels/{channel_id}/messages"),
                Some(serde_json::json!({ "content": text })),
            )
            .await?;
        ensure_success(&resp, false)
    }

    /// `chat.delete`: `DELETE /channels/{channel_id}/messages/{message_id}`.
    ///
    /// # Errors
    /// [`DiscordRestError`] as for [`Self::send_message`]; a missing message
    /// is `Status { status: 404 }` (loud, not swallowed).
    pub async fn delete_message(
        &self,
        channel_id: &str,
        message_id: &str,
    ) -> Result<(), DiscordRestError> {
        require_snowflake(channel_id, "channel_id")?;
        require_snowflake(message_id, "message_id")?;
        let resp = self
            .execute(
                "chat.delete",
                Method::DELETE,
                format!("/channels/{channel_id}/messages/{message_id}"),
                format!("DELETE /channels/{channel_id}/messages"),
                None,
            )
            .await?;
        ensure_success(&resp, false)
    }

    /// `dm.send`: opens (or fetches) the DM channel via
    /// `POST /users/@me/channels {recipient_id}`, then posts `text` to it.
    ///
    /// # Errors
    /// [`DiscordRestError::CannotDm`] when Discord refuses the DM (403 on
    /// either call); otherwise as for [`Self::send_message`].
    pub async fn send_dm(&self, user_id: &str, text: &str) -> Result<(), DiscordRestError> {
        require_snowflake(user_id, "user_id")?;
        require_content(text)?;
        let open = self
            .execute(
                "dm.send",
                Method::POST,
                "/users/@me/channels".to_string(),
                "POST /users/@me/channels".to_string(),
                Some(serde_json::json!({ "recipient_id": user_id })),
            )
            .await?;
        ensure_success(&open, true)?;
        let dm_channel_id = serde_json::from_slice::<serde_json::Value>(&open.body)
            .ok()
            .and_then(|v| v.get("id").and_then(|i| i.as_str()).map(str::to_string))
            .filter(|id| is_snowflake(id))
            .ok_or(DiscordRestError::BadResponse(
                "open-DM response missing channel id",
            ))?;
        let sent = self
            .execute(
                "dm.send",
                Method::POST,
                format!("/channels/{dm_channel_id}/messages"),
                format!("POST /channels/{dm_channel_id}/messages"),
                Some(serde_json::json!({ "content": text })),
            )
            .await?;
        ensure_success(&sent, true)
    }

    /// `dm.send` bound to a community: delivers `text` to `user_id` only if
    /// `user_id` is a member of the guild that `origin_channel_id` (the
    /// channel of the event that triggered the bundle) belongs to.
    ///
    /// # Errors
    /// [`DiscordRestError::NotInCommunity`] when the channel has no guild or
    /// the user is not a member; any lookup failure (permissions, 5xx,
    /// transport) is returned as-is and nothing is sent -- fail closed.
    /// Otherwise as for [`Self::send_dm`].
    pub async fn send_dm_in_community(
        &self,
        origin_channel_id: &str,
        user_id: &str,
        text: &str,
    ) -> Result<(), DiscordRestError> {
        require_snowflake(origin_channel_id, "origin_channel_id")?;
        require_snowflake(user_id, "user_id")?;
        require_content(text)?;
        let Some(guild_id) = self.guild_of_channel(origin_channel_id).await? else {
            tracing::warn!(
                platform = "discord",
                op = "dm.send",
                channel_id = origin_channel_id,
                "dm target refused: origin channel has no guild"
            );
            return Err(DiscordRestError::NotInCommunity);
        };
        if !self.is_guild_member(&guild_id, user_id).await? {
            tracing::warn!(
                platform = "discord",
                op = "dm.send",
                guild_id = %guild_id,
                "dm target refused: not a member of the triggering guild"
            );
            return Err(DiscordRestError::NotInCommunity);
        }
        self.send_dm(user_id, text).await
    }

    /// Resolves the guild a channel belongs to (`GET /channels/{id}` ->
    /// `guild_id`), cached for [`GUILD_CACHE_TTL`]. `None` for a channel
    /// with no guild (a DM / group DM).
    async fn guild_of_channel(&self, channel_id: &str) -> Result<Option<String>, DiscordRestError> {
        if let Some((guild_id, cached_at)) = self
            .guilds
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .get(channel_id)
            .cloned()
        {
            if cached_at.elapsed() < GUILD_CACHE_TTL {
                return Ok(Some(guild_id));
            }
        }
        let resp = self
            .execute(
                "dm.send",
                Method::GET,
                format!("/channels/{channel_id}"),
                "GET /channels".to_string(),
                None,
            )
            .await?;
        ensure_success(&resp, false)?;
        let guild_id = serde_json::from_slice::<serde_json::Value>(&resp.body)
            .map_err(|_| DiscordRestError::BadResponse("channel response is not JSON"))?
            .get("guild_id")
            .and_then(|g| g.as_str())
            .map(str::to_string);
        match guild_id {
            Some(guild_id) if is_snowflake(&guild_id) => {
                let mut cache = self
                    .guilds
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner);
                if cache.len() >= GUILD_CACHE_MAX {
                    cache.clear();
                }
                cache.insert(channel_id.to_string(), (guild_id.clone(), Instant::now()));
                Ok(Some(guild_id))
            }
            Some(_) => Err(DiscordRestError::BadResponse("channel guild_id malformed")),
            None => Ok(None),
        }
    }

    /// Whether `user_id` is a member of `guild_id`
    /// (`GET /guilds/{guild}/members/{user}`): `200` yes; `404` with Discord's
    /// "Unknown Member"/"Unknown User" code no; anything else is an error
    /// (never read as "no" or "yes").
    async fn is_guild_member(
        &self,
        guild_id: &str,
        user_id: &str,
    ) -> Result<bool, DiscordRestError> {
        let resp = self
            .execute(
                "dm.send",
                Method::GET,
                format!("/guilds/{guild_id}/members/{user_id}"),
                format!("GET /guilds/{guild_id}/members"),
                None,
            )
            .await?;
        let code = serde_json::from_slice::<serde_json::Value>(&resp.body)
            .ok()
            .and_then(|v| v.get("code").and_then(serde_json::Value::as_u64));
        if resp.status == 404 && matches!(code, Some(CODE_UNKNOWN_MEMBER | CODE_UNKNOWN_USER)) {
            return Ok(false);
        }
        ensure_success(&resp, false)?;
        Ok(true)
    }

    /// Sends one request honoring the per-route bucket and bounded `429`
    /// retry; returns the first non-429 response.
    async fn execute(
        &self,
        op: &'static str,
        method: Method,
        path: String,
        route: String,
        body: Option<serde_json::Value>,
    ) -> Result<Resp, DiscordRestError> {
        let url = format!("{}{path}", self.api_base);
        let mut attempts = 0u32;
        loop {
            attempts += 1;
            self.wait_for_bucket(&route).await?;
            let mut req = self
                .client
                .request(method.clone(), &url)
                .header("Authorization", format!("Bot {}", self.token.expose()));
            if let Some(b) = &body {
                req = req.json(b);
            }
            let response = req.send().await.map_err(|e| {
                // reqwest errors can embed the URL (channel id only, no
                // secrets); strip it anyway to keep logs minimal.
                DiscordRestError::Transport(e.without_url().to_string())
            })?;
            let status = response.status();
            let headers = response.headers().clone();
            let bytes = response
                .bytes()
                .await
                .map_err(|e| DiscordRestError::Transport(e.without_url().to_string()))?
                .to_vec();
            self.record_bucket(&route, &headers);

            if status != StatusCode::TOO_MANY_REQUESTS {
                return Ok(Resp {
                    status: status.as_u16(),
                    body: bytes,
                });
            }
            let retry_after = parse_retry_after(&headers, &bytes);
            let retry_after_ms = u64::try_from(retry_after.as_millis()).unwrap_or(u64::MAX);
            if attempts > self.max_retries || retry_after > self.max_wait {
                tracing::warn!(
                    platform = "discord",
                    op,
                    attempts,
                    retry_after_ms,
                    "discord rate limit: giving up"
                );
                return Err(DiscordRestError::RateLimited {
                    retry_after_ms,
                    attempts,
                });
            }
            tracing::debug!(
                platform = "discord",
                op,
                attempts,
                retry_after_ms,
                "discord rate limited; waiting before retry"
            );
            tokio::time::sleep(retry_after).await;
        }
    }

    /// Waits out an exhausted bucket for `route`; fails if the wait would
    /// exceed the bound.
    async fn wait_for_bucket(&self, route: &str) -> Result<(), DiscordRestError> {
        let until = self
            .buckets
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .get(route)
            .copied();
        if let Some(until) = until {
            let now = Instant::now();
            if until > now {
                let wait = until - now;
                if wait > self.max_wait {
                    return Err(DiscordRestError::RateLimited {
                        retry_after_ms: u64::try_from(wait.as_millis()).unwrap_or(u64::MAX),
                        attempts: 0,
                    });
                }
                tokio::time::sleep(wait).await;
            }
        }
        Ok(())
    }

    /// Records/clears the bucket from `X-RateLimit-*` response headers.
    fn record_bucket(&self, route: &str, headers: &reqwest::header::HeaderMap) {
        let remaining = header_f64(headers, "x-ratelimit-remaining");
        let reset_after = header_f64(headers, "x-ratelimit-reset-after");
        let mut map = self
            .buckets
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        match (remaining, reset_after) {
            (Some(rem), Some(reset)) if rem < 1.0 && reset > 0.0 => {
                map.insert(route.to_string(), Instant::now() + secs(reset));
            }
            _ => {
                map.remove(route);
            }
        }
    }
}

/// A buffered response (status + body).
struct Resp {
    status: u16,
    body: Vec<u8>,
}

fn require_snowflake(id: &str, field: &'static str) -> Result<(), DiscordRestError> {
    if is_snowflake(id) {
        Ok(())
    } else {
        Err(DiscordRestError::InvalidId { field })
    }
}

fn require_content(text: &str) -> Result<(), DiscordRestError> {
    if text.is_empty() {
        Err(DiscordRestError::EmptyContent)
    } else {
        Ok(())
    }
}

/// Maps a non-2xx response to a typed error. `dm` makes a 403 a
/// [`DiscordRestError::CannotDm`] (the DM surface's "can't message this
/// user" case, loud rather than silent).
fn ensure_success(resp: &Resp, dm: bool) -> Result<(), DiscordRestError> {
    if (200..300).contains(&resp.status) {
        return Ok(());
    }
    let code = serde_json::from_slice::<serde_json::Value>(&resp.body)
        .ok()
        .and_then(|v| v.get("code").and_then(serde_json::Value::as_u64));
    if dm && (resp.status == 403 || code == Some(CODE_CANNOT_DM_USER)) {
        return Err(DiscordRestError::CannotDm {
            status: resp.status,
            code,
        });
    }
    Err(DiscordRestError::Status {
        status: resp.status,
        code,
    })
}

fn secs(v: f64) -> Duration {
    Duration::try_from_secs_f64(v.max(0.0)).unwrap_or(Duration::MAX)
}

fn header_f64(headers: &reqwest::header::HeaderMap, name: &str) -> Option<f64> {
    headers
        .get(name)?
        .to_str()
        .ok()?
        .trim()
        .parse::<f64>()
        .ok()
        .filter(|v| v.is_finite())
}

/// `Retry-After` header (seconds, may be fractional), falling back to the
/// JSON body's `retry_after`, then a conservative 1s.
fn parse_retry_after(headers: &reqwest::header::HeaderMap, body: &[u8]) -> Duration {
    if let Some(v) = header_f64(headers, "retry-after") {
        return secs(v);
    }
    serde_json::from_slice::<serde_json::Value>(body)
        .ok()
        .and_then(|v| v.get("retry_after").and_then(serde_json::Value::as_f64))
        .map_or(Duration::from_secs(1), secs)
}

#[cfg(test)]
mod tests {
    use super::*;
    use wiremock::matchers::{body_json, header, method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    const TOKEN: &str = "tok-secret-123";

    fn client(server: &MockServer) -> DiscordRestClient {
        DiscordRestClient::new(Secret::new(TOKEN), server.uri())
            .unwrap()
            .with_limits(2, Duration::from_secs(2))
    }

    #[test]
    fn snowflake_validation() {
        assert!(is_snowflake("123456789012345678"));
        assert!(!is_snowflake(""));
        assert!(!is_snowflake("12a"));
        assert!(!is_snowflake("../x"));
        assert!(!is_snowflake(&"1".repeat(21)));
    }

    #[tokio::test]
    async fn send_message_posts_with_bot_auth() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/channels/111/messages"))
            .and(header("Authorization", format!("Bot {TOKEN}").as_str()))
            .and(body_json(serde_json::json!({"content": "hi"})))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"id":"1"})))
            .expect(1)
            .mount(&server)
            .await;
        client(&server).send_message("111", "hi").await.unwrap();
    }

    #[tokio::test]
    async fn delete_message_issues_delete() {
        let server = MockServer::start().await;
        Mock::given(method("DELETE"))
            .and(path("/channels/111/messages/222"))
            .and(header("Authorization", format!("Bot {TOKEN}").as_str()))
            .respond_with(ResponseTemplate::new(204))
            .expect(1)
            .mount(&server)
            .await;
        client(&server).delete_message("111", "222").await.unwrap();
    }

    #[tokio::test]
    async fn delete_missing_message_is_loud_404() {
        let server = MockServer::start().await;
        Mock::given(method("DELETE"))
            .respond_with(
                ResponseTemplate::new(404).set_body_json(serde_json::json!({"code":10008})),
            )
            .mount(&server)
            .await;
        let err = client(&server)
            .delete_message("111", "222")
            .await
            .unwrap_err();
        assert!(matches!(
            err,
            DiscordRestError::Status {
                status: 404,
                code: Some(10008)
            }
        ));
    }

    #[tokio::test]
    async fn send_dm_opens_channel_then_posts() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/users/@me/channels"))
            .and(body_json(serde_json::json!({"recipient_id": "777"})))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"id":"999"})))
            .expect(1)
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .and(path("/channels/999/messages"))
            .and(body_json(serde_json::json!({"content": "psst"})))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"id":"5"})))
            .expect(1)
            .mount(&server)
            .await;
        client(&server).send_dm("777", "psst").await.unwrap();
    }

    #[tokio::test]
    async fn send_dm_403_on_post_is_cannot_dm() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/users/@me/channels"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"id":"999"})))
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .and(path("/channels/999/messages"))
            .respond_with(
                ResponseTemplate::new(403).set_body_json(serde_json::json!({"code":50007})),
            )
            .mount(&server)
            .await;
        let err = client(&server).send_dm("777", "x").await.unwrap_err();
        assert!(matches!(
            err,
            DiscordRestError::CannotDm {
                status: 403,
                code: Some(50007)
            }
        ));
    }

    #[tokio::test]
    async fn send_dm_403_on_open_is_cannot_dm() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/users/@me/channels"))
            .respond_with(ResponseTemplate::new(403))
            .mount(&server)
            .await;
        let err = client(&server).send_dm("777", "x").await.unwrap_err();
        assert!(matches!(
            err,
            DiscordRestError::CannotDm { status: 403, .. }
        ));
    }

    #[tokio::test]
    async fn send_dm_open_without_id_is_bad_response() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/users/@me/channels"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({})))
            .mount(&server)
            .await;
        let err = client(&server).send_dm("777", "x").await.unwrap_err();
        assert!(matches!(err, DiscordRestError::BadResponse(_)));
    }

    #[tokio::test]
    async fn rate_limit_429_retries_after_retry_after_then_succeeds() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/channels/111/messages"))
            .respond_with(ResponseTemplate::new(429).insert_header("Retry-After", "0.05"))
            .up_to_n_times(1)
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .and(path("/channels/111/messages"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"id":"1"})))
            .expect(1)
            .mount(&server)
            .await;
        let start = Instant::now();
        client(&server).send_message("111", "hi").await.unwrap();
        assert!(start.elapsed() >= Duration::from_millis(40));
    }

    #[tokio::test]
    async fn rate_limit_body_retry_after_fallback() {
        let server = MockServer::start().await;
        Mock::given(method("DELETE"))
            .respond_with(
                ResponseTemplate::new(429).set_body_json(serde_json::json!({"retry_after":0.02})),
            )
            .up_to_n_times(1)
            .mount(&server)
            .await;
        Mock::given(method("DELETE"))
            .respond_with(ResponseTemplate::new(204))
            .mount(&server)
            .await;
        client(&server).delete_message("1", "2").await.unwrap();
    }

    #[tokio::test]
    async fn rate_limit_exhausted_fails_loudly() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(429).insert_header("Retry-After", "0.01"))
            .mount(&server)
            .await;
        let err = client(&server).send_message("111", "hi").await.unwrap_err();
        assert!(matches!(
            err,
            DiscordRestError::RateLimited { attempts: 3, .. }
        ));
    }

    #[tokio::test]
    async fn rate_limit_wait_beyond_bound_fails_without_sleeping() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(429).insert_header("Retry-After", "3600"))
            .expect(1)
            .mount(&server)
            .await;
        let err = client(&server).send_message("111", "hi").await.unwrap_err();
        assert!(matches!(
            err,
            DiscordRestError::RateLimited {
                retry_after_ms: 3_600_000,
                attempts: 1
            }
        ));
    }

    #[tokio::test]
    async fn exhausted_bucket_delays_next_request_on_same_route() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(
                ResponseTemplate::new(200)
                    .insert_header("X-RateLimit-Remaining", "0")
                    .insert_header("X-RateLimit-Reset-After", "0.15"),
            )
            .mount(&server)
            .await;
        let c = client(&server);
        c.send_message("111", "a").await.unwrap();
        let start = Instant::now();
        c.send_message("111", "b").await.unwrap();
        assert!(start.elapsed() >= Duration::from_millis(100));
    }

    #[tokio::test]
    async fn bucket_wait_beyond_bound_errors() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(
                ResponseTemplate::new(200)
                    .insert_header("X-RateLimit-Remaining", "0")
                    .insert_header("X-RateLimit-Reset-After", "600"),
            )
            .expect(1)
            .mount(&server)
            .await;
        let c = client(&server);
        c.send_message("111", "a").await.unwrap();
        let err = c.send_message("111", "b").await.unwrap_err();
        assert!(matches!(
            err,
            DiscordRestError::RateLimited { attempts: 0, .. }
        ));
    }

    #[tokio::test]
    async fn other_statuses_fail_loudly_without_retry() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(500))
            .expect(1)
            .mount(&server)
            .await;
        let err = client(&server).send_message("111", "hi").await.unwrap_err();
        assert!(matches!(
            err,
            DiscordRestError::Status {
                status: 500,
                code: None
            }
        ));
    }

    #[tokio::test]
    async fn invalid_inputs_rejected_before_any_request() {
        let server = MockServer::start().await; // no mocks: any request would 404
        let c = client(&server);
        assert!(matches!(
            c.send_message("../x", "hi").await,
            Err(DiscordRestError::InvalidId {
                field: "channel_id"
            })
        ));
        assert!(matches!(
            c.send_message("111", "").await,
            Err(DiscordRestError::EmptyContent)
        ));
        assert!(matches!(
            c.delete_message("111", "x/y").await,
            Err(DiscordRestError::InvalidId {
                field: "message_id"
            })
        ));
        assert!(matches!(
            c.send_dm("u", "hi").await,
            Err(DiscordRestError::InvalidId { field: "user_id" })
        ));
        assert!(matches!(
            c.send_dm("1", "").await,
            Err(DiscordRestError::EmptyContent)
        ));
        assert!(server.received_requests().await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn transport_error_does_not_leak_token() {
        // Nothing listens on port 1.
        let c = DiscordRestClient::new(Secret::new(TOKEN), "http://127.0.0.1:1").unwrap();
        let err = c.send_message("111", "hi").await.unwrap_err();
        assert!(matches!(err, DiscordRestError::Transport(_)));
        assert!(!err.to_string().contains(TOKEN));
    }

    #[test]
    fn default_api_base_is_v10() {
        assert!(DEFAULT_API_BASE.ends_with("/api/v10"));
        let c = DiscordRestClient::new(Secret::new("t"), "http://x/").unwrap();
        assert_eq!(c.api_base, "http://x");
    }

    #[test]
    fn retry_after_defaults_to_one_second_when_absent() {
        let h = reqwest::header::HeaderMap::new();
        assert_eq!(parse_retry_after(&h, b"not json"), Duration::from_secs(1));
    }

    /// Mounts the origin-channel -> guild lookup (`channel` 100 -> guild 200).
    async fn mount_guild_lookup(server: &MockServer) {
        Mock::given(method("GET"))
            .and(path("/channels/100"))
            .respond_with(
                ResponseTemplate::new(200)
                    .set_body_json(serde_json::json!({"id":"100","guild_id":"200"})),
            )
            .mount(server)
            .await;
    }

    /// Mounts the open-DM + post-to-DM-channel pair, each expected `times`.
    async fn mount_dm_pair(server: &MockServer, times: u64) {
        Mock::given(method("POST"))
            .and(path("/users/@me/channels"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"id":"999"})))
            .expect(times)
            .mount(server)
            .await;
        Mock::given(method("POST"))
            .and(path("/channels/999/messages"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"id":"5"})))
            .expect(times)
            .mount(server)
            .await;
    }

    /// A member of the triggering guild is DM'd: channel -> guild ->
    /// membership -> open DM -> post, each exactly once.
    #[tokio::test]
    async fn send_dm_in_community_delivers_to_a_guild_member() {
        let server = MockServer::start().await;
        mount_guild_lookup(&server).await;
        Mock::given(method("GET"))
            .and(path("/guilds/200/members/777"))
            .and(header("Authorization", format!("Bot {TOKEN}").as_str()))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({})))
            .expect(1)
            .mount(&server)
            .await;
        mount_dm_pair(&server, 1).await;
        client(&server)
            .send_dm_in_community("100", "777", "psst")
            .await
            .unwrap();
    }

    /// Cross-tenant DM regression: a user who is NOT in the triggering
    /// guild is refused with `NotInCommunity` and no DM channel is opened.
    #[tokio::test]
    async fn send_dm_in_community_refuses_a_non_member_and_opens_no_dm() {
        let server = MockServer::start().await;
        mount_guild_lookup(&server).await;
        Mock::given(method("GET"))
            .and(path("/guilds/200/members/777"))
            .respond_with(
                ResponseTemplate::new(404).set_body_json(serde_json::json!({"code":10007})),
            )
            .mount(&server)
            .await;
        mount_dm_pair(&server, 0).await;
        let err = client(&server)
            .send_dm_in_community("100", "777", "psst")
            .await
            .unwrap_err();
        assert!(matches!(err, DiscordRestError::NotInCommunity));
    }

    /// An origin channel with no guild (a DM / group DM) can never be a
    /// community: refused, nothing sent.
    #[tokio::test]
    async fn send_dm_in_community_refuses_a_channel_without_a_guild() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/channels/100"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"id":"100"})))
            .mount(&server)
            .await;
        mount_dm_pair(&server, 0).await;
        let err = client(&server)
            .send_dm_in_community("100", "777", "psst")
            .await
            .unwrap_err();
        assert!(matches!(err, DiscordRestError::NotInCommunity));
    }

    /// Fail closed: a membership lookup that errors (here a 403 "Missing
    /// Access") is neither "member" nor "non-member" -- it is returned as an
    /// error and nothing is sent.
    #[tokio::test]
    async fn send_dm_in_community_fails_closed_when_membership_lookup_errors() {
        let server = MockServer::start().await;
        mount_guild_lookup(&server).await;
        Mock::given(method("GET"))
            .and(path("/guilds/200/members/777"))
            .respond_with(
                ResponseTemplate::new(403).set_body_json(serde_json::json!({"code":50001})),
            )
            .mount(&server)
            .await;
        mount_dm_pair(&server, 0).await;
        let err = client(&server)
            .send_dm_in_community("100", "777", "psst")
            .await
            .unwrap_err();
        assert!(matches!(
            err,
            DiscordRestError::Status {
                status: 403,
                code: Some(50001)
            }
        ));
    }

    /// The channel -> guild resolution is cached: two DMs from the same
    /// channel cost one `GET /channels/{id}`.
    #[tokio::test]
    async fn guild_lookup_is_cached_across_dms() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/channels/100"))
            .respond_with(
                ResponseTemplate::new(200)
                    .set_body_json(serde_json::json!({"id":"100","guild_id":"200"})),
            )
            .expect(1)
            .mount(&server)
            .await;
        Mock::given(method("GET"))
            .and(path("/guilds/200/members/777"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({})))
            .expect(2)
            .mount(&server)
            .await;
        mount_dm_pair(&server, 2).await;
        let c = client(&server);
        c.send_dm_in_community("100", "777", "a").await.unwrap();
        c.send_dm_in_community("100", "777", "b").await.unwrap();
    }

    #[tokio::test]
    async fn send_dm_in_community_validates_ids_before_any_request() {
        let server = MockServer::start().await; // no mocks: any request would 404
        let c = client(&server);
        assert!(matches!(
            c.send_dm_in_community("../x", "777", "hi").await,
            Err(DiscordRestError::InvalidId {
                field: "origin_channel_id"
            })
        ));
        assert!(matches!(
            c.send_dm_in_community("100", "u", "hi").await,
            Err(DiscordRestError::InvalidId { field: "user_id" })
        ));
        assert!(matches!(
            c.send_dm_in_community("100", "777", "").await,
            Err(DiscordRestError::EmptyContent)
        ));
        assert!(server.received_requests().await.unwrap().is_empty());
    }
}
