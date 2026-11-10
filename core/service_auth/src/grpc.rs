//! gRPC transport to hub-api's `ServiceAuthService` (machine-JWT mint + JWKS).
//!
//! gRPC is the only intra-cluster protocol; this replaces the old REST bootstrap
//! (`POST /internal/service-token`, `GET /internal/service-jwks.json`). Channel policy:
//!
//! * **TLS 1.3 only.** `https://` endpoints go through a custom connector whose rustls
//!   `ClientConfig` is built with `TLS13` as the sole protocol version (tonic's own
//!   `ClientTlsConfig` cannot pin it) and trusts exactly the CA in `HUB_API_GRPC_CA_FILE`;
//!   hostname verification stays on. No CA configured for an `https://` endpoint is an error,
//!   never a fall-back to system roots or to plaintext.
//! * **Plaintext only on loopback.** `http://` is accepted for `127.0.0.0/8`/`::1`/`localhost`
//!   (dev + in-process test servers) and refused for any other host: the mint request carries
//!   a ServiceAccount token.
//! * **Long-lived channel.** One lazily-connected HTTP/2 channel per [`HubChannel`], reused by
//!   every RPC, with keepalive pings while idle; tonic reconnects transparently.
//! * **Bounded deadlines.** Every RPC carries a deadline capped at [`MAX_RPC_DEADLINE`], under
//!   hub-api's server-side ceiling (`hub_api/grpc_internal/interceptors.py`, 5s).
//!
//! The mint RPC is authenticated with the pod's projected ServiceAccount token as
//! `authorization: Bearer <sa-token>` metadata (the same bootstrap credential the REST route
//! used; hub-api validates it with TokenReview). JWKS needs no credential.

use std::sync::Arc;
use std::time::Duration;

use hyper_util::rt::TokioIo;
use rustls_pki_types::pem::PemObject;
use rustls_pki_types::{CertificateDer, ServerName};
use tokio::net::TcpStream;
use tokio::sync::OnceCell;
use tokio_rustls::TlsConnector;
use tonic::metadata::MetadataValue;
use tonic::transport::{Channel, Endpoint, Uri};
use tonic::{Code, Request};
use tracing::debug;

use crate::{HttpTimeouts, ServiceAuthError};

/// Generated from `proto/waddles/hub/internal/v1/service_auth.proto` (`build.rs`).
pub mod pb {
    tonic::include_proto!("waddles.hub.internal.v1");
}

use pb::service_auth_service_client::ServiceAuthServiceClient;

/// Env var naming the PEM CA bundle that signed hub-api's internal gRPC server certificate
/// (the same variable `core/hub_client` and the chart already use).
pub const ENV_GRPC_CA_FILE: &str = "HUB_API_GRPC_CA_FILE";

/// Wire API version sent on every request.
pub const API_VERSION: &str = "v1";

/// Hub-api rejects any RPC whose deadline exceeds 5s; stay comfortably under it.
pub const MAX_RPC_DEADLINE: Duration = Duration::from_secs(4);

/// HTTP/2 keepalive ping interval for the idle long-lived channel.
const KEEPALIVE_INTERVAL: Duration = Duration::from_secs(30);
/// How long to wait for a keepalive ack before the connection is considered dead.
const KEEPALIVE_TIMEOUT: Duration = Duration::from_secs(10);

/// One bound mint reply: the JWT and its lifetime in seconds.
#[derive(Debug)]
pub(crate) struct MintReply {
    pub token: String,
    pub expires_in: u64,
}

/// A long-lived, lazily-connected channel to hub-api's `ServiceAuthService`. Cheap to clone
/// (clones share the one connection); build one and hand it to several clients to share it.
#[derive(Clone)]
pub struct HubChannel {
    inner: Arc<Inner>,
}

struct Inner {
    endpoint: String,
    ca_file: Option<String>,
    timeouts: HttpTimeouts,
    channel: OnceCell<Channel>,
}

impl std::fmt::Debug for HubChannel {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("HubChannel")
            .field("endpoint", &self.inner.endpoint)
            .finish_non_exhaustive()
    }
}

impl HubChannel {
    /// Channel for `endpoint` (`https://host:port`, or `http://127.0.0.1:port` on loopback)
    /// trusting the CA at `ca_file`. Nothing is validated or dialled until first use, so a bad
    /// endpoint/CA surfaces as an error on every call -- loud, never silently degraded.
    #[must_use]
    pub fn lazy(
        endpoint: impl Into<String>,
        ca_file: Option<String>,
        timeouts: HttpTimeouts,
    ) -> Self {
        Self {
            inner: Arc::new(Inner {
                endpoint: endpoint.into(),
                ca_file,
                timeouts,
                channel: OnceCell::new(),
            }),
        }
    }

