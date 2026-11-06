//! BYOK key resolution. The plaintext key lives only in a zeroizing [`ApiKey`]
//! for one job, is never logged, labelled or returned across the guest boundary.

use std::sync::Arc;

use async_trait::async_trait;
use zeroize::Zeroizing;

use crate::auth::BearerSource;
use crate::types::Scope;

/// hub-api path (pinned by `tests/hub_contract.rs` against the Python blueprint).
pub const BYOK_RESOLVE_PATH: &str = "/api/v1/internal/media/byok/resolve";
/// Machine-JWT scope hub-api requires for [`BYOK_RESOLVE_PATH`].
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

/// Production resolver: hub-api `POST /api/v1/internal/media/byok/resolve`
/// `{tenant_id, community_id, provider}` (machine JWT, scope `media:byok:resolve`)
/// -> 200 `{status: ok|not_configured|tier_required, tier, api_key|null}`; any
/// non-200 (400/401/403/404/429/503/500) and any unknown status fails closed.
pub struct HubCredentialResolver {
    http: reqwest::Client,
    base_url: String,
    bearer: Arc<dyn BearerSource>,
}

impl HubCredentialResolver {
    /// `base_url` like `http://hub-api:8204`.
    pub fn new(
        http: reqwest::Client,
        base_url: impl Into<String>,
        bearer: Arc<dyn BearerSource>,
    ) -> Self {
        Self {
            http,
            base_url: base_url.into().trim_end_matches('/').to_string(),
            bearer,
        }
    }
}

#[async_trait]
impl CredentialResolver for HubCredentialResolver {
    async fn resolve(&self, scope: &Scope, provider: &str) -> Result<ApiKey, CredentialError> {
        #[derive(serde::Deserialize)]
        struct Reply {
            status: String,
            api_key: Option<String>,
        }
        let token = self
            .bearer
            .bearer()
            .await
            .map_err(|e| CredentialError::Unavailable(format!("machine jwt: {e}")))?;
        let resp = self
            .http
            .post(format!("{}{}", self.base_url, BYOK_RESOLVE_PATH))
            .bearer_auth(token.as_ref())
            .json(&serde_json::json!({
                "tenant_id": scope.tenant_id,
                "community_id": scope.community_id,
                "provider": provider,
            }))
            .send()
            .await
            .map_err(|e| {
                CredentialError::Unavailable(format!("request failed: {}", e.without_url()))
            })?;
        let status = resp.status();
        if !status.is_success() {
            return Err(CredentialError::Unavailable(format!(
                "hub-api status {}",
                status.as_u16()
            )));
        }
        let body: Reply = resp
            .json()
            .await
            .map_err(|_| CredentialError::Unavailable("malformed resolver response".into()))?;
        match (body.status.as_str(), body.api_key) {
            ("ok", Some(k)) if !k.is_empty() => Ok(ApiKey::new(k)),
            ("ok", _) => Err(CredentialError::Unavailable("ok without api_key".into())),
            ("not_configured", _) => Err(CredentialError::NotConfigured),
            ("tier_required", _) => Err(CredentialError::TierRequired),
            _ => Err(CredentialError::Unavailable(
                "unknown resolver status".into(),
            )),
        }
    }
}
