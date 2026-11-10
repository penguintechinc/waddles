//! Per-community YouTube outbound credential + live-chat binding (P6a).
//!
//! The YouTube analogue of [`crate::outbound_creds_discord`]: on EVERY call it
//! turns a numeric `community_id` plus the stamp-pinned live-chat id into the
//! community's own OAuth access token. Nothing is cached and nothing falls
//! back to another community's credential.
//!
//! **Binding contract** (non-secret metadata on the community's `youtube`
//! connection in hub-api; read from the RESOLVED record, never only the list):
//!
//! | key | value |
//! |---|---|
//! | `live_chat_id` | the live-chat id the community's connection is pinned to, `^[A-Za-z0-9_-]{1,128}$` |
//!
//! `channel_id` / `video_id` belong to the read path and are never served to
//! the outbound path. The OAuth access token is the kind-1 `accessToken`
//! (`ConnectionSecret::OAuthAccessToken`); any other secret scheme is
//! refused. A live-chat id that did not come from this resolved record (the
//! queue payload, the guest) is never trusted: the record must OWN the
//! stamp-pinned id, and no OTHER community may also claim it.
//!
//! **Expiry:** hub-api refreshes OAuth tokens when it resolves (as for
//! Twitch); the executor never refreshes. If the resolver nevertheless hands
//! back a token whose `expires_at` has passed (or is within [`EXPIRY_SKEW`]),
//! the send is refused with the typed, RETRYABLE [`CredError::TokenExpired`].
//!
//! Logs and errors carry fixed reason codes and numeric community ids only --
//! never a token, a chat id or message text.

use std::collections::BTreeMap;
use std::time::Duration;

use connection_resolver::{
    BindingSummary, ConnectionKind, ConnectionRef, ConnectionResolve, ConnectionSecret,
    SecretString,
};

use crate::outbound_creds::{map_resolve_error, record_resolve, CredError};
use crate::twitch_sessions::spec::{MAX_PAGES, PAGE_LIMIT};

/// Provider name as hub-api's binding list knows it.
const PROVIDER: &str = "youtube";
/// Metadata key: the pinned live-chat id.
pub const META_LIVE_CHAT_ID: &str = "live_chat_id";
/// A token expiring within this many seconds is treated as already expired.
pub const EXPIRY_SKEW: i64 = 30;
/// Bound on each hub-api call.
const CALL_TIMEOUT: Duration = Duration::from_secs(5);

/// What the host needs to post to one community's pinned live chat.
pub struct YoutubeBinding {
    /// The OAuth access token (zeroizing, never `Debug`-printed).
    pub oauth_token: SecretString,
    /// The live-chat id the connection is pinned to (equals the stamp pin).
    pub live_chat_id: String,
}

impl std::fmt::Debug for YoutubeBinding {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("YoutubeBinding")
            .field("oauth_token", &"[REDACTED]")
            .field("live_chat_id", &"<redacted>")
            .finish()
    }
}

/// Produces a community's [`YoutubeBinding`]; resolves on every call and
/// never falls back to another community's credential.
#[allow(async_fn_in_trait)] // service-binary-internal, as TwitchCredentialSource
pub trait YoutubeCredentialSource: Send + Sync {
    /// The binding for `community_id` that owns `live_chat_id` (`None` = the
    /// entry has no scope).
    async fn binding(
        &self,
        community_id: Option<i64>,
        live_chat_id: &str,
    ) -> Result<YoutubeBinding, CredError>;
}