    /// As [`HubChannel::lazy`] with the CA taken from `HUB_API_GRPC_CA_FILE` (empty = unset).
    #[must_use]
    pub fn from_env(endpoint: impl Into<String>, timeouts: HttpTimeouts) -> Self {
        let ca = std::env::var(ENV_GRPC_CA_FILE)
            .ok()
            .map(|v| v.trim().to_owned())
            .filter(|v| !v.is_empty());
        Self::lazy(endpoint, ca, timeouts)
    }

    /// As [`HubChannel::lazy`] but validates the endpoint and CA file now, so a misconfigured
    /// service fails at startup rather than at its first mint.
    pub fn validated(
        endpoint: impl Into<String>,
        ca_file: Option<String>,
        timeouts: HttpTimeouts,
    ) -> Result<Self, ServiceAuthError> {
        let chan = Self::lazy(endpoint, ca_file, timeouts);
        let parsed = parse_endpoint(&chan.inner.endpoint)?;
        if parsed.tls {
            tls13_client_config(chan.inner.ca_file.as_deref())?;
        } else {
            require_loopback(&parsed.host)?;
        }
        Ok(chan)
    }

    /// As [`HubChannel::validated`] with the CA from `HUB_API_GRPC_CA_FILE`.
    pub fn validated_from_env(
        endpoint: impl Into<String>,
        timeouts: HttpTimeouts,
    ) -> Result<Self, ServiceAuthError> {
        let env = Self::from_env(endpoint, timeouts);
        Self::validated(
            env.inner.endpoint.clone(),
            env.inner.ca_file.clone(),
            timeouts,
        )
    }

    /// The configured endpoint URL.
    #[must_use]
    pub fn endpoint(&self) -> &str {
        &self.inner.endpoint
    }

    /// Per-RPC deadline: the configured request timeout, capped at [`MAX_RPC_DEADLINE`].
    fn deadline(&self) -> Duration {
        self.inner.timeouts.request.min(MAX_RPC_DEADLINE)
    }

    async fn channel(&self) -> Result<Channel, ServiceAuthError> {
        let channel = self
            .inner
            .channel
            .get_or_try_init(|| async { build_channel(&self.inner).await })
            .await?;
        Ok(channel.clone())
    }

    async fn client(&self) -> Result<ServiceAuthServiceClient<Channel>, ServiceAuthError> {
        Ok(ServiceAuthServiceClient::new(self.channel().await?))
    }

    /// `MintServiceToken`, authenticated with `sa_token` as bearer metadata. A rejection by
    /// hub-api (bad/unknown SA token, scope not allow-listed, invalid binding) is
    /// [`ServiceAuthError::BootstrapRejected`]; an operational failure is `Grpc`.
    pub(crate) async fn mint(
        &self,
        sa_token: &str,
        scope: &str,
        community_id: Option<i64>,
        connection_id: Option<i64>,
    ) -> Result<MintReply, ServiceAuthError> {
        let mut request = Request::new(pb::MintServiceTokenRequest {
            api_version: API_VERSION.to_owned(),
            scope: scope.to_owned(),
            community_id,
            connection_id,
        });
        request.set_timeout(self.deadline());
        let mut bearer: MetadataValue<_> = format!("Bearer {sa_token}").parse().map_err(|_| {
            ServiceAuthError::Config("ServiceAccount token is not valid ASCII".into())
        })?;
        bearer.set_sensitive(true);
        request.metadata_mut().insert("authorization", bearer);
        let response = self
            .client()
            .await?
            .mint_service_token(request)
            .await
            .map_err(mint_status_error)?
            .into_inner();
        Ok(MintReply {
            token: response.token,
            expires_in: response.expires_in,
        })
    }

    /// `GetServiceJwks` -- public keys only, no credential.
    pub(crate) async fn jwks(&self) -> Result<Vec<pb::Jwk>, ServiceAuthError> {
        let mut request = Request::new(pb::GetServiceJwksRequest {
            api_version: API_VERSION.to_owned(),
        });
        request.set_timeout(self.deadline());
        let response = self
            .client()
            .await?
            .get_service_jwks(request)
            .await?
            .into_inner();
        Ok(response.keys)
    }
}

/// Map a mint failure: hub-api *answering and refusing* is a bootstrap rejection (never says
/// which check failed); a server/transport problem is an operational `Grpc` error.
fn mint_status_error(status: tonic::Status) -> ServiceAuthError {
    match status.code() {
        Code::Unavailable
        | Code::DeadlineExceeded
        | Code::Cancelled
        | Code::Unknown
        | Code::Internal
        | Code::ResourceExhausted => ServiceAuthError::Grpc(status),
        code => ServiceAuthError::BootstrapRejected(format!("token endpoint returned {code:?}")),
    }
}

