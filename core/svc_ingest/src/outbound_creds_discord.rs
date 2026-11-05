//! Per-community Discord outbound credential + guild binding (P6a T6d-2).
//!
//! The Discord analogue of [`crate::outbound_creds::TwitchCredentialSource`]:
//! on EVERY call it turns a numeric `community_id` into the community's own
//! bot token, the numeric guild the community's binding pins, and the
//! binding's MANAGED-ROLES allowlist (role key -> role snowflake). Nothing is
//! cached and nothing falls back to another community's credential.
//!
//! **Binding contract** (non-secret metadata on the community's `discord`
//! binding in hub-api; read from the RESOLVED record, never only the list):
//!
//! | key | value |
//! |---|---|
//! | `guild_id` | the guild snowflake the community's bot serves |
//! | `managed_roles` | optional `key:snowflake,key:snowflake` allowlist; keys `[a-z][a-z0-9_-]{0,31}` |
//!
//! The secret must be a `Bearer` scheme secret holding the bot token. A
//! community with no binding is refused; one with several is refused as
//! ambiguous (the host never guesses a guild). Logs and errors carry fixed
//! reason codes and numeric community ids only.

use std::collections::BTreeMap;
use std::time::Duration;

use connection_resolver::{
    BindingSummary, ConnectionRef, ConnectionResolve, ConnectionSecret, SecretString,
};

use crate::discord_rest::is_snowflake;
use crate::outbound_creds::{map_resolve_error, record_resolve, CredError};
use crate::twitch_sessions::spec::{MAX_PAGES, PAGE_LIMIT};

/// Provider name as hub-api's binding list knows it.
const PROVIDER: &str = "discord";
/// Metadata key: the pinned guild snowflake.
pub const META_GUILD_ID: &str = "guild_id";
/// Metadata key: the managed-roles allowlist.
pub const META_MANAGED_ROLES: &str = "managed_roles";
/// Most managed roles one binding may list.
const MAX_MANAGED_ROLES: usize = 64;
/// Bound on each hub-api call.
const CALL_TIMEOUT: Duration = Duration::from_secs(5);

/// Everything the host needs to act as one community's bot in one guild.
pub struct DiscordBinding {
    /// The bot token (zeroizing, never `Debug`-printed).
    pub bot_token: SecretString,
    /// The guild the binding pins (numeric snowflake).
    pub guild_id: String,
    /// Managed-role allowlist: key -> role snowflake.
    pub managed_roles: BTreeMap<String, String>,
}

impl std::fmt::Debug for DiscordBinding {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("DiscordBinding")
            .field("bot_token", &"[REDACTED]")
            .field("guild_id", &self.guild_id)
            .field("managed_roles", &self.managed_roles.len())
            .finish()
    }
}

/// Produces a community's [`DiscordBinding`]; resolves on every call and
/// never falls back to another community's credential.
#[allow(async_fn_in_trait)] // service-binary-internal, as TwitchCredentialSource
pub trait DiscordCredentialSource: Send + Sync {
    /// The binding for `community_id` (`None` = the entry has no scope).
    async fn binding(&self, community_id: Option<i64>) -> Result<DiscordBinding, CredError>;
}

/// `true` for a managed-role key `[a-z][a-z0-9_-]{0,31}`.
#[must_use]
pub fn is_role_key(s: &str) -> bool {
    let mut b = s.bytes();
    s.len() <= 32
        && b.next().is_some_and(|c| c.is_ascii_lowercase())
        && b.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'_' || c == b'-')
}

/// Parses `key:snowflake,key:snowflake`. Any malformed or duplicate entry
/// rejects the WHOLE list (fail closed: a half-parsed allowlist is a guess).
pub(crate) fn parse_managed_roles(raw: &str) -> Option<BTreeMap<String, String>> {
    let mut out = BTreeMap::new();
    if raw.trim().is_empty() {
        return Some(out);
    }
    for pair in raw.split(',') {
        let (k, v) = pair.trim().split_once(':')?;
        if !is_role_key(k) || !is_snowflake(v) || out.len() >= MAX_MANAGED_ROLES {
            return None;
        }
        if out.insert(k.to_string(), v.to_string()).is_some() {
            return None;
        }
    }
    Some(out)
}

/// `(guild_id, managed_roles)` from binding metadata, or `None` if the
/// binding is not a usable Discord binding.
fn spec_of(meta: &BTreeMap<String, String>) -> Option<(String, BTreeMap<String, String>)> {
    let guild = meta.get(META_GUILD_ID)?.trim().to_string();
    if !is_snowflake(&guild) {
        return None;
    }
    let roles = match meta.get(META_MANAGED_ROLES) {
        Some(r) => parse_managed_roles(r)?,
        None => BTreeMap::new(),
    };
    Some((guild, roles))
}

