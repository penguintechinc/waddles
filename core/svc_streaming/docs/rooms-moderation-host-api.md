# Rooms moderation host-API ops

Ported from Go `core/module_rtc` (`handlers.go` routes, `call_features.go`). Media/state
authoritative side lives in `svc_streaming`; the `rooms` app bundle owns policy (ACL roles).

Transport: `POST /host/v1/webrtc/{op}` over the existing mTLS host-API listener
(`HOST_API_*`, port `WEBRTC_HOST_API_PORT`, default 8209), JSON body, `deny_unknown_fields`.
Errors: `{"error": <code>, "message": ..}`; codes `not_found` 404, `invalid_args` 400,
`forbidden` 403, `unavailable` 503, `not_implemented` 501 (also `feature_disabled` ops, see below).

## Feature flag

Key `rooms.moderation`, env baseline `FEATURE_ROOMS_MODERATION` (unset/false = OFF, default).
When OFF every op below except `room.list` / `room.participants` is refused with
`501 {"error":"not_implemented","message":"unsupported: feature rooms.moderation is disabled"}`
(fail loud, never a silent no-op). Token issuance/join lock enforcement is also only active with the flag ON.

## Identity / PII

No usernames anywhere. Participants are identified by `participant_id` (UUID) only; token ids are UUIDs.
All requests carry `community_id` (trusted: derived from the authenticated stage's invocation context)
and, for moderation, `actor_id` (UUID of the acting participant) and `actor_role`.

## AuthZ (interim)

`feature/sdk-caller-can` is not on origin. Interim model = the rooms app role model
(`bundles/rust/rooms/src/logic.rs` `Role`: guest < member < moderator < admin). The rooms app
resolves the actor's role from its per-room ACL and passes it as `actor_role`
(`"guest"|"member"|"moderator"|"admin"`). svc_streaming enforces it as defense in depth.
This migrates to the SDK `caller_can` check later; `actor_role` is then dropped.

| Op | Min role |
|----|----------|
| room.list, room.participants | none (member-level read; no `actor_*` fields) |
| room.leave | self (`actor_id == participant_id`): none; leaving someone else: moderator |
| room.mute, room.unmute, room.mute_all, room.kick, room.lock, room.unlock | moderator (`moderator` or `admin`) |

## Ops

All `room_id` are UUIDs.

### `room.list`
Req `{"community_id"}` -> `{"rooms":[{room_id, community_id, label, route, max_publishers, created_at, publisher_ids:[uuid], subscriber_count, locked}]}` sorted by `created_at`,`room_id`.

### `room.participants`
Req `{"community_id","room_id"}` -> `{"room_id","locked","participants":[{"participant_id","publisher":bool,"subscriber":bool,"muted":bool}],"count"}` (a participant that both publishes and subscribes appears once).

### `room.leave`
Req `{"community_id","room_id","participant_id","actor_id","actor_role"}` -> `{"room_id","participant_id","sessions_closed":n}`. Closes that participant's live WHIP+WHEP peer connections. Does not blocklist (they may rejoin).

### `room.kick`
Req `{"community_id","room_id","participant_id","actor_id","actor_role"}` -> `{"room_id","participant_id","sessions_closed":n}`. Closes live peers AND blocklists the participant id for the room (their tokens now fail `authorize`; no rejoin until `room.revoke_all`/room recreated). Cannot kick self.

### `room.mute` / `room.unmute`
Req `{"community_id","room_id","participant_id","actor_id","actor_role"}` -> `{"room_id","participant_id","muted":bool}`. Enforced in the SFU fanout: a muted publisher's RTP is dropped before reaching any subscriber (persists across that participant's reconnects until unmuted). Muting a non-publishing participant is recorded and applies when they publish.

### `room.mute_all`
Req `{"community_id","room_id","actor_id","actor_role"}` -> `{"room_id","muted":n}` mutes every current publisher except `actor_id`.

### `room.lock` / `room.unlock`
Req `{"community_id","room_id","actor_id","actor_role"}` -> `{"room_id","locked":bool}`. While locked: `token.issue`, `whip.endpoint`, `whep.endpoint` and new WHIP/WHEP joins are refused `403 forbidden` ("room is locked"). Existing sessions are untouched.

### Changed existing behavior
`token.revoke` now also closes the live WHIP/WHEP sessions opened with that token id.
Raise-hand (Go `RaiseHand/LowerHand/...`) is out of scope for svc_streaming (app-state, rooms bundle kv).

## gRPC control plane (hub-api -> svc-streaming)

hub-api's `/api/v1/admin/<c>/calls/rooms*` and `/api/v1/community/<c>/interact/voice/rooms*` routes reach the same
`RoomService` through `waddles.streaming.internal.v1.RoomsService`
(`proto/waddles/streaming/internal/v1/rooms.proto`, server `src/rooms/grpc.rs`), never REST.

- Transport: HTTP/2 over the mTLS accept loop shared with the host-API, on `ROOMS_GRPC_PORT` (chart: `pipeline.svcStreaming.grpcPort`).
  TLS files: `ROOMS_GRPC_SERVER_CERT_FILE`, `ROOMS_GRPC_SERVER_KEY_FILE`, `ROOMS_GRPC_CLIENT_CA_FILE` (client cert required).
  Listener off + logged when `ROOMS_GRPC_PORT` is unset; set with missing TLS files or JWT keys = startup error.
- AuthN: ES256 machine JWT (`authorization: Bearer`) on every RPC; trust keys `SERVICE_JWT_ACTIVE_KID`+`SERVICE_JWT_PUBLIC_KEY`
  (+`SERVICE_JWT_PUBLIC_KEY_<kid>`), `SERVICE_JWT_AUDIENCE`, optional `ROOMS_GRPC_ALLOWED_SUBJECTS` (SPIFFE ids).
  Scopes: `streaming.rooms:read` (ListRooms/GetRoom/ListParticipants), `:write` (Create/Delete/Join/Leave), `:moderate` (Kick/Mute/Unmute/MuteAll/Lock/Unlock).
- AuthZ/flag/telemetry: identical to the host-API ops (actor role -> moderator/admin; `rooms.moderation`; `rooms_moderation_ops_total`, spans),
  plus `rooms_grpc_requests_total{rpc,code}` and `rooms_grpc_request_seconds{rpc}`.
- Errors: NotFound->`NOT_FOUND`, Invalid->`INVALID_ARGUMENT`, LimitReached->`RESOURCE_EXHAUSTED`, Forbidden->`PERMISSION_DENIED`,
  Unsupported/flag off->`UNIMPLEMENTED`, Unconfigured->`UNAVAILABLE`.
- No gRPC equivalent (hub-api answers 501): raised-hand queue and acknowledge (rooms bundle kv state).
