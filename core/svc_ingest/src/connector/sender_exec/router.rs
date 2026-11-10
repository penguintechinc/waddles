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

use super::app_credentials::{AppCredentialSource, NoAppCredentials};
use super::policy::APP_HANDLE_PREFIX;
use super::{SlotError, SlotRequest, SlotSource, SlotValue};

/// Stable refusal code for a platform no source is registered for.
pub const UNKNOWN_PLATFORM: &str = "slot_platform_unknown";

/// Dispatches [`SlotRequest`]s by `req.platform` to the platform's own source.
/// `app.*` handles are answered ONLY by the app-credential source `A`
/// ([`NoAppCredentials`] by default: every app-scoped kind is refused).
pub struct PlatformSlotRouter<T, D, Y, A = NoAppCredentials> {
    twitch: T,
    discord: D,
    youtube: Y,
    app: A,
}

impl<T, D, Y> PlatformSlotRouter<T, D, Y, NoAppCredentials>
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
            app: NoAppCredentials,
        }
    }

    /// Attaches the app-credential source that answers `app.*` handles.
    #[must_use]
    pub fn with_app_credentials<A: AppCredentialSource>(
        self,
        app: A,
    ) -> PlatformSlotRouter<T, D, Y, A> {
        PlatformSlotRouter {
            twitch: self.twitch,
            discord: self.discord,
            youtube: self.youtube,
            app,
        }
    }
}

impl<T, D, Y, A> SlotSource for PlatformSlotRouter<T, D, Y, A>
where
    T: SlotSource,
    D: SlotSource,
    Y: SlotSource,
    A: AppCredentialSource,
{
    async fn resolve(
        &self,
        req: &SlotRequest<'_>,
    ) -> Result<HashMap<String, SlotValue>, SlotError> {
        // App-scoped handles never reach a user-credential platform source
        // (and a mixed set is refused: one credential scope per call).
        let app_handles = req
            .handles
            .iter()
            .filter(|h| h.starts_with(APP_HANDLE_PREFIX))
            .count();
        if app_handles > 0 {
            if app_handles != req.handles.len() {
                return Err(SlotError::Refused("slot_credential_scope_mixed"));
            }
            if !self.app.supports(req.platform) {
                return Err(SlotError::Refused("slot_app_credential_unsupported"));
            }
            return self
                .app
                .resolve(req.platform, req.community_id, req.handles)
                .await;
        }
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
        // The app source is the sole authority for app-scoped credentials; a
        // platform's USER source opting in is not enough (it would be handed
        // `app.` handles it refuses).
        self.app.supports(platform)
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

    /// App source recording the handles it was asked for.
    struct AppTagged(Arc<AtomicUsize>);

    impl AppCredentialSource for AppTagged {
        fn supports(&self, platform: &str) -> bool {
            matches!(platform, "twitch" | "discord")
        }

        async fn resolve(
            &self,
            _platform: &str,
            _community_id: Option<i64>,
            handles: &[&'static str],
        ) -> Result<HashMap<String, SlotValue>, SlotError> {
            self.0.fetch_add(1, Ordering::SeqCst);
            Ok(handles
                .iter()
                .map(|h| ((*h).to_string(), SlotValue::new("app-value".to_string())))
                .collect())
        }
    }

    fn app_req<'a>(platform: &'a str, handles: &'a [&'static str]) -> SlotRequest<'a> {
        SlotRequest {
            guild_scoped: false,
            platform,
            community_id: Some(7),
            pinned_channel: None,
            handles,
        }
    }

    #[tokio::test]
    async fn app_handles_go_only_to_the_app_source_never_a_user_source() {
        let (r, counters) = router();
        let app_calls = Arc::new(AtomicUsize::new(0));
        let r = r.with_app_credentials(AppTagged(Arc::clone(&app_calls)));
        for p in ["twitch", "discord"] {
            assert!(r.supports_app_credentials(p));
            let out = r.resolve(&app_req(p, &["app.token"])).await.unwrap();
            assert_eq!(out["app.token"].expose(), "app-value");
        }
        assert_eq!(app_calls.load(Ordering::SeqCst), 2);
        assert!(
            counters.iter().all(|c| c.load(Ordering::SeqCst) == 0),
            "a user-credential source saw an app handle"
        );
        // youtube has no app surface; kick/unknown neither.
        assert!(!r.supports_app_credentials("youtube"));
        assert_eq!(
            r.resolve(&app_req("youtube", &["app.token"]))
                .await
                .unwrap_err(),
            SlotError::Refused("slot_app_credential_unsupported")
        );
    }

    #[tokio::test]
    async fn mixed_app_and_user_handles_are_refused_without_consulting_anyone() {
        let (r, counters) = router();
        let app_calls = Arc::new(AtomicUsize::new(0));
        let r = r.with_app_credentials(AppTagged(Arc::clone(&app_calls)));
        let err = r
            .resolve(&app_req("twitch", &["app.token", "token"]))
            .await
            .unwrap_err();
        assert_eq!(err, SlotError::Refused("slot_credential_scope_mixed"));
        assert_eq!(app_calls.load(Ordering::SeqCst), 0);
        assert!(counters.iter().all(|c| c.load(Ordering::SeqCst) == 0));
    }

    #[tokio::test]
    async fn without_an_app_source_app_handles_are_refused_fail_closed() {
        let (r, counters) = router();
        let err = r
            .resolve(&app_req("twitch", &["app.token"]))
            .await
            .unwrap_err();
        assert_eq!(err, SlotError::Refused("slot_app_credential_unsupported"));
        assert!(counters.iter().all(|c| c.load(Ordering::SeqCst) == 0));
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
