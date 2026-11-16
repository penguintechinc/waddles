//! Per-tenant at-rest sealing of bundle `kv` values for this stage: the
//! opt-out kill-switch gate and the production [`Sealing`] construction
//! (gRPC DEK broker over `core/hub_client`, shared wiring in
//! `bundle_host_kv::grpc_wiring`). Design:
//! `docs/superpowers/specs/2026-11-08-tenant-at-rest-encryption.md`.

use std::sync::Arc;

use bundle_host_crypto::TenantCrypto;
use bundle_host_kv::grpc_wiring::{build_grpc_sealing, GrpcDekProvider, HubKeyEndpoint};
use bundle_host_kv::{BoxFuture, Sealing, SealingGate};
use bundle_host_storage::{ObjectSealing, SealGate};
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

/// The same kill-switch as the kv gate, for object bodies -- with a different consequence.
/// `false` ("do not seal") makes the storage host REFUSE object writes
/// (`waddles_bundle_storage_denied{reason="sealing_unavailable"}`): there is no plaintext write
/// path for objects, so the switch can stop writes but never downgrade them. Reads of sealed
/// objects keep opening regardless.
impl SealGate for TenantAtRestEncryptionGate {
    fn seal_writes(&self) -> BoxFuture<'_, bool> {
        SealingGate::seal_writes(self)
    }
}

/// Per-tenant at-rest sealing of bundle OBJECT bodies for this stage.
///
/// Shares the kv sealing's DEK cache (`dek_cache`, the handle `build` returned, so the same
/// rotation/revocation listener evicts both) and the kill-switch gate above. Fails closed:
/// no key -> every put/get refuses (never plaintext); the kill-switch refuses writes; and
/// legacy (pre-encryption) plaintext is served only for objects last modified BEFORE
/// `legacy_plaintext_until` (unix seconds, the operator-recorded instant sealing was first
/// enabled). `None` = no plaintext object is ever served.
pub fn object_sealing(
    dek_cache: &GrpcDekProvider,
    license: Option<&Arc<LicenseClient>>,
    legacy_plaintext_until: Option<u64>,
) -> ObjectSealing {
    object_sealing_with(
        TenantCrypto::new(dek_cache.clone()),
        license,
        legacy_plaintext_until,
    )
}

/// [`object_sealing`] over an already-built [`TenantCrypto`] (the seam tests use to supply keys).
pub fn object_sealing_with(
    crypto: TenantCrypto,
    license: Option<&Arc<LicenseClient>>,
    legacy_plaintext_until: Option<u64>,
) -> ObjectSealing {
    let sealing = ObjectSealing::new(crypto)
        .with_gate(Arc::new(TenantAtRestEncryptionGate::new(license.cloned())));
    match legacy_plaintext_until {
        Some(cutoff) => sealing.with_legacy_plaintext_until(cutoff),
        None => sealing,
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
        assert!(SealingGate::seal_writes(&TenantAtRestEncryptionGate::new(None)).await);
    }

    #[tokio::test]
    async fn the_object_gate_is_the_same_kill_switch_and_seals_without_a_license_client() {
        assert!(SealGate::seal_writes(&TenantAtRestEncryptionGate::new(None)).await);
    }

    #[test]
    fn kill_switch_flag_key_follows_the_spec() {
        assert_eq!(
            DISABLE_TENANT_AT_REST_ENCRYPTION_FLAG,
            "waddles.disable-tenant-at-rest-encryption"
        );
    }
}
