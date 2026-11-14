# waddles.core.service.webrtc

Always-on `kind: service` bundle (Rust, `world stage-tools-webrtc`) that is the single
tool/policy surface over `svc-streaming`'s WebRTC rooms. The media path (ICE/DTLS/SRTP, SFU
fan-out, TURN/ICE config, the token-signing secret) stays host-side in Rust; the guest only
calls the `webrtc` host capability (`wit/waddle-bundle/webrtc.wit`).

| Tool | Access | What |
|---|---|---|
| `room.create {label?, max_publishers?<=64, route?:"sfu"}` | write, `required_scope: community:admin` | new room in the caller's community |
| `room.get {room_id}` | read | room view (ids/counts only) |
| `room.close {room_id}` | write, dangerous, `community:admin` | closes room, drops every peer |
| `token.issue {room_id, publish, subscribe, participant_id?, source?, ttl_secs?}` | write, dangerous | short-lived (30..3600 s) room-scoped signed join token |
| `token.revoke {room_id, token_id}` | write, dangerous | revokes a token id |
| `whip.endpoint {room_id, participant_id?, ttl_secs?}` | write, dangerous | publish URL + publish-only token |
| `whep.endpoint {room_id, participant_id?, source?, ttl_secs?}` | write, dangerous | playback URL + subscribe-only token (optionally pinned to one publisher) |
| `track.route {room_id, mode}` | write | `sfu` (local fan-out); `relay` fails loud (unsupported) |
| `recording.start/stop {room_id}` | write | fails loud: no room recording path yet |
| `room.list`, `room.participants {room_id}` | read | live rooms / participants (UUIDs + media flags only) |
| `room.leave|kick|mute|unmute {room_id, participant_id, actor_id, actor_role}`, `room.mute_all|lock|unlock {room_id, actor_id, actor_role}` | write | moderation; kick/mute/unmute/mute_all/lock/unlock are moderator-only and carry `required_scope: community:moderate`, `room.leave` is self-service. `mute_all` never mutes `actor_id` |
| `room.hand.raise {room_id, actor_id, actor_role, api_version?}` | write, `community:member` | the actor raises their OWN hand (idempotent) |
| `room.hand.lower {room_id, participant_id, actor_id, actor_role}` | write, `community:member` | lower your own hand; lowering someone else's needs a moderator role |
| `room.hand.list {room_id, actor_id, actor_role}` | read, `community:member` | moderators/admins get the ordered queue; members get only their own entry (+ `position`) and the queue `count` |
| `room.hand.ack {room_id, participant_id, actor_id, actor_role}` | write, `community:moderate` | acknowledge one raised hand |
| `room.hand.reset {room_id, actor_id, actor_role}` | write, `community:moderate` | clear the whole queue; returns `cleared` |

## Raised hands

The queue is AUTHORITATIVE in svc-streaming's `RoomService` (gRPC `RaiseHand`/`LowerHand`/
`ListHands`/`AcknowledgeHand`/`ResetHands`); neither this bundle nor the rooms app keeps a copy.
The server clears a participant's hand when they leave, are kicked or the room is deleted. Each
hand tool validates its args (lowercase UUIDs, `api_version` absent or `"v1"`, unknown fields
rejected), enforces the contract's minimum role BEFORE the host call (the host re-checks, and the
manifest `required_scope` is verified by the host before dispatch), then calls the host op of the
SAME name (`POST /host/v1/webrtc/room.hand.*`, always stating `api_version: "v1"`). Host op shapes
the server must serve (UUID participant ids; `participant_id` of `raise` is the actor):

| Host op | Request | Response |
|---|---|---|
| `room.hand.raise` | `{api_version, room_id, participant_id}` | `{room_id, hand:{participant_id, raised_at, acknowledged_at?, acknowledged_by?}}` |
| `room.hand.lower` | `{api_version, room_id, participant_id, actor_id, actor_role}` | `{room_id, participant_id, lowered}` (`false` = no hand, not an error) |
| `room.hand.list` | `{api_version, room_id}` | `{room_id, hands:[hand...]}` oldest first |
| `room.hand.ack` | `{api_version, room_id, participant_id, actor_id, actor_role}` | `{room_id, hand}`; `not_found` when no such hand |
| `room.hand.reset` | `{api_version, room_id, actor_id, actor_role}` | `{room_id, cleared}` |

## Access and token policy (one source of truth)

`contract/grants.json` is the machine-readable policy; `src/grants.rs` is its single Rust
implementation (`grants_for(kind, role, ctx)`), compiled into BOTH this bundle and the rooms app
(`#[path]` include, so changing it re-digests both: bump both versions). Closed, out-of-window
and locked rooms refuse non-staff; a green room is staff-only; guests (opt-in per room) only
subscribe; a stage lets only staff publish. Join tokens are short (`join_ttl_secs` 60, never above
`join_ttl_max_secs` 900), ES256, and identify the participant by UUID (`sub`) only -- never a
display name. The unit tests of both crates and `scripts/ci/tests/test_rooms_webrtc_contract.py`
replay the file; the CI test also checks svc-streaming's token and hub-api's REST join against it
(known disagreements are strict xfails owned by the hub-api / svc-streaming branches).

Safety: args validated first (lowercase UUIDs, bounded ints, unknown fields rejected); the
community is never an argument (the stage supplies it); results are rebuilt from a field
whitelist; join tokens are bearer credentials and are never logged; logs carry tool label +
outcome only.

Host side: `core/svc_streaming/src/rooms/` -- `host_api.rs` (mTLS `POST /host/v1/webrtc/{op}`),
`media.rs` (token-authenticated `POST /rtc/rooms/{room}/whip|whep`), `token.rs` (HS256, secret
`WEBRTC_ROOM_TOKEN_SECRET`, >=32 bytes). The stage-side forwarder and gate permission
(`webrtc.control`) are host seams listed in the PR.

Build: `cargo component build --release --target wasm32-wasip2 --locked`; tests: `cargo test`
(bindings are generated by the build).
