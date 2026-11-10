//! Host-side credential sources and shared-credential quota for bundle
//! `http.send` secret refs (host-seams-2).
//!
//! A bundle names a *symbolic* secret ref per call (`spotify.oauth`); the
//! grant map ([`crate::egress::EgressRuleRow::granted_secret_refs`]) says what
//! that ref resolves FROM. The grant value is a [`GrantSource`]:
//!
//! | grant value               | source                                                          |
//! |---------------------------|-----------------------------------------------------------------|
//! | `NAME` / `env:NAME`       | process env (platform-default / shared app credential)          |
//! | `connection:<provider>`   | the INVOKING community's platform connection access token       |
//! | `connection:<p>#meta.<k>` | a non-secret metadata field of that connection (e.g. site id)   |
//! | `app:<name>`              | host-minted + cached OAuth client-credentials token             |
//!
//! Invariants (adversarial-review checklist): the community comes from the
//! host-verified invocation scope ([`CallScope`]) -- never a guest argument;
//! an unresolved ref is an explicit `secret_unresolved` denial (never empty,
//! never another community's credential, never an env fallback for a
//! `connection:` grant); resolved values never reach a log, a metric label or
//! the guest -- they exist only as the `String` handed to the guard, which
//! injects it into the outbound slot after the egress allowlist check and
//! scrubs it from everything returned.

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use base64::Engine;
use penguin_bundle_host::wire::HostResultError;
use serde::Deserialize;

/// Boxed future used by this module's object-safe async seams.
pub type BoxFut<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

fn denied(code: &str, message: impl Into<String>) -> HostResultError {
    HostResultError {
        code: code.to_string(),
        message: message.into(),
    }
}

/// The host-verified `(tenant, community)` of one invocation. `community_id`
/// `0` means "no community scope" (tenant-wide / system), for which every
/// `connection:` grant is refused.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct CallScope {
    pub tenant_id: i64,
    pub community_id: i64,
}

/// Which field of a community connection a `connection:` grant reads.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ConnField {
    /// The credential, formatted as a ready-to-inject slot value.
    Token,
    /// The raw secret value with NO scheme prefix (`#raw`): an API key for a
    /// query slot or a `Authorization: <key>` header.
    Raw,
    /// A non-secret metadata value (`#meta.<key>`).
    Meta(String),
    /// A named secret field of a multi-field credential (`#field.<name>`),
    /// e.g. Wix `siteId`.
    Field(String),
}

/// Where a granted secret ref resolves from. Parsed from the grant value.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GrantSource {
    Env(String),
    Connection {
        provider: String,
        field: ConnField,
    },
    App(String),
    /// `cc:<provider>`: the community's own `<provider>` connection holds an
    /// OAuth client id (metadata `client_id`) + client secret; the HOST does
    /// the client-credentials exchange and caches the token per community.
    ClientCredentials(String),
}

fn is_ident(s: &str, max: usize, extra: &[char]) -> bool {
    !s.is_empty()
        && s.len() <= max
        && s.chars()
            .all(|c| c.is_ascii_alphanumeric() || extra.contains(&c))
}

impl GrantSource {
    /// Parses a grant value; `None` for a malformed one (the caller denies).
    pub fn parse(value: &str) -> Option<Self> {
        if let Some(name) = value.strip_prefix("env:") {
            return is_ident(name, 128, &['_']).then(|| Self::Env(name.to_string()));
        }
        if let Some(rest) = value.strip_prefix("connection:") {
            let (provider, field) = match rest.split_once('#') {
                None => (rest, ConnField::Token),
                Some((p, "raw")) => (p, ConnField::Raw),
                Some((p, f)) => {
                    let (make, key): (fn(String) -> ConnField, &str) =
                        if let Some(k) = f.strip_prefix("meta.") {
                            (ConnField::Meta, k)
                        } else {
                            (ConnField::Field, f.strip_prefix("field.")?)
                        };
                    if !is_ident(key, 64, &['_', '-', '.']) {
                        return None;
                    }
                    (p, make(key.to_string()))
                }
            };
            return is_ident(provider, 32, &['_', '-']).then(|| Self::Connection {
                provider: provider.to_string(),
                field,
            });
        }
        if let Some(p) = value.strip_prefix("cc:") {
            return is_ident(p, 32, &['_', '-']).then(|| Self::ClientCredentials(p.to_string()));
        }
        if let Some(name) = value.strip_prefix("app:") {
            return is_ident(name, 64, &['_', '-', '.']).then(|| Self::App(name.to_string()));
        }
        if value.contains(':') {
            return None;
        }
        is_ident(value, 128, &['_']).then(|| Self::Env(value.to_string()))
    }
}

