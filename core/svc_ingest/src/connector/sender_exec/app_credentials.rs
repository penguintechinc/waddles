//! App-scoped credential slot source (`app.*` handles).
//!
//! The per-platform user sources (`TwitchSlots`, `DiscordSlots`) resolve only
//! the community's USER/bot credential and refuse any `app.` handle. An
//! app-scoped kind (EventSub subscription, Discord command registration) must
//! instead run on the APPLICATION's credential: a client-credentials token
//! minted + cached host-side, the application id, the platform webhook
//! secret. [`PlatformAppCredentials`] supplies those; the router
//! ([`super::router::PlatformSlotRouter`]) sends `app.` handles ONLY here and
//! never to a user source (a user token is never substituted for an app one,
//! and an app token is never offered to a user slot).
//!
//! Values come from the host's own configuration (env / Helm secrets) or the
//! host-minted token cache -- never from the bundle or the payload.

use std::collections::HashMap;
use std::sync::Arc;

use bundle_host_http::credentials::{AppCredentialConfig, AppTokenMinter, ClientAuth};

use super::{SlotError, SlotValue};

/// Name of the minted Twitch application credential in the minter.
const TWITCH_APP: &str = "twitch.app";

/// Resolves `app.*` handles for a platform. `Refused` is a deterministic
/// policy refusal; `Unavailable` is retryable.
#[allow(async_fn_in_trait)] // service-binary-internal
pub trait AppCredentialSource: Send + Sync {
    /// Whether `platform` has an app credential surface at all.
    fn supports(&self, platform: &str) -> bool;

    /// Resolves every requested `app.*` handle or fails the whole call.
    async fn resolve(
        &self,
        platform: &str,
        community_id: Option<i64>,
        handles: &[&'static str],
    ) -> Result<HashMap<String, SlotValue>, SlotError>;
}

/// No app credentials anywhere (the router default): every app-scoped kind is
/// refused, fail-closed.
pub struct NoAppCredentials;

impl AppCredentialSource for NoAppCredentials {
    fn supports(&self, _platform: &str) -> bool {
        false
    }

    async fn resolve(
        &self,
        _platform: &str,
        _community_id: Option<i64>,
        _handles: &[&'static str],
    ) -> Result<HashMap<String, SlotValue>, SlotError> {
        Err(SlotError::Refused("slot_app_credential_unsupported"))
    }
}

/// Host-config-backed app credentials for twitch + discord.
pub struct PlatformAppCredentials {
    minter: Arc<AppTokenMinter>,
    get: Box<dyn Fn(&str) -> Option<String> + Send + Sync>,
}

impl PlatformAppCredentials {
    /// Production wiring: the process environment is the config source and
    /// Twitch's client-credentials endpoint the token source.
    pub fn from_env() -> Result<Self, String> {
        Self::new(Box::new(|k| std::env::var(k).ok()), None)
    }

    /// Explicit config source (tests); `token_url` overrides Twitch's endpoint
    /// ONLY for the loopback test minter.
    pub fn new(
        get: Box<dyn Fn(&str) -> Option<String> + Send + Sync>,
        loopback_token_url: Option<String>,
    ) -> Result<Self, String> {
        let cfg = |url: String| {
            HashMap::from([(
                TWITCH_APP.to_string(),
                AppCredentialConfig {
                    token_url: url,
                    client_id_env: "TWITCH_CLIENT_ID".to_string(),
                    client_secret_env: "TWITCH_CLIENT_SECRET".to_string(),
                    // Raw token: the policy template adds `Bearer `.
                    scheme: String::new(),
                    auth: ClientAuth::Form,
                },
            )])
        };
        let minter = match loopback_token_url {
            Some(url) => AppTokenMinter::new_for_loopback_tests(cfg(url))?,
            None => AppTokenMinter::new(cfg("https://id.twitch.tv/oauth2/token".to_string()))?,
        };
        Ok(Self {
            minter: Arc::new(minter),
            get,
        })
    }

    fn env(&self, key: &'static str) -> Result<SlotValue, SlotError> {
        (self.get)(key)
            .filter(|v| !v.trim().is_empty())
            .map(SlotValue::new)
            .ok_or(SlotError::Refused("slot_unresolved"))
    }

    async fn twitch(&self, handle: &str) -> Result<SlotValue, SlotError> {
        match handle {
            "app.token" => self
                .minter
                .token(TWITCH_APP)
                .await
                .map(SlotValue::new)
                .map_err(|_| SlotError::Unavailable("app_token_unavailable")),
            "app.client_id" => self.env("TWITCH_CLIENT_ID"),
            "app.eventsub_callback" => self.env("TWITCH_EVENTSUB_CALLBACK"),
            "app.eventsub_secret" => self.env("TWITCH_EVENTSUB_SECRET"),
            // The broadcaster id is per-community identity data only the
            // community's own connection knows; this source has none, so it
            // refuses rather than guessing (see the PR's handoff for the
            // owner of `twitch_slots.rs`).
            _ => Err(SlotError::Refused("unknown_handle")),
        }
    }
}

impl AppCredentialSource for PlatformAppCredentials {
    fn supports(&self, platform: &str) -> bool {
        matches!(platform, "twitch" | "discord")
    }

