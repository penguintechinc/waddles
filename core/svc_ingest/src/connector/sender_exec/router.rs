//! The per-platform [`SlotSource`] router.
//!
//! [`super::SenderExecutor`] takes exactly ONE [`SlotSource`]; production has
//! three platform-specific ones (`TwitchSlots`, `DiscordSlots`,
//! `YoutubeSlots`), each of which resolves only its own platform's handles
//! and refuses any other. [`PlatformSlotRouter`] is the dispatcher: the
//! platform string the executor hands in selects exactly one source, and an
//! unknown platform is a typed [`SlotError::Refused`] -- never a default
//! source, never a fallthrough to "the first one that answers" (a credential
//! of one platform must never be offered to another).

use std::collections::HashMap;

use super::{SlotError, SlotRequest, SlotSource, SlotValue};

/// Stable refusal code for a platform no source is registered for.
pub const UNKNOWN_PLATFORM: &str = "slot_platform_unknown";

/// Dispatches [`SlotRequest`]s by `req.platform` to the platform's own source.
pub struct PlatformSlotRouter<T, D, Y> {
    twitch: T,
    discord: D,
    youtube: Y,
}

impl<T, D, Y> PlatformSlotRouter<T, D, Y>
where
    T: SlotSource,
    D: SlotSource,
    Y: SlotSource,
{
    /// A router over the three production platform sources.
    #[must_use]
    pub fn new(twitch: T, discord: D, youtube: Y) -> Self {
        Self {
            twitch,
            discord,
            youtube,
        }
    }
}

impl<T, D, Y> SlotSource for PlatformSlotRouter<T, D, Y>
where
    T: SlotSource,
    D: SlotSource,
    Y: SlotSource,
{
    async fn resolve(
        &self,
        req: &SlotRequest<'_>,
    ) -> Result<HashMap<String, SlotValue>, SlotError> {
        match req.platform {
            "twitch" => self.twitch.resolve(req).await,
            "discord" => self.discord.resolve(req).await,
            "youtube" => self.youtube.resolve(req).await,
            _ => Err(SlotError::Refused(UNKNOWN_PLATFORM)),
        }
    }

    /// Delegates to the platform's own source; an unknown platform (Kick included, which has
    /// no slot source yet) never supports app credentials.
    fn supports_app_credentials(&self, platform: &str) -> bool {
        match platform {
            "twitch" => self.twitch.supports_app_credentials(platform),
            "discord" => self.discord.supports_app_credentials(platform),
            "youtube" => self.youtube.supports_app_credentials(platform),
            _ => false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;

    /// A source that records how often it was asked and answers with its tag.
    struct Tagged {
        tag: &'static str,
        calls: Arc<AtomicUsize>,
    }

    impl SlotSource for Tagged {
        async fn resolve(
            &self,
            _req: &SlotRequest<'_>,
        ) -> Result<HashMap<String, SlotValue>, SlotError> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            Ok(HashMap::from([(
                "who".to_string(),
                SlotValue::new(self.tag.to_string()),
            )]))
        }
    }

    type Counters = [Arc<AtomicUsize>; 3];

    fn router() -> (PlatformSlotRouter<Tagged, Tagged, Tagged>, Counters) {
        let counters: Counters = [
            Arc::new(AtomicUsize::new(0)),
            Arc::new(AtomicUsize::new(0)),
            Arc::new(AtomicUsize::new(0)),
        ];
        let mk = |tag, i: usize| Tagged {
            tag,
            calls: Arc::clone(&counters[i]),
        };
        (
            PlatformSlotRouter::new(mk("twitch", 0), mk("discord", 1), mk("youtube", 2)),
            counters,
        )
    }

    fn req(platform: &str) -> SlotRequest<'_> {
        SlotRequest {
            guild_scoped: false,
            platform,
            community_id: Some(7),
            pinned_channel: Some("#chan"),
            handles: &["token"],
        }
    }

    #[tokio::test]
    async fn each_platform_reaches_only_its_own_source() {
        for (i, p) in ["twitch", "discord", "youtube"].into_iter().enumerate() {
            let (r, counters) = router();
            let out = r.resolve(&req(p)).await.unwrap();
            assert_eq!(out["who"].expose(), p);
            for (j, c) in counters.iter().enumerate() {
                assert_eq!(
                    c.load(Ordering::SeqCst),
                    usize::from(i == j),
                    "{p}: source {j}"
                );
            }
        }
    }

    #[test]
    fn app_credential_support_is_per_platform_and_defaults_off() {
        let (r, _) = router();
        for p in ["twitch", "discord", "youtube", "kick", "slack", ""] {
            assert!(!r.supports_app_credentials(p), "{p:?}");
        }
    }

    #[tokio::test]
    async fn an_unknown_platform_is_refused_and_no_source_is_consulted() {
        for p in ["slack", "", "Twitch", "twitch ", "kick"] {
            let (r, counters) = router();
            let err = r.resolve(&req(p)).await.unwrap_err();
            assert_eq!(err, SlotError::Refused(UNKNOWN_PLATFORM), "{p:?}");
            assert!(
                counters.iter().all(|c| c.load(Ordering::SeqCst) == 0),
                "{p:?} reached a source"
            );
        }
    }
}
