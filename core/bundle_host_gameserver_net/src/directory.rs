//! Resolves a guest-named connection UUID to an admin-registered target, scoped to the
//! invocation's tenant AND community. The guest can never supply a host or port.
//!
//! [`HubServerDirectory`] is the production implementation over hub-api's provider-connection
//! framework (provider `gameserver`): the binding list maps `(community, server_id)` to a numeric
//! connection id and server-derived tenant; the resolve call returns the sealed credential, whose
//! plaintext is `host|port|rcon_password` (written by hub-api's gameserver gateway, never returned
//! by any admin route).

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use connection_resolver::{ConnectionRef, ConnectionResolve, ConnectionSecret};

use crate::error::NetError;
use crate::secret::Secret;

/// The invocation scope: host-derived, never guest input.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct Scope {
    /// Invoking tenant (numeric).
    pub tenant_id: i64,
    /// Invoking community (numeric).
    pub community_id: i64,
}

/// A resolved, registered game-server target. `Debug` redacts the password and address.
pub struct ServerTarget {
    /// Canonical lowercase UUID of the connection (for audit).
    pub server_id: String,
    /// Registered host (DNS name or IP literal).
    pub host: String,
    /// Registered port.
    pub port: u16,
    /// RCON password (empty = connection has none; RCON opens are then refused).
    pub password: Secret,
}

impl std::fmt::Debug for ServerTarget {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ServerTarget")
            .field("server_id", &self.server_id)
            .field("host", &"[REDACTED]")
            .field("port", &"[REDACTED]")
            .field("password", &self.password)
            .finish()
    }
}

/// Boxed future (keeps [`ServerDirectory`] object-safe).
pub type DirFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// Resolves a registered connection for a scope.
pub trait ServerDirectory: Send + Sync {
    /// `Err(Denied("not_registered"))` unless `server` is a connection registered for exactly
    /// `scope.community_id` under `scope.tenant_id`.
    fn lookup<'a>(
        &'a self,
        scope: &'a Scope,
        server: &'a str,
    ) -> DirFuture<'a, Result<ServerTarget, NetError>>;
}

/// Canonical form of a UUID string, or `None` (so a guest cannot probe with odd spellings).
pub fn canonical_uuid(s: &str) -> Option<String> {
    let u = uuid::Uuid::parse_str(s).ok()?;
    let canon = u.hyphenated().to_string();
    (canon == s.to_ascii_lowercase()).then_some(canon)
}

/// Parses the sealed credential plaintext `host|port|password`.
pub fn parse_sealed(plain: &str) -> Option<(String, u16, Secret)> {
    let mut it = plain.splitn(3, '|');
    let host = it.next()?;
    let port: u16 = it.next()?.parse().ok()?;
    let password = it.next()?;
    if host.is_empty() || port == 0 {
        return None;
    }
    Some((host.to_string(), port, Secret::new(password.to_string())))
}

const BINDING_TTL: Duration = Duration::from_secs(15);
const MIN_REFRESH_GAP: Duration = Duration::from_secs(2);
const PAGE: u32 = 200;
const MAX_PAGES: usize = 50;

struct Cache {
    fetched: Option<Instant>,
    map: HashMap<(i64, String), (i64, i64)>,
}

/// Production directory over the hub-api connection resolver.
pub struct HubServerDirectory<R: ConnectionResolve> {
    resolver: R,
    cache: Mutex<Cache>,
}

impl<R: ConnectionResolve> HubServerDirectory<R> {
    /// Wraps a connection resolver.
    pub fn new(resolver: R) -> Self {
        Self {
            resolver,
            cache: Mutex::new(Cache {
                fetched: None,
                map: HashMap::new(),
            }),
        }
    }

    fn cached(&self, community: i64, server: &str) -> Option<(i64, i64)> {
        let c = self.cache.lock().ok()?;
        let fresh = c.fetched.is_some_and(|t| t.elapsed() < BINDING_TTL);
        fresh
            .then(|| c.map.get(&(community, server.to_string())).copied())
            .flatten()
    }

    fn may_refresh(&self) -> bool {
        self.cache
            .lock()
            .map(|c| c.fetched.is_none_or(|t| t.elapsed() >= MIN_REFRESH_GAP))
            .unwrap_or(false)
    }

