//! Per-community Kick outbound credential.
//!
//! The Kick analogue of [`crate::outbound_creds_discord`]: on EVERY call it turns a numeric
//! `community_id` into the community's own Kick OAuth access token. Nothing is cached and nothing
//! falls back to another community's credential.
//!
//! Kick's `chat.send` posts as the application bot to the channel attached to the token, so there
//! is no channel to pin or own-check here (the token itself IS the channel authority, and it is
//! resolved only for the stamped community). A community with no Kick connection is refused; one
//! with several is refused as ambiguous (the host never guesses which channel's token to use).
//!
//! **Expiry:** hub-api refreshes OAuth tokens when it resolves (Kick access tokens are short
//! lived); the executor never refreshes. A token that has expired (or expires within
//! [`EXPIRY_SKEW`]) is refused with the typed, RETRYABLE [`CredError::TokenExpired`].
//!
//! Logs and errors carry fixed reason codes and numeric community ids only, never a token.

use std::time::Duration;

use connection_resolver::{
    BindingSummary, ConnectionKind, ConnectionRef, ConnectionResolve, ConnectionSecret,
    SecretString,
};

use crate::outbound_creds::{map_resolve_error, record_resolve, CredError};
use crate::twitch_sessions::spec::{MAX_PAGES, PAGE_LIMIT};

/// Provider name as hub-api's binding list knows it.
const PROVIDER: &str = "kick";
/// A token expiring within this many seconds is treated as already expired.
pub const EXPIRY_SKEW: i64 = 30;
/// Bound on each hub-api call.
const CALL_TIMEOUT: Duration = Duration::from_secs(5);

/// What the host needs to post as one community's Kick bot.
pub struct KickBinding {
    /// The OAuth access token (zeroizing, never `Debug`-printed).
    pub oauth_token: SecretString,
}

impl std::fmt::Debug for KickBinding {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("KickBinding")
            .field("oauth_token", &"[REDACTED]")
            .finish()
    }
}

/// Produces a community's [`KickBinding`]; resolves on every call and never falls back to
/// another community's credential.
#[allow(async_fn_in_trait)] // service-binary-internal, as TwitchCredentialSource
pub trait KickCredentialSource: Send + Sync {
    /// The binding for `community_id` (`None` = the entry has no scope).
    async fn binding(&self, community_id: Option<i64>) -> Result<KickBinding, CredError>;
}

/// Epoch seconds now. A clock before the epoch reads as "far future", so every expiring token is
/// refused: fail closed.
fn epoch_now() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(i64::MAX, |d| {
            i64::try_from(d.as_secs()).map_or(i64::MAX, |s| s)
        })
}

/// Production [`KickCredentialSource`] over `connection_resolver`.
pub struct ResolverKickCredentials<R: ConnectionResolve> {
    resolver: R,
    clock: fn() -> i64,
}

impl<R: ConnectionResolve> ResolverKickCredentials<R> {
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

    /// Pages the kick binding list (non-secret metadata) to completion; a page failure aborts
    /// rather than yielding a silently shorter list.
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

    async fn binding_inner(&self, community_id: Option<i64>) -> Result<KickBinding, CredError> {
        let community_id = community_id
            .filter(|c| *c > 0)
            .ok_or(CredError::NoCommunityScope)?;
        let mine: Vec<i64> = self
            .list_all()
            .await?
            .into_iter()
            .filter(|b| b.community_id == community_id && b.provider == PROVIDER)
            .map(|b| b.connection_id)
            .collect();
        let connection_id = match mine.as_slice() {
            [] => return Err(CredError::NoBinding),
            [one] => *one,
            _ => return Err(CredError::AmbiguousBinding),
        };
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
        if let Some(exp) = resolved.expires_at {
            if exp <= (self.clock)().saturating_add(EXPIRY_SKEW) {
                return Err(CredError::TokenExpired);
            }
        }
        let ConnectionSecret::OAuthAccessToken { access_token } = resolved.secret else {
            return Err(CredError::NotUsable);
        };
        Ok(KickBinding {
            oauth_token: access_token,
        })
    }
}

