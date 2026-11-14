//! `RoomsService` gRPC server: the hub-api -> svc-streaming control plane for call
//! rooms (contract: `proto/waddles/streaming/internal/v1/rooms.proto`).
//!
//! Transport: HTTP/2 over the same mTLS accept loop as the host-API
//! ([`super::host_api::serve_router`]; client cert required, ALPN `h2`). Every RPC
//! additionally verifies a hub-api ES256 machine JWT ([`super::grpc_auth`]) for the
//! scope it needs, before any request field is parsed. All room logic is delegated
//! to [`RoomService`] (the same methods the host-API ops use), so AuthZ (actor role
//! -> moderator/admin), the `rooms.moderation` flag, and the room/moderation
//! metrics and spans behave identically on both surfaces. This layer adds only
//! per-RPC telemetry (`rooms_grpc_requests_total`, `rooms_grpc_request_seconds`, a
//! `rooms.grpc` span) and the error mapping to gRPC status codes.

use std::future::Future;
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Instant;

use tonic::{Request, Response, Status};
use tracing::Instrument;
use uuid::Uuid;

use super::grpc_auth::{
    ServiceJwtVerifier, SCOPE_MODERATE, SCOPE_READ, SCOPE_WRITE,
};
use super::host_api::{build_server_config, serve_router, HostApiError, HostApiTls};
use super::{ActorRole, IssueRequest, ParticipantInfo, RoomError, RoomInfo, RoomService, RouteMode};

/// Generated protobuf/tonic types for `waddles.streaming.internal.v1`.
pub mod pb {
    tonic::include_proto!("waddles.streaming.internal.v1");
}

use pb::rooms_service_server::{RoomsService, RoomsServiceServer};
use pb::*;

/// The only `api_version` this server speaks. Anything else (including empty) is refused with
/// `UNIMPLEMENTED` before any request field is acted on (rule `backend.md` API Versioning).
pub const API_VERSION: &str = "v1";

/// Implemented by every `RoomsService` request message so [`RoomsGrpc::run`] can enforce
/// [`API_VERSION`] uniformly instead of each handler remembering to.
trait Versioned {
    /// The request's `api_version` field.
    fn api_version(&self) -> &str;
}

macro_rules! versioned {
    ($($t:ty),+ $(,)?) => {
        $(impl Versioned for $t {
            fn api_version(&self) -> &str {
                &self.api_version
            }
        })+
    };
}

versioned!(
    ListRoomsRequest,
    GetRoomRequest,
    ListParticipantsRequest,
    ListHandsRequest,
    CreateRoomRequest,
    DeleteRoomRequest,
    JoinRoomRequest,
    LeaveRoomRequest,
    RaiseHandRequest,
    LowerHandRequest,
    KickParticipantRequest,
    MuteParticipantRequest,
    UnmuteParticipantRequest,
    MuteAllRequest,
    LockRoomRequest,
    UnlockRoomRequest,
    AcknowledgeHandRequest,
);

/// Refuses any `api_version` other than [`API_VERSION`] with `UNIMPLEMENTED`.
fn check_api_version(v: &str) -> Result<(), Status> {
    if v == API_VERSION {
        Ok(())
    } else {
        Err(Status::unimplemented(format!("api_version {v} not supported")))
    }
}

/// Every RPC name in `RoomsService`, kept in lockstep with the proto by the
/// contract test (`tests/rooms_grpc.rs`) and used as the metric `rpc` label.
pub const RPC_NAMES: [&str; 17] = [
    "ListRooms",
    "GetRoom",
    "ListParticipants",
    "CreateRoom",
    "DeleteRoom",
    "JoinRoom",
    "LeaveRoom",
    "KickParticipant",
    "MuteParticipant",
    "UnmuteParticipant",
    "MuteAll",
    "LockRoom",
    "UnlockRoom",
    "ListHands",
    "RaiseHand",
    "LowerHand",
    "AcknowledgeHand",
];

/// The `RoomsService` implementation: JWT-gated, delegating to [`RoomService`].
pub struct RoomsGrpc {
    rooms: Arc<RoomService>,
    auth: Arc<ServiceJwtVerifier>,
}

impl RoomsGrpc {
    /// Builds the service over the shared room state and the JWT verifier.
    pub fn new(rooms: Arc<RoomService>, auth: Arc<ServiceJwtVerifier>) -> Self {
        Self { rooms, auth }
    }

