//! gRPC ingress for the surface path -- the only in-cluster way to publish.
//!
//! `waddles.presentation.internal.v1.SurfaceService/Publish` replaces the REST route the stale
//! overlay builder carried: in-cluster service-to-service traffic is gRPC only. Authentication is
//! unchanged in substance: every call carries a hub-api-minted machine JWT whose scope is bound to
//! exactly the request's `community_id` (`overlay_auth::authorize_push`), verified BEFORE the
//! payload is looked at. The community the frame is keyed by is therefore the one the signature
//! vouches for, never an unchecked field.
//!
//! Logs carry ids, codes and counts only -- never payload content.

use std::net::SocketAddr;
use std::path::Path;
use std::time::Instant;

use bundle_host_surface::pb::surface_service_server::{SurfaceService as SurfaceRpc, SurfaceServiceServer};
use bundle_host_surface::pb::{PublishRequest, PublishResponse};
use bundle_host_surface::{API_VERSION, RETRY_AFTER_MS_KEY};
use overlay_auth::OverlayAuthError;
use overlay_schema::{AlertPayload, OverlayPush, Surface};
use tonic::transport::{Identity, Server, ServerTlsConfig};
use tonic::{Request, Response, Status};

use super::hub::SurfaceFrame;
use super::PublishError;
use crate::http::AppState;
use crate::overlay::detok::DetokScope;
use crate::overlay::render::RenderTheme;

/// Largest request message the server decodes (contract hard cap plus envelope slack).
const MAX_MESSAGE_BYTES: usize = 32 * 1024;

/// The gRPC service. Cheap to build; holds a clone of the shared [`AppState`].
#[derive(Clone)]
pub struct SurfaceIngress {
    state: AppState,
}

impl SurfaceIngress {
    /// Wraps the shared state.
    pub fn new(state: AppState) -> Self {
        Self { state }
    }

    /// The tonic server wrapper with the message-size cap applied.
    pub fn into_server(self) -> SurfaceServiceServer<Self> {
        SurfaceServiceServer::new(self).max_decoding_message_size(MAX_MESSAGE_BYTES)
    }
}

/// Maps a credential failure to a status without revealing which check failed.
fn auth_status(err: OverlayAuthError) -> Status {
    match err {
        OverlayAuthError::MissingKey | OverlayAuthError::MissingBearer => {
            Status::unauthenticated("missing credential")
        }
        OverlayAuthError::ServiceAuth(
            service_auth::ServiceAuthError::Grpc(_)
            | service_auth::ServiceAuthError::Io(_)
            | service_auth::ServiceAuthError::Config(_),
        ) => Status::unavailable("credential verification unavailable"),
        OverlayAuthError::Store(detail) => {
            tracing::error!(error = %detail, "surface ingress credential store error");
            Status::internal("internal error")
        }
        _ => Status::permission_denied("forbidden"),
    }
}

/// Maps a publish refusal to a status. Violations carry `path:rule` only.
pub fn publish_status(err: PublishError) -> Status {
    match err {
        PublishError::UnknownSurface => Status::not_found("unknown surface"),
        PublishError::TooLarge { max } => {
            Status::out_of_range(format!("payload exceeds {max} bytes for this surface"))
        }
        PublishError::RateLimited { retry_after_secs } => rate_limited(retry_after_secs),
        PublishError::Saturated => rate_limited(5),
        PublishError::NotJson => Status::invalid_argument("payload is not valid JSON"),
        PublishError::Unsanitary(msg) => Status::invalid_argument(msg),
        PublishError::Invalid(violations) => {
            let listed: Vec<String> = violations
                .iter()
                .take(8)
                .map(|v| format!("{}:{}", v.path, v.rule))
                .collect();
            Status::invalid_argument(format!(
                "payload does not match the surface schema [{}]",
                listed.join(", ")
            ))
        }
    }
}

fn rate_limited(retry_after_secs: u64) -> Status {
    let mut status = Status::resource_exhausted("rate limited");
    let ms = retry_after_secs.saturating_mul(1000).to_string();
    if let Ok(value) = ms.parse() {
        status.metadata_mut().insert(RETRY_AFTER_MS_KEY, value);
    }
    status
}

#[tonic::async_trait]
impl SurfaceRpc for SurfaceIngress {
    #[tracing::instrument(
        name = "surfaces.publish",
        skip_all,
        fields(community_id = request.get_ref().community_id, surface = %request.get_ref().surface_id)
    )]
    async fn publish(
        &self,
        request: Request<PublishRequest>,
    ) -> Result<Response<PublishResponse>, Status> {
        let headers = request.metadata().clone().into_headers();
        let req = request.into_inner();
        if req.api_version != API_VERSION {
            return Err(Status::unimplemented(format!(
                "api_version {:?} not supported",
                req.api_version
            )));
        }
        if req.community_id <= 0 {
            return Err(Status::invalid_argument("community_id must be positive"));
        }
        // Authenticate first: nothing about the payload is examined for an unverified caller.
        let credential =
            overlay_auth::authorize_push(&self.state.push_trust_source, &headers, req.community_id)
                .await
                .map_err(auth_status)?;
        let community_id = credential.community_id;

        let frame = self
            .state
            .surfaces
            .process(
                community_id,
                &req.surface_id,
                req.payload_json.as_bytes(),
                Instant::now(),
            )
            .map_err(publish_status)?;
        let bridged = if frame.surface == "alert" {
            bridge_alert(&self.state, community_id, &frame).await
        } else {
            false
        };
        tracing::debug!(community_id, seq = frame.seq, bridged, "surface frame published");
        Ok(Response::new(PublishResponse { seq: frame.seq }))
    }
}

