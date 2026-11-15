//! The host-API listener: the mTLS control surface the stage's `webrtc`
//! capability handler calls to serve the `webrtc` service bundle's tools.
//!
//! Same posture as the stages' `host_api.rs`: server cert/key plus a REQUIRED
//! client CA (`HOST_API_SERVER_CERT_FILE`, `HOST_API_SERVER_KEY_FILE`,
//! `HOST_API_CLIENT_CA_FILE`) -- a peer without a cert signed by that CA never
//! reaches a handler. The community id in each request body is trusted
//! BECAUSE the caller is the authenticated stage (it is derived from the
//! bundle's invocation context, never from guest-supplied args).
//!
//! `POST /host/v1/webrtc/{op}` with a JSON body; success `200 {..}`, failure
//! `{"error": <code>, "message": ..}` where `code` is one of the stable codes
//! the executor maps onto the WIT `webrtc.error` variant.

use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use axum::extract::{Path as AxPath, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::post;
use axum::{Json, Router};
use hyper_util::rt::{TokioExecutor, TokioIo};
use hyper_util::server::conn::auto::Builder;
use hyper_util::service::TowerToHyperService;
use rustls::server::WebPkiClientVerifier;
use rustls::{RootCertStore, ServerConfig};
use rustls_pki_types::pem::PemObject;
use rustls_pki_types::{CertificateDer, PrivateKeyDer};
use serde::Deserialize;
use serde_json::{json, Value};
use tokio_rustls::TlsAcceptor;
use uuid::Uuid;

use super::{IssueRequest, RoomError, RoomService, RouteMode};

/// Errors building/running the listener.
#[derive(Debug, thiserror::Error)]
pub enum HostApiError {
    #[error("host-api config error: {0}")]
    Config(String),
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
    #[error("tls error: {0}")]
    Tls(#[from] rustls::Error),
}

/// TLS file locations, read from the environment.
#[derive(Debug, Clone)]
pub struct HostApiTls {
    pub server_cert: PathBuf,
    pub server_key: PathBuf,
    pub client_ca: PathBuf,
    pub port: u16,
}

impl HostApiTls {
    /// `Ok(None)` when no host-API TLS is configured at all (listener not
    /// started, logged); a partial configuration is an error.
    pub fn from_env() -> Result<Option<Self>, HostApiError> {
        let get = |k: &str| std::env::var(k).ok().filter(|v| !v.is_empty());
        let (cert, key, ca) = (
            get("HOST_API_SERVER_CERT_FILE"),
            get("HOST_API_SERVER_KEY_FILE"),
            get("HOST_API_CLIENT_CA_FILE"),
        );
        match (cert, key, ca) {
            (None, None, None) => Ok(None),
            (Some(c), Some(k), Some(a)) => {
                let port = match get("WEBRTC_HOST_API_PORT") {
                    Some(p) => p.parse().map_err(|_| {
                        HostApiError::Config("WEBRTC_HOST_API_PORT is not a port".into())
                    })?,
                    None => 8209,
                };
                Ok(Some(Self {
                    server_cert: c.into(),
                    server_key: k.into(),
                    client_ca: a.into(),
                    port,
                }))
            }
            _ => Err(HostApiError::Config(
                "HOST_API_SERVER_CERT_FILE, HOST_API_SERVER_KEY_FILE and HOST_API_CLIENT_CA_FILE \
                 must all be set (mTLS client verification is not optional)"
                    .into(),
            )),
        }
    }
}

fn load_certs(path: &Path) -> Result<Vec<CertificateDer<'static>>, HostApiError> {
    CertificateDer::pem_file_iter(path)
        .map_err(|e| HostApiError::Config(format!("reading certs from {path:?}: {e}")))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| HostApiError::Config(format!("invalid PEM in {path:?}: {e}")))
}

/// Builds the mTLS `ServerConfig` (client certificate required).
pub fn build_server_config(tls: &HostApiTls) -> Result<ServerConfig, HostApiError> {
    let certs = load_certs(&tls.server_cert)?;
    let key = PrivateKeyDer::from_pem_file(&tls.server_key)
        .map_err(|e| HostApiError::Config(format!("reading private key: {e}")))?;
    let mut roots = RootCertStore::empty();
    for c in load_certs(&tls.client_ca)? {
        roots
            .add(c)
            .map_err(|e| HostApiError::Config(format!("invalid CA certificate: {e}")))?;
    }
    let verifier = WebPkiClientVerifier::builder_with_provider(
        Arc::new(roots),
        Arc::new(rustls::crypto::ring::default_provider()),
    )
    .build()
    .map_err(|e| HostApiError::Config(format!("invalid client verifier: {e}")))?;
    // Explicit provider: this binary links more than one rustls backend.
    ServerConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
        .with_safe_default_protocol_versions()?
        .with_client_cert_verifier(verifier)
        .with_single_cert(certs, key)
        .map_err(Into::into)
}