/// Production [`DiscordCredentialSource`] over `connection_resolver`.
pub struct ResolverDiscordCredentials<R: ConnectionResolve> {
    resolver: R,
}

impl<R: ConnectionResolve> ResolverDiscordCredentials<R> {
    /// Wraps a resolver (`Arc<ConnectionResolver>` in production).
    #[must_use]
    pub fn new(resolver: R) -> Self {
        Self { resolver }
    }

    /// Pages the discord binding list (non-secret metadata) to completion; a
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

    async fn binding_inner(&self, community_id: Option<i64>) -> Result<DiscordBinding, CredError> {
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
        {
            return Err(CredError::NotUsable);
        }
        let (guild_id, managed_roles) = spec_of(&resolved.metadata).ok_or(CredError::NotUsable)?;
        let ConnectionSecret::Bearer { token } = resolved.secret else {
            return Err(CredError::NotUsable);
        };
        Ok(DiscordBinding {
            bot_token: token,
            guild_id,
            managed_roles,
        })
    }
}

impl<R: ConnectionResolve> DiscordCredentialSource for ResolverDiscordCredentials<R> {
    async fn binding(&self, community_id: Option<i64>) -> Result<DiscordBinding, CredError> {
        let result = self.binding_inner(community_id).await;
        let outcome = match &result {
            Ok(_) => "ok",
            Err(e) => e.reason(),
        };
        record_resolve("discord", outcome);
        match &result {
            Ok(_) => tracing::debug!(
                platform = "discord",
                community_id = ?community_id,
                "outbound credential resolved"
            ),
            Err(e) => tracing::warn!(
                platform = "discord",
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
        BindingPage, ConnectionKind, Endpoint, ResolveError, ResolvedConnection,
    };

    use super::*;

    const TOKEN: &str = "BOT-TOKEN-SECRET";

    fn meta(guild: &str, roles: Option<&str>) -> BTreeMap<String, String> {
        let mut m = BTreeMap::new();
        m.insert(META_GUILD_ID.to_string(), guild.to_string());
        if let Some(r) = roles {
            m.insert(META_MANAGED_ROLES.to_string(), r.to_string());
        }
        m
    }

    fn summary(community: i64, conn: i64, m: BTreeMap<String, String>) -> BindingSummary {
        BindingSummary {
            community_id: community,
            connection_id: conn,
            provider: PROVIDER.to_string(),
            kind: ConnectionKind::ExternalHttp,
            metadata: m,
        }
    }

    struct Fake {
        bindings: Vec<BindingSummary>,
        /// Resolve answers with this record instead of the asked one.
        answer: Option<(i64, i64, &'static str)>,
        non_bearer: bool,
        list_err: Option<ResolveError>,
        resolves: AtomicUsize,
        meta_override: Option<BTreeMap<String, String>>,
    }

    impl Fake {
        fn new(bindings: Vec<BindingSummary>) -> Self {
            Self {
                bindings,
                answer: None,
                non_bearer: false,
                list_err: None,
                resolves: AtomicUsize::new(0),
                meta_override: None,
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
                    .unwrap_or_default()
            });
            Ok(ResolvedConnection {
                connection_id: k,
                community_id: c,
                provider: p.to_string(),
                kind: ConnectionKind::ExternalHttp,
                endpoint: Endpoint::default(),
                secret: if self.non_bearer {
                    ConnectionSecret::OAuthAccessToken {
                        access_token: SecretString::new(TOKEN.to_string()),
                    }
                } else {
                    ConnectionSecret::Bearer {
                        token: SecretString::new(TOKEN.to_string()),
                    }
                },
                expires_at: None,
                metadata,
            })
        }

        async fn list_bindings(
            &self,
            _p: &str,
            _c: Option<&str>,
            _after: i64,
            _limit: u32,
        ) -> Result<BindingPage, ResolveError> {
            if let Some(e) = &self.list_err {
                return Err(match e {
                    ResolveError::Auth => ResolveError::Auth,
                    _ => ResolveError::Transient("x"),
                });
            }
            Ok(BindingPage {
                items: self.bindings.clone(),
                next_after: None,
            })
        }
    }

    fn src(f: Fake) -> ResolverDiscordCredentials<Fake> {
        ResolverDiscordCredentials::new(f)
    }

    #[tokio::test]
    async fn resolves_the_communitys_own_binding_with_roles() {
        let s = src(Fake::new(vec![
            summary(1, 10, meta("111", Some("vip:222,mod-x:333"))),
            summary(2, 20, meta("999", None)),
        ]));
        let b = s.binding(Some(1)).await.unwrap();
        assert_eq!(b.bot_token.expose(), TOKEN);
        assert_eq!(b.guild_id, "111");
        assert_eq!(b.managed_roles.get("vip").unwrap(), "222");
        assert_eq!(b.managed_roles.len(), 2);
        // Debug never carries the token.
        assert!(!format!("{b:?}").contains(TOKEN));
        // Per call, uncached: a second call resolves again.
        s.binding(Some(1)).await.unwrap();
        assert_eq!(s.resolver.resolves.load(Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn refuses_without_scope_binding_or_when_ambiguous() {
        let s = src(Fake::new(vec![
            summary(1, 10, meta("111", None)),
            summary(1, 11, meta("112", None)),
        ]));
        assert_eq!(
            s.binding(None).await.unwrap_err(),
            CredError::NoCommunityScope
        );
        assert_eq!(
            s.binding(Some(0)).await.unwrap_err(),
            CredError::NoCommunityScope
        );
        assert_eq!(s.binding(Some(3)).await.unwrap_err(), CredError::NoBinding);
        assert_eq!(
            s.binding(Some(1)).await.unwrap_err(),
            CredError::AmbiguousBinding
        );
        assert_eq!(CredError::AmbiguousBinding.reason(), "ambiguous_binding");
    }

    #[tokio::test]
    async fn refuses_a_resolve_answer_for_another_record() {
        for answer in [(2, 10, PROVIDER), (1, 99, PROVIDER), (1, 10, "twitch")] {
            let mut f = Fake::new(vec![summary(1, 10, meta("111", None))]);
            f.answer = Some(answer);
            assert_eq!(
                src(f).binding(Some(1)).await.unwrap_err(),
                CredError::NotUsable
            );
        }
    }

    #[tokio::test]
    async fn refuses_bad_metadata_and_wrong_secret_scheme() {
        for bad in [
            meta("not-a-snowflake", None),
            meta("111", Some("vip:222,vip:333")),
            meta("111", Some("VIP:222")),
            meta("111", Some("vip:role")),
            meta("111", Some("vip")),
            BTreeMap::new(),
        ] {
            let mut f = Fake::new(vec![summary(1, 10, bad.clone())]);
            f.meta_override = Some(bad);
            assert_eq!(
                src(f).binding(Some(1)).await.unwrap_err(),
                CredError::NotUsable
            );
        }
        let mut f = Fake::new(vec![summary(1, 10, meta("111", None))]);
        f.non_bearer = true;
        assert_eq!(
            src(f).binding(Some(1)).await.unwrap_err(),
            CredError::NotUsable
        );
    }

    #[tokio::test]
    async fn the_resolved_records_metadata_wins_over_the_list() {
        // Metadata rewritten between list and resolve: the resolved guild is used.
        let mut f = Fake::new(vec![summary(1, 10, meta("111", None))]);
        f.meta_override = Some(meta("777", None));
        assert_eq!(src(f).binding(Some(1)).await.unwrap().guild_id, "777");
    }

    #[tokio::test]
    async fn a_failed_list_is_typed_and_never_a_credential() {
        let mut f = Fake::new(vec![]);
        f.list_err = Some(ResolveError::Auth);
        assert_eq!(
            src(f).binding(Some(1)).await.unwrap_err(),
            CredError::NotUsable
        );
        let mut f = Fake::new(vec![]);
        f.list_err = Some(ResolveError::Transient("x"));
        assert_eq!(
            src(f).binding(Some(1)).await.unwrap_err(),
            CredError::Unavailable
        );
    }

    #[test]
    fn role_key_and_list_parsing_rules() {
        assert!(is_role_key("vip") && is_role_key("a-b_9") && is_role_key(&"a".repeat(32)));
        assert!(
            !is_role_key("")
                && !is_role_key("9a")
                && !is_role_key("A")
                && !is_role_key(&"a".repeat(33))
        );
        assert!(parse_managed_roles("").unwrap().is_empty());
        assert_eq!(parse_managed_roles(" vip:1 , b:2 ").unwrap().len(), 2);
        let many = (0..65)
            .map(|i| format!("k{i}:1"))
            .collect::<Vec<_>>()
            .join(",");
        assert!(parse_managed_roles(&many).is_none());
    }
}
