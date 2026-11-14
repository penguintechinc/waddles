# hub-api calls (RTC) REST surface

hub-api is the only REST server for call rooms. It authorises the caller, then talks to
svc-streaming's gRPC `RoomsService` (mTLS + a per-call ES256 machine JWT, `api_version = "v1"`).
Spec: `openapi/v1.yaml` (`/calls/`, `/interact/voice/`, `/town-hall`). Code:
`hub_api/blueprints/v1/calls.py`, `hub_api/services/calls_proxy.py`.

## Rules every route follows

- Flag `waddles.streaming.rtc` gates every route (checked after authz); OFF is `402`
  `FEATURE_NOT_ENABLED`. Default OFF.
- Rooms are addressed by UUID only. `GET rooms` returns `label` for display.
- Participant ids are PER-COMMUNITY pseudonyms, `uuid5(tenant:community:user)`: one person has a
  different id in every community, and the global `hub_users.uuid` is never sent to svc-streaming
  or returned by REST. Display names are resolved inside hub-api for ACTIVE members of the asked
  community only (former, deactivated and unknown ids render `"guest"`) and never reach
  svc-streaming or a log. Non-staff members never receive `publisher_ids` or `acknowledged_by`.
  (The `rooms` app bundle's actor is `community_members.user_uuid`, which for a hub-linked member
  is the global uuid, so chat and REST moderation do not yet share an id: the bundle needs the
  same per-community derivation.)
- `community_id` always comes from the URL path after the membership check, never a body.
- Errors use `{success:false, error:{code,message[,details]}}`. Upstream failures are explicit
  (404 / 403 / 429 / 503 `details.retryable` / 504); nothing is ever an empty success.

## Joining from a browser (WHIP / WHEP)

`POST /api/v1/community/{community_id}/interact/voice/rooms/{room_id}/join` (member scope
`streaming.calls:write`) returns:

| Field | Meaning |
|---|---|
| `token`, `token_type: "Bearer"`, `expires_at` | Short-lived room token (60 s by default, never above 900 s per the shared contract). Send as `Authorization: Bearer <token>` to both endpoints. The response is `Cache-Control: no-store`; the token is never logged. |
| `whip_url` | POST the SDP offer here to publish (only when `publish` is true). |
| `whep_url` | POST the SDP offer here to subscribe (only when `subscribe` is true). |
| `publish`, `subscribe` | The grants the access policy gave THIS caller. |
| `ice_servers` | `RTCPeerConnection` `iceServers` list from `STREAMING_RTC_ICE_SERVERS` (JSON `[{"urls": ["stun:host:3478"]}]`). Empty by default. TURN is not supported yet (v3.3.x, #1318): `turn:` URLs and credentials in that variable are refused with a 503. |

Grants come from the shared policy (`bundles/rust/webrtc/contract/grants.json`, replayed by
`hub_api/tests/test_calls_access.py` and `scripts/ci/tests/test_rooms_webrtc_contract.py`):
a locked room refuses non-staff (`403 ROOM_ACCESS_DENIED`, `room is locked`); staff are never
refused by hub-api's policy.

Room facts fail closed. svc-streaming's `Room` exposes only `locked`, so hub-api records every
room it creates in `call_rooms` (alembic 0110: kind int code, closed, allow_guests, town_hall) and
`call_access.facts_from_room` consumes only that record. A room hub-api did not record is
unknown: non-staff are refused (`403`, `room access cannot be verified`, no token minted), staff
join under the plain community policy. Known stage / green-room / closed rooms get their policy
grants. svc-streaming still refuses every caller (staff included) on a locked room.

## Town Hall

`GET` / `POST /api/v1/communities/{community_id}/town-hall` (member scope; read / write).
`GET` is READ-ONLY: it returns the Town Hall if one was created, else `404 TOWN_HALL_NOT_FOUND`.
`POST` creates it lazily (label `Town Hall`, never expires) and is idempotent:

```json
{"room_id": "<uuid>", "label": "Town Hall", "locked": false, "participant_count": 3,
 "created": false, "join": {"method": "POST", "path": "/api/v1/community/1/interact/voice/rooms/<uuid>/join"},
 "success": true}
```

The room id is recorded server-side in `call_rooms` (`town_hall = true`, partial unique index per
community); it is never found by label. Creating any room named "Town Hall" (any case/spacing)
through the room-create routes is refused (`400 RESERVED_ROOM_LABEL`). Concurrent POSTs race on
the unique index: the loser deletes only the room it just created and returns the winner's. No
code path deletes a room it did not create. The response carries no token: call `join` right
before connecting.

## Who is in a room (member)

`GET /api/v1/communities/{community_id}/calls/rooms/{room}/participants` (member scope
`streaming.calls:read`, membership + flag required, other community's room is 404). Per
participant: per-community pseudonym `participant_id`, `display_name` (ACTIVE members only, else
`"guest"`), `publisher` / `subscriber` / `muted`, `joined_at`, `is_self`. No publisher lists, no
hand internals, no global ids. Camera state and other members' hands are not available to
non-staff (svc-streaming returns only the actor's own hand to a read-scope call).

## Hands

- Member: `POST .../voice/rooms/{room}/raise-hand`, `.../lower-hand`,
  `GET /api/v1/communities/{id}/calls/rooms/{room}/hands/mine` (own state, queue length and
  position only). svc-streaming authorises every hand RPC by actor: the caller must have joined
  the room (call `join` first), and nobody can raise or lower for someone else.
- Admin (`streaming.calls:admin` + community-admin gate): `GET .../raised-hands`,
  `POST .../acknowledge-hand` (`{"user_id": "<participant uuid>"}`), `POST .../reset-hands`.
  The whole queue, acknowledge and reset need the actor to be recorded as room STAFF
  server-side. That happens only when the admin has joined the room through `join`: hub-api then
  mints the join token with the moderate-scope service JWT. A moderation UI must therefore join
  the room before it lists or acknowledges hands; otherwise svc-streaming answers 403.