    /// Verifies the caller's machine JWT for `scope`; failures never say which check failed.
    fn authenticate<T>(&self, req: &Request<T>, scope: &str) -> Result<(), Status> {
        let token = req
            .metadata()
            .get("authorization")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.strip_prefix("Bearer "))
            .ok_or_else(|| Status::unauthenticated("missing bearer token"))?;
        self.auth
            .verify(token, scope)
            .map(|_| ())
            .map_err(|r| {
                if r.reason == crate::http::auth::hardening::REASON_SCOPE_DENIED {
                    Status::permission_denied("scope not permitted")
                } else {
                    Status::unauthenticated("invalid token")
                }
            })
    }

    /// Runs one RPC: authenticate, execute, then record the per-RPC metric/latency/span.
    async fn run<R, T, Fut>(
        &self,
        rpc: &'static str,
        scope: &'static str,
        req: Request<R>,
        f: impl FnOnce(Arc<RoomService>, R) -> Fut,
    ) -> Result<Response<T>, Status>
    where
        R: Versioned,
        Fut: Future<Output = Result<T, RoomError>>,
    {
        let started = Instant::now();
        let span = tracing::info_span!("rooms.grpc", rpc);
        let result = async {
            self.authenticate(&req, scope)?;
            check_api_version(req.get_ref().api_version())?;
            f(self.rooms.clone(), req.into_inner())
                .await
                .map(Response::new)
                .map_err(status_of)
        }
        .instrument(span)
        .await;
        let code = match &result {
            Ok(_) => "ok".to_string(),
            Err(s) => format!("{:?}", s.code()).to_lowercase(),
        };
        let m = &self.rooms.metrics;
        m.grpc_requests_total.with_label_values(&[rpc, &code]).inc();
        m.grpc_request_seconds
            .with_label_values(&[rpc])
            .observe(started.elapsed().as_secs_f64());
        if let Err(s) = &result {
            tracing::debug!(rpc, code = %code, message = %s.message(), "rooms grpc call refused");
        }
        result
    }
}

/// Maps a [`RoomError`] onto the gRPC status the hub-api client turns into HTTP.
fn status_of(e: RoomError) -> Status {
    let msg = e.to_string();
    match e {
        RoomError::NotFound => Status::not_found(msg),
        RoomError::Invalid(_) | RoomError::Ambiguous(_) => Status::invalid_argument(msg),
        RoomError::LimitReached(_) => Status::resource_exhausted(msg),
        RoomError::Forbidden(_) | RoomError::Unauthorized => Status::permission_denied(msg),
        RoomError::Unconfigured(_) | RoomError::Backend(_) => Status::unavailable(msg),
        RoomError::Unsupported(_) => Status::unimplemented(msg),
    }
}

fn uuid(field: &str, v: &str) -> Result<Uuid, RoomError> {
    Uuid::parse_str(v).map_err(|_| RoomError::Invalid(format!("{field} must be a UUID")))
}

fn role(v: i32) -> ActorRole {
    match RoomRole::try_from(v).unwrap_or(RoomRole::Unspecified) {
        RoomRole::Admin => ActorRole::Admin,
        RoomRole::Moderator => ActorRole::Moderator,
        RoomRole::Member => ActorRole::Member,
        // Unspecified/unknown is the least-privileged role, never an implicit grant.
        RoomRole::Guest | RoomRole::Unspecified => ActorRole::Guest,
    }
}

fn room_pb(r: RoomInfo) -> Room {
    Room {
        room_id: r.room_id.to_string(),
        community_id: r.community_id,
        label: r.label,
        max_publishers: r.max_publishers as u32,
        created_at: r.created_at,
        publisher_ids: r.publisher_ids.iter().map(Uuid::to_string).collect(),
        subscriber_count: r.subscriber_count as u32,
        locked: r.locked,
        // Idle-room expiry is not implemented yet (plan T2), so no room ever expires: 0 = disabled.
        empty_timeout_secs: 0,
    }
}

fn participant_pb(p: ParticipantInfo) -> Participant {
    Participant {
        participant_id: p.participant_id.to_string(),
        publisher: p.publisher,
        subscriber: p.subscriber,
        muted: p.muted,
        // The room ledger does not record join times yet (plan T2); unset, never a made-up 0.
        joined_at: None,
    }
}

#[tonic::async_trait]
impl RoomsService for RoomsGrpc {
    async fn list_rooms(
        &self,
        req: Request<ListRoomsRequest>,
    ) -> Result<Response<ListRoomsResponse>, Status> {
        self.run("ListRooms", SCOPE_READ, req, |s, r| async move {
            let rooms = s.list_rooms(&r.community_id).await?;
            Ok(ListRoomsResponse {
                rooms: rooms.into_iter().map(room_pb).collect(),
            })
        })
        .await
    }

