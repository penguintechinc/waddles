//! Rust client for hub-api's internal gRPC surface
//! (`waddles.hub.internal.v1`: `IdentityService`, `KeyService`) --
//! feature/hub-api-internal-grpc. Consumed by `svc_ingest`/`svc_process`/
//! `svc_action`.
//!
//! Every RPC:
//! - is injected with a `core/service_auth`-issued machine JWT
//!   (`Authorization: Bearer <token>` metadata), fetched/cached/refreshed
//!   by [`service_auth::MachineJwtClient`] -- this crate never re-
//!   implements that bootstrap;
//! - carries a bounded per-RPC deadline ([`DEFAULT_RPC_DEADLINE`]),
//!   matching `hub_api/grpc_internal/interceptors.py::
//!   MAX_RPC_DEADLINE_SECONDS`'s server-side ceiling;
//! - runs behind a [`CircuitBreaker`] shared across every RPC on this
//!   client, so a hub-api outage fails fast instead of piling up
//!   in-flight requests against a channel that keeps refusing them;
//! - retries **only** if the RPC is idempotent
//!   ([`ResolveDisplayNames`](HubClient::resolve_display_names),
//!   [`GetStreamDek`](HubClient::get_stream_dek)) and the failure is
//!   transient (`UNAVAILABLE`/`DEADLINE_EXCEEDED`).
//!   [`MintEphemeralPseudonyms`](HubClient::mint_ephemeral_pseudonyms)
//!   is never retried by this client -- callers that need at-least-once
//!   semantics must dedupe on their own, since a retried mint could
//!   otherwise double-count against hub-api's own idempotency window.
//!
//! Connection pooling is a single shared [`tonic::transport::Channel`]:
//! tonic multiplexes concurrent RPCs over the channel's HTTP/2
//! connection(s) internally, so `HubClient` is cheaply `Clone` (an
//! `Arc`-backed handle) rather than opening a new connection per call.

pub mod pb {
    //! Generated from `proto/waddles/hub/internal/v1/*.proto` (`build.rs`).
    tonic::include_proto!("waddles.hub.internal.v1");
}

