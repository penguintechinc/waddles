//! Per-community Twitch outbound credentials (P6a Task 6, slice b).
//!
//! The outbound relay drain used to send every community's chat with ONE
//! process-wide bot identity (`TWITCH_IRC_NICK` / `TWITCH_IRC_OAUTH_TOKEN`).
//! Per the per-tenant bot credentials design
//! (`docs/superpowers/specs/2026-11-02-per-tenant-bot-credentials.md`) a
//! credential belongs to exactly one tenant/community, and a community with
//! no credential must never silently borrow another's. This module is the
//! seam: [`TwitchCredentialSource::credential`] turns a numeric
//! `community_id` + the (already host-pinned) channel into the bot identity
//! to send as.
//!
//! **Production source** ([`ResolverTwitchCredentials`]) -- on EVERY call:
//! 1. pages hub-api's twitch binding list (non-secret metadata only) and keeps
//!    the bindings of `community_id`;
//! 2. requires one of them to own `channel` (its `channel` metadata) and that
//!    no OTHER community also claims it (an ambiguous channel fails closed,
//!    same rule as inbound discovery);
//! 3. resolves exactly that connection through `connection_resolver`
//!    (community-bound machine JWT, `connections:resolve`) and asserts the
//!    answer is the same community/connection/provider/OAuth kind.
//!
//! The secret is returned as a [`connection_resolver::SecretString`]
//! (zeroizing, `Debug` redacted) and is **never cached**: the next send
//! resolves again. Keys are numeric ids only -- never a slug or name. Logs,
//! metrics and error values carry fixed reason codes and numeric ids; never a
//! token, a bot login, a channel name or message text.
//!
//! **Failure is loud and typed** ([`CredError`]): no community scope, no
//! binding, channel not owned, connection not usable, hub-api unavailable.
//! There is no fallback to any other credential. The one other
//! [`TwitchCredentialSource`], [`StaticCredentialSource`], is the documented
//! legacy single-identity path selected ONLY at startup while the
//! `waddles.ingest-dynamic-connections` flag is OFF; it is never consulted by
//! the per-community path, and a per-community drain never holds one.

use std::sync::LazyLock;
use std::time::Duration;

use connection_resolver::{
    BindingSummary, ConnectionKind, ConnectionRef, ConnectionResolve, ConnectionSecret,
    ResolveError, SecretString,
};

use crate::twitch_sessions::spec::{
    SessionSpec, CAPABILITY, MAX_PAGES, META_BOT_LOGIN, PAGE_LIMIT,
};

/// Provider name as hub-api's binding list knows it.
const PROVIDER: &str = "twitch";

/// Bound on each hub-api call made while resolving one credential.
const CALL_TIMEOUT: Duration = Duration::from_secs(5);

/// A Twitch bot identity to send one chat message as. The nick is a username
/// (PII), so `Debug` redacts both fields.
pub struct TwitchCredential {
    /// IRC nick the token authenticates as.
    pub nick: String,
    /// OAuth access token (zeroizing, never `Debug`-printed).
    pub oauth_token: SecretString,
}

impl std::fmt::Debug for TwitchCredential {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("TwitchCredential")
            .field("nick", &"<redacted>")
            .field("oauth_token", &"[REDACTED]")
            .finish()
    }
}

/// Why no credential could be produced for a send. Each variant has a stable
/// machine [`CredError::reason`]; none carries a secret, login or channel.
#[derive(Clone, Copy, Debug, PartialEq, Eq, thiserror::Error)]
pub enum CredError {
    /// The queue entry carries no valid numeric `community_id` (absent,
    /// zero/negative, or a tenant-wide scope) -- never defaulted.
    #[error("outbound entry has no community scope")]
    NoCommunityScope,
    /// The community has no usable twitch connection binding.
    #[error("community has no twitch connection")]
    NoBinding,
    /// The community has twitch bindings but none owns the target channel, or
    /// another community also claims it.
    #[error("target channel is not owned by the community's connection")]
    ChannelNotOwned,
    /// hub-api says the connection exists but cannot be used (revoked,
    /// expired, wrong kind ...), or answered with a different connection than
    /// asked for.
    #[error("community twitch connection is not usable")]
    NotUsable,
    /// hub-api could not be reached / answered in time; retryable.
    #[error("credential resolution temporarily unavailable")]
    Unavailable,
}

