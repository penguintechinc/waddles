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
//!   [`ResolveHandle`](HubClient::resolve_handle),
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
    ResolveHandleRequest, ResolveHandleResponse,
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
    #[error("failed to read CA certificate at {path}: {source}")]
    TlsCaRead {
        path: String,
        #[source]
        source: std::io::Error,
    },
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

/// Whether a failed RPC is the caller's own input being rejected (no such handle,
/// ambiguous handle, malformed request) rather than a sign hub-api is unhealthy.
/// `ResolveHandle` hits these on ordinary typos (`!secret @nobody`), so counting them
/// as circuit failures would let a few bad lookups -- or one hostile chatter -- open
/// the breaker and fail every other RPC (egress detokenization, pseudonym mint) fast.
fn is_caller_error(status: &Status) -> bool {
    matches!(
        status.code(),
        tonic::Code::NotFound | tonic::Code::FailedPrecondition | tonic::Code::InvalidArgument
    )
}

/// Builds the `ClientTlsConfig` [`HubClient::connect`] dials with -- pulled out as its
/// own function so the CA-loading behavior (PR #570 review blocker 2) can be unit
/// tested without a real network connection.
fn build_tls_config(
    ca_cert_path: Option<&str>,
) -> Result<tonic::transport::ClientTlsConfig, HubClientError> {
    let tls = tonic::transport::ClientTlsConfig::new();
    Ok(match ca_cert_path {
        Some(path) => {
            let pem = std::fs::read(path).map_err(|source| HubClientError::TlsCaRead {
                path: path.to_string(),
                source,
            })?;
            tls.ca_certificate(tonic::transport::Certificate::from_pem(pem))
        }
        None => tls.with_enabled_roots(),
    })
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
    ///
    /// `ca_cert_path` (PR #570 review blocker 2) -- path to the PEM CA bundle that
    /// signed hub-api's internal gRPC server cert (the chart wires this from
    /// `HUB_API_GRPC_CA_FILE`, mounted from the same `-hub-api-grpc-tls` Secret
    /// `templates/hub-api.yaml`'s server listens with -- see
    /// `templates/hub-api-grpc-tls-secret.yaml`). `Some(path)` trusts ONLY that CA
    /// (this is a closed internal cluster connection, not a call to the public
    /// internet, so the system/webpki trust store is deliberately not consulted).
    /// `None` falls back to `with_enabled_roots()` for callers that genuinely dial a
    /// public/webpki-rooted endpoint instead. Hostname verification stays ON in both
    /// cases -- `endpoint`'s own host (`waddlebot-hub-api-v3`) is what tonic checks
    /// the leaf cert's SAN against; this function never installs a custom verifier
    /// that could relax that check.
    pub async fn connect(
        endpoint: impl Into<String>,
        token_endpoint: impl Into<String>,
        sa_token_path: impl Into<String>,
        scope: impl Into<String>,
        ca_cert_path: Option<&str>,
    ) -> Result<Self, HubClientError> {
        let tls_config = build_tls_config(ca_cert_path)?;
        let channel = Endpoint::from_shared(endpoint.into())?
            .tls_config(tls_config)?
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

    /// Resolves ONE raw chat reference (`@bob`, `bob`, or a Discord `<@id>`
    /// mention) to its stable UUID inside hub-api's PII boundary -- the raw
    /// handle is sent to hub-api and never comes back, only the UUID does.
    /// Idempotent (a read, or a get-or-create of the same stable pseudonym)
    /// so it is retried like `resolve_display_names`. A handle with no match
    /// surfaces as `HubClientError::Grpc` with `Code::NotFound`, an
    /// ambiguous one as `Code::FailedPrecondition` -- never a default UUID.
    pub async fn resolve_handle(
        &self,
        tenant_id: String,
        platform: String,
        target: String,
    ) -> Result<ResolveHandleResponse, HubClientError> {
        self.call_with_retry(|| {
            let tenant_id = tenant_id.clone();
            let platform = platform.clone();
            let target = target.clone();
            async move {
                let request = self
                    .authed_request(ResolveHandleRequest {
                        tenant_id,
                        platform,
                        target,
                    })
                    .await?;
                let mut client = self.identity.clone();
                client
                    .resolve_handle(request)
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
                Err(HubClientError::Grpc(status)) if is_caller_error(&status) => {
                    // The server answered and rejected the input: proof of health.
                    self.circuit.record_success();
                    return Err(HubClientError::Grpc(status));
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
    fn caller_errors_are_exactly_not_found_failed_precondition_invalid_argument() {
        assert!(is_caller_error(&Status::not_found("no such handle")));
        assert!(is_caller_error(&Status::failed_precondition("ambiguous")));
        assert!(is_caller_error(&Status::invalid_argument("bad")));
        assert!(!is_caller_error(&Status::internal("boom")));
        assert!(!is_caller_error(&Status::unavailable("down")));
        assert!(!is_caller_error(&Status::unauthenticated("no")));
        assert!(!is_caller_error(&Status::permission_denied("no")));
    }

    /// A `HubClient` over a lazy channel that never dials -- `call_with_retry` is driven
    /// with synthetic results, so no network or JWT bootstrap is involved.
    fn offline_client() -> HubClient {
        let channel = Endpoint::from_static("http://127.0.0.1:1").connect_lazy();
        HubClient {
            identity: IdentityServiceClient::new(channel.clone()),
            key: KeyServiceClient::new(channel),
            token_client: Arc::new(MachineJwtClient::new(
                "http://127.0.0.1:1/token",
                "/nonexistent/sa-token",
                "identity:handle:resolve",
            )),
            circuit: Arc::new(CircuitBreaker::new(5, Duration::from_secs(30))),
            deadline: DEFAULT_RPC_DEADLINE,
            max_retries: 2,
        }
    }

    #[tokio::test]
    async fn repeated_not_found_lookups_never_open_the_circuit() {
        let client = offline_client();
        for _ in 0..20 {
            let result: Result<(), HubClientError> = client
                .call_with_retry(|| async {
                    Err(HubClientError::Grpc(Status::not_found("no such handle")))
                })
                .await;
            assert!(matches!(
                result,
                Err(HubClientError::Grpc(ref s)) if s.code() == tonic::Code::NotFound
            ));
        }
        assert!(
            client.circuit.allow(),
            "caller errors are not health failures: the breaker must stay closed"
        );
    }

    #[tokio::test]
    async fn server_errors_still_open_the_circuit() {
        let client = offline_client();
        for _ in 0..5 {
            let result: Result<(), HubClientError> = client
                .call_with_retry(|| async { Err(HubClientError::Grpc(Status::internal("boom"))) })
                .await;
            assert!(result.is_err());
        }
        assert!(
            !client.circuit.allow(),
            "five consecutive INTERNAL failures must open the breaker"
        );
        let blocked: Result<(), HubClientError> = client.call_with_retry(|| async { Ok(()) }).await;
        assert!(matches!(blocked, Err(HubClientError::CircuitOpen)));
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

/// PR #570 review blocker 2 regression coverage: `HubClient::connect` must trust the
/// chart's internal CA (loaded from `ca_cert_path`/`HUB_API_GRPC_CA_FILE`), not just
/// system/webpki roots. `build_tls_config` can't be exercised through a real TLS
/// handshake directly -- tonic keeps the rustls config it builds private until
/// `Endpoint::connect` dials -- so these tests build an equivalent rustls
/// `ClientConfig` from the SAME CA PEM file `build_tls_config` reads and complete a
/// real local handshake, proving the exact failure mode PR #570 flagged: a cert signed
/// by the trusted CA is accepted, one that isn't is rejected (never silently trusted),
/// all with hostname verification left on.
#[cfg(test)]
mod ca_trust_tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use rustls_pki_types::pem::PemObject;
    use rustls_pki_types::{CertificateDer, PrivateKeyDer, ServerName};
    use std::io::Write;
    use std::sync::Arc;

    /// A throwaway CA + leaf cert pair, generated fresh per test (`rcgen`, pure Rust) --
    /// no private key material, test-only or otherwise, ever lands in source control.
    struct TestPki {
        ca_pem: String,
        leaf_cert_pem: String,
        leaf_key_pem: String,
    }

    fn generate_signed_pki(leaf_dns_name: &str) -> TestPki {
        let ca_key = rcgen::KeyPair::generate().expect("generate ca key");
        let ca_params = rcgen::CertificateParams::new(vec!["hub-client-test-ca".to_string()])
            .expect("build ca params");
        let ca_cert = ca_params.self_signed(&ca_key).expect("self-sign ca cert");
        let issuer = rcgen::Issuer::from_params(&ca_params, ca_key);

        let leaf_key = rcgen::KeyPair::generate().expect("generate leaf key");
        let leaf_params = rcgen::CertificateParams::new(vec![leaf_dns_name.to_string()])
            .expect("build leaf params");
        let leaf_cert = leaf_params
            .signed_by(&leaf_key, &issuer)
            .expect("sign leaf cert with test ca");

        TestPki {
            ca_pem: ca_cert.pem(),
            leaf_cert_pem: leaf_cert.pem(),
            leaf_key_pem: leaf_key.serialize_pem(),
        }
    }

    /// A self-signed leaf with no relation at all to the CA the client will trust --
    /// the "unsigned"/untrusted-chain case.
    fn generate_unsigned_leaf(dns_name: &str) -> (String, String) {
        let key = rcgen::KeyPair::generate().expect("generate key");
        let params =
            rcgen::CertificateParams::new(vec![dns_name.to_string()]).expect("build params");
        let cert = params.self_signed(&key).expect("self-sign cert");
        (cert.pem(), key.serialize_pem())
    }

    fn write_temp_pem(contents: &str, label: &str) -> std::path::PathBuf {
        let path = std::env::temp_dir().join(format!(
            "hub-client-test-{}-{}-{label}.pem",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0)
        ));
        let mut f = std::fs::File::create(&path).expect("create temp pem file");
        f.write_all(contents.as_bytes())
            .expect("write temp pem file");
        path
    }

    /// Builds a rustls `RootCertStore` from exactly the CA PEM file `build_tls_config`
    /// would read via `ca_cert_path` -- the equivalent trust material tonic's
    /// `ClientTlsConfig::ca_certificate` installs internally.
    fn root_store_from_ca_file(path: &std::path::Path) -> rustls::RootCertStore {
        let mut store = rustls::RootCertStore::empty();
        for cert in CertificateDer::pem_file_iter(path).expect("read ca pem") {
            store
                .add(cert.expect("valid ca cert"))
                .expect("add ca cert to root store");
        }
        store
    }

    /// Spins up a real local TLS server presenting `server_cert_pem`/`server_key_pem`,
    /// dials it with a client trusting only `ca_path`'s CA, and returns whether the
    /// handshake succeeded -- hostname verification stays on throughout (the real
    /// `rustls::ClientConfig::with_root_certificates` path, no custom verifier).
    async fn handshake_result(
        ca_path: &std::path::Path,
        server_cert_pem: &str,
        server_key_pem: &str,
    ) -> std::io::Result<()> {
        let cert_path = write_temp_pem(server_cert_pem, "servercert");
        let key_path = write_temp_pem(server_key_pem, "serverkey");
        let server_certs: Vec<_> = CertificateDer::pem_file_iter(&cert_path)
            .expect("read server certs")
            .collect::<Result<_, _>>()
            .expect("valid server certs");
        let server_key = PrivateKeyDer::from_pem_file(&key_path).expect("read server private key");
        let server_config = rustls::ServerConfig::builder()
            .with_no_client_auth()
            .with_single_cert(server_certs, server_key)
            .expect("build server tls config");
        let acceptor = tokio_rustls::TlsAcceptor::from(Arc::new(server_config));

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind local listener");
        let addr = listener.local_addr().expect("local addr");
        let server_task = tokio::spawn(async move {
            let (tcp, _) = listener.accept().await.expect("accept");
            acceptor.accept(tcp).await
        });

        let roots = root_store_from_ca_file(ca_path);
        let client_config = rustls::ClientConfig::builder()
            .with_root_certificates(roots)
            .with_no_client_auth();
        let connector = tokio_rustls::TlsConnector::from(Arc::new(client_config));
        let tcp = tokio::net::TcpStream::connect(addr)
            .await
            .expect("connect tcp");
        let server_name = ServerName::try_from("hub-client-test-leaf").expect("valid server name");
        let result = connector.connect(server_name, tcp).await;

        let _ = std::fs::remove_file(&cert_path);
        let _ = std::fs::remove_file(&key_path);
        let _ = server_task.await;
        result.map(|_| ())
    }

    #[tokio::test]
    async fn trusts_a_leaf_certificate_signed_by_the_loaded_ca() {
        let pki = generate_signed_pki("hub-client-test-leaf");
        let ca_path = write_temp_pem(&pki.ca_pem, "ca");

        // Sanity: build_tls_config itself accepts this CA file without error --
        // proves the production code path (HubClient::connect) actually reads it.
        build_tls_config(Some(ca_path.to_str().expect("utf8 path")))
            .expect("build_tls_config must accept a real CA file");

        let result = handshake_result(&ca_path, &pki.leaf_cert_pem, &pki.leaf_key_pem).await;
        let _ = std::fs::remove_file(&ca_path);
        assert!(
            result.is_ok(),
            "a certificate signed by the loaded CA must be trusted, got {result:?}"
        );
    }

    #[tokio::test]
    async fn rejects_a_leaf_certificate_not_signed_by_the_loaded_ca() {
        let trusted = generate_signed_pki("hub-client-test-leaf");
        let ca_path = write_temp_pem(&trusted.ca_pem, "ca");
        // A leaf self-signed by a DIFFERENT, untrusted key -- not part of the chain
        // the CA above ever issued.
        let (untrusted_leaf_cert_pem, untrusted_leaf_key_pem) =
            generate_unsigned_leaf("hub-client-test-leaf");

        let result =
            handshake_result(&ca_path, &untrusted_leaf_cert_pem, &untrusted_leaf_key_pem).await;
        let _ = std::fs::remove_file(&ca_path);
        assert!(
            result.is_err(),
            "a certificate not signed by the loaded CA must be rejected, never silently trusted"
        );
    }

    #[test]
    fn build_tls_config_fails_closed_on_a_missing_ca_file() {
        let result = build_tls_config(Some("/nonexistent/hub-api-grpc-ca/ca.crt"));
        assert!(matches!(result, Err(HubClientError::TlsCaRead { .. })));
    }
}