/// Feeds a validated `alert` surface frame through the existing `alert_box` renderer (escaping
/// included) into the live overlay hub, so OBS alert overlays keep working. A bridge failure is
/// logged loudly but does not fail the publish: the surface fan-out already succeeded.
async fn bridge_alert(state: &AppState, community_id: i64, frame: &SurfaceFrame) -> bool {
    let p = &frame.payload;
    let (Some(alert_type), Some(message)) = (
        p.get("alert_type").and_then(|v| v.as_str()),
        p.get("message").and_then(|v| v.as_str()),
    ) else {
        tracing::error!("validated alert frame lacks alert_type/message; not bridged");
        return false;
    };
    let push = OverlayPush {
        alert: Some(AlertPayload {
            alert_type: alert_type.to_string(),
            user: None,
            display_name: None,
            amount: p.get("amount").cloned(),
            message: Some(message.to_string()),
        }),
        ..Default::default()
    };
    // Sanitization already removed every user token, so no resolve call (and no tenant) is
    // needed; the default theme avoids a database read.
    let scope = DetokScope::new(String::new(), community_id);
    match state
        .detokenizer
        .render_with_metrics(
            &scope,
            Surface::AlertBox,
            &push,
            &RenderTheme::default(),
            &state.render_metrics,
        )
        .await
    {
        Ok(rendered) => {
            state
                .frame_hub
                .publish(community_id, Surface::AlertBox, rendered);
            true
        }
        Err(err) => {
            tracing::error!(community_id, error = %err, "alert surface frame could not be bridged to alert_box");
            false
        }
    }
}

/// Why the gRPC listener could not be configured. Always fatal at startup.
#[derive(Debug, thiserror::Error)]
pub enum IngressConfigError {
    /// Plaintext requested on a non-loopback address.
    #[error(
        "refusing to serve the surface gRPC listener in plaintext on non-loopback {0}: \
         set SURFACE_GRPC_TLS_CERT_FILE and SURFACE_GRPC_TLS_KEY_FILE"
    )]
    PlaintextNotLoopback(SocketAddr),
    /// Only one of cert/key was given.
    #[error("SURFACE_GRPC_TLS_CERT_FILE and SURFACE_GRPC_TLS_KEY_FILE must be set together")]
    HalfTlsPair,
    /// A TLS file could not be read.
    #[error("cannot read TLS file {path:?}: {reason}")]
    TlsRead {
        /// The file that failed.
        path: String,
        /// The OS error.
        reason: String,
    },
    /// tonic rejected the TLS configuration.
    #[error("TLS configuration: {0}")]
    Tls(String),
}

/// Builds the server TLS config from PEM files, or `None` for plaintext. Plaintext is accepted on
/// loopback only (dev + in-process tests); anything reachable from the cluster needs TLS.
pub fn server_tls(
    addr: SocketAddr,
    cert_file: &str,
    key_file: &str,
) -> Result<Option<ServerTlsConfig>, IngressConfigError> {
    match (cert_file.is_empty(), key_file.is_empty()) {
        (true, true) => {
            if addr.ip().is_loopback() {
                Ok(None)
            } else {
                Err(IngressConfigError::PlaintextNotLoopback(addr))
            }
        }
        (false, false) => {
            let read = |p: &str| {
                std::fs::read(Path::new(p)).map_err(|e| IngressConfigError::TlsRead {
                    path: p.to_string(),
                    reason: e.to_string(),
                })
            };
            let identity = Identity::from_pem(read(cert_file)?, read(key_file)?);
            Ok(Some(ServerTlsConfig::new().identity(identity)))
        }
        _ => Err(IngressConfigError::HalfTlsPair),
    }
}

/// Serves the surface gRPC listener on `listener` until `shutdown` resolves.
pub async fn serve(
    state: AppState,
    listener: tokio::net::TcpListener,
    tls: Option<ServerTlsConfig>,
    shutdown: impl std::future::Future<Output = ()> + Send + 'static,
) -> anyhow::Result<()> {
    let mut builder = Server::builder();
    if let Some(tls) = tls {
        builder = builder
            .tls_config(tls)
            .map_err(|e| IngressConfigError::Tls(e.to_string()))?;
    }
    builder
        .add_service(SurfaceIngress::new(state).into_server())
        .serve_with_incoming_shutdown(
            tokio_stream::wrappers::TcpListenerStream::new(listener),
            shutdown,
        )
        .await?;
    Ok(())
}