    async fn refresh(&self) -> Result<(), NetError> {
        let mut map = HashMap::new();
        let mut after = 0;
        for _ in 0..MAX_PAGES {
            let page = self
                .resolver
                .list_bindings("gameserver", None, after, PAGE)
                .await
                .map_err(|e| {
                    tracing::error!(outcome = e.outcome(), "gameserver directory list failed");
                    NetError::Backend
                })?;
            for b in &page.items {
                if let Some(id) = b.metadata.get("server_id").and_then(|s| canonical_uuid(s)) {
                    // A duplicate (community, server id) is ambiguous: mark it unusable (-1), never
                    // "last one wins" -- fail closed.
                    let key = (b.community_id, id);
                    let value = if map.contains_key(&key) {
                        (-1, -1)
                    } else {
                        (b.connection_id, b.tenant_id)
                    };
                    map.insert(key, value);
                }
            }
            match page.next_after {
                Some(n) => after = n,
                None => {
                    if let Ok(mut c) = self.cache.lock() {
                        c.map = map;
                        c.fetched = Some(Instant::now());
                    }
                    return Ok(());
                }
            }
        }
        tracing::error!("gameserver directory list exceeded page cap");
        Err(NetError::Backend)
    }
}

impl<R: ConnectionResolve + 'static> ServerDirectory for HubServerDirectory<R> {
    fn lookup<'a>(
        &'a self,
        scope: &'a Scope,
        server: &'a str,
    ) -> DirFuture<'a, Result<ServerTarget, NetError>> {
        Box::pin(async move {
            let not_registered = NetError::Denied("not_registered");
            let id = canonical_uuid(server).ok_or(not_registered.clone())?;
            let mut hit = self.cached(scope.community_id, &id);
            if hit.is_none() && self.may_refresh() {
                self.refresh().await?;
                hit = self.cached(scope.community_id, &id);
            }
            let (connection_id, tenant_id) = hit.ok_or(not_registered.clone())?;
            if connection_id < 0 {
                return Err(not_registered);
            }
            if tenant_id != scope.tenant_id {
                return Err(not_registered);
            }
            let resolved = self
                .resolver
                .resolve(ConnectionRef {
                    community_id: scope.community_id,
                    connection_id,
                })
                .await
                .map_err(|e| {
                    tracing::error!(outcome = e.outcome(), "gameserver directory resolve failed");
                    NetError::Backend
                })?;
            if resolved.provider != "gameserver"
                || resolved.community_id != scope.community_id
                || resolved.connection_id != connection_id
            {
                return Err(NetError::Denied("not_registered"));
            }
            let ConnectionSecret::OAuthAccessToken { access_token } = &resolved.secret else {
                tracing::error!("gameserver connection has an unexpected secret shape");
                return Err(NetError::Backend);
            };
            let (host, port, password) = parse_sealed(access_token.expose()).ok_or_else(|| {
                tracing::error!("gameserver connection secret is malformed");
                NetError::Backend
            })?;
            Ok(ServerTarget {
                server_id: id,
                host,
                port,
                password,
            })
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn canonical_uuid_rejects_noncanonical_spellings() {
        let u = "123e4567-e89b-12d3-a456-426614174000";
        assert_eq!(canonical_uuid(u).as_deref(), Some(u));
        assert_eq!(
            canonical_uuid(&u.to_uppercase()).as_deref(),
            Some(u),
            "case folded"
        );
        for bad in [
            "",
            "1",
            "123e4567e89b12d3a456426614174000",
            "{123e4567-e89b-12d3-a456-426614174000}",
            "urn:uuid:123e4567-e89b-12d3-a456-426614174000",
            "10.0.0.1:27015",
        ] {
            assert!(canonical_uuid(bad).is_none(), "{bad}");
        }
    }

    #[test]
    fn sealed_parse_keeps_pipes_in_the_password() {
        let (h, p, pw) = parse_sealed("mc.example.com|25575|pa|ss|word").unwrap();
        assert_eq!(
            (h.as_str(), p, pw.expose()),
            ("mc.example.com", 25575, "pa|ss|word")
        );
        for bad in [
            "",
            "host",
            "host|abc|pw",
            "host|0|pw",
            "|25575|pw",
            "host|25575",
            "host|70000|pw",
        ] {
            assert!(parse_sealed(bad).is_none(), "{bad}");
        }
    }

    #[test]
    fn target_debug_redacts_secret_and_address() {
        let t = ServerTarget {
            server_id: "id".into(),
            host: "secret-host.example".into(),
            port: 27015,
            password: Secret::new("topsecret".into()),
        };
        let d = format!("{t:?}");
        assert!(!d.contains("secret-host") && !d.contains("topsecret") && !d.contains("27015"));
    }
}
