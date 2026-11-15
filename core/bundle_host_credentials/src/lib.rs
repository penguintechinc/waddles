//! Production adapters for bundle `http.send` secret refs.
//!
//! * [`ResolverConnections`] -- a [`ConnectionCredentials`] over hub-api's
//!   connection resolver. hub-api's `resolve` refreshes OAuth tokens under a
//!   lock and returns a fresh access token, so refresh stays host/hub-side.
//!   A cached `(community, provider) -> connection_id` directory (the list
//!   endpoint is provider-wide, not per-community) finds the connection;
//!   credentials themselves are NEVER cached here.
//! * [`ValkeyQuotaStore`] -- atomic multi-counter charge in one Lua script.

pub mod wiring;

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use base64::Engine;
use bundle_host_http::credentials::{
    BoxFut, ConnField, ConnectionCredentials, CredentialError, QuotaCharge, QuotaScope, QuotaStore,
    QuotaVerdict,
};
use connection_resolver::{ConnectionKind, ConnectionRef, ConnectionResolve, ConnectionSecret};

/// How long a provider's binding directory is trusted before a refresh.
const DIRECTORY_TTL: Duration = Duration::from_secs(30);
/// A miss within this window of the last refresh is a definitive "not connected"
/// (bounds list calls under a flood of unconnected communities).
const DIRECTORY_MISS_FLOOR: Duration = Duration::from_secs(5);
const PAGE: u32 = 200;
const MAX_PAGES: usize = 500;

struct Directory {
    by_community: HashMap<i64, i64>,
    loaded_at: Instant,
}

/// [`ConnectionCredentials`] over a [`ConnectionResolve`] (the real
/// `ConnectionResolver` in production, a fake in tests).
pub struct ResolverConnections<R: ConnectionResolve> {
    resolver: R,
    directories: Mutex<HashMap<String, std::sync::Arc<tokio::sync::Mutex<Option<Directory>>>>>,
}

impl<R: ConnectionResolve> ResolverConnections<R> {
    /// Wraps `resolver`.
    pub fn new(resolver: R) -> Self {
        Self {
            resolver,
            directories: Mutex::new(HashMap::new()),
        }
    }

    async fn connection_id(
        &self,
        community_id: i64,
        provider: &str,
    ) -> Result<i64, CredentialError> {
        let slot = {
            let mut dirs = self.directories.lock().unwrap_or_else(|e| e.into_inner());
            std::sync::Arc::clone(dirs.entry(provider.to_string()).or_default())
        };
        let mut guard = slot.lock().await;
        if let Some(d) = guard.as_ref() {
            let age = d.loaded_at.elapsed();
            if let Some(id) = d.by_community.get(&community_id) {
                if age < DIRECTORY_TTL {
                    return Ok(*id);
                }
            } else if age < DIRECTORY_MISS_FLOOR {
                return Err(CredentialError::NotConnected);
            }
        }
        let fresh = self.load_directory(provider).await?;
        let found = fresh.by_community.get(&community_id).copied();
        *guard = Some(fresh);
        found.ok_or(CredentialError::NotConnected)
    }

    async fn load_directory(&self, provider: &str) -> Result<Directory, CredentialError> {
        let mut by_community: HashMap<i64, i64> = HashMap::new();
        let mut after = 0;
        for _ in 0..MAX_PAGES {
            let page = self
                .resolver
                .list_bindings(provider, None, after, PAGE)
                .await
                .map_err(|e| {
                    tracing::warn!(outcome = e.outcome(), "connection directory list failed");
                    CredentialError::Unavailable
                })?;
            for b in page.items {
                if !matches!(
                    b.kind,
                    ConnectionKind::PlatformOauth | ConnectionKind::ExternalHttp
                ) {
                    continue;
                }
                // Deterministic when a community has several: lowest id wins.
                by_community
                    .entry(b.community_id)
                    .and_modify(|id| *id = (*id).min(b.connection_id))
                    .or_insert(b.connection_id);
            }
            match page.next_after {
                Some(next) => after = next,
                None => {
                    return Ok(Directory {
                        by_community,
                        loaded_at: Instant::now(),
                    })
                }
            }
        }
        tracing::error!("connection directory exceeded page cap; refusing a partial directory");
        Err(CredentialError::Unavailable)
    }
}

