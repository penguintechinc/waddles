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
//! `rooms.grpc` span carrying `rpc`/`op`, the numeric `community_id`/`tenant_id` and the
//! `outcome`, never a name or free text) and the error mapping to gRPC status codes.
//!
//! The listener also serves the standard `grpc.health.v1` service (`tonic-health`) for
//! in-mesh checks: SERVING only when [`RoomService::readiness`] is `Ok`, and it demands the
//! same `streaming.rooms:read` service JWT as the data RPCs (no inter-service call is
//! unauthenticated). kubelet's native gRPC probe cannot present a client certificate, so it
//! cannot reach this listener; the chart keeps its HTTP `/health` + `/readyz` probes on the
//! HTTP port (an owner-approved exception to the native-gRPC-probe convention).
//!
//! Tenancy: `community_id` comes from the request (hub-api derives it from the end user's
//! verified JWT and is the only mTLS peer allowed to call). When the service JWT is itself
//! community-bound (`community` claim, see `flask_core.service_jwt`), a request naming any
//! other community is refused as `NOT_FOUND` before any room is touched.

use std::future::Future;
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Instant;

use tonic::metadata::MetadataMap;
use tonic::service::interceptor::InterceptedService;
use tonic::{Request, Response, Status};
use tonic_health::ServingStatus;
use tracing::Instrument;
use uuid::Uuid;

use super::grpc_auth::{
    ServiceClaims, ServiceJwtVerifier, SCOPE_MODERATE, SCOPE_READ, SCOPE_WRITE,
};
use super::host_api::{build_server_config, serve_router, HostApiError, HostApiTls};
use super::{
    ActorRole, HandInfo, HandList, HandView, IssueRequest, ParticipantInfo, RoomError, RoomInfo,
    RoomService, RouteMode,
};

/// Generated protobuf/tonic types for `waddles.streaming.internal.v1`.
pub mod pb {
    tonic::include_proto!("waddles.streaming.internal.v1");
}

use pb::rooms_service_server::{RoomsService, RoomsServiceServer};
use pb::*;

/// The only `api_version` this server speaks. Anything else (including empty) is refused with
/// `UNIMPLEMENTED` before any request field is acted on (rule `backend.md` API Versioning).
pub const API_VERSION: &str = "v1";

/// Name the `grpc.health.v1` service reports `RoomsService` readiness under (the overall
/// server, name `""`, is reported alongside it).
pub const HEALTH_SERVICE_NAME: &str = "waddles.streaming.internal.v1.RoomsService";

/// Implemented by every `RoomsService` request message so [`RoomsGrpc::run`] can enforce
/// [`API_VERSION`] and the community binding uniformly instead of each handler
/// remembering to.
trait Versioned {
    /// The request's `api_version` field.
    fn api_version(&self) -> &str;
    /// The request's `community_id` field.
    fn community_id(&self) -> &str;
}