/// Why a credential could not be produced. Fixed codes only, never content.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CredentialError {
    /// The community has no usable connection for the provider / no such field.
    NotConnected,
    /// The credential source is down or not configured (retryable / operator).
    Unavailable,
}

impl CredentialError {
    fn into_host(self, secret_ref: &str) -> HostResultError {
        match self {
            Self::NotConnected => denied(
                "secret_unresolved",
                format!("secret reference {secret_ref:?} has no credential for this community"),
            ),
            Self::Unavailable => denied(
                "secret_unavailable",
                format!("secret reference {secret_ref:?} credential source is unavailable"),
            ),
        }
    }
}

/// Resolves a community connection field to a ready-to-inject slot value.
/// Implemented over hub-api's connection resolver (`bundle_host_credentials`).
pub trait ConnectionCredentials: Send + Sync {
    fn credential<'a>(
        &'a self,
        community_id: i64,
        provider: &'a str,
        field: &'a ConnField,
    ) -> BoxFut<'a, Result<String, CredentialError>>;

    /// `(client_id, client_secret)` held by the community's `provider`
    /// connection (id from metadata `client_id`, secret = the credential).
    /// Default: not supported by this source.
    fn client_pair<'a>(
        &'a self,
        _community_id: i64,
        _provider: &'a str,
    ) -> BoxFut<'a, Result<(String, String), CredentialError>> {
        Box::pin(async { Err(CredentialError::Unavailable) })
    }
}

/// How an OAuth client-credentials app authenticates to its token endpoint.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ClientAuth {
    /// `Authorization: Basic base64(id:secret)` (Spotify).
    BasicHeader,
    /// `client_id` / `client_secret` form fields (SoundCloud, Twitch).
    Form,
}

/// One host-minted application credential (`app:<name>`). The client id and
/// secret are read from the named env vars at mint time; the token URL is
/// operator config, never bundle input.
#[derive(Debug, Clone)]
pub struct AppCredentialConfig {
    pub token_url: String,
    pub client_id_env: String,
    pub client_secret_env: String,
    /// Header scheme prefixed to the token (`Bearer`, `OAuth`); empty returns
    /// the raw token.
    pub scheme: String,
    pub auth: ClientAuth,
}

struct Cached {
    value: String,
    valid_until: Instant,
    /// Hash of the client id/secret the token was minted with (0 for env apps).
    fingerprint: u64,
}

/// Mints and caches OAuth client-credentials tokens host-side, one
/// single-flight slot per app so a burst mints once.
pub struct AppTokenMinter {
    configs: HashMap<String, AppCredentialConfig>,
    slots: Mutex<HashMap<String, Arc<tokio::sync::Mutex<Option<Cached>>>>>,
    http: reqwest::Client,
    allow_loopback_http: bool,
}

#[derive(Deserialize)]
struct TokenReply {
    access_token: String,
    #[serde(default)]
    expires_in: Option<u64>,
}

/// Safety margin: a token is re-minted this long before it expires.
const TOKEN_SKEW: Duration = Duration::from_secs(60);
/// Lifetime assumed when the token endpoint reports none (never longer).
const TOKEN_DEFAULT_TTL: Duration = Duration::from_secs(300);