impl<R: ConnectionResolve> ResolverConnections<R> {
    /// Resolves the community's `provider` connection, checking the answer is
    /// for exactly the community/provider asked (defence in depth).
    async fn resolve_checked(
        &self,
        community_id: i64,
        provider: &str,
    ) -> Result<connection_resolver::ResolvedConnection, CredentialError> {
        let connection_id = self.connection_id(community_id, provider).await?;
        let resolved = self
            .resolver
            .resolve(ConnectionRef {
                community_id,
                connection_id,
            })
            .await
            .map_err(|e| {
                tracing::debug!(outcome = e.outcome(), "connection resolve refused");
                if e.is_transient() || matches!(e, connection_resolver::ResolveError::Auth) {
                    CredentialError::Unavailable
                } else {
                    CredentialError::NotConnected
                }
            })?;
        if resolved.community_id != community_id || resolved.provider != provider {
            return Err(CredentialError::NotConnected);
        }
        Ok(resolved)
    }
}

/// The secret value with no scheme prefix (`#raw`).
fn raw_secret(secret: &ConnectionSecret) -> Option<String> {
    match secret {
        ConnectionSecret::OAuthAccessToken { access_token } => {
            Some(access_token.expose().to_string())
        }
        ConnectionSecret::Bearer { token } => Some(token.expose().to_string()),
        ConnectionSecret::ApiKeyHeader { value, .. } => Some(value.expose().to_string()),
        ConnectionSecret::WixApi { api_key, .. } => Some(api_key.expose().to_string()),
        ConnectionSecret::Basic { .. }
        | ConnectionSecret::AwsAccessKey { .. }
        | ConnectionSecret::GcpServiceAccount { .. } => None,
    }
}

/// Maps a resolved connection + requested field to the slot value. Empty
/// values are never returned (`NotConnected`).
fn field_value(
    resolved: &connection_resolver::ResolvedConnection,
    field: &ConnField,
) -> Result<String, CredentialError> {
    let value = match field {
        ConnField::Meta(key) => resolved.metadata.get(key).cloned(),
        ConnField::Raw => raw_secret(&resolved.secret),
        ConnField::Field(name) => match (&resolved.secret, name.as_str()) {
            (ConnectionSecret::WixApi { api_key, .. }, "apiKey") => {
                Some(api_key.expose().to_string())
            }
            (ConnectionSecret::WixApi { site_id, .. }, "siteId") => {
                Some(site_id.expose().to_string())
            }
            _ => None,
        },
        ConnField::Token => match &resolved.secret {
            ConnectionSecret::OAuthAccessToken { access_token } => {
                Some(format!("Bearer {}", access_token.expose()))
            }
            ConnectionSecret::Bearer { token } => Some(format!("Bearer {}", token.expose())),
            ConnectionSecret::Basic { username, password } => {
                let raw = format!("{}:{}", username.expose(), password.expose());
                Some(format!(
                    "Basic {}",
                    base64::engine::general_purpose::STANDARD.encode(raw)
                ))
            }
            ConnectionSecret::ApiKeyHeader { value, .. } => Some(value.expose().to_string()),
            // Multi-field / cloud credentials have no single header form.
            ConnectionSecret::WixApi { .. }
            | ConnectionSecret::AwsAccessKey { .. }
            | ConnectionSecret::GcpServiceAccount { .. } => None,
        },
    };
    value
        .filter(|v| !v.is_empty())
        .ok_or(CredentialError::NotConnected)
}