struct ParsedEndpoint {
    tls: bool,
    host: String,
    port: u16,
}

/// Split an endpoint URL into scheme/host/port; every field is mandatory.
fn parse_endpoint(endpoint: &str) -> Result<ParsedEndpoint, ServiceAuthError> {
    let uri: Uri = endpoint.parse().map_err(|e| {
        ServiceAuthError::Config(format!("invalid hub-api gRPC endpoint {endpoint:?}: {e}"))
    })?;
    let tls = match uri.scheme_str() {
        Some("https") => true,
        Some("http") => false,
        _ => {
            return Err(ServiceAuthError::Config(format!(
                "hub-api gRPC endpoint must be http(s)://host:port, got {endpoint:?}"
            )))
        }
    };
    let (Some(host), Some(port)) = (uri.host(), uri.port_u16()) else {
        return Err(ServiceAuthError::Config(format!(
            "hub-api gRPC endpoint needs an explicit host and port, got {endpoint:?}"
        )));
    };
    Ok(ParsedEndpoint {
        tls,
        host: host.trim_matches(['[', ']']).to_owned(),
        port,
    })
}

/// Plaintext gRPC is allowed to loopback only.
fn require_loopback(host: &str) -> Result<(), ServiceAuthError> {
    let loopback = host == "localhost"
        || host
            .parse::<std::net::IpAddr>()
            .map(|ip| ip.is_loopback())
            .unwrap_or(false);
    if loopback {
        Ok(())
    } else {
        Err(ServiceAuthError::Config(format!(
            "refusing plaintext gRPC to non-loopback host {host:?}: the machine-JWT bootstrap \
             carries a ServiceAccount token and must use TLS (https:// endpoint + {ENV_GRPC_CA_FILE})"
        )))
    }
}

/// rustls client config: TLS 1.3 only, trusting exactly the CA bundle at `ca_file`, ALPN `h2`.
fn tls13_client_config(ca_file: Option<&str>) -> Result<rustls::ClientConfig, ServiceAuthError> {
    let path = ca_file.ok_or_else(|| {
        ServiceAuthError::Config(format!(
            "{ENV_GRPC_CA_FILE} is required for an https:// hub-api gRPC endpoint"
        ))
    })?;
    let pem = std::fs::read(path)
        .map_err(|e| ServiceAuthError::Config(format!("cannot read gRPC CA file {path:?}: {e}")))?;
    let mut roots = rustls::RootCertStore::empty();
    for cert in CertificateDer::pem_slice_iter(&pem) {
        let cert = cert.map_err(|e| {
            ServiceAuthError::Config(format!("bad PEM in gRPC CA file {path:?}: {e}"))
        })?;
        roots.add(cert).map_err(|e| {
            ServiceAuthError::Config(format!("bad CA certificate in {path:?}: {e}"))
        })?;
    }
    if roots.is_empty() {
        return Err(ServiceAuthError::Config(format!(
            "gRPC CA file {path:?} contains no certificates"
        )));
    }
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let mut config = rustls::ClientConfig::builder_with_provider(provider)
        .with_protocol_versions(&[&rustls::version::TLS13])
        .map_err(|e| ServiceAuthError::Config(format!("TLS 1.3 unavailable: {e}")))?
        .with_root_certificates(roots)
        .with_no_client_auth();
    config.alpn_protocols = vec![b"h2".to_vec()];
    Ok(config)
}

