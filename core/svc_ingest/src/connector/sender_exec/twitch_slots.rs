//! The Twitch [`SlotSource`]: turns the per-community credential seam
//! (`crate::outbound_creds`, P6a T6b) into the four Helix slots the overlay
//! templates carry (`token`, `client_id`, `broadcaster_id`, `sender_id`).
//!
//! * `token` -- the community's own OAuth access token, resolved by
//!   [`TwitchCredentialSource::credential`] on EVERY call (that call also
//!   proves the community owns the stamp-pinned channel and refuses a
//!   channel another community claims). Never cached, never logged.
//! * `client_id` -- the bot application's public client id (host config).
//! * `broadcaster_id` / `sender_id` -- numeric Twitch user ids of the pinned
//!   channel and of the resolved bot login. Helix addresses users by numeric
//!   id; the connection record carries logins, so ONE guarded
//!   `GET /helix/users?login=<channel>&login=<bot>` per send maps them. Not
//!   cached: a login can be renamed and re-registered to a different user, so
//!   a cached id could misdirect a moderation action. The ids come only from
//!   Twitch's answer for the logins the HOST derived -- never from the guest
//!   or the queue payload.

use std::collections::HashMap;

use bundle_host_http::guarded::{GuardedClient, GuardedLimits};
use serde::Deserialize;

use super::subst::SlotValue;
use super::{SlotError, SlotRequest, SlotSource};
use crate::outbound_creds::{CredError, TwitchCredentialSource};

/// Helix users endpoint.
const USERS_URL: &str = "https://api.twitch.tv/helix/users";

#[derive(Deserialize)]
struct Users {
    data: Vec<User>,
}

#[derive(Deserialize)]
struct User {
    id: String,
    login: String,
}

/// Per-community Twitch slot resolution over a [`TwitchCredentialSource`].
pub struct TwitchSlots<C: TwitchCredentialSource> {
    creds: C,
    client_id: String,
    client: GuardedClient,
    users_url: String,
}

impl<C: TwitchCredentialSource> TwitchSlots<C> {
    /// Production wiring: the real Helix users endpoint through a default
    /// (empty-allowlist) guarded client.
    #[must_use]
    pub fn new(creds: C, client_id: String) -> Self {
        Self::with_endpoint(
            creds,
            client_id,
            GuardedClient::new(GuardedLimits::default(), Vec::new()),
            USERS_URL.to_string(),
        )
    }

    /// Explicit endpoint + client (tests point it at a local fake).
    #[must_use]
    pub fn with_endpoint(
        creds: C,
        client_id: String,
        client: GuardedClient,
        users_url: String,
    ) -> Self {
        Self {
            creds,
            client_id,
            client,
            users_url,
        }
    }

    /// Maps the two logins to numeric ids with one guarded GET.
    async fn user_ids(
        &self,
        token: &str,
        channel_login: &str,
        bot_login: &str,
    ) -> Result<(String, String), SlotError> {
        let mut url = reqwest::Url::parse(&self.users_url)
            .map_err(|_| SlotError::Unavailable("twitch_users_lookup_failed"))?;
        url.query_pairs_mut()
            .append_pair("login", channel_login)
            .append_pair("login", bot_login);
        let resp = self
            .client
            .fetch(
                reqwest::Method::GET,
                url.as_str(),
                vec![
                    ("Authorization".to_string(), format!("Bearer {token}")),
                    ("Client-Id".to_string(), self.client_id.clone()),
                ],
                None,
            )
            .await
            .map_err(|_| SlotError::Unavailable("twitch_users_lookup_failed"))?;
        match resp.status {
            200..=299 => {}
            401 | 403 => return Err(SlotError::Refused("connection_not_usable")),
            _ => return Err(SlotError::Unavailable("twitch_users_lookup_failed")),
        }
        let users: Users = serde_json::from_slice(&resp.body)
            .map_err(|_| SlotError::Unavailable("twitch_users_lookup_failed"))?;
        let find = |login: &str| {
            users
                .data
                .iter()
                .find(|u| u.login.eq_ignore_ascii_case(login))
                .map(|u| u.id.clone())
        };
        match (find(channel_login), find(bot_login)) {
            (Some(b), Some(s)) => Ok((b, s)),
            _ => Err(SlotError::Refused("twitch_user_not_found")),
        }
    }
}

/// `#Login` -> `login` (the stamp already canonicalized the channel).
fn login_of(channel: &str) -> String {
    channel.trim_start_matches('#').to_ascii_lowercase()
}

pub(crate) fn map_cred(e: CredError) -> SlotError {
    match e {
        CredError::Unavailable | CredError::TokenExpired => SlotError::Unavailable(e.reason()),
        other => SlotError::Refused(other.reason()),
    }
}