impl<R: ConnectionResolve> ConnectionCredentials for ResolverConnections<R> {
    fn credential<'a>(
        &'a self,
        community_id: i64,
        provider: &'a str,
        field: &'a ConnField,
    ) -> BoxFut<'a, Result<String, CredentialError>> {
        Box::pin(async move {
            let resolved = self.resolve_checked(community_id, provider).await?;
            field_value(&resolved, field)
        })
    }

    fn client_pair<'a>(
        &'a self,
        community_id: i64,
        provider: &'a str,
    ) -> BoxFut<'a, Result<(String, String), CredentialError>> {
        Box::pin(async move {
            let resolved = self.resolve_checked(community_id, provider).await?;
            let id = field_value(&resolved, &ConnField::Meta("client_id".to_string()))?;
            let secret = field_value(&resolved, &ConnField::Raw)?;
            Ok((id, secret))
        })
    }
}

/// Atomic check-then-increment over up to three counters. Returns
/// 0 = allowed, 1 = minute, 2 = day, 3 = community exhausted. Counters are
/// only incremented when EVERY applicable limit has headroom.
const CHARGE_SCRIPT: &str = r#"
local m, d, c = KEYS[1], KEYS[2], KEYS[3]
local lm, ld, lc = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
if lm > 0 and tonumber(redis.call('GET', m) or '0') >= lm then return 1 end
if ld > 0 and tonumber(redis.call('GET', d) or '0') >= ld then return 2 end
if lc > 0 and tonumber(redis.call('GET', c) or '0') >= lc then return 3 end
if lm > 0 then
  if redis.call('INCR', m) == 1 then redis.call('EXPIRE', m, 120) end
end
if ld > 0 then
  if redis.call('INCR', d) == 1 then redis.call('EXPIRE', d, 90000) end
end
if lc > 0 then
  if redis.call('INCR', c) == 1 then redis.call('EXPIRE', c, 90000) end
end
return 0
"#;

/// Valkey-backed [`QuotaStore`]: the budget is global across replicas.
/// Counters are expiring keys under `bundle:quota:`; they are non-durable by
/// design (an eviction or restart resets a window, never over-denies).
pub struct ValkeyQuotaStore {
    client: redis::Client,
    conn: tokio::sync::Mutex<Option<redis::aio::MultiplexedConnection>>,
}

impl ValkeyQuotaStore {
    /// Lazily-connecting store: the connection is opened on first charge and
    /// re-opened after any error, so a Valkey restart heals without a service
    /// restart (and startup never blocks on Valkey).
    pub fn lazy(url: &str) -> Result<Self, String> {
        let client = redis::Client::open(url).map_err(|e| format!("quota valkey url: {e}"))?;
        Ok(Self {
            client,
            conn: tokio::sync::Mutex::new(None),
        })
    }

    /// Store over the service's own (TLS-aware, CA-configured) client.
    pub fn from_client(client: redis::Client) -> Self {
        Self {
            client,
            conn: tokio::sync::Mutex::new(None),
        }
    }

    async fn connection(&self) -> Result<redis::aio::MultiplexedConnection, ()> {
        let mut slot = self.conn.lock().await;
        if let Some(c) = slot.as_ref() {
            return Ok(c.clone());
        }
        let fresh = tokio::time::timeout(
            Duration::from_secs(2),
            self.client.get_multiplexed_async_connection(),
        )
        .await
        .map_err(|_| tracing::warn!("quota valkey connect timed out"))?
        .map_err(|e| tracing::warn!(kind = ?e.kind(), "quota valkey connect failed"))?;
        *slot = Some(fresh.clone());
        Ok(fresh)
    }

    fn keys(c: &QuotaCharge) -> [String; 3] {
        let r = &c.secret_ref;
        [
            format!("bundle:quota:{r}:m:{}", c.epoch_secs / 60),
            format!("bundle:quota:{r}:d:{}", c.epoch_secs / 86_400),
            format!(
                "bundle:quota:{r}:c{}:d:{}",
                c.community_id,
                c.epoch_secs / 86_400
            ),
        ]
    }
}