/// `true` for a live-chat id `^[A-Za-z0-9_-]{1,128}$`.
#[must_use]
pub fn is_live_chat_id(s: &str) -> bool {
    (1..=128).contains(&s.len())
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

/// The live-chat id of a binding's metadata, if it is a valid one.
fn chat_id_of(meta: &BTreeMap<String, String>) -> Option<&str> {
    meta.get(META_LIVE_CHAT_ID)
        .map(String::as_str)
        .filter(|c| is_live_chat_id(c))
}

/// Whether another community's binding names `want` (valid or not -- an
/// unparseable claim to the id still contests it; fail closed).
fn claims_chat(b: &BindingSummary, want: &str) -> bool {
    b.provider == PROVIDER
        && b.metadata
            .get(META_LIVE_CHAT_ID)
            .is_some_and(|c| c.trim() == want)
}

/// Epoch seconds now. A clock before the epoch (or out of `i64` range) reads
/// as "far future", so every expiring token is refused: fail closed.
fn epoch_now() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(i64::MAX, |d| {
            i64::try_from(d.as_secs()).unwrap_or(i64::MAX)
        })
}

/// Production [`YoutubeCredentialSource`] over `connection_resolver`.
pub struct ResolverYoutubeCredentials<R: ConnectionResolve> {
    resolver: R,
    clock: fn() -> i64,
}

impl<R: ConnectionResolve> ResolverYoutubeCredentials<R> {
    /// Wraps a resolver (`Arc<ConnectionResolver>` in production).
    #[must_use]
    pub fn new(resolver: R) -> Self {
        Self {
            resolver,
            clock: epoch_now,
        }
    }

    /// Test seam: an explicit epoch-seconds clock.
    #[must_use]
    pub fn with_clock(resolver: R, clock: fn() -> i64) -> Self {
        Self { resolver, clock }
    }

    /// Pages the youtube binding list (non-secret metadata) to completion; a
    /// page failure aborts rather than yielding a silently shorter list.
    async fn list_all(&self) -> Result<Vec<BindingSummary>, CredError> {
        let mut out = Vec::new();
        let mut after = 0i64;
        for _ in 0..MAX_PAGES {
            let page = tokio::time::timeout(
                CALL_TIMEOUT,
                self.resolver
                    .list_bindings(PROVIDER, None, after, PAGE_LIMIT),
            )
            .await
            .map_err(|_| CredError::Unavailable)?
            .map_err(|e| map_resolve_error(&e))?;
            out.extend(page.items);
            match page.next_after {
                Some(next) if next > after => after = next,
                Some(_) => return Err(CredError::Unavailable),
                None => return Ok(out),
            }
        }
        Err(CredError::BindingListOverflow)
    }

    async fn binding_inner(
        &self,
        community_id: Option<i64>,
        live_chat_id: &str,
    ) -> Result<YoutubeBinding, CredError> {
        let community_id = community_id
            .filter(|c| *c > 0)
            .ok_or(CredError::NoCommunityScope)?;
        if !is_live_chat_id(live_chat_id) {
            return Err(CredError::ChannelNotOwned);
        }
        let mut mine: Vec<i64> = Vec::new();
        let mut community_has_binding = false;
        let mut other_claimant = false;
        for b in self.list_all().await? {
            if b.community_id == community_id && b.provider == PROVIDER {
                community_has_binding = true;
                if chat_id_of(&b.metadata) == Some(live_chat_id) {
                    mine.push(b.connection_id);
                }
            } else if claims_chat(&b, live_chat_id) {
                other_claimant = true;
            }
        }
        if !community_has_binding {
            return Err(CredError::NoBinding);
        }
        if mine.is_empty() || other_claimant {
            return Err(CredError::ChannelNotOwned);
        }
        mine.sort_unstable();
        let connection_id = mine.swap_remove(0);
        let resolved = tokio::time::timeout(
            CALL_TIMEOUT,
            self.resolver.resolve(ConnectionRef {
                community_id,
                connection_id,
            }),
        )
        .await
        .map_err(|_| CredError::Unavailable)?
        .map_err(|e| map_resolve_error(&e))?;
        // Defense in depth: the answer must be exactly what was asked for.
        if resolved.community_id != community_id
            || resolved.connection_id != connection_id
            || resolved.provider != PROVIDER
            || resolved.kind != ConnectionKind::PlatformOauth
        {
            return Err(CredError::NotUsable);
        }
        // Metadata can be rewritten between list and resolve: the resolved
        // record's own chat id is the authority.
        match chat_id_of(&resolved.metadata) {
            Some(c) if c == live_chat_id => {}
            Some(_) => return Err(CredError::ChannelNotOwned),
            None => return Err(CredError::NotUsable),
        }
        if let Some(exp) = resolved.expires_at {
            if exp <= (self.clock)().saturating_add(EXPIRY_SKEW) {
                return Err(CredError::TokenExpired);
            }
        }
        let ConnectionSecret::OAuthAccessToken { access_token } = resolved.secret else {
            return Err(CredError::NotUsable);
        };
        Ok(YoutubeBinding {
            oauth_token: access_token,
            live_chat_id: live_chat_id.to_string(),
        })
    }
}

