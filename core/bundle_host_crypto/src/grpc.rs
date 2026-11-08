//! gRPC transport for the tenant-DEK broker (feature `grpc`).
//!
//! Intra-cluster calls are gRPC (rules/backend.md): this adapts
//! `hub_client::HubClient` (tonic channel with TLS and CA pinning,
//! service-JWT metadata, per-RPC deadline, retries, circuit breaker) to the
//! transport-agnostic [`DekBroker`] trait. The connection is **lazy**: the
//! first DEK request connects, and a failed attempt is retried on the next
//! request, so hub-api being down at pod start never wedges the stage.
//!
//! Wire contract (hub-api `key.proto`, PR #1166): unary
//! `KeyService.GetTenantDek(tenant_id u32, purpose, dek_version, caller_
//! ephemeral_public_key)` returning `enc(32)`, `ciphertext(48)`,
//! `dek_version`, `ttl_seconds`; sealed with the HPKE-lite construction in
//! [`crate::provider`], context `service_id|tenant_id|purpose_label|1`.

use std::sync::Arc;

use hub_client::pb::TenantDekPurpose;
use hub_client::HubClient;
use tokio::sync::OnceCell;

use crate::provider::{
    BoxFuture, DekBroker, HubApiDekProvider, SealedDek, TtlCachedDekProvider, BUNDLE_DATA_PURPOSE,
    DEFAULT_DEK_CACHE_TTL,
};
use crate::TenantCrypto;

/// Machine-JWT scope requested for key fetches (hub-api
/// `KeyService/GetTenantDek`; the token's tenant claim must be `system`).
pub const KEY_SERVICE_SCOPE: &str = "keys:tenant-dek:read";

/// Length of the server's one-time X25519 public key prefix in `sealed_dek`.
const ENC_LEN: usize = 32;
/// Sealed DEK length: 32-byte DEK + 16-byte GCM tag.
const CIPHERTEXT_LEN: usize = 48;

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
            let tenant: u32 = tenant_id
                .parse()
                .map_err(|_| format!("tenant id {tenant_id:?} is not a numeric id"))?;
            let purpose = purpose_enum(purpose)?;
            let client = self.client().await?;
            let resp = client
                .get_tenant_dek(tenant, purpose, version, client_public_key.to_vec())
                .await
                .map_err(|e| format!("key service call failed: {e}"))?;
            sealed_from_response(
                &resp.enc,
                &resp.ciphertext,
                resp.dek_version,
                resp.ttl_seconds,
            )
        })
    }
}

/// Maps the purpose label to the proto enum; unknown labels are refused.
fn purpose_enum(label: &str) -> Result<TenantDekPurpose, String> {
    match label {
        "at-rest" => Ok(TenantDekPurpose::AtRest),
        "ingest-stream" => Ok(TenantDekPurpose::IngestStream),
        other => Err(format!("unknown tenant DEK purpose label {other:?}")),
    }
}

/// Validates the response shape: `enc` exactly 32 bytes, `ciphertext`
/// exactly 48 (32-byte DEK + 16-byte tag).
fn sealed_from_response(
    enc: &[u8],
    ciphertext: &[u8],
    dek_version: u32,
    ttl_seconds: u32,
) -> Result<SealedDek, String> {
    let enc: [u8; ENC_LEN] = enc
        .try_into()
        .map_err(|_| format!("enc is {} bytes, expected {ENC_LEN}", enc.len()))?;
    if ciphertext.len() != CIPHERTEXT_LEN {
        return Err(format!(
            "ciphertext is {} bytes, expected {CIPHERTEXT_LEN}",
            ciphertext.len()
        ));
    }
    Ok(SealedDek {
        enc,
        ciphertext: ciphertext.to_vec(),
        dek_version,
        ttl_seconds: u64::from(ttl_seconds),
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
    fn response_shape_is_validated() {
        assert!(sealed_from_response(&[0u8; 31], &[0u8; 48], 1, 60).is_err());
        assert!(sealed_from_response(&[0u8; 32], &[0u8; 47], 1, 60).is_err());
        let s =
            sealed_from_response(&[1u8; 32], &[2u8; 48], 7, 60).unwrap_or_else(|e| panic!("{e}"));
        assert_eq!((s.enc, s.dek_version, s.ttl_seconds), ([1u8; 32], 7, 60));
        assert_eq!(s.ciphertext, vec![2u8; 48]);
    }

    #[test]
    fn purpose_labels_map_and_unknown_is_refused() {
        assert_eq!(purpose_enum("at-rest"), Ok(TenantDekPurpose::AtRest));
        assert_eq!(
            purpose_enum("ingest-stream"),
            Ok(TenantDekPurpose::IngestStream)
        );
        assert!(purpose_enum("bundle-data").is_err());
    }

    #[tokio::test]
    async fn non_numeric_tenant_and_unconfigured_broker_fail_closed() {
        let b = GrpcDekBroker::new(None);
        let err = b.fetch_sealed("slug", "at-rest", 0, [0u8; 32]).await.err();
        assert!(err.is_some_and(|e| e.contains("not a numeric id")));
        let err = b.fetch_sealed("1", "at-rest", 0, [0u8; 32]).await.err();
        assert!(err.is_some_and(|e| e.contains("not configured")));
    }
}