    async fn get_room(
        &self,
        req: Request<GetRoomRequest>,
    ) -> Result<Response<GetRoomResponse>, Status> {
        self.run("GetRoom", SCOPE_READ, req, |s, r| async move {
            let id = uuid("room_id", &r.room_id)?;
            Ok(GetRoomResponse {
                room: Some(room_pb(s.get(&r.community_id, id).await?)),
            })
        })
        .await
    }

    async fn list_participants(
        &self,
        req: Request<ListParticipantsRequest>,
    ) -> Result<Response<ListParticipantsResponse>, Status> {
        self.run("ListParticipants", SCOPE_READ, req, |s, r| async move {
            let id = uuid("room_id", &r.room_id)?;
            let info = s.get(&r.community_id, id).await?;
            let participants = s.participants(&r.community_id, id).await?;
            Ok(ListParticipantsResponse {
                room_id: id.to_string(),
                locked: info.locked,
                participants: participants.into_iter().map(participant_pb).collect(),
            })
        })
        .await
    }

    async fn create_room(
        &self,
        req: Request<CreateRoomRequest>,
    ) -> Result<Response<CreateRoomResponse>, Status> {
        self.run("CreateRoom", SCOPE_WRITE, req, |s, r| async move {
            if r.empty_timeout_secs.is_some_and(|n| n != 0) {
                return Err(RoomError::Unsupported(
                    "empty_timeout_secs: empty-room expiry is not implemented yet".into(),
                ));
            }
            let info = s
                .create(
                    &r.community_id,
                    r.label,
                    r.max_publishers.map(|n| n as usize),
                    RouteMode::Sfu,
                )
                .await?;
            Ok(CreateRoomResponse {
                room: Some(room_pb(info)),
            })
        })
        .await
    }

    async fn delete_room(
        &self,
        req: Request<DeleteRoomRequest>,
    ) -> Result<Response<DeleteRoomResponse>, Status> {
        self.run("DeleteRoom", SCOPE_WRITE, req, |s, r| async move {
            let id = uuid("room_id", &r.room_id)?;
            Ok(DeleteRoomResponse {
                room: Some(room_pb(s.close(&r.community_id, id).await?)),
            })
        })
        .await
    }

    async fn join_room(
        &self,
        req: Request<JoinRoomRequest>,
    ) -> Result<Response<JoinRoomResponse>, Status> {
        self.run("JoinRoom", SCOPE_WRITE, req, |s, r| async move {
            let room_id = uuid("room_id", &r.room_id)?;
            let participant = uuid("participant_id", &r.participant_id)?;
            let t = s
                .issue_token(
                    &r.community_id,
                    room_id,
                    IssueRequest {
                        participant,
                        publish: r.publish,
                        subscribe: r.subscribe,
                        source: None,
                        ttl_secs: r.ttl_secs,
                    },
                )
                .await?;
            let base = s.public_base_url().trim_end_matches('/').to_string();
            Ok(JoinRoomResponse {
                token: t.token,
                token_id: t.token_id.to_string(),
                participant_id: participant.to_string(),
                expires_at: t.expires_at,
                whip_url: format!("{base}/rtc/rooms/{room_id}/whip"),
                whep_url: format!("{base}/rtc/rooms/{room_id}/whep"),
                api_version: API_VERSION.to_string(),
            })
        })
        .await
    }

    async fn leave_room(
        &self,
        req: Request<LeaveRoomRequest>,
    ) -> Result<Response<LeaveRoomResponse>, Status> {
        self.run("LeaveRoom", SCOPE_WRITE, req, |s, r| async move {
            let room_id = uuid("room_id", &r.room_id)?;
            let participant = uuid("participant_id", &r.participant_id)?;
            let actor = uuid("actor_id", &r.actor_id)?;
            let n = s
                .leave(&r.community_id, room_id, participant, actor, role(r.actor_role))
                .await?;
            Ok(LeaveRoomResponse {
                room_id: room_id.to_string(),
                participant_id: participant.to_string(),
                sessions_closed: n as u32,
            })
        })
        .await
    }

    async fn kick_participant(
        &self,
        req: Request<KickParticipantRequest>,
    ) -> Result<Response<KickParticipantResponse>, Status> {
        self.run("KickParticipant", SCOPE_MODERATE, req, |s, r| async move {
            let room_id = uuid("room_id", &r.room_id)?;
            let participant = uuid("participant_id", &r.participant_id)?;
            let actor = uuid("actor_id", &r.actor_id)?;
            let n = s
                .kick(&r.community_id, room_id, participant, actor, role(r.actor_role))
                .await?;
            Ok(KickParticipantResponse {
                room_id: room_id.to_string(),
                participant_id: participant.to_string(),
                sessions_closed: n as u32,
            })
        })
        .await
    }

