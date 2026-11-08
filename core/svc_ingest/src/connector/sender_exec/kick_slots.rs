//! The Kick [`SlotSource`]: turns the per-community credential seam ([`KickCredentialSource`])
//! into the single host-resolved value a Kick `chat.send` template may carry.
//!
//! | Handle | Slot | Value |
//! |---|---|---|
//! | `token` | `{{KICK_TOKEN}}` | the community's Kick OAuth access token |
//!
//! `chat.send` posts as the application bot to the channel the token is attached to, so there is
//! no destination slot and no channel pin: the token is resolved ONLY for the stamped community
//! (a queue payload or guest can never name another one). Every other handle is refused. The token
//! appears only in the returned zeroizing [`SlotValue`]; never in a log, error or metric.

use std::collections::HashMap;

use super::subst::SlotValue;
use super::twitch_slots::map_cred;
use super::{SlotError, SlotRequest, SlotSource};
use crate::outbound_creds_kick::KickCredentialSource;

/// Connector handle for the OAuth token (matches `policy/kick.rs`).
pub const TOKEN_HANDLE: &str = "token";

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
        if req.handles.iter().any(|h| *h != TOKEN_HANDLE) {
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
            out.insert(
                (*h).to_string(),
                SlotValue::new(binding.oauth_token.expose().to_owned()),
            );
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

    struct Fixed(Result<(), CredError>);

    impl KickCredentialSource for Fixed {
        async fn binding(&self, c: Option<i64>) -> Result<KickBinding, CredError> {
            self.0?;
            assert_eq!(c, Some(1), "community scope must be forwarded verbatim");
            Ok(KickBinding {
                oauth_token: SecretString::new("tok".to_string()),
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
        let s = KickSlots::new(Fixed(Ok(())));
        let out = s.resolve(&req("kick", &["token"])).await.unwrap();
        assert_eq!(out["token"].expose(), "tok");
        assert_eq!(out.len(), 1);
    }

    #[tokio::test]
    async fn other_platforms_and_unknown_handles_are_refused_before_any_resolution() {
        let s = KickSlots::new(Fixed(Err(CredError::NoBinding)));
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
            let s = KickSlots::new(Fixed(Err(e)));
            let got = s.resolve(&req("kick", &["token"])).await.unwrap_err();
            assert_eq!(matches!(got, SlotError::Unavailable(_)), retry, "{e:?}");
        }
    }
}