impl<C: TwitchCredentialSource> SlotSource for TwitchSlots<C> {
    async fn resolve(
        &self,
        req: &SlotRequest<'_>,
    ) -> Result<HashMap<String, SlotValue>, SlotError> {
        if req.platform != "twitch" {
            return Err(SlotError::Refused("unsupported_platform"));
        }
        let channel = req
            .pinned_channel
            .ok_or(SlotError::Refused("no_origin_pin"))?;
        // Resolved fresh on every send; also asserts channel ownership.
        let cred = self
            .creds
            .credential(req.community_id, channel)
            .await
            .map_err(map_cred)?;
        let mut out = HashMap::new();
        for h in req.handles {
            match *h {
                "token" | "client_id" | "broadcaster_id" | "sender_id" => {}
                _ => return Err(SlotError::Refused("unknown_handle")),
            }
        }
        if req.handles.contains(&"broadcaster_id") || req.handles.contains(&"sender_id") {
            let (broadcaster, sender) = self
                .user_ids(
                    cred.oauth_token.expose(),
                    &login_of(channel),
                    &cred.nick.to_ascii_lowercase(),
                )
                .await?;
            out.insert("broadcaster_id".to_string(), SlotValue::new(broadcaster));
            out.insert("sender_id".to_string(), SlotValue::new(sender));
        }
        out.insert(
            "token".to_string(),
            SlotValue::new(cred.oauth_token.expose().to_owned()),
        );
        out.insert(
            "client_id".to_string(),
            SlotValue::new(self.client_id.clone()),
        );
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use std::net::{IpAddr, Ipv4Addr};
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;
    use std::time::Duration;

    use connection_resolver::SecretString;
    use wiremock::matchers::{header, method, path, query_param};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    use super::*;
    use crate::outbound_creds::TwitchCredential;

    struct FakeCreds {
        result: Result<(), CredError>,
        calls: Arc<AtomicUsize>,
    }

    impl TwitchCredentialSource for FakeCreds {
        async fn credential(
            &self,
            community_id: Option<i64>,
            channel: &str,
        ) -> Result<TwitchCredential, CredError> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            assert_eq!(community_id, Some(7));
            assert_eq!(channel, "#Chan");
            self.result?;
            Ok(TwitchCredential {
                nick: "BotNick".to_string(),
                oauth_token: SecretString::new("tok-123".to_string()),
            })
        }
    }

    fn slots(
        server: &MockServer,
        result: Result<(), CredError>,
    ) -> (TwitchSlots<FakeCreds>, Arc<AtomicUsize>) {
        let calls = Arc::new(AtomicUsize::new(0));
        let client = GuardedClient::new(
            GuardedLimits {
                max_redirects: 0,
                timeout: Duration::from_secs(5),
                max_response_bytes: 4096,
            },
            vec![IpAddr::V4(Ipv4Addr::LOCALHOST)],
        );
        (
            TwitchSlots::with_endpoint(
                FakeCreds {
                    result,
                    calls: calls.clone(),
                },
                "cid".to_string(),
                client,
                format!("{}/helix/users", server.uri()),
            ),
            calls,
        )
    }