impl<R: ConnectionResolve> YoutubeCredentialSource for ResolverYoutubeCredentials<R> {
    async fn binding(
        &self,
        community_id: Option<i64>,
        live_chat_id: &str,
    ) -> Result<YoutubeBinding, CredError> {
        let result = self.binding_inner(community_id, live_chat_id).await;
        let outcome = match &result {
            Ok(_) => "ok",
            Err(e) => e.reason(),
        };
        record_resolve("youtube", outcome);
        match &result {
            Ok(_) => tracing::debug!(
                platform = "youtube",
                community_id = ?community_id,
                "outbound credential resolved"
            ),
            Err(e) => tracing::warn!(
                platform = "youtube",
                community_id = ?community_id,
                reason = e.reason(),
                "outbound credential unavailable; send refused (no fallback credential)"
            ),
        }
        result
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use std::sync::atomic::{AtomicUsize, Ordering};

    use connection_resolver::{
        BindingPage, Endpoint, ResolveError, ResolvedConnection, SecretString,
    };

    use super::*;

    const TOKEN: &str = "YT-SECRET-TOKEN";
    const CHAT: &str = "Cg0KC-live_ID";
    const NOW: i64 = 1_000_000;

    fn meta(chat: &str) -> BTreeMap<String, String> {
        [(META_LIVE_CHAT_ID.to_string(), chat.to_string())].into()
    }

    fn summary(community: i64, conn: i64, m: BTreeMap<String, String>) -> BindingSummary {
        BindingSummary {
            tenant_id: 1,
            secret_version: 1,
            community_id: community,
            connection_id: conn,
            provider: PROVIDER.to_string(),
            kind: ConnectionKind::PlatformOauth,
            metadata: m,
        }
    }

    struct Fake {
        bindings: Vec<BindingSummary>,
        resolves: AtomicUsize,
        answer: Option<(i64, i64, &'static str)>,
        kind: ConnectionKind,
        meta_override: Option<BTreeMap<String, String>>,
        expires_at: Option<i64>,
        non_oauth: bool,
        list_err: Option<ResolveError>,
        pages: bool,
    }

    impl Fake {
        fn new(bindings: Vec<BindingSummary>) -> Self {
            Self {
                bindings,
                resolves: AtomicUsize::new(0),
                answer: None,
                kind: ConnectionKind::PlatformOauth,
                meta_override: None,
                expires_at: None,
                non_oauth: false,
                list_err: None,
                pages: false,
            }
        }
    }

    impl ConnectionResolve for Fake {
        async fn resolve(&self, r: ConnectionRef) -> Result<ResolvedConnection, ResolveError> {
            self.resolves.fetch_add(1, Ordering::SeqCst);
            let (c, k, p) = self
                .answer
                .unwrap_or((r.community_id, r.connection_id, PROVIDER));
            let metadata = self.meta_override.clone().unwrap_or_else(|| {
                self.bindings
                    .iter()
                    .find(|b| b.connection_id == r.connection_id)
                    .map(|b| b.metadata.clone())
                    .expect("resolve is only called for listed connections")
            });
            Ok(ResolvedConnection {
                connection_id: k,
                community_id: c,
                provider: p.to_string(),
                kind: self.kind,
                endpoint: Endpoint::default(),
                secret: if self.non_oauth {
                    ConnectionSecret::Bearer {
                        token: SecretString::new(TOKEN.to_string()),
                    }
                } else {
                    ConnectionSecret::OAuthAccessToken {
                        access_token: SecretString::new(TOKEN.to_string()),
                    }
                },
                expires_at: self.expires_at,
                metadata,
            })
        }

        async fn list_bindings(
            &self,
            _p: &str,
            _c: Option<&str>,
            after: i64,
            _limit: u32,
        ) -> Result<BindingPage, ResolveError> {
            if let Some(e) = &self.list_err {
                return Err(match e {
                    ResolveError::Auth => ResolveError::Auth,
                    _ => ResolveError::Transient("x"),
                });
            }
            if self.pages {
                // A cursor that never advances.
                return Ok(BindingPage {
                    items: vec![],
                    next_after: Some(after),
                });
            }
            Ok(BindingPage {
                items: self.bindings.clone(),
                next_after: None,
            })
        }
    }

    fn src(f: Fake) -> ResolverYoutubeCredentials<Fake> {
        ResolverYoutubeCredentials::with_clock(f, || NOW)
    }

    #[tokio::test]
    async fn resolves_the_communitys_own_binding_uncached() {
        let s = src(Fake::new(vec![
            summary(1, 10, meta(CHAT)),
            summary(2, 20, meta("OtherChat")),
        ]));
        let b = s.binding(Some(1), CHAT).await.unwrap();
        assert_eq!(b.oauth_token.expose(), TOKEN);
        assert_eq!(b.live_chat_id, CHAT);
        let dbg = format!("{b:?}");
        assert!(!dbg.contains(TOKEN) && !dbg.contains(CHAT));
        s.binding(Some(1), CHAT).await.unwrap();
        assert_eq!(s.resolver.resolves.load(Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn refuses_without_scope_binding_or_ownership() {
        let s = src(Fake::new(vec![summary(1, 10, meta(CHAT))]));
        assert_eq!(
            s.binding(None, CHAT).await.unwrap_err(),
            CredError::NoCommunityScope
        );
        assert_eq!(
            s.binding(Some(0), CHAT).await.unwrap_err(),
            CredError::NoCommunityScope
        );
        assert_eq!(
            s.binding(Some(-5), CHAT).await.unwrap_err(),
            CredError::NoCommunityScope
        );
        // Wrong community: no binding at all.
        assert_eq!(
            s.binding(Some(3), CHAT).await.unwrap_err(),
            CredError::NoBinding
        );
        // Own binding, different chat: not owned.
        assert_eq!(
            s.binding(Some(1), "Another").await.unwrap_err(),
            CredError::ChannelNotOwned
        );
        // Hostile pin never reaches the resolver.
        assert_eq!(
            s.binding(Some(1), "a&b=c").await.unwrap_err(),
            CredError::ChannelNotOwned
        );
        assert_eq!(s.resolver.resolves.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn a_chat_claimed_by_another_community_is_contested() {
        let s = src(Fake::new(vec![
            summary(1, 10, meta(CHAT)),
            summary(2, 20, meta(CHAT)),
        ]));
        assert_eq!(
            s.binding(Some(1), CHAT).await.unwrap_err(),
            CredError::ChannelNotOwned
        );
        assert_eq!(s.resolver.resolves.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn the_lowest_connection_id_wins_deterministically() {
        let s = src(Fake::new(vec![
            summary(1, 12, meta(CHAT)),
            summary(1, 11, meta(CHAT)),
        ]));
        assert!(s.binding(Some(1), CHAT).await.is_ok());
    }

    #[tokio::test]
    async fn refuses_a_resolve_answer_for_another_record() {
        for answer in [(2, 10, PROVIDER), (1, 99, PROVIDER), (1, 10, "twitch")] {
            let mut f = Fake::new(vec![summary(1, 10, meta(CHAT))]);
            f.answer = Some(answer);
            assert_eq!(
                src(f).binding(Some(1), CHAT).await.unwrap_err(),
                CredError::NotUsable
            );
        }
        let mut f = Fake::new(vec![summary(1, 10, meta(CHAT))]);
        f.kind = ConnectionKind::ExternalHttp;
        assert_eq!(
            src(f).binding(Some(1), CHAT).await.unwrap_err(),
            CredError::NotUsable
        );
    }

    #[tokio::test]
    async fn the_resolved_records_metadata_is_the_authority() {
        // Rewritten between list and resolve to another chat: not owned.
        let mut f = Fake::new(vec![summary(1, 10, meta(CHAT))]);
        f.meta_override = Some(meta("Rewritten"));
        assert_eq!(
            src(f).binding(Some(1), CHAT).await.unwrap_err(),
            CredError::ChannelNotOwned
        );
        // Metadata stripped / invalid at resolve: not usable.
        for m in [BTreeMap::new(), meta("bad id")] {
            let mut f = Fake::new(vec![summary(1, 10, meta(CHAT))]);
            f.meta_override = Some(m);
            assert_eq!(
                src(f).binding(Some(1), CHAT).await.unwrap_err(),
                CredError::NotUsable
            );
        }
    }

    #[tokio::test]
    async fn an_expired_or_nearly_expired_token_is_a_typed_retryable_refusal() {
        for exp in [NOW - 1, NOW, NOW + EXPIRY_SKEW] {
            let mut f = Fake::new(vec![summary(1, 10, meta(CHAT))]);
            f.expires_at = Some(exp);
            assert_eq!(
                src(f).binding(Some(1), CHAT).await.unwrap_err(),
                CredError::TokenExpired
            );
        }
        let mut f = Fake::new(vec![summary(1, 10, meta(CHAT))]);
        f.expires_at = Some(NOW + EXPIRY_SKEW + 1);
        assert!(src(f).binding(Some(1), CHAT).await.is_ok());
        assert_eq!(CredError::TokenExpired.reason(), "token_expired");
    }

    #[tokio::test]
    async fn a_non_oauth_secret_scheme_is_refused() {
        let mut f = Fake::new(vec![summary(1, 10, meta(CHAT))]);
        f.non_oauth = true;
        assert_eq!(
            src(f).binding(Some(1), CHAT).await.unwrap_err(),
            CredError::NotUsable
        );
    }

    #[tokio::test]
    async fn list_failures_are_typed_and_never_a_credential() {
        let mut f = Fake::new(vec![]);
        f.list_err = Some(ResolveError::Auth);
        assert_eq!(
            src(f).binding(Some(1), CHAT).await.unwrap_err(),
            CredError::NotUsable
        );
        let mut f = Fake::new(vec![]);
        f.list_err = Some(ResolveError::Transient("x"));
        assert_eq!(
            src(f).binding(Some(1), CHAT).await.unwrap_err(),
            CredError::Unavailable
        );
        let mut f = Fake::new(vec![]);
        f.pages = true;
        assert_eq!(
            src(f).binding(Some(1), CHAT).await.unwrap_err(),
            CredError::Unavailable
        );
    }

    #[test]
    fn chat_id_charset() {
        assert!(is_live_chat_id("Cg0KC-live_ID") && is_live_chat_id(&"a".repeat(128)));
        for bad in [
            "",
            "a b",
            "a&b",
            "a=b",
            "a/b",
            "a%20",
            "é",
            &"a".repeat(129),
        ] {
            assert!(!is_live_chat_id(bad), "{bad}");
        }
    }

    /// The hub-api wire shapes (list `items[].metadata`, resolve
    /// `accessToken` kind 1 + RFC 3339 `expiresAt`; NOW = 1970-01-12T13:46:40Z) through the REAL
    /// `ConnectionResolver` over a loopback hub.
    mod real_resolver_shape {
        use std::sync::Arc;
        use std::time::Duration;

        use connection_resolver::{
            ConnectionKind, ConnectionResolver, ResolverConfig, ResolverMetrics,
        };
        use wiremock::matchers::{method, path};
        use wiremock::{Mock, MockServer, ResponseTemplate};

        use super::super::*;
        use super::{CHAT, NOW, TOKEN};

        struct NoMetrics;
        impl ResolverMetrics for NoMetrics {
            fn resolve(&self, _: ConnectionKind, _: &'static str, _: Duration) {}
            fn list(&self, _: &'static str) {}
        }

        struct TestSa(std::path::PathBuf);
        impl Drop for TestSa {
            fn drop(&mut self) {
                if let Err(e) = std::fs::remove_file(&self.0) {
                    eprintln!("synthetic sa token cleanup failed: {e}");
                }
            }
        }

        async fn hub(
            resolve_body: serde_json::Value,
        ) -> (
            MockServer,
            ResolverYoutubeCredentials<ConnectionResolver>,
            TestSa,
        ) {
            let server = MockServer::start().await;
            // The machine-JWT mint is gRPC: a real in-process `ServiceAuthService`.
            let mint_endpoint = service_auth::test_support::MockHub::builder()
                .mint(vec![service_auth::test_support::MintReply::token(
                    "machine-tok",
                    3600,
                )])
                .start_detached()
                .await;
            Mock::given(method("GET"))
                .and(path("/api/v1/internal/provider-connections"))
                .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                    "success": true,
                    "items": [{"communityId": 7, "connectionId": 12, "tenantId": 3, "secretVersion": 1, "provider": "youtube",
                               "kind": 1, "metadata": {"live_chat_id": CHAT}}],
                    "nextAfter": null
                })))
                .mount(&server)
                .await;
            Mock::given(method("POST"))
                .and(path(
                    "/api/v1/internal/communities/7/provider-connections/12/resolve",
                ))
                .respond_with(ResponseTemplate::new(200).set_body_json(resolve_body))
                .mount(&server)
                .await;
            let sa = std::env::temp_dir().join(format!("yt-sa-{}", uuid::Uuid::new_v4()));
            std::fs::write(&sa, "synthetic-sa").unwrap();
            let resolver = ConnectionResolver::new(
                ResolverConfig::new(
                    server.uri(),
                    "alpha",
                    mint_endpoint,
                    sa.to_string_lossy().to_string(),
                ),
                Arc::new(NoMetrics),
            )
            .unwrap();
            (
                server,
                ResolverYoutubeCredentials::with_clock(resolver, || NOW),
                TestSa(sa),
            )
        }

        fn served(expires_at: serde_json::Value) -> serde_json::Value {
            serde_json::json!({
                "communityId": 7, "connectionId": 12, "provider": "youtube",
                "kind": 1, "accessToken": TOKEN, "expiresAt": expires_at,
                "metadata": {"live_chat_id": CHAT}
            })
        }

        #[tokio::test]
        async fn a_hub_served_youtube_binding_resolves() {
            let (_s, creds, _sa) = hub(served(serde_json::json!("1970-01-12T14:46:40Z"))).await;
            let b = creds.binding(Some(7), CHAT).await.unwrap();
            assert_eq!(b.oauth_token.expose(), TOKEN);
            assert_eq!(b.live_chat_id, CHAT);
            // Another community never sees it.
            assert_eq!(
                creds.binding(Some(8), CHAT).await.unwrap_err(),
                CredError::NoBinding
            );
        }

        #[tokio::test]
        async fn a_hub_served_expired_token_is_refused_retryably() {
            let (_s, creds, _sa) = hub(served(serde_json::json!("1970-01-12T13:46:35Z"))).await;
            assert_eq!(
                creds.binding(Some(7), CHAT).await.unwrap_err(),
                CredError::TokenExpired
            );
        }
    }
}