async fn build_channel(inner: &Inner) -> Result<Channel, ServiceAuthError> {
    let parsed = parse_endpoint(&inner.endpoint)?;
    // TLS is terminated by OUR connector below (TLS 1.3 only), so tonic is handed an `http`
    // authority: its own `https` path would demand a `ClientTlsConfig` we deliberately don't use.
    let authority = if parsed.host.contains(':') {
        format!("http://[{}]:{}", parsed.host, parsed.port)
    } else {
        format!("http://{}:{}", parsed.host, parsed.port)
    };
    let endpoint = Endpoint::from_shared(authority)
        .map_err(|e| ServiceAuthError::Config(format!("invalid hub-api gRPC endpoint: {e}")))?
        .connect_timeout(inner.timeouts.connect)
        .tcp_nodelay(true)
        .http2_keep_alive_interval(KEEPALIVE_INTERVAL)
        .keep_alive_timeout(KEEPALIVE_TIMEOUT)
        .keep_alive_while_idle(true);
    if !parsed.tls {
        require_loopback(&parsed.host)?;
        debug!(endpoint = %inner.endpoint, "service_auth.grpc_channel_plaintext_loopback");
        return Ok(endpoint.connect_lazy());
    }

    let tls = Arc::new(tls13_client_config(inner.ca_file.as_deref())?);
    let server_name = ServerName::try_from(parsed.host.clone()).map_err(|e| {
        ServiceAuthError::Config(format!("bad TLS server name {:?}: {e}", parsed.host))
    })?;
    let connect_timeout = inner.timeouts.connect;
    let (host, port) = (parsed.host, parsed.port);
    let connector = tower::service_fn(move |_uri: Uri| {
        let connector = TlsConnector::from(tls.clone());
        let server_name = server_name.clone();
        let host = host.clone();
        async move {
            let dial = async {
                let tcp = TcpStream::connect((host.as_str(), port)).await?;
                tcp.set_nodelay(true)?;
                connector.connect(server_name, tcp).await
            };
            match tokio::time::timeout(connect_timeout, dial).await {
                Ok(Ok(stream)) => Ok(TokioIo::new(stream)),
                Ok(Err(e)) => Err(e),
                Err(_) => Err(std::io::Error::new(
                    std::io::ErrorKind::TimedOut,
                    "hub-api gRPC connect timed out",
                )),
            }
        }
    });
    debug!(endpoint = %inner.endpoint, "service_auth.grpc_channel_tls13");
    Ok(endpoint.connect_with_connector_lazy(connector))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn endpoint_must_carry_scheme_host_and_port() {
        for bad in ["hub:50204", "ftp://h:1", "https://hub", "http://", "::"] {
            assert!(
                matches!(parse_endpoint(bad), Err(ServiceAuthError::Config(_))),
                "{bad}"
            );
        }
        let ok = parse_endpoint("https://hub-api.ns.svc:50204").expect("parse");
        assert!(ok.tls && ok.host == "hub-api.ns.svc" && ok.port == 50204);
        assert!(!parse_endpoint("http://[::1]:7").expect("v6").tls);
        assert_eq!(parse_endpoint("http://[::1]:7").expect("v6").host, "::1");
    }

    #[test]
    fn plaintext_is_loopback_only() {
        for host in ["127.0.0.1", "127.9.9.9", "::1", "localhost"] {
            assert!(require_loopback(host).is_ok(), "{host}");
        }
        for host in ["10.0.0.5", "hub-api", "192.168.1.1", "example.com"] {
            let err = require_loopback(host).expect_err(host);
            assert!(err.to_string().contains("non-loopback"), "{err}");
        }
    }

    #[test]
    fn https_without_a_ca_is_an_error_not_a_fallback() {
        let err = tls13_client_config(None).expect_err("must refuse");
        assert!(err.to_string().contains(ENV_GRPC_CA_FILE), "{err}");
    }

    #[test]
    fn ca_file_problems_are_loud() {
        let missing = tls13_client_config(Some("/nonexistent/ca.pem")).expect_err("missing");
        assert!(missing.to_string().contains("cannot read gRPC CA file"));
        let dir = std::env::temp_dir();
        let empty = dir.join(format!("service_auth_empty_ca_{}.pem", std::process::id()));
        std::fs::write(&empty, "not a certificate").expect("write");
        let err = tls13_client_config(empty.to_str()).expect_err("no certs");
        assert!(err.to_string().contains("no certificates"), "{err}");
        let _ = std::fs::remove_file(empty);
    }

    #[test]
    fn mint_status_mapping_separates_rejection_from_outage() {
        for code in [
            Code::Unauthenticated,
            Code::PermissionDenied,
            Code::InvalidArgument,
        ] {
            assert!(matches!(
                mint_status_error(tonic::Status::new(code, "x")),
                ServiceAuthError::BootstrapRejected(_)
            ));
        }
        for code in [Code::Unavailable, Code::DeadlineExceeded, Code::Internal] {
            assert!(matches!(
                mint_status_error(tonic::Status::new(code, "x")),
                ServiceAuthError::Grpc(_)
            ));
        }
    }

    #[test]
    fn deadline_is_capped_under_the_hub_ceiling() {
        let slow = HttpTimeouts {
            connect: Duration::from_secs(3),
            request: Duration::from_secs(10),
        };
        let chan = HubChannel::lazy("http://127.0.0.1:1", None, slow);
        assert_eq!(chan.deadline(), MAX_RPC_DEADLINE);
        let fast = HttpTimeouts {
            connect: Duration::from_secs(1),
            request: Duration::from_millis(500),
        };
        assert_eq!(
            HubChannel::lazy("http://127.0.0.1:1", None, fast).deadline(),
            Duration::from_millis(500)
        );
    }

    #[test]
    fn validated_rejects_bad_config_up_front() {
        let t = HttpTimeouts::default();
        assert!(HubChannel::validated("http://10.1.1.1:50204", None, t).is_err());
        assert!(HubChannel::validated("https://hub:50204", None, t).is_err());
        assert!(HubChannel::validated("http://127.0.0.1:50204", None, t).is_ok());
    }
}