    async fn resolve(
        &self,
        platform: &str,
        _community_id: Option<i64>,
        handles: &[&'static str],
    ) -> Result<HashMap<String, SlotValue>, SlotError> {
        if !self.supports(platform) {
            return Err(SlotError::Refused("slot_app_credential_unsupported"));
        }
        let mut out = HashMap::new();
        for h in handles {
            if !h.starts_with(super::policy::APP_HANDLE_PREFIX) {
                // A user-scoped handle must never be answered from app config.
                return Err(SlotError::Refused("slot_credential_scope_mixed"));
            }
            let v = match platform {
                "twitch" => self.twitch(h).await?,
                _ => match *h {
                    "app.application_id" => self.env("DISCORD_APPLICATION_ID")?,
                    _ => return Err(SlotError::Refused("unknown_handle")),
                },
            };
            out.insert((*h).to_string(), v);
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    fn src(env: &'static [(&'static str, &'static str)]) -> PlatformAppCredentials {
        PlatformAppCredentials::new(
            Box::new(move |k| {
                env.iter()
                    .find(|(n, _)| *n == k)
                    .map(|(_, v)| (*v).to_string())
            }),
            None,
        )
        .unwrap()
    }

    #[tokio::test]
    async fn static_app_handles_resolve_from_host_config() {
        let s = src(&[
            ("TWITCH_CLIENT_ID", "cid"),
            ("TWITCH_EVENTSUB_SECRET", "hmac"),
            ("TWITCH_EVENTSUB_CALLBACK", "https://cb.example/hook"),
            ("DISCORD_APPLICATION_ID", "123"),
        ]);
        let t = s
            .resolve(
                "twitch",
                Some(1),
                &["app.client_id", "app.eventsub_secret", "app.eventsub_callback"],
            )
            .await
            .unwrap();
        assert_eq!(t["app.client_id"].expose(), "cid");
        assert_eq!(t["app.eventsub_secret"].expose(), "hmac");
        let d = s
            .resolve("discord", None, &["app.application_id"])
            .await
            .unwrap();
        assert_eq!(d["app.application_id"].expose(), "123");
    }

    #[tokio::test]
    async fn unset_unknown_mixed_and_unsupported_are_refused() {
        let s = src(&[("TWITCH_CLIENT_ID", "cid")]);
        let e = |r: Result<HashMap<String, SlotValue>, SlotError>| r.unwrap_err();
        assert_eq!(
            e(s.resolve("twitch", Some(1), &["app.eventsub_secret"]).await),
            SlotError::Refused("slot_unresolved")
        );
        assert_eq!(
            e(s.resolve("twitch", Some(1), &["app.broadcaster_id"]).await),
            SlotError::Refused("unknown_handle")
        );
        assert_eq!(
            e(s.resolve("twitch", Some(1), &["app.client_id", "token"]).await),
            SlotError::Refused("slot_credential_scope_mixed")
        );
        assert_eq!(
            e(s.resolve("youtube", Some(1), &["app.client_id"]).await),
            SlotError::Refused("slot_app_credential_unsupported")
        );
        assert_eq!(
            e(s.resolve("discord", Some(1), &["app.client_id"]).await),
            SlotError::Refused("unknown_handle")
        );
        assert!(!s.supports("kick"));
    }

    /// Real-path: the host mints the Twitch app token against a real local
    /// token endpoint, caches it (ONE mint for many sends), returns it raw
    /// (the policy template adds `Bearer `), and the endpoint saw the client
    /// credentials the HOST holds.
    #[tokio::test]
    async fn twitch_app_token_is_minted_once_cached_and_returned_raw() {
        use wiremock::matchers::{body_string_contains, method, path};
        use wiremock::{Mock, MockServer, ResponseTemplate};

        // SAFETY: test-local; only the minter reads these names.
        unsafe {
            std::env::set_var("TWITCH_CLIENT_ID", "host-cid");
            std::env::set_var("TWITCH_CLIENT_SECRET", "host-secret");
        }
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/oauth2/token"))
            .and(body_string_contains("grant_type=client_credentials"))
            .and(body_string_contains("client_id=host-cid"))
            .and(body_string_contains("client_secret=host-secret"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "access_token": "apptok-1", "expires_in": 3600, "token_type": "bearer"
            })))
            .expect(1)
            .mount(&server)
            .await;
        let s = PlatformAppCredentials::new(
            Box::new(|_| None),
            Some(format!("{}/oauth2/token", server.uri())),
        )
        .unwrap();
        for _ in 0..3 {
            let out = s.resolve("twitch", Some(1), &["app.token"]).await.unwrap();
            assert_eq!(out["app.token"].expose(), "apptok-1");
        }
        // `expect(1)` is verified when the server drops.
        server.verify().await;
    }

    #[tokio::test]
    async fn a_failing_token_endpoint_is_unavailable_not_empty() {
        use wiremock::matchers::method;
        use wiremock::{Mock, MockServer, ResponseTemplate};

        // SAFETY: test-local.
        unsafe {
            std::env::set_var("TWITCH_CLIENT_ID", "host-cid");
            std::env::set_var("TWITCH_CLIENT_SECRET", "host-secret");
        }
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(500))
            .mount(&server)
            .await;
        let s = PlatformAppCredentials::new(
            Box::new(|_| None),
            Some(format!("{}/oauth2/token", server.uri())),
        )
        .unwrap();
        assert_eq!(
            s.resolve("twitch", None, &["app.token"]).await.unwrap_err(),
            SlotError::Unavailable("app_token_unavailable")
        );
    }

    #[tokio::test]
    async fn no_app_credentials_refuses_everything() {
        let n = NoAppCredentials;
        assert!(!n.supports("twitch"));
        assert_eq!(
            n.resolve("twitch", None, &["app.token"]).await.unwrap_err(),
            SlotError::Refused("slot_app_credential_unsupported")
        );
    }
}