    async fn mute_participant(
        &self,
        req: Request<MuteParticipantRequest>,
    ) -> Result<Response<MuteParticipantResponse>, Status> {
        self.run("MuteParticipant", SCOPE_MODERATE, req, |s, r| async move {
            let room_id = uuid("room_id", &r.room_id)?;
            let participant = uuid("participant_id", &r.participant_id)?;
            uuid("actor_id", &r.actor_id)?;
            s.set_muted(&r.community_id, room_id, participant, true, role(r.actor_role))
                .await?;
            Ok(MuteParticipantResponse {
                room_id: room_id.to_string(),
                participant_id: participant.to_string(),
                muted: true,
            })
        })
        .await
    }

    async fn unmute_participant(
        &self,
        req: Request<UnmuteParticipantRequest>,
    ) -> Result<Response<UnmuteParticipantResponse>, Status> {
        self.run("UnmuteParticipant", SCOPE_MODERATE, req, |s, r| async move {
            let room_id = uuid("room_id", &r.room_id)?;
            let participant = uuid("participant_id", &r.participant_id)?;
            uuid("actor_id", &r.actor_id)?;
            s.set_muted(&r.community_id, room_id, participant, false, role(r.actor_role))
                .await?;
            Ok(UnmuteParticipantResponse {
                room_id: room_id.to_string(),
                participant_id: participant.to_string(),
                muted: false,
            })
        })
        .await
    }

    async fn mute_all(
        &self,
        req: Request<MuteAllRequest>,
    ) -> Result<Response<MuteAllResponse>, Status> {
        self.run("MuteAll", SCOPE_MODERATE, req, |s, r| async move {
            let room_id = uuid("room_id", &r.room_id)?;
            let actor = uuid("actor_id", &r.actor_id)?;
            let n = s
                .mute_all(&r.community_id, room_id, actor, role(r.actor_role))
                .await?;
            Ok(MuteAllResponse {
                room_id: room_id.to_string(),
                muted: n as u32,
            })
        })
        .await
    }

    async fn lock_room(
        &self,
        req: Request<LockRoomRequest>,
    ) -> Result<Response<LockRoomResponse>, Status> {
        self.run("LockRoom", SCOPE_MODERATE, req, |s, r| async move {
            let room_id = uuid("room_id", &r.room_id)?;
            uuid("actor_id", &r.actor_id)?;
            s.set_locked(&r.community_id, room_id, true, role(r.actor_role))
                .await?;
            Ok(LockRoomResponse {
                room_id: room_id.to_string(),
                locked: true,
            })
        })
        .await
    }

    async fn unlock_room(
        &self,
        req: Request<UnlockRoomRequest>,
    ) -> Result<Response<UnlockRoomResponse>, Status> {
        self.run("UnlockRoom", SCOPE_MODERATE, req, |s, r| async move {
            let room_id = uuid("room_id", &r.room_id)?;
            uuid("actor_id", &r.actor_id)?;
            s.set_locked(&r.community_id, room_id, false, role(r.actor_role))
                .await?;
            Ok(UnlockRoomResponse {
                room_id: room_id.to_string(),
                locked: false,
            })
        })
        .await
    }

    async fn list_hands(
        &self,
        req: Request<ListHandsRequest>,
    ) -> Result<Response<ListHandsResponse>, Status> {
        self.run("ListHands", SCOPE_READ, req, |_s, _r| async move {
            Err::<ListHandsResponse, _>(hands_unimplemented("ListHands"))
        })
        .await
    }

    async fn raise_hand(
        &self,
        req: Request<RaiseHandRequest>,
    ) -> Result<Response<RaiseHandResponse>, Status> {
        self.run("RaiseHand", SCOPE_WRITE, req, |_s, _r| async move {
            Err::<RaiseHandResponse, _>(hands_unimplemented("RaiseHand"))
        })
        .await
    }

    async fn lower_hand(
        &self,
        req: Request<LowerHandRequest>,
    ) -> Result<Response<LowerHandResponse>, Status> {
        self.run("LowerHand", SCOPE_WRITE, req, |_s, _r| async move {
            Err::<LowerHandResponse, _>(hands_unimplemented("LowerHand"))
        })
        .await
    }

    async fn acknowledge_hand(
        &self,
        req: Request<AcknowledgeHandRequest>,
    ) -> Result<Response<AcknowledgeHandResponse>, Status> {
        self.run("AcknowledgeHand", SCOPE_MODERATE, req, |_s, _r| async move {
            Err::<AcknowledgeHandResponse, _>(hands_unimplemented("AcknowledgeHand"))
        })
        .await
    }
}