macro_rules! versioned {
    ($($t:ty),+ $(,)?) => {
        $(impl Versioned for $t {
            fn api_version(&self) -> &str {
                &self.api_version
            }
            fn community_id(&self) -> &str {
                &self.community_id
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
    ResetHandsRequest,
);

/// Refuses any `api_version` other than [`API_VERSION`] with `UNIMPLEMENTED`.
fn check_api_version(v: &str) -> Result<(), Status> {
    if v == API_VERSION {
        Ok(())
    } else {
        Err(Status::unimplemented(format!(
            "api_version {v} not supported"
        )))
    }
}

/// Every RPC name in `RoomsService`, kept in lockstep with the proto by the
/// contract test (`tests/rooms_grpc.rs`) and used as the metric `rpc` label.
pub const RPC_NAMES: [&str; 18] = [
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
    "ResetHands",
];

/// Verifies the `authorization: Bearer ...` machine JWT in `md` for `scope`. Failures never
/// say which check failed: a bad scope/subject is `PERMISSION_DENIED`, anything else
/// `UNAUTHENTICATED`.
fn bearer_claims(
    auth: &ServiceJwtVerifier,
    md: &MetadataMap,
    scopes: &[&str],
) -> Result<ServiceClaims, Status> {
    let token = md
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .ok_or_else(|| Status::unauthenticated("missing bearer token"))?;
    auth.verify_any(token, scopes).map_err(|r| {
        if r.reason == crate::http::auth::hardening::REASON_SCOPE_DENIED {
            Status::permission_denied("scope not permitted")
        } else {
            Status::unauthenticated("invalid token")
        }
    })
}

/// True for a plain decimal id (digits only, bounded): the only shape allowed into a span
/// attribute, so request text can never be recorded as telemetry.
fn is_numeric_id(v: &str) -> bool {
    !v.is_empty() && v.len() <= 20 && v.bytes().all(|b| b.is_ascii_digit())
}

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

    /// Verifies the caller's machine JWT for any of `scopes`; failures never say which check
    /// failed. The returned claims say which scope was presented.
    fn authenticate<T>(&self, req: &Request<T>, scopes: &[&str]) -> Result<ServiceClaims, Status> {
        bearer_claims(&self.auth, req.metadata(), scopes)
    }

    /// Runs one RPC that needs exactly `scope`.
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
        self.run_scoped(rpc, &[scope], req, |s, r, _claims| f(s, r))
            .await
    }

    /// Runs one RPC: authenticate against any of `scopes`, execute (the closure sees the
    /// verified claims, so it can branch on WHICH scope was presented), then record the
    /// per-RPC metric/latency/span.
    async fn run_scoped<R, T, Fut>(
        &self,
        rpc: &'static str,
        scopes: &[&'static str],
        req: Request<R>,
        f: impl FnOnce(Arc<RoomService>, R, ServiceClaims) -> Fut,
    ) -> Result<Response<T>, Status>
    where
        R: Versioned,
        Fut: Future<Output = Result<T, RoomError>>,
    {
        let started = Instant::now();
        let span = tracing::info_span!(
            "rooms.grpc",
            rpc,
            op = rpc,
            community_id = tracing::field::Empty,
            tenant_id = tracing::field::Empty,
            outcome = tracing::field::Empty,
        );
        if is_numeric_id(req.get_ref().community_id()) {
            span.record("community_id", req.get_ref().community_id());
        }
        let outcome_span = span.clone();
        let result = async {
            let claims = self.authenticate(&req, scopes)?;
            if let Some(t) = claims.tenant.as_deref().filter(|t| is_numeric_id(t)) {
                tracing::Span::current().record("tenant_id", t);
            }
            check_api_version(req.get_ref().api_version())?;
            if let Some(bound) = claims.community {
                if req.get_ref().community_id() != bound.to_string() {
                    // A community-bound credential never reaches another community's rooms;
                    // indistinguishable from "not found".
                    return Err(status_of(RoomError::NotFound));
                }
            }
            f(self.rooms.clone(), req.into_inner(), claims)
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
        outcome_span.record("outcome", code.as_str());
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
        empty_timeout_secs: r.empty_timeout_secs,
        participant_count: r.participant_count as u32,
        hand_count: r.hand_count as u32,
    }
}

fn hand_pb(h: HandInfo) -> RaisedHand {
    RaisedHand {
        participant_id: h.participant_id.to_string(),
        raised_at: h.raised_at,
        acknowledged_at: h.acknowledged_at,
        acknowledged_by: h.acknowledged_by.map(|u| u.to_string()),
    }
}

fn participant_pb(p: ParticipantInfo) -> Participant {
    Participant {
        participant_id: p.participant_id.to_string(),
        publisher: p.publisher,
        subscriber: p.subscriber,
        muted: p.muted,
        joined_at: p.joined_at,
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
            let info = s
                .create_with(
                    &r.community_id,
                    r.label,
                    r.max_publishers.map(|n| n as usize),
                    RouteMode::Sfu,
                    r.empty_timeout_secs,
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
        self.run_scoped(
            "JoinRoom",
            &[SCOPE_WRITE, SCOPE_MODERATE],
            req,
            |s, r, c| async move {
                let room_id = uuid("room_id", &r.room_id)?;
                let participant = uuid("participant_id", &r.participant_id)?;
                if r.ttl_secs
                    .is_some_and(|t| t > super::token::JOIN_TTL_MAX_SECS)
                {
                    return Err(RoomError::Invalid(format!(
                        "ttl_secs of a join token must be {}..={} (unset = {})",
                        super::token::MIN_TTL_SECS,
                        super::token::JOIN_TTL_MAX_SECS,
                        super::token::DEFAULT_TTL_SECS
                    )));
                }
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
                            // Staff only ever comes from VERIFIED authority: the moderate-scoped
                            // service JWT. No request field can claim it.
                            staff: c.scope == SCOPE_MODERATE,
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
            },
        )
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
                .leave(
                    &r.community_id,
                    room_id,
                    participant,
                    actor,
                    role(r.actor_role),
                )
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
                .kick(
                    &r.community_id,
                    room_id,
                    participant,
                    actor,
                    role(r.actor_role),
                )
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
            s.set_muted(
                &r.community_id,
                room_id,
                participant,
                true,
                role(r.actor_role),
            )
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
        self.run(
            "UnmuteParticipant",
            SCOPE_MODERATE,
            req,
            |s, r| async move {
                let room_id = uuid("room_id", &r.room_id)?;
                let participant = uuid("participant_id", &r.participant_id)?;
                uuid("actor_id", &r.actor_id)?;
                s.set_muted(
                    &r.community_id,
                    room_id,
                    participant,
                    false,
                    role(r.actor_role),
                )
                .await?;
                Ok(UnmuteParticipantResponse {
                    room_id: room_id.to_string(),
                    participant_id: participant.to_string(),
                    muted: false,
                })
            },
        )
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
        self.run_scoped(
            "ListHands",
            &[SCOPE_READ, SCOPE_MODERATE],
            req,
            |s, r, c| async move {
                let id = uuid("room_id", &r.room_id)?;
                let actor = uuid("actor_id", &r.actor_id)?;
                // The whole queue is reachable only with the moderate scope (and a staff actor);
                // the read scope sees the actor's own entry, queue length and position.
                let view = if c.scope == SCOPE_MODERATE {
                    HandView::All
                } else {
                    HandView::Own
                };
                let HandList {
                    hands,
                    count,
                    position,
                } = s.list_hands(&r.community_id, id, actor, view).await?;
                Ok(ListHandsResponse {
                    room_id: id.to_string(),
                    hands: hands.into_iter().map(hand_pb).collect(),
                    count: count as u32,
                    position: position.map(|p| p as u32),
                })
            },
        )
        .await
    }

    async fn raise_hand(
        &self,
        req: Request<RaiseHandRequest>,
    ) -> Result<Response<RaiseHandResponse>, Status> {
        self.run("RaiseHand", SCOPE_WRITE, req, |s, r| async move {
            let room_id = uuid("room_id", &r.room_id)?;
            let participant = uuid("participant_id", &r.participant_id)?;
            let actor = uuid("actor_id", &r.actor_id)?;
            let hand = s
                .raise_hand(&r.community_id, room_id, participant, actor)
                .await?;
            Ok(RaiseHandResponse {
                hand: Some(hand_pb(hand)),
            })
        })
        .await
    }

    async fn lower_hand(
        &self,
        req: Request<LowerHandRequest>,
    ) -> Result<Response<LowerHandResponse>, Status> {
        self.run_scoped(
            "LowerHand",
            &[SCOPE_WRITE, SCOPE_MODERATE],
            req,
            |s, r, c| async move {
                let room_id = uuid("room_id", &r.room_id)?;
                let participant = uuid("participant_id", &r.participant_id)?;
                let actor = uuid("actor_id", &r.actor_id)?;
                // Lowering someone else's hand needs the VERIFIED moderate scope on top of the
                // staff ACL the service checks; `actor_role` is never read.
                if actor != participant && c.scope != SCOPE_MODERATE {
                    return Err(RoomError::Forbidden(
                        "lowering another participant's hand needs the moderate scope",
                    ));
                }
                let lowered = s
                    .lower_hand(&r.community_id, room_id, participant, actor)
                    .await?;
                Ok(LowerHandResponse {
                    room_id: room_id.to_string(),
                    participant_id: participant.to_string(),
                    lowered,
                })
            },
        )
        .await
    }

    async fn acknowledge_hand(
        &self,
        req: Request<AcknowledgeHandRequest>,
    ) -> Result<Response<AcknowledgeHandResponse>, Status> {
        self.run("AcknowledgeHand", SCOPE_MODERATE, req, |s, r| async move {
            let room_id = uuid("room_id", &r.room_id)?;
            let participant = uuid("participant_id", &r.participant_id)?;
            let actor = uuid("actor_id", &r.actor_id)?;
            let hand = s
                .acknowledge_hand(&r.community_id, room_id, participant, actor)
                .await?;
            Ok(AcknowledgeHandResponse {
                hand: Some(hand_pb(hand)),
            })
        })
        .await
    }

    async fn reset_hands(
        &self,
        req: Request<ResetHandsRequest>,
    ) -> Result<Response<ResetHandsResponse>, Status> {
        self.run("ResetHands", SCOPE_MODERATE, req, |s, r| async move {
            let room_id = uuid("room_id", &r.room_id)?;
            let actor = uuid("actor_id", &r.actor_id)?;
            let cleared = s.reset_hands(&r.community_id, room_id, actor).await?;
            Ok(ResetHandsResponse {
                room_id: room_id.to_string(),
                cleared: cleared as u32,
            })
        })
        .await
    }
}

/// The axum router carrying the tonic services: `RoomsService` plus `grpc.health.v1`.
///
/// Health reports SERVING (for the overall server `""` and for [`HEALTH_SERVICE_NAME`]) only
/// when [`RoomService::readiness`] is `Ok`, else NOT_SERVING with the reason logged; checks
/// need the same `streaming.rooms:read` service JWT as the data RPCs.
pub async fn router(rooms: Arc<RoomService>, auth: Arc<ServiceJwtVerifier>) -> axum::Router {
    let (reporter, health) = tonic_health::server::health_reporter();
    let status = match rooms.readiness() {
        Ok(()) => ServingStatus::Serving,
        Err(reason) => {
            tracing::warn!(reason, "rooms gRPC health: NOT_SERVING");
            ServingStatus::NotServing
        }
    };
    reporter.set_service_status("", status).await;
    reporter
        .set_service_status(HEALTH_SERVICE_NAME, status)
        .await;
    let health_auth = Arc::clone(&auth);
    let health = InterceptedService::new(health, move |req: Request<()>| {
        bearer_claims(&health_auth, req.metadata(), &[SCOPE_READ]).map(|_| req)
    });
    tonic::service::Routes::new(RoomsServiceServer::new(RoomsGrpc::new(rooms, auth)))
        .add_service(health)
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
    serve_router(
        listener,
        server_config,
        router(rooms, auth).await,
        "rooms grpc",
    )
    .await
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
    let Some(port) = std::env::var("ROOMS_GRPC_PORT")
        .ok()
        .filter(|v| !v.is_empty())
    else {
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