impl AppTokenMinter {
    /// Builds a minter; every token URL must be `https://` with a hostname.
    pub fn new(configs: HashMap<String, AppCredentialConfig>) -> Result<Self, String> {
        for (name, c) in &configs {
            let url = reqwest::Url::parse(&c.token_url)
                .map_err(|_| format!("app credential {name:?}: token_url does not parse"))?;
            if url.scheme() != "https" || url.host_str().is_none() {
                return Err(format!("app credential {name:?}: token_url must be https"));
            }
            if let Some(ip) = url
                .host_str()
                .and_then(|h| h.trim_matches(['[', ']']).parse::<std::net::IpAddr>().ok())
            {
                if crate::egress::is_forbidden_address(ip, false).is_some() {
                    return Err(format!(
                        "app credential {name:?}: token_url address forbidden"
                    ));
                }
            }
        }
        let http = reqwest::Client::builder()
            .timeout(Duration::from_secs(10))
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .map_err(|e| format!("token http client: {e}"))?;
        Ok(Self {
            configs,
            slots: Mutex::new(HashMap::new()),
            http,
            allow_loopback_http: false,
        })
    }

    /// TEST ONLY: allows an `http://127.0.0.1` token URL so a real local fake
    /// token endpoint can be exercised. Production wiring never calls this.
    #[doc(hidden)]
    pub fn new_for_loopback_tests(
        configs: HashMap<String, AppCredentialConfig>,
    ) -> Result<Self, String> {
        let http = reqwest::Client::builder()
            .timeout(Duration::from_secs(10))
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .map_err(|e| format!("token http client: {e}"))?;
        Ok(Self {
            configs,
            slots: Mutex::new(HashMap::new()),
            http,
            allow_loopback_http: true,
        })
    }

    /// The formatted slot value (`<scheme> <token>`) for app credential `name`.
    pub async fn token(&self, name: &str) -> Result<String, CredentialError> {
        self.cached(name, name, None).await
    }

    /// Token for `name`'s endpoint using a COMMUNITY's own client id/secret
    /// (`cc:` grants), cached under `cache_key` (include the community) and
    /// invalidated if the secret changes (rotation).
    pub async fn token_for_client(
        &self,
        name: &str,
        cache_key: &str,
        client_id: &str,
        client_secret: &str,
    ) -> Result<String, CredentialError> {
        self.cached(name, cache_key, Some((client_id, client_secret)))
            .await
    }

    async fn cached(
        &self,
        name: &str,
        key: &str,
        pair: Option<(&str, &str)>,
    ) -> Result<String, CredentialError> {
        use std::hash::{Hash, Hasher};
        let cfg = self.configs.get(name).ok_or(CredentialError::Unavailable)?;
        let fingerprint = pair.map_or(0, |p| {
            let mut h = std::collections::hash_map::DefaultHasher::new();
            p.hash(&mut h);
            h.finish()
        });
        let slot = {
            let mut slots = self.slots.lock().unwrap_or_else(|e| e.into_inner());
            Arc::clone(slots.entry(key.to_string()).or_default())
        };
        let mut guard = slot.lock().await;
        if let Some(c) = guard.as_ref() {
            if Instant::now() < c.valid_until && c.fingerprint == fingerprint {
                return Ok(c.value.clone());
            }
        }
        let (id, secret) = match pair {
            Some((i, s)) => (i.to_string(), s.to_string()),
            None => (
                std::env::var(&cfg.client_id_env).map_err(|_| CredentialError::Unavailable)?,
                std::env::var(&cfg.client_secret_env).map_err(|_| CredentialError::Unavailable)?,
            ),
        };
        let mut fresh = self.mint(name, cfg, &id, &secret).await?;
        fresh.fingerprint = fingerprint;
        let value = fresh.value.clone();
        *guard = Some(fresh);
        Ok(value)
    }