use std::sync::atomic::{AtomicU32, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use pb::identity_service_client::IdentityServiceClient;
use pb::key_service_client::KeyServiceClient;
use pb::{
    EphemeralPseudonym, GetStreamDekRequest, GetStreamDekResponse, MintEphemeralPseudonymRequest,
    MintEphemeralPseudonymsRequest, ResolveDisplayNamesRequest, ResolveDisplayNamesResponse,
};
use service_auth::{MachineJwtClient, ServiceAuthError};
use tonic::transport::{Channel, Endpoint};
use tonic::{Request, Status};
use tracing::{debug, warn};

/// Server-side ceiling is 5s (`hub_api/grpc_internal/interceptors.py::
/// MAX_RPC_DEADLINE_SECONDS`) -- stay comfortably under it so a client
/// deadline never itself trips the server's "deadline too long" rejection.
pub const DEFAULT_RPC_DEADLINE: Duration = Duration::from_secs(3);

/// Every failure this client can raise.
#[derive(thiserror::Error, Debug)]
pub enum HubClientError {
    #[error("hub-api internal gRPC call failed: {0}")]
    Grpc(#[from] Status),
    #[error("failed to obtain machine JWT: {0}")]
    Auth(#[from] ServiceAuthError),
    #[error("transport error: {0}")]
    Transport(#[from] tonic::transport::Error),
    #[error("circuit breaker open -- hub-api internal gRPC is failing fast")]
    CircuitOpen,
}

/// Three-state (closed/open/half-open) circuit breaker shared across every
/// RPC this client makes. Opens after `failure_threshold` consecutive
/// failures; after `reset_after`, allows exactly one probe call
/// (half-open) whose outcome flips it fully closed (success) or back open
/// (failure) -- the standard breaker state machine, sized small here since
/// this client's only job is protecting itself from a wedged hub-api, not
/// coordinating cluster-wide load shedding.
pub struct CircuitBreaker {
    failure_threshold: u32,
    reset_after: Duration,
    consecutive_failures: AtomicU32,
    opened_at_epoch_ms: AtomicU64,
}

impl CircuitBreaker {
    pub fn new(failure_threshold: u32, reset_after: Duration) -> Self {
        Self {
            failure_threshold,
            reset_after,
            consecutive_failures: AtomicU32::new(0),
            opened_at_epoch_ms: AtomicU64::new(0),
        }
    }

    /// Returns `true` if a call may proceed right now (closed, or
    /// half-open and this is the one allowed probe).
    fn allow(&self) -> bool {
        let opened_at = self.opened_at_epoch_ms.load(Ordering::Acquire);
        if opened_at == 0 {
            return true; // closed
        }
        let now = now_ms();
        if now.saturating_sub(opened_at) >= self.reset_after.as_millis() as u64 {
            // Half-open: let exactly one probe through by optimistically
            // clearing `opened_at` here; a losing race just means two
            // probes instead of one, which is harmless.
            self.opened_at_epoch_ms.store(0, Ordering::Release);
            return true;
        }
        false
    }

    fn record_success(&self) {
        self.consecutive_failures.store(0, Ordering::Release);
        self.opened_at_epoch_ms.store(0, Ordering::Release);
    }

    fn record_failure(&self) {
        let failures = self.consecutive_failures.fetch_add(1, Ordering::AcqRel) + 1;
        if failures >= self.failure_threshold {
            self.opened_at_epoch_ms.store(now_ms(), Ordering::Release);
            warn!(failures, "hub_client.circuit_opened");
        }
    }
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or(Duration::ZERO)
        .as_millis() as u64
}

/// Whether a transient gRPC failure is safe to retry -- `UNAVAILABLE`
/// (transport-level, connection reset/refused) and `DEADLINE_EXCEEDED`
/// (the previous attempt's deadline lapsed without a definitive
/// response) are the only two codes where "try again" is meaningfully
/// different from "the request itself was rejected".
fn is_retryable(status: &Status) -> bool {
    matches!(
        status.code(),
        tonic::Code::Unavailable | tonic::Code::DeadlineExceeded
    )
}

/// Client for `waddles.hub.internal.v1`'s `IdentityService` + `KeyService`.
/// Cheaply `Clone` -- clones share the same underlying [`Channel`] (pooled
/// HTTP/2 connection(s)), [`MachineJwtClient`] (token cache), and
/// [`CircuitBreaker`] state.
#[derive(Clone)]
pub struct HubClient {
    identity: IdentityServiceClient<Channel>,
    key: KeyServiceClient<Channel>,
    token_client: Arc<MachineJwtClient>,
    circuit: Arc<CircuitBreaker>,
    deadline: Duration,
    max_retries: u32,
}

impl HubClient {
    /// Connects to hub-api's internal gRPC endpoint (e.g.
    /// `https://waddlebot-hub-api-v3:50204`) and builds the machine-JWT
    /// bootstrap client against `token_endpoint`/`sa_token_path`/`scope`
    /// (see `service_auth::MachineJwtClient`).
    pub async fn connect(
        endpoint: impl Into<String>,
        token_endpoint: impl Into<String>,
        sa_token_path: impl Into<String>,
        scope: impl Into<String>,
    ) -> Result<Self, HubClientError> {
        let channel = Endpoint::from_shared(endpoint.into())?
            .tls_config(tonic::transport::ClientTlsConfig::new().with_enabled_roots())?
            .connect()
            .await?;
        Ok(Self {
            identity: IdentityServiceClient::new(channel.clone()),
            key: KeyServiceClient::new(channel),
            token_client: Arc::new(MachineJwtClient::new(token_endpoint, sa_token_path, scope)),
            circuit: Arc::new(CircuitBreaker::new(5, Duration::from_secs(30))),
            deadline: DEFAULT_RPC_DEADLINE,
            max_retries: 2,
        })
    }

    async fn authed_request<T>(&self, message: T) -> Result<Request<T>, HubClientError> {
        let token = self.token_client.get_token().await?;
        let mut request = Request::new(message);
        request.set_timeout(self.deadline);
        request.metadata_mut().insert(
            "authorization",
            format!("Bearer {token}")
                .parse()
                .map_err(|_| Status::internal("invalid token metadata"))?,
        );
        Ok(request)
    }

    /// Mints (or returns the existing) ephemeral pseudonym for each item.
    /// **Never retried** -- see module docs.
    pub async fn mint_ephemeral_pseudonyms(
        &self,
        items: Vec<MintEphemeralPseudonymRequest>,
    ) -> Result<Vec<EphemeralPseudonym>, HubClientError> {
        if !self.circuit.allow() {
            return Err(HubClientError::CircuitOpen);
        }
        let request = self
            .authed_request(MintEphemeralPseudonymsRequest { items })
            .await?;
        let mut client = self.identity.clone();
        match client.mint_ephemeral_pseudonyms(request).await {
            Ok(response) => {
                self.circuit.record_success();
                Ok(response.into_inner().pseudonyms)
            }
            Err(status) => {
                self.circuit.record_failure();
                Err(status.into())
            }
        }
    }

    /// Resolves up to 100 UUIDs to display names. Idempotent -- retried
    /// on `UNAVAILABLE`/`DEADLINE_EXCEEDED` up to `max_retries` times.
    pub async fn resolve_display_names(
        &self,
        tenant_id: String,
        uuids: Vec<String>,
    ) -> Result<ResolveDisplayNamesResponse, HubClientError> {
        self.call_with_retry(|| {
            let tenant_id = tenant_id.clone();
            let uuids = uuids.clone();
            async move {
                let request = self
                    .authed_request(ResolveDisplayNamesRequest { tenant_id, uuids })
                    .await?;
                let mut client = self.identity.clone();
                client
                    .resolve_display_names(request)
                    .await
                    .map(|r| r.into_inner())
                    .map_err(HubClientError::from)
            }
        })
        .await
    }

    /// Returns a sealed stream DEK. Idempotent -- retried the same as
    /// `resolve_display_names`.
    pub async fn get_stream_dek(
        &self,
        tenant_id: String,
        purpose: String,
        version: u32,
        caller_ephemeral_public_key: Vec<u8>,
    ) -> Result<GetStreamDekResponse, HubClientError> {
        self.call_with_retry(|| {
            let tenant_id = tenant_id.clone();
            let purpose = purpose.clone();
            let caller_ephemeral_public_key = caller_ephemeral_public_key.clone();
            async move {
                let request = self
                    .authed_request(GetStreamDekRequest {
                        tenant_id,
                        purpose,
                        version,
                        caller_ephemeral_public_key,
                    })
                    .await?;
                let mut client = self.key.clone();
                client
                    .get_stream_dek(request)
                    .await
                    .map(|r| r.into_inner())
                    .map_err(HubClientError::from)
            }
        })
        .await
    }

    async fn call_with_retry<F, Fut, T>(&self, make_call: F) -> Result<T, HubClientError>
    where
        F: Fn() -> Fut,
        Fut: std::future::Future<Output = Result<T, HubClientError>>,
    {
        let mut attempt = 0;
        loop {
            if !self.circuit.allow() {
                return Err(HubClientError::CircuitOpen);
            }
            match make_call().await {
                Ok(value) => {
                    self.circuit.record_success();
                    return Ok(value);
                }
                Err(HubClientError::Grpc(status))
                    if is_retryable(&status) && attempt < self.max_retries =>
                {
                    self.circuit.record_failure();
                    attempt += 1;
                    let backoff = Duration::from_millis(50 * 2u64.pow(attempt));
                    debug!(attempt, code = ?status.code(), "hub_client.retrying");
                    tokio::time::sleep(backoff).await;
                }
                Err(HubClientError::Grpc(status)) => {
                    self.circuit.record_failure();
                    return Err(HubClientError::Grpc(status));
                }
                Err(other) => {
                    self.circuit.record_failure();
                    return Err(other);
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn circuit_breaker_opens_after_threshold_and_half_opens_after_reset() {
        let breaker = CircuitBreaker::new(3, Duration::from_millis(10));
        assert!(breaker.allow(), "closed breaker always allows");
        breaker.record_failure();
        breaker.record_failure();
        assert!(breaker.allow(), "below threshold still closed");
        breaker.record_failure();
        assert!(!breaker.allow(), "at threshold: open, calls blocked");
        std::thread::sleep(Duration::from_millis(15));
        assert!(breaker.allow(), "past reset_after: half-open probe allowed");
    }

    #[test]
    fn circuit_breaker_success_resets_failure_count() {
        let breaker = CircuitBreaker::new(2, Duration::from_secs(30));
        breaker.record_failure();
        breaker.record_success();
        breaker.record_failure();
        assert!(
            breaker.allow(),
            "one failure after a reset never opens a threshold-2 breaker"
        );
    }

    #[test]
    fn retryable_codes_are_exactly_unavailable_and_deadline_exceeded() {
        assert!(is_retryable(&Status::unavailable("down")));
        assert!(is_retryable(&Status::deadline_exceeded("slow")));
        assert!(!is_retryable(&Status::invalid_argument("bad")));
        assert!(!is_retryable(&Status::permission_denied("no")));
        assert!(!is_retryable(&Status::unauthenticated("no")));
    }
}