impl CredError {
    /// Stable machine code: the `Rejected` reason / metric label.
    #[must_use]
    pub fn reason(self) -> &'static str {
        match self {
            Self::NoCommunityScope => "no_community_scope",
            Self::NoBinding => "no_connection",
            Self::ChannelNotOwned => "channel_not_owned",
            Self::NotUsable => "connection_not_usable",
            Self::Unavailable => "credential_unavailable",
        }
    }
}

static CRED_RESOLVE: LazyLock<prometheus::IntCounterVec> = LazyLock::new(|| {
    // The definition is a compile-time constant; construction cannot fail.
    #[allow(clippy::expect_used)]
    prometheus::IntCounterVec::new(
        prometheus::Opts::new(
            "svc_ingest_outbound_credential_resolve_total",
            "Per-community outbound credential resolutions, labeled by platform/outcome (ok or a CredError reason)",
        ),
        &["platform", "outcome"],
    )
    .expect("valid metric definition")
});

/// Registers the credential-resolution counter on the service registry.
///
/// # Errors
/// Propagates a duplicate-registration error from `prometheus`.
pub fn register_metrics(registry: &prometheus::Registry) -> Result<(), prometheus::Error> {
    registry.register(Box::new(CRED_RESOLVE.clone()))
}

/// Count of one outcome (test/observability helper).
#[must_use]
pub fn resolve_count(platform: &str, outcome: &str) -> u64 {
    CRED_RESOLVE.with_label_values(&[platform, outcome]).get()
}

/// Produces the bot identity a send is made as. Implementations must resolve
/// on every call (no secret caching) and must never fall back to a credential
/// that is not the community's own.
#[allow(async_fn_in_trait)] // service-binary-internal trait, see outbound.rs::IrcOutbound's doc
pub trait TwitchCredentialSource: Send + Sync {
    /// The credential to send to `channel` (a canonical `#login`) on behalf of
    /// `community_id` (numeric `communities.id`, `None` when the entry has no
    /// scope).
    async fn credential(
        &self,
        community_id: Option<i64>,
        channel: &str,
    ) -> Result<TwitchCredential, CredError>;
}

/// Canonical Twitch login for comparison: drop a leading `#`, lowercase.
fn canonical_login(raw: &str) -> String {
    raw.trim().trim_start_matches('#').to_ascii_lowercase()
}

/// Production [`TwitchCredentialSource`] over `connection_resolver`; see the
/// module doc for the per-call sequence and the no-cache guarantee.
pub struct ResolverTwitchCredentials<R: ConnectionResolve> {
    resolver: R,
}

impl<R: ConnectionResolve> ResolverTwitchCredentials<R> {
    /// Wraps a resolver (`Arc<ConnectionResolver>` in production).
    #[must_use]
    pub fn new(resolver: R) -> Self {
        Self { resolver }
    }

    /// Pages the twitch binding list (non-secret metadata) to completion.
    /// Any page failure aborts: a silently shorter list could hide a
    /// competing claimant of the channel.
    async fn list_all(&self) -> Result<Vec<BindingSummary>, CredError> {
        let mut out = Vec::new();
        let mut after = 0i64;
        for _ in 0..MAX_PAGES {
            let page = tokio::time::timeout(
                CALL_TIMEOUT,
                self.resolver
                    .list_bindings(PROVIDER, Some(CAPABILITY), after, PAGE_LIMIT),
            )
            .await
            .map_err(|_| CredError::Unavailable)?
            .map_err(|e| map_resolve_error(&e))?;
            out.extend(page.items);
            match page.next_after {
                Some(next) if next > after => after = next,
                // A cursor that does not advance would loop forever.
                Some(_) => return Err(CredError::Unavailable),
                None => return Ok(out),
            }
        }
        Err(CredError::Unavailable)
    }

