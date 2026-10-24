//! Binding discovery: hub-api binding list -> deduplicated [`SessionSpec`]s.
//!
//! The desired set is returned COMPLETE or not at all: any page failure
//! aborts the whole discovery, because a silently shorter set would make the
//! reconciler tear down healthy sessions.

use std::collections::BTreeMap;
use std::fmt;
use std::time::Duration;

use connection_resolver::{BindingSummary, ConnectionKind, ConnectionResolve, ResolveError};

/// Binding metadata key holding the Twitch channel login (adapter resource id).
pub const META_CHANNEL: &str = "channel";
/// Binding metadata key holding the IRC nick the token authenticates as.
pub const META_BOT_LOGIN: &str = "bot_login";
/// Bindings requested per page.
pub const PAGE_LIMIT: u32 = 200;
/// Hard cap on pages per discovery; exceeding it is a failure, not a truncation.
pub const MAX_PAGES: u32 = 100;
/// Provider name as hub-api's binding list knows it.
const PROVIDER: &str = "twitch";
/// Twitch login length ceiling.
const MAX_LOGIN_LEN: usize = 25;

/// Why a binding could not become a [`SessionSpec`] (never carries the value).
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum SpecError {
    /// The binding is not a platform-OAuth connection.
    #[error("binding kind is not platform_oauth")]
    WrongKind,
    /// A required metadata key is absent.
    #[error("binding metadata is missing required key `{0}`")]
    MissingKey(&'static str),
    /// A metadata value is not a valid Twitch login.
    #[error("binding metadata key `{0}` is not a valid twitch login")]
    InvalidLogin(&'static str),
}

/// One desired Twitch inbound session. Logins are usernames (PII), so `Debug`
/// redacts them.
#[derive(Clone, PartialEq, Eq)]
pub struct SessionSpec {
    pub community_id: i64,
    pub connection_id: i64,
    pub channel: String,
    pub bot_login: String,
}

impl fmt::Debug for SessionSpec {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("SessionSpec")
            .field("community_id", &self.community_id)
            .field("connection_id", &self.connection_id)
            .field("channel", &"<redacted>")
            .field("bot_login", &"<redacted>")
            .finish()
    }
}

/// Lowercases, strips one leading `#`, and validates `^[a-z0-9_]{1,25}$` with
/// a byte loop (no regex dependency).
fn normalize_login(raw: &str, key: &'static str) -> Result<String, SpecError> {
    let lowered = raw.trim().to_ascii_lowercase();
    let login = lowered.strip_prefix('#').unwrap_or(&lowered);
    let ok = !login.is_empty()
        && login.len() <= MAX_LOGIN_LEN
        && login
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_');
    if ok {
        Ok(login.to_owned())
    } else {
        Err(SpecError::InvalidLogin(key))
    }
}

/// Fetches a required metadata value or names the missing key.
fn required<'a>(m: &'a BTreeMap<String, String>, key: &'static str) -> Result<&'a str, SpecError> {
    m.get(key)
        .map(String::as_str)
        .ok_or(SpecError::MissingKey(key))
}

impl SessionSpec {
    /// Identity of the session: `(community_id, connection_id)`.
    pub fn key(&self) -> (i64, i64) {
        (self.community_id, self.connection_id)
    }

    /// Builds a spec from a binding; never invents missing metadata.
    pub fn from_summary(s: &BindingSummary) -> Result<Self, SpecError> {
        if s.kind != ConnectionKind::PlatformOauth {
            return Err(SpecError::WrongKind);
        }
        let channel = normalize_login(required(&s.metadata, META_CHANNEL)?, META_CHANNEL)?;
        let bot_login = normalize_login(required(&s.metadata, META_BOT_LOGIN)?, META_BOT_LOGIN)?;
        Ok(Self {
            community_id: s.community_id,
            connection_id: s.connection_id,
            channel,
            bot_login,
        })
    }
}

/// The complete desired set plus skip counters.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Discovery {
    pub specs: Vec<SessionSpec>,
    pub skipped_malformed: u32,
    pub skipped_duplicate: u32,
}

/// Why discovery failed; there is no partial-success variant.
#[derive(Debug, thiserror::Error)]
pub enum DiscoverError {
    #[error("binding list failed: {0}")]
    Resolve(ResolveError),
    #[error("binding list exceeded the page cap")]
    PageCapExceeded,
    #[error("binding list call timed out")]
    Timeout,
}

