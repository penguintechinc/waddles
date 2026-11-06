//! The YouTube [`SlotSource`] (P6a): turns the per-community binding seam
//! ([`YoutubeCredentialSource`]) into the two host-pinned values a YouTube
//! `chat.send` template may carry.
//!
//! | Handle | Slot | Value |
//! |---|---|---|
//! | `youtube.oauth` | `{{YT_OAUTH_TOKEN}}` | the community's OAuth access token |
//! | `youtube.live_chat_id` | `{{YT_LIVE_CHAT_ID}}` | the connection's pinned live chat |
//!
//! The live-chat id comes ONLY from the resolved connection's non-secret
//! metadata; the stamp-pinned id (`pinned_channel`) is the ownership proof --
//! the record must own exactly that id. An id from the queue payload or the
//! guest is never consulted. Every other handle (`youtube.channel_id`,
//! `youtube.video_id`, anything unknown) is refused: they are read-path slots.
//! The token appears only in the returned zeroizing [`SlotValue`]; never in a
//! log, error or metric.

use std::collections::HashMap;

use super::subst::SlotValue;
use super::twitch_slots::map_cred;
use super::{SlotError, SlotRequest, SlotSource};
use crate::outbound_creds_youtube::YoutubeCredentialSource;

/// Connector handle for the OAuth token.
pub const OAUTH_HANDLE: &str = "youtube.oauth";
/// Connector handle for the pinned live chat.
pub const LIVE_CHAT_HANDLE: &str = "youtube.live_chat_id";

/// Per-community YouTube slot resolution over a [`YoutubeCredentialSource`].
pub struct YoutubeSlots<C: YoutubeCredentialSource> {
    creds: C,
}

impl<C: YoutubeCredentialSource> YoutubeSlots<C> {
    /// Wraps the credential source.
    #[must_use]
    pub fn new(creds: C) -> Self {
        Self { creds }
    }
}

impl<C: YoutubeCredentialSource> SlotSource for YoutubeSlots<C> {
    async fn resolve(
        &self,
        req: &SlotRequest<'_>,
    ) -> Result<HashMap<String, SlotValue>, SlotError> {
        if req.platform != "youtube" {
            return Err(SlotError::Refused("unsupported_platform"));
        }
        if req
            .handles
            .iter()
            .any(|h| *h != OAUTH_HANDLE && *h != LIVE_CHAT_HANDLE)
        {
            return Err(SlotError::Refused("unknown_handle"));
        }
        let pinned = req
            .pinned_channel
            .ok_or(SlotError::Refused("no_origin_pin"))?;
        // Fresh on every send; also proves the community owns the pinned chat.
        let binding = self
            .creds
            .binding(req.community_id, pinned)
            .await
            .map_err(map_cred)?;
        // Defense in depth: never hand out a chat other than the stamped one.
        if binding.live_chat_id != pinned {
            return Err(SlotError::Refused("channel_not_owned"));
        }
        let mut out = HashMap::new();
        for h in req.handles {
            let v = if *h == OAUTH_HANDLE {
                binding.oauth_token.expose().to_owned()
            } else {
                binding.live_chat_id.clone()
            };
            out.insert((*h).to_string(), SlotValue::new(v));
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use connection_resolver::SecretString;

    use super::*;
    use crate::outbound_creds::CredError;
    use crate::outbound_creds_youtube::YoutubeBinding;

    struct Fixed(Result<(), CredError>, &'static str);

    impl YoutubeCredentialSource for Fixed {
        async fn binding(&self, _c: Option<i64>, _chat: &str) -> Result<YoutubeBinding, CredError> {
            self.0?;
            Ok(YoutubeBinding {
                oauth_token: SecretString::new("tok".to_string()),
                live_chat_id: self.1.to_string(),
            })
        }
    }

    fn req<'a>(
        platform: &'a str,
        pin: Option<&'a str>,
        handles: &'a [&'static str],
    ) -> SlotRequest<'a> {
        SlotRequest {
            guild_scoped: false,
            platform,
            community_id: Some(1),
            pinned_channel: pin,
            handles,
        }
    }

    #[tokio::test]
    async fn serves_only_the_requested_handles() {
        let s = YoutubeSlots::new(Fixed(Ok(()), "chat1"));
        let got = s
            .resolve(&req("youtube", Some("chat1"), &[OAUTH_HANDLE]))
            .await
            .unwrap();
        assert_eq!(got.len(), 1);
        assert_eq!(got[OAUTH_HANDLE].expose(), "tok");
        let got = s
            .resolve(&req(
                "youtube",
                Some("chat1"),
                &[OAUTH_HANDLE, LIVE_CHAT_HANDLE],
            ))
            .await
            .unwrap();
        assert_eq!(got[LIVE_CHAT_HANDLE].expose(), "chat1");
    }

    #[tokio::test]
    async fn refusals_are_typed() {
        let ok = || YoutubeSlots::new(Fixed(Ok(()), "chat1"));
        assert_eq!(
            ok().resolve(&req("twitch", Some("chat1"), &[OAUTH_HANDLE]))
                .await
                .unwrap_err(),
            SlotError::Refused("unsupported_platform")
        );
        for h in ["youtube.channel_id", "youtube.video_id", "token"] {
            let handles: &[&'static str] = &[match h {
                "youtube.channel_id" => "youtube.channel_id",
                "youtube.video_id" => "youtube.video_id",
                _ => "token",
            }];
            assert_eq!(
                ok().resolve(&req("youtube", Some("chat1"), handles))
                    .await
                    .unwrap_err(),
                SlotError::Refused("unknown_handle")
            );
        }
        assert_eq!(
            ok().resolve(&req("youtube", None, &[OAUTH_HANDLE]))
                .await
                .unwrap_err(),
            SlotError::Refused("no_origin_pin")
        );
        // A source answering for a different chat than the stamp is refused.
        let s = YoutubeSlots::new(Fixed(Ok(()), "other"));
        assert_eq!(
            s.resolve(&req("youtube", Some("chat1"), &[LIVE_CHAT_HANDLE]))
                .await
                .unwrap_err(),
            SlotError::Refused("channel_not_owned")
        );
    }

    #[tokio::test]
    async fn credential_errors_map_with_retryability() {
        for (e, want, retry) in [
            (CredError::NoBinding, "no_connection", false),
            (CredError::ChannelNotOwned, "channel_not_owned", false),
            (CredError::NotUsable, "connection_not_usable", false),
            (CredError::Unavailable, "credential_unavailable", true),
            (CredError::TokenExpired, "token_expired", true),
        ] {
            let s = YoutubeSlots::new(Fixed(Err(e), "chat1"));
            let err = s
                .resolve(&req("youtube", Some("chat1"), &[OAUTH_HANDLE]))
                .await
                .unwrap_err();
            assert_eq!(
                err,
                if retry {
                    SlotError::Unavailable(want)
                } else {
                    SlotError::Refused(want)
                }
            );
        }
    }
}