impl QuotaStore for ValkeyQuotaStore {
    fn charge<'a>(&'a self, c: &'a QuotaCharge) -> BoxFut<'a, Result<QuotaVerdict, ()>> {
        Box::pin(async move {
            let [m, d, comm] = Self::keys(c);
            let mut conn = self.connection().await?;
            let code: i64 = redis::Script::new(CHARGE_SCRIPT)
                .key(m)
                .key(d)
                .key(comm)
                .arg(c.per_minute.unwrap_or(0))
                .arg(c.per_day.unwrap_or(0))
                .arg(c.community_day.unwrap_or(0))
                .invoke_async(&mut conn)
                .await
                .map_err(|e| {
                    tracing::warn!(kind = ?e.kind(), "quota store error");
                    // Drop the connection so the next charge reconnects.
                    if let Ok(mut slot) = self.conn.try_lock() {
                        *slot = None;
                    }
                })?;
            match code {
                0 => Ok(QuotaVerdict::Allowed),
                1 => Ok(QuotaVerdict::Exhausted(QuotaScope::Minute)),
                2 => Ok(QuotaVerdict::Exhausted(QuotaScope::Day)),
                3 => Ok(QuotaVerdict::Exhausted(QuotaScope::Community)),
                _ => Err(()),
            }
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use connection_resolver::{
        BindingPage, BindingSummary, Endpoint, ResolveError, ResolvedConnection, SecretString,
    };
    use std::collections::BTreeMap;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;

    /// Fake hub: bindings (community -> [(connection_id, provider)]) + a call counter.
    struct FakeHub {
        bindings: Vec<(i64, i64, &'static str)>,
        lists: Arc<AtomicUsize>,
        meta: BTreeMap<String, String>,
        lie_about_community: bool,
    }

    impl ConnectionResolve for FakeHub {
        async fn resolve(&self, r: ConnectionRef) -> Result<ResolvedConnection, ResolveError> {
            let (_, _, provider) = self
                .bindings
                .iter()
                .find(|(c, id, _)| *c == r.community_id && *id == r.connection_id)
                .ok_or(ResolveError::NotFound)?;
            Ok(ResolvedConnection {
                connection_id: r.connection_id,
                community_id: if self.lie_about_community {
                    r.community_id + 1
                } else {
                    r.community_id
                },
                provider: (*provider).to_string(),
                kind: ConnectionKind::PlatformOauth,
                endpoint: Endpoint::default(),
                secret: match *provider {
                    "wix" => ConnectionSecret::WixApi {
                        api_key: SecretString::new("wix-key".to_string()),
                        site_id: SecretString::new("wix-site".to_string()),
                    },
                    "youtube_api" => ConnectionSecret::Bearer {
                        token: SecretString::new("AIza-key".to_string()),
                    },
                    "soundcloud" => ConnectionSecret::Bearer {
                        token: SecretString::new("sc-secret".to_string()),
                    },
                    _ => ConnectionSecret::OAuthAccessToken {
                        access_token: SecretString::new(format!("tok-{}", r.community_id)),
                    },
                },
                expires_at: None,
                metadata: self.meta.clone(),
            })
        }

        async fn list_bindings(
            &self,
            provider: &str,
            _capability: Option<&str>,
            after: i64,
            _limit: u32,
        ) -> Result<BindingPage, ResolveError> {
            self.lists.fetch_add(1, Ordering::SeqCst);
            // Two pages: the cursor splits the bindings in half.
            let items: Vec<BindingSummary> = self
                .bindings
                .iter()
                .filter(|(_, id, p)| *p == provider && *id > after)
                .take(1)
                .map(|(c, id, p)| BindingSummary {
                    community_id: *c,
                    connection_id: *id,
                    tenant_id: 1,
                    secret_version: 1,
                    provider: (*p).to_string(),
                    kind: ConnectionKind::PlatformOauth,
                    metadata: BTreeMap::new(),
                })
                .collect();
            let next_after = items.first().map(|b| b.connection_id).filter(|id| {
                self.bindings
                    .iter()
                    .any(|(_, i, p)| *p == provider && i > id)
            });
            Ok(BindingPage { items, next_after })
        }
    }

    fn hub(lie: bool) -> (ResolverConnections<FakeHub>, Arc<AtomicUsize>) {
        let lists = Arc::new(AtomicUsize::new(0));
        let mut meta = BTreeMap::new();
        meta.insert("site_id".to_string(), "site-1".to_string());
        meta.insert("client_id".to_string(), "sc-client".to_string());
        (
            ResolverConnections::new(FakeHub {
                bindings: vec![
                    (5, 10, "spotify"),
                    (6, 11, "spotify"),
                    (7, 12, "wix"),
                    (8, 13, "youtube_api"),
                    (9, 14, "soundcloud"),
                ],
                lists: Arc::clone(&lists),
                meta,
                lie_about_community: lie,
            }),
            lists,
        )
    }

    #[tokio::test]
    async fn resolves_each_communitys_own_token_across_directory_pages() {
        let (c, _) = hub(false);
        assert_eq!(
            c.credential(5, "spotify", &ConnField::Token).await.unwrap(),
            "Bearer tok-5"
        );
        assert_eq!(
            c.credential(6, "spotify", &ConnField::Token).await.unwrap(),
            "Bearer tok-6"
        );
    }

    #[tokio::test]
    async fn community_without_a_connection_and_wrong_provider_are_not_connected() {
        let (c, _) = hub(false);
        assert_eq!(
            c.credential(9, "spotify", &ConnField::Token).await,
            Err(CredentialError::NotConnected)
        );
        assert_eq!(
            c.credential(5, "wix", &ConnField::Token).await,
            Err(CredentialError::NotConnected)
        );
    }

    #[tokio::test]
    async fn directory_misses_are_rate_limited_not_a_list_per_call() {
        let (c, lists) = hub(false);
        let _ = c.credential(9, "spotify", &ConnField::Token).await;
        let after_first = lists.load(Ordering::SeqCst);
        for _ in 0..20 {
            let _ = c.credential(9, "spotify", &ConnField::Token).await;
        }
        assert_eq!(lists.load(Ordering::SeqCst), after_first);
    }

    #[tokio::test]
    async fn metadata_field_is_returned_and_a_missing_key_is_not_connected() {
        let (c, _) = hub(false);
        assert_eq!(
            c.credential(7, "wix", &ConnField::Meta("site_id".into()))
                .await
                .unwrap(),
            "site-1"
        );
        assert_eq!(
            c.credential(7, "wix", &ConnField::Meta("nope".into()))
                .await,
            Err(CredentialError::NotConnected)
        );
    }

    #[tokio::test]
    async fn wix_scheme_6_maps_api_key_and_site_id_and_has_no_single_header_form() {
        let (c, _) = hub(false);
        assert_eq!(
            c.credential(7, "wix", &ConnField::Raw).await.unwrap(),
            "wix-key"
        );
        assert_eq!(
            c.credential(7, "wix", &ConnField::Field("siteId".into()))
                .await
                .unwrap(),
            "wix-site"
        );
        assert_eq!(
            c.credential(7, "wix", &ConnField::Field("apiKey".into()))
                .await
                .unwrap(),
            "wix-key"
        );
        assert_eq!(
            c.credential(7, "wix", &ConnField::Field("nope".into()))
                .await,
            Err(CredentialError::NotConnected)
        );
        assert_eq!(
            c.credential(7, "wix", &ConnField::Token).await,
            Err(CredentialError::NotConnected)
        );
    }

    #[tokio::test]
    async fn youtube_api_key_is_returned_raw_without_a_scheme_prefix() {
        let (c, _) = hub(false);
        assert_eq!(
            c.credential(8, "youtube_api", &ConnField::Raw)
                .await
                .unwrap(),
            "AIza-key"
        );
    }

    #[tokio::test]
    async fn soundcloud_client_pair_is_metadata_id_plus_the_secret() {
        let (c, _) = hub(false);
        assert_eq!(
            c.client_pair(9, "soundcloud").await.unwrap(),
            ("sc-client".to_string(), "sc-secret".to_string())
        );
        assert_eq!(
            c.client_pair(5, "soundcloud").await,
            Err(CredentialError::NotConnected)
        );
    }

    #[tokio::test]
    async fn a_resolver_answering_for_another_community_is_refused() {
        let (c, _) = hub(true);
        assert_eq!(
            c.credential(5, "spotify", &ConnField::Token).await,
            Err(CredentialError::NotConnected)
        );
    }
}