    async fn mint(
        &self,
        name: &str,
        cfg: &AppCredentialConfig,
        id: &str,
        secret: &str,
    ) -> Result<Cached, CredentialError> {
        if !self.allow_loopback_http && !cfg.token_url.starts_with("https://") {
            return Err(CredentialError::Unavailable);
        }
        if id.is_empty() || secret.is_empty() {
            return Err(CredentialError::Unavailable);
        }
        let mut req = self.http.post(&cfg.token_url);
        req = match cfg.auth {
            ClientAuth::BasicHeader => {
                let basic =
                    base64::engine::general_purpose::STANDARD.encode(format!("{id}:{secret}"));
                req.header("Authorization", format!("Basic {basic}"))
                    .form(&[("grant_type", "client_credentials")])
            }
            ClientAuth::Form => req.form(&[
                ("grant_type", "client_credentials"),
                ("client_id", id),
                ("client_secret", secret),
            ]),
        };
        let resp = req.send().await.map_err(|_| {
            tracing::warn!(app_credential = name, "app token mint request failed");
            CredentialError::Unavailable
        })?;
        if !resp.status().is_success() {
            tracing::warn!(
                app_credential = name,
                status = resp.status().as_u16(),
                "app token mint refused"
            );
            return Err(CredentialError::Unavailable);
        }
        let reply: TokenReply = resp
            .json()
            .await
            .map_err(|_| CredentialError::Unavailable)?;
        if reply.access_token.is_empty() {
            return Err(CredentialError::Unavailable);
        }
        let ttl = reply
            .expires_in
            .map_or(TOKEN_DEFAULT_TTL, Duration::from_secs)
            .saturating_sub(TOKEN_SKEW);
        Ok(Cached {
            value: if cfg.scheme.is_empty() {
                // Raw token: the caller's request template supplies any
                // `Bearer ` prefix itself (sender_exec slots).
                reply.access_token
            } else {
                format!("{} {}", cfg.scheme, reply.access_token)
            },
            valid_until: Instant::now() + ttl,
            fingerprint: 0,
        })
    }
}

/// Budget for one shared credential, keyed by its secret-ref name.
#[derive(Debug, Clone, Copy, PartialEq, Default)]
pub struct QuotaPolicy {
    /// Global calls per UTC minute.
    pub per_minute: Option<u64>,
    /// Global calls per UTC day.
    pub per_day: Option<u64>,
    /// Fraction (0, 1] of `per_day` one community may consume.
    pub community_day_fraction: Option<f64>,
}

impl QuotaPolicy {
    /// `None` when the policy is internally inconsistent (zero / bad fraction).
    pub fn validate(self) -> Option<Self> {
        let frac_ok = self
            .community_day_fraction
            .is_none_or(|f| f.is_finite() && f > 0.0 && f <= 1.0);
        let nonzero = self.per_minute != Some(0) && self.per_day != Some(0);
        let share_needs_day = self.community_day_fraction.is_none() || self.per_day.is_some();
        (frac_ok && nonzero && share_needs_day).then_some(self)
    }

    fn community_day_limit(&self) -> Option<u64> {
        let (frac, day) = (self.community_day_fraction?, self.per_day?);
        Some(((day as f64 * frac).floor() as u64).max(1))
    }
}

/// Which budget a refused charge hit.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum QuotaScope {
    Minute,
    Day,
    Community,
}

/// Outcome of one atomic charge attempt.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum QuotaVerdict {
    Allowed,
    Exhausted(QuotaScope),
}

/// The store backing the counters (Valkey in production). The charge is
/// atomic: it either increments every applicable counter or none.
pub trait QuotaStore: Send + Sync {
    fn charge<'a>(&'a self, charge: &'a QuotaCharge) -> BoxFut<'a, Result<QuotaVerdict, ()>>;
}

/// One atomic charge request.
#[derive(Debug, Clone)]
pub struct QuotaCharge {
    pub secret_ref: String,
    pub community_id: i64,
    pub per_minute: Option<u64>,
    pub per_day: Option<u64>,
    pub community_day: Option<u64>,
    pub epoch_secs: u64,
}

/// In-process [`QuotaStore`] (single replica / tests). Production multi-replica
/// deployments use the Valkey store so the budget is genuinely global.
#[derive(Default)]
pub struct MemoryQuotaStore {
    counters: Mutex<HashMap<String, u64>>,
}

