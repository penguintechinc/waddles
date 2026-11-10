//! BYOK key resolution. The plaintext key lives only in a zeroizing [`ApiKey`]
//! for one job, is never logged, labelled or returned across the guest boundary.

use std::sync::Arc;
use std::time::Duration;

use async_trait::async_trait;
use hub_client::bearer_request;
use hub_client::pb::media_service_client::MediaServiceClient;
use hub_client::pb::{ByokStatus, ResolveByokRequest};
use tonic::transport::Channel;
use zeroize::Zeroizing;

use crate::auth::BearerSource;
use crate::types::Scope;

/// Full gRPC method name (pinned by `tests/hub_contract.rs` against hub-api's servicer).
pub const BYOK_RESOLVE_METHOD: &str = "/waddles.hub.internal.v1.MediaService/ResolveByok";
/// Machine-JWT scope hub-api requires for [`BYOK_RESOLVE_METHOD`].
pub const BYOK_SCOPE: &str = "media:byok:resolve";

/// A provider API key; zeroized on drop, `Debug` redacted.
pub struct ApiKey(Zeroizing<String>);

impl ApiKey {
    /// Wraps a key.
    pub fn new(key: String) -> Self {
        Self(Zeroizing::new(key))
    }
    /// The raw key, for building the provider request header ONLY.
    pub fn expose(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Debug for ApiKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("ApiKey(<redacted>)")
    }
}

/// Why no key could be produced.
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum CredentialError {
    /// The community has no key for this provider.
    #[error("not configured")]
    NotConfigured,
    /// hub-api says the effective tier is below Professional.
    #[error("tier required")]
    TierRequired,
    /// hub-api unreachable / answered unexpectedly.
    #[error("credential resolver unavailable: {0}")]
    Unavailable(String),
}

/// Resolves the BYOK key for `(scope, provider)`.
#[async_trait]
pub trait CredentialResolver: Send + Sync {
    /// Fetches the key (called per job, never cached).
    async fn resolve(&self, scope: &Scope, provider: &str) -> Result<ApiKey, CredentialError>;
}

/// Production resolver: hub-api's internal gRPC `MediaService.ResolveByok`
/// `{tenant_id, community_id, provider}` (machine JWT in `authorization` metadata, scope
/// `media:byok:resolve`) -> `{status: OK|NOT_CONFIGURED|TIER_REQUIRED, tier, api_key?}`. Any gRPC
/// error status (invalid argument, unauthenticated, permission denied, not found, rate limited,
/// unavailable, internal) and any unknown/unspecified status fails closed as `Unavailable`.
///
/// The [`Channel`] is the process's long-lived, lazily re-dialling connection to hub-api
/// (`hub_client::lazy_channel`); the plaintext key is moved out of the response straight into a
/// zeroizing [`ApiKey`].
pub struct HubCredentialResolver {
    rpc: MediaServiceClient<Channel>,
    bearer: Arc<dyn BearerSource>,
    deadline: Duration,
}

impl HubCredentialResolver {
    /// `channel` from `hub_client::lazy_channel`; `deadline` per RPC (hub-api rejects > 5s).
    pub fn new(channel: Channel, bearer: Arc<dyn BearerSource>, deadline: Duration) -> Self {
        Self {
            rpc: MediaServiceClient::new(channel),
            bearer,
            deadline,
        }
    }
}

#[async_trait]
impl CredentialResolver for HubCredentialResolver {
    async fn resolve(&self, scope: &Scope, provider: &str) -> Result<ApiKey, CredentialError> {
        let token = self
            .bearer
            .bearer()
            .await
            .map_err(|e| CredentialError::Unavailable(format!("machine jwt: {e}")))?;
        let request = bearer_request(
            ResolveByokRequest {
                tenant_id: scope.tenant_id,
                community_id: i64::from(scope.community_id),
                provider: provider.to_string(),
            },
            token.as_ref(),
            self.deadline,
        )
        .map_err(|_| CredentialError::Unavailable("invalid token metadata".into()))?;
        let mut rpc = self.rpc.clone();
        let reply = rpc.resolve_byok(request).await.map_err(|status| {
            // Only the status CODE is reported -- never server text.
            CredentialError::Unavailable(format!("hub-api status {:?}", status.code()))
        })?;
        let mut body = reply.into_inner();
        match (ByokStatus::try_from(body.status), body.api_key.take()) {
            (Ok(ByokStatus::Ok), Some(k)) if !k.is_empty() => Ok(ApiKey::new(k)),
            (Ok(ByokStatus::Ok), _) => {
                Err(CredentialError::Unavailable("ok without api_key".into()))
            }
            (Ok(ByokStatus::NotConfigured), _) => Err(CredentialError::NotConfigured),
            (Ok(ByokStatus::TierRequired), _) => Err(CredentialError::TierRequired),
            _ => Err(CredentialError::Unavailable(
                "unknown resolver status".into(),
            )),
        }
    }
}
