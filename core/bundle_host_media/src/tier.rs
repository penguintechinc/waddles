//! Tier gate. Effective tier = `max(tenant, community)` (cascading down),
//! resolved host-side through a [`TierResolver`] seam. **Fail closed**: an
//! unresolvable tier with no last-known value inside the grace window is a
//! refusal, never "allowed".

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use async_trait::async_trait;
use hub_client::bearer_request;
use hub_client::pb::media_service_client::MediaServiceClient;
use hub_client::pb::{GetEffectiveTierRequest, MediaTier};
use tonic::transport::Channel;

use crate::auth::BearerSource;
use crate::types::{MediaError, Scope, Tier, LICENSE_UNKNOWN};

/// Full gRPC method name (pinned by `tests/hub_contract.rs` against hub-api's servicer).
pub const EFFECTIVE_TIER_METHOD: &str = "/waddles.hub.internal.v1.MediaService/GetEffectiveTier";
/// Machine-JWT scope hub-api requires for [`EFFECTIVE_TIER_METHOD`].
pub const TIER_SCOPE: &str = "media:tier:resolve";

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

/// Production resolver: hub-api's internal gRPC `MediaService.GetEffectiveTier`
/// `{tenant_id, community_id}` (machine JWT in metadata, scope `media:tier:resolve`) -> the
/// tier enum, already max(tenant, community) and fail-closed to `free` hub-side
/// (`NOT_FOUND` = community not in tenant -> refused). Last-known values are served within
/// `grace` when hub-api is unreachable; a never-seen scope with hub-api down is refused.
/// The [`Channel`] is the process's long-lived, lazily re-dialling connection to hub-api.
pub struct HubTierResolver {
    rpc: MediaServiceClient<Channel>,
    bearer: Arc<dyn BearerSource>,
    deadline: Duration,
    fresh_ttl: Duration,
    grace: Duration,
    cache: Mutex<HashMap<(i64, i32), (Tier, Instant)>>,
}

impl HubTierResolver {
    /// `channel` from `hub_client::lazy_channel`; `deadline` per RPC (hub-api rejects > 5s).
    pub fn new(
        channel: Channel,
        bearer: Arc<dyn BearerSource>,
        deadline: Duration,
        fresh_ttl: Duration,
        grace: Duration,
    ) -> Self {
        Self {
            rpc: MediaServiceClient::new(channel),
            bearer,
            deadline,
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
        let token = self
            .bearer
            .bearer()
            .await
            .map_err(|e| TierError::Unavailable(format!("machine jwt: {e}")))?;
        let request = bearer_request(
            GetEffectiveTierRequest {
                tenant_id: scope.tenant_id,
                community_id: i64::from(scope.community_id),
            },
            token.as_ref(),
            self.deadline,
        )
        .map_err(|_| TierError::Unavailable("invalid token metadata".into()))?;
        let mut rpc = self.rpc.clone();
        let reply = rpc.get_effective_tier(request).await.map_err(|status| {
            // Only the status CODE is reported -- never server text.
            TierError::Unavailable(format!("hub-api status {:?}", status.code()))
        })?;
        match MediaTier::try_from(reply.into_inner().tier) {
            Ok(MediaTier::Free) => Ok(Tier::Free),
            Ok(MediaTier::Professional) => Ok(Tier::Professional),
            Ok(MediaTier::Enterprise) => Ok(Tier::Enterprise),
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