impl QuotaStore for MemoryQuotaStore {
    fn charge<'a>(&'a self, c: &'a QuotaCharge) -> BoxFut<'a, Result<QuotaVerdict, ()>> {
        Box::pin(async move {
            let minute = format!("{}|m|{}", c.secret_ref, c.epoch_secs / 60);
            let day = format!("{}|d|{}", c.secret_ref, c.epoch_secs / 86_400);
            let comm = format!(
                "{}|c{}|{}",
                c.secret_ref,
                c.community_id,
                c.epoch_secs / 86_400
            );
            let mut map = self.counters.lock().unwrap_or_else(|e| e.into_inner());
            // Drop counters from past windows so the map stays bounded.
            let (now_m, now_d) = (c.epoch_secs / 60, c.epoch_secs / 86_400);
            map.retain(
                |k, _| match k.rsplit('|').next().and_then(|w| w.parse::<u64>().ok()) {
                    Some(w) if k.contains("|m|") => w >= now_m,
                    Some(w) => w >= now_d,
                    None => false,
                },
            );
            let get = |m: &HashMap<String, u64>, k: &String| m.get(k).copied().unwrap_or(0);
            if c.per_minute.is_some_and(|l| get(&map, &minute) >= l) {
                return Ok(QuotaVerdict::Exhausted(QuotaScope::Minute));
            }
            if c.per_day.is_some_and(|l| get(&map, &day) >= l) {
                return Ok(QuotaVerdict::Exhausted(QuotaScope::Day));
            }
            if c.community_day.is_some_and(|l| get(&map, &comm) >= l) {
                return Ok(QuotaVerdict::Exhausted(QuotaScope::Community));
            }
            for (k, on) in [
                (minute, c.per_minute.is_some()),
                (day, c.per_day.is_some()),
                (comm, c.community_day.is_some()),
            ] {
                if on {
                    *map.entry(k).or_insert(0) += 1;
                }
            }
            Ok(QuotaVerdict::Allowed)
        })
    }
}

/// Per-secret-ref budgets over a [`QuotaStore`]. A ref with no policy is not
/// metered; a metered ref whose store is unreachable fails CLOSED.
pub struct QuotaGuard {
    policies: HashMap<String, QuotaPolicy>,
    store: Arc<dyn QuotaStore>,
}

impl QuotaGuard {
    /// Builds a guard; an invalid policy is a construction error (loud).
    pub fn new(
        policies: HashMap<String, QuotaPolicy>,
        store: Arc<dyn QuotaStore>,
    ) -> Result<Self, String> {
        for (name, p) in &policies {
            if p.validate().is_none() {
                return Err(format!("quota policy for {name:?} is invalid"));
            }
        }
        Ok(Self { policies, store })
    }

    /// Whether `secret_ref` is metered.
    pub fn is_metered(&self, secret_ref: &str) -> bool {
        self.policies.contains_key(secret_ref)
    }

    /// Charges one call against `secret_ref` for `community_id`.
    pub async fn charge(&self, secret_ref: &str, community_id: i64) -> Result<(), HostResultError> {
        let Some(p) = self.policies.get(secret_ref) else {
            return Ok(());
        };
        let epoch_secs = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0, |d| d.as_secs());
        let charge = QuotaCharge {
            secret_ref: secret_ref.to_string(),
            community_id,
            per_minute: p.per_minute,
            per_day: p.per_day,
            community_day: p.community_day_limit(),
            epoch_secs,
        };
        match self.store.charge(&charge).await {
            Ok(QuotaVerdict::Allowed) => Ok(()),
            Ok(QuotaVerdict::Exhausted(scope)) => Err(denied(
                "quota_exhausted",
                format!("shared credential {secret_ref:?} budget exhausted ({scope:?})"),
            )),
            Err(()) => Err(denied(
                "quota_unavailable",
                format!("quota store unavailable for metered credential {secret_ref:?}"),
            )),
        }
    }
}

/// The non-env credential sources + the quota guard the egress guard consults.
#[derive(Default, Clone)]
pub struct CredentialSources {
    pub connections: Option<Arc<dyn ConnectionCredentials>>,
    pub apps: Option<Arc<AppTokenMinter>>,
    pub quota: Option<Arc<QuotaGuard>>,
}