/// Loud refusal for the raised-hand RPCs: the contract exists (proto T1) but the server-side
/// queue is not built yet (plan T2); callers must never see a silent empty list or no-op.
fn hands_unimplemented(rpc: &str) -> RoomError {
    RoomError::Unsupported(format!(
        "{rpc}: raised-hand queue is not implemented in svc-streaming yet"
    ))
}

/// The axum router carrying the tonic service (a single `RoomsService` route set).
pub fn router(rooms: Arc<RoomService>, auth: Arc<ServiceJwtVerifier>) -> axum::Router {
    tonic::service::Routes::new(RoomsServiceServer::new(RoomsGrpc::new(rooms, auth)))
        .into_axum_router()
}

/// Server TLS config for the gRPC listener: the host-API mTLS config (client cert
/// required) with ALPN pinned to `h2`.
pub fn build_grpc_server_config(tls: &HostApiTls) -> Result<rustls::ServerConfig, HostApiError> {
    let mut cfg = build_server_config(tls)?;
    cfg.alpn_protocols = vec![b"h2".to_vec()];
    Ok(cfg)
}

/// Serves `RoomsService` on `listener` over mTLS until dropped.
pub async fn serve(
    listener: tokio::net::TcpListener,
    server_config: rustls::ServerConfig,
    rooms: Arc<RoomService>,
    auth: Arc<ServiceJwtVerifier>,
) -> Result<(), HostApiError> {
    serve_router(listener, server_config, router(rooms, auth), "rooms grpc").await
}

/// mTLS file locations for the gRPC listener: `ROOMS_GRPC_SERVER_CERT_FILE`,
/// `ROOMS_GRPC_SERVER_KEY_FILE`, `ROOMS_GRPC_CLIENT_CA_FILE` (all required; a partial
/// set is an error). Separate from the host-API's `HOST_API_*` so enabling the gRPC
/// control plane never implicitly opens the host-API listener.
fn tls_from_env(port: u16) -> Result<HostApiTls, HostApiError> {
    let get = |k: &str| std::env::var(k).ok().filter(|v| !v.is_empty());
    match (
        get("ROOMS_GRPC_SERVER_CERT_FILE"),
        get("ROOMS_GRPC_SERVER_KEY_FILE"),
        get("ROOMS_GRPC_CLIENT_CA_FILE"),
    ) {
        (Some(cert), Some(key), Some(ca)) => Ok(HostApiTls {
            server_cert: cert.into(),
            server_key: key.into(),
            client_ca: ca.into(),
            port,
        }),
        _ => Err(HostApiError::Config(
            "ROOMS_GRPC_PORT is set but ROOMS_GRPC_SERVER_CERT_FILE, ROOMS_GRPC_SERVER_KEY_FILE \
             and ROOMS_GRPC_CLIENT_CA_FILE are not all set (mTLS client verification is not optional)"
                .into(),
        )),
    }
}

/// Binds and serves the gRPC listener from env config. Off (logged) when
/// `ROOMS_GRPC_PORT` is unset; when it IS set, missing mTLS files or missing
/// service-JWT keys are a startup error (fail loud, never an open listener).
pub async fn spawn_from_env(
    bind: std::net::IpAddr,
    rooms: Arc<RoomService>,
) -> Result<(), HostApiError> {
    let Some(port) = std::env::var("ROOMS_GRPC_PORT").ok().filter(|v| !v.is_empty()) else {
        tracing::warn!("rooms gRPC disabled: ROOMS_GRPC_PORT is not set");
        return Ok(());
    };
    let port: u16 = port
        .parse()
        .map_err(|_| HostApiError::Config("ROOMS_GRPC_PORT is not a port".into()))?;
    let tls = tls_from_env(port)?;
    let auth = ServiceJwtVerifier::from_env()
        .map_err(|e| HostApiError::Config(e.to_string()))?
        .ok_or_else(|| {
            HostApiError::Config(
                "ROOMS_GRPC_PORT is set but no SERVICE_JWT_PUBLIC_KEY[_<kid>] is configured".into(),
            )
        })?;
    let cfg = build_grpc_server_config(&tls)?;
    let listener = tokio::net::TcpListener::bind(SocketAddr::new(bind, port)).await?;
    tracing::info!(port, "rooms gRPC (mTLS + service JWT) listening");
    let auth = Arc::new(auth);
    tokio::spawn(async move {
        if let Err(e) = serve(listener, cfg, rooms, auth).await {
            tracing::error!(error = %e, "rooms gRPC listener stopped");
        }
    });
    Ok(())
}
