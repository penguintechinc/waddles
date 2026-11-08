//! Production wiring for kv value sealing (feature `grpc`): the gRPC DEK
//! broker behind a TTL cache, plus the Valkey invalidation listener. Shared
//! by svc_process and svc_action so the two stages cannot drift; each stage
//! supplies only its own kill-switch [`SealingGate`] (its license client).

use std::sync::Arc;
use std::time::Duration;

use bundle_host_crypto::grpc::tenant_crypto_over_grpc;
pub use bundle_host_crypto::grpc::{GrpcDekProvider, HubKeyEndpoint};
use bundle_host_crypto::{run_dek_invalidation_listener, BUNDLE_DATA_PURPOSE};

use crate::seal::{Sealing, SealingGate};

/// Builds the stage's [`Sealing`] over a lazy gRPC broker. `key_endpoint` of
/// `None` (hub-api gRPC not configured) yields a sealing handle whose every
/// seal/open fails closed with `key_unavailable`: loud per operation, never
/// plaintext. Returns the DEK cache handle for [`spawn_dek_invalidation`].
#[must_use]
pub fn build_grpc_sealing(
    key_endpoint: Option<HubKeyEndpoint>,
    service_id: &'static str,
    gate: Arc<dyn SealingGate>,
    legacy_plaintext_reads_allowed: bool,
) -> (Sealing, GrpcDekProvider) {
    if key_endpoint.is_none() {
        tracing::error!(
            service_id,
            "kv sealing: hub-api key service not configured (HUB_API_GRPC_ENDPOINT / \
             SERVICE_JWT_TOKEN_ENDPOINT); every sealed kv op will refuse with key_unavailable \
             until it is configured (or the operator sets \
             waddles.disable-tenant-at-rest-encryption)"
        );
    }
    let (crypto, cache) = tenant_crypto_over_grpc(key_endpoint, service_id);
    (
        Sealing::new(crypto, gate)
            .with_legacy_plaintext_reads_allowed(legacy_plaintext_reads_allowed),
        cache,
    )
}

/// Spawns the DEK rotation/revocation listener on its own Valkey connection
/// (`XREAD BLOCK` must not share a multiplexed connection with traffic). A
/// connect failure is retried forever with backoff; the cache TTL bounds how
/// long a DEK can outlive a missed signal.
pub fn spawn_dek_invalidation(client: redis::Client, cache: GrpcDekProvider) {
    tokio::spawn(async move {
        loop {
            match client.get_multiplexed_async_connection().await {
                Ok(conn) => {
                    // `_keepalive` stays alive across the await, so the
                    // shutdown receiver never resolves: the listener runs
                    // for the life of the process.
                    let (_keepalive, shutdown) = tokio::sync::oneshot::channel::<()>();
                    run_dek_invalidation_listener(
                        conn,
                        cache.clone(),
                        BUNDLE_DATA_PURPOSE,
                        shutdown,
                    )
                    .await;
                    return;
                }
                Err(err) => {
                    tracing::warn!(error = %err, "dek invalidation listener: Valkey connect failed, retrying");
                    tokio::time::sleep(Duration::from_secs(5)).await;
                }
            }
        }
    });
}