fn err_response(e: RoomError) -> Response {
    let (status, code) = match &e {
        RoomError::NotFound => (StatusCode::NOT_FOUND, "not_found"),
        RoomError::Invalid(_) => (StatusCode::BAD_REQUEST, "invalid_args"),
        RoomError::LimitReached(_) => (StatusCode::TOO_MANY_REQUESTS, "quota_exceeded"),
        RoomError::Forbidden(_) | RoomError::Unauthorized => (StatusCode::FORBIDDEN, "forbidden"),
        RoomError::Unconfigured(_) => (StatusCode::SERVICE_UNAVAILABLE, "unavailable"),
        RoomError::Unsupported(_) => (StatusCode::NOT_IMPLEMENTED, "not_implemented"),
        RoomError::Ambiguous(_) => (StatusCode::BAD_REQUEST, "invalid_args"),
        RoomError::Backend(_) => (StatusCode::BAD_GATEWAY, "backend"),
    };
    // Messages are built from closed strings and ids only.
    (
        status,
        Json(json!({"error": code, "message": e.to_string()})),
    )
        .into_response()
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CreateReq {
    community_id: String,
    label: Option<String>,
    max_publishers: Option<usize>,
    route: Option<RouteMode>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RoomReq {
    community_id: String,
    room_id: Uuid,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TokenReq {
    community_id: String,
    room_id: Uuid,
    participant_id: Option<Uuid>,
    publish: bool,
    subscribe: bool,
    source: Option<Uuid>,
    ttl_secs: Option<i64>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RevokeReq {
    community_id: String,
    room_id: Uuid,
    token_id: Uuid,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PipelineTokenReq {
    community_id: String,
    pipeline_id: Uuid,
    ttl_secs: Option<i64>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PipelineRevokeReq {
    community_id: String,
    pipeline_id: Uuid,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct EndpointReq {
    community_id: String,
    room_id: Uuid,
    participant_id: Option<Uuid>,
    source: Option<Uuid>,
    ttl_secs: Option<i64>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RouteReq {
    community_id: String,
    room_id: Uuid,
    mode: RouteMode,
}

fn parse<T: for<'de> Deserialize<'de>>(v: Value) -> Result<T, RoomError> {
    serde_json::from_value(v).map_err(|e| RoomError::Invalid(format!("bad request body: {e}")))
}

async fn dispatch(svc: &RoomService, op: &str, body: Value) -> Result<Value, RoomError> {
    match op {
        "room.create" => {
            let r: CreateReq = parse(body)?;
            let info = svc
                .create(
                    &r.community_id,
                    r.label,
                    r.max_publishers,
                    r.route.unwrap_or(RouteMode::Sfu),
                )
                .await?;
            Ok(json!(info))
        }
        "room.get" => {
            let r: RoomReq = parse(body)?;
            Ok(json!(svc.get(&r.community_id, r.room_id).await?))
        }
        "room.close" => {
            let r: RoomReq = parse(body)?;
            Ok(json!(svc.close(&r.community_id, r.room_id).await?))
        }
        "token.issue" => {
            let r: TokenReq = parse(body)?;
            let participant = r.participant_id.unwrap_or_else(Uuid::new_v4);
            let t = svc
                .issue_token(
                    &r.community_id,
                    r.room_id,
                    IssueRequest {
                        participant,
                        publish: r.publish,
                        subscribe: r.subscribe,
                        source: r.source,
                        ttl_secs: r.ttl_secs,
                    },
                )
                .await?;
            Ok(json!({"token": t.token, "token_id": t.token_id,
                      "participant_id": participant, "expires_at": t.expires_at}))
        }
        "token.revoke" => {
            let r: RevokeReq = parse(body)?;
            svc.revoke_token(&r.community_id, r.room_id, r.token_id).await?;
            Ok(json!({"revoked": true, "token_id": r.token_id}))
        }
        "token.revoke_all" => {
            let r: RoomReq = parse(body)?;
            let epoch = svc.revoke_all(&r.community_id, r.room_id).await?;
            Ok(json!({"room_id": r.room_id, "epoch": epoch}))
        }
        "pipeline.revoke_viewers" => {
            let r: PipelineRevokeReq = parse(body)?;
            let epoch = svc.revoke_pipeline_viewers(&r.community_id, r.pipeline_id).await?;
            Ok(json!({"pipeline_id": r.pipeline_id, "epoch": epoch}))
        }
        "pipeline.whep_token" => {
            let r: PipelineTokenReq = parse(body)?;
            let (t, participant) = svc
                .issue_pipeline_token(&r.community_id, r.pipeline_id, r.ttl_secs)
                .await?;
            let url = format!(
                "{}/whep/{}/{}",
                svc.public_base_url().trim_end_matches('/'),
                r.community_id,
                r.pipeline_id
            );
            Ok(json!({"url": url, "token": t.token, "token_id": t.token_id,
                      "participant_id": participant, "expires_at": t.expires_at}))
        }
        "whip.endpoint" | "whep.endpoint" => {
            let r: EndpointReq = parse(body)?;
            let publish = op == "whip.endpoint";
            if publish && r.source.is_some() {
                return Err(RoomError::Invalid("source applies to whep.endpoint".into()));
            }
            let participant = r.participant_id.unwrap_or_else(Uuid::new_v4);
            let t = svc
                .issue_token(
                    &r.community_id,
                    r.room_id,
                    IssueRequest {
                        participant,
                        publish,
                        subscribe: !publish,
                        source: r.source,
                        ttl_secs: r.ttl_secs,
                    },
                )
                .await?;
            let leaf = if publish { "whip" } else { "whep" };
            let url = format!(
                "{}/rtc/rooms/{}/{leaf}",
                svc.public_base_url().trim_end_matches('/'),
                r.room_id
            );
            Ok(json!({"url": url, "token": t.token, "token_id": t.token_id,
                      "participant_id": participant, "expires_at": t.expires_at}))
        }
        "track.route" => {
            let r: RouteReq = parse(body)?;
            let info = svc.get(&r.community_id, r.room_id).await?;
            match r.mode {
                RouteMode::Sfu => Ok(json!({"room_id": info.room_id, "mode": "sfu"})),
                RouteMode::Relay => Err(RoomError::Unsupported(
                    "route mode relay is unsupported; rooms use the local SFU".into(),
                )),
            }
        }
        "recording.start" | "recording.stop" => Err(RoomError::Unsupported(
            "room recording is unavailable: room publishers have no ffmpeg bridge/RecordSink path yet"
                .into(),
        )),
        _ => Err(RoomError::Invalid(format!("unknown webrtc op {op:?}"))),
    }
}

async fn handle(
    State(svc): State<Arc<RoomService>>,
    AxPath(op): AxPath<String>,
    Json(body): Json<Value>,
) -> Response {
    tracing::debug!(op = %op, "webrtc host-api call");
    match dispatch(&svc, &op, body).await {
        Ok(v) => Json(v).into_response(),
        Err(e) => {
            tracing::debug!(op = %op, error = %e, "webrtc host-api call refused");
            err_response(e)
        }
    }
}

/// Router for `/host/v1/webrtc/{op}` (no TLS; wrap with [`serve`] in production).
pub fn router(svc: Arc<RoomService>) -> Router {
    Router::new()
        .route("/host/v1/webrtc/{op}", post(handle))
        .with_state(svc)
}

/// Accepts mTLS connections on `listener` and serves [`router`] until the
/// future is dropped. A failed handshake (no/invalid client cert) closes the
/// connection before any request is read.
pub async fn serve(
    listener: tokio::net::TcpListener,
    server_config: ServerConfig,
    svc: Arc<RoomService>,
) -> Result<(), HostApiError> {
    let acceptor = TlsAcceptor::from(Arc::new(server_config));
    let app = router(svc);
    loop {
        let (tcp, peer) = listener.accept().await?;
        let acceptor = acceptor.clone();
        let app = app.clone();
        tokio::spawn(async move {
            match acceptor.accept(tcp).await {
                Ok(tls) => {
                    let io = TokioIo::new(tls);
                    let svc = TowerToHyperService::new(app);
                    if let Err(e) = Builder::new(TokioExecutor::new())
                        .serve_connection_with_upgrades(io, svc)
                        .await
                    {
                        tracing::debug!(error = %e, "webrtc host-api connection ended");
                    }
                }
                Err(e) => {
                    tracing::warn!(%peer, error = %e, "webrtc host-api mTLS handshake refused")
                }
            }
        });
    }
}

/// Binds and serves the listener from env config, or logs why it is off.
pub async fn spawn_from_env(
    bind: std::net::IpAddr,
    svc: Arc<RoomService>,
) -> Result<(), HostApiError> {
    let Some(tls) = HostApiTls::from_env()? else {
        tracing::warn!("webrtc host-api disabled: HOST_API_* mTLS files are not configured");
        return Ok(());
    };
    let cfg = build_server_config(&tls)?;
    let listener = tokio::net::TcpListener::bind(SocketAddr::new(bind, tls.port)).await?;
    tracing::info!(port = tls.port, "webrtc host-api (mTLS) listening");
    tokio::spawn(async move {
        if let Err(e) = serve(listener, cfg, svc).await {
            tracing::error!(error = %e, "webrtc host-api listener stopped");
        }
    });
    Ok(())
}
