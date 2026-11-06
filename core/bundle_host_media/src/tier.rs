//! Tier gate. Effective tier = `max(tenant, community)` (cascading down),
//! resolved host-side through a [`TierResolver`] seam. **Fail closed**: an
//! unresolvable tier with no last-known value inside the grace window is a
//! refusal, never "allowed".

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use async_trait::async_trait;

use crate::auth::BearerSource;
use crate::types::{MediaError, Scope, Tier, LICENSE_UNKNOWN};

/// Why a tier could not be established.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum TierError {
    /// The resolver answered but the state is unknown / unparseable.
    #[error("license state unknown")]
    Unknown,
    /// The resolver could not be reached.
    #[error("tier resolver unavailable: {0}")]
    Unavailable(String),
}

/// Reads the effective tier for a scope.
#[async_trait]
pub trait TierResolver: Send + Sync {
    /// Effective tier (`max(tenant, community)`).
    async fn effective_tier(&self, scope: &Scope) -> Result<Tier, TierError>;
}

/// `max(tenant, community)`: a community lifts a Free tenant; a Free community
/// never lowers a paid tenant.
pub fn effective(tenant: Tier, community: Tier) -> Tier {
    tenant.max(community)
}

/// The gate itself: Professional or higher, else `tier-required`; an unresolvable
/// tier is `tier-required(license_unknown)`.
pub async fn require_professional(
    resolver: &dyn TierResolver,
    scope: &Scope,
) -> Result<Tier, MediaError> {
    match resolver.effective_tier(scope).await {
        Ok(t) if t >= Tier::Professional => Ok(t),
        Ok(_) => Err(MediaError::TierRequired("professional".into())),
        Err(e) => {
            tracing::warn!(error = %e, "media tier unresolved; refusing (fail closed)");
            Err(MediaError::TierRequired(LICENSE_UNKNOWN.into()))
        }
    }
}

/// Production resolver: hub-api
/// `GET /api/v1/internal/media/tier?tenant_id=&community_id=` (machine JWT) ->
/// `{"tenant_tier","community_tier"}`; the `max` is taken HERE. Last-known values
/// are served within `grace` when hub-api is unreachable; a never-seen scope with
/// hub-api down is refused.
pub struct HubTierResolver {
    http: reqwest::Client,
    base_url: String,
    bearer: Arc<dyn BearerSource>,
    fresh_ttl: Duration,
    grace: Duration,
    cache: Mutex<HashMap<(i64, i32), (Tier, Instant)>>,
}

impl HubTierResolver {
    /// `base_url` like `http://hub-api:8204` (no trailing slash needed).
    pub fn new(
        http: reqwest::Client,
        base_url: impl Into<String>,
        bearer: Arc<dyn BearerSource>,
        fresh_ttl: Duration,
        grace: Duration,
    ) -> Self {
        Self {
            http,
            base_url: base_url.into().trim_end_matches('/').to_string(),
            bearer,
            fresh_ttl,
            grace,
            cache: Mutex::new(HashMap::new()),
        }
    }

    fn cached(&self, key: (i64, i32), max_age: Duration) -> Result<Option<Tier>, TierError> {
        let guard = match self.cache.lock() {
            Ok(g) => g,
            Err(_) => return Err(TierError::Unavailable("tier cache poisoned".into())),
        };
        Ok(guard
            .get(&key)
            .and_then(|(t, at)| (at.elapsed() <= max_age).then_some(*t)))
    }

    fn store(&self, key: (i64, i32), tier: Tier) {
        if let Ok(mut g) = self.cache.lock() {
            g.insert(key, (tier, Instant::now()));
        }
    }

    async fn fetch(&self, scope: &Scope) -> Result<Tier, TierError> {
        #[derive(serde::Deserialize)]
        struct Body {
            tenant_tier: String,
            community_tier: String,
        }
        let token = self
            .bearer
            .bearer()
            .await
            .map_err(|e| TierError::Unavailable(format!("machine jwt: {e}")))?;
        let url = format!("{}/api/v1/internal/media/tier", self.base_url);
        let resp = self
            .http
            .get(url)
            .query(&[
                ("tenant_id", scope.tenant_id.to_string()),
                ("community_id", scope.community_id.to_string()),
            ])
            .bearer_auth(token.as_ref())
            .send()
            .await
            .map_err(|e| TierError::Unavailable(format!("request failed: {}", e.without_url())))?;
        if !resp.status().is_success() {
            return Err(TierError::Unavailable(format!(
                "hub-api status {}",
                resp.status().as_u16()
            )));
        }
        let body: Body = resp.json().await.map_err(|_| TierError::Unknown)?;
        match (
            Tier::parse(&body.tenant_tier),
            Tier::parse(&body.community_tier),
        ) {
            (Some(t), Some(c)) => Ok(effective(t, c)),
            _ => Err(TierError::Unknown),
        }
    }
}

#[async_trait]
impl TierResolver for HubTierResolver {
    async fn effective_tier(&self, scope: &Scope) -> Result<Tier, TierError> {
        let key = (scope.tenant_id, scope.community_id);
        if let Some(t) = self.cached(key, self.fresh_ttl)? {
            return Ok(t);
        }
        match self.fetch(scope).await {
            Ok(t) => {
                self.store(key, t);
                Ok(t)
            }
            Err(TierError::Unknown) => Err(TierError::Unknown),
            Err(e) => match self.cached(key, self.grace)? {
                Some(t) => {
                    tracing::warn!(error = %e, "hub-api tier lookup failed; serving last-known tier within grace");
                    Ok(t)
                }
                None => Err(e),
            },
        }
    }
}
