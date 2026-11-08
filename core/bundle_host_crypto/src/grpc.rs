//! gRPC transport for the tenant-DEK broker (feature `grpc`).
//!
//! Intra-cluster calls are gRPC (rules/backend.md): this adapts
//! `hub_client::HubClient` (tonic channel with TLS and CA pinning,
//! service-JWT metadata, per-RPC deadline, retries, circuit breaker) to the
//! transport-agnostic [`DekBroker`] trait. The connection is **lazy**: the
//! first DEK request connects, and a failed attempt is retried on the next
//! request, so hub-api being down at pod start never wedges the stage.
//!
//! Wire contract assumed (to be reconciled with hub-api's final proto): RPC
//! `KeyService.GetStreamDek(tenant_id, purpose, version, caller_ephemeral_
//! public_key)`; `sealed_dek` is `enc(32 bytes) || ciphertext`, the HPKE-lite
//! construction in [`crate::provider`], bound to
//! `service_id|tenant_id|purpose|version`.

use std::sync::Arc;

use hub_client::HubClient;
use tokio::sync::OnceCell;

use crate::provider::{
    BoxFuture, DekBroker, HubApiDekProvider, SealedDek, TtlCachedDekProvider, BUNDLE_DATA_PURPOSE,
    DEFAULT_DEK_CACHE_TTL,
};
use crate::TenantCrypto;

/// Machine-JWT scope requested for key fetches (hub-api
/// `REQUIRED_SCOPES` for `KeyService/GetStreamDek`).
pub const KEY_SERVICE_SCOPE: &str = "key:stream-dek:read";

/// Length of the server's one-time X25519 public key prefix in `sealed_dek`.
const ENC_LEN: usize = 32;
/// AES-GCM tag length: the shortest possible non-empty ciphertext.
const MIN_CIPHERTEXT_LEN: usize = 16;

/// Where and how to reach hub-api's internal gRPC key service.
#[derive(Clone, Debug)]
pub struct HubKeyEndpoint {
    /// e.g. `https://waddlebot-hub-api-v3:50204`.
    pub endpoint: String,
    /// Machine-JWT token endpoint (`service_auth::MachineJwtClient`).
    pub token_endpoint: String,
    /// Projected service-account token path.
    pub sa_token_path: String,
    /// PEM CA bundle that signed hub-api's gRPC cert (`None` = webpki roots).
    pub ca_cert_path: Option<String>,
}

/// [`DekBroker`] over hub-api's internal gRPC `KeyService`.
pub struct GrpcDekBroker {
    cfg: Option<HubKeyEndpoint>,
    client: OnceCell<Arc<HubClient>>,
}

impl GrpcDekBroker {
    /// `cfg` of `None` means the deployment has no key-service wiring: every
    /// request then fails closed with a reason naming the missing config.
    #[must_use]
    pub fn new(cfg: Option<HubKeyEndpoint>) -> Self {
        Self {
            cfg,
            client: OnceCell::new(),
        }
    }

    async fn client(&self) -> Result<&Arc<HubClient>, String> {
        let Some(cfg) = &self.cfg else {
            return Err(
                "hub-api key service not configured (HUB_API_GRPC_ENDPOINT / \
                 SERVICE_JWT_TOKEN_ENDPOINT unset)"
                    .to_string(),
            );
        };
        self.client
            .get_or_try_init(|| async {
                HubClient::connect(
                    cfg.endpoint.clone(),
                    cfg.token_endpoint.clone(),
                    cfg.sa_token_path.clone(),
                    KEY_SERVICE_SCOPE,
                    cfg.ca_cert_path.as_deref(),
                )
                .await
                .map(Arc::new)
                .map_err(|e| format!("hub-api key service connect failed: {e}"))
            })
            .await
    }
}

impl DekBroker for GrpcDekBroker {
    fn fetch_sealed<'a>(
        &'a self,
        tenant_id: &'a str,
        purpose: &'a str,
        version: u32,
        client_public_key: [u8; 32],
    ) -> BoxFuture<'a, Result<SealedDek, String>> {
        Box::pin(async move {
            let client = self.client().await?;
            let resp = client
                .get_stream_dek(
                    tenant_id.to_string(),
                    purpose.to_string(),
                    version,
                    client_public_key.to_vec(),
                )
                .await
                .map_err(|e| format!("key service call failed: {e}"))?;
            split_sealed(&resp.sealed_dek, resp.version, u64::from(resp.ttl_seconds))
        })
    }
}

/// Splits `enc(32) || ciphertext` into a [`SealedDek`].
fn split_sealed(sealed: &[u8], dek_version: u32, ttl_seconds: u64) -> Result<SealedDek, String> {
    if sealed.len() < ENC_LEN + MIN_CIPHERTEXT_LEN {
        return Err(format!(
            "sealed_dek is {} bytes, expected enc({ENC_LEN}) || ciphertext",
            sealed.len()
        ));
    }
    let mut enc = [0u8; ENC_LEN];
    enc.copy_from_slice(&sealed[..ENC_LEN]);
    Ok(SealedDek {
        enc,
        ciphertext: sealed[ENC_LEN..].to_vec(),
        dek_version,
        ttl_seconds,
    })
}

/// The production provider: gRPC broker behind the TTL cache.
pub type GrpcDekProvider = TtlCachedDekProvider<HubApiDekProvider<GrpcDekBroker>>;

/// Builds the stage's [`TenantCrypto`] over a lazy gRPC broker, returning
/// the shared cache handle too so a rotation/revocation listener can
/// invalidate it. `service_id` is `"svc-process"` / `"svc-action"`.
#[must_use]
pub fn tenant_crypto_over_grpc(
    cfg: Option<HubKeyEndpoint>,
    service_id: &'static str,
) -> (TenantCrypto, GrpcDekProvider) {
    let provider = TtlCachedDekProvider::new(
        HubApiDekProvider::new(GrpcDekBroker::new(cfg), service_id, BUNDLE_DATA_PURPOSE),
        DEFAULT_DEK_CACHE_TTL,
    );
    (TenantCrypto::new(provider.clone()), provider)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn split_sealed_rejects_short_and_splits_layout() {
        assert!(split_sealed(&[0u8; 47], 1, 1).is_err());
        let mut raw = vec![1u8; 32];
        raw.extend_from_slice(&[2u8; 48]);
        let s = split_sealed(&raw, 7, 60).unwrap_or_else(|e| panic!("{e}"));
        assert_eq!((s.enc, s.dek_version, s.ttl_seconds), ([1u8; 32], 7, 60));
        assert_eq!(s.ciphertext, vec![2u8; 48]);
    }

    #[tokio::test]
    async fn unconfigured_broker_fails_closed() {
        let b = GrpcDekBroker::new(None);
        let err = b.fetch_sealed("1", "bundle-data", 0, [0u8; 32]).await.err();
        assert!(err.is_some_and(|e| e.contains("not configured")));
    }
}