    async fn resolve_inner(
        &self,
        community_id: Option<i64>,
        channel: &str,
    ) -> Result<TwitchCredential, CredError> {
        let community_id = community_id
            .filter(|c| *c > 0)
            .ok_or(CredError::NoCommunityScope)?;
        let want = canonical_login(channel);

        // (community, connection, nick) of bindings claiming `want`.
        let mut mine: Vec<(i64, String)> = Vec::new();
        let mut community_has_binding = false;
        let mut other_claimant = false;
        for b in self.list_all().await? {
            let Ok(spec) = SessionSpec::from_summary(&b) else {
                continue; // malformed binding: never a candidate
            };
            if b.community_id == community_id {
                community_has_binding = true;
                if spec.channel == want {
                    mine.push((b.connection_id, spec.bot_login));
                }
            } else if spec.channel == want {
                other_claimant = true;
            }
        }
        if !community_has_binding {
            return Err(CredError::NoBinding);
        }
        if mine.is_empty() || other_claimant {
            return Err(CredError::ChannelNotOwned);
        }
        // Deterministic when several connections of the community own it.
        mine.sort_by_key(|(id, _)| *id);
        let (connection_id, nick) = mine.swap_remove(0);

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
        let ConnectionSecret::OAuthAccessToken { access_token } = resolved.secret else {
            return Err(CredError::NotUsable);
        };
        // The nick came from the binding metadata, validated as a login.
        debug_assert!(
            !nick.is_empty(),
            "{META_BOT_LOGIN} validated by SessionSpec"
        );
        Ok(TwitchCredential {
            nick,
            oauth_token: access_token,
        })
    }
}

/// Maps a resolver failure to a [`CredError`] without losing retryability.
fn map_resolve_error(e: &ResolveError) -> CredError {
    match e {
        ResolveError::NotFound => CredError::NoBinding,
        ResolveError::NotUsable(_) | ResolveError::Auth | ResolveError::Malformed(_) => {
            CredError::NotUsable
        }
        ResolveError::Transient(_) | ResolveError::CircuitOpen => CredError::Unavailable,
    }
}

impl<R: ConnectionResolve> TwitchCredentialSource for ResolverTwitchCredentials<R> {
    async fn credential(
        &self,
        community_id: Option<i64>,
        channel: &str,
    ) -> Result<TwitchCredential, CredError> {
        let result = self.resolve_inner(community_id, channel).await;
        let outcome = match &result {
            Ok(_) => "ok",
            Err(e) => e.reason(),
        };
        CRED_RESOLVE.with_label_values(&["twitch", outcome]).inc();
        match &result {
            Ok(_) => tracing::debug!(
                platform = "twitch",
                community_id = ?community_id,
                "outbound credential resolved"
            ),
            Err(e) => tracing::warn!(
                platform = "twitch",
                community_id = ?community_id,
                reason = e.reason(),
                "outbound credential unavailable; send refused (no fallback credential)"
            ),
        }
        result
    }
}

/// The documented LEGACY single-identity source: one process-wide bot
/// (`TWITCH_IRC_NICK`/`TWITCH_IRC_OAUTH_TOKEN`) for every community. Selected
/// only at startup while `waddles.ingest-dynamic-connections` is OFF
/// (`crate::try_start_twitch_outbound`); never present in per-community mode,
/// so it can never be a fallback for it. It exists for the single-tenant
/// static-dev deployment only and is removed once the flag is retired.
pub struct StaticCredentialSource {
    nick: String,
    token: String,
}

impl StaticCredentialSource {
    /// Builds the legacy source from the configured identity.
    #[must_use]
    pub fn new(nick: String, token: String) -> Self {
        Self { nick, token }
    }
}

impl TwitchCredentialSource for StaticCredentialSource {
    async fn credential(
        &self,
        _community_id: Option<i64>,
        _channel: &str,
    ) -> Result<TwitchCredential, CredError> {
        Ok(TwitchCredential {
            nick: self.nick.clone(),
            oauth_token: SecretString::new(self.token.clone()),
        })
    }
}