impl CredentialSources {
    /// Resolves a non-env grant for `scope`. Env grants are resolved by the
    /// guard's own broker; this never falls back to env.
    pub async fn resolve(
        &self,
        scope: &CallScope,
        secret_ref: &str,
        source: &GrantSource,
    ) -> Result<String, HostResultError> {
        match source {
            GrantSource::Env(_) => Err(denied("secret_unresolved", "env grant routed to broker")),
            GrantSource::Connection { provider, field } => {
                if scope.community_id < 1 {
                    return Err(CredentialError::NotConnected.into_host(secret_ref));
                }
                let conns = self
                    .connections
                    .as_ref()
                    .ok_or_else(|| CredentialError::Unavailable.into_host(secret_ref))?;
                conns
                    .credential(scope.community_id, provider, field)
                    .await
                    .map_err(|e| e.into_host(secret_ref))
            }
            GrantSource::ClientCredentials(provider) => {
                if scope.community_id < 1 {
                    return Err(CredentialError::NotConnected.into_host(secret_ref));
                }
                let conns = self
                    .connections
                    .as_ref()
                    .ok_or_else(|| CredentialError::Unavailable.into_host(secret_ref))?;
                let apps = self
                    .apps
                    .as_ref()
                    .ok_or_else(|| CredentialError::Unavailable.into_host(secret_ref))?;
                let (id, secret) = conns
                    .client_pair(scope.community_id, provider)
                    .await
                    .map_err(|e| e.into_host(secret_ref))?;
                let key = format!("{provider}|c{}", scope.community_id);
                apps.token_for_client(provider, &key, &id, &secret)
                    .await
                    .map_err(|e| e.into_host(secret_ref))
            }
            GrantSource::App(name) => {
                let apps = self
                    .apps
                    .as_ref()
                    .ok_or_else(|| CredentialError::Unavailable.into_host(secret_ref))?;
                apps.token(name).await.map_err(|e| e.into_host(secret_ref))
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn grant_source_grammar() {
        assert_eq!(
            GrantSource::parse("TOKEN_X"),
            Some(GrantSource::Env("TOKEN_X".into()))
        );
        assert_eq!(
            GrantSource::parse("env:TOKEN_X"),
            Some(GrantSource::Env("TOKEN_X".into()))
        );
        assert_eq!(
            GrantSource::parse("connection:spotify"),
            Some(GrantSource::Connection {
                provider: "spotify".into(),
                field: ConnField::Token
            })
        );
        assert_eq!(
            GrantSource::parse("connection:wix#meta.site_id"),
            Some(GrantSource::Connection {
                provider: "wix".into(),
                field: ConnField::Meta("site_id".into())
            })
        );
        assert_eq!(
            GrantSource::parse("connection:youtube_api#raw"),
            Some(GrantSource::Connection {
                provider: "youtube_api".into(),
                field: ConnField::Raw
            })
        );
        assert_eq!(
            GrantSource::parse("connection:wix#field.siteId"),
            Some(GrantSource::Connection {
                provider: "wix".into(),
                field: ConnField::Field("siteId".into())
            })
        );
        assert_eq!(
            GrantSource::parse("cc:soundcloud"),
            Some(GrantSource::ClientCredentials("soundcloud".into()))
        );
        assert_eq!(GrantSource::parse("cc:"), None);
        assert_eq!(GrantSource::parse("connection:wix#field."), None);
        assert_eq!(
            GrantSource::parse("app:spotify.app"),
            Some(GrantSource::App("spotify.app".into()))
        );
        for bad in [
            "",
            "foo:bar",
            "connection:",
            "connection:a#site",
            "connection:a#meta.",
            "app:",
            "env:",
            "a b",
            "connection:A B",
        ] {
            assert_eq!(GrantSource::parse(bad), None, "{bad:?}");
        }
    }

    #[test]
    fn quota_policy_validation() {
        assert!(QuotaPolicy {
            per_minute: Some(1),
            per_day: Some(10),
            community_day_fraction: Some(0.5)
        }
        .validate()
        .is_some());
        assert!(QuotaPolicy {
            per_minute: Some(0),
            ..Default::default()
        }
        .validate()
        .is_none());
        assert!(QuotaPolicy {
            community_day_fraction: Some(0.5),
            ..Default::default()
        }
        .validate()
        .is_none());
        assert!(QuotaPolicy {
            per_day: Some(5),
            community_day_fraction: Some(1.5),
            ..Default::default()
        }
        .validate()
        .is_none());
        assert!(QuotaPolicy {
            per_day: Some(5),
            community_day_fraction: Some(f64::NAN),
            ..Default::default()
        }
        .validate()
        .is_none());
    }

    fn guard(p: QuotaPolicy) -> QuotaGuard {
        QuotaGuard::new(
            HashMap::from([("r".to_string(), p)]),
            Arc::new(MemoryQuotaStore::default()),
        )
        .unwrap()
    }

    #[tokio::test]
    async fn global_day_budget_is_shared_across_communities_then_exhausts() {
        let g = guard(QuotaPolicy {
            per_day: Some(3),
            ..Default::default()
        });
        for c in [1, 2, 1] {
            g.charge("r", c).await.unwrap();
        }
        assert_eq!(g.charge("r", 3).await.unwrap_err().code, "quota_exhausted");
        // Unmetered refs are never limited.
        g.charge("other", 1).await.unwrap();
    }

    #[tokio::test]
    async fn per_community_share_caps_one_community_not_others() {
        let g = guard(QuotaPolicy {
            per_day: Some(10),
            community_day_fraction: Some(0.2),
            ..Default::default()
        });
        g.charge("r", 1).await.unwrap();
        g.charge("r", 1).await.unwrap();
        assert_eq!(g.charge("r", 1).await.unwrap_err().code, "quota_exhausted");
        g.charge("r", 2).await.unwrap();
    }

    #[tokio::test]
    async fn refused_charge_does_not_consume_other_counters() {
        let store = MemoryQuotaStore::default();
        let c = |comm| QuotaCharge {
            secret_ref: "r".into(),
            community_id: comm,
            per_minute: Some(100),
            per_day: Some(100),
            community_day: Some(1),
            epoch_secs: 1_000_000,
        };
        assert_eq!(store.charge(&c(1)).await.unwrap(), QuotaVerdict::Allowed);
        assert_eq!(
            store.charge(&c(1)).await.unwrap(),
            QuotaVerdict::Exhausted(QuotaScope::Community)
        );
        // Refusal above must not have bumped the global counters: 98 more remain.
        let mut ok = 0;
        for i in 2..200 {
            let mut x = c(i);
            x.community_day = Some(5);
            if store.charge(&x).await.unwrap() == QuotaVerdict::Allowed {
                ok += 1;
            }
        }
        assert_eq!(ok, 99);
    }

    struct DownStore;
    impl QuotaStore for DownStore {
        fn charge<'a>(&'a self, _: &'a QuotaCharge) -> BoxFut<'a, Result<QuotaVerdict, ()>> {
            Box::pin(async { Err(()) })
        }
    }

    #[tokio::test]
    async fn metered_ref_fails_closed_when_store_is_down() {
        let g = QuotaGuard::new(
            HashMap::from([(
                "r".to_string(),
                QuotaPolicy {
                    per_day: Some(1),
                    ..Default::default()
                },
            )]),
            Arc::new(DownStore),
        )
        .unwrap();
        assert_eq!(
            g.charge("r", 1).await.unwrap_err().code,
            "quota_unavailable"
        );
    }

    #[tokio::test]
    async fn connection_grant_without_community_or_source_is_denied() {
        let s = CredentialSources::default();
        let src = GrantSource::parse("connection:spotify").unwrap();
        let none = CallScope {
            tenant_id: 1,
            community_id: 0,
        };
        assert_eq!(
            s.resolve(&none, "spotify.oauth", &src)
                .await
                .unwrap_err()
                .code,
            "secret_unresolved"
        );
        let scoped = CallScope {
            tenant_id: 1,
            community_id: 5,
        };
        assert_eq!(
            s.resolve(&scoped, "spotify.oauth", &src)
                .await
                .unwrap_err()
                .code,
            "secret_unavailable"
        );
    }

    #[test]
    fn minter_rejects_non_https_and_forbidden_ip_token_urls() {
        let cfg = |u: &str| {
            HashMap::from([(
                "a".to_string(),
                AppCredentialConfig {
                    token_url: u.into(),
                    client_id_env: "X".into(),
                    client_secret_env: "Y".into(),
                    scheme: "Bearer".into(),
                    auth: ClientAuth::Form,
                },
            )])
        };
        assert!(AppTokenMinter::new(cfg("http://accounts.spotify.com/api/token")).is_err());
        assert!(AppTokenMinter::new(cfg("https://169.254.169.254/token")).is_err());
        assert!(AppTokenMinter::new(cfg("https://accounts.spotify.com/api/token")).is_ok());
    }
}