/// Pages the twitch binding list to completion (each call bounded by
/// `timeout`), maps and deduplicates it. Two bindings for the same channel
/// keep the lowest `community_id`. Any call failure aborts everything.
pub async fn discover<R: ConnectionResolve>(
    r: &R,
    timeout: Duration,
) -> Result<Discovery, DiscoverError> {
    let mut by_channel: BTreeMap<String, SessionSpec> = BTreeMap::new();
    let mut skipped_malformed = 0u32;
    let mut skipped_duplicate = 0u32;
    let mut after = 0i64;
    let mut pages = 0u32;
    loop {
        if pages >= MAX_PAGES {
            return Err(DiscoverError::PageCapExceeded);
        }
        pages += 1;
        let page =
            tokio::time::timeout(timeout, r.list_bindings(PROVIDER, None, after, PAGE_LIMIT))
                .await
                .map_err(|_| DiscoverError::Timeout)?
                .map_err(DiscoverError::Resolve)?;
        for item in &page.items {
            let spec = match SessionSpec::from_summary(item) {
                Ok(s) => s,
                Err(e) => {
                    tracing::warn!(
                        community_id = item.community_id,
                        connection_id = item.connection_id,
                        error = %e,
                        "skipping malformed twitch binding"
                    );
                    skipped_malformed += 1;
                    continue;
                }
            };
            match by_channel.get(&spec.channel) {
                Some(existing) if existing.community_id <= spec.community_id => {
                    skipped_duplicate += 1;
                }
                Some(_) => {
                    skipped_duplicate += 1;
                    by_channel.insert(spec.channel.clone(), spec);
                }
                None => {
                    by_channel.insert(spec.channel.clone(), spec);
                }
            }
        }
        match page.next_after {
            Some(next) => after = next,
            None => break,
        }
    }
    Ok(Discovery {
        specs: by_channel.into_values().collect(),
        skipped_malformed,
        skipped_duplicate,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use connection_resolver::{BindingPage, ConnectionRef, ResolvedConnection};
    use std::sync::Mutex;

    fn summary(
        community: i64,
        conn: i64,
        channel: Option<&str>,
        bot: Option<&str>,
    ) -> BindingSummary {
        let mut metadata = BTreeMap::new();
        if let Some(c) = channel {
            metadata.insert(META_CHANNEL.to_owned(), c.to_owned());
        }
        if let Some(b) = bot {
            metadata.insert(META_BOT_LOGIN.to_owned(), b.to_owned());
        }
        BindingSummary {
            community_id: community,
            connection_id: conn,
            provider: "twitch".to_owned(),
            kind: ConnectionKind::PlatformOauth,
            metadata,
        }
    }

    /// Scripted resolver: one response per call, recording each `after`.
    struct Fake {
        script: Mutex<Vec<Result<BindingPage, ResolveError>>>,
        afters: Mutex<Vec<i64>>,
        hang: bool,
        endless: bool,
    }

    impl Fake {
        fn new(script: Vec<Result<BindingPage, ResolveError>>) -> Self {
            Self {
                script: Mutex::new(script),
                afters: Mutex::new(vec![]),
                hang: false,
                endless: false,
            }
        }
    }

    impl ConnectionResolve for Fake {
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
            self.afters.lock().unwrap().push(after);
            if self.hang {
                std::future::pending::<()>().await;
            }
            if self.endless {
                return Ok(BindingPage {
                    items: vec![],
                    next_after: Some(after + 1),
                });
            }
            self.script.lock().unwrap().remove(0)
        }
    }

    fn page(items: Vec<BindingSummary>, next: Option<i64>) -> Result<BindingPage, ResolveError> {
        Ok(BindingPage {
            items,
            next_after: next,
        })
    }

    const T: Duration = Duration::from_secs(5);

    #[tokio::test]
    async fn discover_pages_until_next_after_none() {
        let f = Fake::new(vec![
            page(vec![summary(1, 10, Some("a"), Some("bot"))], Some(10)),
            page(vec![summary(2, 20, Some("b"), Some("bot"))], Some(20)),
            page(vec![summary(3, 30, Some("c"), Some("bot"))], None),
        ]);
        let d = discover(&f, T).await.unwrap();
        assert_eq!(d.specs.len(), 3);
        assert_eq!(*f.afters.lock().unwrap(), vec![0, 10, 20]);
    }

    #[tokio::test]
    async fn mid_pagination_error_is_discovery_failure_not_a_shorter_set() {
        let f = Fake::new(vec![
            page(vec![summary(1, 10, Some("a"), Some("bot"))], Some(10)),
            Err(ResolveError::Transient("boom")),
        ]);
        let r = discover(&f, T).await;
        assert!(matches!(
            r,
            Err(DiscoverError::Resolve(ResolveError::Transient(_)))
        ));
    }

    #[tokio::test]
    async fn page_cap_exceeded_is_failure() {
        let mut f = Fake::new(vec![]);
        f.endless = true;
        let r = discover(&f, T).await;
        assert!(matches!(r, Err(DiscoverError::PageCapExceeded)));
        assert_eq!(f.afters.lock().unwrap().len(), MAX_PAGES as usize);
    }

    #[tokio::test(start_paused = true)]
    async fn discover_times_out_per_call() {
        let mut f = Fake::new(vec![]);
        f.hang = true;
        let r = discover(&f, T).await;
        assert!(matches!(r, Err(DiscoverError::Timeout)));
    }

    #[test]
    fn from_summary_missing_channel_is_error() {
        assert_eq!(
            SessionSpec::from_summary(&summary(1, 1, None, Some("bot"))),
            Err(SpecError::MissingKey(META_CHANNEL))
        );
        assert_eq!(
            SessionSpec::from_summary(&summary(1, 1, Some("a"), None)),
            Err(SpecError::MissingKey(META_BOT_LOGIN))
        );
        for bad in ["bad name", "", "#", &"x".repeat(26), "caf\u{e9}"] {
            assert_eq!(
                SessionSpec::from_summary(&summary(1, 1, Some(bad), Some("bot"))),
                Err(SpecError::InvalidLogin(META_CHANNEL)),
                "{bad:?}"
            );
        }
        assert_eq!(
            SessionSpec::from_summary(&summary(1, 1, Some("a"), Some("bad name"))),
            Err(SpecError::InvalidLogin(META_BOT_LOGIN))
        );
    }

    #[test]
    fn from_summary_normalizes_hash_and_case() {
        let s =
            SessionSpec::from_summary(&summary(1, 2, Some("#PenguinzPlays"), Some("WaddleBots")))
                .unwrap();
        assert_eq!(s.channel, "penguinzplays");
        assert_eq!(s.bot_login, "waddlebots");
        assert_eq!(s.key(), (1, 2));
    }

    #[test]
    fn from_summary_rejects_non_oauth_kind() {
        let mut s = summary(1, 1, Some("a"), Some("bot"));
        s.kind = ConnectionKind::ExternalHttp;
        assert_eq!(SessionSpec::from_summary(&s), Err(SpecError::WrongKind));
    }

    #[test]
    fn session_spec_debug_redacts_logins() {
        let s = SessionSpec::from_summary(&summary(1, 2, Some("secretchan"), Some("secretbot")))
            .unwrap();
        let d = format!("{s:?}");
        assert!(!d.contains("secretchan") && !d.contains("secretbot"), "{d}");
        assert!(d.contains("community_id"));
    }

    #[tokio::test]
    async fn duplicate_channel_keeps_lowest_community_id_and_counts() {
        let f = Fake::new(vec![page(
            vec![
                summary(9, 90, Some("Same"), Some("bot")),
                summary(3, 30, Some("#same"), Some("bot")),
            ],
            None,
        )]);
        let d = discover(&f, T).await.unwrap();
        assert_eq!(d.specs.len(), 1);
        assert_eq!(d.specs[0].community_id, 3);
        assert_eq!(d.skipped_duplicate, 1);
    }

    #[tokio::test]
    async fn malformed_binding_is_skipped_and_counted_not_fatal() {
        let f = Fake::new(vec![page(
            vec![
                summary(1, 1, None, Some("bot")),
                summary(2, 2, Some("ok"), Some("bot")),
            ],
            None,
        )]);
        let d = discover(&f, T).await.unwrap();
        assert_eq!(d.specs.len(), 1);
        assert_eq!(d.skipped_malformed, 1);
    }

    /// Phase A golden (`resolve_v2_oauth.json`) carries the twitch adapter's
    /// binding metadata; `channel` and `bot_login` must be present.
    #[test]
    fn contract_fixture_summary_parses_to_a_spec() {
        let raw = include_str!("../../../../hub_api/tests/fixtures/resolve_v2_oauth.json");
        let v: serde_json::Value = serde_json::from_str(raw).unwrap();
        let metadata: BTreeMap<String, String> = v["metadata"]
            .as_object()
            .unwrap()
            .iter()
            .map(|(k, val)| (k.clone(), val.as_str().unwrap().to_owned()))
            .collect();
        for key in [META_CHANNEL, META_BOT_LOGIN] {
            assert!(
                metadata.contains_key(key),
                "Phase A twitch binding metadata missing key `{key}`"
            );
        }
        let s = BindingSummary {
            community_id: v["communityId"].as_i64().unwrap(),
            connection_id: v["connectionId"].as_i64().unwrap(),
            provider: v["provider"].as_str().unwrap().to_owned(),
            kind: ConnectionKind::PlatformOauth,
            metadata,
        };
        let spec = SessionSpec::from_summary(&s).unwrap();
        assert_eq!((spec.community_id, spec.connection_id), (7, 12));
        assert_eq!(spec.channel, "penguinzplays");
        assert_eq!(spec.bot_login, "waddlebots");
    }
}