/// Test helper shared with `outbound` tests: a binding row for `community`.
#[cfg(test)]
pub(crate) fn test_binding(
    community: i64,
    connection: i64,
    channel: &str,
    bot: &str,
) -> BindingSummary {
    let mut metadata = std::collections::BTreeMap::new();
    metadata.insert("channel".to_owned(), channel.to_owned());
    metadata.insert(META_BOT_LOGIN.to_owned(), bot.to_owned());
    BindingSummary {
        community_id: community,
        connection_id: connection,
        provider: PROVIDER.to_owned(),
        kind: ConnectionKind::PlatformOauth,
        metadata,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use connection_resolver::{BindingPage, Endpoint, NotUsableReason, ResolvedConnection};
    use std::collections::BTreeMap;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Mutex;

    /// Fake hub-api: fixed bindings, per-connection tokens, call counters.
    struct Fake {
        bindings: Vec<BindingSummary>,
        tokens: BTreeMap<i64, &'static str>,
        resolve_err: Option<ResolveError>,
        list_err: Option<ResolveError>,
        resolves: AtomicUsize,
        asked: Mutex<Vec<(i64, i64)>>,
        /// Answers with this (community, connection) instead of the asked one.
        lie: Option<(i64, i64)>,
        kind: ConnectionKind,
    }

    impl Fake {
        fn new(bindings: Vec<BindingSummary>, tokens: &[(i64, &'static str)]) -> Self {
            Self {
                bindings,
                tokens: tokens.iter().copied().collect(),
                resolve_err: None,
                list_err: None,
                resolves: AtomicUsize::new(0),
                asked: Mutex::new(vec![]),
                lie: None,
                kind: ConnectionKind::PlatformOauth,
            }
        }
    }

    impl ConnectionResolve for Fake {
        async fn resolve(&self, r: ConnectionRef) -> Result<ResolvedConnection, ResolveError> {
            self.resolves.fetch_add(1, Ordering::SeqCst);
            self.asked
                .lock()
                .unwrap()
                .push((r.community_id, r.connection_id));
            if let Some(e) = &self.resolve_err {
                return Err(e.clone());
            }
            let token = self
                .tokens
                .get(&r.connection_id)
                .ok_or(ResolveError::NotFound)?;
            let (community_id, connection_id) =
                self.lie.unwrap_or((r.community_id, r.connection_id));
            Ok(ResolvedConnection {
                connection_id,
                community_id,
                provider: "twitch".to_owned(),
                kind: self.kind,
                endpoint: Endpoint::default(),
                secret: ConnectionSecret::OAuthAccessToken {
                    access_token: SecretString::new((*token).to_owned()),
                },
                expires_at: None,
            })
        }
        async fn list_bindings(
            &self,
            _p: &str,
            _c: Option<&str>,
            _after: i64,
            _l: u32,
        ) -> Result<BindingPage, ResolveError> {
            if let Some(e) = &self.list_err {
                return Err(e.clone());
            }
            Ok(BindingPage {
                items: self.bindings.clone(),
                next_after: None,
            })
        }
    }

    fn two_communities() -> Fake {
        Fake::new(
            vec![
                test_binding(1, 10, "chan_a", "bot_a"),
                test_binding(2, 20, "chan_b", "bot_b"),
            ],
            &[(10, "tok_a"), (20, "tok_b")],
        )
    }

    /// regression: p6a-t6b -- community A's send resolves A's credential, never B's.
    #[tokio::test]
    async fn community_a_gets_a_credential_and_b_gets_b() {
        let src = ResolverTwitchCredentials::new(two_communities());
        let a = src.credential(Some(1), "#chan_a").await.unwrap();
        assert_eq!(a.nick, "bot_a");
        assert_eq!(a.oauth_token.expose(), "tok_a");
        let b = src.credential(Some(2), "#CHAN_B").await.unwrap();
        assert_eq!(b.nick, "bot_b");
        assert_eq!(b.oauth_token.expose(), "tok_b");
        assert_eq!(*src.resolver.asked.lock().unwrap(), vec![(1, 10), (2, 20)]);
    }

    /// regression: p6a-t6b -- one resolve per use, never cached.
    #[tokio::test]
    async fn every_call_resolves_again() {
        let src = ResolverTwitchCredentials::new(two_communities());
        for _ in 0..3 {
            src.credential(Some(1), "#chan_a").await.unwrap();
        }
        assert_eq!(src.resolver.resolves.load(Ordering::SeqCst), 3);
    }

    /// regression: p6a-t6b -- community A can never send as B's channel/credential.
    #[tokio::test]
    async fn community_cannot_use_another_communitys_channel() {
        let src = ResolverTwitchCredentials::new(two_communities());
        let err = src.credential(Some(1), "#chan_b").await.unwrap_err();
        assert_eq!(err, CredError::ChannelNotOwned);
        assert_eq!(
            src.resolver.resolves.load(Ordering::SeqCst),
            0,
            "no secret fetched"
        );
    }

    #[tokio::test]
    async fn contested_channel_fails_closed() {
        let f = Fake::new(
            vec![
                test_binding(1, 10, "shared", "bot_a"),
                test_binding(2, 20, "shared", "bot_b"),
            ],
            &[(10, "tok_a"), (20, "tok_b")],
        );
        let src = ResolverTwitchCredentials::new(f);
        assert_eq!(
            src.credential(Some(1), "#shared").await.unwrap_err(),
            CredError::ChannelNotOwned
        );
    }

    #[tokio::test]
    async fn missing_scope_is_typed_and_never_resolves() {
        let src = ResolverTwitchCredentials::new(two_communities());
        for c in [None, Some(0), Some(-4)] {
            assert_eq!(
                src.credential(c, "#chan_a").await.unwrap_err(),
                CredError::NoCommunityScope
            );
        }
        assert_eq!(src.resolver.resolves.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn community_without_binding_is_no_connection() {
        let src = ResolverTwitchCredentials::new(two_communities());
        assert_eq!(
            src.credential(Some(3), "#chan_a").await.unwrap_err(),
            CredError::NoBinding
        );
    }

    #[tokio::test]
    async fn malformed_bindings_are_never_candidates() {
        let mut bad = test_binding(1, 10, "chan_a", "bot_a");
        bad.metadata.remove(META_BOT_LOGIN);
        let src = ResolverTwitchCredentials::new(Fake::new(vec![bad], &[(10, "tok_a")]));
        assert_eq!(
            src.credential(Some(1), "#chan_a").await.unwrap_err(),
            CredError::NoBinding
        );
    }

    #[tokio::test]
    async fn resolver_failures_map_to_typed_errors_without_fallback() {
        let cases = [
            (ResolveError::NotFound, CredError::NoBinding),
            (
                ResolveError::NotUsable(NotUsableReason::from_wire("revoked")),
                CredError::NotUsable,
            ),
            (ResolveError::Auth, CredError::NotUsable),
            (ResolveError::Malformed("x"), CredError::NotUsable),
            (ResolveError::Transient("x"), CredError::Unavailable),
            (ResolveError::CircuitOpen, CredError::Unavailable),
        ];
        for (resolve_err, want) in cases {
            let mut f = two_communities();
            f.resolve_err = Some(resolve_err.clone());
            let src = ResolverTwitchCredentials::new(f);
            assert_eq!(
                src.credential(Some(1), "#chan_a").await.unwrap_err(),
                want,
                "{resolve_err:?}"
            );
            let mut f = two_communities();
            f.list_err = Some(resolve_err.clone());
            let src = ResolverTwitchCredentials::new(f);
            let want_list = if matches!(resolve_err, ResolveError::NotFound) {
                CredError::NoBinding
            } else {
                want
            };
            assert_eq!(
                src.credential(Some(1), "#chan_a").await.unwrap_err(),
                want_list,
                "list {resolve_err:?}"
            );
        }
    }

    /// regression: p6a-t6b -- a resolver answering with a different
    /// community's connection is refused, its token never used.
    #[tokio::test]
    async fn mismatched_resolver_answer_is_refused() {
        let mut f = two_communities();
        f.lie = Some((2, 20));
        let src = ResolverTwitchCredentials::new(f);
        assert_eq!(
            src.credential(Some(1), "#chan_a").await.unwrap_err(),
            CredError::NotUsable
        );
    }

    #[tokio::test]
    async fn non_oauth_kind_is_refused() {
        let mut f = two_communities();
        f.kind = ConnectionKind::ExternalHttp;
        let src = ResolverTwitchCredentials::new(f);
        assert_eq!(
            src.credential(Some(1), "#chan_a").await.unwrap_err(),
            CredError::NotUsable
        );
    }

    #[tokio::test]
    async fn non_advancing_cursor_is_unavailable_not_a_loop() {
        struct Stuck;
        impl ConnectionResolve for Stuck {
            async fn resolve(&self, _r: ConnectionRef) -> Result<ResolvedConnection, ResolveError> {
                Err(ResolveError::NotFound)
            }
            async fn list_bindings(
                &self,
                _p: &str,
                _c: Option<&str>,
                after: i64,
                _l: u32,
            ) -> Result<BindingPage, ResolveError> {
                Ok(BindingPage {
                    items: vec![],
                    next_after: Some(after),
                })
            }
        }
        let src = ResolverTwitchCredentials::new(Stuck);
        assert_eq!(
            src.credential(Some(1), "#x").await.unwrap_err(),
            CredError::Unavailable
        );
    }

    #[tokio::test]
    async fn pages_are_followed_to_completion() {
        struct Paged;
        impl ConnectionResolve for Paged {
            async fn resolve(&self, r: ConnectionRef) -> Result<ResolvedConnection, ResolveError> {
                Ok(ResolvedConnection {
                    connection_id: r.connection_id,
                    community_id: r.community_id,
                    provider: "twitch".to_owned(),
                    kind: ConnectionKind::PlatformOauth,
                    endpoint: Endpoint::default(),
                    secret: ConnectionSecret::OAuthAccessToken {
                        access_token: SecretString::new("t".to_owned()),
                    },
                    expires_at: None,
                })
            }
            async fn list_bindings(
                &self,
                _p: &str,
                _c: Option<&str>,
                after: i64,
                _l: u32,
            ) -> Result<BindingPage, ResolveError> {
                Ok(if after == 0 {
                    BindingPage {
                        items: vec![test_binding(1, 10, "chan_a", "bot_a")],
                        next_after: Some(5),
                    }
                } else {
                    // A competing claimant only on page 2 must still be seen.
                    BindingPage {
                        items: vec![test_binding(2, 20, "chan_a", "bot_b")],
                        next_after: None,
                    }
                })
            }
        }
        let src = ResolverTwitchCredentials::new(Paged);
        assert_eq!(
            src.credential(Some(1), "#chan_a").await.unwrap_err(),
            CredError::ChannelNotOwned
        );
    }

    #[test]
    fn debug_never_prints_secret_or_login() {
        let c = TwitchCredential {
            nick: "bot_a".to_owned(),
            oauth_token: SecretString::new("tok_a".to_owned()),
        };
        let s = format!("{c:?}");
        assert!(!s.contains("tok_a") && !s.contains("bot_a"), "{s}");
    }

    #[test]
    fn reasons_are_stable_and_distinct() {
        let all = [
            CredError::NoCommunityScope,
            CredError::NoBinding,
            CredError::ChannelNotOwned,
            CredError::NotUsable,
            CredError::Unavailable,
        ];
        let mut reasons: Vec<_> = all.iter().map(|e| e.reason()).collect();
        reasons.sort_unstable();
        reasons.dedup();
        assert_eq!(reasons.len(), all.len());
    }

    #[tokio::test]
    async fn static_source_is_the_documented_single_identity() {
        let s = StaticCredentialSource::new("legacy".to_owned(), "tok".to_owned());
        let c = s.credential(None, "#x").await.unwrap();
        assert_eq!((c.nick.as_str(), c.oauth_token.expose()), ("legacy", "tok"));
    }

    #[tokio::test]
    async fn outcomes_are_counted_and_registered() {
        let before = resolve_count("twitch", "channel_not_owned");
        let src = ResolverTwitchCredentials::new(two_communities());
        let _ = src.credential(Some(1), "#chan_b").await;
        assert_eq!(resolve_count("twitch", "channel_not_owned"), before + 1);
        let reg = prometheus::Registry::new();
        register_metrics(&reg).unwrap();
        assert!(register_metrics(&reg).is_err());
    }
}
