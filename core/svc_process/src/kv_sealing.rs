//! Per-tenant at-rest sealing of bundle `kv` values for this stage: the
//! opt-out kill-switch gate and the production [`Sealing`] construction
//! (gRPC DEK broker over `core/hub_client`, shared wiring in
//! `bundle_host_kv::grpc_wiring`). Design:
//! `docs/superpowers/specs/2026-11-08-tenant-at-rest-encryption.md`.

use std::sync::Arc;

use bundle_host_kv::grpc_wiring::{build_grpc_sealing, GrpcDekProvider, HubKeyEndpoint};
use bundle_host_kv::{BoxFuture, Sealing, SealingGate};
use penguin_licensing::LicenseClient;

use crate::config::CliConfig;

/// Opt-out kill-switch (core-mechanism convention): unseen / OFF / license
/// server unreachable = sealing ON; ON = operator-chosen emergency rollback
/// to plaintext writes (reads still open envelopes).
pub const DISABLE_TENANT_AT_REST_ENCRYPTION_FLAG: &str =
    "waddles.disable-tenant-at-rest-encryption";

/// [`SealingGate`] over the license client's flag. Bypass-aware: a bypassed
/// client reads every flag `true`, which for an opt-OUT flag must not mean
/// "encryption disabled", so bypass short-circuits to "seal".
pub struct TenantAtRestEncryptionGate(Option<Arc<LicenseClient>>);

impl TenantAtRestEncryptionGate {
    /// `None` (no license client at all) always seals.
    pub fn new(client: Option<Arc<LicenseClient>>) -> Self {
        Self(client)
    }
}

impl SealingGate for TenantAtRestEncryptionGate {
    fn seal_writes(&self) -> BoxFuture<'_, bool> {
        Box::pin(async move {
            match &self.0 {
                None => true,
                Some(client) if client.bypass_active() => true,
                Some(client) => {
                    !client
                        .flag_enabled(DISABLE_TENANT_AT_REST_ENCRYPTION_FLAG)
                        .await
                }
            }
        })
    }
}

/// hub-api key-service location from the stage's CLI config, or `None` when
/// either required setting is unset (sealing then fails closed per op).
pub fn key_endpoint(cli: &CliConfig) -> Option<HubKeyEndpoint> {
    if cli.hub_api_grpc_endpoint.is_empty() || cli.service_jwt_token_endpoint.is_empty() {
        return None;
    }
    Some(HubKeyEndpoint {
        endpoint: cli.hub_api_grpc_endpoint.clone(),
        token_endpoint: cli.service_jwt_token_endpoint.clone(),
        sa_token_path: cli.service_jwt_sa_token_path.clone(),
        ca_cert_path: (!cli.hub_api_grpc_ca_file.is_empty())
            .then(|| cli.hub_api_grpc_ca_file.clone()),
    })
}

/// Builds this stage's [`Sealing`] and the DEK cache handle for the
/// invalidation listener.
pub fn build(cli: &CliConfig, license: Option<&Arc<LicenseClient>>) -> (Sealing, GrpcDekProvider) {
    build_grpc_sealing(
        key_endpoint(cli),
        crate::SERVICE_NAME,
        Arc::new(TenantAtRestEncryptionGate::new(license.cloned())),
        cli.kv_legacy_plaintext_reads_allowed,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn no_license_client_means_sealing_stays_on() {
        assert!(TenantAtRestEncryptionGate::new(None).seal_writes().await);
    }

    #[test]
    fn kill_switch_flag_key_follows_the_spec() {
        assert_eq!(
            DISABLE_TENANT_AT_REST_ENCRYPTION_FLAG,
            "waddles.disable-tenant-at-rest-encryption"
        );
    }
}
