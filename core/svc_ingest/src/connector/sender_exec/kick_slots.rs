//! The Kick [`SlotSource`]: turns the per-community credential seam ([`KickCredentialSource`])
//! into the single host-resolved value a Kick `chat.send` template may carry.
//!
//! | Handle | Slot | Value |
//! |---|---|---|
//! | `token` | `{{KICK_TOKEN}}` | the community's Kick OAuth access token |
//! | `broadcaster_id` | `{{KICK_BROADCASTER_ID}}` | the connection's `broadcaster_user_id` (moderation only; a JSON integer) |
//!
//! `chat.send` posts as the application bot to the channel the token is attached to, so it has no
//! destination slot: the token is resolved ONLY for the stamped community (a queue payload or
//! guest can never name another one). Moderation also resolves the community's OWN broadcaster id
//! from the same binding, so a bundle can never aim the token at another channel; a binding with
//! no valid `broadcaster_user_id` refuses that handle loudly. Every other handle is refused. The token
//! appears only in the returned zeroizing [`SlotValue`]; never in a log, error or metric.

use std::collections::HashMap;

use super::subst::SlotValue;
use super::twitch_slots::map_cred;
use super::{SlotError, SlotRequest, SlotSource};
use crate::outbound_creds_kick::KickCredentialSource;

/// Connector handle for the OAuth token (matches `policy/kick.rs`).
pub const TOKEN_HANDLE: &str = "token";
/// Connector handle for the broadcaster's numeric user id (matches `policy/kick.rs`).
pub const BROADCASTER_HANDLE: &str = "broadcaster_id";

/// Per-community Kick slot resolution over a [`KickCredentialSource`].
pub struct KickSlots<C: KickCredentialSource> {
    creds: C,
}

impl<C: KickCredentialSource> KickSlots<C> {
    /// Wraps the credential source.
    #[must_use]
    pub fn new(creds: C) -> Self {
        Self { creds }
    }
}

impl<C: KickCredentialSource> SlotSource for KickSlots<C> {
    async fn resolve(
        &self,
        req: &SlotRequest<'_>,
    ) -> Result<HashMap<String, SlotValue>, SlotError> {
        if req.platform != "kick" {
            return Err(SlotError::Refused("unsupported_platform"));
        }
        if req
            .handles
            .iter()
            .any(|h| *h != TOKEN_HANDLE && *h != BROADCASTER_HANDLE)
        {
            return Err(SlotError::Refused("unknown_handle"));
        }
        // Fresh on every send; scoped to the stamped community only.
        let binding = self
            .creds
            .binding(req.community_id)
            .await
            .map_err(map_cred)?;
        let mut out = HashMap::new();
        for h in req.handles {
            let value = if *h == BROADCASTER_HANDLE {
                binding
                    .broadcaster_user_id
                    .clone()
                    .ok_or(SlotError::Refused("broadcaster_id_unbound"))?
            } else {
                binding.oauth_token.expose().to_owned()
            };
            out.insert((*h).to_string(), SlotValue::new(value));
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
    use crate::outbound_creds_kick::KickBinding;

    struct Fixed(Result<(), CredError>, Option<&'static str>);

    impl KickCredentialSource for Fixed {
        async fn binding(&self, c: Option<i64>) -> Result<KickBinding, CredError> {
            self.0?;
            assert_eq!(c, Some(1), "community scope must be forwarded verbatim");
            Ok(KickBinding {
                oauth_token: SecretString::new("tok".to_string()),
                broadcaster_user_id: self.1.map(str::to_string),
            })
        }
    }

    fn req<'a>(platform: &'a str, handles: &'a [&'static str]) -> SlotRequest<'a> {
        SlotRequest {
            guild_scoped: false,
            platform,
            community_id: Some(1),
            pinned_channel: None,
            handles,
        }
    }

    #[tokio::test]
    async fn the_token_handle_resolves_without_any_pin() {
        let s = KickSlots::new(Fixed(Ok(()), Some("777")));
        let out = s.resolve(&req("kick", &["token"])).await.unwrap();
        assert_eq!(out["token"].expose(), "tok");
        assert_eq!(out.len(), 1);
    }

    #[tokio::test]
    async fn the_broadcaster_handle_is_the_bindings_own_id_and_refused_when_unbound() {
        let s = KickSlots::new(Fixed(Ok(()), Some("777")));
        let out = s
            .resolve(&req("kick", &["token", "broadcaster_id"]))
            .await
            .unwrap();
        assert_eq!(out["broadcaster_id"].expose(), "777");
        assert_eq!(out["token"].expose(), "tok");
        // No valid broadcaster id on the binding: loud, non-retryable refusal, no fallback.
        let s = KickSlots::new(Fixed(Ok(()), None));
        assert_eq!(
            s.resolve(&req("kick", &["broadcaster_id"]))
                .await
                .unwrap_err(),
            SlotError::Refused("broadcaster_id_unbound")
        );
        // chat.send (token only) is unaffected by an unbound broadcaster.
        assert!(s.resolve(&req("kick", &["token"])).await.is_ok());
    }

    #[tokio::test]
    async fn other_platforms_and_unknown_handles_are_refused_before_any_resolution() {
        let s = KickSlots::new(Fixed(Err(CredError::NoBinding), None));
        for (p, h) in [
            ("twitch", &["token"][..]),
            ("kick", &["token", "channel"][..]),
            ("kick", &["kick.oauth"][..]),
        ] {
            let e = s.resolve(&req(p, h)).await.unwrap_err();
            assert!(matches!(e, SlotError::Refused(_)), "{p} {h:?}");
        }
    }

    #[tokio::test]
    async fn credential_errors_map_to_refused_or_retryable() {
        for (e, retry) in [
            (CredError::NoBinding, false),
            (CredError::AmbiguousBinding, false),
            (CredError::NoCommunityScope, false),
            (CredError::NotUsable, false),
            (CredError::Unavailable, true),
            (CredError::TokenExpired, true),
        ] {
            let s = KickSlots::new(Fixed(Err(e), None));
            let got = s.resolve(&req("kick", &["token"])).await.unwrap_err();
            assert_eq!(matches!(got, SlotError::Unavailable(_)), retry, "{e:?}");
        }
    }
}