impl<R: ConnectionResolve> KickCredentialSource for ResolverKickCredentials<R> {
    async fn binding(&self, community_id: Option<i64>) -> Result<KickBinding, CredError> {
        let result = self.binding_inner(community_id).await;
        let outcome = match &result {
            Ok(_) => "ok",
            Err(e) => e.reason(),
        };
        record_resolve("kick", outcome);
        match &result {
            Ok(_) => tracing::debug!(
                platform = "kick",
                community_id = ?community_id,
                "outbound credential resolved"
            ),
            Err(e) => tracing::warn!(
                platform = "kick",
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
    use std::collections::BTreeMap;
    use std::sync::atomic::{AtomicUsize, Ordering};

    use connection_resolver::{BindingPage, Endpoint, ResolveError, ResolvedConnection};

    use super::*;

    const TOKEN: &str = "KICK-SECRET-TOKEN";
    const NOW: i64 = 1_000_000;

    fn summary(community: i64, conn: i64) -> BindingSummary {
        BindingSummary {
            tenant_id: 1,
            secret_version: 1,
            community_id: community,
            connection_id: conn,
            provider: PROVIDER.to_string(),
            kind: ConnectionKind::PlatformOauth,
            metadata: BTreeMap::new(),
        }
    }

    struct Fake {
        bindings: Vec<BindingSummary>,
        resolves: AtomicUsize,
        answer: Option<(i64, i64, &'static str)>,
        expires_at: Option<i64>,
        non_oauth: bool,
        list_err: bool,
    }

    impl Fake {
        fn new(bindings: Vec<BindingSummary>) -> Self {
            Self {
                bindings,
                resolves: AtomicUsize::new(0),
                answer: None,
                expires_at: None,
                non_oauth: false,
                list_err: false,
            }
        }
    }

    impl ConnectionResolve for Fake {
        async fn resolve(&self, r: ConnectionRef) -> Result<ResolvedConnection, ResolveError> {
            self.resolves.fetch_add(1, Ordering::SeqCst);
            let (c, k, p) = self
                .answer
                .unwrap_or((r.community_id, r.connection_id, PROVIDER));
            Ok(ResolvedConnection {
                connection_id: k,
                community_id: c,
                provider: p.to_string(),
                kind: ConnectionKind::PlatformOauth,
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
                metadata: BTreeMap::new(),
            })
        }

        async fn list_bindings(
            &self,
            _p: &str,
            _c: Option<&str>,
            _after: i64,
            _limit: u32,
        ) -> Result<BindingPage, ResolveError> {
            if self.list_err {
                return Err(ResolveError::Transient("x"));
            }
            Ok(BindingPage {
                items: self.bindings.clone(),
                next_after: None,
            })
        }
    }

    fn src(f: Fake) -> ResolverKickCredentials<Fake> {
        ResolverKickCredentials::with_clock(f, || NOW)
    }

    #[tokio::test]
    async fn resolves_only_the_communitys_own_token_fresh_each_call() {
        let s = src(Fake::new(vec![summary(1, 10), summary(2, 20)]));
        let b = s.binding(Some(1)).await.unwrap();
        assert_eq!(b.oauth_token.expose(), TOKEN);
        assert!(!format!("{b:?}").contains(TOKEN));
        s.binding(Some(1)).await.unwrap();
        assert_eq!(s.resolver.resolves.load(Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn refusals_are_typed_and_nothing_is_resolved_without_a_unique_binding() {
        let s = src(Fake::new(vec![summary(1, 10), summary(1, 11)]));
        for (c, want) in [
            (None, CredError::NoCommunityScope),
            (Some(0), CredError::NoCommunityScope),
            (Some(-4), CredError::NoCommunityScope),
            (Some(1), CredError::AmbiguousBinding),
            (Some(9), CredError::NoBinding),
        ] {
            assert_eq!(s.binding(c).await.unwrap_err(), want, "{c:?}");
        }
        assert_eq!(s.resolver.resolves.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn another_providers_binding_never_counts() {
        let mut other = summary(1, 10);
        other.provider = "twitch".to_string();
        let s = src(Fake::new(vec![other]));
        assert_eq!(s.binding(Some(1)).await.unwrap_err(), CredError::NoBinding);
    }

    #[tokio::test]
    async fn a_mismatched_answer_or_wrong_secret_scheme_is_not_usable() {
        let mut f = Fake::new(vec![summary(1, 10)]);
        f.answer = Some((2, 10, PROVIDER));
        assert_eq!(
            src(f).binding(Some(1)).await.unwrap_err(),
            CredError::NotUsable
        );
        let mut f = Fake::new(vec![summary(1, 10)]);
        f.answer = Some((1, 10, "twitch"));
        assert_eq!(
            src(f).binding(Some(1)).await.unwrap_err(),
            CredError::NotUsable
        );
        let mut f = Fake::new(vec![summary(1, 10)]);
        f.non_oauth = true;
        assert_eq!(
            src(f).binding(Some(1)).await.unwrap_err(),
            CredError::NotUsable
        );
    }

    #[tokio::test]
    async fn an_expired_or_nearly_expired_token_is_refused_retryably() {
        for exp in [NOW - 1, NOW, NOW + EXPIRY_SKEW] {
            let mut f = Fake::new(vec![summary(1, 10)]);
            f.expires_at = Some(exp);
            assert_eq!(
                src(f).binding(Some(1)).await.unwrap_err(),
                CredError::TokenExpired,
                "{exp}"
            );
        }
        let mut f = Fake::new(vec![summary(1, 10)]);
        f.expires_at = Some(NOW + EXPIRY_SKEW + 1);
        assert!(src(f).binding(Some(1)).await.is_ok());
    }

    #[tokio::test]
    async fn a_binding_list_outage_is_unavailable_not_a_missing_binding() {
        let mut f = Fake::new(vec![summary(1, 10)]);
        f.list_err = true;
        assert_eq!(
            src(f).binding(Some(1)).await.unwrap_err(),
            CredError::Unavailable
        );
    }
}