    fn req<'a>(handles: &'a [&'static str], channel: Option<&'a str>) -> SlotRequest<'a> {
        SlotRequest {
            platform: "twitch",
            community_id: Some(7),
            pinned_channel: channel,
            handles,
        }
    }

    const ALL: [&str; 4] = ["token", "client_id", "broadcaster_id", "sender_id"];

    #[tokio::test]
    async fn resolves_token_client_id_and_numeric_ids_with_one_guarded_lookup() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/helix/users"))
            .and(query_param("login", "chan"))
            .and(header("authorization", "Bearer tok-123"))
            .and(header("client-id", "cid"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "data": [{"id": "999", "login": "botnick"}, {"id": "111", "login": "Chan"}]
            })))
            .expect(2)
            .mount(&server)
            .await;
        let (s, calls) = slots(&server, Ok(()));
        let got = s.resolve(&req(&ALL, Some("#Chan"))).await.unwrap();
        assert_eq!(got["broadcaster_id"].expose(), "111");
        assert_eq!(got["sender_id"].expose(), "999");
        assert_eq!(got["token"].expose(), "tok-123");
        assert_eq!(got["client_id"].expose(), "cid");
        // Credential resolved once per resolve call; a second call resolves again.
        s.resolve(&req(&ALL, Some("#Chan"))).await.ok();
        assert_eq!(calls.load(Ordering::SeqCst), 2, "never cached");
    }

    #[tokio::test]
    async fn no_lookup_when_no_id_handle_is_requested() {
        let server = MockServer::start().await;
        let (s, _) = slots(&server, Ok(()));
        let got = s
            .resolve(&req(&["token", "client_id"], Some("#Chan")))
            .await
            .unwrap();
        assert_eq!(got.len(), 2);
        assert!(server.received_requests().await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn credential_refusals_are_typed_and_nothing_is_looked_up() {
        let server = MockServer::start().await;
        for (e, want, retry) in [
            (CredError::NoBinding, "no_connection", false),
            (CredError::ChannelNotOwned, "channel_not_owned", false),
            (CredError::NotUsable, "connection_not_usable", false),
            (CredError::NoCommunityScope, "no_community_scope", false),
            (
                CredError::BindingListOverflow,
                "binding_list_overflow",
                false,
            ),
            (CredError::Unavailable, "credential_unavailable", true),
        ] {
            let (s, _) = slots(&server, Err(e));
            let err = s.resolve(&req(&ALL, Some("#Chan"))).await.unwrap_err();
            let expect = if retry {
                SlotError::Unavailable(want)
            } else {
                SlotError::Refused(want)
            };
            assert_eq!(err, expect);
        }
        assert!(server.received_requests().await.unwrap().is_empty());
    }

    #[tokio::test]
    async fn lookup_failures_fail_closed_with_typed_codes() {
        for (status, body, want) in [
            (
                401u16,
                serde_json::json!({}),
                SlotError::Refused("connection_not_usable"),
            ),
            (
                403,
                serde_json::json!({}),
                SlotError::Refused("connection_not_usable"),
            ),
            (
                500,
                serde_json::json!({}),
                SlotError::Unavailable("twitch_users_lookup_failed"),
            ),
            (
                200,
                serde_json::json!({"nope": 1}),
                SlotError::Unavailable("twitch_users_lookup_failed"),
            ),
            // Only one of the two logins resolved: never guess the other id.
            (
                200,
                serde_json::json!({"data": [{"id": "1", "login": "chan"}]}),
                SlotError::Refused("twitch_user_not_found"),
            ),
            (
                200,
                serde_json::json!({"data": []}),
                SlotError::Refused("twitch_user_not_found"),
            ),
            // Right ids for the WRONG logins (renamed / swapped): refused.
            (
                200,
                serde_json::json!({"data": [{"id": "1", "login": "someone"}, {"id": "2", "login": "else"}]}),
                SlotError::Refused("twitch_user_not_found"),
            ),
        ] {
            let server = MockServer::start().await;
            Mock::given(wiremock::matchers::any())
                .respond_with(ResponseTemplate::new(status).set_body_json(body))
                .mount(&server)
                .await;
            let (s, _) = slots(&server, Ok(()));
            assert_eq!(
                s.resolve(&req(&ALL, Some("#Chan"))).await.unwrap_err(),
                want,
                "{status}"
            );
        }
    }

    #[tokio::test]
    async fn the_lookup_goes_through_the_guard_not_around_it() {
        // Production client (empty allowlist) + a loopback endpoint: refused.
        let server = MockServer::start().await;
        let calls = Arc::new(AtomicUsize::new(0));
        let s = TwitchSlots::with_endpoint(
            FakeCreds {
                result: Ok(()),
                calls,
            },
            "cid".into(),
            GuardedClient::new(GuardedLimits::default(), Vec::new()),
            format!("{}/helix/users", server.uri()),
        );
        let e = s.resolve(&req(&ALL, Some("#Chan"))).await.unwrap_err();
        assert_eq!(e, SlotError::Unavailable("twitch_users_lookup_failed"));
        assert!(server.received_requests().await.unwrap().is_empty());
        // Production constructor targets the real Helix host.
        let p = TwitchSlots::new(
            FakeCreds {
                result: Ok(()),
                calls: Arc::new(AtomicUsize::new(0)),
            },
            "cid".into(),
        );
        assert_eq!(p.users_url, "https://api.twitch.tv/helix/users");
    }

    #[tokio::test]
    async fn wrong_platform_missing_pin_and_unknown_handles_are_refused() {
        let server = MockServer::start().await;
        let (s, calls) = slots(&server, Ok(()));
        let mut r = req(&ALL, Some("#Chan"));
        r.platform = "discord";
        assert_eq!(
            s.resolve(&r).await.unwrap_err(),
            SlotError::Refused("unsupported_platform")
        );
        assert_eq!(
            s.resolve(&req(&ALL, None)).await.unwrap_err(),
            SlotError::Refused("no_origin_pin")
        );
        assert_eq!(
            s.resolve(&req(&["token", "discord-bot"], Some("#Chan")))
                .await
                .unwrap_err(),
            SlotError::Refused("unknown_handle")
        );
        // Unknown handle is refused only after the (ownership-proving) credential call.
        assert_eq!(calls.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn login_of_strips_hash_and_lowercases() {
        assert_eq!(login_of("#MiXed"), "mixed");
        assert_eq!(login_of("plain"), "plain");
    }
}
