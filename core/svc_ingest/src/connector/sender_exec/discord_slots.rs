//! The Discord [`SlotSource`] (P6a T6d-2): turns the per-community binding
//! seam ([`DiscordCredentialSource`]) into the `discord-bot` credential the
//! executor composes into `Authorization: Bot <token>`.
//!
//! On EVERY send it resolves the community's binding fresh (never cached),
//! and proves the stamp-pinned channel belongs to the binding's guild with
//! one guarded `GET /channels/{id}` -- the Discord equivalent of the Twitch
//! "community owns the channel" proof. A channel in another guild, an
//! unreadable channel, or a failed lookup refuses the send; there is no
//! fallback credential. The token appears only in the returned zeroizing
//! [`SlotValue`] and the request header; never in a log, error or metric.
//!
//! The module also owns the tiny guarded REST helper ([`Rest`]) the host
//! duties share, so the `Authorization` composition and status mapping live
//! in one place.

use std::collections::HashMap;

use bundle_host_http::guarded::{GuardedClient, GuardedLimits, GuardedResponse};

use super::subst::SlotValue;
use super::twitch_slots::map_cred;
use super::{SlotError, SlotRequest, SlotSource};
use crate::discord_rest::is_snowflake;
use crate::outbound_creds_discord::DiscordCredentialSource;

/// Discord REST base the host pins egress to.
pub const API_BASE: &str = "https://discord.com/api/v10";
/// The connector's `secret-refs` handle for the bot credential.
pub const BOT_HANDLE: &str = "discord-bot";

/// One bot-authenticated, SSRF-guarded Discord REST caller.
pub(crate) struct Rest<'a> {
    pub(crate) client: &'a GuardedClient,
    pub(crate) base: &'a str,
    pub(crate) token: &'a str,
}

impl Rest<'_> {
    /// One call: `Authorization: Bot <token>` composed here, JSON body when
    /// given. Transport failure is the typed, retryable
    /// `discord_lookup_failed`; the status is the caller's to interpret
    /// (except 401, which is always an unusable connection).
    pub(crate) async fn call(
        &self,
        method: reqwest::Method,
        path: &str,
        body: Option<&serde_json::Value>,
    ) -> Result<GuardedResponse, SlotError> {
        let mut headers = vec![("Authorization".to_string(), format!("Bot {}", self.token))];
        let bytes = match body {
            Some(v) => {
                headers.push(("Content-Type".to_string(), "application/json".to_string()));
                Some(serde_json::to_vec(v).map_err(|_| SlotError::Refused("bad_request"))?)
            }
            None => None,
        };
        let resp = self
            .client
            .fetch(method, &format!("{}{path}", self.base), headers, bytes)
            .await
            .map_err(|_| SlotError::Unavailable("discord_lookup_failed"))?;
        if resp.status == 401 {
            return Err(SlotError::Refused("connection_not_usable"));
        }
        Ok(resp)
    }

    /// The guild a channel belongs to (`GET /channels/{id}` -> `guild_id`);
    /// `None` for a channel with no guild (a DM).
    pub(crate) async fn channel_guild(&self, channel: &str) -> Result<Option<String>, SlotError> {
        if !is_snowflake(channel) {
            return Err(SlotError::Refused("channel_not_owned"));
        }
        let resp = self
            .call(reqwest::Method::GET, &format!("/channels/{channel}"), None)
            .await?;
        match resp.status {
            200..=299 => {}
            403 | 404 => return Err(SlotError::Refused("channel_not_owned")),
            _ => return Err(SlotError::Unavailable("discord_lookup_failed")),
        }
        let v: serde_json::Value = serde_json::from_slice(&resp.body)
            .map_err(|_| SlotError::Unavailable("discord_lookup_failed"))?;
        match v.get("guild_id").and_then(serde_json::Value::as_str) {
            Some(g) if is_snowflake(g) => Ok(Some(g.to_string())),
            Some(_) => Err(SlotError::Unavailable("discord_lookup_failed")),
            None => Ok(None),
        }
    }
}

/// Per-community Discord slot resolution over a [`DiscordCredentialSource`].
pub struct DiscordSlots<C: DiscordCredentialSource> {
    creds: C,
    client: GuardedClient,
    api_base: String,
}

impl<C: DiscordCredentialSource> DiscordSlots<C> {
    /// Production wiring: the real REST base through a default
    /// (empty-allowlist) guarded client.
    #[must_use]
    pub fn new(creds: C) -> Self {
        Self::with_endpoint(
            creds,
            GuardedClient::new(GuardedLimits::default(), Vec::new()),
            API_BASE.to_string(),
        )
    }

    /// Explicit client + REST base (tests point it at a local fake).
    #[must_use]
    pub fn with_endpoint(creds: C, client: GuardedClient, api_base: String) -> Self {
        Self {
            creds,
            client,
            api_base,
        }
    }
}

impl<C: DiscordCredentialSource> SlotSource for DiscordSlots<C> {
    async fn resolve(
        &self,
        req: &SlotRequest<'_>,
    ) -> Result<HashMap<String, SlotValue>, SlotError> {
        if req.platform != "discord" {
            return Err(SlotError::Refused("unsupported_platform"));
        }
        if req.handles.iter().any(|h| *h != BOT_HANDLE) {
            return Err(SlotError::Refused("unknown_handle"));
        }
        let channel = req
            .pinned_channel
            .ok_or(SlotError::Refused("no_origin_pin"))?;
        // Fresh on every send; never another community's credential.
        let binding = self
            .creds
            .binding(req.community_id)
            .await
            .map_err(map_cred)?;
        let rest = Rest {
            client: &self.client,
            base: &self.api_base,
            token: binding.bot_token.expose(),
        };
        if rest.channel_guild(channel).await?.as_deref() != Some(binding.guild_id.as_str()) {
            return Err(SlotError::Refused("channel_not_owned"));
        }
        let mut out = HashMap::new();
        out.insert(
            BOT_HANDLE.to_string(),
            SlotValue::new(binding.bot_token.expose().to_owned()),
        );
        Ok(out)
    }
}
